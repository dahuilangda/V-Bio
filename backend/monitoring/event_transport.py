from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timezone
from typing import Any, Mapping

import redis

from backend.core import config


MONITOR_STREAM_KEY = os.environ.get("VBIO_MONITOR_STREAM_KEY", "vbio:monitor:events").strip()
MONITOR_STREAM_MAXLEN = max(10_000, int(os.environ.get("VBIO_MONITOR_STREAM_MAXLEN", "200000")))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def worker_identity() -> str:
    return str(
        os.environ.get("CELERY_WORKER_NAME")
        or os.environ.get("CELERY_WORKER_HOSTNAME")
        or os.environ.get("HOSTNAME")
        or socket.gethostname()
    ).strip()


def publish_monitor_event(redis_client: Any, event: Mapping[str, Any]) -> str:
    payload = dict(event)
    payload.setdefault("occurred_at", utc_now_iso())
    message_id = redis_client.xadd(
        MONITOR_STREAM_KEY,
        {"payload": json.dumps(payload, ensure_ascii=True, separators=(",", ":"), default=str)},
        maxlen=MONITOR_STREAM_MAXLEN,
        approximate=True,
    )
    if isinstance(message_id, bytes):
        return message_id.decode("utf-8", errors="replace")
    return str(message_id)


def publish_task_status(
    redis_client: Any,
    *,
    task_id: str,
    status: str,
    details_text: str = "",
    details: Mapping[str, Any] | None = None,
) -> str:
    return publish_monitor_event(redis_client, {
        "kind": "task_status",
        "event_type": "task-status",
        "task_id": str(task_id),
        "worker_id": worker_identity(),
        "status": str(status),
        "details_text": str(details_text or ""),
        "details": dict(details or {}),
    })


def publish_task_heartbeat(redis_client: Any, *, task_id: str) -> str:
    return publish_monitor_event(redis_client, {
        "kind": "task_heartbeat",
        "event_type": "task-heartbeat",
        "task_id": str(task_id),
        "worker_id": worker_identity(),
    })


def publish_worker_metadata(
    *,
    event_type: str,
    worker_id: str,
    slots_total: int = 0,
) -> str:
    capabilities_raw = (
        os.environ.get("GPU_WORKER_CAPABILITIES")
        or os.environ.get("CPU_WORKER_CAPABILITIES")
        or os.environ.get("WORKER_CAPABILITIES")
        or ""
    )
    capabilities = sorted({item.strip().lower() for item in capabilities_raw.split(",") if item.strip()})
    if os.environ.get("GPU_WORKER_CAPABILITIES"):
        worker_type = "gpu"
    elif os.environ.get("CPU_WORKER_CAPABILITIES"):
        worker_type = "cpu"
    else:
        worker_type = "mixed"
    client = redis.Redis.from_url(config.REDIS_URL, decode_responses=False)
    return publish_monitor_event(client, {
        "kind": "worker",
        "event_type": event_type,
        "worker_id": str(worker_id),
        "host": str(worker_id).split("@")[-1],
        "worker_type": worker_type,
        "capabilities": capabilities,
        "slots_total": max(0, int(slots_total or 0)),
        "source": "worker",
    })
