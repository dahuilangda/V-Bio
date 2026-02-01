import numpy as np
from typing import Dict, Optional, Set

PROTEIN_RESIDUES: Set[str] = {
    "ALA",
    "ARG",
    "ASN",
    "ASP",
    "CYS",
    "GLN",
    "GLU",
    "GLY",
    "HIS",
    "ILE",
    "LEU",
    "LYS",
    "MET",
    "PHE",
    "PRO",
    "SER",
    "THR",
    "TRP",
    "TYR",
    "VAL",
}

NUCLEIC_ACIDS: Set[str] = {"DA", "DC", "DT", "DG", "A", "C", "U", "G"}


def _calc_d0_array(L_array: np.ndarray, pair_type: str = "protein") -> np.ndarray:
    L = np.maximum(27.0, L_array.astype(float))
    min_value = 2.0 if pair_type == "nucleic_acid" else 1.0
    d0 = 1.24 * np.cbrt(L - 15.0) - 1.8
    return np.maximum(min_value, d0)


def _classify_chain_type(residue_types_subset: np.ndarray) -> str:
    if np.isin(residue_types_subset, list(NUCLEIC_ACIDS)).any():
        return "nucleic_acid"
    return "protein"


def calculate_ipsae(
    pae_matrix: np.ndarray,
    chain_ids: np.ndarray,
    residue_types: Optional[np.ndarray] = None,
    chain_type_map: Optional[Dict[str, str]] = None,
    pae_cutoff: float = 10.0,
) -> Dict[str, float]:
    unique_chains = np.unique(chain_ids)
    scores: Dict[str, float] = {}

    if chain_type_map is None:
        chain_type_map = {}
        for chain in unique_chains:
            if residue_types is None:
                chain_type_map[chain] = "protein"
            else:
                mask = chain_ids == chain
                chain_type_map[chain] = _classify_chain_type(residue_types[mask])

    for chain1 in unique_chains:
        for chain2 in unique_chains:
            if chain1 == chain2:
                continue

            c1_type = chain_type_map.get(chain1, "protein")
            c2_type = chain_type_map.get(chain2, "protein")
            pair_type = (
                "nucleic_acid" if "nucleic_acid" in (c1_type, c2_type) else "protein"
            )

            mask_c1 = chain_ids == chain1
            mask_c2 = chain_ids == chain2
            sub_pae = pae_matrix[np.ix_(mask_c1, mask_c2)]

            if sub_pae.size == 0:
                scores[f"{chain1}_{chain2}"] = 0.0
                continue

            valid_mask = sub_pae < pae_cutoff
            n0res_per_residue = np.sum(valid_mask, axis=1)
            d0_per_residue = _calc_d0_array(n0res_per_residue, pair_type)

            ptm_matrix = 1.0 / (1.0 + (sub_pae / d0_per_residue[:, np.newaxis]) ** 2.0)
            masked_ptm_sum = np.sum(ptm_matrix * valid_mask, axis=1)

            with np.errstate(divide="ignore", invalid="ignore"):
                ipsae_per_residue = masked_ptm_sum / n0res_per_residue

            ipsae_per_residue = np.nan_to_num(ipsae_per_residue, nan=0.0)
            final_score = float(np.max(ipsae_per_residue)) if ipsae_per_residue.size else 0.0
            scores[f"{chain1}_{chain2}"] = final_score

    return scores
