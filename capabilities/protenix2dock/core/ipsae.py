"""IPSAE computation for protenix2dock outputs.

Reuses boltz2score's ligand-aware IPSAE implementation
(capabilities/boltz2score/metrics/ligand_ipsae.py) — a pure numpy module whose
CIF token builder already handles Protenix tokenisation (HETATM records map
1:1 to PAE tokens; per-atom expansion covers modified residues). Protenix's
``*_full_data_sample_N.json`` carries the token-level PAE matrix
(``token_pair_pae``), per-atom pLDDT and the atom→token map, which is
everything IPSAE needs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _ensure_b2s_importable() -> None:
    b2s_root = Path(__file__).resolve().parents[2] / "boltz2score"
    if not b2s_root.exists():
        raise FileNotFoundError(
            f"boltz2score capability not found at {b2s_root}; IPSAE reuse requires it."
        )
    for entry in (str(b2s_root),):
        if entry not in sys.path:
            sys.path.insert(0, entry)


def _sample_files(pred_dir: Path, sample: int) -> tuple[Path, Path] | None:
    full = next(pred_dir.glob(f"*full_data_sample_{sample}.json"), None)
    cif = next(pred_dir.glob(f"*_sample_{sample}.cif"), None)
    if full is None or cif is None:
        return None
    return full, cif


def compute_ipsae_for_output(
    output_dir: Path,
    ligand_chain_id: str = "B",
    pae_cutoff: float = 12.0,
    dist_cutoff: float = 5.0,
) -> dict[int, dict[str, Any]]:
    """Compute IPSAE for every sample under <output_dir>/**/predictions/.

    Returns {sample_index: ipsae_fields}. Writes ipsae_<name>_sample_<i>.json
    next to the confidence files, mirroring boltz2score's layout.
    """
    import numpy as np

    _ensure_b2s_importable()
    from metrics.ligand_ipsae import compute_ligand_ipsae_from_files  # noqa: E402  (b2s module)

    results: dict[int, dict[str, Any]] = {}
    for pred_dir in sorted(output_dir.glob("**/predictions")):
        for full_path in sorted(pred_dir.glob("*full_data_sample_*.json")):
            sample = int(full_path.stem.rsplit("_sample_", 1)[1])
            pair = _sample_files(pred_dir, sample)
            if pair is None:
                continue
            _, cif_path = pair
            full = json.loads(full_path.read_text())
            pae = np.asarray(full["token_pair_pae"], dtype=np.float32)
            if pae.ndim != 2 or pae.shape[0] != pae.shape[1]:
                continue

            # Ligand-atom pLDDTs: ligand tokens are those whose asym differs
            # from the most frequent (protein) asym.
            token_asym = np.asarray(full.get("token_asym_id") or [], dtype=int)
            atom_to_token = np.asarray(full.get("atom_to_token_idx") or [], dtype=int)
            atom_plddt = np.asarray(full.get("atom_plddt") or [], dtype=float)
            ligand_plddts: list[float] = []
            if token_asym.size and atom_to_token.size == atom_plddt.size:
                protein_asym = np.bincount(token_asym).argmax()
                # atom_to_token_idx maps each atom to its token; ligand atoms
                # are those whose token belongs to a non-protein asym chain.
                lig_mask = token_asym[atom_to_token] != protein_asym
                ligand_plddts = atom_plddt[lig_mask].tolist()

            pae_npz = pred_dir / f"_p2d_pae_sample_{sample}.npz"
            np.savez(pae_npz, pae=pae)
            conf_stub = {
                "model_ligand_chain_id": ligand_chain_id,
                "ligand_atom_plddts": ligand_plddts,
            }
            conf_path = pred_dir / f"_p2d_conf_sample_{sample}.json"
            conf_path.write_text(json.dumps(conf_stub))

            ipsae = compute_ligand_ipsae_from_files(
                confidence_path=conf_path,
                cif_path=cif_path,
                pae_path=pae_npz,
                pae_cutoff=pae_cutoff,
                dist_cutoff=dist_cutoff,
            )
            pae_npz.unlink(missing_ok=True)
            conf_path.unlink(missing_ok=True)

            out_path = pred_dir / full_path.name.replace("_full_data_", "_ipsae_")
            out_path.write_text(json.dumps(ipsae, indent=2))
            # Also merge the display fields into the per-sample summary
            # confidence JSON so downstream consumers (frontend result parser)
            # read ligand_ipsae_max / ipsae_dom alongside iptm/plddt.
            summary_conf = pred_dir / full_path.name.replace("_full_data_", "_summary_confidence_")
            if summary_conf.exists():
                payload = json.loads(summary_conf.read_text())
                payload.update({
                    "ligand_ipsae_max": ipsae.get("ligand_ipsae_max"),
                    "ipsae_dom": ipsae.get("ipsae_dom"),
                    "ligand_to_protein_ipsae": ipsae.get("ligand_to_protein_ipsae"),
                    "protein_to_ligand_ipsae": ipsae.get("protein_to_ligand_ipsae"),
                    "interface_pair_count": ipsae.get("interface_pair_count"),
                })
                summary_conf.write_text(json.dumps(payload, indent=2))
            results[sample] = ipsae
    if results:
        n = len(results)
        best = max(results.values(), key=lambda r: r.get("ligand_ipsae_max") or 0.0)
        print(
            f"[Info] IPSAE computed for {n} sample(s); best ligand_ipsae_max="
            f"{best.get('ligand_ipsae_max'):.4f}."
        )
    return results
