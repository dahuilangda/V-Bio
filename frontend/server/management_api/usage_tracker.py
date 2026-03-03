from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from flask import Response, jsonify, request

from management_api.auth_service import TokenContext
from management_api.postgrest_client import PostgrestClient


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class UsageTracker:
    def __init__(self, postgrest: PostgrestClient, logger: Any) -> None:
        self.postgrest = postgrest
        self.logger = logger

    def record_usage(
        self,
        token: Optional[TokenContext],
        *,
        action: str,
        status_code: int,
        succeeded: bool,
        started_at: float,
        project_id: Optional[str],
        task_id: Optional[str],
    ) -> None:
        duration_ms = int(max(0.0, (time.perf_counter() - started_at) * 1000.0))

        meta: Dict[str, Any] = {}
        if project_id:
            meta["project_id"] = project_id
        if task_id:
            meta["task_id"] = task_id

        payload = {
            "token_id": token.token_id if token else None,
            "user_id": token.user_id if token else None,
            "method": request.method,
            "path": request.path,
            "action": action,
            "status_code": int(status_code),
            "succeeded": bool(succeeded),
            "duration_ms": duration_ms,
            "client": (request.headers.get("User-Agent") or "")[:255],
            "meta": meta,
        }

        try:
            self.postgrest.request(
                "POST",
                "api_token_usage",
                payload=payload,
                headers={"Prefer": "return=minimal"},
                expect_json=False,
            )
            if token and succeeded:
                self.postgrest.request(
                    "PATCH",
                    "api_tokens",
                    query={"id": f"eq.{token.token_id}"},
                    payload={"last_used_at": _now_iso()},
                    headers={"Prefer": "return=minimal"},
                    expect_json=False,
                )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Failed to record API token usage: %s", exc)

    def forbidden(
        self,
        message: str,
        token: Optional[TokenContext],
        action: str,
        started_at: float,
        project_id: Optional[str],
    ) -> Tuple[Response, int]:
        self.record_usage(
            token,
            action=action,
            status_code=403,
            succeeded=False,
            started_at=started_at,
            project_id=project_id,
            task_id=None,
        )
        return jsonify({"error": message}), 403
