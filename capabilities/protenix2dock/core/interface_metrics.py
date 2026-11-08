"""Interface-scoped metrics: per-sample ipSAE/iptm aggregation and the
heavy-atom interface geometry gate used for best-sample selection."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np

from core.ipsae import compute_ipsae_for_output

log = logging.getLogger("protenix2dock")


def cif_interface_geometry(cif_path, ligand_chain: str):
    """Heavy-atom interface geometry of one predicted sample.

    Returns (min_dist, clash_pairs) between the binder chain and every
    other polymer chain, or None when the CIF is missing/unreadable. This
    is the geometric gate the best-sample selection was missing: a sample
    with a buried main-chain interpenetration can top the ipsae chart precisely BECAUSE the interface is
    over-buried — score alone selects for it.
    """
    try:
        import gemmi
        st = gemmi.read_structure(str(cif_path))
        st.setup_entities()
        st.remove_hydrogens()
    except Exception:
        return None
    rec_rows: list[list[float]] = []
    binder_rows: list[list[float]] = []
    for ch in st[0]:
        target = binder_rows if ch.name == ligand_chain else rec_rows
        for r in ch:
            if r.het_flag == 'H':
                continue
            for a in r:
                if a.element == gemmi.Element('H'):
                    continue
                target.append([a.pos.x, a.pos.y, a.pos.z])
    if not rec_rows or not binder_rows:
        return None
    rec = np.asarray(rec_rows)
    binder = np.asarray(binder_rows)
    d = np.sqrt(((rec[:, None] - binder[None]) ** 2).sum(-1))
    return float(d.min()), int((d < 2.2).sum())


import os


def interface_weights() -> tuple[float, float, float]:
    """(ipsae_dom, iptm, ligand_ipsae_max) ranking weights.

    Defaults favor ipsae_dom; override via P2D_INTERFACE_WEIGHTS.
    """
    raw = os.environ.get("P2D_INTERFACE_WEIGHTS", "").strip()
    if raw:
        parts = tuple(float(x) for x in raw.split(","))
        if len(parts) == 3:
            return parts  # type: ignore[return-value]
        log.warning("ignoring malformed P2D_INTERFACE_WEIGHTS=%r", raw)
    return (0.5, 0.3, 0.2)



def add_interface_metrics(summary: dict, output_dir: Path, ligand_chain="B",
                          interface: str | None = None):
    """ipSAE + interface-scoped iptm per sample plus the best-by-interface
    entry (mutates in place).

    `interface` is two comma-separated groups of chain letters ("A,B" or
    "AB,C"): pair_iptm is the WEAKEST cross-group pair from the engine's
    chain-pair matrix — the all-pairs global averages in linkers and
    receptor-receptor contacts, which misreports the interface the caller
    cares about. When unset it falls back to the global iptm.
    """
    by_sample = compute_ipsae_for_output(output_dir, ligand_chain_id=ligand_chain)

    def _pair_iptm_of(conf_file: str):
        if not interface or not conf_file:
            return None
        try:
            payload = json.loads(Path(conf_file).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        matrix = payload.get("chain_pair_iptm")
        if not matrix:
            return None
        try:
            first, second = (g.strip() for g in interface.split(","))
        except ValueError:
            return None
        idx = {ch: i for i, ch in enumerate(
            chr(ord("A") + k) for k in range(len(matrix)))}
        values = [float(matrix[idx[a]][idx[b]])
                  for a in first if a in idx for b in second if b in idx]
        return min(values) if values else None

    for entry in summary.get("confidences", []):
        ips = by_sample.get(entry.get("sample"))
        if ips:
            entry["ligand_ipsae_max"] = ips.get("ligand_ipsae_max")
            entry["ipsae_dom"] = ips.get("ipsae_dom")
            entry["ligand_plddt"] = ips.get("ligand_plddt_mean")
            entry["interface_pair_count"] = ips.get("interface_pair_count")
        pair = _pair_iptm_of(entry.get("file") or "")
        if pair is not None:
            entry["pair_iptm"] = round(pair, 4)
        # Per-sample interface geometry for the best-sample gate below.
        conf_file = entry.get("file") or ""
        if conf_file:
            sample_idx = entry.get("sample")
            pred_dir = Path(conf_file).parent
            cif_hits = sorted(pred_dir.glob(f"*sample_{sample_idx}.cif"))
            if cif_hits:
                geom = cif_interface_geometry(cif_hits[0], ligand_chain)
                if geom is not None:
                    entry["interface_min_dist"] = round(geom[0], 3)
                    entry["interface_clash_pairs"] = geom[1]
    if not by_sample:
        return
    w_dom, w_iptm, w_max = interface_weights()
    def score(entry):
        return (w_dom * float(entry.get("ipsae_dom") or 0.0)
                + w_iptm * float(entry.get("iptm") or 0.0)
                + w_max * float(entry.get("ligand_ipsae_max") or 0.0))
    # Geometric gate on the shipped best: among samples with a physically
    # clean interface (no heavy-atom pair under 2.2 A), ship the
    # highest-scoring one; when none is clean the score winner ships but
    # gets geometry_flagged so downstream consumers can demand a re-run.
    clean = [e for e in summary["confidences"]
             if e.get("interface_clash_pairs") == 0
             and float(e.get("interface_min_dist") or 0.0) >= 2.3]
    pool = clean if clean else summary["confidences"]
    best = max(pool, key=score)
    best["interface_score"] = round(score(best), 4)
    if not clean:
        best["geometry_flagged"] = True
        log.warning(
            "best sample %s ships with interface clashes (min %.2f A, %d "
            "pairs under 2.2 A) — no clean sample in this run",
            best.get("sample"), float(best.get("interface_min_dist") or 0.0),
            int(best.get("interface_clash_pairs") or 0))
    summary["n_geometrically_clean"] = len(clean)
    summary["best_by_interface"] = best



