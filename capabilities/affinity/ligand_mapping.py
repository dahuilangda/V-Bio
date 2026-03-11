from __future__ import annotations

from collections import Counter

import gemmi
from rdkit import Chem


def _normalize_exact_atom_name(name: str) -> str:
    return str(name or "").strip()


def _normalize_generated_atom_prefix(name: str) -> str:
    return "".join(ch for ch in str(name or "").strip().upper() if ch.isalnum())


def _to_base36(value: int) -> str:
    digits = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if value <= 0:
        return "0"
    out: list[str] = []
    num = value
    while num:
        num, rem = divmod(num, 36)
        out.append(digits[rem])
    return "".join(reversed(out))


def _generate_atom_name(prefix: str, serial: int) -> str:
    prefix = _normalize_generated_atom_prefix(prefix or "X")
    if len(prefix) >= 2:
        if serial > 36 * 36:
            raise ValueError(f"Too many generated atom names for prefix {prefix[:2]!r}.")
        return f"{prefix[:2]}{_to_base36(serial).rjust(2, '0')[-2:]}"

    if serial > 36 * 36 * 36:
        raise ValueError(f"Too many generated atom names for prefix {prefix[:1]!r}.")
    return f"{prefix[:1] or 'X'}{_to_base36(serial).rjust(3, '0')[-3:]}"


def _element_prefix_for_atom(atom: Chem.Atom) -> str:
    symbol = _normalize_generated_atom_prefix(atom.GetSymbol() or "")
    if not symbol:
        return "X"
    if len(symbol) >= 2 and symbol[0].isalpha() and symbol[1].isalpha():
        return symbol[:2]
    return symbol[:1]


def _extract_atom_preferred_name(atom: Chem.Atom) -> str:
    for prop in ("_original_atom_name", "name", "_TriposAtomName", "_atomName"):
        if atom.HasProp(prop):
            value = _normalize_exact_atom_name(atom.GetProp(prop))
            if value:
                return value
    monomer_info = atom.GetMonomerInfo()
    if monomer_info is not None and hasattr(monomer_info, "GetName"):
        try:
            value = _normalize_exact_atom_name(monomer_info.GetName())
            if value:
                return value
        except Exception:
            pass
    return ""


def ensure_unique_ligand_atom_names(mol: Chem.Mol, context: str) -> tuple[Chem.Mol, int]:
    """Preserve exact names when possible; otherwise assign deterministic unique names."""
    used: set[str] = set()
    serial_by_prefix: dict[str, int] = {}
    renamed = 0

    for atom in mol.GetAtoms():
        preferred_raw = _extract_atom_preferred_name(atom)
        preferred = _normalize_exact_atom_name(preferred_raw)

        candidate = ""
        if preferred and preferred not in used:
            candidate = preferred
        else:
            prefix = _element_prefix_for_atom(atom)
            serial = serial_by_prefix.get(prefix, 1)
            while True:
                generated = _generate_atom_name(prefix, serial)
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
        if atom.HasProp("_TriposAtomName"):
            atom.SetProp("_TriposAtomName", candidate)

    if not used:
        raise ValueError(f"{context}: ligand has no atoms.")
    return mol, renamed


def _is_hydrogen_like(element_or_name: str) -> bool:
    token = str(element_or_name or "").strip().upper()
    return token in {"H", "D", "T"} or token.startswith(("H", "D", "T"))


def heavy_atom_names_from_residue(residue: gemmi.Residue) -> list[str]:
    names: list[str] = []
    for atom in residue:
        element = str(atom.element.name or atom.name[:1]).strip().upper()
        if _is_hydrogen_like(element):
            continue
        name = _normalize_exact_atom_name(atom.name)
        if name:
            names.append(name)
    return names


def heavy_atom_names_from_mol(mol: Chem.Mol | None) -> list[str]:
    if mol is None:
        return []
    try:
        heavy = Chem.RemoveHs(Chem.Mol(mol), sanitize=False)
    except Exception:
        heavy = mol

    names: list[str] = []
    for atom in heavy.GetAtoms():
        name = _normalize_exact_atom_name(_extract_atom_preferred_name(atom))
        if name:
            names.append(name)
    return names


def canonical_heavy_smiles(mol: Chem.Mol | None) -> str:
    if mol is None:
        return ""
    try:
        heavy = Chem.RemoveHs(Chem.Mol(mol), sanitize=False)
        return Chem.MolToSmiles(heavy, canonical=True, isomericSmiles=True)
    except Exception:
        return ""


def ligand_occurrence_signature(
    residue: gemmi.Residue,
    mol: Chem.Mol,
) -> tuple[tuple[tuple[str, int], ...], tuple[tuple[int, int], ...], str]:
    residue_name_counter = Counter(heavy_atom_names_from_residue(residue))
    residue_element_counter = Counter(
        int(atom.element.atomic_number)
        for atom in residue
        if not _is_hydrogen_like(str(atom.element.name or atom.name[:1]))
    )
    smiles = canonical_heavy_smiles(mol)
    return (
        tuple(sorted(residue_name_counter.items())),
        tuple(sorted(residue_element_counter.items())),
        smiles,
    )

