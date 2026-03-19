from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Sequence

import gemmi
import numpy as np
from rdkit import Chem

from utils.ligand_utils import extract_atom_preferred_name

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


def is_hydrogen_like(element_or_name: str) -> bool:
    token = str(element_or_name or "").strip().upper()
    token = re.sub(r"^[0-9]+", "", token)
    return token in {"H", "D", "T"} or token.startswith(("H", "D", "T"))


def normalize_name_key(name: str) -> str:
    return "".join(ch for ch in str(name or "").strip().upper() if ch.isalnum())


def extract_ligand_bfactors_by_chain(structure_path: Path) -> dict[str, dict[str, float]]:
    structure = gemmi.read_structure(str(structure_path))
    structure.setup_entities()
    entity_types = {
        sub: ent.entity_type.name
        for ent in structure.entities
        for sub in ent.subchains
    }

    by_chain: dict[str, dict[str, float]] = {}
    if len(structure) == 0:
        return by_chain

    model = structure[0]
    for chain in model:
        chain_name = str(chain.name).strip()
        if not chain_name:
            continue
        for residue in chain:
            if entity_types.get(residue.subchain) not in {"NonPolymer", "Branched"}:
                continue
            resname = str(residue.name or "").strip().upper()
            if not resname or resname in WATER_RESNAMES or resname in ION_RESNAMES:
                continue

            values: dict[str, float] = {}
            for atom in residue:
                element = str(atom.element.name or atom.name[:1]).strip()
                atom_name = str(atom.name or "").strip()
                if is_hydrogen_like(element or atom_name):
                    continue
                key = normalize_name_key(atom_name)
                if not key:
                    continue
                if key in values:
                    raise RuntimeError(
                        f"Duplicate ligand atom name in structure chain {chain_name}: {atom_name}"
                    )
                values[key] = float(atom.b_iso)

            if values:
                by_chain[chain_name] = values
                break

    return by_chain


def load_raw_ligand_plddt_entries(raw_json_path: Path) -> dict[str, list[dict[str, object]]]:
    if not raw_json_path.exists():
        return {}
    payload = json.loads(raw_json_path.read_text())
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return {}

    by_chain: dict[str, list[dict[str, object]]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        chain = str(entry.get("chain") or "").strip()
        atom_name = str(entry.get("atom_name") or "").strip()
        if not chain or not atom_name:
            continue
        try:
            writer_token_index = int(entry.get("writer_token_index"))
            plddt = float(entry.get("plddt"))
        except Exception:
            continue
        by_chain.setdefault(chain, []).append(
            {
                "atom_name": atom_name,
                "writer_token_index": writer_token_index,
                "plddt": plddt,
            }
        )

    for chain_entries in by_chain.values():
        chain_entries.sort(key=lambda item: int(item["writer_token_index"]))
    return by_chain


def ligand_atom_plddt_stats(plddts: Sequence[float]) -> dict[str, float]:
    values = np.asarray([float(v) for v in plddts], dtype=float)
    if values.size == 0:
        return {}
    return {
        "ligand_plddt_mean": float(values.mean()),
        "ligand_atom_plddt_min": float(values.min()),
        "ligand_atom_plddt_p10": float(np.percentile(values, 10.0)),
        "ligand_atom_plddt_p25": float(np.percentile(values, 25.0)),
        "ligand_atom_plddt_median": float(np.median(values)),
        "ligand_atom_plddt_p75": float(np.percentile(values, 75.0)),
        "ligand_atom_plddt_max": float(values.max()),
        "ligand_atom_plddt_std": float(values.std(ddof=0)),
        "ligand_atom_plddt_fraction_ge_50": float(np.mean(values >= 50.0)),
        "ligand_atom_plddt_fraction_ge_70": float(np.mean(values >= 70.0)),
    }


def resolve_model_ligand_chain_id(
    available_chain_ids: list[str],
    requested_ligand_chain_id: str | None,
) -> str:
    if not available_chain_ids:
        raise RuntimeError("No ligand chain found in output structure.")

    if requested_ligand_chain_id:
        requested = requested_ligand_chain_id.strip()
        if requested:
            requested_upper = requested.upper()
            for chain_id in available_chain_ids:
                if chain_id.upper() == requested_upper:
                    return chain_id
            for chain_id in available_chain_ids:
                chain_upper = chain_id.upper()
                if chain_upper.startswith(f"{requested_upper}X"):
                    return chain_id
                if requested_upper.startswith(f"{chain_upper}X"):
                    return chain_id

    if len(available_chain_ids) == 1:
        return available_chain_ids[0]

    raise RuntimeError(
        "Unable to resolve model ligand chain id uniquely. "
        f"Available ligand chains: {available_chain_ids}. "
        f"Requested chain: {requested_ligand_chain_id!r}."
    )


def resolve_model_ligand_chain_id_from_atom_names(
    by_chain: dict[str, dict[str, float]],
    reference_atom_name_keys: Sequence[str],
    requested_ligand_chain_id: str | None,
) -> str:
    available_chain_ids = sorted(str(chain_id).strip() for chain_id in by_chain if str(chain_id).strip())
    try:
        return resolve_model_ligand_chain_id(available_chain_ids, requested_ligand_chain_id)
    except RuntimeError:
        pass

    reference_keys = {
        normalize_name_key(name)
        for name in reference_atom_name_keys
        if normalize_name_key(name)
    }
    if not reference_keys:
        raise RuntimeError(
            "Unable to resolve model ligand chain id uniquely without reference atom names. "
            f"Available ligand chains: {available_chain_ids}. "
            f"Requested chain: {requested_ligand_chain_id!r}."
        )

    exact_matches: list[str] = []
    containing_matches: list[str] = []
    scored: list[tuple[int, int, int, int, str]] = []
    for chain_id in available_chain_ids:
        model_keys = {
            normalize_name_key(name)
            for name in by_chain.get(chain_id, {})
            if normalize_name_key(name)
        }
        if not model_keys:
            continue
        if model_keys == reference_keys:
            exact_matches.append(chain_id)
        if reference_keys.issubset(model_keys):
            containing_matches.append(chain_id)
        overlap = len(reference_keys & model_keys)
        missing = len(reference_keys - model_keys)
        extra = len(model_keys - reference_keys)
        preferred_chain = int(chain_id.upper().startswith("L"))
        scored.append((overlap, -missing, -extra, preferred_chain, chain_id))

    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(containing_matches) == 1:
        return containing_matches[0]

    if scored:
        scored.sort(reverse=True)
        best = scored[0]
        if len(scored) == 1 or best[:4] != scored[1][:4]:
            return best[4]

    raise RuntimeError(
        "Unable to resolve model ligand chain id uniquely. "
        f"Available ligand chains: {available_chain_ids}. "
        f"Requested chain: {requested_ligand_chain_id!r}."
    )


def build_smiles_order_from_ligand_mol(mol: Chem.Mol) -> tuple[str, list[int], list[str]]:
    heavy = Chem.RemoveHs(Chem.Mol(mol), sanitize=False)
    if heavy.GetNumAtoms() == 0:
        raise RuntimeError("Reference ligand has no heavy atoms.")

    heavy_name_keys: list[str] = []
    seen_name_keys: set[str] = set()
    for atom in heavy.GetAtoms():
        preferred = extract_atom_preferred_name(atom)
        key = normalize_name_key(preferred)
        if not key:
            raise RuntimeError("Reference ligand atom is missing a usable atom name.")
        if key in seen_name_keys:
            raise RuntimeError(f"Duplicate reference ligand atom name: {preferred}")
        seen_name_keys.add(key)
        heavy_name_keys.append(key)

    for idx, atom in enumerate(heavy.GetAtoms(), start=1):
        atom.SetAtomMapNum(idx)
    mapped_smiles = Chem.MolToSmiles(heavy, canonical=False, isomericSmiles=True)
    mapped = Chem.MolFromSmiles(mapped_smiles)
    if mapped is None or mapped.GetNumAtoms() != heavy.GetNumAtoms():
        raise RuntimeError("Failed to build mapped ligand SMILES from reference ligand.")

    smiles_to_heavy: list[int] = []
    seen_indices: set[int] = set()
    for atom in mapped.GetAtoms():
        mapped_idx = atom.GetAtomMapNum() - 1
        if mapped_idx < 0 or mapped_idx >= heavy.GetNumAtoms():
            raise RuntimeError("Invalid atom-map index while building ligand SMILES order.")
        if mapped_idx in seen_indices:
            raise RuntimeError("Duplicate atom-map index while building ligand SMILES order.")
        seen_indices.add(mapped_idx)
        smiles_to_heavy.append(mapped_idx)
        atom.SetAtomMapNum(0)

    if len(smiles_to_heavy) != heavy.GetNumAtoms():
        raise RuntimeError("Incomplete ligand SMILES order mapping.")

    smiles = Chem.MolToSmiles(mapped, canonical=False, isomericSmiles=True)
    if not smiles:
        raise RuntimeError("Failed to build ligand SMILES without atom maps.")

    return smiles, smiles_to_heavy, heavy_name_keys
