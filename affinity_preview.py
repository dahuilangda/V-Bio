from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


class AffinityPreviewError(ValueError):
    """Raised when affinity preview inputs cannot be parsed."""


@dataclass
class AffinityPreviewResult:
    structure_text: str
    structure_format: str
    structure_name: str
    target_structure_text: str
    target_structure_format: str
    ligand_structure_text: str
    ligand_structure_format: str
    ligand_smiles: str
    target_chain_ids: list[str]
    ligand_chain_id: str
    has_ligand: bool
    ligand_is_small_molecule: bool
    supports_activity: bool


def _safe_suffix(filename: str, fallback: str) -> str:
    suffix = Path(str(filename or "").strip()).suffix.lower()
    return suffix or fallback


def _write_temp_text(content: str, suffix: str, base_dir: Path) -> Path:
    fd, path = tempfile.mkstemp(suffix=suffix, dir=str(base_dir))
    try:
        with open(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
    except Exception:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass
        raise
    return Path(path)


def _load_gemmi():
    try:
        import gemmi  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise AffinityPreviewError("gemmi is not available in the current backend environment.") from exc
    return gemmi


def _load_chem():
    try:
        from rdkit import Chem  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise AffinityPreviewError("RDKit is not available in the current backend environment.") from exc
    return Chem


def _read_structure(path: Path) -> Any:
    gemmi = _load_gemmi()
    try:
        structure = gemmi.read_structure(str(path))
    except Exception as exc:  # noqa: BLE001
        raise AffinityPreviewError(f"Failed to parse structure file '{path.name}': {exc}") from exc
    if len(structure) == 0:
        raise AffinityPreviewError(f"Structure file '{path.name}' contains no models.")
    return structure


def _load_ligand_from_file(path: Path) -> Any:
    Chem = _load_chem()
    suffix = path.suffix.lower()

    if suffix in {".sdf", ".sd"}:
        supplier = Chem.SDMolSupplier(str(path), sanitize=False, removeHs=False, strictParsing=False)
        mol = next((item for item in supplier if item is not None), None)
    elif suffix == ".mol2":
        block = path.read_text(encoding="utf-8", errors="ignore")
        try:
            mol = Chem.MolFromMol2Block(block, sanitize=False, removeHs=False, cleanupSubstructures=False)
        except TypeError:
            mol = Chem.MolFromMol2Block(block, sanitize=False, removeHs=False)
    elif suffix == ".mol":
        mol = Chem.MolFromMolFile(str(path), sanitize=False, removeHs=False)
    elif suffix in {".pdb", ".ent"}:
        try:
            mol = Chem.MolFromPDBFile(str(path), removeHs=False, sanitize=False, proximityBonding=False)
        except TypeError:
            mol = Chem.MolFromPDBFile(str(path), removeHs=False, sanitize=False)
    else:
        raise AffinityPreviewError(f"Unsupported ligand format '{suffix}'.")

    if mol is None:
        raise AffinityPreviewError("Failed to parse ligand structure file.")
    if mol.GetNumAtoms() <= 0:
        raise AffinityPreviewError("Ligand file contains no atoms.")
    if mol.GetNumConformers() <= 0:
        raise AffinityPreviewError("Ligand file has no 3D coordinates.")

    return mol


def _to_ligand_smiles(mol: Any) -> str:
    Chem = _load_chem()
    try:
        mol_no_h = Chem.RemoveHs(Chem.Mol(mol), sanitize=False)
    except Exception:
        mol_no_h = Chem.Mol(mol)
    # Keep atom traversal close to input atom order so per-atom confidence can
    # map to 2D atoms deterministically in the UI.
    smiles = Chem.MolToSmiles(mol_no_h, canonical=False, rootedAtAtom=0)
    if not smiles:
        raise AffinityPreviewError("Failed to derive ligand SMILES from uploaded ligand file.")
    return smiles


def _extract_target_chains(structure: Any) -> list[str]:
    if len(structure) == 0:
        return []
    model = structure[0]
    chains: list[str] = []
    for chain in model:
        chain_id = str(chain.name or "").strip()
        if not chain_id:
            continue
        has_polymer = any(residue.het_flag == "A" for residue in chain)
        if has_polymer and chain_id not in chains:
            chains.append(chain_id)
    if chains:
        return chains

    fallback: list[str] = []
    for chain in model:
        chain_id = str(chain.name or "").strip()
        if chain_id and chain_id not in fallback:
            fallback.append(chain_id)
    return fallback


def _is_polymer_chain(chain: Any) -> bool:
    return any(residue.het_flag == "A" for residue in chain)


def _structure_has_polymer(structure: Any) -> bool:
    if len(structure) == 0:
        return False
    return any(_is_polymer_chain(chain) for chain in structure[0])


def _resolve_unique_chain_id(preferred: str, used: set[str]) -> str:
    token = str(preferred or "").strip() or "L"
    if token not in used:
        return token
    index = 1
    while True:
        candidate = f"{token}{index}"
        if candidate not in used:
            return candidate
        index += 1


def _append_ligand_structure(target_structure: Any, ligand_structure: Any) -> list[str]:
    if len(target_structure) == 0:
        raise AffinityPreviewError("Target structure contains no models.")
    if len(ligand_structure) == 0:
        raise AffinityPreviewError("Ligand structure contains no models.")

    target_model = target_structure[0]
    source_model = ligand_structure[0]
    used_chain_ids = {str(chain.name or "").strip() for chain in target_model}
    appended_chain_ids: list[str] = []

    for source_chain in source_model:
        cloned_chain = source_chain.clone()
        chain_id = _resolve_unique_chain_id(str(cloned_chain.name or "").strip(), used_chain_ids)
        cloned_chain.name = chain_id
        target_model.add_chain(cloned_chain)
        used_chain_ids.add(chain_id)
        appended_chain_ids.append(chain_id)

    if not appended_chain_ids:
        raise AffinityPreviewError("Ligand structure contains no chains to append.")
    return appended_chain_ids


def _choose_ligand_chain_id(structure: Any) -> str:
    if len(structure) == 0:
        return "L"
    used = {str(chain.name or "").strip() for chain in structure[0]}
    for candidate in ["L", "Z", "Y", "X", "W", "V"]:
        if candidate not in used:
            return candidate
    index = 1
    while True:
        candidate = f"L{index}"
        if candidate not in used:
            return candidate
        index += 1


def _pick_atom_name(atom: Any, serial: int) -> str:
    return _pick_atom_name_with_index(atom, serial, None)


def _encode_heavy_atom_tag(index: int) -> str:
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    value = max(0, int(index))
    digits = ["0", "0", "0"]
    for i in range(2, -1, -1):
        digits[i] = chars[value % 36]
        value //= 36
    return f"Q{''.join(digits)}"


def _pick_atom_name_with_index(atom: Any, serial: int, smiles_atom_index: int | None) -> str:
    if smiles_atom_index is not None and smiles_atom_index >= 0:
        return _encode_heavy_atom_tag(smiles_atom_index)
    if atom.HasProp("_TriposAtomName"):
        name = atom.GetProp("_TriposAtomName").strip()
        if name:
            return name[:4]
    if atom.HasProp("name"):
        name = atom.GetProp("name").strip()
        if name:
            return name[:4]
    symbol = (atom.GetSymbol() or "X").upper()
    return f"{symbol[:2]}{serial % 100:02d}"[:4]


def _build_heavy_to_smiles_index_map(ligand_mol: Any, ligand_smiles: str) -> dict[int, int]:
    Chem = _load_chem()
    template = Chem.RemoveHs(Chem.Mol(ligand_mol), sanitize=False)
    if template is None or template.GetNumAtoms() <= 0:
        return {}

    tagged = Chem.Mol(template)
    for idx, atom in enumerate(tagged.GetAtoms()):
        atom.SetAtomMapNum(int(idx + 1))
    try:
        tagged_smiles = Chem.MolToSmiles(tagged, canonical=False, rootedAtAtom=0)
        mapped_mol = Chem.MolFromSmiles(tagged_smiles)
    except Exception:
        mapped_mol = None
    if mapped_mol is None or mapped_mol.GetNumAtoms() != template.GetNumAtoms():
        return {}

    mapping: dict[int, int] = {}
    for smiles_idx, atom in enumerate(mapped_mol.GetAtoms()):
        map_num = int(atom.GetAtomMapNum() or 0)
        if map_num <= 0:
            continue
        mapping[int(map_num - 1)] = int(smiles_idx)
    if len(mapping) != template.GetNumAtoms():
        return {}
    return mapping


def _append_ligand_chain(
    structure: Any,
    ligand_mol: Any,
    ligand_chain_id: str,
    heavy_to_smiles_atom_index: dict[int, int] | None = None,
) -> None:
    gemmi = _load_gemmi()
    if len(structure) == 0:
        structure.add_model(gemmi.Model("1"))

    model = structure[0]
    chain = gemmi.Chain(ligand_chain_id)

    residue = gemmi.Residue()
    residue.name = "LIG"
    residue.het_flag = "H"
    residue.seqid = gemmi.SeqId(1, " ")

    conformer = ligand_mol.GetConformer()
    heavy_atom_index = -1
    mapping = heavy_to_smiles_atom_index or {}
    for idx in range(ligand_mol.GetNumAtoms()):
        atom = ligand_mol.GetAtomWithIdx(idx)
        position = conformer.GetAtomPosition(idx)
        gemmi_atom = gemmi.Atom()
        is_hydrogen = int(atom.GetAtomicNum() or 0) == 1
        smiles_atom_index: int | None = None
        if not is_hydrogen:
            heavy_atom_index += 1
            smiles_atom_index = mapping.get(heavy_atom_index, heavy_atom_index)
        gemmi_atom.name = _pick_atom_name_with_index(atom, idx + 1, smiles_atom_index)
        gemmi_atom.element = gemmi.Element(atom.GetSymbol() or "C")
        gemmi_atom.pos = gemmi.Position(float(position.x), float(position.y), float(position.z))
        residue.add_atom(gemmi_atom)

    chain.add_residue(residue)
    model.add_chain(chain)


def _ensure_polymer_sequences(structure: Any) -> None:
    for entity in structure.entities:
        if entity.entity_type.name != "Polymer":
            continue
        if not entity.subchains:
            continue
        sequence: list[str] = []
        for chain in structure[0]:
            for residue in chain:
                if residue.subchain in entity.subchains:
                    sequence.append(residue.name)
        if sequence:
            entity.full_sequence = sequence


def _has_text(value: str | None) -> bool:
    return bool(str(value or "").strip())


def _to_chain_id_text(chain_ids: Iterable[str]) -> str:
    values = [str(item or "").strip() for item in chain_ids]
    values = [item for item in values if item]
    return ",".join(values)


def _serialize_structure(
    structure: Any,
    preferred_format: str,
    temp_path: Path,
    output_stem: str,
) -> tuple[str, str]:
    fmt = "pdb" if str(preferred_format or "").strip().lower() == "pdb" else "cif"
    if fmt == "pdb":
        return structure.make_pdb_string(), "pdb"

    output_path = temp_path / f"{output_stem}.cif"
    document = structure.make_mmcif_document()
    document.write_file(str(output_path))
    return output_path.read_text(encoding="utf-8"), "cif"


def _build_chain_subset_structure(structure: Any, chain_ids: Iterable[str]) -> Any:
    gemmi = _load_gemmi()
    if len(structure) == 0:
        raise AffinityPreviewError("Source structure contains no models.")

    chain_set = {str(chain_id or "").strip() for chain_id in chain_ids if str(chain_id or "").strip()}
    if not chain_set:
        raise AffinityPreviewError("No ligand chain was detected for preview.")

    subset = gemmi.Structure()
    try:
        subset.spacegroup_hm = structure.spacegroup_hm
    except Exception:
        pass
    try:
        subset.cell = structure.cell
    except Exception:
        pass

    model = gemmi.Model("1")
    added = 0
    for chain in structure[0]:
        chain_id = str(chain.name or "").strip()
        if chain_id not in chain_set:
            continue
        model.add_chain(chain.clone())
        added += 1

    if added == 0:
        raise AffinityPreviewError("Unable to build ligand preview chain from uploaded input.")

    subset.add_model(model)
    subset.setup_entities()
    _ensure_polymer_sequences(subset)
    return subset


def build_affinity_preview(
    protein_text: str,
    protein_filename: str,
    ligand_text: str | None = None,
    ligand_filename: str | None = None,
) -> AffinityPreviewResult:
    if not str(protein_text or "").strip():
        raise AffinityPreviewError("Target structure file is empty.")

    protein_suffix = _safe_suffix(protein_filename, ".pdb")

    if protein_suffix not in {".pdb", ".ent", ".cif", ".mmcif"}:
        raise AffinityPreviewError("Target file must be PDB/mmCIF format.")

    has_ligand = _has_text(ligand_text) and _has_text(ligand_filename)
    ligand_suffix = _safe_suffix(ligand_filename or "", ".sdf")
    if has_ligand and ligand_suffix not in {".sdf", ".sd", ".mol2", ".mol", ".pdb", ".ent", ".cif", ".mmcif"}:
        raise AffinityPreviewError("Ligand file must be SDF/MOL2/MOL/PDB/mmCIF format.")

    with tempfile.TemporaryDirectory(prefix="affinity_preview_") as temp_dir:
        temp_path = Path(temp_dir)
        protein_path = _write_temp_text(protein_text, protein_suffix, temp_path)
        structure = _read_structure(protein_path)

        structure.setup_entities()
        target_chain_ids = _extract_target_chains(structure)

        ligand_smiles = ""
        ligand_chain_ids: list[str] = []
        ligand_is_small_molecule = False

        if has_ligand:
            ligand_path = _write_temp_text(str(ligand_text or ""), ligand_suffix, temp_path)
            if ligand_suffix in {".sdf", ".sd", ".mol2", ".mol"}:
                ligand_mol = _load_ligand_from_file(ligand_path)
                ligand_smiles = _to_ligand_smiles(ligand_mol)
                heavy_to_smiles_atom_index = _build_heavy_to_smiles_index_map(ligand_mol, ligand_smiles)
                chain_id = _choose_ligand_chain_id(structure)
                _append_ligand_chain(structure, ligand_mol, chain_id, heavy_to_smiles_atom_index)
                ligand_chain_ids = [chain_id]
                ligand_is_small_molecule = True
            elif ligand_suffix in {".pdb", ".ent"}:
                ligand_structure = _read_structure(ligand_path)
                if _structure_has_polymer(ligand_structure):
                    ligand_chain_ids = _append_ligand_structure(structure, ligand_structure)
                else:
                    try:
                        ligand_mol = _load_ligand_from_file(ligand_path)
                        ligand_smiles = _to_ligand_smiles(ligand_mol)
                        heavy_to_smiles_atom_index = _build_heavy_to_smiles_index_map(ligand_mol, ligand_smiles)
                        chain_id = _choose_ligand_chain_id(structure)
                        _append_ligand_chain(structure, ligand_mol, chain_id, heavy_to_smiles_atom_index)
                        ligand_chain_ids = [chain_id]
                        ligand_is_small_molecule = True
                    except Exception:
                        ligand_chain_ids = _append_ligand_structure(structure, ligand_structure)
            else:
                ligand_structure = _read_structure(ligand_path)
                ligand_chain_ids = _append_ligand_structure(structure, ligand_structure)

        structure.setup_entities()
        _ensure_polymer_sequences(structure)

        # Keep the same visual behavior as direct uploads in prediction workflow:
        # no-ligand preview returns original target text/format;
        # combined preview prefers target's native format.
        target_structure_format = "pdb" if protein_suffix in {".pdb", ".ent"} else "cif"
        target_structure_text = protein_text
        ligand_structure_text = ""
        ligand_structure_format = target_structure_format

        if has_ligand and ligand_chain_ids:
            ligand_only_structure = _build_chain_subset_structure(structure, ligand_chain_ids)
            ligand_structure_text, ligand_structure_format = _serialize_structure(
                ligand_only_structure,
                target_structure_format,
                temp_path,
                "affinity_preview_ligand_only",
            )

        if not has_ligand:
            structure_text = protein_text
            structure_format = target_structure_format
            structure_name = f"affinity_preview_target_only.{'pdb' if structure_format == 'pdb' else 'cif'}"
        else:
            structure_text, structure_format = _serialize_structure(
                structure,
                target_structure_format,
                temp_path,
                "affinity_preview_complex",
            )
            structure_name = f"affinity_preview_complex.{structure_format}"

    if not target_chain_ids:
        target_chain_ids = ["A"]
    ligand_chain_id = _to_chain_id_text(ligand_chain_ids)
    supports_activity = has_ligand and ligand_is_small_molecule and bool(ligand_smiles.strip()) and bool(ligand_chain_id)

    return AffinityPreviewResult(
        structure_text=structure_text,
        structure_format=structure_format,
        structure_name=structure_name,
        target_structure_text=target_structure_text,
        target_structure_format=target_structure_format,
        ligand_structure_text=ligand_structure_text,
        ligand_structure_format=ligand_structure_format,
        ligand_smiles=ligand_smiles,
        target_chain_ids=target_chain_ids,
        ligand_chain_id=ligand_chain_id,
        has_ligand=has_ligand,
        ligand_is_small_molecule=ligand_is_small_molecule,
        supports_activity=supports_activity,
    )
