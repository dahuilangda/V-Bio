from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import os
import threading
import time
from typing import Any, Dict, List, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - production runs on Linux.
    fcntl = None

from management_api.admin_monitor import build_task_statistics
from management_api.postgrest_client import PostgrestClient


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProjectTaskStore:
    def __init__(self, postgrest: PostgrestClient) -> None:
        self.postgrest = postgrest
        self._alias_lock = threading.Lock()
        self._task_alias_ttl_seconds = 24 * 60 * 60
        self._task_alias_max_entries = 20000
        self._task_aliases: Dict[str, Dict[str, Any]] = {}
        try:
            self._admin_statistics_cache_ttl_seconds = max(
                5.0,
                min(300.0, float(os.environ.get("VBIO_ADMIN_STATS_CACHE_TTL_SECONDS", "60"))),
            )
        except (TypeError, ValueError):
            self._admin_statistics_cache_ttl_seconds = 60.0
        self._admin_statistics_cache_file = os.environ.get(
            "VBIO_ADMIN_STATS_CACHE_FILE",
            "/tmp/vbio-admin-statistics-cache.json",
        )
        self._admin_statistics_cache_lock = threading.Lock()
        self._admin_statistics_cache: Dict[
            tuple[int, int, int],
            tuple[float, Dict[str, Any]],
        ] = {}

    def _cleanup_task_aliases_locked(self, now: Optional[float] = None) -> None:
        current = float(now if now is not None else time.time())
        expired = [
            key
            for key, payload in self._task_aliases.items()
            if float(payload.get("expires_at") or 0.0) <= current
        ]
        for key in expired:
            self._task_aliases.pop(key, None)
        if len(self._task_aliases) <= self._task_alias_max_entries:
            return
        overflow = len(self._task_aliases) - self._task_alias_max_entries
        if overflow <= 0:
            return
        ordered = sorted(
            self._task_aliases.items(),
            key=lambda item: float(item[1].get("updated_at") or 0.0),
        )
        for key, _ in ordered[:overflow]:
            self._task_aliases.pop(key, None)

    def remember_task_alias(self, project_id: str, task_id: str) -> None:
        normalized_project_id = str(project_id or "").strip()
        normalized_task_id = str(task_id or "").strip()
        if not normalized_project_id or not normalized_task_id:
            return
        now = time.time()
        with self._alias_lock:
            self._cleanup_task_aliases_locked(now)
            self._task_aliases[normalized_task_id] = {
                "project_id": normalized_project_id,
                "task_id": normalized_task_id,
                "updated_at": now,
                "expires_at": now + float(self._task_alias_ttl_seconds),
            }

    def insert_snapshot(
        self,
        *,
        project_id: str,
        task_id: str,
        task_name: str,
        task_summary: str,
        backend: str,
        seed: Optional[int],
        extra_payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "project_id": project_id,
            "name": task_name,
            "summary": task_summary,
            "task_id": task_id,
            "task_state": "QUEUED",
            "status_text": "Submitted via API",
            "error_text": "",
            "backend": backend,
            "seed": seed,
            "submitted_at": _now_iso(),
        }
        if isinstance(extra_payload, dict) and extra_payload:
            payload.update(extra_payload)

        self.postgrest.request(
            "POST",
            "project_tasks",
            payload=payload,
            headers={"Prefer": "return=minimal"},
            expect_json=False,
        )

    def find_project_task(self, task_id: str, project_id: str) -> Optional[Dict[str, Any]]:
        normalized_task_id = str(task_id or "").strip()
        normalized_project_id = str(project_id or "").strip()
        if not normalized_task_id or not normalized_project_id:
            return None
        rows = self.postgrest.request(
            "GET",
            "project_tasks",
            query={
                "select": "id,project_id,task_id",
                "task_id": f"eq.{normalized_task_id}",
                "project_id": f"eq.{normalized_project_id}",
                "order": "created_at.desc",
                "limit": "1",
            },
        )
        if rows:
            return rows[0]
        now = time.time()
        with self._alias_lock:
            self._cleanup_task_aliases_locked(now)
            alias = self._task_aliases.get(normalized_task_id)
            if not alias:
                return None
            if str(alias.get("project_id") or "").strip() != normalized_project_id:
                return None
            alias["updated_at"] = now
            return {
                "id": f"alias:{normalized_task_id}",
                "project_id": normalized_project_id,
                "task_id": normalized_task_id,
            }

    def find_project_tasks(self, task_ids: List[str], project_id: str) -> Dict[str, Dict[str, Any]]:
        normalized_project_id = str(project_id or "").strip()
        normalized_task_ids = list(dict.fromkeys(str(task_id or "").strip() for task_id in task_ids if str(task_id or "").strip()))
        if not normalized_project_id or not normalized_task_ids:
            return {}

        results: Dict[str, Dict[str, Any]] = {}
        chunk_size = 100
        for index in range(0, len(normalized_task_ids), chunk_size):
            chunk = normalized_task_ids[index:index + chunk_size]
            rows = self.postgrest.request(
                "GET",
                "project_tasks",
                query={
                    "select": "id,project_id,task_id",
                    "task_id": f"in.({','.join(chunk)})",
                    "project_id": f"eq.{normalized_project_id}",
                    "order": "created_at.desc",
                },
            )
            for row in rows or []:
                task_id = str((row or {}).get("task_id") or "").strip()
                if task_id and task_id not in results:
                    results[task_id] = row

        now = time.time()
        with self._alias_lock:
            self._cleanup_task_aliases_locked(now)
            for normalized_task_id in normalized_task_ids:
                if normalized_task_id in results:
                    continue
                alias = self._task_aliases.get(normalized_task_id)
                if not alias:
                    continue
                if str(alias.get("project_id") or "").strip() != normalized_project_id:
                    continue
                alias["updated_at"] = now
                results[normalized_task_id] = {
                    "id": f"alias:{normalized_task_id}",
                    "project_id": normalized_project_id,
                    "task_id": normalized_task_id,
                }

        return results

    def mark_task_cancelled(self, task_row_id: str) -> None:
        normalized_task_row_id = str(task_row_id or "").strip()
        if not normalized_task_row_id:
            return
        if normalized_task_row_id.startswith("alias:"):
            return
        self.postgrest.request(
            "PATCH",
            "project_tasks",
            query={"id": f"eq.{normalized_task_row_id}"},
            payload={
                "task_state": "REVOKED",
                "status_text": "Cancelled via API",
                "completed_at": _now_iso(),
            },
            headers={"Prefer": "return=minimal"},
            expect_json=False,
        )

    def delete_task_row(self, task_row_id: str) -> None:
        normalized_task_row_id = str(task_row_id or "").strip()
        if not normalized_task_row_id:
            return
        if normalized_task_row_id.startswith("alias:"):
            return
        self.postgrest.request(
            "DELETE",
            "project_tasks",
            query={"id": f"eq.{normalized_task_row_id}"},
            headers={"Prefer": "return=minimal"},
            expect_json=False,
        )

    @contextmanager
    def _admin_statistics_process_lock(self):
        if fcntl is None:
            yield
            return

        lock_path = f"{self._admin_statistics_cache_file}.lock"
        with open(lock_path, "a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _admin_statistics_cache_token(cache_key: tuple[int, int, int]) -> str:
        return ":".join(str(part) for part in cache_key)

    def _read_shared_admin_statistics(
        self,
        cache_key: tuple[int, int, int],
        now: float,
    ) -> tuple[Dict[str, Any], float] | None:
        try:
            with open(self._admin_statistics_cache_file, "r", encoding="utf-8") as cache_file:
                raw_cache = json.load(cache_file)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

        entries = raw_cache.get("entries") if isinstance(raw_cache, dict) else None
        entry = entries.get(self._admin_statistics_cache_token(cache_key)) if isinstance(entries, dict) else None
        if not isinstance(entry, dict):
            return None
        try:
            expires_at = float(entry.get("expires_at") or 0.0)
        except (TypeError, ValueError):
            return None
        payload = entry.get("payload")
        if expires_at <= now or not isinstance(payload, dict):
            return None
        return payload, expires_at

    def _write_shared_admin_statistics(
        self,
        cache_key: tuple[int, int, int],
        payload: Dict[str, Any],
        expires_at: float,
    ) -> None:
        entries: Dict[str, Any] = {}
        try:
            with open(self._admin_statistics_cache_file, "r", encoding="utf-8") as cache_file:
                raw_cache = json.load(cache_file)
            raw_entries = raw_cache.get("entries") if isinstance(raw_cache, dict) else None
            if isinstance(raw_entries, dict):
                entries.update(raw_entries)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass

        now = time.time()
        unexpired_entries: Dict[str, Any] = {}
        for key, value in entries.items():
            if not isinstance(value, dict):
                continue
            try:
                entry_expires_at = float(value.get("expires_at") or 0.0)
            except (TypeError, ValueError):
                continue
            if entry_expires_at > now:
                unexpired_entries[key] = value
        entries = unexpired_entries
        entries[self._admin_statistics_cache_token(cache_key)] = {
            "expires_at": expires_at,
            "payload": payload,
        }
        if len(entries) > 16:
            entries = dict(sorted(
                entries.items(),
                key=lambda item: float(item[1].get("expires_at") or 0.0),
                reverse=True,
            )[:16])
        with open(self._admin_statistics_cache_file, "w", encoding="utf-8") as cache_file:
            json.dump({"entries": entries}, cache_file)

    def _build_admin_statistics(
        self,
        *,
        hours: int,
        limit: int,
        recent: int,
    ) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(hours=hours)
        rows = self.postgrest.request(
            "GET",
            "project_tasks_list",
            query={
                "select": (
                    "id,project_id,task_id,name,backend,task_state,status_text,error_text,"
                    "submitted_at,completed_at,duration_seconds,created_at"
                ),
                "task_id": "neq.",
                "or": (
                    f"(submitted_at.gte.{window_start.isoformat()},"
                    f"and(submitted_at.is.null,created_at.gte.{window_start.isoformat()}))"
                ),
                "order": "submitted_at.desc.nullslast,created_at.desc",
                "limit": str(limit),
            },
        )
        return build_task_statistics(
            rows or [],
            window_hours=hours,
            now=now,
            row_limit=limit,
            recent_limit=recent,
        )

    def get_admin_statistics(
        self,
        *,
        window_hours: int = 24,
        row_limit: int = 10000,
        recent_limit: int = 20,
    ) -> Dict[str, Any]:
        hours = max(1, min(24 * 31, int(window_hours or 24)))
        limit = max(1, min(50000, int(row_limit or 10000)))
        recent = max(1, min(200, int(recent_limit or 20)))
        cache_key = (hours, limit, recent)

        # Keep the lock while refreshing so simultaneous dashboard tabs share
        # one PostgREST query and aggregation instead of stampeding the store.
        with self._admin_statistics_cache_lock:
            monotonic_now = time.monotonic()
            cached = self._admin_statistics_cache.get(cache_key)
            if cached and monotonic_now < cached[0]:
                return cached[1]

            try:
                with self._admin_statistics_process_lock():
                    wall_now = time.time()
                    shared = self._read_shared_admin_statistics(cache_key, wall_now)
                    if shared is not None:
                        payload, shared_expires_at = shared
                        remaining = max(0.0, shared_expires_at - wall_now)
                        self._admin_statistics_cache[cache_key] = (
                            monotonic_now + remaining,
                            payload,
                        )
                        return payload

                    payload = self._build_admin_statistics(
                        hours=hours,
                        limit=limit,
                        recent=recent,
                    )
                    shared_expires_at = time.time() + self._admin_statistics_cache_ttl_seconds
                    try:
                        self._write_shared_admin_statistics(
                            cache_key,
                            payload,
                            shared_expires_at,
                        )
                    except OSError:
                        # The in-process cache remains a safe fallback when the
                        # configured shared cache directory is unavailable.
                        pass
            except OSError:
                payload = self._build_admin_statistics(
                    hours=hours,
                    limit=limit,
                    recent=recent,
                )

            expires_at = time.monotonic() + self._admin_statistics_cache_ttl_seconds
            self._admin_statistics_cache[cache_key] = (expires_at, payload)
            expired_keys = [
                key
                for key, entry in self._admin_statistics_cache.items()
                if entry[0] <= monotonic_now
            ]
            for key in expired_keys:
                if key != cache_key:
                    self._admin_statistics_cache.pop(key, None)
            while len(self._admin_statistics_cache) > 16:
                eviction_candidates = [
                    key for key in self._admin_statistics_cache if key != cache_key
                ]
                if not eviction_candidates:
                    break
                oldest_key = min(
                    eviction_candidates,
                    key=lambda key: self._admin_statistics_cache[key][0],
                )
                self._admin_statistics_cache.pop(oldest_key, None)
            return payload
