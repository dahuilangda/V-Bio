"""Custom-residue CCD (Chemical Component Definition) builder.

Extracted from backend/runtime/run_single_prediction.py. This module contains the
pure CCD-construction helpers that turn user-drawn SMILES into mmCIF
``data_<CCD>`` blocks suitable for AF3 / Protenix / Boltz: backbone topology
detection, atom naming, 3D embedding, and cif serialization.

Functions that touch task-specific filesystem / runtime state (the Protenix
common-cache overlay and the Boltz per-task mols dir) remain in
run_single_prediction.py and import the builders from here.
"""
import re
import sys
import pickle
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any

import numpy as np
from rdkit import Chem
from rdkit.Chem import AllChem, Descriptors, rdMolDescriptors


# SMARTS for a peptide-linkable backbone: N - C(alpha, tetrahedral) - C(=O)[O|N].
# Used by _custom_ccd_has_amino_acid_backbone to detect whether a custom residue
# already carries a canonical amino-acid backbone.
CUSTOM_RESIDUE_BACKBONE_SMARTS = "[NX3;!$(NC=O)]-[C;X4]-C(=O)[O,N]"


def _normalize_backbone_override(raw: Any) -> Optional[Dict[str, int]]:
    """Validate/normalize a manual backbone override (5 non-negative integer slots)."""
    if not isinstance(raw, dict):
        return None
    backbone: Dict[str, int] = {}
    for slot in ("n", "ca", "c", "o", "oxt"):
        try:
            num = int(raw.get(slot))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if num < 0:
            return None
        backbone[slot] = num
    return backbone


def _normalize_custom_ccd_molecules(raw_value: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_value, list):
        return []
    molecules: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_value:
        if not isinstance(item, dict):
            continue
        ccd = re.sub(r"[^A-Za-z0-9_-]", "", str(item.get("ccd") or "")).upper()[:12]
        smiles = str(item.get("smiles") or "").strip()
        if not ccd or not smiles or ccd in seen:
            continue
        kind = str(item.get("kind") or "residue").strip().lower()
        if kind not in {"residue", "ligand"}:
            kind = "residue"
        seen.add(ccd)
        molecules.append({
            "ccd": ccd,
            "smiles": smiles,
            "base_residue": str(item.get("base_residue") or item.get("baseResidue") or "").strip().upper()[:1],
            "label": str(item.get("label") or "").strip()[:80],
            "kind": kind,
            "backbone": _normalize_backbone_override(item.get("backbone")),
            "cTerminalAmidated": bool(item.get("cTerminalAmidated")),
        })
    return molecules


def _boltz_custom_ccd_aliases(ccd: str) -> List[str]:
    code = re.sub(r"[^A-Za-z0-9_-]", "", str(ccd or "")).upper()
    aliases: List[str] = []
    for candidate in (code, code[:5]):
        if candidate and candidate not in aliases:
            aliases.append(candidate)
    return aliases


def _custom_ccd_has_amino_acid_backbone(mol: Chem.Mol) -> bool:
    query = Chem.MolFromSmarts(CUSTOM_RESIDUE_BACKBONE_SMARTS)
    return bool(query is not None and mol.HasSubstructMatch(query))


def _is_carbonyl_carbon(mol: Chem.Mol, atom_idx: int) -> bool:
    atom = mol.GetAtomWithIdx(atom_idx)
    if atom.GetAtomicNum() != 6:
        return False
    for bond in atom.GetBonds():
        other = bond.GetOtherAtom(atom)
        if other.GetAtomicNum() == 8 and bond.GetBondType() == Chem.BondType.DOUBLE:
            return True
    return False


def _is_amide_like_nitrogen(mol: Chem.Mol, atom_idx: int) -> bool:
    atom = mol.GetAtomWithIdx(atom_idx)
    if atom.GetAtomicNum() != 7:
        return False
    for neighbor in atom.GetNeighbors():
        if _is_carbonyl_carbon(mol, neighbor.GetIdx()):
            return True
    return False


def _find_residue_backbone_topology(mol: Chem.Mol, *, amidated: bool = False) -> Dict[str, Any]:
    # Amidated residues terminate in -C(=O)NH2 (an amide) instead of -C(=O)OH (a carboxyl). The
    # match is still a 3-tuple (carbon, carbonyl-O, terminal-atom) where the terminal atom is O
    # for carboxyl or N for amide, so the downstream scoring heuristic below is shared verbatim.
    terminal_smarts = "[CX3](=[OX1])[NX3H2,NX3H1,NX4H2]" if amidated else "[CX3](=O)[OX1H0-,OX2H1]"
    terminal_pattern = Chem.MolFromSmarts(terminal_smarts)
    if terminal_pattern is None:
        raise RuntimeError("Failed to build residue terminal SMARTS.")
    terminal_matches = list(mol.GetSubstructMatches(terminal_pattern))
    if not terminal_matches:
        if amidated:
            raise ValueError(
                "C 端酰胺化自定义残基必须包含末端酰胺基团 C(=O)N"
                "（请确认已勾选「C 端酰胺化」，且 SMILES 的 C 端为 -C(=O)N）。"
            )
        raise ValueError("Custom residue must contain a terminal carboxyl group C(=O)O.")

    n_candidates = [
        atom.GetIdx()
        for atom in mol.GetAtoms()
        if atom.GetAtomicNum() == 7 and atom.GetDegree() > 0
    ]
    if not n_candidates:
        raise ValueError("Custom residue must contain a peptide-linkable nitrogen atom.")

    best: Dict[str, Any] | None = None
    excluded_by_terminal = {idx for match in terminal_matches for idx in match[1:]}
    for terminal in terminal_matches:
        if len(terminal) < 3:
            continue
        carbon_idx, carbonyl_oxygen_idx, terminal_oxygen_idx = terminal[:3]
        for n_idx in n_candidates:
            if n_idx == carbon_idx or n_idx in excluded_by_terminal:
                continue
            try:
                path = list(Chem.rdmolops.GetShortestPath(mol, n_idx, carbon_idx))
            except Exception:
                continue
            if len(path) < 3:
                continue
            if any(idx in excluded_by_terminal for idx in path[1:-1]):
                continue
            interior = path[1:-1]
            if not interior:
                continue
            if len(interior) > 8:
                continue
            first = mol.GetAtomWithIdx(interior[0])
            if first.GetAtomicNum() not in {6, 7}:
                continue
            hetero_penalty = sum(1 for idx in interior if mol.GetAtomWithIdx(idx).GetAtomicNum() not in {6, 7})
            amide_penalty = 4 if _is_amide_like_nitrogen(mol, n_idx) else 0
            score = (amide_penalty, hetero_penalty, len(path))
            candidate = {
                "n_idx": n_idx,
                "c_idx": carbon_idx,
                "o_idx": carbonyl_oxygen_idx,
                "oxt_idx": terminal_oxygen_idx,
                "path": path,
                "score": score,
            }
            if best is None or score < best["score"]:
                best = candidate

    if best is None:
        raise ValueError(
            "Custom residue must contain a peptide-linkable nitrogen connected to a terminal carboxyl group by a reasonable heavy-atom backbone path."
        )
    return best


def _set_atom_name(atom: Chem.Atom, name: str) -> None:
    atom.SetProp("name", name)
    atom.SetProp("atom_name", name)
    atom.SetProp("alt_name", name)


def _set_custom_ccd_atom_properties(mol: Chem.Mol, *, kind: str, residue_topology: Optional[Dict[str, Any]] = None, amidated: bool = False) -> None:
    element_count: Dict[str, int] = {}
    for atom in mol.GetAtoms():
        symbol = atom.GetSymbol().upper()
        element_count[symbol] = element_count.get(symbol, 0) + 1
        name = f"{symbol}{element_count[symbol]}"
        if len(name) > 4:
            raise ValueError(f"Custom CCD atom name exceeds 4 characters: {name}")
        _set_atom_name(atom, name)
        # Bool prop (not a "0"/"1" string): RDKit GetPropsAsDict() parses numeric strings
        # back to ints, which would break the reader. Bool round-trips cleanly.
        atom.SetBoolProp("leaving_atom", False)

    if kind != "residue":
        return
    if not residue_topology:
        raise ValueError("Custom residue topology was not resolved.")

    path = [int(idx) for idx in residue_topology.get("path") or []]
    n_idx = int(residue_topology["n_idx"])
    c_idx = int(residue_topology["c_idx"])
    o_idx = int(residue_topology["o_idx"])
    oxt_idx = int(residue_topology["oxt_idx"])

    _set_atom_name(mol.GetAtomWithIdx(n_idx), "N")
    _set_atom_name(mol.GetAtomWithIdx(c_idx), "C")
    _set_atom_name(mol.GetAtomWithIdx(o_idx), "O")
    terminal = mol.GetAtomWithIdx(oxt_idx)
    if amidated:
        # C-terminal amide: the terminal atom is nitrogen (NXT), which is NOT a leaving atom — it
        # is part of the -CONH2 terminus (PDB convention, e.g. CCD 9AT/ZZJ). leaving_atom stays "0".
        _set_atom_name(terminal, "NXT")
    else:
        _set_atom_name(terminal, "OXT")
        terminal.SetBoolProp("leaving_atom", True)

    backbone_names = ["CA", "CB", "CG", "CD", "CE", "CZ", "CH", "CI"]
    for offset, atom_idx in enumerate(path[1:-1]):
        if offset >= len(backbone_names):
            break
        _set_atom_name(mol.GetAtomWithIdx(atom_idx), backbone_names[offset])


def _residue_topology_from_backbone_override(mol: Chem.Mol, backbone: Dict[str, int], *, amidated: bool = False) -> Dict[str, Any]:
    """Resolve a residue_topology from a user-supplied manual backbone assignment (0-based
    heavy-atom indices). Authoritative and validated: every slot's element and the carboxyl
    chemistry must match, else raise — never silently fall back to the topology heuristic.
    `mol` here is pre-AddHs (heavy atoms only), matching the 2D depiction index space."""
    if not isinstance(backbone, dict):
        raise ValueError("Custom residue backbone override must be an object.")

    def _slot_idx(slot: str) -> int:
        try:
            num = int(backbone.get(slot))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ValueError(f"Backbone slot '{slot}' must be an integer atom index.")
        if num < 0 or num >= mol.GetNumAtoms():
            raise ValueError(
                f"Backbone slot '{slot}' index {num} is out of range (0..{mol.GetNumAtoms() - 1})."
            )
        return num

    n_idx = _slot_idx("n")
    ca_idx = _slot_idx("ca")
    c_idx = _slot_idx("c")
    o_idx = _slot_idx("o")
    oxt_idx = _slot_idx("oxt")
    if len({n_idx, ca_idx, c_idx, o_idx, oxt_idx}) != 5:
        raise ValueError("Backbone slots must each point at a distinct atom.")

    def _atomic_num(idx: int) -> int:
        return mol.GetAtomWithIdx(idx).GetAtomicNum()

    if _atomic_num(n_idx) != 7:
        raise ValueError("Backbone N must be a nitrogen atom.")
    if _atomic_num(ca_idx) not in (6, 7):
        raise ValueError("Backbone CA must be a carbon or nitrogen atom.")
    if _atomic_num(c_idx) != 6:
        raise ValueError("Backbone C must be a carbon atom.")
    if _atomic_num(o_idx) != 8:
        raise ValueError("Backbone O must be the carbonyl oxygen atom.")
    # The terminal (5th) slot is the carboxyl hydroxyl oxygen (OXT) by default, or the C-terminal
    # amide nitrogen (NXT) when amidated — both bond to the carbonyl carbon C.
    if amidated:
        if _atomic_num(oxt_idx) != 7:
            raise ValueError("C 端酰胺化模式下，第 5 个骨架槽位（NXT）必须是氮原子。")
    elif _atomic_num(oxt_idx) != 8:
        raise ValueError("Backbone OXT must be an oxygen atom.")

    # C must be the carbonyl carbon: bonded to both O and the terminal atom, with O the carbonyl
    # (C=O). For carboxyl, the terminal OXT is the leaving hydroxyl (C-OH); for amide, NXT is a
    # single-bonded terminal nitrogen (C-N) that is NOT a leaving atom.
    c_neighbors = {b.GetOtherAtomIdx(c_idx) for b in mol.GetAtomWithIdx(c_idx).GetBonds()}
    if o_idx not in c_neighbors or oxt_idx not in c_neighbors:
        raise ValueError("Backbone C must be directly bonded to both O and the terminal atom (carboxyl/amide carbon).")

    def _is_double_bond(a: int, b: int) -> bool:
        for bond in mol.GetAtomWithIdx(a).GetBonds():
            if bond.GetOtherAtomIdx(a) == b:
                return bond.GetBondType() == Chem.BondType.DOUBLE
        return False

    if not _is_double_bond(c_idx, o_idx):
        raise ValueError("Backbone O must be the carbonyl oxygen (C=O).")

    if amidated:
        for bond in mol.GetAtomWithIdx(c_idx).GetBonds():
            if bond.GetOtherAtomIdx(c_idx) == oxt_idx:
                if bond.GetBondType() != Chem.BondType.SINGLE:
                    raise ValueError("C 端酰胺化模式下，C-NXT 必须是单键（酰胺 C-N 单键）。")
                break

    # CA must connect the backbone (bonded to N or C), matching the topology heuristic's path.
    ca_neighbors = {b.GetOtherAtomIdx(ca_idx) for b in mol.GetAtomWithIdx(ca_idx).GetBonds()}
    if n_idx not in ca_neighbors and c_idx not in ca_neighbors:
        raise ValueError("Backbone CA must be bonded to N or C.")

    return {
        "n_idx": n_idx,
        "c_idx": c_idx,
        "o_idx": o_idx,
        "oxt_idx": oxt_idx,
        # path[1:-1] is consumed by _set_custom_ccd_atom_properties to name CA; sidechain atoms
        # beyond CA keep their {ELEMENT}{count} names, matching the frontend picker exactly.
        "path": [n_idx, ca_idx, c_idx],
    }


def _build_custom_ccd_mol(smiles: str, *, kind: str = "residue", backbone: Optional[Dict[str, int]] = None, amidated: bool = False) -> Chem.Mol:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError("RDKit failed to parse custom CCD SMILES.")
    Chem.SanitizeMol(mol)
    if kind == "residue":
        # A manual backbone override (user-clicked atoms) is authoritative and validated; the
        # topology heuristic is only a fallback when no override is supplied.
        residue_topology = (
            _residue_topology_from_backbone_override(mol, backbone, amidated=amidated)
            if backbone else _find_residue_backbone_topology(mol, amidated=amidated)
        )
    else:
        residue_topology = None
    mol = Chem.AddHs(mol)
    _set_custom_ccd_atom_properties(mol, kind=kind, residue_topology=residue_topology, amidated=amidated)
    embed_status = AllChem.EmbedMolecule(mol, AllChem.ETKDGv3())
    if embed_status == -1:
        embed_status = AllChem.EmbedMolecule(mol, useRandomCoords=True)
    if embed_status == -1:
        raise ValueError(f"RDKit 3D embedding failed for custom CCD SMILES: {smiles}")
    try:
        AllChem.UFFOptimizeMolecule(mol)
    except Exception as exc:
        # Embedding already succeeded; UFF is geometry refinement only, so a failure leaves an
        # unoptimized (but valid) conformer. Log it so a malformed custom CCD stays traceable.
        print(f"⚠️ UFF optimization failed for custom CCD SMILES ({smiles}): {exc}", file=sys.stderr)
    for conformer in mol.GetConformers():
        conformer.SetProp("name", "Ideal")
    mol.atom_map = {atom.GetProp("name"): atom.GetIdx() for atom in mol.GetAtoms() if atom.GetSymbol().upper() not in {"H", "D"}}
    mol.name = "custom"
    mol.sanitized = True
    mol.ref_conf_id = 0
    mol.ref_conf_type = "rdkit"
    mol.ref_mask = np.ones(mol.GetNumAtoms(), dtype=bool)
    return mol

def _custom_ccd_mol_atom_names(mol: Chem.Mol, *, include_hydrogens: bool = False) -> List[str]:
    names: List[str] = []
    for atom in mol.GetAtoms():
        if not include_hydrogens and atom.GetSymbol().upper() in {"H", "D"}:
            continue
        names.append(atom.GetProp("name") if atom.HasProp("name") else f"{atom.GetSymbol().upper()}{atom.GetIdx() + 1}")
    return names


def _custom_ccd_mol_to_cif_block(
    ccd: str,
    mol: Chem.Mol,
    *,
    kind: str,
    label: str = "",
    base_residue: str = "",
) -> str:
    formula = rdMolDescriptors.CalcMolFormula(mol)
    weight = Descriptors.MolWt(mol)
    comp_type = "L-PEPTIDE LINKING" if kind == "residue" else "NON-POLYMER"
    name = label or ccd
    clean_name = name.replace(chr(39), "")
    base = str(base_residue or "").strip().upper()[:1]
    if kind == "residue" and base not in "ARNDCQEGHILKMFPSTWYV":
        raise ValueError(f"Custom residue CCD {ccd} requires a valid base_residue for canonical mapping.")
    one_letter_code = base if kind == "residue" else "?"
    parent_comp_id = base if kind == "residue" else "?"
    three_letter_code = ccd if len(ccd) <= 3 else "?"
    lines: List[str] = [
        f"data_{ccd}",
        "#",
        f"_chem_comp.id {ccd}",
        f"_chem_comp.name '{clean_name}'",
        f"_chem_comp.type '{comp_type}'",
        f"_chem_comp.formula '{formula}'",
        f"_chem_comp.mon_nstd_parent_comp_id {parent_comp_id}",
        "_chem_comp.pdbx_synonyms ?",
        "_chem_comp.pdbx_formal_charge 0",
        "_chem_comp.pdbx_initial_date 2026-06-26",
        "_chem_comp.pdbx_modified_date 2026-06-26",
        "_chem_comp.pdbx_ambiguous_flag N",
        "_chem_comp.pdbx_release_status REL",
        "_chem_comp.pdbx_replaced_by ?",
        "_chem_comp.pdbx_replaces ?",
        f"_chem_comp.formula_weight {weight:.3f}",
        f"_chem_comp.one_letter_code {one_letter_code}",
        f"_chem_comp.three_letter_code {three_letter_code}",
        "_chem_comp.pdbx_model_coordinates_details ?",
        "_chem_comp.pdbx_model_coordinates_missing_flag N",
        "_chem_comp.pdbx_ideal_coordinates_details RDKit",
        "_chem_comp.pdbx_ideal_coordinates_missing_flag N",
        "_chem_comp.pdbx_model_coordinates_db_code ?",
        "_chem_comp.pdbx_subcomponent_list ?",
        "_chem_comp.pdbx_processing_site V-Bio",
        "#",
        "loop_",
        "_chem_comp_atom.comp_id",
        "_chem_comp_atom.atom_id",
        "_chem_comp_atom.type_symbol",
        "_chem_comp_atom.charge",
        "_chem_comp_atom.pdbx_model_Cartn_x_ideal",
        "_chem_comp_atom.pdbx_model_Cartn_y_ideal",
        "_chem_comp_atom.pdbx_model_Cartn_z_ideal",
        "_chem_comp_atom.pdbx_leaving_atom_flag",
        "_chem_comp_atom.alt_atom_id",
        "_chem_comp_atom.pdbx_component_atom_id",
    ]
    conf = mol.GetConformer(mol.ref_conf_id if hasattr(mol, "ref_conf_id") else 0)
    for atom in mol.GetAtoms():
        if atom.GetSymbol().upper() in {"H", "D"}:
            continue
        atom_name = atom.GetPropsAsDict().get("name") or atom.GetSymbol().upper()
        pos = conf.GetAtomPosition(atom.GetIdx())
        # leaving_atom is a canonical bool (set via SetBoolProp): True marks a leaving atom
        # (e.g. OXT on a non-amidated C-terminus). Ligand/linker Mols carry no such prop, so
        # absence means "not a leaving atom". Any other type is a programming error — in
        # particular a "0"/"1" string, which GetPropsAsDict() silently parses back to an int.
        leaving_flag = atom.GetPropsAsDict().get("leaving_atom")
        if isinstance(leaving_flag, bool):
            leaving = "Y" if leaving_flag else "N"
        elif leaving_flag is None:
            leaving = "N"
        else:
            raise ValueError(
                f"CCD {ccd} atom {atom.GetIdx()} leaving_atom must be a bool or unset, got {leaving_flag!r}"
            )
        lines.append(
            f"{ccd} {atom_name} {atom.GetSymbol().upper()} {atom.GetFormalCharge()} {pos.x:.4f} {pos.y:.4f} {pos.z:.4f} {leaving} {atom_name} {atom_name}"
        )
    lines.extend([
        "#",
        "loop_",
        "_chem_comp_bond.comp_id",
        "_chem_comp_bond.atom_id_1",
        "_chem_comp_bond.atom_id_2",
        "_chem_comp_bond.value_order",
        "_chem_comp_bond.pdbx_aromatic_flag",
    ])
    bond_order_map = {
        Chem.BondType.SINGLE: "SING",
        Chem.BondType.DOUBLE: "DOUB",
        Chem.BondType.TRIPLE: "TRIP",
        Chem.BondType.AROMATIC: "SING",
    }
    for bond in mol.GetBonds():
        atom1 = bond.GetBeginAtom()
        atom2 = bond.GetEndAtom()
        if atom1.GetSymbol().upper() in {"H", "D"} or atom2.GetSymbol().upper() in {"H", "D"}:
            continue
        order = bond_order_map.get(bond.GetBondType(), "SING")
        aromatic = "Y" if bond.GetIsAromatic() else "N"
        lines.append(f"{ccd} {atom1.GetProp('name')} {atom2.GetProp('name')} {order} {aromatic}")
    lines.append("#")
    return "\n".join(lines) + "\n"


def build_ligand_ccd_mmcif(ccd: str, mol: Chem.Mol, label: str = "") -> str:
    """Emit a NON-POLYMER ligand CCD mmcif from a Mol whose atoms carry a 'name' prop
    (e.g. a Boltz CCD-cache Mol). Same format as custom-residue mmcif so AF3 (userCCD) and
    Protenix (CCD overlay) resolve the ligand identically to Boltz's own CCD."""
    return _custom_ccd_mol_to_cif_block(ccd, mol, kind="ligand", label=label)


def _append_custom_residues_ccd(
    extra_files: List[Tuple[Path, str]],
    cif_text: Optional[str],
    temp_dir: str,
    prefix: str,
) -> None:
    """Write the custom-residue CCD mmcif (the exact definitions the backend fed to the
    predictor) into the result archive so users can download them with the structure."""
    text = str(cif_text or "").strip()
    if not text:
        return
    dest = Path(temp_dir) / "custom_residues.cif"
    dest.write_text(text, encoding="utf-8")
    extra_files.append((dest, f"{prefix}/custom_residues.cif"))


def _append_custom_residues_ccd_from_molecules(
    extra_files: List[Tuple[Path, str]],
    custom_molecules: List[Dict[str, Any]],
    temp_dir: str,
    prefix: str,
) -> None:
    """Re-build the custom-residue CCD mmcif from the (already normalized/merged) molecule list
    and add it to the archive. Used by backends whose archive is assembled in a different scope
    from where the bundle was first built."""
    if not custom_molecules:
        return
    cif_text, _ = _build_custom_ccd_bundle(custom_molecules)
    _append_custom_residues_ccd(extra_files, cif_text, temp_dir, prefix)


def _build_custom_ccd_bundle(molecules: List[Dict[str, str]]) -> Tuple[str, Dict[str, Chem.Mol]]:
    blocks: List[str] = []
    mols: Dict[str, Chem.Mol] = {}
    for item in _normalize_custom_ccd_molecules(molecules):
        kind = item.get("kind") or "residue"
        ccd = item["ccd"]
        amidated = bool(item.get("cTerminalAmidated") or False)
        mol = _build_custom_ccd_mol(item["smiles"], kind=kind, backbone=item.get("backbone"), amidated=amidated)
        mol.name = ccd
        mols[ccd] = mol
        blocks.append(_custom_ccd_mol_to_cif_block(
            ccd,
            mol,
            kind=kind,
            label=item.get("label") or ccd,
            base_residue=item.get("base_residue") or "",
        ))
    return "\n".join(blocks), mols
