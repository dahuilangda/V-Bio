# celery_app.py
import logging
import multiprocessing

_logger = logging.getLogger(__name__)

try:
    multiprocessing.set_start_method('spawn', force=True)
except RuntimeError as exc:
    # spawn is required for CUDA/psutil correctness in workers; silently falling back to fork
    # would mask CUDA init failures. Log so a misconfigured environment stays visible.
    _logger.warning('Could not set multiprocessing start method to spawn: %s', exc)

from celery import Celery
from kombu import Queue
from backend.core import config
from backend.scheduling.capability_router import build_capability_queue, list_known_queues

# 创建 Celery 实例
celery_app = Celery(
    'boltz_tasks',
    broker=config.CELERY_BROKER_URL,
    backend=config.CELERY_RESULT_BACKEND,
    include=['backend.worker.tasks']
)

# Define capability queues only (legacy generic queues are removed).
known_queues = list_known_queues()
celery_app.conf.task_queues = tuple(
    Queue(queue_name, routing_key=queue_name)
    for queue_name in known_queues
)
default_queue = build_capability_queue('boltz2', 'default')
celery_app.conf.task_default_queue = default_queue
celery_app.conf.task_default_exchange = default_queue
celery_app.conf.task_default_routing_key = default_queue

celery_app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    task_create_missing_queues=True,
    # Reliability: avoid losing tasks when worker crashes/restarts.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    broker_connection_retry_on_startup=True,
    broker_transport_options={
        # Must be larger than expected long-running prediction tasks.
        "visibility_timeout": 24 * 60 * 60,
    },
    task_track_started=True,
    worker_send_task_events=True,
    task_send_sent_event=True,
    worker_heartbeat=15.0,
)

# Ensure workers request tasks one at a time to avoid queue starvation and
# provide fair interleaving when multiple jobs are waiting.
celery_app.conf.worker_prefetch_multiplier = 1
app = celery_app


def _worker_slot_count(sender) -> int:
    pool = getattr(sender, "pool", None)
    for value in (getattr(pool, "limit", None), getattr(pool, "num_processes", None)):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed >= 0:
            return parsed
    return 0


try:
    from celery.signals import worker_ready, worker_shutdown
    from backend.monitoring.event_transport import publish_worker_metadata

    @worker_ready.connect(weak=False)
    def _publish_monitor_worker_ready(sender=None, **_kwargs):
        worker_id = str(getattr(sender, "hostname", "") or "").strip()
        if not worker_id:
            return
        try:
            publish_worker_metadata(
                event_type="worker-ready",
                worker_id=worker_id,
                slots_total=_worker_slot_count(sender),
            )
        except Exception:
            _logger.exception("Failed to publish worker-ready monitor event for %s", worker_id)

    @worker_shutdown.connect(weak=False)
    def _publish_monitor_worker_shutdown(sender=None, **_kwargs):
        worker_id = str(getattr(sender, "hostname", "") or "").strip()
        if not worker_id:
            return
        try:
            publish_worker_metadata(event_type="worker-shutdown", worker_id=worker_id)
        except Exception:
            _logger.exception("Failed to publish worker-shutdown monitor event for %s", worker_id)
except ImportError:  # pragma: no cover - Celery is a required production dependency.
    _logger.exception("Unable to register worker monitor signals")

if __name__ == '__main__':
    celery_app.start()
