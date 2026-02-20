# celery_app.py
import multiprocessing

try:
    multiprocessing.set_start_method('spawn', force=True)
except RuntimeError:
    pass

from celery import Celery
from kombu import Queue
import config

# 创建 Celery 实例
celery_app = Celery(
    'boltz_tasks',
    broker=config.CELERY_BROKER_URL,
    backend=config.CELERY_RESULT_BACKEND,
    include=['tasks']
)

# 定义任务队列用于优先级排序
celery_app.conf.task_queues = (
    Queue('high_priority', routing_key='high_priority'),
    Queue('default', routing_key='default'),
    Queue(config.CPU_QUEUE, routing_key=config.CPU_QUEUE),
)
celery_app.conf.task_default_queue = 'default'
celery_app.conf.task_default_exchange = 'default'
celery_app.conf.task_default_routing_key = 'default'

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

if __name__ == '__main__':
    celery_app.start()
