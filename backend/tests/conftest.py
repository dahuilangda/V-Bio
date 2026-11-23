import pytest


@pytest.fixture(autouse=True)
def isolate_brokers(monkeypatch):
    def blocked(*args, **kwargs):
        raise RuntimeError("unit tests must not reach the production broker")

    from backend.core import celery_app as celery_module
    from redis.connection import Connection

    monkeypatch.setattr(celery_module.celery_app, "send_task", blocked)
    monkeypatch.setattr(celery_module.celery_app.control, "revoke", blocked)
    monkeypatch.setattr(Connection, "connect", blocked)
