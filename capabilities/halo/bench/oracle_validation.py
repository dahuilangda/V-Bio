"""Oracle validation: Boltz2Score score-mode vs experimental activity per target.

Protocol matches the repository's mode benchmark: score mode on the crystal
poses (confidence + affinity + ipSAE), then Spearman/Pearson correlation of
affinity_pic50 (and affinity_pic50_mw, ipSAE) with experimental pIC50.
Targets already covered by the repo (cdk8, cmet, ...) are read from their
mode_benchmark_compare outputs; cdk2 is scored fresh here.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import pearsonr, spearmanr

from halo import REPO_ROOT
from halo.data.ligands import load_ligand_table
from halo.data.targets import get_target


def run_score_cli(target, out_dir: Path, gpu: int) -> Path:
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, str(REPO_ROOT / "capabilities" / "boltz2score" / "boltz2score.py"), "--mode", "score",
        "--protein_file", str(target.protein_pdb),
        "--ligand_file", str(target.ligands_sdf),
        "--output_dir", str(out_dir),
        "--compute_ipsae", "--enable_affinity",
        "--target_chain", target.target_chain, "--ligand_chain", target.ligand_chain,
        "--recycling_steps", "1", "--affinity_recycling_steps", "1",
        "--trainer_precision", "bf16-mixed", "--accelerator", "gpu", "--devices", "1",
        "--seed", "42",
    ]
    import os

    env = dict(os.environ) | {"CUDA_VISIBLE_DEVICES": str(gpu)}
    r = subprocess.run(cmd, env=env, cwd=str(REPO_ROOT / "capabilities" / "boltz2score"), timeout=7200,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    (out_dir / "cli.log").write_text(r.stdout[-4000:])
    return out_dir


def parse_score_dir(score_dir: Path, protein_stem: str) -> pd.DataFrame:
    rows = []
    for d in sorted(score_dir.iterdir()):
        m = re.search(r"__(\d{4})_", d.name)
        if not (d.is_dir() and m):
            continue
        idx = int(m.group(1))
        row = {"ligand_index": idx - 1}
        conf_f = d / "best_confidence.json"
        if conf_f.exists():
            c = json.loads(conf_f.read_text())
            row.update({"ipsae": c.get("ipsae_dom"), "ligand_plddt_mean": c.get("ligand_plddt_mean"),
                        "confidence_score": c.get("confidence_score"), "iptm": c.get("iptm")})
        for f in d.glob("affinity_*.json"):
            a = json.loads(f.read_text())
            row.update({"affinity_pic50": a.get("affinity_pic50"),
                        "affinity_pic50_mw": a.get("affinity_pic50_mw"),
                        "affinity_probability_binary": a.get("affinity_probability_binary")})
            break
        rows.append(row)
    return pd.DataFrame(rows)


def correlate(df: pd.DataFrame, x: str, y: str = "activity_pic50") -> dict | None:
    sub = df.dropna(subset=[x, y])
    if len(sub) < 5:
        return None
    return {
        "n": len(sub),
        "pearson": float(pearsonr(sub[x], sub[y])[0]),
        "spearman": float(spearmanr(sub[x], sub[y])[0]),
    }


def run_validation(target_names: list[str], gpus=(1, 2, 3), out_dir=Path("runs/oracle_validation"),
                   rounds: int = 1, limit: int | None = None) -> pd.DataFrame:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    with ThreadPoolExecutor(max_workers=min(len(target_names), len(gpus))) as pool:
        futs = {}
        for gi, name in enumerate(target_names):
            target = get_target(name)
            tdir = out_dir / name
            done_marker = tdir / "done"
            if done_marker.exists():
                continue
            futs[pool.submit(run_score_cli, target, tdir / "score", gpus[gi % len(gpus)])] = name
        for f in futs:
            f.result()
    # parse + correlate
    for name in target_names:
        target = get_target(name)
        tdir = out_dir / name
        score_dir = tdir / "score"
        if not score_dir.exists():
            continue
        table = load_ligand_table(target.ligands_sdf)
        if limit:
            table = table.head(limit)
        pred = parse_score_dir(score_dir, target.protein_pdb.stem)
        merged = table.merge(pred, left_on="index", right_on="ligand_index", how="inner")
        merged.to_csv(tdir / "merged.csv", index=False)
        for metric in ("affinity_pic50", "affinity_pic50_mw", "ipsae", "ligand_plddt_mean"):
            c = correlate(merged, metric)
            if c:
                results.append({"target": name, "metric": metric, **c})
        (tdir / "done").write_text("ok")
    df = pd.DataFrame(results)
    if len(df):
        df.to_csv(out_dir / "validation_correlations.csv", index=False)
        print(df.to_string(index=False))
    return df


def collect_repo_validations() -> pd.DataFrame:
    """Reuse the repo's existing mode-benchmark correlations (score mode)."""
    rows = []
    for f in sorted(REPO_ROOT.glob("data/*/mode_benchmark_compare/benchmark_mode_compare_correlations.csv")):
        target = f.parts[-3]
        d = pd.read_csv(f)
        for metric in ("score__affinity_pic50", "score__affinity_pic50_mw", "score__ipsae_dom"):
            sub = d[d["metric"] == metric]
            if len(sub):
                rows.append({"target": target, "metric": metric.replace("score__", ""),
                             "n": int(sub["n"].iloc[0]), "pearson": float(sub["pearson"].iloc[0]),
                             "spearman": float(sub["spearman"].iloc[0])})
    return pd.DataFrame(rows)
