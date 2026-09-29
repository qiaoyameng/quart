from __future__ import annotations

import asyncio

import pytest

from quart import g
from quart import Quart
from quart import request
from quart import ResponseReturnValue
from quart import session
from quart import sub_request


@pytest.fixture
def app() -> Quart:
    app = Quart(__name__)
    app.secret_key = "secret"

    @app.route("/echo", methods=["POST"])
    async def echo() -> ResponseReturnValue:
        body = await request.get_data()
        return {
            "method": request.method,
            "path": request.path,
            "args": request.args.to_dict(),
            "body": body.decode(),
            "header": request.headers.get("X-Custom", ""),
        }

    return app


async def test_sub_request_basic(app: Quart) -> None:
    async with app.test_request_context("/"):
        response = await app.sub_request(
            "/echo?a=b",
            method="POST",
            headers={"X-Custom": "value"},
            data=b"hello",
        )
    assert response.status_code == 200
    assert await response.get_json() == {
        "method": "POST",
        "path": "/echo",
        "args": {"a": "b"},
        "body": "hello",
        "header": "value",
    }


async def test_sub_request_helper(app: Quart) -> None:
    async with app.test_request_context("/"):
        response = await sub_request("/echo", method="POST", data="hello")
    assert (await response.get_json())["body"] == "hello"


async def test_sub_request_hooks(app: Quart) -> None:
    events = []

    @app.before_request
    async def before() -> None:
        events.append(("before", request.path))

    @app.after_request
    async def after(response):  # type: ignore[no-untyped-def]
        events.append(("after", request.path))
        response.headers["X-After"] = "yes"
        return response

    @app.teardown_request
    async def teardown(_: BaseException | None) -> None:
        events.append(("teardown", request.path))

    async with app.test_request_context("/"):
        response = await app.sub_request("/echo", method="POST")
        assert ("before", "/echo") in events
        assert ("after", "/echo") in events
        assert response.headers["X-After"] == "yes"
    assert ("teardown", "/echo") in events


async def test_sub_request_exception_handling(app: Quart) -> None:
    @app.route("/boom")
    async def boom() -> ResponseReturnValue:
        raise ValueError("boom")

    @app.errorhandler(ValueError)
    async def handle(_: ValueError) -> ResponseReturnValue:
        return "handled", 500

    async with app.test_request_context("/"):
        response = await app.sub_request("/boom")
    assert response.status_code == 500
    assert await response.get_data() == b"handled"


async def test_sub_request_not_found(app: Quart) -> None:
    async with app.test_request_context("/"):
        response = await app.sub_request("/missing")
    assert response.status_code == 404


async def test_sub_request_context_isolation(app: Quart) -> None:
    @app.route("/mutate")
    async def mutate() -> ResponseReturnValue:
        g.value = "sub"
        session["key"] = "sub"
        request.polluted = True  # type: ignore[attr-defined]
        return "ok"

    @app.route("/outer")
    async def outer() -> ResponseReturnValue:
        g.value = "outer"
        session["key"] = "outer"
        await app.sub_request("/mutate")
        assert g.value == "outer"
        assert session["key"] == "outer"
        assert not hasattr(request, "polluted")
        return "done"

    test_client = app.test_client()
    response = await test_client.get("/outer")
    assert response.status_code == 200
    assert await response.get_data() == b"done"


async def test_sub_request_cookies_not_forwarded_by_default(app: Quart) -> None:
    @app.route("/read-cookie")
    async def read_cookie() -> ResponseReturnValue:
        return request.cookies.get("name", "none")

    @app.route("/outer")
    async def outer() -> ResponseReturnValue:
        response = await app.sub_request("/read-cookie")
        return await response.get_data()

    test_client = app.test_client()
    test_client.set_cookie("localhost", "name", "value")
    response = await test_client.get("/outer")
    assert await response.get_data() == b"none"


async def test_sub_request_cookies_forwarded(app: Quart) -> None:
    @app.route("/read-cookie")
    async def read_cookie() -> ResponseReturnValue:
        return request.cookies.get("name", "none")

    @app.route("/outer")
    async def outer() -> ResponseReturnValue:
        response = await app.sub_request("/read-cookie", forward_cookies=True)
        return await response.get_data()

    test_client = app.test_client()
    test_client.set_cookie("localhost", "name", "value")
    response = await test_client.get("/outer")
    assert await response.get_data() == b"value"


async def test_sub_request_session_isolated_with_forwarded_cookies(
    app: Quart,
) -> None:
    @app.route("/mutate-session")
    async def mutate_session() -> ResponseReturnValue:
        session["key"] = "sub"
        return "ok"

    @app.route("/outer")
    async def outer() -> ResponseReturnValue:
        session["key"] = "outer"
        await app.sub_request("/mutate-session", forward_cookies=True)
        assert session["key"] == "outer"
        return "done"

    test_client = app.test_client()
    response = await test_client.get("/outer")
    assert response.status_code == 200


async def test_sub_request_nesting_and_depth_limit(app: Quart) -> None:
    app.config["SUBREQUEST_MAX_DEPTH"] = 3

    @app.route("/recurse")
    async def recurse() -> ResponseReturnValue:
        try:
            response = await app.sub_request("/recurse")
            return await response.get_data()
        except RuntimeError:
            return "limited"

    async with app.test_request_context("/"):
        response = await app.sub_request("/recurse")
    assert response.status_code == 200
    assert await response.get_data() == b"limited"


async def test_sub_request_concurrent(app: Quart) -> None:
    @app.route("/whoami")
    async def whoami() -> ResponseReturnValue:
        await asyncio.sleep(0.01)  # force interleaving
        return f"{request.path}:{request.headers['X-Name']}"

    async with app.test_request_context("/"):
        response_a, response_b = await asyncio.gather(
            app.sub_request("/whoami", headers={"X-Name": "a"}),
            app.sub_request("/whoami", headers={"X-Name": "b"}),
        )
    assert await response_a.get_data() == b"/whoami:a"
    assert await response_b.get_data() == b"/whoami:b"


async def test_sub_request_streaming_response(app: Quart) -> None:
    @app.route("/stream")
    async def stream() -> ResponseReturnValue:
        async def generator():  # type: ignore[no-untyped-def]
            yield b"chunk1"
            yield b"chunk2"

        return generator()

    async with app.test_request_context("/"):
        response = await app.sub_request("/stream")
    assert await response.get_data() == b"chunk1chunk2"


async def test_sub_request_cancelled(app: Quart) -> None:
    generator_closed = asyncio.Event()
    context_torn_down = asyncio.Event()

    @app.route("/stream")
    async def stream() -> ResponseReturnValue:
        async def generator():  # type: ignore[no-untyped-def]
            try:
                yield b"chunk"
                await asyncio.sleep(3600)
            finally:
                generator_closed.set()

        return generator()

    @app.teardown_request
    async def teardown(_: BaseException | None) -> None:
        context_torn_down.set()

    async with app.test_request_context("/"):
        task = asyncio.ensure_future(app.sub_request("/stream"))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert generator_closed.is_set()
    assert context_torn_down.is_set()


async def test_sub_request_aggregation(app: Quart) -> None:
    @app.route("/a")
    async def a() -> ResponseReturnValue:
        return "a"

    @app.route("/b")
    async def b() -> ResponseReturnValue:
        return "b"

    @app.route("/aggregate")
    async def aggregate() -> ResponseReturnValue:
        response_a, response_b = await asyncio.gather(
            app.sub_request("/a"), app.sub_request("/b")
        )
        return {
            "a": await response_a.get_data(as_text=True),
            "b": await response_b.get_data(as_text=True),
        }

    test_client = app.test_client()
    response = await test_client.get("/aggregate")
    assert await response.get_json() == {"a": "a", "b": "b"}
