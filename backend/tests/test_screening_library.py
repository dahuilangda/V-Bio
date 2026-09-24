from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml
from flask import Flask, request

FRONTEND_SERVER_ROOT = Path(__file__).resolve().parents[2] / "frontend" / "server"
if str(FRONTEND_SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(FRONTEND_SERVER_ROOT))

from backend.routes.prediction import register_prediction_routes
from backend.routes.task import register_task_routes
from backend.runtime.screening_library import (
    merge_screening_compounds_file_into_yaml,
    parse_screening_compounds_file,
)
from backend.scheduling.capability_router import capability_from_prediction_backend
from backend.services.common_utils import infer_use_msa_server_from_yaml_text
from management_api.task_snapshot import build_prediction_task_snapshot_from_yaml

TARGET_ONLY_YAML = """\
version: 1
sequences:
  - protein:
      id: A
      sequence: ACDEFGHIK
"""

FASTA_LIBRARY = ">aspirin\nCC(=O)OC1=CC=CC=C1C(=O)O\n>caffeine\nCn1c(=O)c2c(ncn2C)n(C)c1=O\n"
SMI_LIBRARY = "CCO ethanol\nc1ccccc1 benzene\n"
CSV_LIBRARY = "smiles,name\nCCO,ethanol\nc1ccccc1,benzene\n"
TSV_LIBRARY = "CCO\tethanol\nc1ccccc1\tbenzene\n"


def _register_predict_app() -> tuple[Flask, mock.Mock]:
    app = Flask(__name__)
    predict_task = mock.Mock()
    predict_task.apply_async.return_value = SimpleNamespace(id="task-1")
    register_prediction_routes(
        app,
        require_api_token=lambda handler: handler,
        logger=mock.Mock(),
        config_module=SimpleNamespace(MSA_SERVER_URL="", TENANT_MAX_DAILY=0),
        predict_task=predict_task,
        parse_bool=lambda value, default: default if value is None else str(value).lower() == "true",
        parse_int=lambda value, default: default if value is None else int(value),
        infer_use_msa_server_from_yaml_text=infer_use_msa_server_from_yaml_text,
        extract_template_meta_from_yaml=lambda _source: {},
        normalize_chain_id_list=lambda _value: [],
        select_queue_for_capability=lambda capability, priority: {
            "online": True,
            "queue": f"cap.{capability}.{priority}",
        },
        capability_from_prediction_backend=capability_from_prediction_backend,
    )
    return app, predict_task


class ScreeningLibraryParserTests(unittest.TestCase):
    def test_parses_fasta_style_records(self) -> None:
        compounds = parse_screening_compounds_file(FASTA_LIBRARY)
        self.assertEqual([c["name"] for c in compounds], ["aspirin", "caffeine"])
        self.assertEqual(compounds[0]["smiles"], "CC(=O)OC1=CC=CC=C1C(=O)O")
        self.assertEqual([c["id"] for c in compounds], ["aspirin", "caffeine"])

    def test_fasta_smiles_takes_first_token_of_continuation_lines(self) -> None:
        compounds = parse_screening_compounds_file(">mixed\nCCO extra tokens\n")
        self.assertEqual(compounds[0]["smiles"], "CCO")

    def test_content_before_any_fasta_header_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "expected a record header"):
            parse_screening_compounds_file(">\nCCO\n")

    def test_fasta_record_without_smiles_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "SMILES is missing"):
            parse_screening_compounds_file(">orphan\n>filled\nCCO\n")

    def test_parses_smi_lines_with_names(self) -> None:
        compounds = parse_screening_compounds_file(SMI_LIBRARY)
        self.assertEqual([(c["smiles"], c["name"]) for c in compounds], [
            ("CCO", "ethanol"),
            ("c1ccccc1", "benzene"),
        ])

    def test_plain_lines_default_names(self) -> None:
        compounds = parse_screening_compounds_file("CCO\nc1ccccc1\n")
        self.assertEqual([c["name"] for c in compounds], ["Compound 1", "Compound 2"])

    def test_parses_csv_with_header(self) -> None:
        compounds = parse_screening_compounds_file(CSV_LIBRARY)
        self.assertEqual([(c["smiles"], c["name"]) for c in compounds], [
            ("CCO", "ethanol"),
            ("c1ccccc1", "benzene"),
        ])

    def test_csv_header_aliases(self) -> None:
        compounds = parse_screening_compounds_file(
            "canonical_smiles,compound_name\nCCO,ethanol\n"
        )
        self.assertEqual((compounds[0]["smiles"], compounds[0]["name"]), ("CCO", "ethanol"))

    def test_parses_headerless_tsv_columns(self) -> None:
        compounds = parse_screening_compounds_file(TSV_LIBRARY)
        self.assertEqual([(c["smiles"], c["name"]) for c in compounds], [
            ("CCO", "ethanol"),
            ("c1ccccc1", "benzene"),
        ])

    def test_quoted_csv_preserves_spaced_names(self) -> None:
        compounds = parse_screening_compounds_file('smiles,name\n"CCO","ethyl alcohol"\n')
        self.assertEqual((compounds[0]["smiles"], compounds[0]["name"]), ("CCO", "ethyl alcohol"))

    def test_unterminated_quote_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unterminated quoted field"):
            parse_screening_compounds_file('smiles,name\n"CCO,ethanol\n')

    def test_empty_and_comment_only_files_are_rejected(self) -> None:
        for text in ("", "# just a comment\n", "\n\n"):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, "no compound records"):
                    parse_screening_compounds_file(text)

    def test_overlong_smiles_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "longer than 4096"):
            parse_screening_compounds_file(">big\n" + 4097 * "C" + "\n")

    def test_duplicate_names_get_unique_ids(self) -> None:
        compounds = parse_screening_compounds_file(">dup\nCCO\n>dup\nCCS\n")
        self.assertEqual([c["id"] for c in compounds], ["dup", "dup-2"])

    def test_non_ascii_names_fall_back_to_positional_ids(self) -> None:
        compounds = parse_screening_compounds_file(">阿司匹林\nCCO\n")
        self.assertEqual(compounds[0]["id"], "compound-001")
        self.assertEqual(compounds[0]["name"], "阿司匹林")

    def test_leading_tab_row_matches_ts_field_semantics(self) -> None:
        # The TS parser splits the untrimmed line, so a leading tab makes the first
        # (smiles) column empty and the row must be rejected, not shifted.
        with self.assertRaisesRegex(ValueError, "Line 2: SMILES is missing."):
            parse_screening_compounds_file("CCO\tname1\n\tCCN\tname2\n")

    def test_trailing_tab_keeps_empty_last_field(self) -> None:
        compounds = parse_screening_compounds_file("CCO\tname1\t\n")
        self.assertEqual((compounds[0]["smiles"], compounds[0]["name"]), ("CCO", "name1"))

    def test_non_diacritic_combining_marks_slug_like_ts(self) -> None:
        compounds = parse_screening_compounds_file(">a\u064Bb\nCCO\n")
        self.assertEqual(compounds[0]["id"], "a-b")

    def test_crlf_input_is_normalized(self) -> None:
        compounds = parse_screening_compounds_file(">aspirin\r\nCCO\r\n")
        self.assertEqual((compounds[0]["name"], compounds[0]["smiles"]), ("aspirin", "CCO"))

    def test_csv_row_with_missing_smiles_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "SMILES is missing"):
            parse_screening_compounds_file("smiles,name\n,ethanol\n")


class ScreeningLibraryMergeTests(unittest.TestCase):
    def test_merges_library_into_target_only_yaml(self) -> None:
        merged = yaml.safe_load(merge_screening_compounds_file_into_yaml(TARGET_ONLY_YAML, FASTA_LIBRARY))
        self.assertEqual(
            [c["name"] for c in merged["virtual_screening"]["compounds"]],
            ["aspirin", "caffeine"],
        )
        self.assertEqual(merged["sequences"][0]["protein"]["sequence"], "ACDEFGHIK")

    def test_preserves_existing_screening_name(self) -> None:
        yaml_with_name = TARGET_ONLY_YAML + "virtual_screening:\n  name: kinase panel\n"
        merged = yaml.safe_load(merge_screening_compounds_file_into_yaml(yaml_with_name, SMI_LIBRARY))
        self.assertEqual(merged["virtual_screening"]["name"], "kinase panel")
        self.assertEqual(len(merged["virtual_screening"]["compounds"]), 2)

    def test_rejects_inline_and_file_library_together(self) -> None:
        inline = TARGET_ONLY_YAML + "virtual_screening:\n  compounds:\n    - smiles: CCO\n"
        with self.assertRaisesRegex(ValueError, "not both"):
            merge_screening_compounds_file_into_yaml(inline, SMI_LIBRARY)

    def test_rejects_invalid_yaml(self) -> None:
        with self.assertRaisesRegex(ValueError, "not valid YAML"):
            merge_screening_compounds_file_into_yaml("version: [", SMI_LIBRARY)

    def test_propagates_parse_errors(self) -> None:
        with self.assertRaisesRegex(ValueError, "no compound records"):
            merge_screening_compounds_file_into_yaml(TARGET_ONLY_YAML, "")


class CompoundsFilePredictRouteTests(unittest.TestCase):
    def _post_vs(self, app: Flask, yaml_content: str, library: str | None):
        data = {
            "backend": "nesso",
            "workflow": "virtual_screening",
            "yaml_file": (io.BytesIO(yaml_content.encode("utf-8")), "input.yaml"),
        }
        if library is not None:
            data["compounds_file"] = (io.BytesIO(library.encode("utf-8")), "library.smi")
        return app.test_client().post("/predict", data=data, content_type="multipart/form-data")

    def test_merges_compounds_file_into_submitted_yaml(self) -> None:
        app, predict_task = _register_predict_app()
        response = self._post_vs(app, TARGET_ONLY_YAML, FASTA_LIBRARY)
        self.assertEqual(response.status_code, 202)
        submitted_args = predict_task.apply_async.call_args.kwargs["args"][0]
        submitted = yaml.safe_load(submitted_args["yaml_content"])
        self.assertEqual(
            [c["name"] for c in submitted["virtual_screening"]["compounds"]],
            ["aspirin", "caffeine"],
        )

    def test_rejects_compounds_file_for_other_workflows(self) -> None:
        app, _ = _register_predict_app()
        data = {
            "backend": "boltz",
            "workflow": "prediction",
            "use_msa_server": "false",
            "yaml_file": (io.BytesIO(TARGET_ONLY_YAML.encode("utf-8")), "input.yaml"),
            "compounds_file": (io.BytesIO(SMI_LIBRARY.encode("utf-8")), "library.smi"),
        }
        response = app.test_client().post("/predict", data=data, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 400)
        self.assertIn("only accepted for workflow=virtual_screening", response.get_json()["error"])

    def test_rejects_inline_plus_file_library(self) -> None:
        inline = TARGET_ONLY_YAML + "virtual_screening:\n  compounds:\n    - smiles: CCO\n"
        app, _ = _register_predict_app()
        response = self._post_vs(app, inline, SMI_LIBRARY)
        self.assertEqual(response.status_code, 400)
        self.assertIn("not both", response.get_json()["error"])

    def test_rejects_malformed_library(self) -> None:
        app, _ = _register_predict_app()
        response = self._post_vs(app, TARGET_ONLY_YAML, "# nothing here\n")
        self.assertEqual(response.status_code, 400)
        self.assertIn("no compound records", response.get_json()["error"])


class CompoundsFileSnapshotTests(unittest.TestCase):
    def test_snapshot_includes_file_library(self) -> None:
        from management_api.task_snapshot import TASK_INPUT_OPTIONS_KEY

        app = Flask(__name__)
        with app.test_request_context(
            "/predict",
            method="POST",
            data={
                "workflow": "virtual_screening",
                "yaml_file": (io.BytesIO(TARGET_ONLY_YAML.encode("utf-8")), "input.yaml"),
                "compounds_file": (io.BytesIO(FASTA_LIBRARY.encode("utf-8")), "library.smi"),
            },
            content_type="multipart/form-data",
        ):
            snapshot = build_prediction_task_snapshot_from_yaml(request, mock.Mock())

        options = snapshot["properties"][TASK_INPUT_OPTIONS_KEY]
        self.assertEqual(options["virtualScreening"]["compoundCount"], 2)
        self.assertIn(">aspirin", options["virtualScreeningInput"])
        self.assertIn(">caffeine", options["virtualScreeningInput"])

    def test_snapshot_survives_unparseable_library(self) -> None:
        app = Flask(__name__)
        with app.test_request_context(
            "/predict",
            method="POST",
            data={
                "workflow": "virtual_screening",
                "yaml_file": (io.BytesIO(TARGET_ONLY_YAML.encode("utf-8")), "input.yaml"),
                "compounds_file": (io.BytesIO(b"\xff\xfe garbage"), "library.smi"),
            },
            content_type="multipart/form-data",
        ):
            snapshot = build_prediction_task_snapshot_from_yaml(request, mock.Mock())
        self.assertEqual(snapshot["components"][0]["type"], "protein")
        self.assertNotIn("__vbio_input_options_v1", snapshot["properties"])


class ScreeningResultsRouteTests(unittest.TestCase):
    def _register_results_app(self, archive_path: str) -> Flask:
        app = Flask(__name__)
        register_task_routes(
            app,
            require_api_token=lambda handler: handler,
            celery_app=mock.Mock(),
            task_monitor=mock.Mock(),
            predict_task=mock.Mock(),
            config_module=SimpleNamespace(UPLOAD_FOLDER=tempfile.gettempdir()),
            logger=mock.Mock(),
            find_result_archive=lambda _task_id: archive_path,
            resolve_result_archive_path=lambda _task_id: ("results.zip", archive_path),
            build_or_get_view_archive=lambda *_a, **_k: archive_path,
            get_tracker_status=lambda _task_id: (None, None),
            get_compact_prediction_metrics=lambda _task_id: {},
            list_known_queues=lambda: [],
            get_worker_capability_snapshot=lambda: {},
        )
        return app

    def _write_archive(self, screening: dict | None) -> str:
        fd, path = tempfile.mkstemp(suffix=".zip")
        with os_fd_close(fd):
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("manifest.json", "{}")
                if screening is not None:
                    archive.writestr("nesso/screening.json", json.dumps(screening))
        return path

    def test_serves_ranked_screening_json(self) -> None:
        screening = {"compounds": [{"name": "best", "affinity_pred_value": -1.2}]}
        app = self._register_results_app(self._write_archive(screening))
        response = app.test_client().get("/results/task-1/screening")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["compounds"][0]["name"], "best")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_missing_screening_member_returns_404(self) -> None:
        app = self._register_results_app(self._write_archive(None))
        response = app.test_client().get("/results/task-1/screening")
        self.assertEqual(response.status_code, 404)
        self.assertIn("virtual-screening", response.get_json()["error"])

    def test_oversized_screening_member_returns_500(self) -> None:
        import sys as _sys
        from backend.routes.task import SCREENING_JSON_MAX_BYTES

        big = {"compounds": [{"name": "pad", "blob": "x" * (SCREENING_JSON_MAX_BYTES + 1)}]}
        fd, path = tempfile.mkstemp(suffix=".zip")
        with os_fd_close(fd):
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("nesso/screening.json", json.dumps(big))
        app = self._register_results_app(path)
        response = app.test_client().get("/results/task-1/screening")
        self.assertEqual(response.status_code, 500)
        self.assertIn("too large", response.get_json()["error"])

    def test_invalid_screening_payload_returns_500(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".zip")
        with os_fd_close(fd):
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("nesso/screening.json", "{\"compounds\": 3}")
        app = self._register_results_app(path)
        response = app.test_client().get("/results/task-1/screening")
        self.assertEqual(response.status_code, 500)


class os_fd_close:
    """contextlib.closing for raw file descriptors."""

    def __init__(self, fd: int):
        self.fd = fd

    def __enter__(self):
        return self.fd

    def __exit__(self, *exc):
        import os

        os.close(self.fd)
        return False


if __name__ == "__main__":
    unittest.main()
