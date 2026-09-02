#!/usr/bin/env python3
"""Independent evaluation: native affinity head(s) on p2d poses vs baselines.

Runs protenix2dock score mode over the cdk8-33 ligand set (p2d dock poses),
with PROTENIX_AFFINITY_CKPT pointing at one checkpoint or a comma-separated
ensemble, then reports Spearman vs pIC50 next to the recorded baselines.

Usage (inside the protenix runtime image):
    python eval_affinity.py --pose_root /data/affinity_training/eval_poses \
        --ckpt "/path/a.pt,/path/b.pt" --out /data/affinity_training/eval_out
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
from pathlib import Path


def run_one(pose_cif: Path, out_dir: Path, msa_url: str, seed: int) -> dict | None:
    cmd = [
        "/usr/local/micromamba/envs/protenix/bin/python",
        "/workspace/vbio/capabilities/protenix2dock/protenix2dock.py",
        "--mode", "score",
        "--input", str(pose_cif),
        "--output_dir", str(out_dir),
        "--msa_cache_dir", "/data/msa_cache",
        "--msa_server_url", msa_url,
        "--seed", str(seed),
        "--low_vram",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
    if proc.returncode != 0:
        return None
    conf = next(out_dir.glob("**/*_summary_confidence_sample_0.json"), None)
    if conf is None:
        return None
    return json.loads(conf.read_text())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pose_root", required=True,
                    help="dir of lig_<i>/pose.cif evaluation poses")
    ap.add_argument("--ckpt", required=True,
                    help="single .pt or comma-separated ensemble")
    ap.add_argument("--out", required=True)
    ap.add_argument("--ligands_tsv", default="/tmp/p2d_test/bench_ligands.tsv")
    ap.add_argument("--msa_server_url", default="http://172.17.3.200:8080")
    args = ap.parse_args()

    os.environ["PROTENIX_AFFINITY_CKPT"] = args.ckpt
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    rows = []
    for line in Path(args.ligands_tsv).read_text().splitlines():
        if line.strip():
            p = line.split("\t")
            rows.append((int(p[0]), float(p[2])))

    preds, labels = [], []
    for idx, ic50 in rows:
        pose = Path(args.pose_root) / f"lig_{idx}" / "pose.cif"
        if not pose.exists():
            continue
        payload = run_one(pose, out_root / f"lig_{idx}", args.msa_server_url, 42)
        if payload and payload.get("affinity_pred_value") is not None:
            preds.append(payload["affinity_pred_value"])
            labels.append(-math.log10(ic50))
            print(f"lig_{idx}: value={payload['affinity_pred_value']:.3f} "
                  f"(pIC50={labels[-1]:.2f})", flush=True)

    if len(preds) >= 8:
        from scipy.stats import spearmanr

        rho, p = spearmanr(preds, labels)
        print(f"\nn={len(preds)} spearman={rho:+.3f} (p={p:.1e})")
        print("baselines: boltz2-bridge(p2d pose)=+0.404 | boltz2 native=+0.700 | nesso=+0.754")
        (out_root / "eval_summary.json").write_text(json.dumps({
            "n": len(preds), "spearman": rho, "p_value": p,
            "ckpt": args.ckpt,
        }, indent=2))


if __name__ == "__main__":
    main()
