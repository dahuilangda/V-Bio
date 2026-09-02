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
from pathlib import Path
import sys
import time
import uuid
from typing import Any, Dict, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from flask import Flask, Response, jsonify, request, stream_with_context
from backend.monitoring.monitor_store import MonitorStore
from management_api.auth_service import AuthService
from management_api.gateway_handlers import GatewayHandlers
from management_api.http_session import create_pooled_session
from management_api.lead_opt_overlay import LeadOptOverlayService
from management_api.lead_opt_routes import register_lead_opt_routes
from management_api.postgrest_client import PostgrestClient
from management_api.runtime_proxy import RuntimeProxy
from management_api.jwt_clients import JwtClientStore
from management_api.jwt_auth import JwtTokenError, JwtUserService, decode_login_jwt, issue_login_jwt
from management_api.task_store import ProjectTaskStore
from management_api.ccd_download import build_task_ccd_response
from management_api.copilot import CopilotAssistant
from management_api.copilot_complete import CopilotCompleter, completion_config_from_env
from management_api.copilot_settings import (
    apply_runtime_overrides,
    load_saved_settings,
    mark_settings_reloaded,
    merge_and_save,
    public_view,
    reload_settings_if_changed,
    test_connectivity,
    validate_api_url,
    validate_proxy_url,
)
from management_api.copilot_stream import copilot_event_stream, register_steering, submit_follow_up, submit_steering
from management_api.usage_tracker import UsageTracker
from management_api.monitor_stream import MonitorNotificationBroker, sse_frames

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
VBIO_MONITOR_DATABASE_URL = os.environ.get("VBIO_MONITOR_DATABASE_URL", "").strip()
RUNTIME_MAX_INFLIGHT_REQUESTS = int(os.environ.get("VBIO_RUNTIME_MAX_INFLIGHT_REQUESTS", "128"))
RUNTIME_STATUS_HISTORY_SIZE = int(os.environ.get("VBIO_RUNTIME_STATUS_HISTORY_SIZE", "200"))

SERVER_HOST = os.environ.get("VBIO_MGMT_HOST", "127.0.0.1")
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
def _env_float(name: str, default: float) -> float:
    """Parse a float env var, falling back (silently) on malformed values — a bad
    VBIO_COPILOT_TIMEOUT_SECONDS must not take the whole management API down at import."""
    raw = os.environ.get(name, "")
    try:
        return float(raw) if raw.strip() else default
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        return int(raw) if raw.strip() else default
    except (TypeError, ValueError):
        return default


COPILOT_TIMEOUT_SECONDS = _env_float("VBIO_COPILOT_TIMEOUT_SECONDS", 90.0)
COPILOT_MAX_REQUEST_BYTES = _env_int("VBIO_COPILOT_MAX_REQUEST_BYTES", 524288)
COPILOT_ENABLE_THINKING = os.environ.get("VBIO_COPILOT_ENABLE_THINKING", "").strip().lower() in {"1", "true", "yes", "on"}
# Inline auto-complete model. Optional VBIO_COPILOT_COMPLETE_* overrides let a smaller/faster model
# serve per-keystroke completions; each falls back to the planner value when unset.
_COPILOT_COMPLETE_API_URL, _COPILOT_COMPLETE_API_KEY, _COPILOT_COMPLETE_MODEL = completion_config_from_env(os.environ.get)
COPILOT_COMPLETE_API_URL = _COPILOT_COMPLETE_API_URL or COPILOT_API_URL
COPILOT_COMPLETE_API_KEY = _COPILOT_COMPLETE_API_KEY or COPILOT_API_KEY
COPILOT_COMPLETE_MODEL = _COPILOT_COMPLETE_MODEL or COPILOT_MODEL
COPILOT_COMPLETE_TIMEOUT_SECONDS = float(os.environ.get("VBIO_COPILOT_COMPLETE_TIMEOUT_SECONDS", "8"))
COPILOT_COMPLETE_MAX_REQUEST_BYTES = int(os.environ.get("VBIO_COPILOT_COMPLETE_MAX_REQUEST_BYTES", "32768"))

# Runtime settings (proxy / LLM overrides) can flip Copilot from unconfigured to configured without
# a restart.  These mutable holders track the live state (updated when settings are applied) so route
# handlers see the current value rather than a frozen import-time constant.
_copilot_runtime_state: dict[str, bool] = {"configured": COPILOT_CONFIGURED}


def _copilot_is_configured() -> bool:
    return _copilot_runtime_state["configured"]


def _copilot_is_completion_enabled() -> bool:
    # Completion requires the main planner to be configured AND the completer to have a URL+model.
    # The completer's live state is tracked by update_runtime_overrides, so check its live attrs.
    # NOTE: copilot_completer is defined later in this module; Python resolves the name at call time.
    return _copilot_runtime_state["configured"] and bool(copilot_completer.chat_api_url and copilot_completer.chat_model)


def _check_settings_reload() -> None:
    """Hot-reload settings if the file changed on disk.

    Each gunicorn worker has its own copy of the Copilot singletons.  When one worker
    saves settings, it updates its own singletons immediately; other workers detect the
    file change (via mtime) on their next request and re-apply.  This makes "live apply"
    work correctly in a multi-worker deployment.
    """
    new_settings = reload_settings_if_changed()
    if new_settings is not None:
        apply_runtime_overrides(copilot_assistant, copilot_completer, new_settings)
        _recompute_copilot_configured(new_settings)


JWT_CLIENTS_FILE = os.environ.get("VBIO_JWT_CLIENTS_FILE", "frontend/.run/jwt_clients.json").strip()
# Session HMAC secret MUST be explicitly set — never fall back to the runtime API token. The runtime
# token is bundled into the browser SPA and known to clients; reusing it for session signing would
# let anyone forge admin management sessions. If unset, the server starts but all session-dependent
# admin endpoints return a clear configuration error instead of silently using an insecure key.
SESSION_SECRET = os.environ.get("VBIO_SESSION_SECRET", "").strip()
if not SESSION_SECRET:
    logger.warning(
        "VBIO_SESSION_SECRET is not set — admin management session endpoints are DISABLED. "
        "Set VBIO_SESSION_SECRET to a strong random string to enable admin session auth."
    )
MANAGEMENT_SESSION_TTL_SECONDS = int(os.environ.get("VBIO_MANAGEMENT_SESSION_TTL_SECONDS", str(12 * 60 * 60)))
MANAGEMENT_SESSION_REFRESH_TTL_SECONDS = int(
    os.environ.get("VBIO_MANAGEMENT_SESSION_REFRESH_TTL_SECONDS", str(30 * 24 * 60 * 60))
)
SUPER_ADMIN_USERNAMES = os.environ.get("VBIO_SUPER_ADMIN_USERNAMES", "").strip() or os.environ.get("VITE_SUPER_ADMIN_USERNAMES", "").strip()
SUPER_ADMIN_EMAILS = os.environ.get("VBIO_SUPER_ADMIN_EMAILS", "").strip() or os.environ.get("VITE_SUPER_ADMIN_EMAILS", "").strip()

FORM_FIELDS_INTERNAL = {"project_id", "task_name", "task_summary", "operation_mode"}
DEFAULT_PROTENIX_PREDICT_SEED = 42

app = Flask(__name__)
# Streamed-body cap for the COPILOT routes only (request.content_length alone is
# bypassable with chunked encoding). Scoped via before_request: an app-wide
# MAX_CONTENT_LENGTH would also cap /predict multipart uploads (MSA files exceed 512 KiB).


@app.before_request
def _copilot_body_cap():
    if request.path.startswith("/vbio-api/copilot/"):
        declared = request.content_length
        if declared is None or declared > COPILOT_MAX_REQUEST_BYTES:
            # None = chunked (no Content-Length) — read bounded and check actual size.
            if declared is None:
                data = request.get_data(cache=True)
                if len(data) > COPILOT_MAX_REQUEST_BYTES:
                    return jsonify({"error": "Copilot request is too large."}), 413
            else:
                return jsonify({"error": "Copilot request is too large."}), 413
    return None

runtime_http = create_pooled_session(
    pool_connections=max(8, RUNTIME_HTTP_POOL_SIZE),
    pool_maxsize=max(8, RUNTIME_HTTP_POOL_SIZE),
)
# Disable trust_env so HTTP_PROXY/HTTPS_PROXY/NO_PROXY environment variables are NEVER silently
# applied to outbound requests.  Proxy routing is controlled explicitly: Copilot external calls
# (LLM, UniProt, etc.) use per-call ``proxies=self._proxies`` from the settings panel; internal
# calls (RuntimeProxy → runtime backend, PostgREST → DB) must always be direct.  Without this,
# a non-localhost runtime IP would be routed through an env-var proxy and break.
runtime_http.trust_env = False
postgrest_http = create_pooled_session(
    pool_connections=max(4, POSTGREST_HTTP_POOL_SIZE),
    pool_maxsize=max(4, POSTGREST_HTTP_POOL_SIZE),
)
postgrest_http.trust_env = False

postgrest_client = PostgrestClient(
    base_url=VBIO_POSTGREST_URL,
    apikey=VBIO_POSTGREST_APIKEY,
    timeout_seconds=VBIO_POSTGREST_TIMEOUT_SECONDS,
    session=postgrest_http,
)
auth_service = AuthService(postgrest_client)
usage_tracker = UsageTracker(postgrest_client, logger)
task_store = ProjectTaskStore(postgrest_client)
monitor_store = MonitorStore(VBIO_MONITOR_DATABASE_URL) if VBIO_MONITOR_DATABASE_URL else None
monitor_broker = (
    MonitorNotificationBroker(VBIO_MONITOR_DATABASE_URL, monitor_store)
    if monitor_store is not None
    else None
)
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
    enable_thinking=COPILOT_ENABLE_THINKING,
)
copilot_completer = CopilotCompleter(
    chat_api_url=COPILOT_COMPLETE_API_URL,
    chat_api_key=COPILOT_COMPLETE_API_KEY,
    chat_model=COPILOT_COMPLETE_MODEL,
    timeout_seconds=COPILOT_COMPLETE_TIMEOUT_SECONDS,
    session=runtime_http,
    logger=logger,
)

# Apply persisted runtime settings (proxy / LLM overrides) so restarts honor user config saved via
# the Copilot UI.  Each field is only applied when the saved value is non-empty, preserving env-var
# defaults for unconfigured fields.
def _recompute_copilot_configured(settings: Dict[str, Any]) -> None:
    """Recompute the live 'configured' flag from the current effective config.

    Properly resets to ``False`` when there is no effective API URL, so clearing the
    URL in the UI disables Copilot instead of leaving the flag stuck ``True``.
    """
    effective_url = str(settings.get("api_url") or "").strip() or COPILOT_API_URL
    enabled = COPILOT_ENABLED not in {"0", "false", "no", "off"} and bool(effective_url)
    _copilot_runtime_state["configured"] = enabled


_copilot_saved_settings = load_saved_settings()
if _copilot_saved_settings:
    apply_runtime_overrides(copilot_assistant, copilot_completer, _copilot_saved_settings)
    _recompute_copilot_configured(_copilot_saved_settings)


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


def _issue_management_session(*, user_id: str, username: str, email: Any) -> str:
    issued_at = int(time.time())
    return _sign_management_session({
        "sub": str(user_id or ""),
        "username": str(username or ""),
        "email": email,
        "iat": issued_at,
        "exp": issued_at + MANAGEMENT_SESSION_TTL_SECONDS,
        "refresh_exp": issued_at + MANAGEMENT_SESSION_REFRESH_TTL_SECONDS,
    })


def _verify_management_session(token: str, *, allow_refresh: bool = False, require_super_admin: bool = True) -> Dict[str, Any]:
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

    now = int(time.time())
    expires_at = int(payload.get("exp") or 0)
    if expires_at <= now:
        issued_at = int(payload.get("iat") or 0)
        refresh_expires_at = int(
            payload.get("refresh_exp") or (issued_at + MANAGEMENT_SESSION_REFRESH_TTL_SECONDS)
        )
        if not allow_refresh or issued_at <= 0 or refresh_expires_at <= now:
            raise PermissionError("Management session expired")
    if require_super_admin and not _is_super_admin(str(payload.get("username") or ""), str(payload.get("email") or "")):
        raise PermissionError("Forbidden")
    return payload


def _session_from_user_row(row: Dict[str, Any], *, provider: str, login_at: str | None = None) -> Dict[str, Any]:
    username = str(row.get("username") or "")
    email = row.get("email")
    is_super_admin = _is_super_admin(username, str(email or ""))
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
    session["managementToken"] = _issue_management_session(
        user_id=session["userId"],
        username=username,
        email=email,
    )
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

def _find_user_by_id(user_id: str) -> Dict[str, Any] | None:
    normalized = str(user_id or "").strip()
    if not normalized:
        return None
    rows = postgrest_client.request(
        "GET",
        "app_users",
        query={"select": "*", "id": f"eq.{normalized}", "limit": "1"},
    )
    return rows[0] if rows else None


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_error(exc: Exception, *, default_msg: str = "Internal server error") -> str:
    """Return a user-safe error message, logging the full exception for debugging.

    Never return raw str(exc) to the client — it can leak internal hostnames, SQL fragments, file
    paths, and stack-internal class names that help attackers fingerprint the stack. The full
    exception is logged server-side for debugging; the client gets a generic message.
    """
    logger.debug("Suppressed exception detail for client: %s", exc, exc_info=True)
    return default_msg


# ── Password hashing ─────────────────────────────────────────────────────────
# Uses hashlib.scrypt (Python 3.6+ stdlib, strong memory-hard KDF). The hash format is:
#   scrypt$<n>$<r>$<p>$<salt_hex>$<hash_hex>
# Legacy hashes are unsalted SHA-256 of "username::password" — verified for backward compat and
# transparently upgraded to scrypt on the next successful login by the caller.

_SCRYPT_N = 16384  # CPU/memory cost (must be a power of 2)
_SCRYPT_R = 8      # block size
_SCRYPT_P = 1      # parallelism
_SCRYPT_DKLEN = 32  # derived key length


def _hash_password_scrypt(password: str, *, salt: bytes | None = None) -> str:
    """Return a scrypt hash string in the format scrypt$n$r$p$salt_hex$hash_hex."""
    salt = salt or os.urandom(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def _verify_password(password: str, username: str, stored_hash: str) -> bool:
    """Verify a password against the stored hash. Supports scrypt (current) and legacy SHA-256.
    Returns True on match. Uses hmac.compare_digest for timing-safe comparison."""
    if not stored_hash:
        return False
    parts = stored_hash.split("$")
    if len(parts) == 6 and parts[0] == "scrypt":
        # Current format: scrypt$n$r$p$salt_hex$hash_hex
        try:
            n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
            salt = bytes.fromhex(parts[4])
            expected = bytes.fromhex(parts[5])
            dk = hashlib.scrypt(
                password.encode("utf-8"), salt=salt,
                n=n, r=r, p=p, dklen=len(expected),
            )
            return hmac.compare_digest(dk, expected)
        except (ValueError, TypeError):
            return False
    # Legacy format: unsalted SHA-256 of "username::password"
    legacy_expected = hashlib.sha256(f"{username}::{password}".encode("utf-8")).hexdigest()
    return hmac.compare_digest(legacy_expected, stored_hash)


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
    # Liveness only — internal topology (URLs) belongs behind the admin gate.
    return jsonify({"ok": True}), 200


@app.get("/vbio-api/runtime_status")
def runtime_status() -> Tuple[Response, int]:
    forbidden = _require_platform_admin()
    if forbidden:
        return forbidden
    return jsonify(
        {
            "ok": True,
            "runtime_proxy": runtime_proxy.get_runtime_status(),
            "overlay_service": lead_opt_overlay_service.get_status(),
        }
    ), 200


@app.get("/vbio-api/copilot/config")
def copilot_config() -> Tuple[Response, int]:
    return jsonify({"enabled": _copilot_is_configured(), "completionEnabled": _copilot_is_completion_enabled()}), 200


@app.get("/vbio-api/copilot/settings")
def copilot_get_settings() -> Tuple[Response, int]:
    """Return the current persisted Copilot settings (API key masked, never raw).

    Requires a platform-admin management session — the response reveals deployment
    internals (LLM endpoint, model, proxy host) even though the key itself is masked.
    """
    forbidden = _require_platform_admin()
    if forbidden:
        return forbidden
    settings = load_saved_settings()
    return jsonify(public_view(settings)), 200


@app.post("/vbio-api/copilot/settings")
def copilot_save_settings() -> Tuple[Response, int]:
    """Merge, persist, and live-apply Copilot runtime settings.

    Requires a platform-admin management session.  ``proxy`` / ``api_url`` / ``model``
    are replaced (empty string clears the override); ``api_key`` is only updated when a
    non-empty value is supplied (the browser only ever holds a masked key).
    """
    forbidden = _require_platform_admin()
    if forbidden:
        return forbidden
    payload = request.get_json(silent=True) or {}
    # Validate scheme before persisting — prevents injecting non-HTTP schemes.
    try:
        if "proxy" in payload:
            payload["proxy"] = validate_proxy_url(str(payload.get("proxy") or ""))
        if "api_url" in payload:
            payload["api_url"] = validate_api_url(str(payload.get("api_url") or ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        current = merge_and_save(payload)  # atomic read-modify-write under cross-process lock
        apply_runtime_overrides(copilot_assistant, copilot_completer, current)
        _recompute_copilot_configured(current)
        mark_settings_reloaded()  # prevent this worker from redundantly re-applying on next request
    except Exception as exc:
        logger.exception("Failed to save Copilot settings")
        return jsonify({"error": _safe_error(exc)}), 500
    return jsonify({"ok": True, "settings": public_view(current)}), 200


@app.post("/vbio-api/copilot/settings/test")
def copilot_test_settings() -> Tuple[Response, int]:
    """Test proxy (UniProt reachability) and LLM endpoint connectivity.

    Requires a platform-admin management session (the endpoint sends the persisted API
    key to the configured LLM URL, so it must not be callable by unauthenticated users).
    Accepts settings inline (from the form) so admins can test before saving.  When
    ``api_key`` is empty, falls back to the persisted key.
    """
    forbidden = _require_platform_admin()
    if forbidden:
        return forbidden
    payload = request.get_json(silent=True) or {}
    saved = load_saved_settings()
    # Proxy: respect the form value as-is (empty = no proxy). Do NOT fall back to saved —
    # the user may have just cleared the field and needs to see the result without a proxy.
    try:
        proxy = validate_proxy_url(str(payload.get("proxy") or "").strip())
        api_url = validate_api_url(
            str(payload.get("api_url") or "").strip()
            or str(saved.get("api_url") or "").strip()
            or COPILOT_API_URL
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    model = str(payload.get("model") or "").strip() or str(saved.get("model") or "").strip() or COPILOT_MODEL
    # Empty form key → use the persisted key for the test.
    api_key = str(payload.get("api_key") or "").strip() or str(saved.get("api_key") or "").strip()
    try:
        results = test_connectivity(runtime_http, proxy=proxy, api_url=api_url, api_key=api_key, model=model)
    except Exception as exc:
        logger.exception("Copilot settings connectivity test failed")
        return jsonify({"error": _safe_error(exc)}), 500
    return jsonify(results), 200


# ── F2: server-side auth surface (registration/profile/users/tokens) ─────────────────────
from management_api.auth_endpoints import (  # noqa: E402
    handle_admin_create_user,
    handle_admin_list_users,
    handle_admin_update_user,
    handle_create_token,
    handle_delete_token,
    handle_list_tokens,
    handle_me,
    handle_register,
    handle_update_profile,
    handle_update_token,
    handle_users_by_ids,
    handle_users_search,
)


@app.post("/vbio-api/auth/register")
def auth_register() -> Tuple[Response, int]:
    return handle_register(gateway)


@app.get("/vbio-api/auth/me")
def auth_me() -> Tuple[Response, int]:
    return handle_me(gateway)


@app.patch("/vbio-api/auth/profile")
def auth_update_profile() -> Tuple[Response, int]:
    return handle_update_profile(gateway)


@app.post("/vbio-api/auth/users/by-ids")
def auth_users_by_ids() -> Tuple[Response, int]:
    return handle_users_by_ids(gateway)


@app.get("/vbio-api/auth/users/search")
def auth_users_search() -> Tuple[Response, int]:
    return handle_users_search(gateway)


@app.get("/vbio-api/admin/users")
def admin_list_users() -> Tuple[Response, int]:
    return handle_admin_list_users(gateway)


@app.post("/vbio-api/admin/users")
def admin_create_user() -> Tuple[Response, int]:
    return handle_admin_create_user(gateway)


@app.patch("/vbio-api/admin/users/<user_id>")
def admin_update_user(user_id: str) -> Tuple[Response, int]:
    return handle_admin_update_user(gateway, user_id)


@app.get("/vbio-api/tokens")
def list_tokens() -> Tuple[Response, int]:
    return handle_list_tokens(gateway)


@app.post("/vbio-api/tokens")
def create_token() -> Tuple[Response, int]:
    return handle_create_token(gateway)


@app.patch("/vbio-api/tokens/<token_id>")
def update_token(token_id: str) -> Tuple[Response, int]:
    return handle_update_token(gateway, token_id)


@app.delete("/vbio-api/tokens/<token_id>")
def delete_token(token_id: str) -> Tuple[Response, int]:
    return handle_delete_token(gateway, token_id)


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
            return jsonify({"error": "Invalid credentials"}), 401
        username = str(user.get("username") or "").strip().lower()
        stored_hash = str(user.get("password_hash") or "")
        # Verify the password using timing-safe comparison. Supports two formats:
        # - "scrypt$<n>$<r>$<p>$<salt_hex>$<hash_hex>" (current, strong KDF)
        # - legacy unsalted SHA-256 of "username::password" (auto-upgraded on successful login)
        if not _verify_password(password, username, stored_hash):
            return jsonify({"error": "Invalid credentials"}), 401
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
        return jsonify({"error": _safe_error(exc)}), 500


@app.post("/vbio-api/auth/management-session/refresh")
def refresh_management_session() -> Tuple[Response, int]:
    token = str(request.headers.get("X-VBio-Session") or "").strip()
    if not token:
        return jsonify({"error": "Missing management session"}), 401
    try:
        claims = _verify_management_session(token, allow_refresh=True, require_super_admin=False)
        user = _find_user_by_id(str(claims.get("sub") or ""))
        if not user or user.get("deleted_at"):
            return jsonify({"error": "Management user is no longer active"}), 401
        username = str(user.get("username") or "")
        email = user.get("email")
        if not bool(user.get("is_admin") or _is_super_admin(username, str(email or ""))):
            return jsonify({"error": "Forbidden"}), 403
        refreshed = _issue_management_session(
            user_id=str(user.get("id") or ""),
            username=username,
            email=email,
        )
        return jsonify({"managementToken": refreshed}), 200
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 401
    except Exception as exc:
        logger.exception("Management session refresh failed")
        return jsonify({"error": _safe_error(exc)}), 500


@app.post("/vbio-api/auth/jwt")
def complete_jwt_login() -> Tuple[Response, int]:
    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token") or "").strip()
    try:
        claims = decode_login_jwt(token, jwt_client_store)
        session = jwt_user_service.upsert_user_from_claims(claims)
        session["managementToken"] = _issue_management_session(
            user_id=str(session.get("userId") or ""),
            username=str(session.get("username") or ""),
            email=session.get("email"),
        )
        session["isSuperAdmin"] = _is_super_admin(session.get("username"), session.get("email"))
        session["isAdmin"] = bool(session.get("isAdmin") or session.get("isSuperAdmin"))
        return jsonify({"session": session}), 200
    except JwtTokenError as exc:
        return jsonify({"error": str(exc)}), 401
    except Exception as exc:
        logger.exception("JWT login failed")
        return jsonify({"error": _safe_error(exc)}), 500


def _require_jwt_admin() -> Tuple[Response, int] | None:
    token = str(request.headers.get("X-VBio-Session") or "").strip()
    if not token:
        return jsonify({"error": "Missing management session"}), 403
    try:
        _verify_management_session(token)
        return None
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403

def _require_platform_admin() -> Tuple[Response, int] | None:
    token = str(request.headers.get("X-VBio-Session") or "").strip()
    if not token:
        return jsonify({"error": "Missing management session"}), 403
    try:
        claims = _verify_management_session(token, require_super_admin=False)
        user = _find_user_by_id(str(claims.get("sub") or ""))
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    except Exception:
        logger.exception("Platform administrator verification failed")
        return jsonify({"error": "Unable to verify administrator session"}), 503

    if not user or user.get("deleted_at"):
        return jsonify({"error": "Management user is no longer active"}), 403
    username = str(user.get("username") or "")
    email = str(user.get("email") or "")
    if not bool(user.get("is_admin") or _is_super_admin(username, email)):
        return jsonify({"error": "Forbidden"}), 403
    return None


@app.get("/vbio-api/admin/cluster-overview")
def get_admin_cluster_overview() -> Tuple[Response, int]:
    forbidden = _require_platform_admin()
    if forbidden:
        return forbidden

    try:
        window_hours = int(request.args.get("window_hours") or 24)
    except (TypeError, ValueError):
        window_hours = 24
    window_hours = max(1, min(24 * 31, window_hours))

    if monitor_store is None:
        return jsonify({"error": "PostgreSQL monitor store is not configured"}), 503
    try:
        return jsonify(monitor_store.get_overview(window_hours=window_hours)), 200
    except Exception as exc:
        logger.exception("Unable to read PostgreSQL monitor snapshot")
        return jsonify({"error": _safe_error(exc)}), 503


@app.get("/vbio-api/admin/monitor-stream")
def stream_admin_monitor() -> Response | Tuple[Response, int]:
    forbidden = _require_platform_admin()
    if forbidden:
        return forbidden
    if monitor_store is None or monitor_broker is None:
        return jsonify({"error": "PostgreSQL monitor store is not configured"}), 503

    try:
        window_hours = max(1, min(24 * 31, int(request.args.get("window_hours") or 24)))
    except (TypeError, ValueError):
        window_hours = 24
    cursor_value = request.headers.get("Last-Event-ID") or request.args.get("cursor") or "0"
    try:
        event_cursor = max(0, int(cursor_value))
    except (TypeError, ValueError):
        event_cursor = 0

    try:
        subscription = monitor_broker.subscribe(event_cursor)
    except Exception as exc:
        logger.exception("Unable to subscribe to PostgreSQL monitor notifications")
        return jsonify({"error": _safe_error(exc)}), 503

    frames = sse_frames(
        subscription,
        lambda: monitor_store.get_overview(window_hours=window_hours),
    )
    response = Response(stream_with_context(frames), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache, no-transform"
    response.headers["X-Accel-Buffering"] = "no"
    return response


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
    client, _secret = jwt_client_store.create_client(
        name=str(payload.get("name") or "").strip(),
        issuer=str(payload.get("issuer") or "navigation").strip(),
        audience=str(payload.get("audience") or "vbio").strip(),
        max_ttl_seconds=int(payload.get("max_ttl_seconds") or 300),
    )
    token, expires_at = issue_login_jwt(
        client,
        subject=str(payload.get("subject") or client.client_id),
        username=str(payload.get("username") or client.client_id),
        name=str(payload.get("display_name") or client.name),
        email=str(payload.get("email") or "") or None,
    )
    return jsonify({
        "client": client.public_dict(),
        "token": token,
        "expires_at": expires_at,
    }), 201


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



@app.post("/vbio-api/admin/jwt-clients/<client_id>/token")
def issue_jwt_client_token(client_id: str) -> Tuple[Response, int]:
    forbidden = _require_jwt_admin()
    if forbidden:
        return forbidden
    payload = request.get_json(silent=True) or {}
    client = jwt_client_store.get_client(client_id)
    if not client:
        return jsonify({"error": "JWT client not found"}), 404
    try:
        token, expires_at = issue_login_jwt(
            client,
            subject=str(payload.get("subject") or client.client_id),
            username=str(payload.get("username") or client.client_id),
            name=str(payload.get("display_name") or client.name),
            email=str(payload.get("email") or "") or None,
            ttl_seconds=int(payload.get("ttl_seconds") or client.max_ttl_seconds),
        )
        return jsonify({
            "client": client.public_dict(),
            "token": token,
            "expires_at": expires_at,
        }), 200
    except JwtTokenError as exc:
        return jsonify({"error": str(exc)}), 409

@app.post("/vbio-api/admin/jwt-clients/<client_id>/rotate")
def rotate_jwt_client(client_id: str) -> Tuple[Response, int]:
    forbidden = _require_jwt_admin()
    if forbidden:
        return forbidden
    try:
        client, secret = jwt_client_store.rotate_secret(client_id)
        token, expires_at = issue_login_jwt(client)
        return jsonify({
            "client": client.public_dict(),
            "secret": secret,
            "token": token,
            "expires_at": expires_at,
        }), 200
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


@app.post("/vbio-api/copilot/turn")
def copilot_turn() -> Tuple[Response, int]:
    _check_settings_reload()
    if not _copilot_is_configured():
        return jsonify({"error": "Copilot is not configured."}), 404
    if _copilot_request_too_large():
        return jsonify({"error": "Copilot request is too large. Attach files by reference instead of sending file content."}), 413
    payload = request.get_json(silent=True) or {}
    try:
        result = copilot_assistant.plan_turn(
            context_type=str(payload.get("context_type") or "").strip(),
            context_payload=payload.get("context_payload") if isinstance(payload.get("context_payload"), dict) else {},
            user_id=str(payload.get("user_id") or "").strip(),
            username=str(payload.get("username") or "").strip(),
            content=str(payload.get("content") or "").strip(),
        )
        return jsonify(result), 200
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        # No silent downgrade here: a planner that fails to converge returns state="failed" as a
        # NORMAL result (the loop's honest terminal state), so by the time an exception reaches
        # this handler it is a genuine server/transport fault — surface it as a 502 and log it.
        # Never fabricate a state="complete" answer for a failed plan.
        logger.exception("Copilot turn failed")
        return jsonify({"error": _safe_error(exc)}), 502


@app.post("/vbio-api/copilot/stream")
def copilot_stream() -> Response:
    _check_settings_reload()
    if not _copilot_is_configured():
        return jsonify({"error": "Copilot is not configured."}), 404
    if _copilot_request_too_large():
        return jsonify({"error": "Copilot request is too large. Attach files by reference instead of sending file content."}), 413
    payload = request.get_json(silent=True) or {}
    context_type = str(payload.get("context_type") or "").strip()
    context_payload = payload.get("context_payload") if isinstance(payload.get("context_payload"), dict) else {}
    user_id = str(payload.get("user_id") or "").strip()
    username = str(payload.get("username") or "").strip()
    content = str(payload.get("content") or "").strip()

    # The client-generated turn key rides the stream so mid-turn interjections (steering)
    # can address exactly this in-flight turn. Registered EAGERLY here — a Flask response
    # stream's body runs lazily (first consumer read), so in-generator registration would
    # miss steers arriving before the first frame; the generator's finally still unregisters.
    turn_key = str(payload.get("turn_key") or "").strip()
    if turn_key:
        register_steering(turn_key)
        register_steering(turn_key + "::followup")

    def plan(on_step, abort, get_steering=None, get_follow_ups=None):
        # No silent downgrade: non-convergence is already a normal state="failed" result from
        # plan_turn; any exception here is a genuine fault that copilot_event_stream surfaces as
        # an honest event:error frame (never a fabricated state="complete" result).
        return copilot_assistant.plan_turn(
            context_type=context_type,
            context_payload=context_payload,
            user_id=user_id,
            username=username,
            content=content,
            on_event=on_step,
            abort=abort,
            get_steering=get_steering,
            get_follow_ups=get_follow_ups,
        )

    return Response(
        stream_with_context(copilot_event_stream(plan, turn_key=turn_key)),
        mimetype="text/event-stream",
    )


@app.post("/vbio-api/copilot/steer")
def copilot_steer() -> Tuple[Response, int]:
    """Queue a user interjection for an in-flight streaming turn (pi steering alignment)."""
    if not _copilot_is_configured():
        return jsonify({"error": "Copilot is not configured."}), 404
    payload = request.get_json(silent=True) or {}
    turn_key = str(payload.get("turn_key") or "").strip()
    text = str(payload.get("text") or "").strip()
    if not turn_key or not text:
        return jsonify({"error": "turn_key and text are required."}), 400
    if len(text) > 4000:
        return jsonify({"error": "Steering text is too large."}), 413
    is_follow_up = bool(payload.get("follow_up")) or turn_key.endswith("::followup")
    queued = (
        submit_follow_up(turn_key.removesuffix("::followup"), text)
        if is_follow_up
        else submit_steering(turn_key, text)
    )
    if queued:
        return jsonify({"queued": True}), 200
    # Unknown key (turn ended or never existed) or full queue — both honest, actionable.
    return jsonify({"queued": False, "error": "No in-flight turn with that key (it may have finished), or the steering queue is full."}), 409


@app.post("/vbio-api/copilot/complete")
def copilot_complete() -> Tuple[Response, int]:
    # Inline auto-complete is best-effort assistance: it never blocks the composer or surfaces an
    # error to the user. When disabled or on any failure it returns an empty suggestion.
    _check_settings_reload()
    if not _copilot_is_completion_enabled():
        return jsonify({"suggestion": "", "completions": []}), 200
    content_length = request.content_length
    if content_length is not None and content_length > COPILOT_COMPLETE_MAX_REQUEST_BYTES:
        return jsonify({"suggestion": ""}), 200
    payload = request.get_json(silent=True) or {}
    try:
        suggestion = copilot_completer.complete(
            context_type=str(payload.get("context_type") or "").strip(),
            content=str(payload.get("content") or "").strip(),
            context_payload=payload.get("context_payload"),
            user_id=str(payload.get("user_id") or "").strip(),
            username=str(payload.get("username") or "").strip(),
        )
        # Backward compatible: ``suggestion`` stays the top-ranked suffix (legacy single-ghost
        # consumers), ``completions`` carries the full ranked top-10 for the picker.
        return jsonify({"suggestion": suggestion[0] if suggestion else "", "completions": suggestion}), 200
    except Exception as exc:  # never 5xx — autocomplete must degrade silently to "no suggestion"
        logger.debug("Copilot completion failed: %s", str(exc)[:300])
        return jsonify({"suggestion": "", "completions": []}), 200


@app.post("/vbio-api/predict")
def submit_predict() -> Tuple[Response, int]:
    return gateway.forward_submit("/predict", "submit_predict")


@app.post("/vbio-api/api/boltz2score")
def submit_boltz2score() -> Tuple[Response, int]:
    return gateway.forward_submit("/api/boltz2score", "submit_boltz2score")


@app.post("/vbio-api/api/affinity_train")
def submit_affinity_train() -> Tuple[Response, int]:
    return gateway.forward_submit("/api/affinity_train", "submit_affinity_train")


@app.post("/vbio-api/api/lead_optimization/submit")
def submit_lead_optimization() -> Tuple[Response, int]:
    return (
        jsonify(
            {
                "error": (
                    "Legacy /api/lead_optimization/submit pipeline is disabled. "
                    "Use the HALO generative workflow: /api/lead_optimization/halo_optimize."
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
    return gateway.forward_task_read(task_id, "/results", "read_results_view", upstream_suffix="/view")


@app.get("/vbio-api/results/<task_id>/screening")
def get_results_screening(task_id: str) -> Tuple[Response, int]:
    return gateway.forward_task_read(task_id, "/results", "read_screening", upstream_suffix="/screening")


@app.get("/vbio-api/tasks/<task_id>/ccd")
def get_task_ccd(task_id: str) -> Tuple[Response, int]:
    # Auth: verify the caller has access to this task's project, same as every other task read
    # endpoint. Without this, any anonymous user who guesses a task_id can download another
    # tenant's CCD artifacts.
    try:
        project_id = gateway._read_project_id_from_query()
        token_plain = (request.headers.get("X-API-Token") or "").strip()
        token = gateway._authorize_project_read(project_id, token_plain)
        # Platform token without a project_id skips the scoped lookup (same as task reads);
        # project tokens always carry one and must match a visible task row.
        if project_id:
            task_row = gateway.task_store.find_project_task(task_id, project_id)
            if not task_row:
                return jsonify({"error": "Task not found"}), 404
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    except Exception:
        logger.exception("CCD download auth failed for task %s", task_id)
        return jsonify({"error": "Authorization failed"}), 403
    return build_task_ccd_response(task_id)


register_lead_opt_routes(
    app,
    forward_task_read=gateway.forward_task_read,
    forward_quick_json=gateway.forward_quick_json,
    forward_quick_multipart=gateway.forward_quick_multipart,
    forward_quick_get=gateway.forward_quick_get,
    pocket_overlay_handler=gateway.handle_lead_optimization_pocket_overlay,
)


# ── F1 hardening: gateway coverage for every path the SPA used to call directly ──────────
from management_api.gateway_runtime_index import handle_tasks_runtime_index  # noqa: E402


@app.get("/vbio-api/tasks/runtime_index")
def tasks_runtime_index() -> Tuple[Response, int]:
    """Project-filtered runtime worker index (the raw index is cross-tenant)."""
    return handle_tasks_runtime_index(gateway)


@app.post("/vbio-api/api/affinity/preview")
def affinity_preview() -> Tuple[Response, int]:
    # Stateless structure preview (no task) — project-bound token, read-level.
    return gateway.forward_quick_multipart("/api/affinity/preview", "affinity_preview", require_submit=False)



@app.post("/vbio-api/api/export/tasks_excel")
def export_tasks_excel() -> Tuple[Response, int]:
    return gateway.forward_quick_json("/api/export/tasks_excel", "export_tasks_excel", require_submit=True)


@app.get("/vbio-api/api/export/tasks_excel/<export_id>/status")
def export_tasks_excel_status(export_id: str) -> Tuple[Response, int]:
    from urllib.parse import quote

    return gateway.forward_quick_get(
        f"/api/export/tasks_excel/{quote(export_id, safe='')}/status", "export_tasks_excel_status"
    )


@app.post("/vbio-api/api/export/tasks_excel/<export_id>/cancel")
def export_tasks_excel_cancel(export_id: str) -> Tuple[Response, int]:
    from urllib.parse import quote

    return gateway.forward_quick_json(
        f"/api/export/tasks_excel/{quote(export_id, safe='')}/cancel",
        "export_tasks_excel_cancel",
        require_submit=True,
    )


@app.get("/vbio-api/api/export/tasks_excel/<export_id>/download")
def export_tasks_excel_download(export_id: str) -> Tuple[Response, int]:
    from urllib.parse import quote

    # Binary passthrough (Content-Disposition preserved by build_flask_response).
    return gateway.forward_quick_get(
        f"/api/export/tasks_excel/{quote(export_id, safe='')}/download", "export_tasks_excel_download"
    )


@app.delete("/vbio-api/tasks/<task_id>")
def cancel_or_delete_task(task_id: str) -> Tuple[Response, int]:
    return gateway.cancel_or_delete_task(task_id)


if __name__ == "__main__":
    app.run(host=SERVER_HOST, port=SERVER_PORT, debug=False, threaded=True)
