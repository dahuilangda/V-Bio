from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path
from typing import Sequence

import gemmi
import numpy as np

from boltz.data import const
from boltz.data.types import InferenceOptions, Manifest, StructureV2, TemplateInfo
from utils.ligand_alignment import is_hydrogen_like
from utils.ligand_utils import fix_cif_entity_ids

WATER_RESNAMES = {"HOH", "WAT", "H2O"}
ION_RESNAMES = {
    "NA",
    "K",
    "CL",
    "CA",
    "MG",
    "ZN",
    "MN",
    "FE",
    "CU",
    "CO",
    "NI",
    "CD",
    "HG",
    "BR",
    "IOD",
    "I",
}
PROTEIN_CHAIN_TYPE = const.chain_type_ids["PROTEIN"]


def filter_structure_by_chains(
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
            parts = [f"{key}→{','.join(values)}" for key, values in suggestion_map.items()]
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

    doc = structure.make_mmcif_document()
    doc.write_file(str(output_path))

def _chain_name_matches(candidate: str, requested: str) -> bool:
    cand = str(candidate or "").strip().upper()
    req = str(requested or "").strip().upper()
    if not cand or not req:
        return False
    return cand == req or cand.startswith(f"{req}X") or req.startswith(f"{cand}X")


def _iter_chain_residues(structure: StructureV2, chain: object):
    res_start = int(chain["res_idx"])
    res_end = res_start + int(chain["res_num"])
    for residue in structure.residues[res_start:res_end]:
        yield residue


def _heavy_atom_coords_from_atom_block(atom_block: object) -> list[tuple[float, float, float]]:
    coords: list[tuple[float, float, float]] = []
    for atom in atom_block:
        atom_name = str(atom["name"] or "").strip()
        if is_hydrogen_like(atom_name):
            continue
        xyz = atom["coords"]
        coords.append((float(xyz[0]), float(xyz[1]), float(xyz[2])))
    return coords


def _heavy_atom_entries_from_atom_block(
    atom_block: object,
    atom_index_offset: int = 0,
) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for local_idx, atom in enumerate(atom_block):
        atom_name = str(atom["name"] or "").strip()
        if is_hydrogen_like(atom_name):
            continue
        xyz = atom["coords"]
        entries.append(
            {
                "atom_idx": int(atom_index_offset + local_idx),
                "atom_name": atom_name,
                "coords": (float(xyz[0]), float(xyz[1]), float(xyz[2])),
            }
        )
    return entries


def _min_pair_distance(
    coords_a: Sequence[tuple[float, float, float]],
    coords_b: Sequence[tuple[float, float, float]],
) -> float:
    best = float("inf")
    for ax, ay, az in coords_a:
        for bx, by, bz in coords_b:
            dx = ax - bx
            dy = ay - by
            dz = az - bz
            dist_sq = dx * dx + dy * dy + dz * dz
            if dist_sq < best:
                best = dist_sq
    return best ** 0.5 if best < float("inf") else float("inf")


def _select_pose_anchor_atoms(
    ligand_atoms: Sequence[dict[str, object]],
    max_atoms: int,
) -> list[dict[str, object]]:
    if max_atoms <= 0 or len(ligand_atoms) <= max_atoms:
        return list(ligand_atoms)
    coords = np.array([atom["coords"] for atom in ligand_atoms], dtype=float)
    centroid = coords.mean(axis=0)
    selected: list[int] = [int(np.argmax(np.linalg.norm(coords - centroid[None, :], axis=1)))]
    while len(selected) < max_atoms:
        best_idx = None
        best_dist = -1.0
        for idx in range(len(ligand_atoms)):
            if idx in selected:
                continue
            min_dist = min(float(np.linalg.norm(coords[idx] - coords[j])) for j in selected)
            if min_dist > best_dist:
                best_dist = min_dist
                best_idx = idx
        if best_idx is None:
            break
        selected.append(best_idx)
    return [ligand_atoms[idx] for idx in selected]


def configure_anchored_refine_constraints(
    processed_dir: Path,
    record_id: str,
    requested_ligand_chain_id: str | None,
    requested_target_chains: Sequence[str],
    contact_cutoff: float,
    max_distance: float,
    max_residues: int,
    pose_anchor_atoms: int,
    pose_anchor_slack: float,
    anchor_strategy: str,
    output_dir: Path | None = None,
) -> dict[str, object]:
    manifest_path = processed_dir / "manifest.json"
    structure_path = processed_dir / "structures" / f"{record_id}.npz"
    manifest = Manifest.load(manifest_path)
    structure = StructureV2.load(structure_path).remove_invalid_chains()

    record_idx = next((idx for idx, rec in enumerate(manifest.records) if rec.id == record_id), None)
    if record_idx is None:
        raise RuntimeError(f"Record {record_id!r} not found in manifest {manifest_path}")
    record = manifest.records[record_idx]

    ligand_chain = None
    ligand_candidates = []
    for chain in structure.chains:
        if int(chain["mol_type"]) != const.chain_type_ids["NONPOLYMER"]:
            continue
        chain_name = str(chain["name"] or "").strip()
        ligand_candidates.append((int(chain["asym_id"]), chain_name))
        if requested_ligand_chain_id and _chain_name_matches(chain_name, requested_ligand_chain_id):
            ligand_chain = chain
            break
    if ligand_chain is None:
        if len(ligand_candidates) == 1:
            ligand_chain = next(
                chain for chain in structure.chains if int(chain["asym_id"]) == ligand_candidates[0][0]
            )
        elif not requested_ligand_chain_id:
            preferred_candidates = [
                (asym_id, chain_name)
                for asym_id, chain_name in ligand_candidates
                if str(chain_name or "").strip().upper().startswith("L")
            ]
            if len(preferred_candidates) == 1:
                ligand_chain = next(
                    chain for chain in structure.chains if int(chain["asym_id"]) == preferred_candidates[0][0]
                )
        elif requested_ligand_chain_id:
            raise RuntimeError(
                "Failed to resolve ligand chain for anchored refinement. "
                f"Requested={requested_ligand_chain_id!r}, available={ligand_candidates}."
            )
        else:
            raise RuntimeError(
                "Anchored refinement requires exactly one nonpolymer ligand chain when --ligand_chain is not provided. "
                f"Available ligand chains: {ligand_candidates}."
            )

    ligand_chain_name = str(ligand_chain["name"] or "").strip()
    ligand_asym_id = int(ligand_chain["asym_id"])
    ligand_atom_start = int(ligand_chain["atom_idx"])
    ligand_atom_end = ligand_atom_start + int(ligand_chain["atom_num"])
    ligand_coords = _heavy_atom_coords_from_atom_block(structure.atoms[ligand_atom_start:ligand_atom_end])
    ligand_atom_entries = _heavy_atom_entries_from_atom_block(
        structure.atoms[ligand_atom_start:ligand_atom_end],
        atom_index_offset=ligand_atom_start,
    )
    if not ligand_coords:
        raise RuntimeError("Anchored refinement could not find heavy atoms in the ligand chain.")

    requested_targets = {str(chain_id or "").strip() for chain_id in requested_target_chains if str(chain_id or "").strip()}
    target_chains = []
    for chain in structure.chains:
        if int(chain["mol_type"]) == const.chain_type_ids["NONPOLYMER"]:
            continue
        chain_name = str(chain["name"] or "").strip()
        if requested_targets and not any(_chain_name_matches(chain_name, req) for req in requested_targets):
            continue
        target_chains.append(chain)
    if not target_chains:
        raise RuntimeError(
            "Anchored refinement could not find target polymer chains. "
            f"Requested targets: {sorted(requested_targets) or 'all polymer chains'}."
        )

    contact_rows: list[dict[str, object]] = []
    for chain in target_chains:
        chain_name = str(chain["name"] or "").strip()
        asym_id = int(chain["asym_id"])
        for residue in _iter_chain_residues(structure, chain):
            atom_start = int(residue["atom_idx"])
            atom_end = atom_start + int(residue["atom_num"])
            residue_coords = _heavy_atom_coords_from_atom_block(structure.atoms[atom_start:atom_end])
            if not residue_coords:
                continue
            min_distance = _min_pair_distance(ligand_coords, residue_coords)
            if min_distance > float(contact_cutoff):
                continue
            contact_rows.append(
                {
                    "chain_name": chain_name,
                    "asym_id": asym_id,
                    "res_idx": int(residue["res_idx"]),
                    "res_name": str(residue["name"] or "").strip(),
                    "min_distance": float(min_distance),
                }
            )

    contact_rows.sort(key=lambda row: (row["min_distance"], row["chain_name"], row["res_idx"]))
    if max_residues > 0:
        contact_rows = contact_rows[:max_residues]
    if not contact_rows:
        raise RuntimeError(
            "Anchored refinement did not find any pocket residues near the ligand. "
            f"Try increasing --anchor_contact_cutoff (current {contact_cutoff:.2f} A)."
        )

    pocket_contacts = [(int(row["asym_id"]), int(row["res_idx"])) for row in contact_rows]
    pose_contact_constraints: list[tuple[tuple[int, int], tuple[int, int], float, bool]] = []
    if pose_anchor_atoms > 0 and pose_anchor_slack > 0:
        residue_coord_map: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
        for chain in target_chains:
            asym_id = int(chain["asym_id"])
            for residue in _iter_chain_residues(structure, chain):
                key = (asym_id, int(residue["res_idx"]))
                if key not in pocket_contacts:
                    continue
                atom_start = int(residue["atom_idx"])
                atom_end = atom_start + int(residue["atom_num"])
                residue_coord_map[key] = _heavy_atom_coords_from_atom_block(structure.atoms[atom_start:atom_end])

        for atom in _select_pose_anchor_atoms(ligand_atom_entries, max_atoms=pose_anchor_atoms):
            atom_coords = [atom["coords"]]
            best_key = None
            best_dist = float("inf")
            for residue_key, residue_coords in residue_coord_map.items():
                dist = _min_pair_distance(atom_coords, residue_coords)
                if dist < best_dist:
                    best_dist = dist
                    best_key = residue_key
            if best_key is None or not math.isfinite(best_dist):
                continue
            pose_contact_constraints.append(
                (
                    (ligand_asym_id, int(atom["atom_idx"])),
                    best_key,
                    float(best_dist + pose_anchor_slack),
                    True,
                )
            )

    # Boltz2 contact constraints are soft token-contact potentials, not rigid-body
    # pose restraints. Applying many ligand atom-level contacts lets the model
    # satisfy them by tearing ligand internal geometry instead of moving the
    # ligand as a whole. Keep the computed atom anchors for diagnostics, but
    # only emit pocket-level constraints into inference.
    pocket_constraints = [(ligand_asym_id, pocket_contacts, float(max_distance), True)]
    contact_constraints = None
    applied_anchor_strategy = "pocket_only"

    inference_options = InferenceOptions(
        pocket_constraints=pocket_constraints,
        contact_constraints=contact_constraints,
    )
    updated_record = replace(record, inference_options=inference_options)
    manifest.records[record_idx] = updated_record
    manifest.dump(manifest_path)

    summary = {
        "record_id": record_id,
        "ligand_chain_name": ligand_chain_name,
        "ligand_asym_id": ligand_asym_id,
        "target_chain_names": [str(chain["name"] or "").strip() for chain in target_chains],
        "contact_cutoff_angstrom": float(contact_cutoff),
        "contact_max_distance_angstrom": float(max_distance),
        "requested_anchor_strategy": anchor_strategy,
        "applied_anchor_strategy": applied_anchor_strategy,
        "max_residues": int(max_residues),
        "contact_residue_count": len(contact_rows),
        "contact_residues": contact_rows,
        "pose_anchor_atom_count": len(pose_contact_constraints),
        "pose_anchor_slack_angstrom": float(pose_anchor_slack),
        "pose_anchor_constraints": [
            {
                "ligand_asym_id": int(src[0]),
                "ligand_atom_idx": int(src[1]),
                "target_asym_id": int(dst[0]),
                "target_res_idx": int(dst[1]),
                "max_distance": float(threshold),
            }
            for src, dst, threshold, _force in pose_contact_constraints
        ],
    }
    if output_dir is not None:
        summary_path = output_dir / record_id / "anchored_refine_constraints.json"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def configure_distal_self_templates(
    processed_dir: Path,
    record_id: str,
    contact_rows: Sequence[dict[str, object]],
    template_threshold: float,
    pocket_margin: int,
) -> dict[str, object]:
    manifest_path = processed_dir / "manifest.json"
    manifest = Manifest.load(manifest_path)
    record_idx = next((idx for idx, rec in enumerate(manifest.records) if rec.id == record_id), None)
    if record_idx is None:
        raise RuntimeError(f"Record {record_id!r} not found in manifest {manifest_path}")
    record = manifest.records[record_idx]

    blocked_by_chain: dict[str, set[int]] = {}
    for row in contact_rows:
        chain_name = str(row.get("chain_name") or "").strip()
        res_idx = int(row.get("res_idx"))
        blocked = blocked_by_chain.setdefault(chain_name, set())
        for pos in range(res_idx - pocket_margin, res_idx + pocket_margin + 1):
            if pos >= 1:
                blocked.add(pos)

    template_records: list[TemplateInfo] = []
    kept_spans: list[dict[str, int | str]] = []
    for chain in record.chains:
        if int(chain.mol_type) != PROTEIN_CHAIN_TYPE:
            continue
        chain_name = str(chain.chain_name)
        num_res = int(chain.num_residues)
        blocked = blocked_by_chain.get(chain_name, set())
        span_start: int | None = None
        for pos in range(1, num_res + 1):
            is_free = pos not in blocked
            if is_free and span_start is None:
                span_start = pos
            elif not is_free and span_start is not None:
                if pos - span_start >= 1:
                    template_records.append(
                        TemplateInfo(
                            name="self",
                            query_chain=chain_name,
                            query_st=span_start,
                            query_en=pos - 1,
                            template_chain=chain_name,
                            template_st=span_start,
                            template_en=pos - 1,
                            force=True,
                            threshold=float(template_threshold),
                        )
                    )
                    kept_spans.append(
                        {
                            "chain_name": chain_name,
                            "start": int(span_start),
                            "end": int(pos - 1),
                        }
                    )
                span_start = None
        if span_start is not None and num_res - span_start + 1 >= 1:
            template_records.append(
                TemplateInfo(
                    name="self",
                    query_chain=chain_name,
                    query_st=span_start,
                    query_en=num_res,
                    template_chain=chain_name,
                    template_st=span_start,
                    template_en=num_res,
                    force=True,
                    threshold=float(template_threshold),
                )
            )
            kept_spans.append(
                {
                    "chain_name": chain_name,
                    "start": int(span_start),
                    "end": int(num_res),
                }
            )

    updated_record = replace(record, templates=template_records or None)
    manifest.records[record_idx] = updated_record
    manifest.dump(manifest_path)
    return {
        "record_id": record_id,
        "template_threshold": float(template_threshold),
        "pocket_margin": int(pocket_margin),
        "template_span_count": len(kept_spans),
        "template_spans": kept_spans,
    }
