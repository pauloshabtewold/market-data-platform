import threading
import time

import anyio.to_thread
import psycopg
from fastapi.testclient import TestClient

import api.main
from api.main import create_app

DEAD_DSN = "postgresql://nobody:nobody@127.0.0.1:1/none"


class _IdlePool:
    def open(self, wait=False):
        pass

    def close(self):
        pass


def _saturated(client, release):
    limiter = client.portal.call(anyio.to_thread.current_default_thread_limiter)
    client.portal.call(setattr, limiter, "total_tokens", 1)
    held = client.portal.start_task_soon(anyio.to_thread.run_sync, release.wait)
    deadline = time.monotonic() + 5
    while client.portal.call(lambda: limiter.borrowed_tokens) != 1:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    return held


def _get(client, path, give_up_after):
    out = {}

    def get():
        start = time.monotonic()
        out["response"] = client.get(path)
        out["elapsed"] = time.monotonic() - start

    thread = threading.Thread(target=get)
    thread.start()
    thread.join(give_up_after)
    return out.get("response"), out.get("elapsed")


def test_health_answers_internal_while_every_worker_thread_is_held(monkeypatch):
    async def _fails(dsn):
        raise psycopg.OperationalError("down")

    monkeypatch.setattr(api.main, "_check_database", _fails)
    app = create_app(dsn=DEAD_DSN)
    app.state.pool = _IdlePool()
    release = threading.Event()
    with TestClient(app, raise_server_exceptions=False) as client:
        held = _saturated(client, release)
        try:
            response, elapsed = _get(client, "/health", 3)
            assert response is not None, "the 500 waited for a worker thread"
            assert response.status_code == 500
            assert elapsed < 1.0
        finally:
            release.set()
            held.result(5)


def test_a_refused_parameter_answers_while_every_worker_thread_is_held():
    app = create_app(dsn=DEAD_DSN)
    app.state.pool = _IdlePool()
    release = threading.Event()
    with TestClient(app, raise_server_exceptions=False) as client:
        held = _saturated(client, release)
        try:
            response, elapsed = _get(client, "/symbols?limit=abc", 3)
            assert response is not None, "the 400 waited for a worker thread"
            assert response.status_code == 400
            assert elapsed < 1.0
        finally:
            release.set()
            held.result(5)
