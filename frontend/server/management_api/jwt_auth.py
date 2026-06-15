from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from typing import Any, Dict, Optional

from management_api.postgrest_client import PostgrestClient
from management_api.jwt_clients import JwtClient, JwtClientStore


class JwtTokenError(ValueError):
    pass



def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _signing_input(header: str, payload: str) -> bytes:
    return f"{header}.{payload}".encode("ascii")


def decode_login_jwt(token: str, client_store: JwtClientStore) -> Dict[str, Any]:
    parts = str(token or "").strip().split(".")
    if len(parts) != 3:
        raise JwtTokenError("Invalid JWT token format")

    header_part, payload_part, signature_part = parts
    try:
        header = json.loads(_b64url_decode(header_part).decode("utf-8"))
        payload = json.loads(_b64url_decode(payload_part).decode("utf-8"))
    except Exception as exc:
        raise JwtTokenError("Invalid JWT token payload") from exc

    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise JwtTokenError("Invalid JWT token payload")
    if str(header.get("alg") or "") != "HS256":
        raise JwtTokenError("Unsupported JWT token algorithm")

    client_id = str(header.get("kid") or payload.get("client_id") or "").strip()
    client = client_store.get_client(client_id)
    if not client or not client.active:
        raise JwtTokenError("JWT client is not active")

    _verify_signature(client, header_part, payload_part, signature_part)
    _validate_claims(payload, client)
    return payload


def _verify_signature(client: JwtClient, header_part: str, payload_part: str, signature_part: str) -> None:
    expected_signature = hmac.new(
        client.secret.encode("utf-8"),
        _signing_input(header_part, payload_part),
        hashlib.sha256,
    ).digest()
    try:
        provided_signature = _b64url_decode(signature_part)
    except Exception as exc:
        raise JwtTokenError("Invalid JWT token signature") from exc
    if not hmac.compare_digest(expected_signature, provided_signature):
        raise JwtTokenError("Invalid JWT token signature")


def _validate_claims(payload: Dict[str, Any], client: JwtClient) -> None:
    now = int(time.time())
    exp = _read_int(payload.get("exp"))
    iat = _read_int(payload.get("iat"))
    nbf = _read_int(payload.get("nbf"))
    if exp is None or exp <= now:
        raise JwtTokenError("JWT token expired")
    if nbf is not None and nbf > now + 30:
        raise JwtTokenError("JWT token is not active yet")
    if iat is not None and iat > now + 30:
        raise JwtTokenError("JWT token issued-at is in the future")
    if iat is not None and client.max_ttl_seconds > 0 and exp - iat > client.max_ttl_seconds:
        raise JwtTokenError("JWT token lifetime is too long")
    if client.issuer and str(payload.get("iss") or "") != client.issuer:
        raise JwtTokenError("JWT token issuer is not accepted")
    if client.audience and not _audience_matches(payload.get("aud"), client.audience):
        raise JwtTokenError("JWT token audience is not accepted")


def _read_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _audience_matches(value: Any, expected: str) -> bool:
    if isinstance(value, str):
        return value == expected
    if isinstance(value, list):
        return expected in {str(item) for item in value}
    return False


def _normalize_username(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip().lower()).strip("-._")
    if len(cleaned) >= 3:
        return cleaned[:64]
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]
    return f"user-{digest}"


def _session_from_user(row: Dict[str, Any], *, login_at: str) -> Dict[str, Any]:
    return {
        "userId": str(row.get("id") or ""),
        "username": str(row.get("username") or ""),
        "name": str(row.get("name") or ""),
        "email": row.get("email"),
        "avatarUrl": row.get("avatar_url"),
        "isAdmin": bool(row.get("is_admin")),
        "loginAt": login_at,
        "authProvider": "jwt",
    }


class JwtUserService:
    def __init__(self, postgrest: PostgrestClient) -> None:
        self.postgrest = postgrest

    def upsert_user_from_claims(self, claims: Dict[str, Any]) -> Dict[str, Any]:
        email = str(claims.get("email") or "").strip().lower() or None
        username_source = str(
            claims.get("username")
            or claims.get("preferred_username")
            or email
            or claims.get("sub")
            or ""
        ).strip()
        if not username_source:
            raise JwtTokenError("JWT token must include username, email, or sub")
        username = _normalize_username(username_source)
        name = str(claims.get("name") or username_source or username).strip() or username
        avatar_url = str(claims.get("avatar_url") or claims.get("picture") or "").strip() or None
        login_at = _utc_now_iso()

        existing = self._find_existing_user(email=email, username=username)
        if existing:
            payload = {
                "username": existing.get("username") or username,
                "name": name,
                "email": email or existing.get("email"),
                "avatar_url": avatar_url,
                "last_login_at": login_at,
                "deleted_at": None,
            }
            updated = self.postgrest.request(
                "PATCH",
                "app_users",
                query={"id": f"eq.{existing['id']}", "select": "*"},
                payload=payload,
                headers={"Prefer": "return=representation"},
            )
            return _session_from_user(updated[0], login_at=login_at)

        created = self.postgrest.request(
            "POST",
            "app_users",
            query={"select": "*"},
            payload={
                "username": username,
                "name": name,
                "email": email,
                "avatar_url": avatar_url,
                "password_hash": "",
                "is_admin": False,
                "last_login_at": login_at,
            },
            headers={"Prefer": "return=representation"},
        )
        return _session_from_user(created[0], login_at=login_at)

    def _find_existing_user(self, *, email: Optional[str], username: str) -> Optional[Dict[str, Any]]:
        if email:
            rows = self.postgrest.request(
                "GET",
                "app_users",
                query={"select": "*", "email": f"eq.{email}", "limit": "1"},
            )
            if rows:
                return rows[0]
        rows = self.postgrest.request(
            "GET",
            "app_users",
            query={"select": "*", "username": f"eq.{username}", "limit": "1"},
        )
        return rows[0] if rows else None


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
