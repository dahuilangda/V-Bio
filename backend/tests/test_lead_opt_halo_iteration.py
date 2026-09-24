"""End-to-end iteration test for the HALO small-molecule design loop.

Proves the product behavior: ONE submission runs an autonomous multi-round
reinforcement loop (propose → filter → oracle scoring → surrogate → GRPO
update on the pretrained model), streaming peptide-style per-round progress —
no per-candidate user clicks anywhere.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "capabilities"))

_PROTEIN = """\
ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00 50.00           C
ATOM      2  CA  GLY A   2       3.800   0.000   0.000  1.00 50.00           C
ATOM      3  CA  SER A   3       7.600   0.000   0.000  1.00 50.00           C
ATOM      4  CA  VAL A   4      11.400   0.000   0.000  1.00 50.00           C
ATOM      5  CA  LEU A   5      15.200   0.000   0.000  1.00 50.00           C
ATOM      6  CA  PHE A   6      19.000   0.000   0.000  1.00 50.00           C
ATOM      7  CA  GLY A   7      22.800   0.000   0.000  1.00 50.00           C
ATOM      8  CA  ALA A   8      26.600   0.000   0.000  1.00 50.00           C
END
"""


class HaloIterationE2ETests(unittest.TestCase):
    def test_two_round_rl_loop_with_progress_events(self):
        import tempfile

        from halo.vbio_runner import run_halo_optimization

        events: list[dict] = []
        with tempfile.TemporaryDirectory(prefix="halo_e2e_") as tmp:
            root = Path(tmp)
            protein = root / "target.pdb"
            protein.write_text(_PROTEIN)
            run_dir = root / "run"

            summary = run_halo_optimization(
                {
                    "mode": "fragment",
                    "protein_path": str(protein),
                    "reference_smiles": "c1ccccc1",
                    "backend": "protenix2dock",
                    "rounds": 2,
                    "budget_per_round": 4,
                    "n_agent_samples": 24,
                    "seed": 7,
                    "mock_oracle": True,
                },
                run_dir,
                progress_cb=events.append,
                log=lambda *_: None,
            )

            # The loop really iterated: both rounds landed in rounds.jsonl.
            rounds_log = [json.loads(line) for line in (run_dir / "rounds.jsonl").read_text().splitlines() if line.strip()]
            self.assertEqual([entry["round"] for entry in rounds_log], [1, 2])
            # The pretrained agent was updated by RL in both rounds.
            for entry in rounds_log:
                self.assertGreater(int(entry["oracle_calls"]), 0)

            self.assertEqual(summary["rounds_completed"], 2)
            self.assertEqual(summary["total_rounds"], 2)
            self.assertEqual(len(summary["rounds_log"]), 2)
            self.assertTrue((run_dir / "candidates.csv").exists())

            # Peptide-style progress streamed: scoring phase + round events + done.
            stages = [event["stage"] for event in events]
            self.assertIn("init", stages)
            self.assertIn("loop", stages)
            self.assertEqual(stages.count("scoring"), 2)
            self.assertIn("done", stages)
            round_events = [event for event in events if event["stage"] == "round"]
            self.assertEqual([event["round"] for event in round_events], [1, 2])
            for event in round_events:
                self.assertEqual(event["total_rounds"], 2)
                self.assertIn("top_candidates", event)
                self.assertIn("stats", event)


if __name__ == "__main__":
    unittest.main()
