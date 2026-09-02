"""Server-side auth surface (F2): registration, profile, user admin, share search, API tokens.

Previously the SPA performed these DIRECTLY against PostgREST as the anonymous role —
client-side password hashing, client-chosen `is_admin`, plaintext tokens in a world-readable
column. These endpoints move every sensitive operation behind the management session (or a
public register with server-side hashing + server-side super-admin determination), so the
database policies can drop anonymous access to app_users/api_tokens entirely.
"""
from __future__ import annotations

import hashlib
import secrets
from typing import Any, Dict, List, Optional, Tuple

from flask import jsonify, request

# Imported lazily inside functions: vbio_management_api imports this module at
# route-registration time, so a module-level import would be circular.


def _server_helpers():
    import vbio_management_api as server

    return server

# Fields a non-admin client may ever see on a user row.
SAFE_USER_FIELDS = ("id", "username", "name", "email", "avatar_url", "is_admin", "deleted_at", "created_at", "last_login_at")


def _session_user(postgrest) -> Optional[Dict[str, Any]]:
    """Resolve the caller's app_user row from the X-VBio-Session header (or None)."""
    token = (request.headers.get("X-VBio-Session") or "").strip()
    try:
        claims = _server_helpers()._verify_management_session(token, allow_refresh=True, require_super_admin=False)
    except Exception:
        return None
    if not claims:
        return None
    user_id = str(claims.get("sub") or "").strip()
    username = str(claims.get("username") or "").strip()
    if user_id:
        rows = postgrest.request("GET", "app_users", query={"id": f"eq.{user_id}", "limit": "1"})
        if rows:
            return rows[0]
    if username:
        rows = postgrest.request("GET", "app_users", query={"username": f"eq.{username}", "limit": "1"})
        if rows:
            return rows[0]
    return None


def _safe(user: Dict[str, Any]) -> Dict[str, Any]:
    return {key: user.get(key) for key in SAFE_USER_FIELDS if key in user}


def _unauthorized() -> Tuple[Any, int]:
    return jsonify({"error": "Management session required."}), 401


# ── Public: registration ──────────────────────────────────────────────────────────────────

def handle_register(gateway) -> Tuple[Any, int]:
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    name = str(payload.get("name") or username).strip() or username
    email = str(payload.get("email") or "").strip().lower() or None

    if not username or len(username) < 2:
        return jsonify({"error": "Username must be at least 2 characters."}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400

    postgrest = gateway.auth_service.postgrest
    # Uniqueness (same checks the SPA did, now server-side).
    if postgrest.request("GET", "app_users", query={"username": f"eq.{username}", "select": "id", "limit": "1"}):
        return jsonify({"error": "Username already exists."}), 409
    if email and postgrest.request("GET", "app_users", query={"email": f"eq.{email}", "select": "id", "limit": "1"}):
        return jsonify({"error": "Email already registered."}), 409

    # SERVER-side: strong hash + admin only via the env super-admin lists.
    password_hash = _server_helpers()._hash_password_scrypt(password)
    is_admin = _server_helpers()._is_super_admin(username, email)
    created = postgrest.request(
        "POST",
        "app_users",
        payload={"username": username, "name": name, "email": email, "password_hash": password_hash, "is_admin": is_admin},
        query={"select": "id,username,name,email,is_admin"},
        headers={"Prefer": "return=representation"},
    )
    if not created:
        return jsonify({"error": "Failed to create the account."}), 500
    return jsonify({"user": _safe(created[0])}), 201


# ── Session-scoped: profile ───────────────────────────────────────────────────────────────

def handle_me(gateway) -> Tuple[Any, int]:
    user = _session_user(gateway.auth_service.postgrest)
    if not user:
        return _unauthorized()
    return jsonify({"user": _safe(user)}), 200


def handle_update_profile(gateway) -> Tuple[Any, int]:
    user = _session_user(gateway.auth_service.postgrest)
    if not user:
        return _unauthorized()
    payload = request.get_json(silent=True) or {}
    patch: Dict[str, Any] = {}
    for field in ("name", "avatar_url"):
        value = payload.get(field)
        if isinstance(value, str):
            patch[field] = value.strip()
    new_password = str(payload.get("password") or "")
    current_password = str(payload.get("current_password") or "")
    if new_password:
        if len(new_password) < 8:
            return jsonify({"error": "Password must be at least 8 characters."}), 400
        if not _server_helpers()._verify_password(current_password, user.get("username") or "", user.get("password_hash") or ""):
            return jsonify({"error": "Current password is incorrect."}), 403
        patch["password_hash"] = _server_helpers()._hash_password_scrypt(new_password)
    if not patch:
        return jsonify({"error": "Nothing to update."}), 400
    updated = gateway.auth_service.postgrest.request(
        "PATCH", "app_users", payload=patch, query={"id": f"eq.{user['id']}", "select": "*"},
        headers={"Prefer": "return=representation"},
    )
    return jsonify({"user": _safe(updated[0]) if updated else _safe(user)}), 200


def handle_users_by_ids(gateway) -> Tuple[Any, int]:
    user = _session_user(gateway.auth_service.postgrest)
    if not user:
        return _unauthorized()
    payload = request.get_json(silent=True) or {}
    ids = payload.get("ids")
    if not isinstance(ids, list) or not ids or len(ids) > 200:
        return jsonify({"error": "ids must be a non-empty list (max 200)."}), 400
    clean = [str(i).strip() for i in ids if str(i or "").strip()]
    rows = gateway.auth_service.postgrest.request(
        "GET",
        "app_users",
        query={"id": f"in.({','.join(clean)})", "select": "id,username,name,avatar_url", "limit": "200"},
    )
    return jsonify({"users": rows or []}), 200


def handle_users_search(gateway) -> Tuple[Any, int]:
    user = _session_user(gateway.auth_service.postgrest)
    if not user:
        return _unauthorized()
    query_text = str(request.args.get("q") or "").strip()
    if len(query_text) < 1:
        return jsonify({"users": []}), 200
    # Share-dialog semantics: match username/name/email prefix-ish, active users only.
    like = f"*{query_text.lower()}*"
    rows = gateway.auth_service.postgrest.request(
        "GET",
        "app_users",
        query={
            "or": f"(username.ilike.{like},name.ilike.{like},email.ilike.{like})",
            "deleted_at": "is.null",
            "select": "id,username,name,email,avatar_url",
            "order": "username.asc",
            "limit": "20",
        },
    )
    return jsonify({"users": rows or []}), 200


# ── Platform-admin: user management ───────────────────────────────────────────────────────

def handle_admin_list_users(gateway) -> Tuple[Any, int]:
    forbidden = _server_helpers()._require_platform_admin()
    if forbidden:
        return forbidden
    rows = gateway.auth_service.postgrest.request(
        "GET", "app_users", query={"select": "*", "order": "created_at.desc", "limit": "500"}
    )
    return jsonify({"users": [_safe(row) for row in rows or []]}), 200


def handle_admin_create_user(gateway) -> Tuple[Any, int]:
    forbidden = _server_helpers()._require_platform_admin()
    if forbidden:
        return forbidden
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not username or len(password) < 8:
        return jsonify({"error": "Username and a password of at least 8 characters are required."}), 400
    email = str(payload.get("email") or "").strip().lower() or None
    postgrest = gateway.auth_service.postgrest
    if postgrest.request("GET", "app_users", query={"username": f"eq.{username}", "select": "id", "limit": "1"}):
        return jsonify({"error": "Username already exists."}), 409
    is_admin = bool(payload.get("is_admin")) or _server_helpers()._is_super_admin(username, email)
    created = postgrest.request(
        "POST",
        "app_users",
        payload={
            "username": username,
            "name": str(payload.get("name") or username).strip() or username,
            "email": email,
            "password_hash": _server_helpers()._hash_password_scrypt(password),
            "is_admin": is_admin,
        },
        query={"select": "id,username,name,email,is_admin"},
        headers={"Prefer": "return=representation"},
    )
    return jsonify({"user": _safe(created[0]) if created else {}}), 201


def handle_admin_update_user(gateway, user_id: str) -> Tuple[Any, int]:
    forbidden = _server_helpers()._require_platform_admin()
    if forbidden:
        return forbidden
    payload = request.get_json(silent=True) or {}
    patch: Dict[str, Any] = {}
    for field in ("name", "email", "avatar_url"):
        if isinstance(payload.get(field), str):
            patch[field] = payload[field].strip()
    if "is_admin" in payload:
        patch["is_admin"] = bool(payload.get("is_admin")) or None
        if patch["is_admin"] is None:
            patch["is_admin"] = False
    if "deleted_at" in payload:
        value = payload.get("deleted_at")
        patch["deleted_at"] = str(value).strip() if value else None
    new_password = str(payload.get("password") or "")
    if new_password:
        if len(new_password) < 8:
            return jsonify({"error": "Password must be at least 8 characters."}), 400
        patch["password_hash"] = _server_helpers()._hash_password_scrypt(new_password)
    if not patch:
        return jsonify({"error": "Nothing to update."}), 400
    updated = gateway.auth_service.postgrest.request(
        "PATCH", "app_users", payload=patch, query={"id": f"eq.{user_id}", "select": "*"},
        headers={"Prefer": "return=representation"},
    )
    if not updated:
        return jsonify({"error": "User not found."}), 404
    return jsonify({"user": _safe(updated[0])}), 200


# ── Session-scoped: API tokens ────────────────────────────────────────────────────────────

_TOKEN_FIELDS = "id,user_id,name,project_id,allow_submit,allow_delete,allow_cancel,is_active,revoked_at,expires_at,created_at,last_used_at"


def handle_list_tokens(gateway) -> Tuple[Any, int]:
    user = _session_user(gateway.auth_service.postgrest)
    if not user:
        return _unauthorized()
    rows = gateway.auth_service.postgrest.request(
        "GET", "api_tokens", query={"user_id": f"eq.{user['id']}", "select": _TOKEN_FIELDS, "order": "created_at.desc", "limit": "200"}
    )
    return jsonify({"tokens": rows or []}), 200


def handle_create_token(gateway) -> Tuple[Any, int]:
    user = _session_user(gateway.auth_service.postgrest)
    if not user:
        return _unauthorized()
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip() or "api-token"
    project_id = str(payload.get("project_id") or "").strip()
    if not project_id:
        return jsonify({"error": "project_id is required."}), 400
    # The project must exist (same check the gateway does on submit). ensure_project_exists
    # raises PermissionError when missing and returns None on success — the old truthiness
    # check read success as failure, so token creation ALWAYS returned 404.
    try:
        gateway.auth_service.ensure_project_exists(project_id)
    except PermissionError:
        return jsonify({"error": "Project not found."}), 404

    token_plain = f"vbio_{secrets.token_urlsafe(32)}"
    token_hash = hashlib.sha256(token_plain.encode("utf-8")).hexdigest()
    created = gateway.auth_service.postgrest.request(
        "POST",
        "api_tokens",
        payload={
            "user_id": user["id"],
            "name": name[:80],
            "project_id": project_id,
            "token_hash": token_hash,
            "allow_submit": bool(payload.get("allow_submit", True)),
            "allow_delete": bool(payload.get("allow_delete", False)),
            "allow_cancel": bool(payload.get("allow_cancel", True)),
            "is_active": True,
        },
        query={"select": _TOKEN_FIELDS},
        headers={"Prefer": "return=representation"},
    )
    if not created:
        return jsonify({"error": "Failed to create the token."}), 500
    # The plaintext crosses the wire exactly ONCE, in this response; the row stores only
    # the hash (token_plain is never written).
    return jsonify({"token": created[0], "token_plain": token_plain}), 201


def handle_update_token(gateway, token_id: str) -> Tuple[Any, int]:
    user = _session_user(gateway.auth_service.postgrest)
    if not user:
        return _unauthorized()
    payload = request.get_json(silent=True) or {}
    patch: Dict[str, Any] = {}
    if "name" in payload:
        patch["name"] = str(payload.get("name") or "").strip()[:80]
    for flag in ("allow_submit", "allow_delete", "allow_cancel", "is_active"):
        if flag in payload:
            patch[flag] = bool(payload.get(flag))
    if "revoked_at" in payload:
        value = payload.get("revoked_at")
        patch["revoked_at"] = str(value).strip() if value else None
    if not patch:
        return jsonify({"error": "Nothing to update."}), 400
    updated = gateway.auth_service.postgrest.request(
        "PATCH",
        "api_tokens",
        payload=patch,
        query={"id": f"eq.{token_id}", "user_id": f"eq.{user['id']}", "select": _TOKEN_FIELDS},
        headers={"Prefer": "return=representation"},
    )
    if not updated:
        return jsonify({"error": "Token not found."}), 404
    return jsonify({"token": updated[0]}), 200


def handle_delete_token(gateway, token_id: str) -> Tuple[Any, int]:
    user = _session_user(gateway.auth_service.postgrest)
    if not user:
        return _unauthorized()
    deleted = gateway.auth_service.postgrest.request(
        "DELETE", "api_tokens", query={"id": f"eq.{token_id}", "user_id": f"eq.{user['id']}", "select": "id"},
        headers={"Prefer": "return=representation"},
    )
    if not deleted:
        return jsonify({"error": "Token not found."}), 404
    return jsonify({"ok": True}), 200
