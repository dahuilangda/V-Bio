"""项目级任务状态 SSE：终态对账后即时推送，轮询降级为对账兜底。

链路：网关 batch 对账（reconcile_terminal_rows 写终态）→ publish
（进程内扇出 + pg_notify 跨 gunicorn worker）→ LISTEN 线程扇出 → SSE 帧
（changed 事件 + 心跳 + 600s 强制重连）。无订阅者时 LISTEN/NOTIFY 连接
惰性；订阅队列满丢弃并计数——SSE 只是加速器，对账与轮询才是正确性来源。
"""

from __future__ import annotations

import json
import logging
import queue
import select
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Iterator

import psycopg2

LOGGER = logging.getLogger(__name__)

CHANNEL = "vbio_task_events"
KEEPALIVE_SECONDS = 15.0
MAX_LIFETIME_SECONDS = 600.0


@dataclass
class TaskStatusSubscription:
    subscription_id: str
    project_id: str
    notifications: "queue.Queue[list[dict]]"
    hub: "TaskStatusHub"

    def wait(self, timeout: float) -> list[dict] | None:
        try:
            return self.notifications.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self.hub.unsubscribe(self.subscription_id)

    def __enter__(self) -> "TaskStatusSubscription":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class TaskStatusHub:
    """进程内订阅注册表 + 跨 gunicorn worker 的 PG LISTEN 连接（惰性启动）。

    publish 两条路：进程内直投（对账请求所在的 worker）与 pg_notify（另一
    个 worker 上的订阅者经 LISTEN 线程接收）。队列上限 8 条，满则丢弃新帧
    并计数——订阅端还有 4s 轮询兜底，丢帧只影响即时性不影响正确性。
    """

    def __init__(
        self,
        database_url: str,
        *,
        connection_factory: Callable[[str], Any] | None = None,
        notify_connection_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.database_url = str(database_url or "").strip()
        self._listen_factory = connection_factory or self._open_listen_connection
        self._notify_factory = notify_connection_factory or self._open_notify_connection
        self._subscribers: dict[str, queue.Queue[list[dict]]] = {}
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._notify_connection = None
        self._dropped = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        with self._start_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._listen_loop,
                name="vbio-task-status-listener",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._close_notify_connection()

    def subscribe(self, project_id: str) -> TaskStatusSubscription:
        normalized = str(project_id or "").strip()
        self.start()
        subscription_id = uuid.uuid4().hex
        notifications: queue.Queue[list[dict]] = queue.Queue(maxsize=8)
        with self._lock:
            self._subscribers[subscription_id] = (normalized, notifications)
        return TaskStatusSubscription(subscription_id, normalized, notifications, self)

    def unsubscribe(self, subscription_id: str) -> None:
        with self._lock:
            self._subscribers.pop(subscription_id, None)

    def publish(self, project_id: str, entries: list[dict]) -> None:
        """终态变更入口：进程内扇出 + pg_notify 通知另一 worker。"""
        normalized_project = str(project_id or "").strip()
        payload = [
            {"task_id": str(entry.get("task_id") or ""), "state": str(entry.get("state") or "")}
            for entry in (entries or [])
            if isinstance(entry, dict) and str(entry.get("task_id") or "").strip()
        ]
        if not payload:
            return
        event = {"project_id": normalized_project, "entries": payload}
        with self._lock:
            subscribers = list(self._subscribers.values())
        delivered = 0
        for subscriber_project, notifications in subscribers:
            if subscriber_project and subscriber_project != normalized_project:
                continue
            dropped = self._offer(notifications, event)
            delivered += 1
            if dropped:
                self._dropped += dropped
        if self._dropped and self._dropped % 50 == 0:
            LOGGER.warning("Task status SSE queues full; dropped %d frames total", self._dropped)
        self._notify_remote(event)

    def _notify_remote(self, event: dict) -> None:
        connection = self._ensure_notify_connection()
        if connection is None:
            return
        try:
            with connection.cursor() as cursor:
                cursor.execute("select pg_notify(%s, %s)", (CHANNEL, json.dumps(event, default=str)))
        except Exception:
            LOGGER.warning("Task status pg_notify failed; remote worker sees it on next poll.", exc_info=True)
            self._close_notify_connection()

    def _ensure_notify_connection(self):
        if self._notify_connection is not None:
            return self._notify_connection
        if not self.database_url:
            return None
        try:
            self._notify_connection = self._notify_factory(self.database_url)
        except Exception:
            LOGGER.warning("Task status notify connection unavailable; falling back to in-process only.", exc_info=True)
            self._notify_connection = None
        return self._notify_connection

    def _close_notify_connection(self) -> None:
        if self._notify_connection is not None:
            try:
                self._notify_connection.close()
            except Exception:
                pass
            self._notify_connection = None

    def _open_notify_connection(self, database_url: str):
        connection = psycopg2.connect(str(database_url).strip(), connect_timeout=10, application_name="vbio-task-status-notify")
        connection.set_session(autocommit=True)
        return connection

    def _open_listen_connection(self, database_url: str):
        connection = psycopg2.connect(str(database_url).strip(), connect_timeout=10, application_name="vbio-task-status-listener")
        connection.set_session(autocommit=True)
        return connection

    def _offer(self, notifications: "queue.Queue[list[dict]]", event: list[dict]) -> int:
        dropped = 0
        try:
            while True:
                notifications.get_nowait()
                dropped += 1
        except queue.Empty:
            pass
        try:
            notifications.put_nowait(event)
        except queue.Full:
            dropped += 1
        return dropped

    def _fan_out(self, event: dict) -> None:
        event_project = str(event.get("project_id") or "").strip()
        entries = event.get("entries")
        if not isinstance(entries, list) or not entries:
            return
        with self._lock:
            subscribers = list(self._subscribers.values())
        for subscriber_project, notifications in subscribers:
            if subscriber_project and event_project and subscriber_project != event_project:
                continue
            self._offer(notifications, entries)

    def _listen_loop(self) -> None:
        reconnect_delay = 1.0
        while not self._stop.is_set():
            connection = None
            try:
                connection = self._listen_factory(self.database_url)
                if connection is None:
                    return
                with connection.cursor() as cursor:
                    cursor.execute(f"listen {CHANNEL}")
                reconnect_delay = 1.0
                while not self._stop.is_set():
                    ready, _, _ = select.select([connection], [], [], 15.0)
                    if not ready:
                        continue
                    connection.poll()
                    while connection.notifies:
                        notification = connection.notifies.pop(0)
                        try:
                            self._fan_out(json.loads(notification.payload))
                        except (TypeError, ValueError, json.JSONDecodeError):
                            LOGGER.warning("Ignored invalid task status notification: %r", notification.payload)
            except Exception:
                if not self._stop.is_set():
                    LOGGER.exception("Task status LISTEN connection failed")
                    self._stop.wait(reconnect_delay)
                    reconnect_delay = min(30.0, reconnect_delay * 2.0)
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass


def task_status_sse_frames(
    subscription: TaskStatusSubscription,
    *,
    keepalive_seconds: float = 15.0,
    max_lifetime_seconds: float = 600.0,
) -> Iterator[str]:
    """SSE 帧生成器：changed 事件 + 心跳 + 到期强制重连（前端重连后重新订阅）。"""
    deadline = time.monotonic() + max(30.0, max_lifetime_seconds)
    with subscription:
        while True:
            remaining_lifetime = deadline - time.monotonic()
            if remaining_lifetime <= 0:
                yield "event: reconnect\ndata: {}\n\n"
                return
            entries = subscription.wait(min(keepalive_seconds, remaining_lifetime))
            if entries is None:
                if time.monotonic() >= deadline:
                    yield "event: reconnect\ndata: {}\n\n"
                    return
                yield ": keepalive\n\n"
                continue
            payload = json.dumps(entries, ensure_ascii=True, separators=(",", ":"), default=str)
            yield f"event: changed\ndata: {payload}\n\n"
