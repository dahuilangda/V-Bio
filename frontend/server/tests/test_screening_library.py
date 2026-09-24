"""Tests for the compounds_file snapshot path and the mirrored library parser.

The parser here must behave identically to ``backend/runtime/screening_library.py``
(the runtime authority) — the parity test imports both and asserts the same records
for a battery of library formats, so the two copies cannot drift.
"""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from flask import Flask, request

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from management_api.gateway_task import forward_task_read
from management_api.screening_library import parse_screening_compounds_file
from management_api.task_snapshot import (
    TASK_INPUT_OPTIONS_KEY,
    build_prediction_task_snapshot_from_yaml,
)
from backend.runtime.screening_library import (
    parse_screening_compounds_file as runtime_parse_screening_compounds_file,
)

TARGET_ONLY_YAML = """\
version: 1
sequences:
  - protein:
      id: A
      sequence: ACDEFGHIK
"""

FASTA_LIBRARY = ">aspirin\nCC(=O)OC1=CC=CC=C1C(=O)O\n>caffeine\nCn1c(=O)c2c(ncn2C)n(C)c1=O\n"

PARITY_VECTORS = [
    FASTA_LIBRARY,
    "CCO ethanol\nc1ccccc1 benzene\n",
    "CCO\nc1ccccc1\n",
    "smiles,name\nCCO,ethanol\nc1ccccc1,benzene\n",
    "canonical_smiles,compound_name\nCCO,ethanol\n",
    "CCO\tethanol\nc1ccccc1\tbenzene\n",
    'smiles,name\n"CCO","ethyl alcohol"\n',
    ">dup\nCCO\n>dup\nCCS\n",
    ">阿司匹林\nCCO\n",
    ">aspirin\r\nCCO\r\n",
    ">mixed\nCCO extra tokens\n",
    "# comment only\n",
    "",
    ">\nCCO\n",
    ">orphan\n>filled\nCCO\n",
    "smiles,name\n,ethanol\n",
    'smiles,name\n"CCO,ethanol\n',
    "smiles,name\n,ethanol\nCCO,late\n",
    "CCO\tname1\n\tCCN\tname2\n",
    "CCO\tname1\t\n",
    ">a\u064Bb\nCCO\n",
]


class ParserParityTests(unittest.TestCase):
    def test_mirrored_parser_matches_runtime_for_all_vectors(self) -> None:
        for vector in PARITY_VECTORS:
            with self.subTest(vector=vector[:40]):
                local_error = runtime_error = None
                local = runtime = None
                try:
                    local = parse_screening_compounds_file(vector)
                except ValueError as exc:
                    local_error = str(exc)
                try:
                    runtime = runtime_parse_screening_compounds_file(vector)
                except ValueError as exc:
                    runtime_error = str(exc)
                self.assertEqual(local, runtime)
                self.assertEqual(local_error, runtime_error)


class CompoundsFileSnapshotTests(unittest.TestCase):
    def _snapshot(self, files: dict) -> dict:
        app = Flask(__name__)
        data = {"yaml_file": (io.BytesIO(TARGET_ONLY_YAML.encode("utf-8")), "input.yaml"), **files}
        with app.test_request_context(
            "/predict", method="POST", data=data, content_type="multipart/form-data"
        ):
            return build_prediction_task_snapshot_from_yaml(request, mock.Mock())

    def test_snapshot_includes_file_library(self) -> None:
        snapshot = self._snapshot({"compounds_file": (io.BytesIO(FASTA_LIBRARY.encode("utf-8")), "library.smi")})
        options = snapshot["properties"][TASK_INPUT_OPTIONS_KEY]
        self.assertEqual(options["virtualScreening"]["compoundCount"], 2)
        self.assertIn(">aspirin", options["virtualScreeningInput"])
        self.assertIn(">caffeine", options["virtualScreeningInput"])

    def test_snapshot_skips_library_the_runtime_would_reject(self) -> None:
        snapshot = self._snapshot({"compounds_file": (io.BytesIO(b"\xff\xfe not utf8"), "library.smi")})
        self.assertEqual(snapshot["components"][0]["type"], "protein")
        self.assertNotIn(TASK_INPUT_OPTIONS_KEY, snapshot["properties"])

    def test_snapshot_keeps_inline_library_when_no_file(self) -> None:
        inline = TARGET_ONLY_YAML + "virtual_screening:\n  compounds:\n    - smiles: CCO\n"
        app = Flask(__name__)
        with app.test_request_context(
            "/predict",
            method="POST",
            data={"yaml_file": (io.BytesIO(inline.encode("utf-8")), "input.yaml")},
            content_type="multipart/form-data",
        ):
            snapshot = build_prediction_task_snapshot_from_yaml(request, mock.Mock())
        options = snapshot["properties"][TASK_INPUT_OPTIONS_KEY]
        self.assertEqual(options["virtualScreening"]["compoundCount"], 1)


class ForwardTaskReadSuffixTests(unittest.TestCase):
    def _gateway(self, seen: dict):
        def proxy_get(path, query):
            seen["path"] = path
            return SimpleNamespace(_path=path)

        return SimpleNamespace(
            _read_project_id_from_query=lambda: "project-1",
            _authorize_project_read=lambda *_a, **_k: SimpleNamespace(token="t", is_platform=False),
            task_store=SimpleNamespace(find_project_task=lambda *_a: {"id": "row-1"}),
            _proxy_get=proxy_get,
            _build_flask_response=lambda upstream: ("body", 200),
            _record_usage=lambda *a, **k: None,
            _forbidden=lambda *a, **k: ("forbidden", 403),
            logger=mock.Mock(),
        )

    def test_suffix_is_appended_to_upstream_path(self) -> None:
        seen: dict = {}
        gateway = self._gateway(seen)
        app = Flask(__name__)
        with app.test_request_context("/vbio-api/results/task-1/screening?project_id=project-1"):
            forward_task_read(gateway, "task-1", "/results", "read_screening", upstream_suffix="/screening")
        self.assertEqual(seen["path"], "/results/task-1/screening")

        with app.test_request_context("/vbio-api/results/task-1/view?project_id=project-1"):
            forward_task_read(gateway, "task-1", "/results", "read_results_view", upstream_suffix="/view")
        self.assertEqual(seen["path"], "/results/task-1/view")

        with app.test_request_context("/vbio-api/results/task-1?project_id=project-1"):
            forward_task_read(gateway, "task-1", "/results", "read_results")
        self.assertEqual(seen["path"], "/results/task-1")

    def test_forward_returns_response(self) -> None:
        gateway = self._gateway({})
        app = Flask(__name__)
        with app.test_request_context("/vbio-api/results/task-1/screening?project_id=project-1"):
            body, status = forward_task_read(
                gateway, "task-1", "/results", "read_screening", upstream_suffix="/screening"
            )
        self.assertEqual(status, 200)


class GatewayHandlerWrapperTests(unittest.TestCase):
    def test_bound_wrapper_forwards_suffix_kwarg(self) -> None:
        from management_api.gateway_handlers import GatewayHandlers

        seen: dict = {}

        def proxy_get(path, query):
            seen["path"] = path
            return SimpleNamespace(_path=path)

        gateway = SimpleNamespace(
            _read_project_id_from_query=lambda: "project-1",
            _authorize_project_read=lambda *_a, **_k: SimpleNamespace(token="t", is_platform=False),
            task_store=SimpleNamespace(find_project_task=lambda *_a: {"id": "row-1"}),
            _proxy_get=proxy_get,
            _build_flask_response=lambda upstream: ("body", 200),
            _record_usage=lambda *a, **k: None,
            _forbidden=lambda *a, **k: ("forbidden", 403),
            logger=mock.Mock(),
        )
        app = Flask(__name__)
        with app.test_request_context("/vbio-api/results/task-1/screening?project_id=project-1"):
            GatewayHandlers.forward_task_read(
                gateway, "task-1", "/results", "read_screening", upstream_suffix="/screening"
            )
        self.assertEqual(seen["path"], "/results/task-1/screening")


if __name__ == "__main__":
    unittest.main()


class SubmitSnapshotIdempotencyTests(unittest.TestCase):
    """One runtime task is exactly one task row — the doubled-submit regression.

    The gateway's submit snapshot and the frontend's own draft-row patch both write a row
    for the same task_id; whichever lands second used to INSERT a duplicate ("Task <id>"
    rows users saw as doubled tasks). The unique partial index on task_id now enforces the
    invariant, and the snapshot insert must YIELD on conflict (Prefer resolution=
    ignore-duplicates) instead of erroring the submit.
    """

    def test_insert_snapshot_uses_ignore_duplicates_prefer(self):
        from management_api.task_store import ProjectTaskStore

        recorded: dict[str, Any] = {}

        class StubPostgrest:
            def request(self, method: str, table: str, *, payload=None, headers=None, expect_json=True, **_kwargs):
                recorded.update({"method": method, "table": table, "payload": payload, "headers": headers})
                return None

        store = ProjectTaskStore(postgrest=StubPostgrest())
        store.insert_snapshot(
            project_id="proj-1",
            task_id="task-1",
            task_name="CD73",
            task_summary="",
            backend="protenix",
            seed=42,
        )
        self.assertEqual(recorded["method"], "POST")
        self.assertEqual(recorded["table"], "project_tasks")
        prefer = str((recorded["headers"] or {}).get("Prefer") or "")
        self.assertIn("resolution=ignore-duplicates", prefer)
        self.assertEqual(recorded["payload"]["task_id"], "task-1")
