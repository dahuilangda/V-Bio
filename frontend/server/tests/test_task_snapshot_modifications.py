from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask, request

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from management_api.task_snapshot import build_prediction_task_snapshot_from_yaml

MODIFIED_YAML = """\
version: 1
sequences:
  - protein:
      id: A
      sequence: ACDEFGHIK
  - protein:
      id: B
      sequence: CAARFKVWWCLSNHC
      modifications:
        - position: 11
          ccd: NLE
          baseResidue: L
        - position: 1
          ccd: nc1
        - position: 0
          ccd: BAD
        - position: 40
          ccd: BAD
        - position: 3
        - ccd: NOPOS
  - ligand:
      id: L
      ccd: ZBR
"""


def _snapshot(yaml_text: str) -> dict:
    app = Flask(__name__)
    with app.test_request_context(
        "/predict",
        method="POST",
        data={"yaml_file": (io.BytesIO(yaml_text.encode("utf-8")), "input.yaml")},
        content_type="multipart/form-data",
    ):
        return build_prediction_task_snapshot_from_yaml(request, mock.Mock())


class ProteinModificationSnapshotTests(unittest.TestCase):
    def test_protein_component_carries_modifications(self) -> None:
        components = _snapshot(MODIFIED_YAML)["components"]
        binder = next(item for item in components if item["id"] == "yaml-protein-2")
        self.assertEqual(
            binder["modifications"],
            [
                {
                    "id": "yaml-protein-2-mod-11",
                    "position": 11,
                    "terminal": "internal",
                    "baseResidue": "L",
                    "ccd": "NLE",
                    "inputMethod": "ccd",
                },
                {
                    "id": "yaml-protein-2-mod-1",
                    "position": 1,
                    "terminal": "n_term",
                    "baseResidue": "C",
                    "ccd": "NC1",
                    "inputMethod": "ccd",
                },
            ],
        )

    def test_components_without_modifications_have_no_key(self) -> None:
        components = _snapshot(MODIFIED_YAML)["components"]
        self.assertNotIn("modifications", components[0])
        self.assertNotIn("modifications", components[2])

    def test_last_residue_is_c_terminal(self) -> None:
        yaml_text = (
            "version: 1\nsequences:\n  - protein:\n      id: A\n      sequence: ACDE\n"
            "      modifications:\n        - position: 4\n          ccd: NME\n"
        )
        modification = _snapshot(yaml_text)["components"][0]["modifications"][0]
        self.assertEqual(modification["terminal"], "c_term")
        self.assertEqual(modification["baseResidue"], "E")


if __name__ == "__main__":
    unittest.main()
