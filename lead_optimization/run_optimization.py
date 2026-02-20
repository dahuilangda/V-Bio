#!/usr/bin/env python3
"""Legacy lead optimization submit runner shim.

The historical optimization-engine pipeline is removed.
VBio lead optimization now runs through MMP workflow APIs.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Legacy lead optimization runner (disabled).")
    parser.add_argument("--target_config", type=str, required=False)
    parser.add_argument("--input_compound", type=str, required=False)
    parser.add_argument("--input_file", type=str, required=False)
    parser.add_argument("--output_dir", type=str, required=True)
    return parser


def main() -> int:
    parser = _build_parser()
    args, _unknown = parser.parse_known_args()

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "status": "deprecated",
        "message": (
            "Legacy /api/lead_optimization/submit pipeline has been disabled. "
            "Use Lead Optimization MMP workflow APIs instead."
        ),
        "created_at": time.time(),
    }
    (output_dir / "optimization_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with (output_dir / "optimization_results.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["status", "message"])
        writer.writerow(["deprecated", summary["message"]])

    sys.stderr.write(f"{summary['message']}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

