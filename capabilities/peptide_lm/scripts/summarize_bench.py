#!/usr/bin/env python
"""Merge benchmark reports into the final comparison table."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(*reports):
    from pathlib import Path

    reports = reports or ["runs/bench/report.json"]
    data: dict = {}
    for rp in reports:
        p = Path(rp)
        if not p.exists():
            continue
        d = json.loads(p.read_text())
        setting = "leadopt" if "leadopt" in str(p.parent.name) + str(p) else "denovo"
        for target, arms in d.items():
            for arm, r in arms.items():
                if r:
                    data.setdefault(target, {})[f"{arm}:{setting}"] = r
    print(f"{'target':8s} {'arm':18s} {'best':>7s} {'top5':>7s} {'unique':>7s} {'oracle':>7s}")
    for target, arms in data.items():
        for arm, r in arms.items():
            if not r:
                continue
            print(f"{target:8s} {arm:18s} "
                  f"{(r.get('best_composite') or float('nan')):7.4f} "
                  f"{(r.get('top5_mean') or float('nan')):7.4f} "
                  f"{r.get('unique_scored', r.get('unique', 0)):7d} "
                  f"{r.get('oracle_calls', 0):7d}")
    print()
    for target, arms in data.items():
        for setting in ("denovo", "leadopt"):
            lm = arms.get(f"peptidelm:{setting}")
            ga = arms.get(f"ga:{setting}")
            if lm and ga and lm.get("best_composite") and ga.get("best_composite"):
                d = lm["best_composite"] - ga["best_composite"]
                d5 = (lm.get("top5_mean") or 0) - (ga.get("top5_mean") or 0)
                print(f"{target:8s} {setting:8s} best {d:+.4f}, top5 {d5:+.4f} "
                      f"({'WIN' if d > 0 else 'LOSS'})")


if __name__ == "__main__":
    main(*sys.argv[1:])
