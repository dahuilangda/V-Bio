#!/usr/bin/env python3
"""VBio management API gateway.

This service keeps VBio API-token/project authorization in the VBio layer,
then proxies runtime calls to the original Boltz-WebUI backend unchanged.
"""

from __future__ import annotations

import logging
import os
from typing import Tuple

from flask import Flask, Response, jsonify
from management_api.auth_service import AuthService
from management_api.gateway_handlers import GatewayHandlers
from management_api.http_session import create_pooled_session
from management_api.lead_opt_overlay import LeadOptOverlayService
from management_api.lead_opt_routes import register_lead_opt_routes
from management_api.postgrest_client import PostgrestClient
from management_api.runtime_proxy import RuntimeProxy
from management_api.task_store import ProjectTaskStore
from management_api.usage_tracker import UsageTracker

LOG_LEVEL = os.environ.get("VBIO_MGMT_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("vbio-management-api")

VBIO_POSTGREST_URL = os.environ.get("VBIO_POSTGREST_URL", "http://127.0.0.1:54321").rstrip("/")
VBIO_POSTGREST_APIKEY = os.environ.get("VBIO_POSTGREST_APIKEY", "").strip()
VBIO_POSTGREST_TIMEOUT_SECONDS = float(os.environ.get("VBIO_POSTGREST_TIMEOUT_SECONDS", "8"))

RUNTIME_API_BASE_URL = os.environ.get("VBIO_RUNTIME_API_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
RUNTIME_API_TOKEN = (
    os.environ.get("VBIO_RUNTIME_API_TOKEN", "").strip() or os.environ.get("BOLTZ_API_TOKEN", "").strip()
)
RUNTIME_TIMEOUT_SECONDS = float(os.environ.get("VBIO_RUNTIME_TIMEOUT_SECONDS", "180"))
RUNTIME_HTTP_POOL_SIZE = int(os.environ.get("VBIO_RUNTIME_HTTP_POOL_SIZE", "64"))
POSTGREST_HTTP_POOL_SIZE = int(os.environ.get("VBIO_POSTGREST_HTTP_POOL_SIZE", "32"))
RUNTIME_MAX_INFLIGHT_REQUESTS = int(os.environ.get("VBIO_RUNTIME_MAX_INFLIGHT_REQUESTS", "128"))
RUNTIME_STATUS_HISTORY_SIZE = int(os.environ.get("VBIO_RUNTIME_STATUS_HISTORY_SIZE", "200"))

SERVER_HOST = os.environ.get("VBIO_MGMT_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("VBIO_MGMT_PORT", "5055"))
LEAD_OPT_OVERLAY_MAX_WORKERS = int(os.environ.get("VBIO_LEAD_OPT_OVERLAY_MAX_WORKERS", "4"))
LEAD_OPT_OVERLAY_MAX_PENDING = int(os.environ.get("VBIO_LEAD_OPT_OVERLAY_MAX_PENDING", "32"))
LEAD_OPT_OVERLAY_CACHE_SIZE = int(os.environ.get("VBIO_LEAD_OPT_OVERLAY_CACHE_SIZE", "256"))
LEAD_OPT_OVERLAY_CACHE_TTL_SECONDS = float(os.environ.get("VBIO_LEAD_OPT_OVERLAY_CACHE_TTL_SECONDS", "300"))
LEAD_OPT_OVERLAY_TIMEOUT_SECONDS = float(os.environ.get("VBIO_LEAD_OPT_OVERLAY_TIMEOUT_SECONDS", "8"))

FORM_FIELDS_INTERNAL = {"project_id", "task_name", "task_summary", "operation_mode"}
DEFAULT_PROTENIX_PREDICT_SEED = 42

app = Flask(__name__)

runtime_http = create_pooled_session(
    pool_connections=max(8, RUNTIME_HTTP_POOL_SIZE),
    pool_maxsize=max(8, RUNTIME_HTTP_POOL_SIZE),
)
postgrest_http = create_pooled_session(
    pool_connections=max(4, POSTGREST_HTTP_POOL_SIZE),
    pool_maxsize=max(4, POSTGREST_HTTP_POOL_SIZE),
)

postgrest_client = PostgrestClient(
    base_url=VBIO_POSTGREST_URL,
    apikey=VBIO_POSTGREST_APIKEY,
    timeout_seconds=VBIO_POSTGREST_TIMEOUT_SECONDS,
    session=postgrest_http,
)
auth_service = AuthService(postgrest_client)
usage_tracker = UsageTracker(postgrest_client, logger)
task_store = ProjectTaskStore(postgrest_client)
runtime_proxy = RuntimeProxy(
    runtime_api_base_url=RUNTIME_API_BASE_URL,
    runtime_api_token=RUNTIME_API_TOKEN,
    runtime_timeout_seconds=RUNTIME_TIMEOUT_SECONDS,
    session=runtime_http,
    logger=logger,
    form_fields_internal=FORM_FIELDS_INTERNAL,
    default_protenix_predict_seed=DEFAULT_PROTENIX_PREDICT_SEED,
    max_inflight_requests=RUNTIME_MAX_INFLIGHT_REQUESTS,
    status_history_size=RUNTIME_STATUS_HISTORY_SIZE,
)
lead_opt_overlay_service = LeadOptOverlayService(
    max_workers=LEAD_OPT_OVERLAY_MAX_WORKERS,
    max_pending=LEAD_OPT_OVERLAY_MAX_PENDING,
    cache_size=LEAD_OPT_OVERLAY_CACHE_SIZE,
    cache_ttl_seconds=LEAD_OPT_OVERLAY_CACHE_TTL_SECONDS,
    task_timeout_seconds=LEAD_OPT_OVERLAY_TIMEOUT_SECONDS,
)

gateway = GatewayHandlers(
    auth_service=auth_service,
    usage_tracker=usage_tracker,
    runtime_proxy=runtime_proxy,
    task_store=task_store,
    lead_opt_overlay_service=lead_opt_overlay_service,
    logger=logger,
    default_protenix_predict_seed=DEFAULT_PROTENIX_PREDICT_SEED,
)


@app.get("/vbio-api/healthz")
def healthz() -> Tuple[Response, int]:
    return jsonify(
        {
            "ok": True,
            "runtime_api_base_url": RUNTIME_API_BASE_URL,
            "postgrest_url": VBIO_POSTGREST_URL,
        }
    ), 200


@app.get("/vbio-api/runtime_status")
def runtime_status() -> Tuple[Response, int]:
    return jsonify(
        {
            "ok": True,
            "runtime_proxy": runtime_proxy.get_runtime_status(),
            "overlay_service": lead_opt_overlay_service.get_status(),
        }
    ), 200


@app.post("/vbio-api/predict")
def submit_predict() -> Tuple[Response, int]:
    return gateway.forward_submit("/predict", "submit_predict")


@app.post("/vbio-api/api/boltz2score")
def submit_boltz2score() -> Tuple[Response, int]:
    return gateway.forward_submit("/api/boltz2score", "submit_boltz2score")


@app.post("/vbio-api/api/lead_optimization/submit")
def submit_lead_optimization() -> Tuple[Response, int]:
    return (
        jsonify(
            {
                "error": (
                    "Legacy /api/lead_optimization/submit pipeline is disabled. "
                    "Use Lead Optimization MMP workflow APIs."
                )
            }
        ),
        410,
    )


@app.get("/vbio-api/status/<task_id>")
def get_status(task_id: str) -> Tuple[Response, int]:
    return gateway.forward_task_read(task_id, "/status", "read_status")


@app.get("/vbio-api/results/<task_id>")
def get_results(task_id: str) -> Tuple[Response, int]:
    return gateway.forward_task_read(task_id, "/results", "read_results")


@app.get("/vbio-api/results/<task_id>/view")
def get_results_view(task_id: str) -> Tuple[Response, int]:
    return gateway.forward_task_read(task_id, "/results", "read_results_view")


register_lead_opt_routes(
    app,
    forward_task_read=gateway.forward_task_read,
    forward_quick_json=gateway.forward_quick_json,
    forward_quick_multipart=gateway.forward_quick_multipart,
    forward_quick_get=gateway.forward_quick_get,
    pocket_overlay_handler=gateway.handle_lead_optimization_pocket_overlay,
)


@app.delete("/vbio-api/tasks/<task_id>")
def cancel_or_delete_task(task_id: str) -> Tuple[Response, int]:
    return gateway.cancel_or_delete_task(task_id)


if __name__ == "__main__":
    app.run(host=SERVER_HOST, port=SERVER_PORT, debug=False, threaded=True)
