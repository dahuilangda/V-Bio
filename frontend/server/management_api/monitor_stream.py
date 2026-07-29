from __future__ import annotations

import logging
import json
import queue
import select
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Iterator

from backend.monitoring.monitor_store import MonitorStore, open_listen_connection


LOGGER = logging.getLogger(__name__)


@dataclass
class MonitorSubscription:
    subscription_id: str
    notifications: queue.Queue[int]
    broker: "MonitorNotificationBroker"

    def wait(self, timeout: float) -> int | None:
        try:
            return self.notifications.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self.broker.unsubscribe(self.subscription_id)

    def coalesce(self, sequence: int, duration: float) -> int:
        latest = max(0, int(sequence or 0))
        deadline = time.monotonic() + max(0.0, duration)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return latest
            next_sequence = self.wait(remaining)
            if next_sequence is None:
                return latest
            latest = max(latest, next_sequence)

    def __enter__(self) -> "MonitorSubscription":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class MonitorNotificationBroker:
    """One PostgreSQL LISTEN connection per process with bounded SSE fan-out."""

    def __init__(self, database_url: str, store: MonitorStore) -> None:
        self.database_url = str(database_url or "").strip()
        self.store = store
        self._subscribers: dict[str, queue.Queue[int]] = {}
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        with self._start_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._listen_loop,
                name="vbio-postgres-monitor-listener",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def subscribe(self, cursor: int) -> MonitorSubscription:
        self.start()
        subscription_id = uuid.uuid4().hex
        notifications: queue.Queue[int] = queue.Queue(maxsize=1)
        with self._lock:
            self._subscribers[subscription_id] = notifications
        latest = self.store.latest_sequence()
        if latest > max(0, int(cursor or 0)):
            self._replace_notification(notifications, latest)
        return MonitorSubscription(subscription_id, notifications, self)

    def unsubscribe(self, subscription_id: str) -> None:
        with self._lock:
            self._subscribers.pop(subscription_id, None)

    def publish(self, sequence: int) -> None:
        normalized = max(0, int(sequence or 0))
        if not normalized:
            return
        with self._lock:
            subscribers = list(self._subscribers.values())
        for notifications in subscribers:
            self._replace_notification(notifications, normalized)

    @staticmethod
    def _replace_notification(notifications: queue.Queue[int], sequence: int) -> None:
        try:
            while True:
                notifications.get_nowait()
        except queue.Empty:
            pass
        try:
            notifications.put_nowait(sequence)
        except queue.Full:
            pass

    def _listen_loop(self) -> None:
        reconnect_delay = 1.0
        while not self._stop.is_set():
            connection = None
            try:
                connection = open_listen_connection(self.database_url)
                with connection.cursor() as cursor:
                    cursor.execute("listen vbio_monitor")
                reconnect_delay = 1.0
                self.publish(self.store.latest_sequence())
                while not self._stop.is_set():
                    ready, _, _ = select.select([connection], [], [], 15.0)
                    if not ready:
                        continue
                    connection.poll()
                    while connection.notifies:
                        notification = connection.notifies.pop(0)
                        try:
                            self.publish(int(notification.payload))
                        except (TypeError, ValueError):
                            LOGGER.warning("Ignored invalid PostgreSQL monitor notification: %r", notification.payload)
            except Exception:
                if not self._stop.is_set():
                    LOGGER.exception("PostgreSQL monitor LISTEN connection failed")
                    self._stop.wait(reconnect_delay)
                    reconnect_delay = min(30.0, reconnect_delay * 2.0)
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass


def sse_frames(
    subscription: MonitorSubscription,
    snapshot_loader,
    *,
    keepalive_seconds: float = 15.0,
    coalesce_seconds: float = 0.2,
    max_lifetime_seconds: float = 600.0,
) -> Iterator[str]:
    deadline = time.monotonic() + max(30.0, max_lifetime_seconds)
    with subscription:
        while True:
            remaining_lifetime = deadline - time.monotonic()
            if remaining_lifetime <= 0:
                yield "event: reconnect\ndata: {}\n\n"
                return
            sequence = subscription.wait(min(keepalive_seconds, remaining_lifetime))
            if sequence is None:
                if time.monotonic() >= deadline:
                    yield "event: reconnect\ndata: {}\n\n"
                    return
                yield ": keepalive\n\n"
                continue
            sequence = subscription.coalesce(sequence, coalesce_seconds)
            snapshot = snapshot_loader()
            actual_sequence = max(sequence, int(snapshot.get("sequence") or 0))
            snapshot["sequence"] = actual_sequence
            payload = json.dumps(snapshot, ensure_ascii=True, separators=(",", ":"), default=str)
            yield f"id: {actual_sequence}\nevent: overview\ndata: {payload}\n\n"
