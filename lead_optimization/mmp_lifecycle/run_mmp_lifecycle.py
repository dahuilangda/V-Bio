from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from lead_optimization.mmp_lifecycle.cli import main
else:
    from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
