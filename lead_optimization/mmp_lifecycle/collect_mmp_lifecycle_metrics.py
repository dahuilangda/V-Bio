from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from lead_optimization.mmp_lifecycle.models import PostgresTarget
    from lead_optimization.mmp_lifecycle.services.report_service import (
        fetch_schema_metrics,
        write_metrics_json,
    )
else:
    from .models import PostgresTarget
    from .services.report_service import fetch_schema_metrics, write_metrics_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect MMP lifecycle metrics for one schema")
    parser.add_argument("--postgres_url", required=True, type=str)
    parser.add_argument("--postgres_schema", required=True, type=str)
    parser.add_argument("--recent_limit", default=10, type=int)
    parser.add_argument("--output_json", default="", type=str)
    args = parser.parse_args(argv)

    target = PostgresTarget.from_inputs(url=args.postgres_url, schema=args.postgres_schema)
    payload = fetch_schema_metrics(target, recent_limit=args.recent_limit)
    if args.output_json:
        path = write_metrics_json(args.output_json, payload)
        print(path)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
