from __future__ import annotations

from typing import Any, Dict, Optional

import requests


class PostgrestClient:
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
        self.timeout_seconds = float(timeout_seconds)
        self.session = session

    def headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self.apikey:
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

