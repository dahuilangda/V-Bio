from __future__ import annotations

from pathlib import Path

import gemmi

from utils.ligand_utils import fix_cif_entity_ids


def validate_unique_atom_ids_for_writer(input_path: Path, max_items: int = 20) -> None:
    """Fail fast on duplicate atom IDs that break mmCIF writer serialization."""
    structure = gemmi.read_structure(str(input_path))
    duplicates: list[tuple[str, int, str, str, str]] = []

    for model in structure:
        for chain in model:
            for residue in chain:
                seen: set[tuple[str, str | None]] = set()
                for atom in residue:
                    atom_name = atom.name.strip() or atom.element.name.strip() or "?"
                    raw_alt = str(getattr(atom, "altloc", "") or "")
                    raw_alt = raw_alt.strip()
                    alt_id = None if raw_alt in {"", "\x00", ".", "?"} else raw_alt
                    key = (atom_name, alt_id)
                    if key in seen:
                        duplicates.append(
                            (
                                chain.name,
                                int(residue.seqid.num),
                                residue.name.strip() or "?",
                                atom_name,
                                alt_id or "None",
                            )
                        )
                        if len(duplicates) >= max_items:
                            break
                    else:
                        seen.add(key)
                if len(duplicates) >= max_items:
                    break
            if len(duplicates) >= max_items:
                break
        if len(duplicates) >= max_items:
            break

    if duplicates:
        detail = "; ".join(
            f"chain={c}, res={r}:{rn}, atom={a}, alt={alt}"
            for c, r, rn, a, alt in duplicates
        )
        raise ValueError(
            "Input has duplicate atom IDs within the same residue "
            "(same atom name + altloc), which modelcif writer rejects. "
            f"Examples: {detail}. "
            "Please deduplicate the structure before scoring."
        )


def _normalize_pdb_duplicate_atom_ids_for_writer(pdb_path: Path) -> int:
    """Canonicalize duplicate PDB atom names within a residue for writer compatibility."""
    lines = pdb_path.read_text().splitlines()
    out_lines: list[str] = []
    used_names: dict[tuple[str, str, str, str, str], set[str]] = {}
    serial_counters: dict[tuple[str, str, str, str, str], int] = {}
    renamed = 0

    for raw in lines:
        if not raw.startswith(("ATOM", "HETATM")):
            out_lines.append(raw)
            continue
        line = raw.rstrip("\n")
        if len(line) < 54:
            out_lines.append(line)
            continue
        if len(line) < 80:
            line = line.ljust(80)

        record = line[:6].strip() or "ATOM"
        atom_name = line[12:16].strip()
        res_name = line[17:20].strip() or "UNK"
        chain_id = line[21:22].strip() or "_"
        res_seq = line[22:26].strip() or "0"
        ins_code = line[26:27].strip() or ""
        residue_key = (record, chain_id, res_seq, ins_code, res_name)
        used = used_names.setdefault(residue_key, set())

        if not atom_name:
            atom_name = (line[76:78].strip() or "X").upper()

        candidate = atom_name
        if candidate in used:
            prefix = "".join(ch for ch in candidate.upper() if ch.isalnum())[:1]
            if not prefix:
                prefix = "".join(ch for ch in line[76:78].upper() if ch.isalnum())[:1] or "X"
            idx = serial_counters.get(residue_key, 1)
            while True:
                next_name = f"{prefix}{idx:03d}"[-4:]
                idx += 1
                if next_name not in used:
                    candidate = next_name
                    break
            serial_counters[residue_key] = idx
            line = f"{line[:12]}{candidate.rjust(4)}{line[16:]}"
            renamed += 1

        used.add(candidate)
        out_lines.append(line.rstrip())

    if renamed:
        pdb_path.write_text("\n".join(out_lines) + "\n")
    return renamed


def _normalize_cif_duplicate_atom_ids_for_writer(cif_path: Path) -> int:
    """Canonicalize duplicate CIF atom IDs within a residue for writer compatibility."""
    structure = gemmi.read_structure(str(cif_path))
    renamed = 0

    for model in structure:
        for chain in model:
            for residue in chain:
                used: set[tuple[str, str | None]] = set()
                serial_counters: dict[str | None, int] = {}
                for atom in residue:
                    atom_name = atom.name.strip() or atom.element.name.strip() or "X"
                    raw_alt = str(getattr(atom, "altloc", "") or "").strip()
                    alt_id = None if raw_alt in {"", "\x00", ".", "?"} else raw_alt
                    key = (atom_name, alt_id)
                    if key not in used:
                        used.add(key)
                        continue

                    prefix = "".join(ch for ch in atom_name.upper() if ch.isalnum())[:1]
                    if not prefix:
                        prefix = "".join(ch for ch in atom.element.name.upper() if ch.isalnum())[:1] or "X"
                    idx = serial_counters.get(alt_id, 1)
                    while True:
                        candidate = f"{prefix}{idx:03d}"[-4:]
                        idx += 1
                        if (candidate, alt_id) not in used:
                            break
                    serial_counters[alt_id] = idx
                    atom.name = candidate
                    used.add((candidate, alt_id))
                    renamed += 1

    if renamed:
        doc = structure.make_mmcif_document()
        doc.write_file(str(cif_path))
        fix_cif_entity_ids(cif_path)
    return renamed


def normalize_duplicate_atom_ids_for_writer(input_path: Path) -> int:
    suffix = input_path.suffix.lower()
    if suffix in {".pdb", ".ent"}:
        return _normalize_pdb_duplicate_atom_ids_for_writer(input_path)
    if suffix in {".cif", ".mmcif"}:
        return _normalize_cif_duplicate_atom_ids_for_writer(input_path)
    return 0
