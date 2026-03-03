# celery_app.py
import multiprocessing

try:
    multiprocessing.set_start_method('spawn', force=True)
except RuntimeError:
    pass

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
)

# Ensure workers request tasks one at a time to avoid queue starvation and
# provide fair interleaving when multiple jobs are waiting.
celery_app.conf.worker_prefetch_multiplier = 1
app = celery_app

if __name__ == '__main__':
    celery_app.start()
