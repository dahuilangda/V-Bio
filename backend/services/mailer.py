"""Task-completion email notifications (SwissDock/HDOCK submit pattern):
optional notify_email at submit, sent via SMTP SSL from the owning worker.
Email is auxiliary — failures are logged loudly, never fail or retry the task."""
from __future__ import annotations

import logging
import re

from backend.core import config

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_notify_email(value: object) -> bool:
    return isinstance(value, str) and bool(_EMAIL_RE.match(value.strip()))


def _smtp_configured() -> bool:
    return bool(getattr(config, "SMTP_HOST", "") and getattr(config, "SMTP_USER", ""))


_DIGEST_MAX_ROWS = 50


def digest_window_seconds() -> int:
    """Batching window for task emails; 0 = one immediate email per task."""
    return int(getattr(config, "NOTIFY_DIGEST_WINDOW_SECONDS", 900) or 0)


def _enqueue_digest_event(
    *,
    to_email: str,
    task_id: str,
    state: str,
    task_kind: str,
    project_id: str | None,
) -> None:
    """Append one event to the recipient's pending digest.

    First event atomically claims the window marker (SET NX EX) and schedules
    the flush task with a countdown; later events just append. A lost flush
    task self-heals via marker TTL expiry.
    """
    import json as _json

    from backend.core.celery_app import celery_app
    from gpu_manager import get_redis_client

    window = digest_window_seconds()
    event = _json.dumps({
        "task_id": task_id,
        "state": state,
        "kind": task_kind,
        "project_id": project_id or "",
        "time": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).strftime("%H:%M:%S"),
    })
    client = get_redis_client()
    pipe = client.pipeline()
    pipe.rpush(f"vbio:notify:pending:{to_email}", event)
    pipe.execute()
    claimed = client.set(
        f"vbio:notify:window:{to_email}", "1", ex=window, nx=True)
    if claimed:
        celery_app.send_task(
            "backend.worker.tasks.flush_notification_digest",
            args=[to_email], countdown=window)


def flush_notification_digest(to_email: str) -> bool:
    """Drain the recipient's pending events and send ONE summary email."""
    import json as _json

    from gpu_manager import get_redis_client

    client = get_redis_client()
    key = f"vbio:notify:pending:{to_email}"
    raw = client.lrange(key, 0, -1)
    if not raw:
        return True
    client.delete(key)
    events = []
    for row in raw:
        try:
            events.append(_json.loads(row))
        except Exception:
            logger.warning("dropping malformed digest event %r", row)
    overflow = 0
    if len(events) > _DIGEST_MAX_ROWS:
        overflow = len(events) - _DIGEST_MAX_ROWS
        events = events[:_DIGEST_MAX_ROWS]

    frontend_base = str(getattr(config, "FRONTEND_URL", "") or "").rstrip("/")
    lines = [f"Task summary — {len(events)} task{'s' if len(events) != 1 else ''} reached a terminal state:",
             ""]
    for e in events:
        row = f"  [{e.get('state')}] {e.get('kind')} · {e.get('time')} UTC · {str(e.get('task_id'))[:8]}"
        if e.get("project_id"):
            row += f" · project {str(e['project_id'])[:8]}"
        lines.append(row)
    if overflow:
        lines.append(f"  … and {overflow} more")
    if frontend_base:
        lines += ["", f"Open V-Bio: {frontend_base}/projects"]
    lines += ["", "One email per 15-minute window while tasks keep finishing.",
              "You receive this because your account email is set as the notification address."]

    subject = (f"[V-Bio] {len(events)} task{'s' if len(events) != 1 else ''} "
               f"finished (batched)")
    return _send_plain(to_email, subject, "\n".join(lines))


def _send_plain(to_email: str, subject: str, body: str) -> bool:
    """SMTP send via the shared dependency-free leaf (see smtp_sender)."""
    from backend.services.smtp_sender import send_plain_smtp
    return send_plain_smtp(to_email, subject, body)


def send_task_notification(
    *,
    to_email: str,
    task_id: str,
    state: str,
    task_kind: str = "job",
    project_id: str | None = None,
    detail: str = "",
) -> bool:
    """Record a task terminal event for the recipient.

    With batching the event joins the digest window, otherwise sends
    immediately. Never raises: the compute result is already durable.
    """
    if not is_valid_notify_email(to_email):
        logger.warning("notify_email %r for task %s is malformed; dropped",
                       to_email, task_id)
        return False
    if digest_window_seconds() > 0:
        try:
            _enqueue_digest_event(
                to_email=to_email, task_id=task_id, state=state,
                task_kind=task_kind, project_id=project_id)
            return True
        except Exception:
            logger.error("digest enqueue failed for %s; sending directly",
                         to_email, exc_info=True)
            try:
                from gpu_manager import get_redis_client
                client = get_redis_client()
                client.lrem(f"vbio:notify:pending:{to_email}", 1, event)
                client.delete(f"vbio:notify:window:{to_email}")
            except Exception:
                logger.error("digest enqueue cleanup failed for %s",
                             to_email, exc_info=True)
    return _send_task_email_direct(
        to_email=to_email, task_id=task_id, state=state,
        task_kind=task_kind, project_id=project_id, detail=detail)


def _send_task_email_direct(
    *,
    to_email: str,
    task_id: str,
    state: str,
    task_kind: str = "job",
    project_id: str | None = None,
    detail: str = "",
) -> bool:
    """Send one completion/failure notification. Returns True on success."""
    if not _smtp_configured():
        logger.warning(
            "notify_email requested for task %s but SMTP is not configured "
            "(SMTP_HOST/SMTP_USER); notification dropped", task_id)
        return False
    if not is_valid_notify_email(to_email):
        logger.warning("notify_email %r for task %s is malformed; dropped",
                       to_email, task_id)
        return False

    frontend_base = str(getattr(config, "FRONTEND_URL", "") or "").rstrip("/")
    state_word = "completed" if state == "SUCCESS" else f"finished ({state})"
    subject = f"[V-Bio] Your {task_kind} {state_word}"
    lines = [
        f"Task: {task_id}",
        f"State: {state}",
    ]
    lines.insert(0, f"Kind: {task_kind}")
    if project_id:
        lines.append(f"Project: {project_id}")
    if detail:
        lines.append(f"Detail: {detail[:500]}")
    if frontend_base:
        path = f"/projects/{project_id}" if project_id else "/projects"
        lines.append(f"Open: {frontend_base}{path}")
    body = "\n".join(lines)

    return _send_plain(to_email, subject, body)
