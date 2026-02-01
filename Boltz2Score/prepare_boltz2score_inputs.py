import re
import tempfile
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

import gemmi
from rdkit import Chem
from rdkit.Chem import rdDetermineBonds

from boltz.data.parse.mmcif import parse_mmcif, ParsedStructure
from boltz.data.parse.pdb import parse_pdb
from boltz.data.mol import load_canonicals
from boltz.main import get_cache_path

STANDARD_RESIDUES = {
    "ALA",
    "ARG",
    "ASN",
    "ASP",
    "CYS",
    "GLN",
    "GLU",
    "GLY",
    "HIS",
    "ILE",
    "LEU",
    "LYS",
    "MET",
    "PHE",
    "PRO",
    "SER",
    "THR",
    "TRP",
    "TYR",
    "VAL",
    "MSE",
    "SEC",
    "PYL",
    "UNK",
    "A",
    "C",
    "G",
    "T",
    "U",
    "DA",
    "DC",
    "DG",
    "DT",
}

SKIP_RESIDUES = {
    "HOH",
    "WAT",
    "H2O",
    "NA",
    "CL",
    "MG",
    "CA",
    "K",
    "ZN",
    "FE",
    "SO4",
    "PO4",
    "ACE",
    "NME",
}


def sanitize_atom_name(name: str) -> str:
    if name is None:
        return "X"
    cleaned = str(name).strip().upper()
    if not cleaned:
        return "X"
    safe_chars = []
    for ch in cleaned:
        code = ord(ch)
        if 32 <= code <= 95:
            safe_chars.append(ch)
        else:
            safe_chars.append("X")
    return ("".join(safe_chars)[:4] or "X")


def _extract_ligand_name_from_error(error_message: str) -> Optional[str]:
    patterns = [
        r"CCD component ([A-Za-z0-9]+) not found",
        r"CCD component '([^']+)' not found",
        r"CCD component \"([^\"]+)\" not found",
    ]
    for pattern in patterns:
        match = re.search(pattern, error_message)
        if match:
            return match.group(1)
    return None


def _is_ligand_residue(residue: gemmi.Residue) -> bool:
    resname = residue.name.strip()
    if not resname:
        return False
    if resname in STANDARD_RESIDUES or resname in SKIP_RESIDUES:
        return False
    return True


def _iter_ligand_residues(structure: gemmi.Structure) -> Iterable[gemmi.Residue]:
    for model in structure:
        for chain in model:
            for residue in chain:
                if _is_ligand_residue(residue):
                    yield residue


def _ccd_matches_residue(mol: Chem.Mol, residue: gemmi.Residue) -> bool:
    if mol is None:
        return False
    mol_atom_names = set()
    for atom in mol.GetAtoms():
        if atom.HasProp("name"):
            mol_atom_names.add(sanitize_atom_name(atom.GetProp("name")))
    if not mol_atom_names:
        return False

    residue_atom_names = set()
    for atom in residue:
        atom_name = atom.name if hasattr(atom, "name") else None
        if not atom_name:
            atom_name = f"{atom.element.name}{len(residue_atom_names) + 1}"
        residue_atom_names.add(sanitize_atom_name(atom_name))

    return residue_atom_names.issubset(mol_atom_names)


def _build_rdkit_mol_from_residue(
    residue: gemmi.Residue, ligand_name: str
) -> Optional[Chem.Mol]:
    from tempfile import NamedTemporaryFile

    atom_names = []
    with NamedTemporaryFile(suffix=".sdf", delete=False, mode="w") as temp_sdf:
        temp_sdf.write(f"{ligand_name}\n")
        temp_sdf.write("  Generated from structure coordinates\n")
        temp_sdf.write("\n")
        atom_count = len(residue)
        temp_sdf.write(f"{atom_count:3d}  0  0  0  0  0  0  0  0  0  0 V2000\n")
        for i, atom in enumerate(residue):
            pos = atom.pos
            element = atom.element.name
            atom_name = atom.name if hasattr(atom, "name") else f"{element}{i + 1}"
            atom_names.append(atom_name)
            temp_sdf.write(
                f"{pos.x:10.4f}{pos.y:10.4f}{pos.z:10.4f} {element:2s}  0  0  0  0  0  0  0  0  0  0  0  0\n"
            )
        temp_sdf.write("M  END\n")
        temp_sdf.write("$$$$\n")
        temp_path = temp_sdf.name

    mol = Chem.MolFromMolFile(temp_path, sanitize=False)
    if mol is None:
        return None

    try:
        rdDetermineBonds.DetermineBonds(mol, charge=0)
        Chem.SanitizeMol(mol, sanitizeOps=Chem.SANITIZE_ALL)
    except Exception:
        try:
            mol = Chem.MolFromMolFile(temp_path, sanitize=False)
            sanitize_ops = (
                Chem.SANITIZE_ALL
                ^ Chem.SANITIZE_PROPERTIES
                ^ Chem.SANITIZE_KEKULIZE
            )
            Chem.SanitizeMol(mol, sanitizeOps=sanitize_ops)
        except Exception:
            mol = Chem.MolFromMolFile(temp_path, sanitize=False)

    if mol is None:
        return None

    mol.SetProp("_Name", ligand_name)
    mol.SetProp("name", ligand_name)
    mol.SetProp("id", ligand_name)
    mol.SetProp("resname", ligand_name)

    for i, atom in enumerate(mol.GetAtoms()):
        if i < len(atom_names):
            atom.SetProp("name", sanitize_atom_name(atom_names[i]))
        else:
            atom.SetProp("name", sanitize_atom_name(f"{atom.GetSymbol()}{i + 1}"))

    try:
        Path(temp_path).unlink(missing_ok=True)
    except Exception:
        pass

    return mol


def _read_structure(path: Path) -> gemmi.Structure:
    if path.suffix.lower() == ".cif":
        doc = gemmi.cif.read(str(path))
        block = doc[0]
        structure = gemmi.make_structure_from_block(block)
        structure.setup_entities()
        return structure
    if path.suffix.lower() == ".pdb":
        structure = gemmi.read_structure(str(path))
        structure.setup_entities()
        return structure
    raise ValueError(f"Unsupported file format: {path}")


def _build_entity_sequences(structure: gemmi.Structure) -> Dict[str, list[str]]:
    sequences: Dict[str, list[str]] = {}
    subchains = {c.subchain_id(): c for c in structure[0].subchains()}

    for entity in structure.entities:
        if entity.entity_type.name != "Polymer":
            continue
        seq = list(entity.full_sequence) if entity.full_sequence else []
        if not seq:
            for subchain_id in entity.subchains:
                raw_chain = subchains.get(subchain_id)
                if raw_chain is None:
                    continue
                seq = [res.name for res in raw_chain]
                if seq:
                    break
        if seq:
            sequences[str(entity.name)] = seq
    return sequences


def _write_mmcif_with_sequences(structure: gemmi.Structure) -> Path:
    # Ensure entity subchain mappings include current subchains (e.g. Axp/Bxp)
    subchain_ids = [span.subchain_id() for span in structure[0].subchains()]
    polymer_entities = [e for e in structure.entities if e.entity_type.name == "Polymer"]

    for chain in structure[0]:
        try:
            entity_id = int(chain.entity_id)
        except Exception:
            entity_id = None
        target_entity = None
        if entity_id is not None and 0 <= entity_id < len(structure.entities):
            target_entity = structure.entities[entity_id]
        if target_entity is None:
            continue
        for residue in chain:
            if residue.subchain and residue.subchain not in target_entity.subchains:
                target_entity.subchains.append(residue.subchain)

    for subchain_id in subchain_ids:
        if any(subchain_id in e.subchains for e in structure.entities):
            continue

        target_entity = None
        base_name = subchain_id[:-2] if subchain_id.endswith("xp") else subchain_id

        for entity in structure.entities:
            if base_name in entity.subchains or subchain_id in entity.subchains:
                target_entity = entity
                break

        if target_entity is None and len(polymer_entities) == 1:
            target_entity = polymer_entities[0]

        if target_entity is not None and subchain_id not in target_entity.subchains:
            target_entity.subchains.append(subchain_id)

    doc = structure.make_mmcif_document()
    block = doc[0]

    sequences = _build_entity_sequences(structure)
    if sequences:
        loop = block.init_loop(
            "_entity_poly_seq.",
            ["entity_id", "num", "mon_id"],
        )
        for entity_id, seq in sequences.items():
            for idx, mon in enumerate(seq, start=1):
                loop.add_row([entity_id, str(idx), mon])

    # Ensure entity subchain table includes any synthesized subchains
    subchain_ids = [span.subchain_id() for span in structure[0].subchains()]
    if subchain_ids:
        loop = block.init_loop(
            "_entity_poly.",
            [
                "entity_id",
                "type",
                "nstd_linkage",
                "nstd_monomer",
                "pdbx_seq_one_letter_code",
                "pdbx_seq_one_letter_code_can",
                "pdbx_strand_id",
            ],
        )
        entities = [e for e in structure.entities if e.entity_type.name == "Polymer"]
        for entity in entities:
            entity_id = str(entity.name)
            seq = sequences.get(entity_id, [])
            if seq:
                one_letter = gemmi.one_letter_code(seq)
            else:
                one_letter = "?"

            strands = [sc for sc in entity.subchains if sc in subchain_ids]
            if not strands and len(entities) == 1:
                strands = subchain_ids
            if not strands:
                continue
            loop.add_row(
                [
                    entity_id,
                    "polypeptide(L)",
                    "no",
                    "no",
                    one_letter,
                    one_letter,
                    ",".join(strands),
                ]
            )

    temp = tempfile.NamedTemporaryFile(suffix=".cif", delete=False)
    temp_path = Path(temp.name)
    temp.close()
    doc.write_file(str(temp_path))
    return temp_path


def prepare_structure_inputs(
    input_path: Path,
) -> Tuple[ParsedStructure, Dict[str, Chem.Mol], gemmi.Structure]:
    cache_dir = Path(get_cache_path())
    mol_dir = cache_dir / "mols"
    canonicals = load_canonicals(mol_dir)

    structure = _read_structure(input_path)
    extra_mols: Dict[str, Chem.Mol] = {}

    for residue in _iter_ligand_residues(structure):
        resname = residue.name.strip()
        if not resname:
            continue
        needs_custom = resname not in canonicals
        if not needs_custom:
            needs_custom = not _ccd_matches_residue(canonicals[resname], residue)
        if needs_custom:
            mol = _build_rdkit_mol_from_residue(residue, resname)
            if mol is not None:
                extra_mols[resname] = mol

    mols = dict(canonicals)
    mols.update(extra_mols)

    normalized_cif = _write_mmcif_with_sequences(structure)
    parsed = None
    try:
        for _ in range(5):
            try:
                parsed = parse_mmcif(
                    path=str(normalized_cif),
                    mols=mols,
                    moldir=str(mol_dir),
                    compute_interfaces=False,
                )
                break
            except ValueError as exc:
                ligand_name = _extract_ligand_name_from_error(str(exc))
                if not ligand_name:
                    raise
                residue = None
                for res in _iter_ligand_residues(structure):
                    if res.name.strip() == ligand_name:
                        residue = res
                        break
                if residue is None:
                    raise
                mol = _build_rdkit_mol_from_residue(residue, ligand_name)
                if mol is None:
                    raise
                mols[ligand_name] = mol
                extra_mols[ligand_name] = mol
    finally:
        try:
            normalized_cif.unlink(missing_ok=True)
        except Exception:
            pass

    if parsed is None:
        raise RuntimeError("Failed to parse structure after custom ligand attempts.")

    return parsed, extra_mols, structure
