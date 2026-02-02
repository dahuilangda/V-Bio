#!/usr/bin/env python3
"""Collect Boltz2Score confidence summaries into a CSV."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Dict, List


def _select_confidence_file(conf_files: List[Path]) -> Path | None:
    if not conf_files:
        return None
    for path in conf_files:
        if "_model_0" in path.name or "_model_1" in path.name:
            return path
    return conf_files[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect Boltz2Score metrics.")
    parser.add_argument("--pred_dir", required=True, type=str)
    parser.add_argument("--output_csv", required=True, type=str)
    args = parser.parse_args()

    pred_dir = Path(args.pred_dir).expanduser().resolve()
    output_csv = Path(args.output_csv).expanduser().resolve()

    rows: List[Dict[str, str]] = []
    all_keys = set()

    for subdir in sorted(pred_dir.iterdir()):
        if not subdir.is_dir():
            continue
        conf_files = sorted(subdir.glob("confidence_*.json"))
        conf_file = _select_confidence_file(conf_files)
        if not conf_file:
            continue
        with conf_file.open("r") as f:
            data = json.load(f)

        row: Dict[str, str] = {"id": subdir.name}
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                row[key] = json.dumps(value)
            else:
                row[key] = str(value)
        rows.append(row)
        all_keys.update(row.keys())

    if not rows:
        raise RuntimeError(f"No confidence JSON files found in {pred_dir}")

    fieldnames = ["id"] + sorted(k for k in all_keys if k != "id")
    output_csv.parent.mkdir(parents=True, exist_ok=True)

    with output_csv.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    print(f"Wrote {len(rows)} rows to {output_csv}")


if __name__ == "__main__":
    main()
