#!/usr/bin/env python3
"""Collect Protenix summary confidence JSON files into a CSV table."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


def _as_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def collect_rows(pred_dir: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in sorted(pred_dir.rglob("*_summary_confidence_sample_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                continue
            rows.append(
                {
                    "file": str(path.relative_to(pred_dir)),
                    "ranking_score": _as_scalar(payload.get("ranking_score")),
                    "plddt": _as_scalar(payload.get("plddt")),
                    "ptm": _as_scalar(payload.get("ptm")),
                    "iptm": _as_scalar(payload.get("iptm")),
                    "gpde": _as_scalar(payload.get("gpde")),
                    "chain_plddt": _as_scalar(payload.get("chain_plddt")),
                    "chain_ptm": _as_scalar(payload.get("chain_ptm")),
                    "chain_iptm": _as_scalar(payload.get("chain_iptm")),
                    "chain_pair_iptm": _as_scalar(payload.get("chain_pair_iptm")),
                    "chain_pair_iptm_global": _as_scalar(payload.get("chain_pair_iptm_global")),
                }
            )
        except Exception:
            continue
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect Protenix summary confidence JSON files into CSV.")
    parser.add_argument("--pred_dir", required=True, help="Directory containing Protenix inference outputs.")
    parser.add_argument("--output_csv", required=True, help="Path to write CSV.")
    args = parser.parse_args()

    pred_dir = Path(args.pred_dir).resolve()
    if not pred_dir.exists():
        raise FileNotFoundError(f"Prediction directory not found: {pred_dir}")

    rows = collect_rows(pred_dir)
    if not rows:
        raise RuntimeError(f"No summary confidence JSON found under: {pred_dir}")

    output_csv = Path(args.output_csv).resolve()
    output_csv.parent.mkdir(parents=True, exist_ok=True)

    fields = [
        "file",
        "ranking_score",
        "plddt",
        "ptm",
        "iptm",
        "gpde",
        "chain_plddt",
        "chain_ptm",
        "chain_iptm",
        "chain_pair_iptm",
        "chain_pair_iptm_global",
    ]
    with output_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} rows to {output_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
