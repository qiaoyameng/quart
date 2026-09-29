from __future__ import annotations

import asyncio

import pytest

from quart import g
from quart import Quart
from quart import request
from quart import session


@pytest.fixture(name="app")
def _app() -> Quart:
    return Quart(__name__)


async def test_subrequest_basic(app: Quart) -> None:
    @app.route("/part")
    async def part() -> str:
        return "part-data"

    @app.route("/agg")
    async def agg() -> str:
        response = await app.subrequest("/part")
        assert response.status_code == 200
        body = await response.get_data(as_text=True)
        return f"agg[{body}]"

    response = await app.test_client().get("/agg")
    assert await response.get_data(as_text=True) == "agg[part-data]"


async def test_subrequest_method_headers_and_body(app: Quart) -> None:
    @app.route("/echo", methods=["POST"])
    async def echo() -> str:
        body = await request.get_data(as_text=True)
        return f"{request.method}|{request.headers['X-Custom']}|{body}"

    @app.route("/agg")
    async def agg() -> str:
        response = await app.subrequest(
            "/echo", method="POST", headers={"X-Custom": "yes"}, data="payload"
        )
        return await response.get_data(as_text=True)

    response = await app.test_client().get("/agg")
    assert await response.get_data(as_text=True) == "POST|yes|payload"


async def test_subrequest_runs_hooks(app: Quart) -> None:
    calls = []

    @app.before_request
    async def before() -> None:
        calls.append(("before", request.path))

    @app.after_request
    async def after(response):
        calls.append(("after", request.path))
        return response

    @app.teardown_request
    async def teardown(error) -> None:
        calls.append(("teardown", request.path))

    @app.route("/part")
    async def part() -> str:
        return "part"

    @app.route("/agg")
    async def agg() -> str:
        await app.subrequest("/part")
        return "agg"

    await app.test_client().get("/agg")
    assert ("before", "/part") in calls
    assert ("after", "/part") in calls
    assert ("teardown", "/part") in calls


async def test_subrequest_error_handling(app: Quart) -> None:
    @app.route("/broken")
    async def broken() -> str:
        raise ValueError("broken")

    @app.route("/agg")
    async def agg() -> str:
        missing = await app.subrequest("/missing")
        assert missing.status_code == 404
        broken_response = await app.subrequest("/broken")
        assert broken_response.status_code == 500
        return "handled"

    response = await app.test_client().get("/agg")
    assert response.status_code == 200
    assert await response.get_data(as_text=True) == "handled"


async def test_subrequest_context_isolation(app: Quart) -> None:
    app.secret_key = "secret"

    @app.route("/pollute")
    async def pollute() -> str:
        g.value = "sub"
        session["value"] = "sub"
        return "polluted"

    @app.route("/agg")
    async def agg() -> str:
        g.value = "outer"
        session["value"] = "outer"
        await app.subrequest("/pollute")
        return f"{g.value}|{session['value']}"

    response = await app.test_client().get("/agg")
    assert await response.get_data(as_text=True) == "outer|outer"


async def test_subrequest_cookies_not_forwarded_by_default(app: Quart) -> None:
    @app.route("/who")
    async def who() -> str:
        return request.cookies.get("name", "anon")

    @app.route("/agg")
    async def agg() -> str:
        response = await app.subrequest("/who")
        return await response.get_data(as_text=True)

    client = app.test_client()
    client.set_cookie("localhost", "name", "value")
    response = await client.get("/agg")
    assert await response.get_data(as_text=True) == "anon"


async def test_subrequest_cookies_forwarded(app: Quart) -> None:
    @app.route("/who")
    async def who() -> str:
        return request.cookies.get("name", "anon")

    @app.route("/agg")
    async def agg() -> str:
        response = await app.subrequest("/who", forward_cookies=True)
        return await response.get_data(as_text=True)

    client = app.test_client()
    client.set_cookie("localhost", "name", "value")
    response = await client.get("/agg")
    assert await response.get_data(as_text=True) == "value"


async def test_subrequest_max_depth(app: Quart) -> None:
    app.config["SUBREQUEST_MAX_DEPTH"] = 2

    @app.route("/recursive")
    async def recursive() -> str:
        try:
            response = await app.subrequest("/recursive")
            return await response.get_data(as_text=True)
        except RuntimeError:
            return "limit"

    response = await app.test_client().get("/recursive")
    assert await response.get_data(as_text=True) == "limit"


async def test_subrequest_concurrent(app: Quart) -> None:
    @app.route("/echo")
    async def echo() -> str:
        await asyncio.sleep(0.01)
        return f"{request.path}?{request.query_string.decode()}"

    @app.route("/agg")
    async def agg() -> str:
        first, second = await asyncio.gather(
            app.subrequest("/echo?name=a"), app.subrequest("/echo?name=b")
        )
        first_body = await first.get_data(as_text=True)
        second_body = await second.get_data(as_text=True)
        return f"{first_body}|{second_body}"

    response = await app.test_client().get("/agg")
    assert await response.get_data(as_text=True) == "/echo?name=a|/echo?name=b"


async def test_subrequest_cancelled_with_outer(app: Quart) -> None:
    teardown_done = asyncio.Event()
    generator_closed = asyncio.Event()

    @app.teardown_request
    async def teardown(error) -> None:
        if request.path == "/slow":
            teardown_done.set()

    @app.route("/slow")
    async def slow():
        async def stream():
            try:
                yield "data"
                await asyncio.sleep(60)
                yield "more"
            finally:
                generator_closed.set()

        return stream()

    @app.route("/outer")
    async def outer() -> str:
        await app.subrequest("/slow")
        return "done"

    task = asyncio.ensure_future(app.test_client().get("/outer"))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert teardown_done.is_set()
    assert generator_closed.is_set()
