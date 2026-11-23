from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml
from flask import Flask

from backend.core import config
from backend.core.errors import UserInputError
from backend.runtime.backends import (
    is_structure_backend,
    resolve_ccd_before_gpu,
    structure_backend,
)
from backend.runtime.backends.base import StructureBackend
from backend.runtime.backends.protenix import ProtenixBackend
from backend.runtime.backends.request import PredictionRequest
from backend.runtime.ccd import (
    CcdCatalog,
    ImageCcdCatalog,
    MmcifCcdCatalog,
    apply_custom_ccd_code_remap,
)

ENGINES = ("boltz", "alphafold3", "protenix", "mol1", "esmfold2")
D_ARG = {"ccd": "DRG", "smiles": "N[C@H](CCCNC(N)=N)C(=O)O", "kind": "residue", "base_residue": "R"}
GLA = {"ccd": "GLA", "smiles": "N[C@@H](CC(C(=O)O)C(=O)O)C(=O)O", "kind": "residue", "base_residue": "E"}
AMIDE_FLAGGED_AS_ACID = {"ccd": "DRG", "smiles": "N[C@H](CCCNC(N)=N)C(=O)N", "kind": "residue"}


class StubCatalog(CcdCatalog):
    def __init__(self, codes) -> None:
        super().__init__()
        self.name = "stub"
        self.given = frozenset(codes)
        self.reads = 0

    def read(self) -> frozenset:
        self.reads += 1
        return self.given


class Engine(StructureBackend):
    name = "mol1"

    def __init__(self, catalog: CcdCatalog) -> None:
        self.catalog = catalog

    def ccd_catalog(self) -> CcdCatalog:
        return self.catalog

    def execute(self, request, ctx) -> None:
        raise AssertionError("execute 不应被调用")


class InjectingEngine(Engine):
    injects_referenced_presets = True


def request_with(*modifications: tuple[int, str], ligand: str | None = None) -> PredictionRequest:
    sequences = [{
        "protein": {
            "id": "C",
            "sequence": "IHATCPAEAWAWINRK",
            "modifications": [{"position": position, "ccd": ccd} for position, ccd in modifications],
        },
    }]
    if ligand:
        sequences.append({"ligand": {"id": "L", "ccd": ligand}})
    return PredictionRequest.from_yaml(yaml.safe_dump({"sequences": sequences}))


class CatalogTests(unittest.TestCase):
    def test_reserve_renames_standard_collisions_around_taken_slots(self):
        catalog = StubCatalog({"ALA", "GLY", "Z003"})
        molecules = [{"ccd": "Z001"}, {"ccd": "ALA"}, {"ccd": "GLA"}, {"ccd": "GLY"}]
        self.assertEqual(catalog.rename_collisions(molecules), {"ALA": "Z002", "GLY": "Z004"})
        self.assertEqual([m["ccd"] for m in molecules], ["Z001", "Z002", "GLA", "Z004"])

    def test_unresolved_and_empty_dictionary(self):
        self.assertEqual(StubCatalog({"ACE"}).missing(["ACE", "ZAC"]), ["ZAC"])
        with self.assertRaises(RuntimeError):
            StubCatalog(set()).codes()

    def test_mmcif_catalog_tracks_file_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "components.cif"
            path.write_text("data_AAA\n loop_\n data_INDENTED\n")
            self.assertEqual(MmcifCcdCatalog("t", path).codes(), frozenset({"AAA"}))
            path.write_text("data_AAA\ndata_BBB\n")
            self.assertEqual(MmcifCcdCatalog("t", path).codes(), frozenset({"AAA", "BBB"}))
        with self.assertRaises(FileNotFoundError):
            MmcifCcdCatalog("t", "/nonexistent/components.cif").codes()

    def test_image_catalog_reads_inside_the_engine_image(self):
        output = SimpleNamespace(returncode=0, stderr="", stdout="Loading CCD\n__VBIO_CCD_CODES__2\nALA\nARG\n")
        with mock.patch("backend.runtime.ccd.subprocess.run", return_value=output) as run, \
                mock.patch("backend.worker.docker_cmd.ensure_image") as ensure:
            catalog = ImageCcdCatalog("boltz", "img:1", "python", "codes = ['ala', 'ARG']",
                                      volumes=[("/host", "/cache")], env={"K": "V"})
            self.assertEqual(catalog.codes(), frozenset({"ALA", "ARG"}))
        ensure.assert_called_once_with("img:1")
        command = run.call_args.args[0]
        self.assertEqual(command[:5], ["docker", "run", "--rm", "--entrypoint", "python"])
        self.assertIn("/host:/cache:ro", command)
        self.assertIn("K=V", command)
        self.assertEqual(command[-3:], ["img:1", "-c", catalog.script])
        run.assert_called_once()

    def test_image_catalog_surfaces_reader_failures(self):
        failing = SimpleNamespace(returncode=1, stderr="boom", stdout="")
        truncated = SimpleNamespace(returncode=0, stderr="", stdout="__VBIO_CCD_CODES__3\nALA\n")
        for output in (failing, truncated):
            with self.subTest(returncode=output.returncode), \
                    mock.patch("backend.runtime.ccd.subprocess.run", return_value=output), \
                    mock.patch("backend.worker.docker_cmd.ensure_image"):
                with self.assertRaises(RuntimeError):
                    ImageCcdCatalog("boltz", "img", "python", "codes = ['ALA']").codes()


class PlanTests(unittest.TestCase):
    def test_prepare_rejects_codes_the_engine_cannot_spell(self):
        structure_backend("mol1").check_ccd_codes(["NME_TBA"])
        with self.assertRaises(UserInputError):
            structure_backend("protenix").check_ccd_codes(["NME_TBA"])
        with self.assertRaises(UserInputError):
            structure_backend("protenix2dock").validate_ccd_input(request_with((1, "NME_TBA")), [])

    def test_prepare_rejects_stacked_modifications_for_every_engine(self):
        request = request_with((1, "ACE"), (1, "DRG"))
        for name in ENGINES:
            with self.subTest(name), self.assertRaises(UserInputError):
                structure_backend(name).validate_ccd_input(request, [])

    def test_prepare_builds_molecules_for_every_engine(self):
        request = request_with((3, "DRG"))
        for name in ENGINES:
            with self.subTest(name):
                structure_backend(name).validate_ccd_input(request, [D_ARG])
                with self.assertRaisesRegex(ValueError, "terminal carboxyl"):
                    structure_backend(name).validate_ccd_input(request, [AMIDE_FLAGGED_AS_ACID])

    def test_residue_parent_is_required_wherever_ccd_blocks_are_serialized(self):
        orphan = {key: value for key, value in D_ARG.items() if key != "base_residue"}
        for name in ENGINES:
            with self.subTest(name):
                if name == "esmfold2":
                    structure_backend(name).validate_ccd_input(request_with((3, "DRG")), [orphan])
                    continue
                with self.assertRaisesRegex(ValueError, "requires a valid base_residue"):
                    structure_backend(name).validate_ccd_input(request_with((3, "DRG")), [orphan])

    def test_resolve_reports_codes_the_engine_cannot_resolve(self):
        engine = Engine(StubCatalog({"ACE"}))
        request = request_with((1, "ACE"), (3, "ZAC"))
        with self.assertRaisesRegex(UserInputError, "ZAC"):
            engine.resolve_ccd_codes(request, engine.validate_ccd_input(request, []))
        resolution = engine.resolve_ccd_codes(request, engine.validate_ccd_input(request, [dict(D_ARG, ccd="ZAC")]))
        self.assertEqual([m["ccd"] for m in resolution.molecules], ["ZAC"])

    def test_resolve_renames_collisions_and_rewrites_references(self):
        engine = Engine(StubCatalog({"ACE", "GLA"}))
        request = request_with((1, "ACE"), (3, "GLA"), ligand="GLA")
        resolution = engine.resolve_ccd_codes(request, engine.validate_ccd_input(request, [GLA]))
        self.assertEqual(resolution.remap, {"GLA": "Z001"})
        self.assertEqual(resolution.molecules[0]["ccd"], "Z001")
        rewritten = PredictionRequest.from_yaml(
            apply_custom_ccd_code_remap(request.source_yaml, resolution.remap))
        self.assertEqual(rewritten.entities[0].modifications[1].ccd, "Z001")
        self.assertEqual(rewritten.entities[1].ccd, ["Z001"])

    def test_resolve_accepts_codes_defined_by_the_platform(self):
        engine = Engine(StubCatalog({"ACE"}))
        request = request_with((4, "NLE"), ligand="SEZ")
        engine.resolve_ccd_codes(request, engine.validate_ccd_input(request, []))

    def test_resolve_skips_the_catalog_without_ccd_references(self):
        catalog = StubCatalog({"ACE"})
        engine = Engine(catalog)
        request = PredictionRequest.from_yaml("sequences:\n  - protein:\n      id: A\n      sequence: GAG\n")
        engine.resolve_ccd_codes(request, engine.validate_ccd_input(request, []))
        self.assertEqual(catalog.reads, 0)

    def test_engines_needing_preset_definitions_get_them_injected(self):
        engine = InjectingEngine(StubCatalog({"NLE"}))
        request = request_with((4, "NLE"))
        resolution = engine.resolve_ccd_codes(request, engine.validate_ccd_input(request, []))
        self.assertEqual(resolution.remap, {"NLE": "Z001"})
        self.assertEqual(resolution.molecules[0]["smiles"], "N[C@@H](CCCCC)C(=O)O")

    def test_resolution_reuses_the_result_computed_before_gpu_allocation(self):
        catalog = StubCatalog({"GLA"})
        engine = Engine(catalog)
        request = request_with((3, "GLA"))
        bound = {"molecules": [{"ccd": "Z007"}], "remap": {"GLA": "Z007"}}
        resolution = engine.ccd_resolution(request, {"custom_ccd_molecules": [GLA], "ccd_resolution": bound})
        self.assertEqual((resolution.molecules, resolution.remap), ([{"ccd": "Z007"}], {"GLA": "Z007"}))
        self.assertEqual(catalog.reads, 0)
        self.assertEqual(engine.ccd_resolution(request, {"custom_ccd_molecules": []}).remap, {})
        self.assertEqual(catalog.reads, 1)


class RegistryTests(unittest.TestCase):
    def test_each_engine_declares_its_own_catalog_source(self):
        protenix = structure_backend("protenix").ccd_catalog()
        self.assertIsInstance(protenix, MmcifCcdCatalog)
        self.assertEqual(protenix.path, Path(config.PROTENIX_COMMON_CACHE_DIR) / "components.cif")

        settings = {
            "BOLTZ2_HOST_CACHE_DIR": "/host/boltz", "MOL1_COMMON_DIR": "/host/mol1/common/common",
            "RESULTS_BASE_DIR": "/data/results",
        }
        with mock.patch.multiple(config, **settings):
            catalogs = {name: structure_backend(name).ccd_catalog() for name in
                        ("boltz", "alphafold3", "mol1", "esmfold2")}
        for name, catalog in catalogs.items():
            self.assertIsInstance(catalog, ImageCcdCatalog, name)
            self.assertEqual(catalog.image, {"boltz": config.BOLTZ2_DOCKER_IMAGE, "alphafold3": config.ALPHAFOLD3_DOCKER_IMAGE,
                                             "mol1": config.MOL1_DOCKER_IMAGE, "esmfold2": config.ESMFOLD2_DOCKER_IMAGE}[name])
        self.assertIn("/root/.boltz/ccd.pkl", catalogs["boltz"].script)
        self.assertEqual(catalogs["boltz"].volumes, (("/host/boltz", "/root/.boltz"),))
        self.assertEqual(catalogs["alphafold3"].volumes, ())
        self.assertEqual(catalogs["mol1"].volumes, (("/host/mol1/common/common", "/cache/common"),))
        self.assertEqual(catalogs["esmfold2"].volumes, (("/data/esmfold2/hf_cache", "/root/.cache/huggingface"),))
        self.assertEqual(catalogs["esmfold2"].env, {"HF_HOME": "/root/.cache/huggingface"})

        del settings["BOLTZ2_HOST_CACHE_DIR"]
        with mock.patch.object(config, "BOLTZ2_HOST_CACHE_DIR", ""), self.assertRaises(RuntimeError):
            structure_backend("boltz").ccd_catalog()

    def test_task_entry_resolves_before_gpu_and_skips_other_backends(self):
        self.assertTrue(is_structure_backend("boltz2dock"))
        self.assertFalse(is_structure_backend("nesso"))
        self.assertIsNone(resolve_ccd_before_gpu("nesso", "sequences: []", []))
        with mock.patch.object(ProtenixBackend, "ccd_catalog", return_value=StubCatalog({"FME"})):
            with self.assertRaisesRegex(UserInputError, "ZAC"):
                resolve_ccd_before_gpu("protenix2dock", request_with((3, "ZAC")).source_yaml, [])
            resolution = resolve_ccd_before_gpu("protenix2dock", request_with((3, "FME")).source_yaml, [])
        self.assertEqual(resolution, {"molecules": [], "remap": {}})


FRONTEND_SERVER_ROOT = Path(__file__).resolve().parents[2] / "frontend" / "server"
if str(FRONTEND_SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(FRONTEND_SERVER_ROOT))

from backend.routes.prediction import register_prediction_routes  # noqa: E402
from backend.scheduling.capability_router import capability_from_prediction_backend  # noqa: E402
from backend.services.common_utils import infer_use_msa_server_from_yaml_text  # noqa: E402


def _register_predict_app(offline: tuple[str, ...] = ()) -> tuple[Flask, mock.Mock]:
    app = Flask(__name__)
    predict_task = mock.Mock()
    predict_task.apply_async.return_value = SimpleNamespace(id="task-1")
    register_prediction_routes(
        app,
        require_api_token=lambda handler: handler,
        logger=mock.Mock(),
        config_module=SimpleNamespace(MSA_SERVER_URL="http://msa", TENANT_MAX_DAILY=0),
        predict_task=predict_task,
        parse_bool=lambda value, default: default if value is None else str(value).lower() == "true",
        parse_int=lambda value, default: default if value is None else int(value),
        infer_use_msa_server_from_yaml_text=infer_use_msa_server_from_yaml_text,
        extract_template_meta_from_yaml=lambda _source: {},
        normalize_chain_id_list=lambda _value: [],
        select_queue_for_capability=lambda capability, priority: {
            "online": capability not in offline, "queue": f"cap.{capability}.{priority}"},
        capability_from_prediction_backend=capability_from_prediction_backend,
    )
    return app, predict_task


def _post(app: Flask, backend: str, modifications: list[dict], molecules: list[dict]):
    sequences = [{"protein": {"id": "A", "sequence": "IHATCPAEAWAWINRK", "modifications": modifications}}]
    data = {
        "backend": backend,
        "workflow": "prediction",
        "use_msa_server": "false",
        "yaml_file": (io.BytesIO(yaml.safe_dump({"sequences": sequences}).encode()), "input.yaml"),
        "custom_ccd_molecules": json.dumps(molecules),
    }
    return app.test_client().post("/predict", data=data, content_type="multipart/form-data")


class PredictRouteCcdTests(unittest.TestCase):
    def test_ccd_payload_is_accepted_for_every_engine(self):
        app, predict_task = _register_predict_app()
        for name in ENGINES:
            with self.subTest(name):
                response = _post(app, name, [{"position": 3, "ccd": "DRG"}], [D_ARG])
                self.assertEqual(response.status_code, 202, response.get_json())
        submitted = predict_task.apply_async.call_args.kwargs["args"][0]
        self.assertEqual(
            [(m["ccd"], m["smiles"], m["base_residue"]) for m in submitted["custom_ccd_molecules"]],
            [("DRG", D_ARG["smiles"], "R")])

    def test_submit_side_never_reads_engine_dictionaries(self):
        app, _ = _register_predict_app()
        with mock.patch.object(MmcifCcdCatalog, "read", side_effect=FileNotFoundError), \
                mock.patch.object(ImageCcdCatalog, "read", side_effect=FileNotFoundError):
            response = _post(app, "protenix", [{"position": 3, "ccd": "DRG"}], [D_ARG])
        self.assertEqual(response.status_code, 202)

    def test_submit_side_rejects_bad_ccd_input(self):
        app, _ = _register_predict_app()
        orphan = {key: value for key, value in D_ARG.items() if key != "base_residue"}
        cases = [
            ("boltz", [{"position": 1, "ccd": "ACE"}, {"position": 1, "ccd": "DRG"}], [D_ARG]),
            ("esmfold2", [{"position": 3, "ccd": "DRG"}], [AMIDE_FLAGGED_AS_ACID]),
            ("mol1", [{"position": 3, "ccd": "DRG"}], [AMIDE_FLAGGED_AS_ACID]),
            ("protenix", [{"position": 3, "ccd": "DRG"}], [orphan]),
            ("protenix", [{"position": 3, "ccd": "NME_TBA"}], [dict(D_ARG, ccd="NME_TBA")]),
        ]
        for backend, modifications, molecules in cases:
            with self.subTest(backend=backend, modifications=modifications):
                response = _post(app, backend, modifications, molecules)
                self.assertEqual(response.status_code, 400, response.get_json())
                self.assertEqual(response.get_json()["backend"], backend)


if __name__ == "__main__":
    unittest.main()


def _post_design(app, backend, options):
    data = {
        "backend": backend,
        "workflow": "peptide_design",
        "use_msa_server": "false",
        "yaml_file": (io.BytesIO(yaml.safe_dump({"sequences": [
            {"protein": {"id": "A", "sequence": "IHATCPAEAWAWINRK"}}]}).encode()), "input.yaml"),
        "peptide_design_options": json.dumps(options),
    }
    return app.test_client().post("/predict", data=data, content_type="multipart/form-data")


class PredictRouteDesignQueueTests(unittest.TestCase):
    def test_candidate_scoring_follows_the_scoring_backend(self):
        app, predict_task = _register_predict_app()
        response = _post_design(app, "protenix2dock", {"peptideDesignMode": "linear"})
        self.assertEqual(response.status_code, 202, response.get_json())
        submitted = predict_task.apply_async.call_args.kwargs["args"][0]
        self.assertEqual(submitted["peptide_subtask_queue"], "cap.protenix2dock.default")

    def test_rfd4_scoring_follows_the_scoring_backend_not_the_generator(self):
        app, predict_task = _register_predict_app()
        options = {"peptideProposerBackend": "rfd4", "peptideScorerBackend": "mol1"}
        response = _post_design(app, "rfd4", options)
        self.assertEqual(response.status_code, 202, response.get_json())
        self.assertEqual(predict_task.apply_async.call_args.kwargs["queue"], "cap.peptide_design.default")
        submitted = predict_task.apply_async.call_args.kwargs["args"][0]
        self.assertEqual(submitted["peptide_subtask_queue"], "cap.mol1.default")

    def test_rfd4_design_needs_both_the_generator_and_the_scorer_online(self):
        app, _ = _register_predict_app(offline=("mol1",))
        response = _post_design(app, "rfd4", {"peptideProposerBackend": "rfd4", "peptideScorerBackend": "mol1"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["capability"], "mol1")


    def test_rfd4_scoring_defaults_to_the_task_backend(self):
        app, predict_task = _register_predict_app()
        response = _post_design(app, "mol1", {"peptideProposerBackend": "rfd4"})
        self.assertEqual(response.status_code, 202, response.get_json())
        submitted = predict_task.apply_async.call_args.kwargs["args"][0]
        self.assertEqual(submitted["peptide_subtask_queue"], "cap.mol1.default")
