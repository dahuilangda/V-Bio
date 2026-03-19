#!/usr/bin/env python3
"""Collect Boltz2Score confidence summaries into a CSV."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Dict, List

from utils.result_utils import discover_record_dirs, select_confidence_file_from_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect Boltz2Score metrics.")
    parser.add_argument("--pred_dir", required=True, type=str)
    parser.add_argument("--output_csv", required=True, type=str)
    args = parser.parse_args()

    pred_dir = Path(args.pred_dir).expanduser().resolve()
    output_csv = Path(args.output_csv).expanduser().resolve()

    rows: List[Dict[str, str]] = []
    all_keys = set()

    for record_id, subdir in sorted(discover_record_dirs(pred_dir).items()):
        conf_file = select_confidence_file_from_dir(subdir, required=False)
        if not conf_file:
            continue
        with conf_file.open("r") as f:
            data = json.load(f)

        row: Dict[str, str] = {"id": record_id}
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
