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
    """Return True if CCD atom names can map to residue atom names.

    Boltz2 maps ligand coordinates by atom *name*. If names do not match,
    coordinates will be dropped (atoms marked not present), which degrades
    confidence on small molecules. We therefore require name-level agreement
    (with light normalization) rather than element-only matching.
    """
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
    ccd_elements = []
    for atom in ref_mol.GetAtoms():
        if atom.HasProp("name"):
            name = atom.GetProp("name")
            ccd_names.append(name)
            ccd_elements.append(atom.GetSymbol())
        elif atom.HasProp("atomName"):
            name = atom.GetProp("atomName")
            ccd_names.append(name)
            ccd_elements.append(atom.GetSymbol())
        else:
            ccd_names.append(atom.GetSymbol())
            ccd_elements.append(atom.GetSymbol())

    from collections import Counter
    import re

    def _norm(name: str) -> str:
        # Normalize common PDB/CCD naming differences without losing identity.
        norm = re.sub(r"[^A-Za-z0-9]", "", name.strip().upper())
        norm = norm.lstrip("0123456789")
        return norm

    res_counter = Counter(res_names)
    ccd_counter = Counter(ccd_names)
    res_norm_counter = Counter(_norm(n) for n in res_names)
    ccd_norm_counter = Counter(_norm(n) for n in ccd_names)

    # Try exact atom name matching first
    exact_match = True
    for name, count in res_counter.items():
        if ccd_counter.get(name, 0) < count:
            exact_match = False
            break

    if exact_match:
        return True

    # Try normalized name matching (handles simple formatting differences).
    norm_match = True
    for name, count in res_norm_counter.items():
        if ccd_norm_counter.get(name, 0) < count:
            norm_match = False
            break

    return norm_match


def _has_non_single_bonds(mol: Chem.Mol) -> bool:
    return any(
        bond.GetBondType() not in (Chem.rdchem.BondType.SINGLE,)
        for bond in mol.GetBonds()
    )


def _assign_bond_orders(mol: Chem.Mol) -> Chem.Mol:
    """Try to assign bond orders; fall back gracefully if not possible."""
    base = Chem.Mol(mol)

    try:
        candidate = Chem.Mol(base)
        rdDetermineBonds.DetermineBonds(candidate)
        if _has_non_single_bonds(candidate):
            return candidate
    except Exception:
        pass

    # If all bonds are single, try a small charge sweep to recover bond orders.
    for charge in (-8, -6, -4, -2, 0, 2, 4, 6, 8):
        try:
            candidate = Chem.Mol(base)
            rdDetermineBonds.DetermineBonds(candidate, charge=charge)
        except Exception:
            continue
        if _has_non_single_bonds(candidate):
            return candidate

    return base


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
    mol = _assign_bond_orders(mol)

    return mol


def _extract_pdb_ligand_block(
    pdb_lines: list[str],
    resname: str,
    chain_id: str,
    resseq: int,
    icode: str,
) -> tuple[str, list[str]] | None:
    """Extract a PDB block (HETATM + CONECT) for a specific ligand residue."""
    het_lines = []
    serials: set[int] = set()
    icode_val = icode.strip() if icode else ""

    for line in pdb_lines:
        if not line.startswith(("HETATM", "ATOM")):
            continue
        if line[17:20].strip() != resname:
            continue
        if line[21].strip() != chain_id:
            continue
        try:
            line_resseq = int(line[22:26].strip())
        except ValueError:
            continue
        if line_resseq != resseq:
            continue
        line_icode = line[26].strip() if len(line) > 26 else ""
        if icode_val and line_icode != icode_val:
            continue

        het_lines.append(line)
        try:
            serial = int(line[6:11].strip())
        except ValueError:
            continue
        serials.add(serial)

    if not het_lines:
        return None

    conect_lines = []
    for line in pdb_lines:
        if not line.startswith("CONECT"):
            continue
        raw_numbers = [line[6:11], line[11:16], line[16:21], line[21:26], line[26:31]]
        numbers: list[int] = []
        for raw in raw_numbers:
            raw = raw.strip()
            if not raw:
                continue
            try:
                numbers.append(int(raw))
            except ValueError:
                continue
        if not numbers:
            continue
        if numbers[0] not in serials:
            continue
        if any(num in serials for num in numbers[1:]):
            conect_lines.append(line)

    block_lines = het_lines + conect_lines + ["END"]
    return "\n".join(block_lines), het_lines


def _build_custom_ligand_mol_from_pdb(
    pdb_block: str,
    het_lines: list[str],
    resname: str,
) -> Chem.Mol | None:
    """Build an RDKit molecule from a PDB ligand block (uses CONECT if present)."""
    mol = Chem.MolFromPDBBlock(pdb_block, removeHs=False, sanitize=False)
    if mol is None:
        return None

    mol = _assign_bond_orders(mol)
    try:
        Chem.SanitizeMol(mol)
    except Exception:
        pass

    atom_names = [line[12:16].strip() for line in het_lines]
    if atom_names and len(atom_names) == mol.GetNumAtoms():
        for atom, name in zip(mol.GetAtoms(), atom_names):
            if name:
                atom.SetProp("name", name)

    mol.SetProp("_Name", resname)
    mol.SetProp("name", resname)
    mol.SetProp("id", resname)
    return mol


def _mol_has_atom_names(mol: Chem.Mol | None) -> bool:
    if mol is None or mol.GetNumAtoms() == 0:
        return False
    heavy_atoms = [atom for atom in mol.GetAtoms() if atom.GetAtomicNum() > 1]
    atoms_to_check = heavy_atoms or list(mol.GetAtoms())
    return all(atom.HasProp("name") for atom in atoms_to_check)


def _load_mol_from_cache(mol_dir: Path, code: str) -> Chem.Mol | None:
    mol_path = mol_dir / f"{code}.pkl"
    if not mol_path.exists():
        return None
    with mol_path.open("rb") as f:
        return pickle.load(f)


def _get_cached_mol(mols: dict, mol_dir: Path, code: str) -> Chem.Mol | None:
    mol = mols.get(code)
    if _mol_has_atom_names(mol):
        return mol

    loaded = _load_mol_from_cache(mol_dir, code)
    if loaded is not None:
        mols[code] = loaded
        return loaded

    if mol is not None:
        mols.pop(code, None)
    return None


def _collect_custom_ligands(path: Path, mols: dict, mol_dir: Path) -> dict:
    """Collect custom ligand definitions that should override CCD entries."""
    structure = gemmi.read_structure(str(path))
    structure.setup_entities()

    entity_types = {
        sub: ent.entity_type.name
        for ent in structure.entities
        for sub in ent.subchains
    }

    pdb_lines = None
    if path.suffix.lower() in {".pdb", ".ent"}:
        try:
            pdb_lines = path.read_text().splitlines()
        except Exception:
            pdb_lines = None

    custom_mols: dict = {}
    for chain in structure[0]:
        for residue in chain:
            sub = residue.subchain
            if entity_types.get(sub) not in {"NonPolymer", "Branched"}:
                continue
            resname = residue.name.strip()
            if resname in WATER_RESNAMES or resname in ION_RESNAMES:
                continue

            ccd_mol = _get_cached_mol(mols, mol_dir, resname)
            ccd_matches = _ccd_matches_residue(residue, ccd_mol) if ccd_mol else False

            if (
                resname in GENERIC_LIGAND_NAMES
                or resname not in mols
                or not ccd_matches
            ):
                if resname not in custom_mols:
                    custom_mol = None
                    if pdb_lines:
                        extracted = _extract_pdb_ligand_block(
                            pdb_lines=pdb_lines,
                            resname=resname,
                            chain_id=chain.name,
                            resseq=residue.seqid.num,
                            icode=residue.seqid.icode,
                        )
                        if extracted:
                            pdb_block, het_lines = extracted
                            custom_mol = _build_custom_ligand_mol_from_pdb(
                                pdb_block=pdb_block,
                                het_lines=het_lines,
                                resname=resname,
                            )
                    if custom_mol is None:
                        custom_mol = _build_custom_ligand_mol(residue)
                    custom_mols[resname] = custom_mol

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
    if not _mol_has_atom_names(mols.get("ALA")):
        # Some cache builds ship ccd.pkl without atom name properties.
        # Fall back to per-residue cache files that retain atom names.
        print(
            "[Warning] CCD cache is missing atom names; falling back to "
            "per-residue molecule files."
        )
        mols = {}

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
            custom_mols = _collect_custom_ligands(path, mols, mol_dir)
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
