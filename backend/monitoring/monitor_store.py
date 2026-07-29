from __future__ import annotations

import math
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Mapping

import psycopg2
from psycopg2.extras import Json, RealDictCursor
from psycopg2.pool import ThreadedConnectionPool


TERMINAL_BUCKETS = frozenset({"success", "failure", "cancelled"})
MONITOR_RECENT_TASK_LIMIT = 50
CELERY_TASK_STATES = {
    "task-sent": "SENT",
    "task-received": "RECEIVED",
    "task-started": "STARTED",
    "task-succeeded": "SUCCESS",
    "task-failed": "FAILURE",
    "task-rejected": "REJECTED",
    "task-revoked": "REVOKED",
    "task-retried": "RETRY",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _event_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and math.isfinite(float(value)):
        parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
    else:
        text = str(value or "").strip()
        if not text:
            return _utc_now()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return _utc_now()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json_object(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _json_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple, set)) else []


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return _event_time(value).isoformat()


class MonitorStore:
    """PostgreSQL-backed monitor projections and compact administrator snapshots."""

    def __init__(
        self,
        database_url: str,
        *,
        min_connections: int = 1,
        max_connections: int | None = None,
    ) -> None:
        self.database_url = str(database_url or "").strip()
        if not self.database_url:
            raise RuntimeError("VBIO_MONITOR_DATABASE_URL is required")
        configured_max = _integer(os.environ.get("VBIO_MONITOR_DB_POOL_MAX"), 12)
        pool_max = max(min_connections, max_connections or configured_max)
        timeout = max(1, _integer(os.environ.get("VBIO_MONITOR_DB_CONNECT_TIMEOUT_SECONDS"), 10))
        self._pool = ThreadedConnectionPool(
            minconn=max(1, min_connections),
            maxconn=pool_max,
            dsn=self.database_url,
            connect_timeout=timeout,
            application_name="vbio-monitor",
        )
        self.worker_lease_seconds = max(15, _integer(os.environ.get("VBIO_MONITOR_WORKER_LEASE_SECONDS"), 90))
        self.task_lease_seconds = max(90, _integer(os.environ.get("VBIO_MONITOR_TASK_LEASE_SECONDS"), 180))

    @contextmanager
    def _transaction(self, *, read_only: bool = False) -> Iterator[RealDictCursor]:
        connection = self._pool.getconn()
        try:
            connection.autocommit = False
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("set local role service_role")
                cursor.execute("set local statement_timeout = '10s'")
                if read_only:
                    cursor.execute("set transaction isolation level repeatable read read only")
                yield cursor
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            self._pool.putconn(connection)

    def close(self) -> None:
        self._pool.closeall()

    @staticmethod
    def _event_exists(cursor: RealDictCursor, event_key: str) -> bool:
        cursor.execute("select 1 from public.monitor_events where event_key = %s", (event_key,))
        return cursor.fetchone() is not None

    @staticmethod
    def _record_event(
        cursor: RealDictCursor,
        *,
        event_key: str,
        entity_type: str,
        entity_id: str,
        event_type: str,
        state_bucket: str | None,
        occurred_at: datetime,
        source: str,
        payload: Mapping[str, Any],
    ) -> int | None:
        cursor.execute(
            """
            select public.monitor_record_event(%s, %s, %s, %s, %s, %s, %s, %s) as sequence
            """,
            (
                event_key,
                entity_type,
                entity_id,
                event_type,
                state_bucket,
                occurred_at,
                source,
                Json(dict(payload)),
            ),
        )
        row = cursor.fetchone() or {}
        return _integer(row.get("sequence"), 0) or None

    def apply_event(self, event: Mapping[str, Any]) -> bool:
        kind = str(event.get("kind") or "").strip().lower()
        if kind == "worker":
            return self._apply_worker_event(event)
        if kind in {"task", "task_status", "task_heartbeat"}:
            return self._apply_task_event(event)
        raise ValueError(f"Unsupported monitor event kind: {kind or '<empty>'}")

    def _apply_worker_event(self, event: Mapping[str, Any]) -> bool:
        worker_id = str(event.get("worker_id") or event.get("hostname") or "").strip()
        if not worker_id:
            raise ValueError("Worker monitor event is missing worker_id")
        event_type = str(event.get("event_type") or "worker-heartbeat").strip().lower()
        occurred_at = _event_time(event.get("occurred_at") or event.get("timestamp"))
        event_key = str(event.get("event_key") or f"worker:{worker_id}:{event_type}:{occurred_at.isoformat()}")
        source = str(event.get("source") or "collector")
        metadata = _json_object(event.get("metadata"))
        with self._transaction() as cursor:
            cursor.execute(
                "select * from public.monitor_workers_current where worker_id = %s for update",
                (worker_id,),
            )
            old = cursor.fetchone()
            if old and event_type in {"worker-offline", "worker-shutdown"} and occurred_at < old["last_seen_at"]:
                return False

            old_metadata = _json_object(old.get("metadata")) if old else {}
            old_metadata.update(metadata)
            queues = _json_list(event.get("queues")) or (_json_list(old.get("queues")) if old else [])
            capabilities = _json_list(event.get("capabilities")) or (
                _json_list(old.get("capabilities")) if old else []
            )
            worker_type = str(event.get("worker_type") or (old.get("worker_type") if old else "mixed") or "mixed")
            slots_total = max(0, _integer(event.get("slots_total"), _integer(old.get("slots_total") if old else 0)))
            active_count = max(0, _integer(event.get("active_count"), _integer(old.get("active_count") if old else 0)))
            reserved_count = max(0, _integer(event.get("reserved_count"), _integer(old.get("reserved_count") if old else 0)))
            scheduled_count = max(0, _integer(event.get("scheduled_count"), _integer(old.get("scheduled_count") if old else 0)))
            state = "offline" if event_type in {"worker-offline", "worker-shutdown"} else "online"
            if state == "offline":
                active_count = 0
                reserved_count = 0
                scheduled_count = 0
            slots_busy = min(slots_total, active_count) if slots_total else active_count
            gpu_slots = max(0, _integer(event.get("gpu_slots_total"), slots_total if worker_type == "gpu" else 0))
            cpu_slots = max(0, _integer(event.get("cpu_slots_total"), slots_total if worker_type == "cpu" else 0))
            stats = _json_object(old.get("worker_stats")) if old else {}
            stats.update(_json_object(event.get("worker_stats")))
            counters = _json_object(event.get("executed_by_task_name")) or (
                _json_object(old.get("executed_by_task_name")) if old else {}
            )
            lease_expires = occurred_at if state == "offline" else occurred_at + timedelta(seconds=self.worker_lease_seconds)
            cursor.execute(
                """
                insert into public.monitor_workers_current (
                  worker_id, server, host, worker_type, queues, capabilities,
                  slots_total, slots_busy, slots_idle, gpu_slots_total, cpu_slots_total,
                  active_count, reserved_count, scheduled_count,
                  executed_total_since_start, executed_by_task_name, worker_stats, metadata,
                  state, first_seen_at, last_seen_at, lease_expires_at, updated_at
                ) values (
                  %s, %s, %s, %s, %s, %s,
                  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  %s, %s, %s, %s, now()
                )
                on conflict (worker_id) do update set
                  server = excluded.server, host = excluded.host, worker_type = excluded.worker_type,
                  queues = excluded.queues, capabilities = excluded.capabilities,
                  slots_total = excluded.slots_total, slots_busy = excluded.slots_busy,
                  slots_idle = excluded.slots_idle, gpu_slots_total = excluded.gpu_slots_total,
                  cpu_slots_total = excluded.cpu_slots_total, active_count = excluded.active_count,
                  reserved_count = excluded.reserved_count, scheduled_count = excluded.scheduled_count,
                  executed_total_since_start = excluded.executed_total_since_start,
                  executed_by_task_name = excluded.executed_by_task_name,
                  worker_stats = excluded.worker_stats, metadata = excluded.metadata,
                  state = excluded.state,
                  last_seen_at = greatest(monitor_workers_current.last_seen_at, excluded.last_seen_at),
                  lease_expires_at = excluded.lease_expires_at, updated_at = now()
                """,
                (
                    worker_id,
                    str(event.get("server") or worker_id),
                    str(event.get("host") or worker_id.split("@")[-1]),
                    worker_type,
                    Json(queues),
                    Json(capabilities),
                    slots_total,
                    slots_busy,
                    max(0, slots_total - slots_busy),
                    gpu_slots,
                    cpu_slots,
                    active_count,
                    reserved_count,
                    scheduled_count,
                    max(0, _integer(event.get("executed_total_since_start"), _integer(old.get("executed_total_since_start") if old else 0))),
                    Json(counters),
                    Json(stats),
                    Json(old_metadata),
                    state,
                    occurred_at,
                    occurred_at,
                    lease_expires,
                ),
            )
            changed = old is None or str(old.get("state")) != state
            semantic = changed or event_type in {"worker-online", "worker-ready", "worker-offline", "worker-shutdown"}
            if semantic:
                self._record_event(
                    cursor,
                    event_key=event_key,
                    entity_type="worker",
                    entity_id=worker_id,
                    event_type=event_type,
                    state_bucket=None,
                    occurred_at=occurred_at,
                    source=source,
                    payload={"worker_id": worker_id, "state": state},
                )
            return True

    def _apply_task_event(self, event: Mapping[str, Any]) -> bool:
        task_id = str(event.get("task_id") or event.get("uuid") or "").strip()
        if not task_id:
            raise ValueError("Task monitor event is missing task_id")
        event_type = str(event.get("event_type") or "task-status").strip().lower()
        occurred_at = _event_time(event.get("occurred_at") or event.get("timestamp"))
        event_key = str(event.get("event_key") or f"task:{task_id}:{event_type}:{occurred_at.isoformat()}")
        source = str(event.get("source") or "collector")
        with self._transaction() as cursor:
            cursor.execute(
                "select * from public.monitor_tasks_current where task_id = %s for update",
                (task_id,),
            )
            old = cursor.fetchone()
            if event_type == "task-heartbeat" or str(event.get("kind") or "").lower() == "task_heartbeat":
                if old and old.get("state_bucket") == "running":
                    cursor.execute(
                        """
                        update public.monitor_tasks_current
                           set last_seen_at = greatest(last_seen_at, %s),
                               lease_expires_at = greatest(coalesce(lease_expires_at, %s), %s),
                               updated_at = now()
                         where task_id = %s
                        """,
                        (occurred_at, occurred_at, occurred_at + timedelta(seconds=self.task_lease_seconds), task_id),
                    )
                return bool(old)
            if self._event_exists(cursor, event_key):
                return False

            raw_state = str(event.get("raw_state") or event.get("status") or CELERY_TASK_STATES.get(event_type) or "PENDING").upper()
            cursor.execute("select public.monitor_normalize_task_state(%s) as bucket", (raw_state,))
            bucket = str((cursor.fetchone() or {}).get("bucket") or "other")
            if old:
                old_bucket = str(old.get("state_bucket") or "other")
                if old_bucket in TERMINAL_BUCKETS and bucket not in TERMINAL_BUCKETS:
                    return False
                last_event_at = old.get("last_event_at")
                if last_event_at and occurred_at < last_event_at and bucket != old_bucket:
                    return False
            else:
                old_bucket = ""

            name = str(event.get("name") or (old.get("name") if old else "") or "")
            capability = event.get("capability") if event.get("capability") is not None else (old.get("capability") if old else None)
            worker_id = event.get("worker_id") if event.get("worker_id") is not None else (old.get("worker_id") if old else None)
            queue_name = str(event.get("queue") or (old.get("queue") if old else "") or "")
            status_text = str(event.get("status_text") or event.get("details_text") or raw_state)
            error_text = str(event.get("error_text") or event.get("exception") or "")
            details = _json_object(old.get("details")) if old else {}
            details.update(_json_object(event.get("details")))
            if event.get("slot_state"):
                details["slot_state"] = str(event.get("slot_state"))
            submitted_at = old.get("submitted_at") if old else None
            started_at = old.get("started_at") if old else None
            completed_at = old.get("completed_at") if old else None
            if event_type == "task-sent" or (bucket == "queued" and submitted_at is None):
                submitted_at = occurred_at
            if bucket == "running" and started_at is None:
                started_at = occurred_at
            if bucket in TERMINAL_BUCKETS:
                completed_at = occurred_at
            runtime = _finite_float(event.get("runtime_seconds"))
            if runtime is None and started_at and completed_at:
                runtime = max(0.0, (completed_at - started_at).total_seconds())
            lease_expires = occurred_at + timedelta(seconds=self.task_lease_seconds) if bucket == "running" else None
            state_changed = old is None or str(old.get("raw_state")) != raw_state or old_bucket != bucket
            revision = _integer(old.get("state_revision") if old else 0) + (1 if state_changed else 0)
            cursor.execute(
                """
                insert into public.monitor_tasks_current (
                  task_id, name, capability, queue, worker_id, raw_state, state_bucket,
                  status_text, error_text, details, submitted_at, started_at, completed_at,
                  runtime_seconds, last_seen_at, lease_expires_at, last_event_at,
                  last_event_key, state_revision, updated_at
                ) values (
                  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  %s, %s, %s, %s, %s, %s, now()
                )
                on conflict (task_id) do update set
                  name = case when excluded.name <> '' then excluded.name else monitor_tasks_current.name end,
                  capability = coalesce(excluded.capability, monitor_tasks_current.capability),
                  queue = case when excluded.queue <> '' then excluded.queue else monitor_tasks_current.queue end,
                  worker_id = coalesce(excluded.worker_id, monitor_tasks_current.worker_id),
                  raw_state = excluded.raw_state, state_bucket = excluded.state_bucket,
                  status_text = excluded.status_text,
                  error_text = case when excluded.error_text <> '' then excluded.error_text else monitor_tasks_current.error_text end,
                  details = excluded.details,
                  submitted_at = coalesce(monitor_tasks_current.submitted_at, excluded.submitted_at),
                  started_at = coalesce(monitor_tasks_current.started_at, excluded.started_at),
                  completed_at = coalesce(excluded.completed_at, monitor_tasks_current.completed_at),
                  runtime_seconds = coalesce(excluded.runtime_seconds, monitor_tasks_current.runtime_seconds),
                  last_seen_at = greatest(monitor_tasks_current.last_seen_at, excluded.last_seen_at),
                  lease_expires_at = excluded.lease_expires_at,
                  last_event_at = excluded.last_event_at, last_event_key = excluded.last_event_key,
                  state_revision = excluded.state_revision, updated_at = now()
                """,
                (
                    task_id, name, capability, queue_name, worker_id, raw_state, bucket,
                    status_text, error_text, Json(details), submitted_at, started_at, completed_at,
                    runtime, occurred_at, lease_expires, occurred_at, event_key, revision,
                ),
            )
            if state_changed:
                self._record_event(
                    cursor,
                    event_key=event_key,
                    entity_type="task",
                    entity_id=task_id,
                    event_type=event_type,
                    state_bucket=bucket,
                    occurred_at=occurred_at,
                    source=source,
                    payload={"task_id": task_id, "state": raw_state, "state_bucket": bucket},
                )
            return True

    def reconcile_cluster_snapshot(self, snapshot: Mapping[str, Any]) -> int:
        workers = _json_object(snapshot.get("workers"))
        reconciled = 0
        observed_at = _utc_now()
        marker = observed_at.isoformat()
        for worker_id, raw_worker in workers.items():
            worker = _json_object(raw_worker)
            resources = _json_object(worker.get("resources"))
            counts = _json_object(worker.get("task_counts"))
            counters = _json_object(worker.get("task_counters"))
            self.apply_event({
                "kind": "worker",
                "event_type": "worker-online",
                "event_key": f"reconcile:{marker}:worker:{worker_id}",
                "worker_id": worker_id,
                "server": worker.get("server") or worker_id,
                "host": worker.get("host"),
                "worker_type": worker.get("worker_type"),
                "queues": worker.get("queues"),
                "capabilities": worker.get("capabilities"),
                "slots_total": resources.get("slots_total"),
                "gpu_slots_total": resources.get("gpu_slots_total"),
                "cpu_slots_total": resources.get("cpu_slots_total"),
                "active_count": counts.get("active"),
                "reserved_count": counts.get("reserved"),
                "scheduled_count": counts.get("scheduled"),
                "executed_total_since_start": counters.get("executed_total_since_start"),
                "executed_by_task_name": counters.get("executed_by_task_name"),
                "worker_stats": worker.get("worker_stats"),
                "occurred_at": observed_at,
                "source": "startup_reconciliation",
            })
            for slot_state in ("active", "reserved", "scheduled"):
                tasks = _json_list(_json_object(worker.get("tasks")).get(slot_state))
                for task in tasks:
                    row = _json_object(task)
                    task_id = str(row.get("id") or "").strip()
                    if not task_id:
                        continue
                    raw_state = "STARTED" if slot_state == "active" else "RECEIVED"
                    self.apply_event({
                        "kind": "task",
                        "event_type": "task-started" if slot_state == "active" else "task-received",
                        "event_key": f"reconcile:{marker}:task:{task_id}:{slot_state}",
                        "task_id": task_id,
                        "name": row.get("name"),
                        "capability": row.get("capability"),
                        "queue": row.get("queue"),
                        "worker_id": worker_id,
                        "raw_state": raw_state,
                        "slot_state": slot_state,
                        "runtime_seconds": row.get("runtime_seconds"),
                        "occurred_at": observed_at,
                        "source": "startup_reconciliation",
                    })
            reconciled += 1
        return reconciled

    def expire_leases(self) -> int:
        with self._transaction() as cursor:
            cursor.execute("select public.monitor_expire_leases() as expired")
            return _integer((cursor.fetchone() or {}).get("expired"), 0)

    def latest_sequence(self) -> int:
        with self._transaction(read_only=True) as cursor:
            cursor.execute("select coalesce(max(sequence), 0) as sequence from public.monitor_events")
            return _integer((cursor.fetchone() or {}).get("sequence"), 0)

    @staticmethod
    def _task_brief(row: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        started_at = row.get("started_at")
        runtime = _finite_float(row.get("runtime_seconds"))
        if runtime is None and started_at:
            runtime = max(0.0, (now - _event_time(started_at)).total_seconds())
        return {
            "id": str(row.get("task_id") or ""),
            "name": str(row.get("name") or ""),
            "capability": row.get("capability"),
            "state": str(row.get("raw_state") or ""),
            "queue": str(row.get("queue") or ""),
            "runtime_seconds": runtime,
            "time_start": _iso(started_at),
            "eta": None,
        }

    def get_overview(
        self,
        *,
        window_hours: int = 24,
        recent_limit: int = MONITOR_RECENT_TASK_LIMIT,
    ) -> dict[str, Any]:
        hours = max(1, min(24 * 31, _integer(window_hours, 24)))
        recent = max(1, min(MONITOR_RECENT_TASK_LIMIT, _integer(recent_limit, MONITOR_RECENT_TASK_LIMIT)))
        now = _utc_now()
        with self._transaction(read_only=True) as cursor:
            cursor.execute(
                """
                select * from public.monitor_workers_current
                where state = 'online' and lease_expires_at >= now()
                order by server
                """
            )
            worker_rows = list(cursor.fetchall())
            cursor.execute(
                """
                select * from public.monitor_tasks_current
                where state_bucket in ('queued', 'running')
                  and (state_bucket <> 'running' or lease_expires_at >= now())
                order by updated_at desc
                """
            )
            task_rows = list(cursor.fetchall())
            cursor.execute("select public.monitor_task_statistics(%s, %s) as statistics", (hours, recent))
            statistics = (cursor.fetchone() or {}).get("statistics") or {}
            cursor.execute("select coalesce(max(sequence), 0) as sequence from public.monitor_events")
            sequence = _integer((cursor.fetchone() or {}).get("sequence"), 0)

        tasks_by_worker: dict[str, list[Mapping[str, Any]]] = {}
        for task in task_rows:
            worker_id = str(task.get("worker_id") or "")
            if worker_id:
                tasks_by_worker.setdefault(worker_id, []).append(task)

        workers: dict[str, dict[str, Any]] = {}
        total_slots = total_busy = total_gpu = total_cpu = 0
        for row in worker_rows:
            worker_id = str(row.get("worker_id") or "")
            assigned = tasks_by_worker.get(worker_id, [])
            active = [self._task_brief(task, now) for task in assigned if task.get("state_bucket") == "running"]
            reserved = [self._task_brief(task, now) for task in assigned if task.get("state_bucket") == "queued"]
            slots_total = max(0, _integer(row.get("slots_total")))
            active_count = max(len(active), _integer(row.get("active_count")))
            busy = min(slots_total, active_count) if slots_total else active_count
            gpu_slots = max(0, _integer(row.get("gpu_slots_total")))
            cpu_slots = max(0, _integer(row.get("cpu_slots_total")))
            stats = _json_object(row.get("worker_stats"))
            uptime = max(0, _integer(stats.get("uptime_seconds"), _integer(stats.get("uptime"))))
            total_slots += slots_total
            total_busy += busy
            total_gpu += gpu_slots
            total_cpu += cpu_slots
            workers[worker_id] = {
                "server": str(row.get("server") or worker_id),
                "host": str(row.get("host") or ""),
                "worker_type": str(row.get("worker_type") or "mixed"),
                "queues": _json_list(row.get("queues")),
                "capabilities": _json_list(row.get("capabilities")),
                "resources": {
                    "slots_total": slots_total,
                    "slots_busy": busy,
                    "slots_idle": max(0, slots_total - busy),
                    "gpu_slots_total": gpu_slots,
                    "cpu_slots_total": cpu_slots,
                },
                "utilization": {"slot_utilization": (busy / slots_total) if slots_total else 0.0},
                "tasks": {"active": active, "reserved": reserved, "scheduled": []},
                "tasks_truncated": {"active": False, "reserved": False, "scheduled": False},
                "task_counts": {
                    "active": active_count,
                    "reserved": max(len(reserved), _integer(row.get("reserved_count"))),
                    "scheduled": max(0, _integer(row.get("scheduled_count"))),
                },
                "task_counters": {
                    "executed_total_since_start": max(0, _integer(row.get("executed_total_since_start"))),
                    "executed_by_task_name": _json_object(row.get("executed_by_task_name")),
                },
                "worker_stats": {
                    "uptime_seconds": uptime,
                    "pid": _integer(stats.get("pid")),
                    "clock": _integer(stats.get("clock")),
                },
            }

        capabilities: dict[str, dict[str, Any]] = {}
        for worker_id, worker in workers.items():
            for capability_value in worker["capabilities"]:
                capability = str(capability_value or "").strip()
                if not capability:
                    continue
                item = capabilities.setdefault(capability, {
                    "online": True,
                    "workers": [],
                    "worker_count": 0,
                    "max_running_tasks_upper_bound": 0,
                    "gpu_slots_total": 0,
                    "cpu_slots_total": 0,
                    "active_tasks_count": 0,
                    "reserved_tasks_count": 0,
                    "scheduled_tasks_count": 0,
                    "active_tasks": [],
                    "reserved_tasks": [],
                    "scheduled_tasks": [],
                })
                item["workers"].append(worker_id)
                item["worker_count"] += 1
                item["max_running_tasks_upper_bound"] += worker["resources"]["slots_total"]
                item["gpu_slots_total"] += worker["resources"]["gpu_slots_total"]
                item["cpu_slots_total"] += worker["resources"]["cpu_slots_total"]
                for slot in ("active", "reserved", "scheduled"):
                    matching = [task for task in worker["tasks"][slot] if task.get("capability") in (None, capability)]
                    item[f"{slot}_tasks"].extend(matching)
                    item[f"{slot}_tasks_count"] += len(matching)

        cluster = {
            "generated_at": now.isoformat(),
            "worker_count": len(workers),
            "summary": {
                "workers_total": len(workers),
                "capabilities_total": len(capabilities),
                "capabilities_online": len(capabilities),
                "slots_total": total_slots,
                "slots_busy": total_busy,
                "slots_idle": max(0, total_slots - total_busy),
                "gpu_slots_total": total_gpu,
                "cpu_slots_total": total_cpu,
            },
            "workers": workers,
            "servers": workers,
            "capabilities": capabilities,
        }
        return {
            "sequence": sequence,
            "generated_at": now.isoformat(),
            "cluster": cluster,
            "cluster_error": "",
            "tasks": statistics,
            "tasks_error": "",
        }


def open_listen_connection(database_url: str):
    connection = psycopg2.connect(
        str(database_url or "").strip(),
        connect_timeout=max(1, _integer(os.environ.get("VBIO_MONITOR_DB_CONNECT_TIMEOUT_SECONDS"), 10)),
        application_name="vbio-monitor-listener",
    )
    connection.set_session(autocommit=True)
    return connection
