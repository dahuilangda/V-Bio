#!/usr/bin/env python3
"""V-Bio management API gateway.

This service keeps V-Bio API-token/project authorization in the frontend layer,
then proxies runtime calls to the original V-Bio backend unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any, Dict, Tuple

from flask import Flask, Response, jsonify, request
from management_api.auth_service import AuthService
from management_api.gateway_handlers import GatewayHandlers
from management_api.http_session import create_pooled_session
from management_api.lead_opt_overlay import LeadOptOverlayService
from management_api.lead_opt_routes import register_lead_opt_routes
from management_api.postgrest_client import PostgrestClient
from management_api.runtime_proxy import RuntimeProxy
from management_api.jwt_clients import JwtClientStore
from management_api.jwt_auth import JwtTokenError, JwtUserService, decode_login_jwt
from management_api.task_store import ProjectTaskStore
from management_api.copilot import CopilotAssistant
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
COPILOT_API_URL = (
    os.environ.get("VBIO_COPILOT_API_URL", "").strip()
    or os.environ.get("VBIO_TASK_CHAT_API_URL", "").strip()
)
COPILOT_API_KEY = (
    os.environ.get("VBIO_COPILOT_API_KEY", "").strip()
    or os.environ.get("VBIO_TASK_CHAT_API_KEY", "").strip()
)
COPILOT_MODEL = (
    os.environ.get("VBIO_COPILOT_MODEL", "").strip()
    or os.environ.get("VBIO_TASK_CHAT_MODEL", "").strip()
)
COPILOT_ENABLED = os.environ.get("VBIO_COPILOT_ENABLED", "").strip().lower()
COPILOT_CONFIGURED = COPILOT_ENABLED not in {"0", "false", "no", "off"} and bool(COPILOT_API_URL)
COPILOT_TIMEOUT_SECONDS = float(os.environ.get("VBIO_COPILOT_TIMEOUT_SECONDS", "90"))
COPILOT_MAX_REQUEST_BYTES = int(os.environ.get("VBIO_COPILOT_MAX_REQUEST_BYTES", "524288"))

JWT_CLIENTS_FILE = os.environ.get("VBIO_JWT_CLIENTS_FILE", "frontend/.run/jwt_clients.json").strip()
SESSION_SECRET = os.environ.get("VBIO_SESSION_SECRET", "").strip() or RUNTIME_API_TOKEN
SUPER_ADMIN_USERNAMES = os.environ.get("VBIO_SUPER_ADMIN_USERNAMES", "").strip() or os.environ.get("VITE_SUPER_ADMIN_USERNAMES", "").strip()
SUPER_ADMIN_EMAILS = os.environ.get("VBIO_SUPER_ADMIN_EMAILS", "").strip() or os.environ.get("VITE_SUPER_ADMIN_EMAILS", "").strip()

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
jwt_user_service = JwtUserService(postgrest_client)
jwt_client_store = JwtClientStore(JWT_CLIENTS_FILE)
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
copilot_assistant = CopilotAssistant(
    chat_api_url=COPILOT_API_URL,
    chat_api_key=COPILOT_API_KEY,
    chat_model=COPILOT_MODEL,
    timeout_seconds=COPILOT_TIMEOUT_SECONDS,
    session=runtime_http,
    logger=logger,
)



def _parse_env_set(value: str) -> set[str]:
    return {item.strip().lower() for item in str(value or "").split(",") if item.strip()}


def _is_super_admin(username: str | None, email: str | None) -> bool:
    usernames = _parse_env_set(SUPER_ADMIN_USERNAMES)
    emails = _parse_env_set(SUPER_ADMIN_EMAILS)
    normalized_username = str(username or "").strip().lower()
    normalized_email = str(email or "").strip().lower()
    return bool((normalized_username and normalized_username in usernames) or (normalized_email and normalized_email in emails))


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode((data + "=" * (-len(data) % 4)).encode("ascii"))


def _sign_management_session(payload: Dict[str, Any]) -> str:
    body = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = hmac.new(SESSION_SECRET.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64url_encode(signature)}"


def _verify_management_session(token: str) -> Dict[str, Any]:
    if not SESSION_SECRET:
        raise PermissionError("Management sessions are not configured")
    try:
        body, signature = str(token or "").strip().split(".", 1)
        expected = hmac.new(SESSION_SECRET.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
        provided = _b64url_decode(signature)
        if not hmac.compare_digest(expected, provided):
            raise PermissionError("Invalid management session")
        payload = json.loads(_b64url_decode(body).decode("utf-8"))
    except PermissionError:
        raise
    except Exception as exc:
        raise PermissionError("Invalid management session") from exc
    if int(payload.get("exp") or 0) <= int(time.time()):
        raise PermissionError("Management session expired")
    if not _is_super_admin(str(payload.get("username") or ""), str(payload.get("email") or "")):
        raise PermissionError("Forbidden")
    return payload


def _session_from_user_row(row: Dict[str, Any], *, provider: str, login_at: str | None = None) -> Dict[str, Any]:
    username = str(row.get("username") or "")
    email = row.get("email")
    is_super_admin = _is_super_admin(username, str(email or ""))
    issued_at = int(time.time())
    session = {
        "userId": str(row.get("id") or ""),
        "username": username,
        "name": str(row.get("name") or ""),
        "email": email,
        "avatarUrl": row.get("avatar_url"),
        "isAdmin": bool(row.get("is_admin") or is_super_admin),
        "isSuperAdmin": is_super_admin,
        "loginAt": login_at or _utc_now_iso(),
        "authProvider": provider,
    }
    session["managementToken"] = _sign_management_session({
        "sub": session["userId"],
        "username": username,
        "email": email,
        "iat": issued_at,
        "exp": issued_at + 12 * 60 * 60,
    })
    return session


def _find_user_by_identifier(identifier: str) -> Dict[str, Any] | None:
    value = str(identifier or "").strip().lower()
    if not value:
        return None
    rows = postgrest_client.request(
        "GET",
        "app_users",
        query={"select": "*", "username": f"eq.{value}", "limit": "1"},
    )
    if rows:
        return rows[0]
    rows = postgrest_client.request(
        "GET",
        "app_users",
        query={"select": "*", "email": f"eq.{value}", "limit": "1"},
    )
    return rows[0] if rows else None


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

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


@app.get("/vbio-api/copilot/config")
def copilot_config() -> Tuple[Response, int]:
    return jsonify({"enabled": COPILOT_CONFIGURED}), 200




@app.post("/vbio-api/auth/login")
def complete_local_login() -> Tuple[Response, int]:
    payload = request.get_json(silent=True) or {}
    identifier = str(payload.get("identifier") or "").strip()
    password = str(payload.get("password") or "")
    if not identifier or not password:
        return jsonify({"error": "Username or password is required"}), 400
    try:
        user = _find_user_by_identifier(identifier)
        if not user or user.get("deleted_at"):
            return jsonify({"error": "User not found"}), 401
        username = str(user.get("username") or "").strip().lower()
        expected = hashlib.sha256(f"{username}::{password}".encode("utf-8")).hexdigest()
        if expected != str(user.get("password_hash") or ""):
            return jsonify({"error": "Invalid password"}), 401
        login_at = _utc_now_iso()
        is_super_admin = _is_super_admin(username, str(user.get("email") or ""))
        updated = postgrest_client.request(
            "PATCH",
            "app_users",
            query={"id": f"eq.{user['id']}", "select": "*"},
            payload={"is_admin": bool(user.get("is_admin") or is_super_admin), "last_login_at": login_at},
            headers={"Prefer": "return=representation"},
        )
        return jsonify({"session": _session_from_user_row(updated[0], provider="local", login_at=login_at)}), 200
    except Exception as exc:
        logger.exception("Local login failed")
        return jsonify({"error": str(exc)}), 500


@app.post("/vbio-api/auth/jwt")
def complete_jwt_login() -> Tuple[Response, int]:
    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token") or "").strip()
    try:
        claims = decode_login_jwt(token, jwt_client_store)
        session = jwt_user_service.upsert_user_from_claims(claims)
        session["managementToken"] = _sign_management_session({
            "sub": session.get("userId"),
            "username": session.get("username"),
            "email": session.get("email"),
            "iat": int(time.time()),
            "exp": int(time.time()) + 12 * 60 * 60,
        })
        session["isSuperAdmin"] = _is_super_admin(session.get("username"), session.get("email"))
        session["isAdmin"] = bool(session.get("isAdmin") or session.get("isSuperAdmin"))
        return jsonify({"session": session}), 200
    except JwtTokenError as exc:
        return jsonify({"error": str(exc)}), 401
    except Exception as exc:
        logger.exception("JWT login failed")
        return jsonify({"error": str(exc)}), 500


def _require_jwt_admin() -> Tuple[Response, int] | None:
    token = str(request.headers.get("X-VBio-Session") or "").strip()
    if not token:
        return jsonify({"error": "Missing management session"}), 403
    try:
        _verify_management_session(token)
        return None
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403


@app.get("/vbio-api/admin/jwt-clients")
def list_jwt_clients() -> Tuple[Response, int]:
    forbidden = _require_jwt_admin()
    if forbidden:
        return forbidden
    return jsonify({"clients": [client.public_dict() for client in jwt_client_store.list_clients()]}), 200


@app.post("/vbio-api/admin/jwt-clients")
def create_jwt_client() -> Tuple[Response, int]:
    forbidden = _require_jwt_admin()
    if forbidden:
        return forbidden
    payload = request.get_json(silent=True) or {}
    client, secret = jwt_client_store.create_client(
        name=str(payload.get("name") or "").strip(),
        issuer=str(payload.get("issuer") or "navigation").strip(),
        audience=str(payload.get("audience") or "vbio").strip(),
        max_ttl_seconds=int(payload.get("max_ttl_seconds") or 300),
    )
    return jsonify({"client": client.public_dict(), "secret": secret}), 201


@app.patch("/vbio-api/admin/jwt-clients/<client_id>")
def update_jwt_client(client_id: str) -> Tuple[Response, int]:
    forbidden = _require_jwt_admin()
    if forbidden:
        return forbidden
    payload = request.get_json(silent=True) or {}
    try:
        client = jwt_client_store.update_client(client_id, payload if isinstance(payload, dict) else {})
        return jsonify({"client": client.public_dict()}), 200
    except KeyError:
        return jsonify({"error": "JWT client not found"}), 404


@app.post("/vbio-api/admin/jwt-clients/<client_id>/rotate")
def rotate_jwt_client(client_id: str) -> Tuple[Response, int]:
    forbidden = _require_jwt_admin()
    if forbidden:
        return forbidden
    try:
        client, secret = jwt_client_store.rotate_secret(client_id)
        return jsonify({"client": client.public_dict(), "secret": secret}), 200
    except KeyError:
        return jsonify({"error": "JWT client not found"}), 404


@app.delete("/vbio-api/admin/jwt-clients/<client_id>")
def delete_jwt_client(client_id: str) -> Tuple[Response, int]:
    forbidden = _require_jwt_admin()
    if forbidden:
        return forbidden
    try:
        jwt_client_store.delete_client(client_id)
        return jsonify({"ok": True}), 200
    except KeyError:
        return jsonify({"error": "JWT client not found"}), 404


def _copilot_request_too_large() -> bool:
    content_length = request.content_length
    return content_length is not None and content_length > COPILOT_MAX_REQUEST_BYTES


@app.post("/vbio-api/copilot/assistant")
def copilot_assistant_answer() -> Tuple[Response, int]:
    if not COPILOT_CONFIGURED:
        return jsonify({"error": "Copilot is not configured."}), 404
    if _copilot_request_too_large():
        return jsonify({"error": "Copilot request is too large. Attach files by reference instead of sending file content."}), 413
    payload = request.get_json(silent=True) or {}
    try:
        content = copilot_assistant.answer_context(
            context_type=str(payload.get("context_type") or "").strip(),
            context_payload=payload.get("context_payload") if isinstance(payload.get("context_payload"), dict) else {},
            user_id=str(payload.get("user_id") or "").strip(),
            username=str(payload.get("username") or "").strip(),
            content=str(payload.get("content") or "").strip(),
        )
        return jsonify({"content": content}), 200
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.exception("Copilot assistant failed")
        return jsonify({"error": str(exc)}), 502


@app.post("/vbio-api/copilot/plan_actions")
def copilot_plan_actions() -> Tuple[Response, int]:
    if not COPILOT_CONFIGURED:
        return jsonify({"error": "Copilot is not configured.", "actions": []}), 404
    if _copilot_request_too_large():
        return jsonify({"error": "Copilot request is too large. Attach files by reference instead of sending file content.", "actions": []}), 413
    payload = request.get_json(silent=True) or {}
    ctx_type = str(payload.get("context_type") or "").strip()
    content = str(payload.get("content") or "").strip()
    try:
        actions = copilot_assistant.plan_actions(
            context_type=ctx_type,
            context_payload=payload.get("context_payload") if isinstance(payload.get("context_payload"), dict) else {},
            user_id=str(payload.get("user_id") or "").strip(),
            username=str(payload.get("username") or "").strip(),
            content=content,
        )
        return jsonify({"actions": actions}), 200
    except ValueError as exc:
        return jsonify({"error": str(exc), "actions": []}), 400
    except Exception as exc:
        logger.exception("Copilot action planning failed")
        return jsonify({"error": str(exc), "actions": []}), 502


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


@app.post("/vbio-api/status/batch")
def get_status_batch() -> Tuple[Response, int]:
    return gateway.forward_task_status_batch()


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
