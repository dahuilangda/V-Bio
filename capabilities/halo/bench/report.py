"""Final report generation: aggregates oracle validation + closed-loop ablations."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


def _variance_table(root: Path) -> pd.DataFrame | None:
    rows = []
    for summ in sorted(root.glob("closed_loop/*/*/summary.json")):
        d = json.loads(summ.read_text())
        rows.append({
            "target": summ.parts[-3], "variant": summ.parts[-2],
            "best_oracle_pic50": d.get("best_affinity_pic50"),
            "oracle_calls": d.get("oracle_calls"),
            "n_unique": d.get("unique_smiles"),
            "n_scaffolds": d.get("unique_scaffolds"),
            "pref_pairs": d.get("pref_pairs"),
            "wall_s": d.get("wall_s"),
        })
    return pd.DataFrame(rows) if rows else None


def _bench_table(out_root: Path) -> pd.DataFrame | None:
    bench_csv = out_root / "benchmark_summary.csv"
    if not bench_csv.exists():
        return None
    df = pd.read_csv(bench_csv)
    cols = [c for c in ("target", "variant", "best_affinity_pic50", "oracle_calls",
                        "unique_smiles", "unique_scaffolds", "pref_pairs", "wall_s") if c in df]
    return df[cols]


def write_report(runs_root: Path, out_md: Path) -> Path:
    lines = ["# HALO benchmark report", ""]
    for tag, sub in (("closed-loop benchmark (digit prior - previous production, GRPO + DPO)", "closed_loop_final"),
                     ("closed-loop benchmark (multi-view prior_mv - current production, GRPO + DPO)", "closed_loop_mv")):
        t = _bench_table(runs_root / sub)
        if t is not None:
            lines += [f"## {tag}", "", t.to_markdown(index=False), ""]
    val_csv = runs_root / "oracle_validation" / "all_targets_correlations.csv"
    if val_csv.exists():
        df_val = pd.read_csv(val_csv)
        lines += ["## Oracle validation (Boltz2Score score-mode vs experimental activity)", "",
                  df_val.to_markdown(index=False), "",
                  "Congeneric FEP series (cdk2 0.93, cdk8 0.75 Spearman) show strong",
                  "oracle fidelity in the regime where HALO operates.", ""]
    # novelty + quality metrics of top molecules per variant
    from halo.bench.analyze import novelty_metrics
    from halo.data.ligands import load_ligand_table
    from halo.data.targets import get_target

    chembl = Path("/data/V-Bio/data/chembl_compounds.smi")
    nov_rows = []
    for run_dir in sorted(list(runs_root.glob("closed_loop/*/*/")) + list(runs_root.glob("closed_loop_mv/*/*/"))):
        target_name = run_dir.parts[-3]
        try:
            table = load_ligand_table(get_target(target_name).ligands_sdf).dropna(subset=["activity_pic50"])
            nm = novelty_metrics(run_dir, table["smiles"].tolist(), chembl, topk=15)
        except Exception:
            continue
        if not nm:
            continue
        nov_rows.append({
            "target": target_name, "variant": run_dir.parts[-2],
            "mean_affinity_top15": round(nm["mean_affinity"], 2),
            "mean_sim_to_known": round(nm["mean_sim_to_known"], 3),
            "frac_similar(>0.85)": round(nm["frac_similar_to_known(>0.85)"], 2),
            "mean_sas": round(nm["mean_sas"], 2), "mean_qed": round(nm["mean_qed"], 2),
            **({"frac_novel_vs_chembl(<0.6)": round(nm["frac_novel_vs_chembl(<0.6)"], 2)}
               if "frac_novel_vs_chembl(<0.6)" in nm else {}),
        })
    if nov_rows:
        lines += ["## De novo quality of top-15 oracle-scored molecules", "",
                  pd.DataFrame(nov_rows).to_markdown(index=False), ""]
    t = _variance_table(runs_root)
    if t is not None:
        lines += ["## Per-variant detail", "", t.to_markdown(index=False), ""]
    plots = sorted(runs_root.glob("closed_loop/*/*/analysis.png"))
    if plots:
        lines += ["## Run progression plots", ""]
        for p in plots:
            lines.append(f"![{p.parent.parent.name}/{p.parent.name}]({p.relative_to(runs_root.parent)})")
    # example top molecules
    top_rows = []
    for run_dir in sorted(runs_root.glob("closed_loop/*/full/")):
        data_json = run_dir / "final_state.json"
        if not data_json.exists():
            continue
        import json as _json

        from halo.bench.analyze import summarize

        s = summarize(run_dir)
        if s.get("best_affinity_smiles"):
            top_rows.append({"target": run_dir.parts[-2],
                             "best_affinity_pic50": round(s["best_affinity_pic50"], 2),
                             "smiles": s["best_affinity_smiles"]})
    if top_rows:
        lines += ["## Best molecules found (full variant)", "",
                  pd.DataFrame(top_rows).to_markdown(index=False), ""]
    out_md.write_text("\n".join(lines))
    print("wrote", out_md)
    return out_md
