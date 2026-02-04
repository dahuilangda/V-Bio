#!/usr/bin/env python3
"""Single-step Boltz2Score: input a PDB/mmCIF, output scores."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional, Sequence

import torch
import gemmi
from rdkit import Chem

Chem.SetDefaultPickleProperties(Chem.PropertyPickleOptions.AllProps)

from boltz.main import get_cache_path
from boltz.data import const
from boltz.data.types import StructureV2
from prepare_boltz2score_inputs import prepare_inputs
from run_boltz2score import run_scoring


def _parse_chain_list(value: Optional[str]) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


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


def _normalize_atom_name(name: str) -> str:
    return "".join(ch for ch in name.strip().upper() if ch.isalnum())


def _element_prefix_for_atom(atom: Chem.Atom) -> str:
    """Return a compact element-aware prefix for generated atom names."""
    symbol = _normalize_atom_name(atom.GetSymbol() or "")
    if not symbol:
        return "X"
    # Keep two-letter element symbols (CL, BR, NA...) when available.
    if len(symbol) >= 2 and symbol[0].isalpha() and symbol[1].isalpha():
        return symbol[:2]
    return symbol[:1]


def _generate_atom_name(prefix: str, serial: int) -> str:
    """Generate deterministic <=4-char atom names compatible with mmCIF writer."""
    prefix = _normalize_atom_name(prefix or "X")
    if len(prefix) >= 2:
        # 2-letter element prefix + 2 base36 digits
        if serial > 36 * 36:
            raise ValueError(f"Too many atoms for prefix {prefix[:2]!r}.")
        return f"{prefix[:2]}{_to_base36(serial).rjust(2, '0')[-2:]}"

    # 1-letter prefix + 3 base36 digits
    if serial > 36 * 36 * 36:
        raise ValueError(f"Too many atoms for prefix {prefix[:1]!r}.")
    return f"{prefix[:1] or 'X'}{_to_base36(serial).rjust(3, '0')[-3:]}"


def _extract_atom_preferred_name(atom: Chem.Atom) -> str:
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


def _ensure_unique_ligand_atom_names(mol: Chem.Mol) -> tuple[Chem.Mol, int]:
    """Normalize ligand atom names for stable atom mapping and mmCIF compatibility."""
    used: set[str] = set()
    serial_by_prefix: dict[str, int] = {}
    renamed = 0

    for atom in mol.GetAtoms():
        preferred_raw = _extract_atom_preferred_name(atom)
        normalized = _normalize_atom_name(preferred_raw or "")

        candidate = None
        if normalized and len(normalized) <= 4 and normalized not in used:
            candidate = normalized
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

    return mol, renamed


def _snapshot_conformer_positions(mol: Chem.Mol) -> list[tuple[float, float, float]]:
    if mol.GetNumConformers() == 0:
        return []
    conf = mol.GetConformer()
    return [
        (float(conf.GetAtomPosition(i).x), float(conf.GetAtomPosition(i).y), float(conf.GetAtomPosition(i).z))
        for i in range(mol.GetNumAtoms())
    ]


def _restore_conformer_positions(mol: Chem.Mol, positions: list[tuple[float, float, float]]) -> None:
    if not positions or mol.GetNumConformers() == 0 or len(positions) != mol.GetNumAtoms():
        return
    conf = mol.GetConformer()
    for idx, (x, y, z) in enumerate(positions):
        conf.SetAtomPosition(idx, (x, y, z))


def _load_ligand_from_file(ligand_path: Path):
    """Load ligand from various file formats, preserving original atom names and coordinates."""
    ligand_path = Path(ligand_path)

    if ligand_path.suffix.lower() == '.mol2':
        # Read MOL2 file
        with open(ligand_path) as f:
            mol2_content = f.read()

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

        # Parse and store original atom names from MOL2
        atom_section_started = False
        atom_data = []
        for line in mol2_content.split('\n'):
            if line.startswith('@<TRIPOS>ATOM'):
                atom_section_started = True
                continue
            if atom_section_started:
                if line.startswith('@<TRIPOS>') or not line.strip():
                    break
                parts = line.split()
                if len(parts) >= 7:
                    atom_idx = int(parts[0])  # 1-indexed
                    atom_name = parts[1]
                    atom_data.append((atom_idx, atom_name))

        # Store original atom names
        for atom_idx_mol2, atom_name in atom_data:
            rdkit_idx = atom_idx_mol2 - 1  # Convert to 0-indexed
            if rdkit_idx < mol.GetNumAtoms():
                atom = mol.GetAtomWithIdx(rdkit_idx)
                atom.SetProp("_original_atom_name", atom_name)
                atom.SetProp("name", atom_name)

        original_positions = _snapshot_conformer_positions(mol)
        if not original_positions:
            raise ValueError(f"MOL2 ligand has no 3D conformer: {ligand_path}")
        mol, renamed = _ensure_unique_ligand_atom_names(mol)
        _restore_conformer_positions(mol, original_positions)
        print(f"Loaded ligand from MOL2: {mol.GetNumAtoms()} atoms (renamed: {renamed})")
        return mol

    elif ligand_path.suffix.lower() in {'.sdf', '.sd'}:
        # Read SDF file
        supplier = Chem.SDMolSupplier(
            str(ligand_path),
            sanitize=False,
            removeHs=False,
            strictParsing=False,
        )
        mol = next((m for m in supplier if m is not None), None)
        if mol is None:
            raise ValueError(f"Failed to read SDF file: {ligand_path}")
        original_positions = _snapshot_conformer_positions(mol)
        if not original_positions:
            raise ValueError(f"SDF ligand has no 3D conformer: {ligand_path}")
        mol, renamed = _ensure_unique_ligand_atom_names(mol)
        _restore_conformer_positions(mol, original_positions)
        print(f"Loaded ligand from SDF: {mol.GetNumAtoms()} atoms (renamed: {renamed})")
        return mol

    elif ligand_path.suffix.lower() == '.mol':
        mol = Chem.MolFromMolFile(str(ligand_path), sanitize=False, removeHs=False)
        if mol is None:
            raise ValueError(f"Failed to read MOL file: {ligand_path}")
        original_positions = _snapshot_conformer_positions(mol)
        if not original_positions:
            raise ValueError(f"MOL ligand has no 3D conformer: {ligand_path}")
        mol, renamed = _ensure_unique_ligand_atom_names(mol)
        _restore_conformer_positions(mol, original_positions)
        print(f"Loaded ligand from MOL: {mol.GetNumAtoms()} atoms (renamed: {renamed})")
        return mol

    elif ligand_path.suffix.lower() in {'.pdb', '.ent'}:
        # Read PDB file
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
        original_positions = _snapshot_conformer_positions(mol)
        if not original_positions:
            raise ValueError(f"PDB ligand has no 3D conformer: {ligand_path}")
        mol, renamed = _ensure_unique_ligand_atom_names(mol)
        _restore_conformer_positions(mol, original_positions)
        print(f"Loaded ligand from PDB: {mol.GetNumAtoms()} atoms (renamed: {renamed})")
        return mol

    else:
        raise ValueError(f"Unsupported ligand file format: {ligand_path.suffix}")


def _fix_cif_entity_ids(cif_file: Path) -> None:
    """Fix entity IDs in CIF file to remove special characters like '!'.

    gemmi's make_mmcif_document() sometimes adds special characters to entity IDs
    to differentiate them, which causes parsing errors in Boltz.
    """
    import re

    with open(cif_file, 'r') as f:
        content = f.read()

    # Fix entity IDs in _entity.id section (format: "ID type" at start of line)
    content = re.sub(r'^([A-Z][A-Z0-9]*)!\s+', r'\1 ', content, flags=re.MULTILINE)

    # Fix entity IDs in _struct_asym.entity_id column (format: "asym_id entity_id!")
    content = re.sub(r'([A-Za-z0-9]+)\s+([A-Z][A-Z0-9]*)(!)\s*$', r'\1 \2', content, flags=re.MULTILINE)

    # Fix entity IDs in atom records (format: "... LIG! ...")
    # This handles label_entity_id column in ATOM/HETATM records
    content = re.sub(r'([A-Z][A-Z0-9]*)(!)\s+\.', r'\1 .', content)  # Before a period
    content = re.sub(r'([A-Z][A-Z0-9]*)(!)\s+\?', r'\1 ?', content)  # Before a question mark
    content = re.sub(r'([A-Z][A-Z0-9]*)(!)(\s+)', r'\1\3', content)  # Before whitespace

    with open(cif_file, 'w') as f:
        f.write(content)


def _validate_unique_atom_ids_for_writer(input_path: Path, max_items: int = 20) -> None:
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
        alt_loc = line[16:17].strip() or ""
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
        _fix_cif_entity_ids(cif_path)
    return renamed


def _filter_structure_by_chains(
    input_path: Path,
    target_chains: Sequence[str],
    ligand_chains: Sequence[str],
    output_path: Path,
) -> None:
    structure = gemmi.read_structure(str(input_path))
    keep = {c.strip() for c in (list(target_chains) + list(ligand_chains)) if c.strip()}
    if not keep:
        raise ValueError("No chains specified for affinity filtering.")

    model = structure[0]
    existing = {chain.name for chain in model}

    resolved_keep = set()
    missing = []

    def _load_auth_to_label_map() -> dict[str, set[str]]:
        mapping: dict[str, set[str]] = {}
        if input_path.suffix.lower() not in {".cif", ".mmcif"}:
            return mapping
        try:
            doc = gemmi.cif.read(str(input_path))
            block = doc[0]
            label_col = block.find_values("_struct_asym.id")
            auth_col = block.find_values("_struct_asym.pdbx_auth_asym_id")
            if label_col and auth_col and len(label_col) == len(auth_col):
                for i in range(len(label_col)):
                    label = label_col[i]
                    auth = auth_col[i]
                    if auth and label:
                        mapping.setdefault(auth, set()).add(label)
        except Exception:
            return {}
        return mapping

    def _chain_variants(value: str) -> set[str]:
        v = value.strip()
        if not v:
            return set()
        variants = {v, v.lower()}
        lead = v.lstrip("0123456789")
        if lead:
            variants.add(lead)
            variants.add(lead.lower())
        trail = v.rstrip("0123456789")
        if trail:
            variants.add(trail)
            variants.add(trail.lower())
        no_digits = "".join(c for c in v if not c.isdigit())
        if no_digits:
            variants.add(no_digits)
            variants.add(no_digits.lower())
        alnum = "".join(c for c in v if c.isalnum())
        if alnum:
            variants.add(alnum)
            variants.add(alnum.lower())
        return variants

    existing_lower = {c.lower(): c for c in existing}
    for chain_id in keep:
        if chain_id in existing:
            resolved_keep.add(chain_id)
            continue
        if chain_id.lower() in existing_lower:
            resolved_keep.add(existing_lower[chain_id.lower()])
            continue
        missing.append(chain_id)

    suggestion_map: dict[str, list[str]] = {}
    auth_to_label: dict[str, set[str]] = {}
    if missing:
        auth_to_label = _load_auth_to_label_map()
        variant_map: dict[str, set[str]] = {}
        for chain_name in existing:
            for key in _chain_variants(chain_name):
                variant_map.setdefault(key, set()).add(chain_name)
        for auth_key, labels in auth_to_label.items():
            for key in _chain_variants(auth_key):
                variant_map.setdefault(key, set()).update(labels)

        unresolved = []
        for chain_id in missing:
            if chain_id in auth_to_label:
                mapped = [c for c in auth_to_label[chain_id] if c in existing]
                if mapped:
                    resolved_keep.update(mapped)
                    continue

            variant_candidates: set[str] = set()
            for key in _chain_variants(chain_id):
                variant_candidates.update(variant_map.get(key, set()))
            variant_candidates = {c for c in variant_candidates if c in existing}
            if len(variant_candidates) == 1:
                resolved_keep.add(next(iter(variant_candidates)))
                continue
            if variant_candidates:
                suggestion_map[chain_id] = sorted(variant_candidates)
            unresolved.append(chain_id)
        missing = unresolved

    if missing:
        auth_keys = ", ".join(sorted(auth_to_label.keys()))
        available = ", ".join(sorted(existing))
        suggestions = ""
        if suggestion_map:
            parts = [
                f"{key}→{','.join(values)}" for key, values in suggestion_map.items()
            ]
            suggestions = f" Suggestions: {'; '.join(parts)}."
        raise ValueError(
            f"Chains not found in structure: {', '.join(missing)}. "
            f"Available chains: {available or 'none'}. "
            f"Auth chain IDs: {auth_keys or 'none'}."
            f"{suggestions}"
        )

    remove_names = [chain.name for chain in model if chain.name not in resolved_keep]
    for chain_name in remove_names:
        model.remove_chain(chain_name)

    structure.remove_empty_chains()
    structure.setup_entities()

    # Ensure polymer sequences are defined for mmCIF parsing
    for entity in structure.entities:
        if entity.entity_type.name != "Polymer":
            continue
        if not entity.subchains:
            continue
        seq = []
        for chain in structure[0]:
            for res in chain:
                if res.subchain in entity.subchains:
                    seq.append(res.name)
        if seq:
            entity.full_sequence = seq

    doc = structure.make_mmcif_document()
    doc.write_file(str(output_path))


def _run_affinity(
    complex_file: Path,
    output_dir: Path,
    cache_dir: Path,
    result_id: str,
    accelerator: str,
    devices: int,
    affinity_refine: bool = False,
    seed: int | None = None,
    work_dir: Path | None = None,
) -> Optional[dict]:
    try:
        import sys
        repo_root = Path(__file__).resolve().parents[1]
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        import affinity.main as affinity_main
        if getattr(affinity_main, "_ccd_name_manager", None) is not None:
            affinity_main._ccd_name_manager.redis_client = None
        Boltzina = affinity_main.Boltzina
    except Exception as exc:  # noqa: BLE001
        print(f"[Warning] Failed to import affinity module: {exc}")
        return None

    affinity_ckpt = cache_dir / "boltz2_aff.ckpt"
    if not affinity_ckpt.exists():
        print(
            f"[Warning] Affinity checkpoint not found: {affinity_ckpt}. Skipping affinity."
        )
        return None

    os.environ["BOLTZ_CACHE"] = str(cache_dir)

    affinity_out = output_dir / "affinity"
    affinity_work = (work_dir / "affinity_work") if work_dir else (affinity_out / "work")
    affinity_out.mkdir(parents=True, exist_ok=True)
    affinity_work.mkdir(parents=True, exist_ok=True)

    boltzina = Boltzina(
        output_dir=str(affinity_out),
        work_dir=str(affinity_work),
        # Enable diffusion refinement for affinity if requested
        skip_run_structure=not affinity_refine,
        use_kernels=False,
        run_trunk_and_structure=True,
        accelerator=accelerator,
        devices=devices,
        num_workers=0,
        seed=seed,
    )
    print(
        f"[Info] Running affinity with "
        f"{'diffusion refinement' if affinity_refine else 'input-structure scoring'} "
        f"(seed={seed if seed is not None else 'none'})."
    )

    boltzina.predict([str(complex_file)])
    if not boltzina.results:
        return None

    result = dict(boltzina.results[0])
    result["input_file"] = str(complex_file)
    affinity_json = output_dir / result_id / f"affinity_{result_id}.json"
    affinity_json.parent.mkdir(parents=True, exist_ok=True)
    affinity_json.write_text(json.dumps(result, indent=2))
    return result


def _write_chain_map(processed_dir: Path, output_dir: Path, record_id: str) -> None:
    try:
        structure = StructureV2.load(
            processed_dir / "structures" / f"{record_id}.npz"
        )
        structure = structure.remove_invalid_chains()
        chain_map = {
            str(idx): str(chain["name"])
            for idx, chain in enumerate(structure.chains)
        }
        chain_map_path = output_dir / record_id / "chain_map.json"
        chain_map_path.parent.mkdir(parents=True, exist_ok=True)
        chain_map_path.write_text(json.dumps(chain_map, indent=2))
    except Exception as exc:  # noqa: BLE001
        print(f"[Warning] Failed to write chain map: {exc}")


def _collect_atom_coverage(processed_dir: Path, record_id: str) -> dict:
    """Collect per-chain atom presence diagnostics from processed inputs."""
    structure = StructureV2.load(processed_dir / "structures" / f"{record_id}.npz")
    structure = structure.remove_invalid_chains()

    chain_type_labels = {value: key.lower() for key, value in const.chain_type_ids.items()}
    ligand_type = const.chain_type_ids["NONPOLYMER"]

    chain_stats = []
    ligand_stats = []
    for chain in structure.chains:
        chain_name = str(chain["name"])
        mol_type = int(chain["mol_type"])
        atom_start = int(chain["atom_idx"])
        atom_end = atom_start + int(chain["atom_num"])
        atoms = structure.atoms[atom_start:atom_end]

        total_atoms = int(len(atoms))
        present_atoms = int(atoms["is_present"].sum()) if total_atoms else 0
        present_fraction = (present_atoms / total_atoms) if total_atoms else 0.0

        res_start = int(chain["res_idx"])
        res_end = res_start + int(chain["res_num"])
        residues = structure.residues[res_start:res_end]
        residue_names = sorted({str(res["name"]) for res in residues})

        item = {
            "chain": chain_name,
            "mol_type": chain_type_labels.get(mol_type, str(mol_type)),
            "total_atoms": total_atoms,
            "present_atoms": present_atoms,
            "present_fraction": present_fraction,
            "residue_names": residue_names,
        }
        chain_stats.append(item)
        if mol_type == ligand_type:
            ligand_stats.append(item)

    return {
        "record_id": record_id,
        "chain_atom_coverage": chain_stats,
        "ligand_atom_coverage": ligand_stats,
    }


def _write_atom_coverage(processed_dir: Path, output_dir: Path, record_id: str) -> None:
    """Write atom coverage diagnostics and attach summary to confidence JSON."""
    try:
        coverage = _collect_atom_coverage(processed_dir, record_id)
        struct_dir = output_dir / record_id
        struct_dir.mkdir(parents=True, exist_ok=True)

        diag_path = struct_dir / f"ligand_atom_coverage_{record_id}.json"
        diag_path.write_text(json.dumps(coverage, indent=2))

        for conf_path in sorted(struct_dir.glob(f"confidence_{record_id}_model_*.json")):
            try:
                data = json.loads(conf_path.read_text())
            except Exception:
                continue
            data["ligand_atom_coverage"] = coverage["ligand_atom_coverage"]
            data["chain_atom_coverage"] = coverage["chain_atom_coverage"]
            conf_path.write_text(json.dumps(data, indent=2))
    except Exception as exc:  # noqa: BLE001
        print(f"[Warning] Failed to write atom coverage diagnostics: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Boltz2Score on a single PDB/mmCIF (one step)."
    )
    parser.add_argument(
        "--input",
        required=False,
        type=str,
        help="Input structure file (.pdb/.cif/.mmcif)",
    )
    parser.add_argument(
        "--output_dir",
        required=True,
        type=str,
        help="Output directory for score results",
    )
    parser.add_argument(
        "--cache",
        type=str,
        default=None,
        help="Boltz cache directory (default: BOLTZ_CACHE or ~/.boltz)",
    )
    parser.add_argument(
        "--checkpoint",
        type=str,
        default=None,
        help="Path to boltz2_conf.ckpt (default: <cache>/boltz2_conf.ckpt)",
    )
    parser.add_argument("--devices", type=int, default=1)
    parser.add_argument(
        "--accelerator",
        type=str,
        default="gpu" if torch.cuda.is_available() else "cpu",
    )
    parser.add_argument(
        "--num_workers",
        type=int,
        default=0,
        help="DataLoader workers (default: 0 for compatibility)",
    )
    parser.add_argument(
        "--output_format",
        type=str,
        default="mmcif",
        choices=["pdb", "mmcif"],
    )
    parser.add_argument("--recycling_steps", type=int, default=3)
    parser.add_argument("--sampling_steps", type=int, default=1)
    parser.add_argument("--diffusion_samples", type=int, default=1)
    parser.add_argument("--max_parallel_samples", type=int, default=1)
    parser.add_argument("--step_scale", type=float, default=1.5)
    parser.add_argument("--no_kernels", action="store_true")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument(
        "--work_dir",
        type=str,
        default=None,
        help="Optional work dir to keep processed intermediates",
    )
    parser.add_argument(
        "--keep_work",
        action="store_true",
        help="Keep temporary work directory (default: delete)",
    )
    parser.add_argument(
        "--target_chain",
        type=str,
        default=None,
        help="Target protein chain ID(s), comma-separated (enables affinity if set with --ligand_chain)",
    )
    parser.add_argument(
        "--ligand_chain",
        type=str,
        default=None,
        help="Ligand chain ID(s), comma-separated (enables affinity if set with --target_chain)",
    )
    parser.add_argument(
        "--affinity_refine",
        action="store_true",
        help="Run diffusion refinement before affinity (higher quality, slower).",
    )
    parser.add_argument(
        "--enable_affinity",
        action="store_true",
        help="Force affinity prediction (requires both --target_chain and --ligand_chain).",
    )
    parser.add_argument(
        "--auto_enable_affinity",
        action="store_true",
        help="Compatibility flag for API clients; affinity runs only when both chains are provided.",
    )
    parser.add_argument(
        "--protein_file",
        type=str,
        default=None,
        help="Protein structure file (.pdb/.cif/.mmcif) for separate input mode",
    )
    parser.add_argument(
        "--ligand_file",
        type=str,
        default=None,
        help="Ligand structure file (.sdf/.mol/.mol2/.pdb) for separate input mode",
    )
    parser.add_argument(
        "--ligand_smiles_map",
        type=str,
        default=None,
        help="Optional JSON map of ligand chain (or 'chain:resname') to SMILES for topology override.",
    )

    args = parser.parse_args()

    # Validate input arguments
    has_input = args.input is not None
    has_separate = args.protein_file is not None and args.ligand_file is not None

    if not has_input and not has_separate:
        parser.error("Either --input or both --protein_file and --ligand_file must be provided")
    if has_input and has_separate:
        parser.error("Cannot use both --input and separate --protein_file/--ligand_file options")
    if args.protein_file and not args.ligand_file:
        parser.error("--ligand_file is required when using --protein_file")
    if args.ligand_file and not args.protein_file:
        parser.error("--protein_file is required when using --ligand_file")

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    cache_dir = Path(args.cache or get_cache_path()).expanduser().resolve()

    # Initialize work_dir early (needed for separate file mode)
    if args.work_dir:
        work_dir = Path(args.work_dir).expanduser().resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
        cleanup = False
    else:
        work_dir = Path(
            tempfile.mkdtemp(prefix="boltz2score_", dir=output_dir)
        )
        cleanup = not args.keep_work

    preloaded_custom_mols: dict[str, Chem.Mol] | None = None
    ligand_smiles_map: dict[str, str] = {}
    if args.ligand_smiles_map:
        try:
            parsed_map = json.loads(args.ligand_smiles_map)
            if isinstance(parsed_map, dict):
                for key, value in parsed_map.items():
                    if not isinstance(key, str) or not isinstance(value, str):
                        continue
                    key = key.strip()
                    value = value.strip()
                    if key and value:
                        ligand_smiles_map[key] = value
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Invalid --ligand_smiles_map JSON: {exc}") from exc

    # Check for separate protein and ligand file mode
    if args.protein_file and args.ligand_file:
        # Separate input mode: combine protein and ligand files
        protein_path = Path(args.protein_file).expanduser().resolve()
        ligand_path = Path(args.ligand_file).expanduser().resolve()

        if not protein_path.exists():
            raise FileNotFoundError(f"Protein file not found: {protein_path}")
        if not ligand_path.exists():
            raise FileNotFoundError(f"Ligand file not found: {ligand_path}")

        # Combine protein and ligand files into a single structure
        from pathlib import Path as PathLib
        import gemmi
        from rdkit import Chem

        Chem.SetDefaultPickleProperties(Chem.PropertyPickleOptions.AllProps)

        # Load ligand from file
        ligand_mol = _load_ligand_from_file(ligand_path)
        if ligand_mol is None:
            raise ValueError(f"Failed to load ligand from {ligand_path}")
        preloaded_custom_mols = {"LIG": Chem.Mol(ligand_mol)}

        # Read protein structure
        if protein_path.suffix.lower() in {'.pdb', '.ent'}:
            structure = gemmi.read_structure(str(protein_path))
        elif protein_path.suffix.lower() in {'.cif', '.mmcif'}:
            structure = gemmi.read_structure(str(protein_path))
        else:
            raise ValueError(f"Unsupported protein file format: {protein_path.suffix}")

        # Setup entities for protein first
        structure.setup_entities()

        # Add ligand as a separate chain
        ligand_chain = gemmi.Chain("L")
        residue = gemmi.Residue()
        residue.name = "LIG"
        residue.seqid = gemmi.SeqId(1, " ")

        # Add atoms from ligand molecule
        conf = ligand_mol.GetConformer()
        for i in range(ligand_mol.GetNumAtoms()):
            atom = ligand_mol.GetAtomWithIdx(i)
            pos = conf.GetAtomPosition(i)
            gemmi_atom = gemmi.Atom()
            # Get atom name - prefer original atom name
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

        # Re-setup entities to properly classify ligand as NonPolymer
        structure.setup_entities()

        # Ensure polymer sequences are defined for mmCIF parsing
        # This is needed when PDB files lack SEQRES records
        for entity in structure.entities:
            if entity.entity_type.name != "Polymer":
                continue
            if not entity.subchains:
                continue
            seq = []
            for chain in structure[0]:
                for res in chain:
                    if res.subchain in entity.subchains:
                        seq.append(res.name)
            if seq:
                entity.full_sequence = seq

        # Write combined structure as CIF
        combined_dir = work_dir / "combined"
        combined_dir.mkdir(parents=True, exist_ok=True)
        combined_file = combined_dir / "combined_complex.cif"

        doc = structure.make_mmcif_document()
        doc.write_file(str(combined_file))

        print(f"Created combined structure: {combined_file}")
        print(f"  Protein: {protein_path.name}")
        print(f"  Ligand: {ligand_path.name}")

        # Fix entity IDs in CIF to remove special characters
        _fix_cif_entity_ids(combined_file)

        # Use the combined file for further processing
        input_path = combined_file
    else:
        # Standard single file mode
        input_path = Path(args.input).expanduser().resolve()
        if not input_path.exists():
            raise FileNotFoundError(f"Input not found: {input_path}")

    record_id = input_path.stem

    # Optional affinity prediction (requires target + ligand chains)
    target_chains = _parse_chain_list(args.target_chain)
    ligand_chains = _parse_chain_list(args.ligand_chain)
    run_affinity = bool(target_chains) and bool(ligand_chains)

    if (target_chains or ligand_chains) and not run_affinity:
        msg = (
            "Affinity needs both --target_chain and --ligand_chain. "
            "Skipping affinity and keeping scoring results only."
        )
        if args.enable_affinity:
            raise ValueError(msg)
        print(
            "[Warning] "
            f"{msg} Got target={target_chains or 'none'}, ligand={ligand_chains or 'none'}."
        )
    elif args.enable_affinity and not run_affinity:
        raise ValueError(
            "Affinity needs both --target_chain and --ligand_chain. "
            "Use both flags or omit --enable_affinity."
        )

    if run_affinity:
        if set(target_chains) & set(ligand_chains):
            raise ValueError("Target and ligand chains must be different.")
        shared_subset_input = work_dir / f"{record_id}_shared_subset.cif"
        _filter_structure_by_chains(
            input_path=input_path,
            target_chains=target_chains,
            ligand_chains=ligand_chains,
            output_path=shared_subset_input,
        )
        input_path = shared_subset_input
        print(
            f"[Info] Locked shared chain subset for Boltz2Score + Boltzina: "
            f"target={target_chains}, ligand={ligand_chains}, file={input_path}"
        )

    # Create isolated input dir with the single structure
    input_dir = work_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    staged_suffix = input_path.suffix if input_path.suffix else ".cif"
    staged_input = input_dir / f"{record_id}{staged_suffix.lower()}"
    if staged_input.exists():
        staged_input.unlink()
    shutil.copy2(input_path, staged_input)
    renamed = 0
    if staged_input.suffix.lower() in {".pdb", ".ent"}:
        renamed = _normalize_pdb_duplicate_atom_ids_for_writer(staged_input)
    elif staged_input.suffix.lower() in {".cif", ".mmcif"}:
        renamed = _normalize_cif_duplicate_atom_ids_for_writer(staged_input)
    if renamed:
        print(
            f"[Info] Normalized {renamed} duplicate atom IDs in "
            f"{staged_input.name} for mmCIF writer compatibility."
        )
    _validate_unique_atom_ids_for_writer(staged_input)

    # Prepare processed inputs (structure scoring)
    prepare_inputs(
        input_dir=input_dir,
        out_dir=work_dir,
        cache_dir=cache_dir,
        recursive=False,
        preloaded_custom_mols=preloaded_custom_mols,
        ligand_smiles_map=ligand_smiles_map if ligand_smiles_map else None,
    )

    # Run scoring
    run_scoring(
        processed_dir=work_dir / "processed",
        output_dir=output_dir,
        cache_dir=cache_dir,
        checkpoint=Path(args.checkpoint) if args.checkpoint else None,
        devices=args.devices,
        accelerator=args.accelerator,
        num_workers=args.num_workers,
        output_format=args.output_format,
        recycling_steps=args.recycling_steps,
        sampling_steps=args.sampling_steps,
        diffusion_samples=args.diffusion_samples,
        max_parallel_samples=args.max_parallel_samples,
        step_scale=args.step_scale,
        no_kernels=args.no_kernels,
        seed=args.seed,
    )

    _write_chain_map(
        processed_dir=work_dir / "processed",
        output_dir=output_dir,
        record_id=record_id,
    )
    _write_atom_coverage(
        processed_dir=work_dir / "processed",
        output_dir=output_dir,
        record_id=record_id,
    )

    if run_affinity:
        print(f"[Info] Affinity input source locked to scoring input path: {staged_input}")
        _run_affinity(
            complex_file=staged_input,
            output_dir=output_dir,
            cache_dir=cache_dir,
            result_id=record_id,
            accelerator=args.accelerator,
            devices=args.devices,
            affinity_refine=args.affinity_refine,
            seed=args.seed,
            work_dir=work_dir,
        )

    if cleanup:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
