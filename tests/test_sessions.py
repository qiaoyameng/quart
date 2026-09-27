from __future__ import annotations

import asyncio
from http.cookies import SimpleCookie

import pytest
from hypercorn.typing import HTTPScope
from werkzeug.datastructures import Headers

from quart.app import Quart
from quart.sessions import InMemorySessionInterface
from quart.sessions import InMemorySessionStore
from quart.sessions import SecureCookieSession
from quart.sessions import SecureCookieSessionInterface
from quart.sessions import SessionConflictError
from quart.sessions import VersionedSession
from quart.testing import no_op_push
from quart.wrappers import Request
from quart.wrappers import Response


async def test_secure_cookie_session_interface_open_session(
    http_scope: HTTPScope,
) -> None:
    session = SecureCookieSession()
    session["something"] = "else"
    interface = SecureCookieSessionInterface()
    app = Quart(__name__)
    app.secret_key = "secret"
    response = Response("")
    await interface.save_session(app, session, response)
    request = Request(
        "GET",
        "http",
        "/",
        b"",
        Headers(),
        "",
        "1.1",
        http_scope,
        send_push_promise=no_op_push,
    )
    request.headers["Cookie"] = response.headers["Set-Cookie"]
    new_session = await interface.open_session(app, request)
    assert new_session == session


async def test_secure_cookie_session_interface_save_session() -> None:
    session = SecureCookieSession()
    session["something"] = "else"
    interface = SecureCookieSessionInterface()
    app = Quart(__name__)
    app.secret_key = "secret"
    response = Response("")
    await interface.save_session(app, session, response)
    cookies: SimpleCookie = SimpleCookie()
    cookies.load(response.headers["Set-Cookie"])
    cookie = cookies[app.config["SESSION_COOKIE_NAME"]]
    assert cookie["path"] == interface.get_cookie_path(app)
    assert cookie["httponly"] == "" if not interface.get_cookie_httponly(app) else True
    assert cookie["secure"] == "" if not interface.get_cookie_secure(app) else True
    assert cookie["samesite"] == (interface.get_cookie_samesite(app) or "")
    assert cookie["domain"] == (interface.get_cookie_domain(app) or "")
    assert cookie["expires"] == (interface.get_expiration_time(app, session) or "")
    assert response.headers["Vary"] == "Cookie"


async def _save_session(session: SecureCookieSession) -> Response:
    interface = SecureCookieSessionInterface()
    app = Quart(__name__)
    app.secret_key = "secret"
    response = Response("")
    await interface.save_session(app, session, response)
    return response


async def test_secure_cookie_session_interface_save_session_no_modification() -> None:
    session = SecureCookieSession()
    session["something"] = "else"
    session.modified = False
    response = await _save_session(session)
    assert response.headers.get("Set-Cookie") is None


async def test_secure_cookie_session_interface_save_session_no_access() -> None:
    session = SecureCookieSession()
    session["something"] = "else"
    session.accessed = False
    session.modified = False
    response = await _save_session(session)
    assert response.headers.get("Set-Cookie") is None
    assert response.headers.get("Vary") is None


def _versioned_app() -> tuple[Quart, InMemorySessionInterface]:
    app = Quart(__name__)
    app.secret_key = "secret"
    interface = InMemorySessionInterface()
    app.session_interface = interface
    return app, interface


async def _seed_session(
    interface: InMemorySessionInterface, data: dict, session_id: str = "sid"
) -> int:
    return await interface.store.apply(session_id, 0, data, set())


async def test_versioned_session_concurrent_merge() -> None:
    # Two requests read the same version and change different keys,
    # both saves must merge without losing data.
    app, interface = _versioned_app()
    version = await _seed_session(interface, {"base": 1})
    session_a = VersionedSession({"base": 1}, version=version, session_id="sid")
    session_b = VersionedSession({"base": 1}, version=version, session_id="sid")
    session_a["a"] = 1
    session_b["b"] = 2
    await asyncio.gather(
        interface.save_session(app, session_a, None),
        interface.save_session(app, session_b, None),
    )
    _, data = await interface.store.load("sid")
    assert data == {"base": 1, "a": 1, "b": 2}


async def test_versioned_session_concurrent_merge_many() -> None:
    # Many concurrent requests writing distinct keys all merge.
    app, interface = _versioned_app()
    version = await _seed_session(interface, {"base": 1})

    async def write(index: int) -> None:
        session = VersionedSession({"base": 1}, version=version, session_id="sid")
        session[f"key{index}"] = index
        await interface.save_session(app, session, None)

    await asyncio.gather(*(write(index) for index in range(20)))
    _, data = await interface.store.load("sid")
    assert data == {"base": 1, **{f"key{i}": i for i in range(20)}}


async def test_versioned_session_write_write_conflict() -> None:
    # Two requests writing the same key must report a conflict, the
    # first save must not be silently overwritten.
    app, interface = _versioned_app()
    version = await _seed_session(interface, {"key": "original"})
    session_a = VersionedSession({"key": "original"}, version=version, session_id="sid")
    session_b = VersionedSession({"key": "original"}, version=version, session_id="sid")
    session_a["key"] = "a"
    session_b["key"] = "b"
    await interface.save_session(app, session_a, None)
    with pytest.raises(SessionConflictError) as exc_info:
        await interface.save_session(app, session_b, None)
    assert exc_info.value.conflicts == {"key": "write"}
    _, data = await interface.store.load("sid")
    assert data == {"key": "a"}


async def test_versioned_session_delete_write_conflict() -> None:
    # A delete racing a write of the same key must report a conflict.
    app, interface = _versioned_app()
    version = await _seed_session(interface, {"key": "original"})
    session_a = VersionedSession({"key": "original"}, version=version, session_id="sid")
    session_b = VersionedSession({"key": "original"}, version=version, session_id="sid")
    del session_a["key"]
    session_b["key"] = "b"
    await interface.save_session(app, session_b, None)
    with pytest.raises(SessionConflictError) as exc_info:
        await interface.save_session(app, session_a, None)
    assert exc_info.value.conflicts == {"key": "delete"}
    _, data = await interface.store.load("sid")
    assert data == {"key": "b"}


async def test_versioned_session_clear_keeps_concurrent_keys() -> None:
    # clear() must not delete keys added concurrently by other requests.
    app, interface = _versioned_app()
    version = await _seed_session(interface, {"x": 1})
    session_a = VersionedSession({"x": 1}, version=version, session_id="sid")
    session_b = VersionedSession({"x": 1}, version=version, session_id="sid")
    session_b["y"] = 2
    await interface.save_session(app, session_b, None)
    session_a.clear()
    await interface.save_session(app, session_a, None)
    _, data = await interface.store.load("sid")
    assert data == {"y": 2}


async def test_versioned_session_nested_value_conflicts_as_whole() -> None:
    # Nested values are treated as a whole by top-level key, so
    # concurrent changes to different nested keys still conflict.
    app, interface = _versioned_app()
    version = await _seed_session(interface, {"cfg": {"a": 1, "b": 2}})
    session_a = VersionedSession(
        {"cfg": {"a": 1, "b": 2}}, version=version, session_id="sid"
    )
    session_b = VersionedSession(
        {"cfg": {"a": 1, "b": 2}}, version=version, session_id="sid"
    )
    session_a["cfg"]["a"] = 10
    session_b["cfg"]["b"] = 20
    await interface.save_session(app, session_a, None)
    with pytest.raises(SessionConflictError) as exc_info:
        await interface.save_session(app, session_b, None)
    assert exc_info.value.conflicts == {"cfg": "write"}
    _, data = await interface.store.load("sid")
    assert data == {"cfg": {"a": 10, "b": 2}}


async def test_versioned_session_cookie_roundtrip(http_scope: HTTPScope) -> None:
    app, interface = _versioned_app()
    session = VersionedSession(session_id="sid", new=True)
    session["something"] = "else"
    response = Response("")
    await interface.save_session(app, session, response)
    request = Request(
        "GET",
        "http",
        "/",
        b"",
        Headers(),
        "",
        "1.1",
        http_scope,
        send_push_promise=no_op_push,
    )
    request.headers["Cookie"] = response.headers["Set-Cookie"]
    new_session = await interface.open_session(app, request)
    assert new_session == {"something": "else"}
    assert new_session.session_id == "sid"
    assert new_session.version == session.version


async def test_versioned_session_saved_before_response_headers() -> None:
    # The store must hold the changes by the time the response (and
    # hence its headers) is returned to the client.
    app, interface = _versioned_app()

    @app.route("/")
    async def index() -> str:
        from quart import session

        session["count"] = session.get("count", 0) + 1
        return ""

    client = app.test_client()
    await client.get("/")
    await client.get("/")
    _, data = await interface.store.load(
        next(iter(interface.store._entries))  # type: ignore[attr-defined]
    )
    assert data == {"count": 2}


async def test_versioned_session_websocket_save() -> None:
    # Session writes after a websocket is accepted have a clear result:
    # they are committed to the store once the handler finishes.
    app, interface = _versioned_app()

    @app.websocket("/ws")
    async def ws() -> None:
        from quart import session
        from quart import websocket

        session["ws"] = "written"
        await websocket.send("ok")

    client = app.test_client()
    async with client.websocket("/ws") as test_websocket:
        await test_websocket.receive()
    assert len(interface.store._entries) == 1  # type: ignore[attr-defined]
    _, data, _ = next(iter(interface.store._entries.values()))  # type: ignore[attr-defined]
    assert data == {"ws": "written"}
