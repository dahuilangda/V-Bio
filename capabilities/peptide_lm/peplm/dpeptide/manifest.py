"""Manifest-level record surgery for the fixed-receptor docking protocol.

CRITICAL (validated the hard way): the inference data module reads the
MANIFEST, not records/<id>.json — template edits must go through
Manifest.load/Manifest.dump. Records are frozen dataclasses; mutations go
through dataclasses.replace and are written back into manifest.records.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable, Sequence


def update_manifest_record(processed_dir: Path, record_id: str, mutate: Callable) -> dict:
    """Apply ``mutate(record) -> (new_record, info)`` on the manifest record."""
    from boltz.data.types import Manifest

    manifest_path = processed_dir / "manifest.json"
    manifest = Manifest.load(manifest_path)
    idx = next((i for i, r in enumerate(manifest.records) if r.id == record_id), None)
    if idx is None:
        raise RuntimeError(f"Record {record_id!r} not found in {manifest_path}")
    new_record, info = mutate(manifest.records[idx])
    manifest.records[idx] = new_record
    manifest.dump(manifest_path)
    return info or {}


def _strip_suffix(name: str) -> str:
    return name[:-1] if len(name) > 1 and name[-1].isdigit() else name


def filter_templates_to_receptor(
    processed_dir: Path, record_id: str, receptor_chains: Sequence[str]
) -> int:
    """Drop template rows whose query chain is not a receptor (manifest edit).

    The peptide must NOT be pinned to its input pose by a self-template while
    the receptor template keeps the D-target fold hinted to the trunk.
    """
    receptor_set = {c.strip() for c in receptor_chains}

    def mutate(record):
        templates = list(record.templates or [])
        kept = [t for t in templates if _strip_suffix(str(t.query_chain or "")) in receptor_set]
        return replace(record, templates=kept or None), {"dropped": len(templates) - len(kept)}

    return update_manifest_record(processed_dir, record_id, mutate)["dropped"]


def configure_peptide_pocket_constraints(
    processed_dir: Path,
    record_id: str,
    ligand_chain_letter: str,
    target_chain_letters: Sequence[str],
    contact_cutoff: float = 8.0,
    max_distance: float = 6.0,
    max_residues: int = 30,
) -> dict:
    """Pocket conditioning for a POLYMER peptide binder.

    Mirrors the production configure_anchored_refine_constraints but selects
    the ligand chain by name — the production version only accepts
    NONPOLYMER chains, which excludes peptides.
    """
    import numpy as np
    from boltz.data import const
    from boltz.data.types import InferenceOptions, StructureV2

    structure = StructureV2.load(processed_dir / "structures" / f"{record_id}.npz").remove_invalid_chains()

    def _coords(start, end):
        return [tuple(map(float, structure.atoms[i]["coords"])) for i in range(start, end)]

    lig_chain = None
    for chain in structure.chains:
        if _strip_suffix(str(chain["name"] or "").strip()) == ligand_chain_letter.strip():
            lig_chain = chain
            break
    if lig_chain is None:
        raise RuntimeError(
            f"peptide chain {ligand_chain_letter!r} not found; chains: "
            + ", ".join(str(c["name"]) for c in structure.chains)
        )
    lig_asym = int(lig_chain["asym_id"])
    lig_start = int(lig_chain["atom_idx"])
    lig_arr = np.asarray(_coords(lig_start, lig_start + int(lig_chain["atom_num"])))

    targets = {c.strip() for c in target_chain_letters}
    rows = []
    for chain in structure.chains:
        name = str(chain["name"] or "").strip()
        if chain is lig_chain or _strip_suffix(name) == ligand_chain_letter.strip():
            continue
        if int(chain["mol_type"]) == const.chain_type_ids["NONPOLYMER"]:
            continue
        if targets and _strip_suffix(name) not in targets:
            continue
        asym_id = int(chain["asym_id"])
        res_start = int(chain["res_idx"])
        res_end = res_start + int(chain["res_num"])
        for residue in structure.residues[res_start:res_end]:
            atom_start = int(residue["atom_idx"])
            atom_end = atom_start + int(residue["atom_num"])
            arr = np.asarray(_coords(atom_start, atom_end))
            if arr.size == 0:
                continue
            d = float(np.linalg.norm(arr[:, None, :] - lig_arr[None, :, :], axis=2).min())
            if d <= contact_cutoff:
                rows.append((d, asym_id, int(residue["res_idx"]), str(residue["name"])))
    rows.sort(key=lambda r: r[0])
    if max_residues > 0:
        rows = rows[:max_residues]
    if not rows:
        raise RuntimeError("no pocket residues found near the placed peptide")
    contacts = [(asym, idx) for _, asym, idx, _ in rows]

    def mutate(record):
        options = InferenceOptions(
            pocket_constraints=[(lig_asym, contacts, float(max_distance), True)],
            contact_constraints=None,
        )
        return replace(record, inference_options=options), None

    update_manifest_record(processed_dir, record_id, mutate)
    return {
        "ligand_asym_id": lig_asym,
        "pocket_residue_count": len(contacts),
        "max_distance": max_distance,
    }
