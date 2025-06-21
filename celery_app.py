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
)

if __name__ == '__main__':
    celery_app.start()