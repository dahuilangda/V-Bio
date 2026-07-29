from __future__ import annotations

import json
import logging
import os
import queue
import signal
import socket
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Mapping

from redis.exceptions import ResponseError

from backend.core.celery_app import celery_app
from backend.monitoring.event_transport import MONITOR_STREAM_KEY
from backend.monitoring.migration_runner import apply_migrations
from backend.monitoring.monitor_store import MonitorStore
from backend.scheduling.capability_router import build_worker_capability_snapshot, parse_capability_queue
from gpu_manager import get_redis_client


LOGGER = logging.getLogger(__name__)
WORKER_EVENT_TYPES = frozenset({"worker-online", "worker-heartbeat", "worker-offline"})
TASK_EVENT_TYPES = frozenset({
    "task-sent", "task-received", "task-started", "task-succeeded",
    "task-failed", "task-rejected", "task-revoked", "task-retried",
})


def _bounded_text(value: Any, limit: int = 12_000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def _event_datetime(value: Any) -> str:
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _queue_capability(queue_name: str) -> str | None:
    token = str(queue_name or "").strip().lower()
    fragments = token.split(".")
    if len(fragments) == 3 and fragments[0] == "cap" and fragments[1]:
        return fragments[1]
    parsed = parse_capability_queue(queue_name)
    return parsed[0] if parsed else None


def normalize_celery_event(event: Mapping[str, Any]) -> dict[str, Any] | None:
    event_type = str(event.get("type") or "").strip().lower()
    clock = str(event.get("clock") or "0")
    occurred_at = _event_datetime(event.get("timestamp"))
    if event_type in WORKER_EVENT_TYPES:
        worker_id = str(event.get("hostname") or "").strip()
        if not worker_id:
            return None
        return {
            "kind": "worker",
            "event_type": event_type,
            "event_key": f"celery:{event_type}:{worker_id}:{clock}",
            "worker_id": worker_id,
            "host": worker_id.split("@")[-1],
            "active_count": event.get("active"),
            "executed_total_since_start": event.get("processed"),
            "worker_stats": {
                "clock": event.get("clock"),
                "loadavg": event.get("loadavg"),
                "pid": event.get("pid"),
            },
            "metadata": {
                "software": event.get("sw_ident"),
                "software_version": event.get("sw_ver"),
                "operating_system": event.get("sw_sys"),
            },
            "occurred_at": occurred_at,
            "source": "celery",
        }
    if event_type in TASK_EVENT_TYPES:
        task_id = str(event.get("uuid") or "").strip()
        if not task_id:
            return None
        queue_name = str(event.get("queue") or event.get("routing_key") or "")
        parsed_capability = _queue_capability(queue_name)
        return {
            "kind": "task",
            "event_type": event_type,
            "event_key": f"celery:{event_type}:{task_id}:{clock}",
            "task_id": task_id,
            "name": str(event.get("name") or ""),
            "worker_id": event.get("hostname"),
            "queue": queue_name,
            "capability": parsed_capability,
            "runtime_seconds": event.get("runtime"),
            "exception": _bounded_text(event.get("exception")),
            "details": {"result": _bounded_text(event.get("result"), 2_000)} if event_type == "task-succeeded" else {},
            "occurred_at": occurred_at,
            "source": "celery",
        }
    return None


class MonitorCollector:
    def __init__(self, store: MonitorStore, redis_client: Any) -> None:
        self.store = store
        self.redis = redis_client
        self.stream_key = MONITOR_STREAM_KEY
        self.group = os.environ.get("VBIO_MONITOR_CONSUMER_GROUP", "vbio-monitor-postgres").strip()
        self.consumer = os.environ.get(
            "VBIO_MONITOR_CONSUMER_NAME",
            f"{socket.gethostname()}-{os.getpid()}",
        ).strip()
        self.lease_sweep_seconds = max(5, int(os.environ.get("VBIO_MONITOR_LEASE_SWEEP_SECONDS", "15")))
        self.heartbeat_write_seconds = max(5, int(os.environ.get("VBIO_MONITOR_HEARTBEAT_WRITE_SECONDS", "15")))
        self.reclaim_idle_ms = max(5_000, int(os.environ.get("VBIO_MONITOR_RECLAIM_IDLE_MS", "60000")))
        self._events: queue.Queue[dict[str, Any]] = queue.Queue(
            maxsize=max(1_000, int(os.environ.get("VBIO_MONITOR_EVENT_QUEUE_SIZE", "20000")))
        )
        self._retry: deque[tuple[str, Mapping[Any, Any]]] = deque()
        self._stop = threading.Event()
        self._receiver_thread: threading.Thread | None = None
        self._last_worker_heartbeat: dict[str, float] = {}

    def stop(self, *_args: Any) -> None:
        self._stop.set()

    def _ensure_consumer_group(self) -> None:
        try:
            self.redis.xgroup_create(self.stream_key, self.group, id="0-0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def _on_celery_event(self, raw_event: Mapping[str, Any]) -> None:
        normalized = normalize_celery_event(raw_event)
        if normalized is None:
            return
        if normalized.get("event_type") == "worker-heartbeat":
            worker_id = str(normalized.get("worker_id") or "")
            now = time.monotonic()
            previous = self._last_worker_heartbeat.get(worker_id, 0.0)
            if now - previous < self.heartbeat_write_seconds:
                return
            self._last_worker_heartbeat[worker_id] = now
        self._events.put(normalized, timeout=5)

    def _receive_celery_events(self) -> None:
        wakeup = True
        while not self._stop.is_set():
            try:
                with celery_app.connection_for_read() as connection:
                    receiver = celery_app.events.Receiver(
                        connection,
                        handlers={"*": self._on_celery_event},
                        app=celery_app,
                    )
                    should_wakeup = wakeup
                    wakeup = False
                    receiver.capture(limit=None, timeout=None, wakeup=should_wakeup)
            except Exception:
                if not self._stop.is_set():
                    LOGGER.exception("Celery monitor event receiver disconnected")
                    self._stop.wait(2.0)

    def _start_receiver(self) -> None:
        self._receiver_thread = threading.Thread(
            target=self._receive_celery_events,
            name="vbio-celery-event-receiver",
            daemon=True,
        )
        self._receiver_thread.start()

    @staticmethod
    def _decode_stream_message(message_id: Any, fields: Mapping[Any, Any]) -> tuple[str, dict[str, Any]]:
        stream_id = message_id.decode() if isinstance(message_id, bytes) else str(message_id)
        raw_payload = fields.get(b"payload") if b"payload" in fields else fields.get("payload")
        if isinstance(raw_payload, bytes):
            raw_payload = raw_payload.decode("utf-8")
        payload = json.loads(str(raw_payload or "{}"))
        if not isinstance(payload, dict):
            raise ValueError(f"Redis monitor stream entry {stream_id} is not a JSON object")
        payload["event_key"] = f"redis:{stream_id}"
        payload.setdefault("source", "worker_stream")
        return stream_id, payload

    def _apply_stream_message(self, message_id: Any, fields: Mapping[Any, Any]) -> None:
        stream_id, event = self._decode_stream_message(message_id, fields)
        self.store.apply_event(event)
        self.redis.xack(self.stream_key, self.group, stream_id)

    def _claim_pending(self) -> None:
        result = self.redis.xautoclaim(
            self.stream_key,
            self.group,
            self.consumer,
            min_idle_time=self.reclaim_idle_ms,
            start_id="0-0",
            count=100,
        )
        messages = result[1] if isinstance(result, (list, tuple)) and len(result) > 1 else []
        for message_id, fields in messages:
            self._retry.append((message_id, fields))

    def _read_stream(self) -> None:
        result = self.redis.xreadgroup(
            self.group,
            self.consumer,
            {self.stream_key: ">"},
            count=100,
            block=250,
        )
        for _stream, messages in result or []:
            for message_id, fields in messages:
                self._retry.append((message_id, fields))

    def run_forever(self) -> None:
        self._ensure_consumer_group()
        try:
            snapshot = build_worker_capability_snapshot(celery_app=celery_app)
            reconciled = self.store.reconcile_cluster_snapshot(snapshot)
            LOGGER.info("Startup reconciliation persisted %s workers", reconciled)
        except Exception:
            LOGGER.exception("One-time worker reconciliation failed; collector will continue from events")
        self._claim_pending()
        self._start_receiver()
        next_lease_sweep = time.monotonic()
        next_reclaim = time.monotonic() + (self.reclaim_idle_ms / 1000.0)
        while not self._stop.is_set():
            try:
                while self._retry:
                    message_id, fields = self._retry[0]
                    self._apply_stream_message(message_id, fields)
                    self._retry.popleft()
                for _index in range(100):
                    try:
                        event = self._events.get_nowait()
                    except queue.Empty:
                        break
                    self.store.apply_event(event)
                self._read_stream()
                now = time.monotonic()
                if now >= next_lease_sweep:
                    expired = self.store.expire_leases()
                    if expired:
                        LOGGER.info("Expired %s monitor leases", expired)
                    next_lease_sweep = now + self.lease_sweep_seconds
                if now >= next_reclaim:
                    self._claim_pending()
                    next_reclaim = now + (self.reclaim_idle_ms / 1000.0)
            except Exception:
                LOGGER.exception("Monitor collector iteration failed")
                self._stop.wait(2.0)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("VBIO_MONITOR_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    database_url = os.environ.get("VBIO_MONITOR_DATABASE_URL", "").strip()
    migration_database_url = os.environ.get("VBIO_MONITOR_MIGRATION_DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("VBIO_MONITOR_DATABASE_URL is required")
    if not migration_database_url:
        raise RuntimeError("VBIO_MONITOR_MIGRATION_DATABASE_URL is required")
    apply_migrations(migration_database_url)
    store = MonitorStore(database_url, min_connections=1, max_connections=4)
    collector = MonitorCollector(store, get_redis_client())
    signal.signal(signal.SIGTERM, collector.stop)
    signal.signal(signal.SIGINT, collector.stop)
    try:
        collector.run_forever()
    finally:
        store.close()


if __name__ == "__main__":
    main()
