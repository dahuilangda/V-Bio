"""REAL Flask integration tests: POST /predict through the actual app.

Exercises the genuine HTTP+route contract: real ``require_api_token`` (X-API-Token
header against backend.core.config.BOLTZ_API_TOKEN), real multipart handling
(the route requires a ``yaml_file`` FILE part), and the submission-time custom-CCD
dry-run rejection. Nothing here touches GPUs or the celery broker: only request
validation branches are driven; the success path that would enqueue GPU work is out
of scope for unit CI and covered by the workers' own suites.
"""

from __future__ import annotations

import io
import os
import sys

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.flask_real]

YAML = """sequences:
  - protein:
      id: A
      sequence: ETLVRPKPLLLKLLKSVGAQKDTYTMKEVLFYLGQYIMTKRLYDEKQQHIVYCSNDLLGDLFGVPSFSVKEHRKIYTMIYRNLV
"""


if os.getcwd() != "/data/V-Bio":
    os.chdir("/data/V-Bio")
sys.path.insert(0, "/data/V-Bio")


@pytest.fixture(scope="module")
def flask_app():
    from backend.app import app as _app  # noqa: E402

    _app.config["TESTING"] = True
    return _app


@pytest.fixture()
def api_token():
    """The exact secret the production decorator compares against."""
    from backend.core import config as backend_config  # noqa: E402

    return str(getattr(backend_config, "BOLTZ_API_TOKEN"))


def _post_predict(client, token: str, backend: str = "", workflow: str = "",
                  yaml_text: str | None = YAML, extra_form=None,
                  with_yaml_file: bool = True):
    data = {"workflow": workflow, "backend": backend}
    if extra_form:
        data.update(extra_form)
    if with_yaml_file:
        data["yaml_file"] = (io.BytesIO(yaml_text.encode("utf-8")), "input.yaml")
    return client.post(
        "/predict",
        data=data,
        headers={"X-API-Token": token},
        content_type="multipart/form-data",
        follow_redirects=False,
    )


class TestPredictRouteRealApp:
    def test_rejects_boltz2dock_for_prediction_workflow(self, flask_app, api_token):
        # Docking engines are whitelisted for peptide_design ONLY.
        with flask_app.test_client() as c:
            resp = _post_predict(c, api_token, backend="boltz2dock", workflow="prediction")
        assert resp.status_code == 400
        assert b"peptide_design" in resp.data

    def test_rejects_protenix2dock_for_prediction_workflow(self, flask_app, api_token):
        with flask_app.test_client() as c:
            resp = _post_predict(c, api_token, backend="protenix2dock", workflow="prediction")
        assert resp.status_code == 400
        assert b"peptide_design" in resp.data

    def test_missing_yaml_file_part(self, flask_app, api_token):
        with flask_app.test_client() as c:
            resp = _post_predict(c, api_token, workflow="prediction",
                                 backend="boltz", with_yaml_file=False)
        assert resp.status_code == 400
        assert b"yaml_file" in resp.data

    def test_rejects_disconnected_custom_ccd_smiles_with_named_code(self, flask_app, api_token):
        # Submission-time dry-run through the production CCD builders: dot-disconnected
        # SMILES is a chemical-component violation and must fail here (HTTP 400 naming
        # the CCD) instead of inside a GPU task later.
        custom_ccd_molecules = [
            {"ccd": "FRAG1", "smiles": "CC.CC", "kind": "residue",
             "baseResidue": "A"},
        ]
        with flask_app.test_client() as c:
            resp = _post_predict(
                c, api_token, backend="boltz2dock", workflow="peptide_design",
                extra_form={
                    "custom_ccd_molecules": __import__("json").dumps(custom_ccd_molecules),
                },
            )
        assert resp.status_code == 400
        assert b"Custom CCD rejected" in resp.data
        assert b"FRAG1" in resp.data

    def test_unauthenticated_request_is_forbidden(self, flask_app):
        with flask_app.test_client() as c:
            resp = c.post("/predict", data={"workflow": "prediction"},
                          content_type="multipart/form-data")
        assert resp.status_code == 403
