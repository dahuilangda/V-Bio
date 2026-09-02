"""Run analysis + plots for a HALO run directory."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd


def load_run(run_dir: Path) -> dict:
    run_dir = Path(run_dir)
    out = {}
    for name, fname in (("rounds", "rounds.jsonl"), ("feedback", "feedback.jsonl")):
        p = run_dir / fname
        out[name] = [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []
    out["candidates"] = pd.read_csv(run_dir / "candidates.csv") if (run_dir / "candidates.csv").exists() else pd.DataFrame()
    out["oracle"] = pd.read_csv(run_dir / "oracle_scores.csv") if (run_dir / "oracle_scores.csv").exists() else pd.DataFrame()
    out["state"] = json.loads((run_dir / "final_state.json").read_text()) if (run_dir / "final_state.json").exists() else {}
    return out


def summarize(run_dir: Path) -> dict:
    data = load_run(run_dir)
    cand, oracle, rounds = data["candidates"], data["oracle"], data["rounds"]
    summary: dict = {"run_dir": str(run_dir), "n_rounds": len(rounds)}
    if len(oracle):
        oracle = oracle.copy()
        for c in ("affinity_pic50", "ipsae", "ligand_plddt_mean"):
            oracle[c] = pd.to_numeric(oracle[c], errors="coerce")
        summary["oracle_calls"] = int(len(oracle))
        summary["best_affinity_pic50"] = float(oracle["affinity_pic50"].max())
        summary["best_affinity_smiles"] = str(oracle.loc[oracle["affinity_pic50"].idxmax(), "smiles"]) if oracle["affinity_pic50"].notna().any() else None
        summary["best_ipsae"] = float(oracle["ipsae"].max())
        # top-k mean/min: single-point max hides preference/quality trade-offs
        # (a single max can hide preference/quality trade-offs)
        rk = oracle.dropna(subset=["affinity_pic50"])["affinity_pic50"].sort_values(ascending=False)
        for k in (5, 15):
            if len(rk) >= k:
                summary[f"top{k}_mean_pic50"] = float(rk.head(k).mean())
                summary[f"top{k}_min_pic50"] = float(rk.head(k).min())
        # novelty accounting: rank affinity only among molecules that are
        # neither known compounds nor near-copies
        try:
            from halo.score.novelty_index import load_default

            ni = load_default()
            if ni is not None:
                uniq = oracle.dropna(subset=["affinity_pic50"])["smiles"].unique()
                tcs = ni.max_tanimoto_batch(list(uniq))
                known = [bool(ni.is_known(s)) for s in uniq]
                summary["frac_known_copies"] = float(np.mean(known)) if len(known) else 0.0
                novel_mask = ~np.array(known) & (np.array(tcs) < 0.90)
                novel_summary = dict(zip(uniq, novel_mask))
                nov_rows = oracle[oracle["smiles"].map(lambda s: novel_summary.get(s, False))]
                if len(nov_rows):
                    summary["novel_best_affinity_pic50"] = float(nov_rows["affinity_pic50"].max())
                    summary["frac_novel_tc_lt_090"] = float(np.mean(novel_mask))
        except Exception:
            pass
        # progress: best-so-far affinity vs cumulative calls
        oracle_sorted = oracle.dropna(subset=["affinity_pic50"]).reset_index(drop=True)
        if len(oracle_sorted):
            summary["affinity_curve"] = list(np.maximum.accumulate(oracle_sorted["affinity_pic50"]).round(3))
        # fraction of molecules beating the best known ligand
        if "state" in data and "elite" in data.get("state", {}):
            pass
    if len(cand):
        cand = cand.copy()
        cand["final_reward"] = pd.to_numeric(cand["final_reward"], errors="coerce")
        summary["n_candidates"] = int(len(cand))
        summary["unique_smiles"] = int(cand["smiles"].nunique())
        # diversity: unique Murcko scaffolds
        from rdkit import Chem
        from rdkit.Chem.Scaffolds import MurckoScaffold

        scaffs = set()
        for s in cand["smiles"].unique():
            m = Chem.MolFromSmiles(s)
            if m is not None:
                try:
                    scaffs.add(MurckoScaffold.MurckoScaffoldSmiles(mol=m))
                except Exception:
                    pass
        summary["unique_scaffolds"] = len(scaffs)
    if rounds:
        summary["total_oracle_s"] = float(sum(r.get("oracle_s", 0) for r in rounds))
        summary["wall_s"] = float(sum(r.get("elapsed_s", 0) for r in rounds))
    summary["pref_pairs"] = data["state"].get("pref_pairs") or (rounds[-1].get("pref_pairs") if rounds else None)
    return summary


def plot_run(run_dir: Path, out_png: Path | None = None) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    data = load_run(run_dir)
    rounds = data["rounds"]
    if not rounds:
        print("no rounds to plot")
        return
    fig, axes = plt.subplots(2, 2, figsize=(11, 8))
    r = range(1, len(rounds) + 1)

    ax = axes[0][0]
    ax.plot(r, [x.get("top_final_reward", np.nan) for x in rounds], "o-", label="top final reward")
    ax.plot(r, [x.get("mean_final_reward", np.nan) for x in rounds], "s--", label="mean final reward")
    ax.set_xlabel("round"); ax.set_ylabel("reward"); ax.legend(); ax.set_title("Reward progression")

    ax = axes[0][1]
    best = [x.get("best_affinity_oracle") for x in rounds]
    cum = [v if v is not None else np.nan for v in best]
    cum = pd.Series(cum).ffill()
    ax.plot(r, cum, "o-", color="tab:red")
    ax.set_xlabel("round"); ax.set_ylabel("best oracle pIC50"); ax.set_title("Best structure-based affinity")

    ax = axes[1][0]
    ax.plot(r, [x.get("pool", 0) for x in rounds], label="pool size")
    ax.plot(r, [x.get("oracle_calls", 0) for x in rounds], label="oracle calls")
    ax.plot(r, [x.get("surrogate_n", 0) for x in rounds], label="surrogate observations")
    ax.set_xlabel("round"); ax.legend(); ax.set_title("Loop activity")

    ax = axes[1][1]
    pref = [x.get("pref_pairs", 0) for x in rounds]
    ax.plot(r, pref, "d-", color="tab:green")
    ax.set_xlabel("round"); ax.set_ylabel("human preference pairs"); ax.set_title("Human-in-the-loop")

    fig.suptitle(f"HALO run: {Path(run_dir).name}")
    fig.tight_layout()
    out_png = out_png or Path(run_dir) / "analysis.png"
    fig.savefig(out_png, dpi=140)
    print("wrote", out_png)


def novelty_metrics(run_dir: Path, known_smiles: list[str], chembl_path: Path | None = None,
                    topk: int = 20) -> dict:
    """De novo quality metrics of the loop's top oracle-scored molecules."""
    from rdkit import Chem, DataStructs
    from rdkit.Chem import rdFingerprintGenerator

    from halo.score.properties import compute_descriptors

    data = load_run(run_dir)
    oracle = data["oracle"]
    if not len(oracle):
        return {}
    gen = Chem.MolFromSmiles
    fp_gen = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
    known = [gen(s) for s in known_smiles]
    known = [m for m in known if m is not None]
    known_fps = [fp_gen.GetFingerprint(m) for m in known]

    chembl_fps = None
    if chembl_path and Path(chembl_path).exists():
        from halo.data.ligands import load_smiles_corpus

        corpus = load_smiles_corpus(Path(chembl_path), limit=8000)
        mols = [m for m in (gen(s) for s in corpus) if m is not None]
        chembl_fps = [fp_gen.GetFingerprint(m) for m in mols]

    top = oracle.dropna(subset=["affinity_pic50"]).sort_values("affinity_pic50", ascending=False).head(topk)
    rows = []
    for _, r in top.iterrows():
        m = gen(r["smiles"])
        if m is None:
            continue
        fp = fp_gen.GetFingerprint(m)
        sims_known = [DataStructs.TanimotoSimilarity(fp, k) for k in known_fps]
        row = {
            "smiles": r["smiles"], "affinity_pic50": float(r["affinity_pic50"]),
            "ipsae": r.get("ipsae"), "sim_to_known_max": max(sims_known) if sims_known else None,
            **{k: float(v) for k, v in compute_descriptors(m).items() if k in ("sas", "qed", "mw", "clogp")},
        }
        if chembl_fps:
            sims_c = [DataStructs.TanimotoSimilarity(fp, c) for c in chembl_fps]
            row["sim_to_chembl_max"] = max(sims_c) if sims_c else None
        rows.append(row)
    if not rows:
        return {}
    import numpy as np

    out = {
        "topk": pd.DataFrame(rows),
        "frac_similar_to_known(>0.85)": float(np.mean([r["sim_to_known_max"] > 0.85 for r in rows])),
        "mean_sim_to_known": float(np.mean([r["sim_to_known_max"] for r in rows])),
        "mean_sas": float(np.mean([r["sas"] for r in rows])),
        "mean_qed": float(np.mean([r["qed"] for r in rows])),
        "mean_affinity": float(np.mean([r["affinity_pic50"] for r in rows])),
    }
    if "sim_to_chembl_max" in rows[0]:
        out["frac_novel_vs_chembl(<0.6)"] = float(np.mean([r["sim_to_chembl_max"] < 0.6 for r in rows]))
    return out


def analyze_run(run_dir: Path) -> dict:
    summary = summarize(run_dir)
    print(json.dumps(summary, indent=1, default=str))
    try:
        plot_run(run_dir)
    except Exception as e:
        print("plot failed:", e)
    return summary
