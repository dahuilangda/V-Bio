from __future__ import annotations

import io
import json
import os
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

from management_api.task_snapshot import (
    TASK_INPUT_OPTIONS_KEY,
    build_prediction_task_snapshot_from_yaml,
)
from backend.routes.prediction import register_prediction_routes
from backend.runtime import nesso_backend
from backend.services.common_utils import infer_use_msa_server_from_yaml_text, is_msa_disabled
from backend.scheduling.capability_router import (
    build_capability_queue,
    capability_from_prediction_backend,
)


VALID_SCREENING_INPUT = """\
version: 1
sequences:
  - protein:
      id: A
      sequence: ACDEFGHIK
virtual_screening:
  name: kinase panel
  compounds:
    - id: weak-hit
      name: Weak hit
      smiles: CCO
    - id: best-hit
      name: Best hit
      smiles: c1ccccc1
"""


def _register_test_app(
    *,
    msa_server_url: str = "",
) -> tuple[Flask, mock.Mock]:
    app = Flask(__name__)
    predict_task = mock.Mock()
    predict_task.apply_async.return_value = SimpleNamespace(id="task-1")
    register_prediction_routes(
        app,
        require_api_token=lambda handler: handler,
        logger=mock.Mock(),
        config_module=SimpleNamespace(MSA_SERVER_URL=msa_server_url),
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


class BoltzMsaPolicyTests(unittest.TestCase):
    EMPTY_MSA_INPUT = """\
version: 1
sequences:
  - protein:
      id: A
      sequence: AAAAAAA
      msa: empty
"""

    EXTERNAL_MSA_INPUT = """\
version: 1
sequences:
  - protein:
      id: A
      sequence: ACDEFGHIKLMNPQRSTVWY
"""

    def _post(
        self,
        app: Flask,
        yaml_content: str,
        *,
        backend: str,
        use_msa_server: bool = True,
    ) -> object:
        return app.test_client().post(
            "/predict",
            data={
                "backend": backend,
                "workflow": "prediction",
                "use_msa_server": str(use_msa_server).lower(),
                "yaml_file": (io.BytesIO(yaml_content.encode("utf-8")), "input.yaml"),
            },
            content_type="multipart/form-data",
        )

    def test_explicit_empty_msa_does_not_require_server(self) -> None:
        for backend in ("boltz", "alphafold3", "protenix"):
            with self.subTest(backend=backend):
                app, predict_task = _register_test_app(msa_server_url="")
                response = self._post(app, self.EMPTY_MSA_INPUT, backend=backend)

                self.assertEqual(response.status_code, 202)
                submitted_args = predict_task.apply_async.call_args.kwargs["args"][0]
                self.assertFalse(submitted_args["use_msa_server"])

    def test_protein_without_msa_still_requires_server(self) -> None:
        for backend in ("boltz", "alphafold3", "protenix"):
            with self.subTest(backend=backend):
                app, predict_task = _register_test_app(msa_server_url="")
                response = self._post(
                    app,
                    self.EXTERNAL_MSA_INPUT,
                    backend=backend,
                    use_msa_server=False,
                )

                self.assertEqual(response.status_code, 503)
                self.assertIn("MSA_SERVER_URL", response.get_json()["error"])
                predict_task.apply_async.assert_not_called()

    def test_protein_without_msa_forces_configured_server(self) -> None:
        for backend in ("boltz", "alphafold3", "protenix"):
            with self.subTest(backend=backend):
                app, predict_task = _register_test_app(msa_server_url="http://msa.test:8080")
                response = self._post(
                    app,
                    self.EXTERNAL_MSA_INPUT,
                    backend=backend,
                    use_msa_server=False,
                )

                self.assertEqual(response.status_code, 202)
                submitted_args = predict_task.apply_async.call_args.kwargs["args"][0]
                self.assertTrue(submitted_args["use_msa_server"])

    def test_msa_value_helpers_distinguish_empty_and_external_inputs(self) -> None:
        self.assertTrue(is_msa_disabled("empty"))
        self.assertTrue(is_msa_disabled(0))
        self.assertFalse(is_msa_disabled("/tmp/query.a3m"))
        self.assertFalse(infer_use_msa_server_from_yaml_text(self.EMPTY_MSA_INPUT))
        self.assertTrue(infer_use_msa_server_from_yaml_text(self.EXTERNAL_MSA_INPUT))


class NessoScreeningInputTests(unittest.TestCase):
    def test_normalizes_batch_deduplicates_smiles_and_resolves_safe_ids(self) -> None:
        source = """\
version: 1
sequences:
  - protein:
      id: target
      sequence: ACDEFG
virtual_screening:
  compounds:
    - id: Hit One
      name: First
      smiles: CCO
    - id: Hit@One
      name: Second
      smiles: CCN
    - id: duplicate
      name: Same ethanol
      smiles: OCC
"""
        prepared = nesso_backend.normalize_nesso_screening_input_yaml(source)

        self.assertEqual(prepared["target_chain_ids"], ["target"])
        self.assertEqual(prepared["submitted_compound_count"], 3)
        self.assertEqual([item["record_id"] for item in prepared["compounds"]], ["hit-one", "hit-one-2"])
        self.assertEqual([item["canonical_smiles"] for item in prepared["compounds"]], ["CCO", "CCN"])
        self.assertEqual(len(prepared["warnings"]), 1)
        self.assertIn("Skipped duplicate SMILES", prepared["warnings"][0])

    def test_rejects_invalid_screening_contracts(self) -> None:
        parsed = yaml.safe_load(VALID_SCREENING_INPUT)
        cases = {
            "missing compounds": {**parsed, "virtual_screening": {"compounds": []}},
            "invalid target sequence": {
                **parsed,
                "sequences": [{"protein": {"id": "A", "sequence": "ACD123"}}],
            },
            "invalid context ligand": {
                **parsed,
                "sequences": [
                    *parsed["sequences"],
                    {"ligand": {"id": "B", "smiles": "not a smiles"}},
                ],
            },
            "prediction properties": {**parsed, "properties": [{"affinity": {"binder": "L"}}]},
            "constraints": {**parsed, "constraints": [{"pocket": {}}]},
            "invalid smiles": {
                **parsed,
                "virtual_screening": {"compounds": [{"name": "bad", "smiles": "not a smiles"}]},
            },
            "too many compounds": {
                **parsed,
                "virtual_screening": {
                    "compounds": [{"name": f"c{index}", "smiles": "C" * (index + 1)} for index in range(201)]
                },
            },
        }

        for name, value in cases.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                nesso_backend.normalize_nesso_screening_input_yaml(
                    yaml.safe_dump(value, sort_keys=False)
                )

    def test_preserves_multi_protein_complex_and_context_ligands(self) -> None:
        source = """\
version: 1
sequences:
  - protein:
      id: [A, B]
      sequence: ACDEFG
  - protein:
      id: C
      sequence: HIKLMN
  - ligand:
      id: D
      smiles: CCO
  - ligand:
      id: [E, F]
      ccd: ATP
virtual_screening:
  compounds:
    - id: candidate
      smiles: CCN
"""

        prepared = nesso_backend.normalize_nesso_screening_input_yaml(source)

        self.assertEqual(prepared["target_chain_ids"], ["A", "B", "C"])
        self.assertEqual(prepared["context_ligand_chain_ids"], ["D", "E", "F"])
        self.assertEqual(prepared["ligand_chain_id"], "G")
        self.assertEqual(len(prepared["protein_entries"]), 2)
        self.assertEqual(len(prepared["context_ligand_entries"]), 2)
        self.assertEqual(len(prepared["complex_entries"]), 4)


class NessoPredictionRouteTests(unittest.TestCase):
    def _post(self, app: Flask, *, backend: str, workflow: str | None) -> object:
        data: dict[str, object] = {
            "backend": backend,
            "yaml_file": (io.BytesIO(VALID_SCREENING_INPUT.encode("utf-8")), "input.yaml"),
        }
        if workflow is not None:
            data["workflow"] = workflow
        return app.test_client().post("/predict", data=data, content_type="multipart/form-data")

    def test_rejects_nesso_as_an_ordinary_prediction_backend(self) -> None:
        app, predict_task = _register_test_app()
        response = self._post(app, backend="nesso", workflow="prediction")

        self.assertEqual(response.status_code, 400)
        self.assertIn("independent Virtual Screening backend", response.get_json()["error"])
        predict_task.apply_async.assert_not_called()

    def test_rejects_non_nesso_virtual_screening_backend(self) -> None:
        app, predict_task = _register_test_app()
        response = self._post(app, backend="boltz", workflow="virtual_screening")

        self.assertEqual(response.status_code, 400)
        self.assertIn("requires backend=nesso", response.get_json()["error"])
        predict_task.apply_async.assert_not_called()

    def test_dispatches_virtual_screening_to_its_independent_queue(self) -> None:
        app, predict_task = _register_test_app()
        response = self._post(app, backend="nesso-1", workflow="virtual_screening")

        self.assertEqual(response.status_code, 202)
        call = predict_task.apply_async.call_args
        submitted_args = call.kwargs["args"][0]
        self.assertEqual(call.kwargs["queue"], "cap.nesso.default")
        self.assertEqual(submitted_args["backend"], "nesso")
        self.assertEqual(submitted_args["workflow"], "virtual_screening")
        self.assertEqual(submitted_args["yaml_content"], VALID_SCREENING_INPUT)
        self.assertEqual(submitted_args["seed"], 42)
        self.assertFalse(submitted_args["use_msa_server"])


class NessoTaskSnapshotTests(unittest.TestCase):
    def test_preserves_screening_batch_for_direct_api_submissions(self) -> None:
        app = Flask(__name__)
        with app.test_request_context(
            "/predict",
            method="POST",
            data={
                "workflow": "virtual_screening",
                "yaml_file": (io.BytesIO(VALID_SCREENING_INPUT.encode("utf-8")), "input.yaml"),
            },
            content_type="multipart/form-data",
        ):
            snapshot = build_prediction_task_snapshot_from_yaml(request, mock.Mock())

        self.assertEqual(len(snapshot["components"]), 1)
        self.assertEqual(snapshot["components"][0]["type"], "protein")
        options = snapshot["properties"][TASK_INPUT_OPTIONS_KEY]
        self.assertEqual(options["virtualScreening"]["compoundCount"], 2)
        self.assertIn(">Weak hit\nCCO", options["virtualScreeningInput"])
        self.assertIn(">Best hit\nc1ccccc1", options["virtualScreeningInput"])


class NessoRuntimeTests(unittest.TestCase):
    def test_uses_its_own_capability_queues(self) -> None:
        self.assertEqual(capability_from_prediction_backend("nesso-1"), "nesso")
        self.assertEqual(build_capability_queue("nesso", "high"), "cap.nesso.high")
        self.assertEqual(build_capability_queue("nesso", "default"), "cap.nesso.default")

    def test_redacts_credentials_from_logged_docker_command(self) -> None:
        rendered = nesso_backend._format_docker_command_for_log(
            [
                "docker",
                "run",
                "--env",
                "HF_TOKEN=hf-secret",
                "-e",
                "HTTPS_PROXY=http://user:password@proxy:2080",
                "--env",
                "NESSO_CACHE=/cache",
                "--env=API_KEY=another-secret",
            ]
        )

        self.assertNotIn("hf-secret", rendered)
        self.assertNotIn("password", rendered)
        self.assertIn("'HF_TOKEN=***'", rendered)
        self.assertIn("'HTTPS_PROXY=***'", rendered)
        self.assertIn("NESSO_CACHE=/cache", rendered)
        self.assertNotIn("another-secret", rendered)

    def test_runs_one_batch_and_writes_ranked_affinity_only_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "result.zip"
            cache_path = root / "cache"
            docker_commands: list[list[str]] = []

            def fake_docker_run(command: list[str], log_path: Path) -> None:
                docker_commands.append(command)
                input_files = sorted((root / "nesso_runtime" / "inputs").glob("*.yaml"))
                self.assertEqual([path.stem for path in input_files], ["best-hit", "weak-hit"])
                for path in input_files:
                    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
                    self.assertEqual(len(parsed["sequences"]), 2)
                    self.assertEqual(parsed["properties"], [{"affinity": {"binder": "B"}}])
                    value = -2.0 if path.stem == "best-hit" else 1.0
                    affinity_path = root / "nesso_runtime" / "output" / "predictions" / path.stem / "affinity.json"
                    affinity_path.parent.mkdir(parents=True, exist_ok=True)
                    affinity_path.write_text(
                        json.dumps({
                            "affinity_pred_value": value,
                            "affinity_pred_value1": value - 0.1,
                            "affinity_pred_value2": value + 0.1,
                            "affinity_probability_binary": 0.9 if value < 0 else 0.2,
                            "entropy_crop_pl": 0.4,
                        }),
                        encoding="utf-8",
                    )
                log_path.write_text("batch completed\n", encoding="utf-8")

            with (
                mock.patch.object(nesso_backend, "NESSO_DOCKER_IMAGE", "test-nesso:latest"),
                mock.patch.object(nesso_backend, "NESSO_HOST_CACHE_DIR", str(cache_path)),
                mock.patch.object(nesso_backend, "NESSO_CONTAINER_CACHE_DIR", "/cache"),
                mock.patch.object(nesso_backend, "NESSO_MODEL_REVISION", "v1.0.0"),
                mock.patch.object(nesso_backend, "NESSO_NO_KERNELS", "true"),
                mock.patch.object(nesso_backend, "NESSO_NUM_WORKERS", 0),
                mock.patch.object(nesso_backend, "NESSO_RECYCLING_STEPS", 5),
                mock.patch.object(nesso_backend, "NESSO_PRECISION", "bf16-mixed"),
                mock.patch.object(nesso_backend.subprocess, "run"),
                mock.patch.object(nesso_backend, "_run_docker_command", side_effect=fake_docker_run),
                mock.patch.dict(os.environ, {"BOLTZ_ASSIGNED_GPU_ID": "2"}, clear=False),
            ):
                nesso_backend.run_nesso_backend(
                    temp_dir=str(root),
                    yaml_content=VALID_SCREENING_INPUT,
                    output_archive_path=str(archive_path),
                    seed=17,
                    task_id="test task",
                )

            self.assertEqual(len(docker_commands), 1)
            command = docker_commands[0]
            self.assertIn("test-nesso:latest", command)
            self.assertEqual(command[command.index("--gpus") + 1], "device=2")
            self.assertEqual(command[command.index("--seed") + 1], "17")
            predict_index = command.index("predict")
            self.assertEqual(command[predict_index + 1], "/workspace/task/inputs")
            self.assertIn("--require_affinity", command)
            self.assertIn("--no_kernels", command)
            self.assertEqual(command[command.index("--num_workers") + 1], "1")
            self.assertIn("HF_HUB_OFFLINE=1", command)
            self.assertIn("TRANSFORMERS_OFFLINE=1", command)
            self.assertIn(f"{cache_path}:/cache:ro", command)

            with zipfile.ZipFile(archive_path) as archive:
                names = set(archive.namelist())
                self.assertTrue({
                    "nesso/README.txt",
                    "nesso/affinity.json",
                    "nesso/screening.json",
                    "nesso/manifest.json",
                    "nesso/inputs/best-hit.yaml",
                    "nesso/inputs/weak-hit.yaml",
                    "nesso/output/predictions/best-hit/affinity.json",
                    "nesso/output/predictions/weak-hit/affinity.json",
                }.issubset(names))
                best = json.loads(archive.read("nesso/affinity.json"))
                screening = json.loads(archive.read("nesso/screening.json"))
                manifest = json.loads(archive.read("nesso/manifest.json"))

            self.assertEqual(best["id"], "best-hit")
            self.assertEqual(best["rank"], 1)
            self.assertAlmostEqual(best["ic50_um"], 0.01)
            self.assertAlmostEqual(best["pic50"], 8.0)
            self.assertAlmostEqual(best["ensemble_spread"], 0.2)
            self.assertEqual([row["id"] for row in screening["compounds"]], ["best-hit", "weak-hit"])
            self.assertEqual(screening["summary"]["best_compound_id"], "best-hit")
            self.assertFalse(screening["structure_available"])
            self.assertEqual(manifest["workflow"], "virtual_screening")
            self.assertEqual(manifest["compound_count"], 2)

    def test_fails_atomically_when_batch_outputs_are_missing_or_unexpected(self) -> None:
        for mode in ("missing", "unexpected"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive_path = root / "result.zip"
                cache_path = root / "cache"

                def fake_docker_run(_command: list[str], _log_path: Path) -> None:
                    record_ids = ["weak-hit"] if mode == "missing" else ["weak-hit", "best-hit", "stray"]
                    for record_id in record_ids:
                        affinity_path = root / "nesso_runtime" / "output" / "predictions" / record_id / "affinity.json"
                        affinity_path.parent.mkdir(parents=True, exist_ok=True)
                        affinity_path.write_text(json.dumps({"affinity_pred_value": 0.0}), encoding="utf-8")

                with (
                    mock.patch.object(nesso_backend, "NESSO_DOCKER_IMAGE", "test-nesso:latest"),
                    mock.patch.object(nesso_backend, "NESSO_HOST_CACHE_DIR", str(cache_path)),
                    mock.patch.object(nesso_backend, "_run_docker_command", side_effect=fake_docker_run),
                    mock.patch.object(nesso_backend.subprocess, "run"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "does not match submitted compounds"):
                        nesso_backend.run_nesso_backend(
                            temp_dir=str(root),
                            yaml_content=VALID_SCREENING_INPUT,
                            output_archive_path=str(archive_path),
                        )

                self.assertFalse(archive_path.exists())

    def test_rejects_non_finite_optional_metrics_before_writing_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "result.zip"
            cache_path = root / "cache"

            def fake_docker_run(_command: list[str], _log_path: Path) -> None:
                for record_id in ("weak-hit", "best-hit"):
                    affinity_path = root / "nesso_runtime" / "output" / "predictions" / record_id / "affinity.json"
                    affinity_path.parent.mkdir(parents=True, exist_ok=True)
                    affinity_path.write_text(
                        json.dumps({
                            "affinity_pred_value": 0.0,
                            "entropy_crop_pl": float("nan") if record_id == "weak-hit" else 0.2,
                        }),
                        encoding="utf-8",
                    )

            with (
                mock.patch.object(nesso_backend, "NESSO_DOCKER_IMAGE", "test-nesso:latest"),
                mock.patch.object(nesso_backend, "NESSO_HOST_CACHE_DIR", str(cache_path)),
                mock.patch.object(nesso_backend, "_run_docker_command", side_effect=fake_docker_run),
                mock.patch.object(nesso_backend.subprocess, "run"),
            ):
                with self.assertRaisesRegex(RuntimeError, "non-finite"):
                    nesso_backend.run_nesso_backend(
                        temp_dir=str(root),
                        yaml_content=VALID_SCREENING_INPUT,
                        output_archive_path=str(archive_path),
                    )

            self.assertFalse(archive_path.exists())


if __name__ == "__main__":
    unittest.main()
