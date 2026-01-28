#!/usr/bin/env python3
"""Prepare Boltz2Score inputs from PDB/mmCIF structures."""

from __future__ import annotations

import argparse
import pickle
from dataclasses import replace
from pathlib import Path
import tempfile
from typing import Iterable, List, Tuple

from rdkit import Chem
from rdkit.Chem import rdDetermineBonds

from boltz.main import get_cache_path
import gemmi
from boltz.data import const
from boltz.data.parse.pdb import parse_pdb
from boltz.data.parse.mmcif import parse_mmcif
from boltz.data.types import ChainInfo, Manifest, Record


STRUCT_EXTS = {".pdb", ".ent", ".cif", ".mmcif"}

# Generic ligand names that should prefer custom definitions
GENERIC_LIGAND_NAMES = {"LIG", "UNK", "UNL"}

# Basic residue name filters
WATER_RESNAMES = {"HOH", "WAT", "H2O"}
ION_RESNAMES = {
    "NA", "CL", "MG", "CA", "K", "ZN", "FE", "MN", "CU", "CO", "NI",
    "CD", "HG", "SR", "BA", "CS", "LI", "BR", "I",
}


def _ccd_matches_residue(residue: gemmi.Residue, ccd_mol: Chem.Mol) -> bool:
    """Return True if CCD atom names can map to residue atom names."""
    if ccd_mol is None:
        return False

    res_names = [atom.name.strip() for atom in residue if atom.name.strip()]
    if not res_names:
        return False

    # Compare against heavy-atom CCD names (Boltz removes H during parsing).
    try:
        ref_mol = Chem.RemoveHs(ccd_mol, sanitize=False)
    except Exception:
        ref_mol = ccd_mol

    ccd_names = []
    for atom in ref_mol.GetAtoms():
        if atom.HasProp("name"):
            ccd_names.append(atom.GetProp("name"))
        elif atom.HasProp("atomName"):
            ccd_names.append(atom.GetProp("atomName"))
        else:
            ccd_names.append(atom.GetSymbol())

    from collections import Counter

    res_counter = Counter(res_names)
    ccd_counter = Counter(ccd_names)
    for name, count in res_counter.items():
        if ccd_counter.get(name, 0) < count:
            return False
    return True


def _build_custom_ligand_mol(residue: gemmi.Residue) -> Chem.Mol:
    """Create a minimal RDKit molecule from a gemmi residue."""
    rw_mol = Chem.RWMol()
    atom_names = []
    for atom in residue:
        element = atom.element.name if atom.element.name else atom.name[:1]
        rd_atom = Chem.Atom(element)
        idx = rw_mol.AddAtom(rd_atom)
        rw_mol.GetAtomWithIdx(idx).SetProp("name", atom.name.strip())
        atom_names.append(atom.name.strip())

    mol = rw_mol.GetMol()
    conf = Chem.Conformer(mol.GetNumAtoms())
    for i, atom in enumerate(residue):
        pos = atom.pos
        conf.SetAtomPosition(i, (pos.x, pos.y, pos.z))
    mol.AddConformer(conf, assignId=True)

    mol.SetProp("_Name", residue.name)
    mol.SetProp("name", residue.name)
    mol.SetProp("id", residue.name)

    # Try to infer bonds from coordinates; fall back to no bonds on failure.
    try:
        rdDetermineBonds.DetermineBonds(mol)
    except Exception:
        pass

    return mol


def _collect_custom_ligands(path: Path, mols: dict) -> dict:
    """Collect custom ligand definitions that should override CCD entries."""
    structure = gemmi.read_structure(str(path))
    structure.setup_entities()

    entity_types = {
        sub: ent.entity_type.name
        for ent in structure.entities
        for sub in ent.subchains
    }

    custom_mols: dict = {}
    for chain in structure[0]:
        for residue in chain:
            sub = residue.subchain
            if entity_types.get(sub) not in {"NonPolymer", "Branched"}:
                continue
            resname = residue.name.strip()
            if resname in WATER_RESNAMES or resname in ION_RESNAMES:
                continue

            ccd_mol = mols.get(resname)
            ccd_matches = _ccd_matches_residue(residue, ccd_mol) if ccd_mol else False

            if (
                resname in GENERIC_LIGAND_NAMES
                or resname not in mols
                or not ccd_matches
            ):
                if resname not in custom_mols:
                    custom_mols[resname] = _build_custom_ligand_mol(residue)

    return custom_mols


def _iter_struct_files(input_dir: Path, recursive: bool) -> List[Path]:
    if recursive:
        files = [p for p in input_dir.rglob("*") if p.suffix.lower() in STRUCT_EXTS]
    else:
        files = [p for p in input_dir.iterdir() if p.suffix.lower() in STRUCT_EXTS]
    return sorted(files)


def _load_ccd(ccd_path: Path) -> dict:
    if not ccd_path.exists():
        raise FileNotFoundError(f"CCD file not found: {ccd_path}")
    with ccd_path.open("rb") as f:
        return pickle.load(f)


def _parse_structure(path: Path, mols: dict, mol_dir: Path):
    suffix = path.suffix.lower()
    if suffix in {".pdb", ".ent"}:
        try:
            return parse_pdb(
                str(path),
                mols=mols,
                moldir=str(mol_dir),
                use_assembly=False,
                compute_interfaces=False,
            )
        except Exception:
            return _parse_pdb_with_sequence(
                path=path,
                mols=mols,
                mol_dir=mol_dir,
            )
    if suffix in {".cif", ".mmcif"}:
        return parse_mmcif(
            str(path),
            mols=mols,
            moldir=str(mol_dir),
            use_assembly=False,
            compute_interfaces=False,
        )
    raise ValueError(f"Unsupported structure format: {path}")


def _parse_pdb_with_sequence(path: Path, mols: dict, mol_dir: Path):
    """Parse PDB by injecting polymer sequences into a temporary mmCIF."""
    structure = gemmi.read_structure(str(path))
    structure.setup_entities()

    # Fill missing polymer sequences (common when PDB lacks SEQRES)
    for entity in structure.entities:
        if entity.entity_type.name != "Polymer":
            continue
        if not entity.subchains:
            continue
        # Use the first subchain to define the entity sequence
        subchain_id = entity.subchains[0]
        seq = []
        for chain in structure[0]:
            for res in chain:
                if res.subchain == subchain_id:
                    seq.append(res.name)
        if seq:
            entity.full_sequence = seq

    # Match the subchain renaming logic in boltz.data.parse.pdb
    subchain_counts, subchain_renaming = {}, {}
    for chain in structure[0]:
        subchain_counts[chain.name] = 0
        for res in chain:
            if res.subchain not in subchain_renaming:
                subchain_renaming[res.subchain] = chain.name + str(
                    subchain_counts[chain.name] + 1
                )
                subchain_counts[chain.name] += 1
            res.subchain = subchain_renaming[res.subchain]
    for entity in structure.entities:
        entity.subchains = [subchain_renaming[sub] for sub in entity.subchains]

    doc = structure.make_mmcif_document()
    with tempfile.NamedTemporaryFile(suffix=".cif") as tmp_cif:
        doc.write_file(tmp_cif.name)
        return parse_mmcif(
            tmp_cif.name,
            mols=mols,
            moldir=str(mol_dir),
            use_assembly=False,
            compute_interfaces=False,
        )


def _build_record(target_id: str, parsed) -> Record:
    chains = parsed.data.chains
    chain_infos = []
    for chain in chains:
        chain_infos.append(
            ChainInfo(
                chain_id=int(chain["asym_id"]),
                chain_name=str(chain["name"]),
                mol_type=int(chain["mol_type"]),
                cluster_id=-1,
                msa_id=-1,
                num_residues=int(chain["res_num"]),
                valid=True,
                entity_id=int(chain["entity_id"]),
            )
        )

    struct_info = parsed.info
    if struct_info.num_chains is None:
        struct_info = replace(struct_info, num_chains=len(chains))

    return Record(
        id=target_id,
        structure=struct_info,
        chains=chain_infos,
        interfaces=[],
        inference_options=None,
        templates=None,
        md=None,
        affinity=None,
    )


def prepare_inputs(
    input_dir: Path,
    out_dir: Path,
    cache_dir: Path,
    recursive: bool,
) -> Tuple[Manifest, List[Path]]:
    struct_dir = out_dir / "processed" / "structures"
    records_dir = out_dir / "processed" / "records"
    msa_dir = out_dir / "processed" / "msa"
    mols_dir = out_dir / "processed" / "mols"

    struct_dir.mkdir(parents=True, exist_ok=True)
    records_dir.mkdir(parents=True, exist_ok=True)
    msa_dir.mkdir(parents=True, exist_ok=True)
    mols_dir.mkdir(parents=True, exist_ok=True)

    mol_dir = cache_dir / "mols"
    ccd_path = cache_dir / "ccd.pkl"

    if not mol_dir.exists():
        raise FileNotFoundError(
            f"Molecule directory not found: {mol_dir}. Please download Boltz2 assets."
        )

    # Ensure RDKit pickle properties are available
    Chem.SetDefaultPickleProperties(Chem.PropertyPickleOptions.AllProps)

    mols = _load_ccd(ccd_path)

    struct_files = _iter_struct_files(input_dir, recursive)
    if not struct_files:
        raise FileNotFoundError(f"No structure files found in {input_dir}")

    records: List[Record] = []
    failed: List[Path] = []
    for path in struct_files:
        target_id = path.stem
        custom_mols = {}
        overridden = {}
        try:
            custom_mols = _collect_custom_ligands(path, mols)
            if custom_mols:
                for name, mol in custom_mols.items():
                    if name in mols:
                        overridden[name] = mols[name]
                    mols[name] = mol

            parsed = _parse_structure(path, mols=mols, mol_dir=mol_dir)
            record = _build_record(target_id, parsed)
            # Dump structure and record
            parsed.data.dump(struct_dir / f"{target_id}.npz")
            record.dump(records_dir / f"{target_id}.json")

            # Collect extra molecules (ligands) not guaranteed in mol cache
            extra_mols = {}
            if custom_mols:
                extra_mols.update(custom_mols)
            if extra_mols:
                with (mols_dir / f"{target_id}.pkl").open("wb") as f:
                    pickle.dump(extra_mols, f)

            records.append(record)
        except Exception as exc:  # noqa: BLE001
            print(f"[Warning] Failed to process {path}: {exc}")
            failed.append(path)
        finally:
            # Always restore CCD entries if we overrode them for this structure
            if custom_mols:
                for name in custom_mols:
                    if name in overridden:
                        mols[name] = overridden[name]
                    else:
                        mols.pop(name, None)

    manifest = Manifest(records=records)
    manifest.dump(out_dir / "processed" / "manifest.json")

    return manifest, failed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare Boltz2Score inputs from PDB/mmCIF structures."
    )
    parser.add_argument("--input_dir", required=True, type=str, help="Input directory")
    parser.add_argument(
        "--output_dir",
        required=True,
        type=str,
        help="Output directory for processed inputs",
    )
    parser.add_argument(
        "--cache",
        type=str,
        default=None,
        help="Boltz cache directory (default: BOLTZ_CACHE or ~/.boltz)",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Recursively scan input_dir for structures",
    )

    args = parser.parse_args()

    input_dir = Path(args.input_dir).expanduser().resolve()
    out_dir = Path(args.output_dir).expanduser().resolve()
    cache_dir = Path(args.cache or get_cache_path()).expanduser().resolve()

    out_dir.mkdir(parents=True, exist_ok=True)

    manifest, failed = prepare_inputs(
        input_dir=input_dir,
        out_dir=out_dir,
        cache_dir=cache_dir,
        recursive=args.recursive,
    )

    print(f"Prepared {len(manifest.records)} inputs in {out_dir / 'processed'}")
    if failed:
        print(f"Failed to process {len(failed)} files. See warnings above.")


if __name__ == "__main__":
    main()
