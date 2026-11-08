#!/usr/bin/env python3
"""Plot detailed benchmark comparisons for Boltz2Score modes and baselines."""

from __future__ import annotations

import argparse
import math
import re
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np
import pandas as pd
import seaborn as sns


def _preferred_font_family() -> str:
    available = {font.name for font in font_manager.fontManager.ttflist}
    for candidate in ("Arial", "Liberation Sans", "DejaVu Sans"):
        if candidate in available:
            return candidate
    return "sans-serif"


def set_standard_style() -> None:
    """Set a consistent style and parameters for all plots."""
    font_family = _preferred_font_family()
    plt.rcParams["font.family"] = font_family
    plt.rcParams["font.weight"] = "bold"
    plt.rcParams["font.size"] = 20
    plt.rcParams["axes.titlesize"] = 18
    plt.rcParams["axes.labelsize"] = 18
    plt.rcParams["legend.fontsize"] = 14
    plt.rcParams["xtick.labelsize"] = 18
    plt.rcParams["ytick.labelsize"] = 18

    sns.set(style="white", context="talk")
    sns.set_palette(["#9BB1D4", "#F2A07B"])

    plt.rcParams.update(
        {
            "axes.prop_cycle": plt.cycler(
                color=["#9BB1D4", "#F2A07B", "#8FB396", "#F28E2B"]
            )
        }
    )

    plt.tick_params(axis="y", which="both", left=True, right=False, length=5)
    plt.tick_params(axis="x", which="both", bottom=True, top=False, length=5)
    plt.grid(False)


def format_axes(ax: plt.Axes) -> None:
    """Apply the standard formatting to a matplotlib axis."""
    font_family = _preferred_font_family()
    for spine in ax.spines.values():
        spine.set_edgecolor("black")
        spine.set_linewidth(1.5)

    for label in ax.get_xticklabels() + ax.get_yticklabels():
        label.set_fontweight("bold")
        label.set_fontname(font_family)

    ax.yaxis.set_ticks_position("left")
    ax.xaxis.set_ticks_position("bottom")


METHOD_TICK_LABELS = {
    "score": "Score",
    "pose": "Pose",
    "refine": "Refine",
    "interface": "Interface",
    "FEP 20ns": "FEP 20ns",
    "FEP 5ns": "FEP 5ns",
    "MMGBSA": "MMGBSA",
    "Docking": "Docking",
    "Docking In-place": "Docking\nIn-place",
}


def apply_method_tick_labels(ax: plt.Axes, *, rotation: float = 35.0, fontsize: int = 14) -> None:
    ticks = ax.get_xticklabels()
    labels = [METHOD_TICK_LABELS.get(tick.get_text(), tick.get_text()) for tick in ticks]
    ax.set_xticks(ax.get_xticks(), labels=labels)
    for label in ax.get_xticklabels():
        label.set_fontsize(fontsize)
        label.set_rotation(rotation)
        label.set_ha("right")


MODE_ORDER = ["score", "pose", "refine", "interface"]
MODE_CANDIDATE_SUFFIXES = [
    "affinity_pic50_mw",
    "affinity_pic50",
    "affinity_pred_value_mw",
    "affinity_pred_value",
    "affinity_probability_binary",
]
BASELINE_METHODS = {
    "FEP 20ns": "fep_20ns_score",
    "FEP 5ns": "fep_5ns_score",
    "MMGBSA": "mmgbsa_score",
    "Docking": "docking_glide_score_better",
    "Docking In-place": "docking_inplace_glide_score_better",
}
METHOD_CLASS_COLORS = {"Mode": "#9BB1D4", "Baseline": "#F2A07B"}


def slugify(text: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "_", str(text).strip()).strip("_").lower()
    return value or "item"


def correlation_safe(frame: pd.DataFrame, x_col: str, y_col: str, method: str) -> tuple[float, int]:
    subset = frame[[x_col, y_col]].dropna()
    if len(subset) < 3:
        return math.nan, int(len(subset))
    corr = subset[x_col].corr(subset[y_col], method=method)
    return (float(corr) if corr is not None else math.nan), int(len(subset))


def discover_compare_dirs(input_root: Path) -> list[Path]:
    if input_root.is_file() and input_root.name == "benchmark_mode_compare_merged.csv":
        return [input_root.parent]
    if (input_root / "benchmark_mode_compare_merged.csv").exists():
        return [input_root]
    compare_dirs = sorted(
        path.parent
        for path in input_root.rglob("benchmark_mode_compare_merged.csv")
    )
    return compare_dirs


def load_compare_bundle(compare_dir: Path) -> tuple[str, pd.DataFrame, pd.DataFrame]:
    merged_path = compare_dir / "benchmark_mode_compare_merged.csv"
    corr_path = compare_dir / "benchmark_mode_compare_correlations.csv"
    if not merged_path.exists():
        raise FileNotFoundError(f"Missing merged CSV: {merged_path}")
    if not corr_path.exists():
        raise FileNotFoundError(f"Missing correlations CSV: {corr_path}")

    benchmark_name = compare_dir.parent.name if compare_dir.name == "mode_benchmark_compare" else compare_dir.name
    merged = pd.read_csv(merged_path)
    correlations = pd.read_csv(corr_path)
    return benchmark_name, merged, correlations


def choose_activity_target(merged: pd.DataFrame, requested_target: str | None) -> str:
    if requested_target:
        if requested_target not in merged.columns:
            raise ValueError(f"Requested target column not found: {requested_target}")
        return requested_target
    for candidate in ("activity_pic50", "activity_score_from_dg"):
        if candidate in merged.columns:
            return candidate
    raise ValueError("No supported activity target found in merged table.")


def prettify_metric(metric_name: str) -> str:
    base = str(metric_name).split("__", 1)[-1]
    explicit = {
        "affinity_pic50_mw": "Affinity pIC50 MW",
        "affinity_pic50": "Affinity pIC50",
        "affinity_pred_value_mw": "Affinity log10(IC50 uM) MW",
        "affinity_pred_value": "Affinity log10(IC50 uM)",
        "affinity_probability_binary": "Affinity Probability",
    }
    if base in explicit:
        return explicit[base]
    return base.replace("_", " ").title()


def select_mode_metric(
    correlations: pd.DataFrame,
    mode_name: str,
    target_column: str,
) -> str | None:
    mode_prefix = f"{mode_name}__"
    subset = correlations[
        correlations["metric"].astype(str).str.startswith(mode_prefix)
        & (correlations["target"].astype(str) == target_column)
    ].copy()
    if subset.empty:
        return None

    subset["suffix"] = subset["metric"].astype(str).str.removeprefix(mode_prefix)
    subset["suffix_rank"] = subset["suffix"].apply(
        lambda value: MODE_CANDIDATE_SUFFIXES.index(value)
        if value in MODE_CANDIDATE_SUFFIXES
        else len(MODE_CANDIDATE_SUFFIXES)
    )
    subset["abs_spearman"] = subset["spearman"].abs()
    subset["abs_pearson"] = subset["pearson"].abs()
    subset = subset.sort_values(
        by=["suffix_rank", "abs_spearman", "abs_pearson", "n"],
        ascending=[True, False, False, False],
    )
    return str(subset.iloc[0]["metric"])


def build_method_summary(
    benchmark_name: str,
    merged: pd.DataFrame,
    correlations: pd.DataFrame,
    target_column: str,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []

    for mode_name in MODE_ORDER:
        metric = select_mode_metric(correlations, mode_name, target_column)
        if metric is None or metric not in merged.columns:
            continue
        spearman, n = correlation_safe(merged, metric, target_column, "spearman")
        pearson, _ = correlation_safe(merged, metric, target_column, "pearson")
        rows.append(
            {
                "benchmark": benchmark_name,
                "method": mode_name,
                "metric": metric,
                "metric_label": prettify_metric(metric),
                "method_class": "Mode",
                "spearman": spearman,
                "pearson": pearson,
                "n": n,
            }
        )

    for method_name, metric in BASELINE_METHODS.items():
        if metric not in merged.columns:
            continue
        spearman, n = correlation_safe(merged, metric, target_column, "spearman")
        pearson, _ = correlation_safe(merged, metric, target_column, "pearson")
        rows.append(
            {
                "benchmark": benchmark_name,
                "method": method_name,
                "metric": metric,
                "metric_label": prettify_metric(metric),
                "method_class": "Baseline",
                "spearman": spearman,
                "pearson": pearson,
                "n": n,
            }
        )

    summary = pd.DataFrame(rows)
    if not summary.empty:
        summary["abs_spearman"] = summary["spearman"].abs()
        summary = summary.sort_values(
            by=["method_class", "abs_spearman", "pearson"],
            ascending=[True, False, False],
        ).reset_index(drop=True)
    return summary


def plot_method_bar(
    summary: pd.DataFrame,
    benchmark_name: str,
    target_label: str,
    output_path: Path,
    value_column: str,
) -> None:
    data = summary.dropna(subset=[value_column]).sort_values(by=value_column, ascending=False)
    if data.empty:
        return

    set_standard_style()
    fig, ax = plt.subplots(figsize=(12, 7), constrained_layout=True)
    sns.barplot(
        data=data,
        x="method",
        y=value_column,
        hue="method_class",
        dodge=False,
        palette=METHOD_CLASS_COLORS,
        ax=ax,
    )
    ax.set_xlabel("")
    ax.set_ylabel(value_column.title())
    apply_method_tick_labels(ax, rotation=35, fontsize=14)
    ax.legend(frameon=False, title="")

    for patch, (_, row) in zip(ax.patches, data.iterrows()):
        height = patch.get_height()
        if pd.isna(height):
            continue
        ax.text(
            patch.get_x() + patch.get_width() / 2.0,
            height + (0.01 if height >= 0 else -0.03),
            f"{height:.2f}",
            ha="center",
            va="bottom" if height >= 0 else "top",
            fontsize=12,
            fontweight="bold",
        )

    format_axes(ax)
    fig.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close(fig)


def plot_scatter_grid(
    merged: pd.DataFrame,
    summary: pd.DataFrame,
    benchmark_name: str,
    target_column: str,
    output_path: Path,
    max_panels: int = 9,
) -> None:
    plot_rows = summary.dropna(subset=["spearman"]).sort_values(by="abs_spearman", ascending=False).head(max_panels)
    if plot_rows.empty:
        return

    n_panels = len(plot_rows)
    n_cols = 3
    n_rows = int(math.ceil(n_panels / n_cols))

    set_standard_style()
    fig, axes = plt.subplots(
        n_rows,
        n_cols,
        figsize=(6.2 * n_cols, 5.8 * n_rows),
        constrained_layout=True,
    )
    axes_flat = np.atleast_1d(axes).ravel()

    for ax, (_, row) in zip(axes_flat, plot_rows.iterrows()):
        metric = str(row["metric"])
        subset = merged[[metric, target_column]].dropna()
        if subset.empty:
            ax.set_visible(False)
            continue

        sns.regplot(
            data=subset,
            x=metric,
            y=target_column,
            scatter_kws={"s": 60, "alpha": 0.85, "color": "#9BB1D4", "edgecolor": "black", "linewidths": 0.5},
            line_kws={"color": "#F28E2B", "linewidth": 2.0},
            ci=None,
            ax=ax,
        )
        method_label = METHOD_TICK_LABELS.get(str(row["method"]), str(row["method"]))
        ax.set_xlabel(f"{method_label}\n{row['metric_label']}")
        ax.set_ylabel(target_column)
        format_axes(ax)

    for ax in axes_flat[n_panels:]:
        ax.set_visible(False)

    fig.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close(fig)


def plot_benchmark_heatmap(
    summary: pd.DataFrame,
    benchmark_name: str,
    output_path: Path,
) -> None:
    if summary.empty:
        return
    heatmap_df = summary.set_index("method")[["spearman", "pearson"]]
    set_standard_style()
    fig, ax = plt.subplots(
        figsize=(6, max(5, 0.6 * len(heatmap_df))),
        constrained_layout=True,
    )
    sns.heatmap(
        heatmap_df,
        annot=True,
        fmt=".2f",
        cmap="RdBu_r",
        center=0.0,
        linewidths=1.0,
        linecolor="white",
        cbar_kws={"shrink": 0.8},
        ax=ax,
    )
    ax.set_xlabel("")
    ax.set_ylabel("")
    format_axes(ax)
    fig.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close(fig)


def plot_cross_benchmark_heatmap(all_summary: pd.DataFrame, output_path: Path) -> None:
    if all_summary.empty:
        return
    heatmap_df = all_summary.pivot(index="benchmark", columns="method", values="spearman")
    if heatmap_df.empty:
        return

    set_standard_style()
    fig, ax = plt.subplots(
        figsize=(max(10, 1.3 * len(heatmap_df.columns)), max(5, 0.8 * len(heatmap_df.index))),
        constrained_layout=True,
    )
    sns.heatmap(
        heatmap_df,
        annot=True,
        fmt=".2f",
        cmap="RdBu_r",
        center=0.0,
        linewidths=1.0,
        linecolor="white",
        cbar_kws={"shrink": 0.8},
        ax=ax,
    )
    ax.set_xlabel("")
    ax.set_ylabel("")
    format_axes(ax)
    fig.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close(fig)


def plot_cross_benchmark_box(all_summary: pd.DataFrame, output_path: Path) -> None:
    data = all_summary.dropna(subset=["spearman"]).copy()
    if data.empty:
        return
    method_order = (
        data.groupby("method")["spearman"].mean().sort_values(ascending=False).index.tolist()
    )

    set_standard_style()
    fig, ax = plt.subplots(figsize=(13, 7), constrained_layout=True)
    sns.boxplot(
        data=data,
        x="method",
        y="spearman",
        hue="method_class",
        order=method_order,
        palette=METHOD_CLASS_COLORS,
        dodge=False,
        fliersize=0,
        ax=ax,
    )
    sns.stripplot(
        data=data,
        x="method",
        y="spearman",
        order=method_order,
        color="black",
        size=6,
        alpha=0.65,
        ax=ax,
    )
    ax.set_xlabel("")
    ax.set_ylabel("Spearman vs Activity")
    apply_method_tick_labels(ax, rotation=35, fontsize=14)
    ax.legend(frameon=False, title="")
    format_axes(ax)
    fig.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close(fig)


def plot_cross_benchmark_bar_grid(all_summary: pd.DataFrame, output_path: Path) -> None:
    data = all_summary.dropna(subset=["spearman"]).copy()
    if data.empty:
        return

    benchmark_order = sorted(data["benchmark"].unique().tolist())
    n_panels = len(benchmark_order)
    n_cols = 3 if n_panels > 4 else 2
    n_rows = int(math.ceil(n_panels / n_cols))

    set_standard_style()
    fig, axes = plt.subplots(
        n_rows,
        n_cols,
        figsize=(7.4 * n_cols, 5.6 * n_rows),
        constrained_layout=True,
    )
    axes_flat = np.atleast_1d(axes).ravel()

    legend_handles = None
    legend_labels = None
    for ax, benchmark_name in zip(axes_flat, benchmark_order):
        subset = data[data["benchmark"] == benchmark_name].copy()
        if subset.empty:
            ax.set_visible(False)
            continue
        subset = subset.sort_values(by="spearman", ascending=False)
        sns.barplot(
            data=subset,
            x="method",
            y="spearman",
            hue="method_class",
            dodge=False,
            palette=METHOD_CLASS_COLORS,
            ax=ax,
        )
        ax.set_title(str(benchmark_name), fontsize=14, fontweight="bold", pad=8)
        ax.set_xlabel("")
        ax.set_ylabel("Spearman")
        apply_method_tick_labels(ax, rotation=40, fontsize=12)
        ax.tick_params(axis="y", labelsize=12)
        if legend_handles is None:
            legend_handles, legend_labels = ax.get_legend_handles_labels()
        legend = ax.get_legend()
        if legend is not None:
            legend.remove()
        format_axes(ax)

    for ax in axes_flat[n_panels:]:
        ax.set_visible(False)

    if legend_handles and legend_labels:
        fig.legend(
            legend_handles,
            legend_labels,
            loc="upper center",
            bbox_to_anchor=(0.5, 1.02),
            ncol=len(legend_labels),
            frameon=False,
        )
    fig.savefig(output_path, dpi=300, bbox_inches="tight")
    plt.close(fig)


def write_summary_tables(
    all_summary: pd.DataFrame,
    output_dir: Path,
) -> None:
    all_summary.to_csv(output_dir / "plot_method_activity_summary.csv", index=False)
    aggregate = (
        all_summary.groupby(["method", "method_class"], dropna=False)
        .agg(
            benchmarks=("benchmark", "nunique"),
            mean_spearman=("spearman", "mean"),
            std_spearman=("spearman", "std"),
            mean_pearson=("pearson", "mean"),
            representative_metric=("metric_label", lambda values: ", ".join(sorted(set(str(v) for v in values if str(v))))),
        )
        .reset_index()
        .sort_values(by="mean_spearman", ascending=False)
    )
    aggregate.to_csv(output_dir / "plot_method_activity_aggregate.csv", index=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plot affinity-vs-activity comparisons for Boltz2Score modes vs FEP/MMGBSA/docking baselines."
    )
    parser.add_argument(
        "--input_root",
        type=str,
        default="data",
        help="Root containing benchmark compare outputs, or a specific mode_benchmark_compare directory.",
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default=None,
        help="Directory to write plots. Default: <input_root>/mode_benchmark_compare_plots",
    )
    parser.add_argument(
        "--target",
        type=str,
        default=None,
        help="Activity target column to compare against. Default: auto-prefer activity_pic50, then activity_score_from_dg.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    input_root = Path(args.input_root).expanduser().resolve()
    compare_dirs = discover_compare_dirs(input_root)
    if not compare_dirs:
        raise FileNotFoundError(f"No benchmark compare outputs found under {input_root}")

    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else (input_root / "mode_benchmark_compare_plots")
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    all_summaries: list[pd.DataFrame] = []
    for compare_dir in compare_dirs:
        benchmark_name, merged, correlations = load_compare_bundle(compare_dir)
        target_column = choose_activity_target(merged, args.target)
        benchmark_out = output_dir / benchmark_name
        benchmark_out.mkdir(parents=True, exist_ok=True)

        summary = build_method_summary(
            benchmark_name=benchmark_name,
            merged=merged,
            correlations=correlations,
            target_column=target_column,
        )
        if summary.empty:
            print(f"[{benchmark_name}] No plottable method/activity correlations found.")
            continue
        all_summaries.append(summary)

        plot_method_bar(
            summary=summary,
            benchmark_name=benchmark_name,
            target_label=target_column,
            output_path=benchmark_out / "activity_spearman_bar.png",
            value_column="spearman",
        )
        plot_method_bar(
            summary=summary,
            benchmark_name=benchmark_name,
            target_label=target_column,
            output_path=benchmark_out / "activity_pearson_bar.png",
            value_column="pearson",
        )
        plot_benchmark_heatmap(
            summary=summary,
            benchmark_name=benchmark_name,
            output_path=benchmark_out / "activity_method_heatmap.png",
        )
        plot_scatter_grid(
            merged=merged,
            summary=summary,
            benchmark_name=benchmark_name,
            target_column=target_column,
            output_path=benchmark_out / "activity_scatter_grid.png",
        )
        summary.to_csv(benchmark_out / "activity_method_summary.csv", index=False)
        print(f"[{benchmark_name}] Wrote plots -> {benchmark_out}")

    if not all_summaries:
        raise RuntimeError("No benchmark summaries could be built for plotting.")

    all_summary = pd.concat(all_summaries, ignore_index=True)
    write_summary_tables(all_summary, output_dir)
    plot_cross_benchmark_bar_grid(
        all_summary=all_summary,
        output_path=output_dir / "cross_benchmark_spearman_bar_grid.png",
    )
    plot_cross_benchmark_heatmap(
        all_summary=all_summary,
        output_path=output_dir / "cross_benchmark_spearman_heatmap.png",
    )
    plot_cross_benchmark_box(
        all_summary=all_summary,
        output_path=output_dir / "cross_benchmark_spearman_boxplot.png",
    )
    print(f"Wrote aggregate plots -> {output_dir}")


if __name__ == "__main__":
    main()
