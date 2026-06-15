from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class JwtClient:
    client_id: str
    name: str
    secret: str
    issuer: str
    audience: str
    max_ttl_seconds: int
    active: bool
    created_at: str
    updated_at: str

    def public_dict(self) -> Dict[str, Any]:
        return {
            "client_id": self.client_id,
            "name": self.name,
            "issuer": self.issuer,
            "audience": self.audience,
            "max_ttl_seconds": self.max_ttl_seconds,
            "active": self.active,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class JwtClientStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def list_clients(self) -> List[JwtClient]:
        return [self._from_row(row) for row in self._read_rows()]

    def get_client(self, client_id: str) -> Optional[JwtClient]:
        normalized = str(client_id or "").strip()
        if not normalized:
            return None
        for client in self.list_clients():
            if client.client_id == normalized:
                return client
        return None

    def create_client(
        self,
        *,
        name: str,
        issuer: str,
        audience: str,
        max_ttl_seconds: int,
    ) -> tuple[JwtClient, str]:
        now = _utc_now_iso()
        client_id = f"jwt_{secrets.token_urlsafe(12).replace('-', '').replace('_', '')[:18]}"
        secret = secrets.token_urlsafe(48)
        client = JwtClient(
            client_id=client_id,
            name=str(name or "").strip() or client_id,
            secret=secret,
            issuer=str(issuer or "navigation").strip() or "navigation",
            audience=str(audience or "vbio").strip() or "vbio",
            max_ttl_seconds=max(60, min(3600, int(max_ttl_seconds or 300))),
            active=True,
            created_at=now,
            updated_at=now,
        )
        rows = self._read_rows()
        rows.append(self._to_row(client))
        self._write_rows(rows)
        return client, secret

    def update_client(self, client_id: str, patch: Dict[str, Any]) -> JwtClient:
        rows = self._read_rows()
        for index, row in enumerate(rows):
            if str(row.get("client_id") or "") != client_id:
                continue
            row = dict(row)
            if "name" in patch:
                row["name"] = str(patch.get("name") or "").strip() or row.get("name") or client_id
            if "issuer" in patch:
                row["issuer"] = str(patch.get("issuer") or "").strip() or "navigation"
            if "audience" in patch:
                row["audience"] = str(patch.get("audience") or "").strip() or "vbio"
            if "max_ttl_seconds" in patch:
                row["max_ttl_seconds"] = max(60, min(3600, int(patch.get("max_ttl_seconds") or 300)))
            if "active" in patch:
                row["active"] = bool(patch.get("active"))
            row["updated_at"] = _utc_now_iso()
            rows[index] = row
            self._write_rows(rows)
            return self._from_row(row)
        raise KeyError("JWT client not found")

    def rotate_secret(self, client_id: str) -> tuple[JwtClient, str]:
        rows = self._read_rows()
        for index, row in enumerate(rows):
            if str(row.get("client_id") or "") != client_id:
                continue
            secret = secrets.token_urlsafe(48)
            row = dict(row)
            row["secret"] = secret
            row["updated_at"] = _utc_now_iso()
            rows[index] = row
            self._write_rows(rows)
            return self._from_row(row), secret
        raise KeyError("JWT client not found")

    def delete_client(self, client_id: str) -> None:
        rows = self._read_rows()
        next_rows = [row for row in rows if str(row.get("client_id") or "") != client_id]
        if len(next_rows) == len(rows):
            raise KeyError("JWT client not found")
        self._write_rows(next_rows)

    def _read_rows(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text())
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict)]

    def _write_rows(self, rows: List[Dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(rows, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(tmp_name, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    @staticmethod
    def _from_row(row: Dict[str, Any]) -> JwtClient:
        return JwtClient(
            client_id=str(row.get("client_id") or ""),
            name=str(row.get("name") or ""),
            secret=str(row.get("secret") or ""),
            issuer=str(row.get("issuer") or "navigation"),
            audience=str(row.get("audience") or "vbio"),
            max_ttl_seconds=int(row.get("max_ttl_seconds") or 300),
            active=bool(row.get("active", True)),
            created_at=str(row.get("created_at") or ""),
            updated_at=str(row.get("updated_at") or ""),
        )

    @staticmethod
    def _to_row(client: JwtClient) -> Dict[str, Any]:
        return {
            "client_id": client.client_id,
            "name": client.name,
            "secret": client.secret,
            "issuer": client.issuer,
            "audience": client.audience,
            "max_ttl_seconds": client.max_ttl_seconds,
            "active": client.active,
            "created_at": client.created_at,
            "updated_at": client.updated_at,
        }


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
