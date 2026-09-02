from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from management_api.postgrest_client import PostgrestClient


def _platform_token() -> str:
    return str(os.environ.get("VBIO_RUNTIME_API_TOKEN", "") or "").strip()


def _is_platform_token(token_plain: str) -> bool:
    expected = _platform_token()
    return bool(expected) and bool(token_plain) and hmac.compare_digest(token_plain, expected)


@dataclass
class TokenContext:
    token_id: str
    user_id: str
    name: str
    project_id: Optional[str]
    allow_submit: bool
    allow_delete: bool
    allow_cancel: bool

    @property
    def is_platform(self) -> bool:
        """The deployment's shared runtime token: trusted cross-project (the exact trust
        level the runtime backend itself granted before it moved behind the gateway)."""
        return self.token_id == "platform"


# The deployment's shared runtime token (also baked into the SPA bundle). When presented to
# the GATEWAY it identifies the trusted platform caller — the same trust level the runtime
# backend itself granted before it was pulled behind the gateway. Per-project API tokens
# keep their stricter project-bound semantics when presented instead.
PLATFORM_TOKEN_CONTEXT = TokenContext(
    token_id="platform",
    user_id="platform",
    name="platform runtime token",
    project_id=None,
    allow_submit=True,
    allow_delete=True,
    allow_cancel=True,
)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class AuthService:
    def __init__(self, postgrest: PostgrestClient) -> None:
        self.postgrest = postgrest

    def validate_token(self, token_plain: str) -> TokenContext:
        if not token_plain:
            raise PermissionError("Missing X-API-Token header")
        if _is_platform_token(token_plain):
            return PLATFORM_TOKEN_CONTEXT

        token_hash = hashlib.sha256(token_plain.encode("utf-8")).hexdigest()
        rows = self.postgrest.request(
            "GET",
            "api_tokens",
            query={
                "select": "id,user_id,name,project_id,allow_submit,allow_delete,allow_cancel,is_active,revoked_at,expires_at",
                "token_hash": f"eq.{token_hash}",
                "limit": "1",
            },
        )
        if not rows:
            raise PermissionError("Invalid API token")

        row = rows[0]
        if not bool(row.get("is_active", False)):
            raise PermissionError("API token is inactive")
        if row.get("revoked_at"):
            raise PermissionError("API token was revoked")

        expires_at = _parse_iso(row.get("expires_at"))
        if expires_at and expires_at <= datetime.now(timezone.utc):
            raise PermissionError("API token expired")

        return TokenContext(
            token_id=str(row.get("id") or ""),
            user_id=str(row.get("user_id") or ""),
            name=str(row.get("name") or ""),
            project_id=str(row.get("project_id") or "").strip() or None,
            allow_submit=bool(row.get("allow_submit", False)),
            allow_delete=bool(row.get("allow_delete", False)),
            allow_cancel=bool(row.get("allow_cancel", False)),
        )

    def ensure_project_exists(self, project_id: str) -> None:
        rows = self.postgrest.request(
            "GET",
            "projects",
            query={
                "select": "id",
                "id": f"eq.{project_id}",
                "deleted_at": "is.null",
                "limit": "1",
            },
        )
        if not rows:
            raise PermissionError("Unknown project_id; create project in V-Bio web first")

    def authorize_submit(self, project_id: str, token_plain: str) -> TokenContext:
        token = self.validate_token(token_plain)
        if token.is_platform:
            return token
        if not token.allow_submit:
            raise PermissionError("This token does not allow submit")
        if not token.project_id:
            raise PermissionError("This token is not bound to a project")
        if token.project_id != project_id:
            raise PermissionError("Token project_id does not match submitted project_id")
        self.ensure_project_exists(project_id)
        return token

    def authorize_task_action(self, project_id: str, token_plain: str, *, require_delete: bool) -> TokenContext:
        token = self.validate_token(token_plain)
        if token.is_platform:
            return token
        if require_delete:
            if not token.allow_delete:
                raise PermissionError("This token does not allow delete")
        else:
            if not token.allow_cancel:
                raise PermissionError("This token does not allow cancel")
        if not token.project_id:
            raise PermissionError("This token is not bound to a project")
        if token.project_id != project_id:
            raise PermissionError("Token project_id does not match request project_id")
        return token

    def authorize_project_read(self, project_id: str, token_plain: str) -> TokenContext:
        token = self.validate_token(token_plain)
        if token.is_platform:
            return token
        if not token.project_id:
            raise PermissionError("This token is not bound to a project")
        if token.project_id != project_id:
            raise PermissionError("Token project_id does not match request project_id")
        return token

    def authorize_quick_project_action(
        self,
        token_plain: str,
        project_id: Optional[str],
        *,
        require_submit: bool,
    ) -> TokenContext:
        token = self.validate_token(token_plain)
        if token.is_platform:
            return token
        if require_submit and not token.allow_submit:
            raise PermissionError("This token does not allow submit")
        if not token.project_id:
            raise PermissionError("This token is not bound to a project")
        if project_id and token.project_id != project_id:
            raise PermissionError("Token project_id does not match request project_id")
        if project_id:
            self.ensure_project_exists(project_id)
        return token
