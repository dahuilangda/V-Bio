"""Cross-layer parity of the affinity/boltz2score mode enum.

The mode list is declared independently in five Python layers and the TS type union; a drift
between any two silently changes what runs (the worker now rejects unknown modes loudly, but
the drift itself must be caught in tests before it reaches a task). This test binds them all.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.routes import affinity as affinity_route
from backend.worker import tasks as worker_tasks
from capabilities.boltz2score.core.modes import SUPPORTED_MODES
from management_api.task_snapshot import build_affinity_task_snapshot  # noqa: F401  (import guard)
from management_api.copilot_capabilities import TASK_PARAMETER_SCHEMA


def _set_from_literal(source: str, marker: str) -> frozenset:
    """Extract the set literal that follows ``marker`` in ``source``."""
    match = re.search(re.escape(marker) + r"\s*\{(.*?)\}", source, re.DOTALL)
    if not match:
        raise AssertionError(f"literal not found after marker {marker!r}")
    return frozenset(re.findall(r"['\"]([a-z_]+)['\"]", match.group(1)))


def _ts_union(source: str, marker: str) -> frozenset:
    match = re.search(re.escape(marker) + r"\s*=\s*([^;]+);", source)
    if not match:
        raise AssertionError(f"TS union not found after marker {marker!r}")
    return frozenset(re.findall(r"'([a-z]+)'", match.group(1)))


class AffinityModeParityTests(unittest.TestCase):
    def test_all_layers_declare_the_same_modes(self) -> None:
        capability = frozenset(SUPPORTED_MODES)
        worker_source = Path(worker_tasks.__file__).read_text(encoding="utf-8")
        snapshot_source = Path(__file__).resolve().parents[1] / "management_api" / "task_snapshot.py"
        snapshot_text = snapshot_source.read_text(encoding="utf-8")
        models_ts = (REPO_ROOT / "frontend" / "src" / "types" / "models.ts").read_text(encoding="utf-8")

        route = frozenset(affinity_route.VALID_BOLTZ2SCORE_MODES)
        worker = _set_from_literal(worker_source, "requested_mode not in")
        snapshot = _set_from_literal(snapshot_text, "mode not in")
        copilot = frozenset(TASK_PARAMETER_SCHEMA["affinityMode"]["values"])
        ts = _ts_union(models_ts, "export type AffinityScoringMode")

        self.assertEqual(route, capability, "route vs capability mode drift")
        self.assertEqual(worker, capability, "worker vs capability mode drift")
        self.assertEqual(snapshot, capability, "snapshot vs capability mode drift")
        self.assertEqual(copilot, capability, "copilot TASK_PARAMETER_SCHEMA vs capability mode drift")
        self.assertEqual(ts, capability, "TS AffinityScoringMode vs capability mode drift")


if __name__ == "__main__":
    unittest.main()
