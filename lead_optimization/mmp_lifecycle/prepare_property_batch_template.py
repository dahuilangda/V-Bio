from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from lead_optimization.mmp_lifecycle.services.template_service import (
        write_property_batch_template,
    )
else:
    from .services.template_service import write_property_batch_template


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare property batch TSV template")
    parser.add_argument("--output", required=True, type=str, help="output tsv path")
    parser.add_argument(
        "--properties",
        default="PROPERTY_A,PROPERTY_B",
        type=str,
        help="comma-separated property names",
    )
    parser.add_argument("--rows", default=3, type=int, help="placeholder row count")
    args = parser.parse_args(argv)
    properties = [token.strip() for token in str(args.properties or "").split(",") if token.strip()]
    path = write_property_batch_template(args.output, property_names=properties, rows=args.rows)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
