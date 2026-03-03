#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Predicted vs Experimental pIC50 可视化与评估脚本
------------------------------------------------
功能：
1) 从 CSV 读取列：实验 pIC50（默认 pIC50_exp）、AI 预测 pIC50（默认 pIC50_pred）
2) 从 FEP ΔG（kcal/mol，默认列名 FEP_Pred. ΔG）换算 FEP_pIC50_pred = -ΔG/(2.303*RT), RT≈0.593
3) 分别绘制两张图（AI、FEP）：
   - 散点
   - 线性回归线（方程与 R²）
   - 理想线 y=x
   - ±1 阴影带
4) 输出 summary_metrics.csv：R²、斜率、截距、MAE、RMSE、±1 覆盖率等

用法示例：
python pic50_compare.py \
  --input hif2a_pIC50_comparison.csv \
  --outdir out_plots \
  --xcol pIC50_exp \
  --ycol pIC50_pred \
  --fep_dg_col "FEP_Pred. ΔG" \
  --save_fep_pred
"""

import os
import argparse
import math
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

# -------------------- metrics helpers --------------------
def linregress_xy(x, y):
    """简单线性回归 y = a*x + b，返回 a, b"""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    xm = np.mean(x)
    ym = np.mean(y)
    denom = np.sum((x - xm) ** 2)
    if denom == 0:
        return np.nan, np.nan
    a = np.sum((x - xm) * (y - ym)) / denom
    b = ym - a * xm
    return a, b

def r2_score_xy(x, y):
    """R² = 1 - SS_res/SS_tot"""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    a, b = linregress_xy(x, y)
    yhat = a * x + b
    ss_res = np.sum((y - yhat) ** 2)
    ss_tot = np.sum((y - np.mean(y)) ** 2)
    if ss_tot == 0:
        return np.nan
    return 1.0 - ss_res / ss_tot

def mae_xy(x, y):
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    return float(np.mean(np.abs(y - x)))

def rmse_xy(x, y):
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    return float(np.sqrt(np.mean((y - x) ** 2)))

def within_band_xy(x, y, band=1.0):
    """返回落在 |y - x| <= band 的数量与比例"""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    mask = np.isfinite(x) & np.isfinite(y)
    n = int(mask.sum())
    k = int(np.sum(np.abs(y[mask] - x[mask]) <= band))
    pct = (k / n * 100.0) if n > 0 else np.nan
    return k, n, pct

# -------------------- plotting --------------------
def plot_scatter_with_reg(x, y, title, outpath, dpi=200):
    """
    单图：散点 + 回归线(带方程与R²) + y=x + ±1 阴影带
    注意：为通用性，这里仅用 matplotlib（不依赖 seaborn），并且每张图只包含一个绘图对象。
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    mask = np.isfinite(x) & np.isfinite(y)
    x = x[mask]; y = y[mask]

    a, b = linregress_xy(x, y)
    r2 = r2_score_xy(x, y)

    # 画布
    plt.figure(figsize=(7, 7))

    # 散点
    plt.scatter(x, y, s=40, alpha=0.7, edgecolor='k', linewidths=0.5)

    # 回归线
    if np.isfinite(a) and np.isfinite(b):
        xs = np.linspace(x.min(), x.max(), 200)
        ys = a * xs + b
        plt.plot(xs, ys, linewidth=2, label=f"Fit: y={a:.2f}x+{b:.2f}; R²={r2:.3f}")

    # 理想线 y=x
    lo, hi = float(np.min(x)), float(np.max(x))
    plt.plot([lo, hi], [lo, hi], linestyle="--", linewidth=1)

    # ±1 阴影带（围绕 y=x）
    band_x = np.linspace(lo, hi, 200)
    plt.fill_between(band_x, band_x - 1.0, band_x + 1.0, alpha=0.15)

    plt.xlabel("Experimental pIC50")
    plt.ylabel("Predicted pIC50")
    plt.title(title)
    plt.legend(loc="best")
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(outpath, dpi=dpi)
    plt.close()

# -------------------- main --------------------
def main():
    parser = argparse.ArgumentParser(description="Compare predicted vs experimental pIC50 with regression, R² and ±1 band.")
    parser.add_argument("--input", required=True, help="输入 CSV 路径")
    parser.add_argument("--outdir", default="pic50_out", help="输出目录（图与汇总）")
    parser.add_argument("--xcol", default="pIC50_exp", help="实验 pIC50 列名")
    parser.add_argument("--ycol", default="pIC50_pred", help="AI 预测 pIC50 列名")
    parser.add_argument("--fep_dg_col", default="FEP_Pred. ΔG", help="FEP 预测 ΔG 列名（kcal/mol）")
    parser.add_argument("--save_fep_pred", action="store_true", help="是否把 FEP_pIC50_pred 写回 CSV")
    parser.add_argument("--dpi", type=int, default=200, help="保存图片分辨率 DPI")
    args = parser.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    df = pd.read_csv(args.input)

    # --- AI vs EXP ---
    if args.xcol not in df.columns or args.ycol not in df.columns:
        raise ValueError(f"找不到列：{args.xcol} 或 {args.ycol}。实际列有：{list(df.columns)}")

    x_ai = df[args.xcol]
    y_ai = df[args.ycol]

    a_ai, b_ai = linregress_xy(x_ai, y_ai)
    r2_ai = r2_score_xy(x_ai, y_ai)
    mae_ai = mae_xy(x_ai, y_ai)
    rmse_ai = rmse_xy(x_ai, y_ai)
    k_ai, n_ai, pct_ai = within_band_xy(x_ai, y_ai, band=1.0)

    plot_scatter_with_reg(
        x_ai, y_ai,
        title="AI Model Predicted vs Experimental",
        outpath=os.path.join(args.outdir, "AI_vs_Exp.png"),
        dpi=args.dpi
    )

    # --- FEP vs EXP ---
    if args.fep_dg_col in df.columns:
        RT = 0.593  # kcal/mol at 298K
        df["FEP_pIC50_pred"] = -df[args.fep_dg_col] / (2.303 * RT)
        x_fep = df[args.xcol]
        y_fep = df["FEP_pIC50_pred"]

        a_fep, b_fep = linregress_xy(x_fep, y_fep)
        r2_fep = r2_score_xy(x_fep, y_fep)
        mae_fep = mae_xy(x_fep, y_fep)
        rmse_fep = rmse_xy(x_fep, y_fep)
        k_fep, n_fep, pct_fep = within_band_xy(x_fep, y_fep, band=1.0)

        plot_scatter_with_reg(
            x_fep, y_fep,
            title="FEP Predicted vs Experimental",
            outpath=os.path.join(args.outdir, "FEP_vs_Exp.png"),
            dpi=args.dpi
        )
    else:
        a_fep = b_fep = r2_fep = mae_fep = rmse_fep = k_fep = n_fep = pct_fep = np.nan

    # --- 保存汇总指标 ---
    summary = pd.DataFrame([
        {
            "model": "AI",
            "xcol": args.xcol,
            "ycol": args.ycol,
            "slope": a_ai,
            "intercept": b_ai,
            "R2": r2_ai,
            "MAE": mae_ai,
            "RMSE": rmse_ai,
            "within_±1_count": k_ai,
            "within_±1_total": n_ai,
            "within_±1_percent": pct_ai
        },
        {
            "model": "FEP",
            "xcol": args.xcol,
            "ycol": "FEP_pIC50_pred" if args.fep_dg_col in df.columns else np.nan,
            "slope": a_fep,
            "intercept": b_fep,
            "R2": r2_fep,
            "MAE": mae_fep,
            "RMSE": rmse_fep,
            "within_±1_count": k_fep,
            "within_±1_total": n_fep,
            "within_±1_percent": pct_fep
        }
    ])

    summary_path = os.path.join(args.outdir, "summary_metrics.csv")
    summary.to_csv(summary_path, index=False)

    # 可选写回 FEP_pIC50_pred 列
    if args.save_fep_pred and "FEP_pIC50_pred" in df.columns:
        out_csv = os.path.join(args.outdir, "data_with_fep_pic50.csv")
        df.to_csv(out_csv, index=False)

    print("Done.")
    print(f"- 图像: {os.path.join(args.outdir, 'AI_vs_Exp.png')}")
    if args.fep_dg_col in df.columns:
        print(f"- 图像: {os.path.join(args.outdir, 'FEP_vs_Exp.png')}")
    print(f"- 指标: {summary_path}")

if __name__ == "__main__":
    main()
