from __future__ import annotations

import asyncio

import pytest

from quart import Quart
from quart import session
from quart import websocket
from quart.sessions import InMemorySessionInterface
from quart.sessions import InMemorySessionStore
from quart.sessions import SessionChangeset
from quart.sessions import SessionConflictError
from quart.sessions import VersionedSession


def test_changeset_tracks_top_level_keys() -> None:
    session_ = VersionedSession({"a": 1, "b": 2}, sid="sid", version=3, new=False)
    session_["a"] = 10
    session_["c"] = 3
    del session_["b"]
    changeset = session_.changeset
    assert changeset.sets == {"a": 10, "c": 3}
    assert changeset.deletes == frozenset({"b"})


def test_changeset_treats_nested_values_as_whole() -> None:
    session_ = VersionedSession(
        {"nested": {"x": 1}, "other": {"y": 1}}, sid="sid", version=1, new=False
    )
    session_["nested"]["x"] = 2
    changeset = session_.changeset
    assert changeset.sets == {"nested": {"x": 2}}
    assert changeset.deletes == frozenset()


def test_changeset_unmodified() -> None:
    session_ = VersionedSession({"a": 1}, sid="sid", version=1, new=False)
    assert not session_.changeset
    session_["a"] = 2
    session_["a"] = 1
    assert not session_.changeset


def test_clear_only_deletes_snapshot_keys() -> None:
    session_ = VersionedSession({"a": 1}, sid="sid", version=1, new=False)
    session_["new"] = 2
    session_.clear()
    changeset = session_.changeset
    assert changeset.sets == {}
    assert changeset.deletes == frozenset({"a"})


async def test_store_merges_different_keys() -> None:
    store = InMemorySessionStore()
    await store.commit("sid", 0, SessionChangeset(sets={"a": 1}))
    _, version = (await store.load("sid"))  # type: ignore[misc]
    await store.commit("sid", version, SessionChangeset(sets={"b": 2}))
    await store.commit("sid", version, SessionChangeset(sets={"c": 3}))
    data, _ = (await store.load("sid"))  # type: ignore[misc]
    assert data == {"a": 1, "b": 2, "c": 3}


async def test_store_same_key_write_conflict() -> None:
    store = InMemorySessionStore()
    await store.commit("sid", 0, SessionChangeset(sets={"a": 1}))
    _, version = (await store.load("sid"))  # type: ignore[misc]
    await store.commit("sid", version, SessionChangeset(sets={"a": 2}))
    with pytest.raises(SessionConflictError) as exc_info:
        await store.commit("sid", version, SessionChangeset(sets={"a": 3}))
    assert exc_info.value.conflicts == frozenset({"a"})
    data, _ = (await store.load("sid"))  # type: ignore[misc]
    assert data == {"a": 2}


async def test_store_delete_write_conflict() -> None:
    store = InMemorySessionStore()
    await store.commit("sid", 0, SessionChangeset(sets={"a": 1}))
    _, version = (await store.load("sid"))  # type: ignore[misc]
    await store.commit("sid", version, SessionChangeset(deletes=frozenset({"a"})))
    with pytest.raises(SessionConflictError):
        await store.commit("sid", version, SessionChangeset(sets={"a": 2}))


async def test_store_write_delete_conflict() -> None:
    store = InMemorySessionStore()
    await store.commit("sid", 0, SessionChangeset(sets={"a": 1}))
    _, version = (await store.load("sid"))  # type: ignore[misc]
    await store.commit("sid", version, SessionChangeset(sets={"a": 2}))
    with pytest.raises(SessionConflictError):
        await store.commit("sid", version, SessionChangeset(deletes=frozenset({"a"})))


async def test_store_clear_keeps_concurrently_added_keys() -> None:
    store = InMemorySessionStore()
    await store.commit("sid", 0, SessionChangeset(sets={"a": 1}))
    _, version = (await store.load("sid"))  # type: ignore[misc]
    # A concurrent request adds a key and commits first.
    await store.commit("sid", version, SessionChangeset(sets={"b": 2}))
    # This request clears everything it saw (only "a").
    await store.commit("sid", version, SessionChangeset(deletes=frozenset({"a"})))
    data, _ = (await store.load("sid"))  # type: ignore[misc]
    assert data == {"b": 2}


async def _make_app() -> tuple[Quart, InMemorySessionInterface]:
    app = Quart(__name__)
    interface = InMemorySessionInterface()
    app.session_interface = interface

    entered = 0
    release = asyncio.Event()

    @app.route("/prime")
    async def prime() -> str:
        session["existing"] = "yes"
        return "ok"

    @app.route("/write/<key>/<value>")
    async def write(key: str, value: str) -> str:
        nonlocal entered
        session[key] = value
        # Hold both requests until each has opened its session and
        # staged its write, so both save against the same version.
        entered += 1
        if entered == 2:
            release.set()
        await release.wait()
        return "ok"

    return app, interface


async def _stored_data(interface: InMemorySessionInterface) -> dict:
    assert len(interface.store._sessions) == 1
    sid = next(iter(interface.store._sessions))
    stored = await interface.store.load(sid)
    assert stored is not None
    return stored[0]


async def test_concurrent_requests_merge() -> None:
    app, interface = await _make_app()
    client = app.test_client()
    await client.get("/prime")
    response_a, response_b = await asyncio.gather(
        client.get("/write/left/1"), client.get("/write/right/2")
    )
    assert response_a.status_code == 200
    assert response_b.status_code == 200
    # The store is already updated now that the responses (and hence
    # their headers) have been sent.
    assert await _stored_data(interface) == {
        "existing": "yes",
        "left": "1",
        "right": "2",
    }


async def test_concurrent_requests_conflict() -> None:
    app, interface = await _make_app()
    client = app.test_client()
    await client.get("/prime")
    response_a, response_b = await asyncio.gather(
        client.get("/write/shared/1"), client.get("/write/shared/2")
    )
    # The loser is reported as a 500 (SessionConflictError) rather
    # than silently overwriting the winner.
    statuses = sorted([response_a.status_code, response_b.status_code])
    assert statuses == [200, 500]
    stored = await _stored_data(interface)
    assert stored["existing"] == "yes"
    assert stored["shared"] in {"1", "2"}


async def test_session_saved_before_response_headers() -> None:
    app = Quart(__name__)
    interface = InMemorySessionInterface()
    app.session_interface = interface

    @app.route("/")
    async def index() -> str:
        session["key"] = "value"
        return "ok"

    client = app.test_client()
    response = await client.get("/")
    assert response.status_code == 200
    # The response has been fully sent, so the save must have
    # completed before the headers were emitted.
    assert await _stored_data(interface) == {"key": "value"}


async def test_session_roundtrip_via_cookie() -> None:
    app = Quart(__name__)
    app.session_interface = InMemorySessionInterface()

    @app.route("/set/<value>")
    async def set_value(value: str) -> str:
        session["key"] = value
        return "ok"

    @app.route("/get")
    async def get_value() -> str:
        return session.get("key", "missing")

    client = app.test_client()
    await client.get("/set/stored")
    response = await client.get("/get")
    assert (await response.get_data()) == b"stored"


async def test_websocket_session_write_is_saved() -> None:
    app = Quart(__name__)
    interface = InMemorySessionInterface()
    app.session_interface = interface

    @app.websocket("/ws")
    async def ws() -> None:
        # The connection has already been accepted at this point;
        # writing the session commits to the store on close.
        session["ws_key"] = "ws_value"
        await websocket.send("done")

    client = app.test_client()
    async with client.websocket("/ws") as test_websocket:
        assert await test_websocket.receive() == "done"
    assert await _stored_data(interface) == {"ws_key": "ws_value"}


async def test_websocket_session_conflict_is_raised() -> None:
    app = Quart(__name__)
    interface = InMemorySessionInterface()
    app.session_interface = interface
    session_ = VersionedSession({"key": "old"}, sid="sid", version=0, new=False)
    await interface.commit("sid", 0, session_.changeset)
    session_._rebase(1)
    # A concurrent commit moves the same key forward.
    await interface.commit(
        "sid", session_.version, SessionChangeset(sets={"key": "other"})
    )
    session_["key"] = "new"
    with pytest.raises(SessionConflictError):
        await interface.save_session(app, session_, None)
