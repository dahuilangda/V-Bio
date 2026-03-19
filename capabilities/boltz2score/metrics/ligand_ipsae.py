#!/usr/bin/env python3
"""Ligand-aware IPSAE utilities for Boltz2Score outputs."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from utils.result_utils import confidence_model_stem, select_confidence_file


@dataclass
class Token:
    token_index: int
    chain_id: str
    residue_name: str
    residue_seq_num: str
    atom_name: str
    coord: np.ndarray
    kind: str
    label: str


def ptm_func(x: np.ndarray, d0: float) -> np.ndarray:
    return 1.0 / (1.0 + (x / d0) ** 2.0)


def calc_d0(length: int) -> float:
    length = max(int(length), 1)
    if length > 27:
        return max(1.0, 1.24 * (float(length) - 15.0) ** (1.0 / 3.0) - 1.8)
    return 1.0


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def _resolve_layout_paths(
    result_dir: Path,
    model_index: int | None = None,
) -> tuple[Path, Path, Path, Path | None]:
    result_dir = result_dir.expanduser().resolve()
    if not result_dir.exists():
        raise FileNotFoundError(f"Result directory not found: {result_dir}")

    candidate_dir = result_dir / "combined_complex"
    base_dir = candidate_dir if candidate_dir.is_dir() else result_dir

    conf_files = sorted(base_dir.glob("confidence_*.json"))
    conf_path = select_confidence_file(conf_files, include_best_alias=False, model_index=model_index)
    if conf_path is None:
        raise FileNotFoundError("No confidence JSON files found.")
    model_stem = confidence_model_stem(conf_path)

    cif_path = base_dir / f"{model_stem}.cif"
    if not cif_path.exists():
        mmcif_path = base_dir / f"{model_stem}.mmcif"
        if mmcif_path.exists():
            cif_path = mmcif_path
        else:
            raise FileNotFoundError(
                f"Cannot find mmCIF output for {conf_path.name}. Expected {cif_path.name}."
            )

    pae_path = base_dir / f"pae_{model_stem}.npz"
    if not pae_path.exists():
        raise FileNotFoundError(
            f"Cannot find full PAE output for {conf_path.name}. Expected {pae_path.name}."
        )

    chain_map_path: Path | None = None
    for candidate in (base_dir / "chain_map.json", result_dir / "chain_map.json"):
        if candidate.exists():
            chain_map_path = candidate
            break

    return conf_path, cif_path, pae_path, chain_map_path


def _read_atom_rows(cif_path: Path) -> tuple[list[str], list[list[str]]]:
    fields: list[str] = []
    rows: list[list[str]] = []
    with cif_path.open() as handle:
        for line in handle:
            if line.startswith("_atom_site."):
                fields.append(line.strip().split(".", 1)[1])
                continue
            if fields and (line.startswith("ATOM") or line.startswith("HETATM")):
                rows.append(line.split())
                continue
            if rows:
                break
    if not fields or not rows:
        raise RuntimeError(f"Failed to parse atom rows from {cif_path}")
    return fields, rows


def _build_tokens(cif_path: Path, ligand_chain_id: str) -> tuple[list[Token], list[Token]]:
    fields, rows = _read_atom_rows(cif_path)
    idx = {name: pos for pos, name in enumerate(fields)}
    chain_field = "auth_asym_id" if "auth_asym_id" in idx else "label_asym_id"

    residue_order: list[tuple[str, str, str]] = []
    residue_atoms: dict[tuple[str, str, str], list[dict[str, object]]] = {}
    ligand_tokens: list[Token] = []
    extra_protein_tokens: list[Token] = []

    for parts in rows:
        chain_id = parts[idx[chain_field]]
        residue_name = parts[idx["label_comp_id"]]
        residue_seq_num = parts[idx["label_seq_id"]]
        atom_name = parts[idx["label_atom_id"]]
        coord = np.array(
            [
                float(parts[idx["Cartn_x"]]),
                float(parts[idx["Cartn_y"]]),
                float(parts[idx["Cartn_z"]]),
            ],
            dtype=float,
        )

        if residue_seq_num == ".":
            if chain_id == ligand_chain_id:
                ligand_tokens.append(
                    Token(
                        token_index=-1,
                        chain_id=chain_id,
                        residue_name=residue_name,
                        residue_seq_num=residue_seq_num,
                        atom_name=atom_name,
                        coord=coord,
                        kind="ligand_atom",
                        label=f"{chain_id}:{residue_name}:{atom_name}",
                    )
                )
            elif residue_name != "HOH":
                extra_protein_tokens.append(
                    Token(
                        token_index=-1,
                        chain_id=chain_id,
                        residue_name=residue_name,
                        residue_seq_num=residue_seq_num,
                        atom_name=atom_name,
                        coord=coord,
                        kind="protein_cofactor_atom",
                        label=f"{chain_id}:{residue_name}:{atom_name}",
                    )
                )
            continue

        residue_key = (chain_id, residue_seq_num, residue_name)
        if residue_key not in residue_atoms:
            residue_order.append(residue_key)
            residue_atoms[residue_key] = []
        residue_atoms[residue_key].append(
            {
                "atom_name": atom_name,
                "coord": coord,
            }
        )

    protein_tokens: list[Token] = []
    for chain_id, residue_seq_num, residue_name in residue_order:
        if chain_id == ligand_chain_id:
            continue
        atoms = residue_atoms[(chain_id, residue_seq_num, residue_name)]
        preferred_atom = None
        if residue_name not in {"ACE", "NMA"}:
            for name in ("CB", "CA"):
                preferred_atom = next((atom for atom in atoms if atom["atom_name"] == name), None)
                if preferred_atom is not None:
                    break
        if preferred_atom is None:
            preferred_atom = atoms[0]
        protein_tokens.append(
            Token(
                token_index=-1,
                chain_id=chain_id,
                residue_name=residue_name,
                residue_seq_num=residue_seq_num,
                atom_name=str(preferred_atom["atom_name"]),
                coord=np.asarray(preferred_atom["coord"], dtype=float),
                kind="protein_residue",
                label=f"{chain_id}:{residue_name}:{residue_seq_num}:{preferred_atom['atom_name']}",
            )
        )

    all_tokens = protein_tokens + extra_protein_tokens + ligand_tokens
    for token_index, token in enumerate(all_tokens):
        token.token_index = token_index

    return protein_tokens + extra_protein_tokens, ligand_tokens


def _resolve_ligand_chain_id(confidence: dict) -> str:
    ligand_chain_id = str(confidence.get("model_ligand_chain_id") or "").strip()
    if ligand_chain_id:
        return ligand_chain_id

    requested_chain = str(confidence.get("requested_ligand_chain_id") or "").strip()
    if requested_chain:
        return requested_chain

    coverage = confidence.get("ligand_atom_coverage") or []
    detected = sorted(
        {
            str(item.get("chain") or "").strip()
            for item in coverage
            if isinstance(item, dict) and str(item.get("chain") or "").strip()
        }
    )
    if len(detected) == 1:
        return detected[0]

    raise RuntimeError("Missing ligand chain annotation in confidence JSON.")


def compute_ligand_ipsae_from_files(
    confidence_path: Path,
    cif_path: Path,
    pae_path: Path,
    pae_cutoff: float,
    dist_cutoff: float,
    chain_map_path: Path | None = None,
    result_dir: Path | None = None,
) -> dict[str, object]:
    confidence = _load_json(confidence_path)
    ligand_chain_id = _resolve_ligand_chain_id(confidence)
    pae = np.load(pae_path)["pae"]

    protein_tokens, ligand_tokens = _build_tokens(cif_path, ligand_chain_id)
    if not protein_tokens:
        raise RuntimeError("No protein tokens found for IPSAE.")
    if not ligand_tokens:
        raise RuntimeError("No ligand tokens found for IPSAE.")
    if len(protein_tokens) + len(ligand_tokens) != pae.shape[0]:
        raise RuntimeError(
            "Token count mismatch: "
            f"{len(protein_tokens)} protein + {len(ligand_tokens)} ligand != {pae.shape[0]} PAE tokens"
        )

    protein_idx = np.array([token.token_index for token in protein_tokens], dtype=int)
    ligand_idx = np.array([token.token_index for token in ligand_tokens], dtype=int)

    protein_coords = np.stack([token.coord for token in protein_tokens], axis=0)
    ligand_coords = np.stack([token.coord for token in ligand_tokens], axis=0)
    distances = np.sqrt(((protein_coords[:, None, :] - ligand_coords[None, :, :]) ** 2).sum(axis=2))

    pae_pl = pae[np.ix_(protein_idx, ligand_idx)]
    pae_lp = pae[np.ix_(ligand_idx, protein_idx)]
    valid_pl = (distances <= dist_cutoff) & (pae_pl < pae_cutoff)
    valid_lp = (distances.T <= dist_cutoff) & (pae_lp < pae_cutoff)

    protein_scores = np.zeros(len(protein_tokens), dtype=float)
    protein_counts = np.zeros(len(protein_tokens), dtype=int)
    for i in range(len(protein_tokens)):
        mask = valid_pl[i]
        protein_counts[i] = int(mask.sum())
        if protein_counts[i] == 0:
            continue
        d0 = calc_d0(protein_counts[i])
        protein_scores[i] = float(ptm_func(pae_pl[i, mask], d0).mean())

    ligand_scores = np.zeros(len(ligand_tokens), dtype=float)
    ligand_counts = np.zeros(len(ligand_tokens), dtype=int)
    for j in range(len(ligand_tokens)):
        mask = valid_lp[j]
        ligand_counts[j] = int(mask.sum())
        if ligand_counts[j] == 0:
            continue
        d0 = calc_d0(ligand_counts[j])
        ligand_scores[j] = float(ptm_func(pae_lp[j, mask], d0).mean())

    all_valid = valid_pl
    unique_protein = int(np.count_nonzero(all_valid.any(axis=1)))
    unique_ligand = int(np.count_nonzero(all_valid.any(axis=0)))
    n0dom = unique_protein + unique_ligand
    d0dom = calc_d0(n0dom)
    if np.any(all_valid):
        ipsae_dom = float(ptm_func(pae_pl[all_valid], d0dom).mean())
        mean_interface_pae = float(pae_pl[all_valid].mean())
        mean_interface_dist = float(distances[all_valid].mean())
    else:
        ipsae_dom = 0.0
        mean_interface_pae = math.nan
        mean_interface_dist = math.nan

    best_protein_index = int(np.argmax(protein_scores))
    best_ligand_index = int(np.argmax(ligand_scores))
    protein_to_ligand = float(protein_scores[best_protein_index])
    ligand_to_protein = float(ligand_scores[best_ligand_index])

    pair_chains_iptm = confidence.get("pair_chains_iptm") or {}
    ligand_chain_pos = None
    protein_chain_pos = None
    if chain_map_path is not None and chain_map_path.exists():
        chain_map = _load_json(chain_map_path)
        inverse_map = {str(v): str(k) for k, v in chain_map.items()}
        ligand_chain_pos = inverse_map.get(ligand_chain_id)
        protein_chain_ids = sorted({token.chain_id for token in protein_tokens})
        if protein_chain_ids:
            protein_chain_pos = inverse_map.get(protein_chain_ids[0])

    pair_iptm = math.nan
    if protein_chain_pos is not None and ligand_chain_pos is not None:
        try:
            pair_iptm = float(pair_chains_iptm[str(ligand_chain_pos)][str(protein_chain_pos)])
        except Exception:
            pair_iptm = math.nan

    ligand_plddts = confidence.get("ligand_atom_plddts") or []
    ligand_plddt_mean = float(np.mean(ligand_plddts)) if ligand_plddts else math.nan
    ligand_ipsae_max = max(protein_to_ligand, ligand_to_protein)

    return {
        "result_dir": str((result_dir or confidence_path.parent).resolve()),
        "ligand_chain_id": ligand_chain_id,
        "protein_token_count": len(protein_tokens),
        "ligand_token_count": len(ligand_tokens),
        "pae_cutoff": pae_cutoff,
        "dist_cutoff": dist_cutoff,
        "interface_pair_count": int(all_valid.sum()),
        "interface_protein_token_count": unique_protein,
        "interface_ligand_token_count": unique_ligand,
        "pair_chain_iptm": pair_iptm,
        "ligand_plddt_mean": ligand_plddt_mean,
        "ipsae_dom": ipsae_dom,
        "protein_to_ligand_ipsae": protein_to_ligand,
        "ligand_to_protein_ipsae": ligand_to_protein,
        "ligand_ipsae_max": ligand_ipsae_max,
        "mean_interface_pae": mean_interface_pae,
        "mean_interface_distance": mean_interface_dist,
        "best_protein_token": protein_tokens[best_protein_index].label,
        "best_protein_token_pairs": int(protein_counts[best_protein_index]),
        "best_ligand_token": ligand_tokens[best_ligand_index].label,
        "best_ligand_token_pairs": int(ligand_counts[best_ligand_index]),
    }


def compute_ligand_ipsae(
    result_dir: Path,
    pae_cutoff: float,
    dist_cutoff: float,
    model_index: int | None = None,
) -> dict[str, object]:
    conf_path, cif_path, pae_path, chain_map_path = _resolve_layout_paths(
        result_dir=result_dir,
        model_index=model_index,
    )
    return compute_ligand_ipsae_from_files(
        confidence_path=conf_path,
        cif_path=cif_path,
        pae_path=pae_path,
        pae_cutoff=pae_cutoff,
        dist_cutoff=dist_cutoff,
        chain_map_path=chain_map_path,
        result_dir=result_dir,
    )
