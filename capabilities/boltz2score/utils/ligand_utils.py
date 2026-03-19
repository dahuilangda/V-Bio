from __future__ import annotations

import re
from pathlib import Path

import gemmi
from rdkit import Chem


def _to_base36(value: int) -> str:
    digits = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if value <= 0:
        return "0"
    out = []
    num = value
    while num:
        num, rem = divmod(num, 36)
        out.append(digits[rem])
    return "".join(reversed(out))


def normalize_atom_name(name: str) -> str:
    return "".join(ch for ch in name.strip().upper() if ch.isalnum())


def element_prefix_for_atom(atom: Chem.Atom) -> str:
    symbol = normalize_atom_name(atom.GetSymbol() or "")
    if not symbol:
        return "X"
    if len(symbol) >= 2 and symbol[0].isalpha() and symbol[1].isalpha():
        return symbol[:2]
    return symbol[:1]


def generate_atom_name(prefix: str, serial: int) -> str:
    prefix = normalize_atom_name(prefix or "X")
    if len(prefix) >= 2:
        if serial > 36 * 36:
            raise ValueError(f"Too many atoms for prefix {prefix[:2]!r}.")
        return f"{prefix[:2]}{_to_base36(serial).rjust(2, '0')[-2:]}"
    if serial > 36 * 36 * 36:
        raise ValueError(f"Too many atoms for prefix {prefix[:1]!r}.")
    return f"{prefix[:1] or 'X'}{_to_base36(serial).rjust(3, '0')[-3:]}"


def extract_atom_preferred_name(atom: Chem.Atom) -> str:
    if atom.HasProp("_original_atom_name"):
        return atom.GetProp("_original_atom_name")
    if atom.HasProp("name"):
        return atom.GetProp("name")
    monomer_info = atom.GetMonomerInfo()
    if monomer_info is not None and hasattr(monomer_info, "GetName"):
        try:
            name = monomer_info.GetName()
            if name:
                return str(name)
        except Exception:
            pass
    return ""


def ensure_unique_ligand_atom_names(mol: Chem.Mol) -> tuple[Chem.Mol, int]:
    used: set[str] = set()
    serial_by_prefix: dict[str, int] = {}
    renamed = 0

    for atom in mol.GetAtoms():
        preferred_raw = extract_atom_preferred_name(atom)
        normalized = normalize_atom_name(preferred_raw or "")

        candidate = None
        if normalized and len(normalized) <= 4 and normalized not in used:
            candidate = normalized
        else:
            prefix = element_prefix_for_atom(atom)
            serial = serial_by_prefix.get(prefix, 1)
            while True:
                generated = generate_atom_name(prefix, serial)
                serial += 1
                if generated not in used:
                    candidate = generated
                    break
            serial_by_prefix[prefix] = serial
            renamed += 1

        used.add(candidate)
        if preferred_raw:
            atom.SetProp("_source_atom_name", preferred_raw)
        atom.SetProp("_original_atom_name", candidate)
        atom.SetProp("name", candidate)

    return mol, renamed


def snapshot_conformer_positions(mol: Chem.Mol) -> list[tuple[float, float, float]]:
    if mol.GetNumConformers() == 0:
        return []
    conf = mol.GetConformer()
    return [
        (float(conf.GetAtomPosition(i).x), float(conf.GetAtomPosition(i).y), float(conf.GetAtomPosition(i).z))
        for i in range(mol.GetNumAtoms())
    ]


def restore_conformer_positions(mol: Chem.Mol, positions: list[tuple[float, float, float]]) -> None:
    if not positions or mol.GetNumConformers() == 0 or len(positions) != mol.GetNumAtoms():
        return
    conf = mol.GetConformer()
    for idx, (x, y, z) in enumerate(positions):
        conf.SetAtomPosition(idx, (x, y, z))


def load_ligand_from_file(ligand_path: Path) -> Chem.Mol:
    ligand_path = Path(ligand_path)

    if ligand_path.suffix.lower() == ".mol2":
        with open(ligand_path) as handle:
            mol2_content = handle.read()

        try:
            mol = Chem.MolFromMol2Block(
                mol2_content,
                sanitize=False,
                removeHs=False,
                cleanupSubstructures=False,
            )
        except TypeError:
            mol = Chem.MolFromMol2Block(mol2_content, sanitize=False, removeHs=False)
        if mol is None:
            raise ValueError(f"Failed to read MOL2 file: {ligand_path}")

        atom_section_started = False
        atom_data = []
        for line in mol2_content.split("\n"):
            if line.startswith("@<TRIPOS>ATOM"):
                atom_section_started = True
                continue
            if atom_section_started:
                if line.startswith("@<TRIPOS>") or not line.strip():
                    break
                parts = line.split()
                if len(parts) >= 7:
                    atom_data.append((int(parts[0]), parts[1]))

        for atom_idx_mol2, atom_name in atom_data:
            rdkit_idx = atom_idx_mol2 - 1
            if rdkit_idx < mol.GetNumAtoms():
                atom = mol.GetAtomWithIdx(rdkit_idx)
                atom.SetProp("_original_atom_name", atom_name)
                atom.SetProp("name", atom_name)

        original_positions = snapshot_conformer_positions(mol)
        if not original_positions:
            raise ValueError(f"MOL2 ligand has no 3D conformer: {ligand_path}")
        mol, renamed = ensure_unique_ligand_atom_names(mol)
        restore_conformer_positions(mol, original_positions)
        print(f"Loaded ligand from MOL2: {mol.GetNumAtoms()} atoms (renamed: {renamed})")
        return mol

    if ligand_path.suffix.lower() in {".sdf", ".sd"}:
        supplier = Chem.SDMolSupplier(
            str(ligand_path),
            sanitize=False,
            removeHs=False,
            strictParsing=False,
        )
        mol = next((m for m in supplier if m is not None), None)
        if mol is None:
            raise ValueError(f"Failed to read SDF file: {ligand_path}")
        original_positions = snapshot_conformer_positions(mol)
        if not original_positions:
            raise ValueError(f"SDF ligand has no 3D conformer: {ligand_path}")
        mol, renamed = ensure_unique_ligand_atom_names(mol)
        restore_conformer_positions(mol, original_positions)
        print(f"Loaded ligand from SDF: {mol.GetNumAtoms()} atoms (renamed: {renamed})")
        return mol

    if ligand_path.suffix.lower() == ".mol":
        mol = Chem.MolFromMolFile(str(ligand_path), sanitize=False, removeHs=False)
        if mol is None:
            raise ValueError(f"Failed to read MOL file: {ligand_path}")
        original_positions = snapshot_conformer_positions(mol)
        if not original_positions:
            raise ValueError(f"MOL ligand has no 3D conformer: {ligand_path}")
        mol, renamed = ensure_unique_ligand_atom_names(mol)
        restore_conformer_positions(mol, original_positions)
        print(f"Loaded ligand from MOL: {mol.GetNumAtoms()} atoms (renamed: {renamed})")
        return mol

    if ligand_path.suffix.lower() in {".pdb", ".ent"}:
        try:
            mol = Chem.MolFromPDBFile(
                str(ligand_path),
                removeHs=False,
                sanitize=False,
                proximityBonding=False,
            )
        except TypeError:
            mol = Chem.MolFromPDBFile(
                str(ligand_path),
                removeHs=False,
                sanitize=False,
            )
        if mol is None:
            raise ValueError(f"Failed to read PDB file: {ligand_path}")
        original_positions = snapshot_conformer_positions(mol)
        if not original_positions:
            raise ValueError(f"PDB ligand has no 3D conformer: {ligand_path}")
        mol, renamed = ensure_unique_ligand_atom_names(mol)
        restore_conformer_positions(mol, original_positions)
        print(f"Loaded ligand from PDB: {mol.GetNumAtoms()} atoms (renamed: {renamed})")
        return mol

    raise ValueError(f"Unsupported ligand file format: {ligand_path.suffix}")


def canonical_isomeric_smiles_from_mol(mol: Chem.Mol) -> str:
    try:
        base = Chem.RemoveHs(Chem.Mol(mol), sanitize=False)
        Chem.AssignStereochemistry(base, cleanIt=True, force=True)
        return Chem.MolToSmiles(base, canonical=True, isomericSmiles=True)
    except Exception:
        return ""


def fix_cif_entity_ids(cif_file: Path) -> None:
    content = Path(cif_file).read_text()
    content = re.sub(r"^([A-Z][A-Z0-9]*)!\s+", r"\1 ", content, flags=re.MULTILINE)
    content = re.sub(r"([A-Za-z0-9]+)\s+([A-Z][A-Z0-9]*)(!)\s*$", r"\1 \2", content, flags=re.MULTILINE)
    content = re.sub(r"([A-Z][A-Z0-9]*)(!)\s+\.", r"\1 .", content)
    content = re.sub(r"([A-Z][A-Z0-9]*)(!)\s+\?", r"\1 ?", content)
    content = re.sub(r"([A-Z][A-Z0-9]*)(!)(\s+)", r"\1\3", content)
    Path(cif_file).write_text(content)


def slugify_identifier(value: str, fallback: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    text = text.strip("._-")
    return text or fallback


def load_ligand_entries_from_file(ligand_path: Path) -> list[dict[str, object]]:
    ligand_path = Path(ligand_path)
    suffix = ligand_path.suffix.lower()
    if suffix not in {".sdf", ".sd"}:
        mol = load_ligand_from_file(ligand_path)
        return [
            {
                "mol": Chem.Mol(mol),
                "label": ligand_path.stem,
                "source_index": 1,
                "smiles": canonical_isomeric_smiles_from_mol(mol),
            }
        ]

    supplier = Chem.SDMolSupplier(
        str(ligand_path),
        sanitize=False,
        removeHs=False,
        strictParsing=False,
    )
    entries: list[dict[str, object]] = []
    skipped = 0
    for idx, mol in enumerate(supplier, start=1):
        if mol is None:
            skipped += 1
            continue
        original_positions = snapshot_conformer_positions(mol)
        if not original_positions:
            print(f"[Warning] Skipping SDF molecule #{idx}: missing 3D conformer.")
            skipped += 1
            continue
        mol, renamed = ensure_unique_ligand_atom_names(mol)
        restore_conformer_positions(mol, original_positions)

        label = ""
        for prop_name in ("_Name", "Name", "ID", "id"):
            if mol.HasProp(prop_name):
                label = str(mol.GetProp(prop_name)).strip()
                if label:
                    break
        if not label:
            label = f"{ligand_path.stem}_{idx:04d}"

        entries.append(
            {
                "mol": Chem.Mol(mol),
                "label": label,
                "source_index": idx,
                "smiles": canonical_isomeric_smiles_from_mol(mol),
            }
        )
        print(
            f"Loaded ligand #{idx} from SDF: {mol.GetNumAtoms()} atoms "
            f"(renamed: {renamed}, label: {label})"
        )

    if skipped:
        print(f"[Warning] Skipped {skipped} invalid SDF molecule(s) from {ligand_path.name}.")
    if not entries:
        raise ValueError(f"Failed to load any valid 3D ligands from SDF: {ligand_path}")
    if len(entries) > 1:
        print(f"[Info] Multi-SDF mode: loaded {len(entries)} ligands from {ligand_path.name}.")
    return entries


def build_combined_input_from_parts(
    protein_path: Path,
    ligand_mol: Chem.Mol,
    ligand_source_label: str,
    ligand_smiles_map: dict[str, str],
    work_dir: Path,
    record_id: str,
) -> tuple[Path, dict[str, Chem.Mol], Chem.Mol, dict[str, str]]:
    if protein_path.suffix.lower() not in {".pdb", ".ent", ".cif", ".mmcif"}:
        raise ValueError(f"Unsupported protein file format: {protein_path.suffix}")

    structure = gemmi.read_structure(str(protein_path))
    structure.setup_entities()

    ligand_mol = Chem.Mol(ligand_mol)
    ligand_mol, _ = ensure_unique_ligand_atom_names(ligand_mol)
    preloaded_custom_mols = {"LIG": Chem.Mol(ligand_mol)}
    reference_ligand_mol = Chem.Mol(ligand_mol)

    resolved_ligand_smiles_map = dict(ligand_smiles_map or {})
    ligand_smiles_from_file = canonical_isomeric_smiles_from_mol(ligand_mol)
    if ligand_smiles_from_file:
        if resolved_ligand_smiles_map:
            provided_values = [str(v or "").strip() for v in resolved_ligand_smiles_map.values()]
            if any(v and v != ligand_smiles_from_file for v in provided_values):
                print(
                    "[Info] Replacing provided ligand_smiles_map values with "
                    "canonical SMILES derived from uploaded ligand file."
                )
            resolved_ligand_smiles_map = {
                key: ligand_smiles_from_file for key in resolved_ligand_smiles_map
            }
        else:
            resolved_ligand_smiles_map = {"L": ligand_smiles_from_file}
        print(f"[Info] Canonical ligand SMILES from file: {ligand_smiles_from_file}")

    ligand_chain = gemmi.Chain("L")
    residue = gemmi.Residue()
    residue.name = "LIG"
    residue.seqid = gemmi.SeqId(1, " ")

    conf = ligand_mol.GetConformer()
    for atom_idx in range(ligand_mol.GetNumAtoms()):
        atom = ligand_mol.GetAtomWithIdx(atom_idx)
        pos = conf.GetAtomPosition(atom_idx)
        gemmi_atom = gemmi.Atom()
        if atom.HasProp("_original_atom_name"):
            gemmi_atom.name = atom.GetProp("_original_atom_name")
        elif atom.HasProp("name"):
            gemmi_atom.name = atom.GetProp("name")
        else:
            gemmi_atom.name = atom.GetSymbol()
        gemmi_atom.element = gemmi.Element(atom.GetSymbol())
        gemmi_atom.pos = gemmi.Position(pos.x, pos.y, pos.z)
        residue.add_atom(gemmi_atom)

    ligand_chain.add_residue(residue)
    structure[0].add_chain(ligand_chain)
    structure.setup_entities()

    for entity in structure.entities:
        if entity.entity_type.name != "Polymer" or not entity.subchains:
            continue
        seq = []
        for chain in structure[0]:
            for res in chain:
                if res.subchain in entity.subchains:
                    seq.append(res.name)
        if seq:
            entity.full_sequence = seq

    combined_dir = work_dir / "combined"
    combined_dir.mkdir(parents=True, exist_ok=True)
    combined_file = combined_dir / f"{record_id}.cif"
    doc = structure.make_mmcif_document()
    doc.write_file(str(combined_file))
    fix_cif_entity_ids(combined_file)

    print(f"Created combined structure: {combined_file}")
    print(f"  Protein: {protein_path.name}")
    print(f"  Ligand: {ligand_source_label}")

    return combined_file, preloaded_custom_mols, reference_ligand_mol, resolved_ligand_smiles_map
