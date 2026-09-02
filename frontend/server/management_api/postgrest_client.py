from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, Optional

import requests


def _sign_jwt_hs256(secret: str, claims: Dict[str, Any]) -> str:
    header = {"alg": "HS256", "typ": "JWT"}

    def b64(obj: Dict[str, Any]) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    signing_input = f"{b64(header)}.{b64(claims)}"
    signature = hmac.new(secret.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256).digest()
    return f"{signing_input}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode('ascii')}"


class PostgrestClient:
    """PostgREST client.

    When PGRST_JWT_SECRET is configured (server-only secret, shared with the PostgREST
    container), every request runs as `service_role` via a short-lived signed JWT —
    distinguishable from anonymous browser traffic, which is what lets the sensitive
    tables (app_users, api_tokens) drop anonymous access entirely.
    """

    def __init__(
        self,
        *,
        base_url: str,
        apikey: str,
        timeout_seconds: float,
        session: requests.Session,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.apikey = apikey.strip()
        self._jwt_secret = str(os.environ.get("PGRST_JWT_SECRET") or "").strip()
        self.timeout_seconds = float(timeout_seconds)
        self.session = session

    def headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self._jwt_secret:
            now = int(time.time())
            headers["Authorization"] = f"Bearer {_sign_jwt_hs256(self._jwt_secret, {'role': 'service_role', 'iat': now, 'exp': now + 300})}"
        elif self.apikey:
            headers["apikey"] = self.apikey
            headers["Authorization"] = f"Bearer {self.apikey}"
        if extra:
            headers.update(extra)
        return headers

    def request(
        self,
        method: str,
        table_or_view: str,
        *,
        query: Optional[Dict[str, str]] = None,
        payload: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        expect_json: bool = True,
    ) -> Any:
        url = f"{self.base_url}/{table_or_view.lstrip('/')}"
        response = self.session.request(
            method,
            url,
            params=query,
            json=payload,
            headers=self.headers(headers),
            timeout=self.timeout_seconds,
        )
        if not response.ok:
            text = response.text.strip()
            raise RuntimeError(f"PostgREST {response.status_code}: {text}")
        if not expect_json or response.status_code == 204:
            return None
        if not response.content:
            return None
        return response.json()

