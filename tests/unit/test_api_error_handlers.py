import asyncio
import threading

import anyio
import anyio.to_thread
import httpx
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.errors import INTERNAL_MESSAGE, ApiError
from api.main import create_app

# unreachable, and nothing here reaches a pool: every response below is decided before one would be
DEAD_DSN = "postgresql://nobody:nobody@127.0.0.1:1/none"
# generous for a response that needs no thread on a loaded runner, and far below HOLD_SECONDS
ANSWER_WITHIN_SECONDS = 2.0
HOLD_SECONDS = 30.0

INTERNAL_BODY = {
    "error": {"code": "internal", "message": "the request could not be completed", "detail": None}
}


class _EndpointHTTPException(StarletteHTTPException):
    pass


def test_every_error_handler_answers_while_every_worker_thread_is_held():
    # Starlette runs a plain-def handler through the same thread pool the sync routes use, so under
    # a burst of slow requests a refusal that needs no database waits behind them for a thread
    app = create_app(dsn=DEAD_DSN)
    release = threading.Event()

    @app.get("/_hold")
    def hold():
        release.wait(HOLD_SECONDS)
        return {}

    @app.get("/_sync_ping")
    def sync_ping():
        return {}

    # async, so that everything a request to it spends on a thread is spent by a handler
    @app.get("/_refuse")
    async def refuse(kind: str):
        if kind == "api":
            raise ApiError(500, "internal", INTERNAL_MESSAGE, None)
        if kind == "endpoint_http":
            raise _EndpointHTTPException(status_code=418)
        raise RuntimeError("unhandled")

    # one handler each, and the 405 on a shipped route, whose method check runs before its sync
    # dependency would take a thread
    cases = (
        ("404 unknown route", "GET", "/nowhere", None),
        ("405 wrong method", "POST", "/symbols", None),
        ("400 request validation", "GET", "/_refuse", None),
        ("500 ApiError", "GET", "/_refuse", {"kind": "api"}),
        ("500 endpoint HTTPException", "GET", "/_refuse", {"kind": "endpoint_http"}),
        ("500 unhandled exception", "GET", "/_refuse", {"kind": "unhandled"}),
    )

    async def scenario():
        limiter = anyio.to_thread.current_default_thread_limiter()
        limiter.total_tokens = 1
        answers = {}
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async with anyio.create_task_group() as group:
                group.start_soon(client.get, "/_hold")
                try:
                    with anyio.fail_after(ANSWER_WITHIN_SECONDS):
                        while limiter.borrowed_tokens < limiter.total_tokens:
                            await anyio.sleep(0.01)
                    # the control: without it, a limiter the app's thread pool does not use passes
                    with anyio.move_on_after(0.5) as control:
                        await client.get("/_sync_ping")
                    answers["control"] = control.cancelled_caught
                    for name, method, path, params in cases:
                        with anyio.move_on_after(ANSWER_WITHIN_SECONDS):
                            answers[name] = await client.request(method, path, params=params)
                finally:
                    release.set()
        return answers

    answers = asyncio.run(scenario())
    assert answers.pop("control") is True, "a sync route answered while the only thread is held"
    unanswered = [name for name, _, _, _ in cases if name not in answers]
    assert unanswered == [], f"waited for a worker thread: {unanswered}"

    # and each body is the one its handler builds, so a handler that answers promptly by answering
    # something else fails here rather than passing
    unknown = answers["404 unknown route"]
    assert unknown.status_code == 404
    assert unknown.json() == {
        "error": {
            "code": "invalid_params",
            "message": "no route matches this path and method",
            "detail": {"reason": "unknown_route", "path": "/nowhere"},
        }
    }
    wrong_method = answers["405 wrong method"]
    assert wrong_method.status_code == 405
    assert wrong_method.headers["allow"] == "GET"
    assert wrong_method.json()["error"]["detail"] == {"reason": "unknown_route", "path": "/symbols"}
    invalid = answers["400 request validation"]
    assert invalid.status_code == 400
    assert invalid.json() == {
        "error": {
            "code": "invalid_params",
            "message": "one or more parameters are not valid",
            "detail": {
                "reason": "invalid_parameter",
                "parameter": "kind",
                "location": "query",
                "errors": [{"parameter": "kind", "location": "query", "type": "missing"}],
            },
        }
    }
    for name in ("500 ApiError", "500 endpoint HTTPException", "500 unhandled exception"):
        assert answers[name].status_code == 500, name
        assert answers[name].json() == INTERNAL_BODY, name
