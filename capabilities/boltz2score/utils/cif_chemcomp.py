"""Inject _chem_comp_atom / _chem_comp_bond categories into output CIFs.

Boltz2Score writes custom ligands with synthetic residue names (LIG, LIG1, ...)
that do not exist in the PDB chemical component dictionary. MolStar (and most
mmCFF viewers) bond non-polymer components from the chem_comp categories in the
file, falling back to dictionary lookups by residue name. Without either, the
ligand renders as disconnected atoms.

This module re-attaches the true ligand bond graph (captured from the custom
RDKit mol used during input preparation) to the output structures:

  - job.py snapshots the processed ligand bond table per record
    (``_snapshot_custom_ligand_bonds``).
  - ``inject_chem_comp_into_record`` rewrites every output CIF of the record,
    appending ``_chem_comp_atom`` / ``_chem_comp_bond`` loops for the ligand
    residue and mapping model atom names to the captured table by name.

Only heavy atoms are emitted (hydrogens are absent from the output anyway).
Atom naming in the output is preserved; unmapped atoms are skipped with a
warning rather than silently breaking the bond table.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence

import gemmi

# Bond types as expected in _chem_comp_bond.value_type (CCD subset used here)
_BOND_TYPE_MAP = {
    "SINGLE": "SING",
    "DOUBLE": "DOUB",
    "TRIPLE": "TRIP",
    "AROMATIC": "AROMATIC",
}


def _bond_type_name(bond_type) -> str:
    name = str(getattr(bond_type, "name", str(bond_type))).upper()
    if name == "AROMATIC":
        # CCD has no delocalized aromatic flag; pick DOUBLE so the viewer
        # renders conjugated systems with sensible bond orders. Alternating
        # Kekulé orders are not reconstructed here.
        return "DOUB"
    return _BOND_TYPE_MAP.get(name, "SING")


def snapshot_custom_ligand_bonds(
    processed_dir: Path,
    record_id: str,
) -> dict[str, list[tuple[str, str, str]]]:
    """Read the processed custom-ligand pkl and return resname -> bond list.

    Each bond entry is (atom_name_a, atom_name_b, ccd_value_type). Returns {}
    when the record used no custom ligands or the pickle is unreadable.
    """
    import pickle

    mols_path = processed_dir / "mols" / f"{record_id}.pkl"
    if not mols_path.exists():
        return {}
    try:
        with mols_path.open("rb") as handle:
            payload = pickle.load(handle)  # noqa: S301
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}

    table: dict[str, list[tuple[str, str, str]]] = {}
    for resname, mol in payload.items():
        if mol is None:
            continue
        try:
            bonds: list[tuple[str, str, str]] = []
            for bond in mol.GetBonds():
                name_a = _atom_name(bond.GetBeginAtom())
                name_b = _atom_name(bond.GetEndAtom())
                if not name_a or not name_b:
                    continue
                bonds.append((name_a, name_b, _bond_type_name(bond.GetBondType())))
            if bonds:
                table[str(resname)] = bonds
        except Exception:
            continue
    return table


def _atom_name(atom) -> str | None:
    for prop in ("_original_atom_name", "name"):
        if atom.HasProp(prop):
            value = atom.GetProp(prop).strip()
            if value:
                return value
    return None


def inject_chem_comp_into_record(
    record_output_dir: Path,
    ligand_bonds: dict[str, list[tuple[str, str, str]]],
    max_residues: int = 4,
) -> int:
    """Append chem_comp categories for ligand residues to all CIFs in a record dir.

    Returns the number of CIF files rewritten.
    """
    if not ligand_bonds:
        return 0

    rewritten = 0
    for cif_path in sorted(record_output_dir.glob("*.cif")):
        try:
            text = cif_path.read_text()
            if "_chem_comp_bond." in text:
                rewritten += 1  # already has bonding info
                continue

            doc = gemmi.cif.read(str(cif_path))
            block = doc.sole_block()

            # Resolve which ligand resnames actually appear as HETATM here.
            present = _het_resnames_in_block(block)
            matched = [res for res in ligand_bonds if res in present]
            if not matched:
                continue
            # Cap the number of injected components to keep files small for
            # multi-ligand outputs; the common case is a single ligand. Name what was
            # skipped so the truncation is visible, not silent.
            if len(matched) > max_residues:
                skipped = [res for res in matched[max_residues:]]
                print(
                    f"[Warning] chem_comp bond injection capped at {max_residues} ligands; "
                    f"skipping: {', '.join(skipped)}"
                )
            matched = matched[:max_residues]

            for resname in matched:
                bonds = ligand_bonds[resname]
                atoms = sorted({name for bond in bonds for name in bond[:2]})
                _append_chem_comp_loops(block, resname, atoms, bonds, doc)

            doc.write_file(str(cif_path))
            rewritten += 1
        except Exception as exc:  # noqa: BLE001 — best effort, never break scoring
            print(f"[Warning] chem_comp injection skipped for {cif_path.name}: {exc}")
    return rewritten


def _het_resnames_in_block(block) -> set[str]:
    import gemmi as _g

    try:
        st = _g.make_structure_from_block(block)
    except Exception:
        return set()
    names = set()
    for model in st:
        for chain in model:
            for residue in chain:
                if residue.het_flag == "H":
                    name = residue.name.strip()
                    if name and name not in {"HOH", "WAT"}:
                        names.add(name)
    return names


def _append_chem_comp_loops(block, resname: str, atoms: Sequence[str], bonds, doc) -> None:
    atom_loop = block.init_loop(
        "_chem_comp_atom.",
        ["comp_id", "atom_id", "type_symbol", "model_Cartn_x", "model_Cartn_y", "model_Cartn_z"],
    )
    # Positional values are irrelevant for bonding; zero coordinates keep the
    # category valid without implying a reference conformer.
    for name in atoms:
        element = re.sub(r"[^A-Za-z]", "", name)[:1].upper() or "C"
        atom_loop.add_row([resname, name, element, "0.0", "0.0", "0.0"])

    bond_loop = block.init_loop(
        "_chem_comp_bond.",
        ["comp_id", "atom_id_1", "atom_id_2", "value_order"],
    )
    for name_a, name_b, value_type in bonds:
        bond_loop.add_row([resname, name_a, name_b, value_type])
