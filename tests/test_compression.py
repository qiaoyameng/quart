from __future__ import annotations

import asyncio
import gzip

import pytest
from hypercorn.typing import ASGIReceiveEvent
from hypercorn.typing import ASGISendEvent
from hypercorn.typing import HTTPScope
from werkzeug.datastructures import Accept
from werkzeug.http import parse_accept_header

from quart import Quart
from quart import Response
from quart.asgi import ASGIHTTPConnection
from quart.compression import choose_content_encoding
from quart.compression import identity_acceptable


@pytest.mark.parametrize(
    "value, expected",
    [
        ("gzip", "gzip"),
        ("GZip", "gzip"),
        ("gzip;q=0.5", "gzip"),
        ("gzip;q=0", None),
        ("br, gzip;q=0", None),
        ("deflate", None),
        ("*", "gzip"),
        ("*;q=0.5", "gzip"),
        ("*;q=0", None),
        # An explicit entry takes precedence over the wildcard
        ("gzip;q=0, *;q=1", None),
        ("*;q=1, gzip;q=0", None),
        ("", None),
    ],
)
def test_choose_content_encoding(value: str, expected: str | None) -> None:
    accept = parse_accept_header(value, Accept)
    assert choose_content_encoding(accept) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        ("gzip", True),
        ("identity", True),
        ("identity;q=0", False),
        ("identity;q=0.5", True),
        ("*;q=0", False),
        ("*;q=0, identity;q=1", True),
        ("", True),
    ],
)
def test_identity_acceptable(value: str, expected: bool) -> None:
    accept = parse_accept_header(value, Accept)
    assert identity_acceptable(accept) == expected


@pytest.fixture(name="app")
def _app() -> Quart:
    app = Quart(__name__)
    app.config["RESPONSE_COMPRESSION"] = True

    @app.route("/large")
    async def large() -> str:
        return "x" * 1000

    @app.route("/small")
    async def small() -> str:
        return "x" * 10

    @app.route("/encoded")
    async def encoded() -> Response:
        return Response("x" * 1000, headers={"Content-Encoding": "br"})

    @app.route("/no-transform")
    async def no_transform() -> Response:
        response = Response("x" * 1000)
        response.cache_control.no_transform = True
        return response

    @app.route("/empty")
    async def empty() -> tuple[str, int]:
        return "", 204

    @app.route("/etag")
    async def etag() -> Response:
        response = Response("x" * 1000)
        response.set_etag("strong")
        return response

    @app.route("/weak-etag")
    async def weak_etag() -> Response:
        response = Response("x" * 1000)
        response.set_etag("weak", weak=True)
        return response

    @app.route("/vary")
    async def vary() -> Response:
        return Response("x" * 1000, headers={"Vary": "Cookie"})

    @app.route("/vary-star")
    async def vary_star() -> Response:
        return Response("x" * 1000, headers={"Vary": "*"})

    return app


async def test_disabled_by_default() -> None:
    app = Quart(__name__)

    @app.route("/")
    async def index() -> str:
        return "x" * 1000

    response = await app.test_client().get("/", headers={"Accept-Encoding": "gzip"})
    assert "Content-Encoding" not in response.headers
    assert await response.get_data(as_text=False) == b"x" * 1000


async def test_compresses_when_accepted(app: Quart) -> None:
    response = await app.test_client().get(
        "/large", headers={"Accept-Encoding": "gzip"}
    )
    assert response.status_code == 200
    assert response.headers["Content-Encoding"] == "gzip"
    assert response.headers["Vary"] == "Accept-Encoding"
    data = await response.get_data(as_text=False)
    assert gzip.decompress(data) == b"x" * 1000


async def test_not_compressed_without_accept_encoding(app: Quart) -> None:
    response = await app.test_client().get("/large")
    assert "Content-Encoding" not in response.headers
    assert await response.get_data(as_text=False) == b"x" * 1000


async def test_small_known_body_not_compressed(app: Quart) -> None:
    response = await app.test_client().get(
        "/small", headers={"Accept-Encoding": "gzip"}
    )
    assert "Content-Encoding" not in response.headers
    assert await response.get_data(as_text=False) == b"x" * 10


async def test_min_size_configurable(app: Quart) -> None:
    app.config["RESPONSE_COMPRESSION_MIN_SIZE"] = 5
    response = await app.test_client().get(
        "/small", headers={"Accept-Encoding": "gzip"}
    )
    assert response.headers["Content-Encoding"] == "gzip"
    data = await response.get_data(as_text=False)
    assert gzip.decompress(data) == b"x" * 10


async def test_already_encoded_untouched(app: Quart) -> None:
    response = await app.test_client().get(
        "/encoded", headers={"Accept-Encoding": "gzip"}
    )
    assert response.headers["Content-Encoding"] == "br"
    assert await response.get_data(as_text=False) == b"x" * 1000


async def test_no_transform_untouched(app: Quart) -> None:
    response = await app.test_client().get(
        "/no-transform", headers={"Accept-Encoding": "gzip"}
    )
    assert "Content-Encoding" not in response.headers


async def test_no_entity_status_untouched(app: Quart) -> None:
    response = await app.test_client().get(
        "/empty", headers={"Accept-Encoding": "gzip"}
    )
    assert response.status_code == 204
    assert "Content-Encoding" not in response.headers


async def test_strong_etag_removed(app: Quart) -> None:
    response = await app.test_client().get(
        "/etag", headers={"Accept-Encoding": "gzip"}
    )
    assert response.headers["Content-Encoding"] == "gzip"
    assert "ETag" not in response.headers


async def test_weak_etag_kept(app: Quart) -> None:
    response = await app.test_client().get(
        "/weak-etag", headers={"Accept-Encoding": "gzip"}
    )
    assert response.headers["Content-Encoding"] == "gzip"
    assert response.headers["ETag"] == 'W/"weak"'


async def test_vary_merged(app: Quart) -> None:
    response = await app.test_client().get(
        "/vary", headers={"Accept-Encoding": "gzip"}
    )
    assert response.headers["Vary"] == "Cookie, Accept-Encoding"


async def test_vary_star_unchanged(app: Quart) -> None:
    response = await app.test_client().get(
        "/vary-star", headers={"Accept-Encoding": "gzip"}
    )
    assert response.headers["Vary"] == "*"


async def test_identity_refused_not_acceptable(app: Quart) -> None:
    response = await app.test_client().get(
        "/large", headers={"Accept-Encoding": "identity;q=0"}
    )
    assert response.status_code == 406


async def test_identity_refused_compresses_small_body(app: Quart) -> None:
    response = await app.test_client().get(
        "/small", headers={"Accept-Encoding": "identity;q=0, gzip"}
    )
    assert response.headers["Content-Encoding"] == "gzip"
    data = await response.get_data(as_text=False)
    assert gzip.decompress(data) == b"x" * 10


def _make_scope(method: str = "GET", accept_encoding: str = "gzip") -> HTTPScope:
    return {
        "type": "http",
        "asgi": {},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"quart"),
            (b"accept-encoding", accept_encoding.encode()),
        ],
        "client": ("127.0.0.1", 80),
        "server": None,
        "extensions": {},
        "state": {},  # type: ignore[typeddict-item]
    }


def _start_headers(sent: list[ASGISendEvent]) -> dict[bytes, bytes]:
    start = next(m for m in sent if m["type"] == "http.response.start")
    return dict(start["headers"])  # type: ignore[typeddict-item]


def _bodies(sent: list[ASGISendEvent]) -> list[bytes]:
    return [
        m["body"]  # type: ignore[typeddict-item]
        for m in sent
        if m["type"] == "http.response.body"
    ]


async def test_first_chunk_sent_while_streaming() -> None:
    # The first compressed chunk must be sent as soon as the first
    # upstream chunk is available, without waiting for the stream to
    # complete.
    app = Quart(__name__)
    app.config["RESPONSE_COMPRESSION"] = True
    release = asyncio.Event()

    @app.route("/")
    async def index():  # type: ignore[no-untyped-def]
        async def gen():  # type: ignore[no-untyped-def]
            yield b"x" * 100
            await release.wait()
            yield b"y" * 100

        return gen()

    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait({"type": "http.request", "body": b"", "more_body": False})
    sent: list[ASGISendEvent] = []
    first_body_received = asyncio.Event()

    async def receive() -> ASGIReceiveEvent:
        return await queue.get()

    async def send(message: ASGISendEvent) -> None:
        sent.append(message)
        if message["type"] == "http.response.body":
            if message["more_body"]:
                first_body_received.set()
            else:
                queue.put_nowait({"type": "http.disconnect"})

    connection = ASGIHTTPConnection(app, _make_scope())
    task = asyncio.create_task(connection(receive, send))
    # This times out if Quart buffers the whole stream before sending
    await asyncio.wait_for(first_body_received.wait(), timeout=1)
    assert not release.is_set()
    release.set()
    await asyncio.wait_for(task, timeout=1)

    headers = _start_headers(sent)
    assert headers[b"content-encoding"] == b"gzip"
    assert b"content-length" not in headers
    bodies = _bodies(sent)
    assert bodies[0].startswith(b"\x1f\x8b")  # gzip magic in the first chunk
    assert gzip.decompress(b"".join(bodies)) == b"x" * 100 + b"y" * 100


async def test_head_not_compressed() -> None:
    app = Quart(__name__)
    app.config["RESPONSE_COMPRESSION"] = True

    @app.route("/")
    async def index() -> str:
        return "x" * 1000

    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait({"type": "http.request", "body": b"", "more_body": False})
    sent: list[ASGISendEvent] = []

    async def receive() -> ASGIReceiveEvent:
        return await queue.get()

    async def send(message: ASGISendEvent) -> None:
        sent.append(message)
        if message["type"] == "http.response.body" and not message["more_body"]:
            queue.put_nowait({"type": "http.disconnect"})

    connection = ASGIHTTPConnection(app, _make_scope(method="HEAD"))
    await asyncio.wait_for(connection(receive, send), timeout=1)

    headers = _start_headers(sent)
    assert b"content-encoding" not in headers
    assert headers[b"content-length"] == b"1000"


async def test_empty_stream_compressed() -> None:
    app = Quart(__name__)
    app.config["RESPONSE_COMPRESSION"] = True

    @app.route("/")
    async def index():  # type: ignore[no-untyped-def]
        async def gen():  # type: ignore[no-untyped-def]
            if False:
                yield b""

        return gen()

    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait({"type": "http.request", "body": b"", "more_body": False})
    sent: list[ASGISendEvent] = []

    async def receive() -> ASGIReceiveEvent:
        return await queue.get()

    async def send(message: ASGISendEvent) -> None:
        sent.append(message)
        if message["type"] == "http.response.body" and not message["more_body"]:
            queue.put_nowait({"type": "http.disconnect"})

    connection = ASGIHTTPConnection(app, _make_scope())
    await asyncio.wait_for(connection(receive, send), timeout=1)

    headers = _start_headers(sent)
    assert headers[b"content-encoding"] == b"gzip"
    assert b"content-length" not in headers
    # The empty stream is still a valid gzip member
    assert gzip.decompress(b"".join(_bodies(sent))) == b""
    # The stream is properly terminated
    final = [m for m in sent if m["type"] == "http.response.body"][-1]
    assert final == {"type": "http.response.body", "body": b"", "more_body": False}


async def test_streaming_error_drops_connection() -> None:
    app = Quart(__name__)
    app.config["RESPONSE_COMPRESSION"] = True
    closed = asyncio.Event()

    @app.route("/")
    async def index():  # type: ignore[no-untyped-def]
        async def gen():  # type: ignore[no-untyped-def]
            try:
                yield b"x" * 100
                raise RuntimeError("boom")
            finally:
                closed.set()

        return gen()

    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait({"type": "http.request", "body": b"", "more_body": False})
    sent: list[ASGISendEvent] = []

    async def receive() -> ASGIReceiveEvent:
        return await queue.get()

    async def send(message: ASGISendEvent) -> None:
        sent.append(message)

    connection = ASGIHTTPConnection(app, _make_scope())
    with pytest.raises(ExceptionGroup) as exc_info:
        await connection(receive, send)
    assert exc_info.value.subgroup(RuntimeError) is not None

    # The headers and first chunk were sent, but the stream is not
    # terminated - the connection is dropped mid-stream.
    headers = _start_headers(sent)
    assert headers[b"content-encoding"] == b"gzip"
    assert not any(
        m["type"] == "http.response.body" and not m["more_body"] for m in sent
    )
    assert closed.is_set()


async def test_disconnect_closes_upstream() -> None:
    app = Quart(__name__)
    app.config["RESPONSE_COMPRESSION"] = True
    closed = asyncio.Event()

    @app.route("/")
    async def index():  # type: ignore[no-untyped-def]
        async def gen():  # type: ignore[no-untyped-def]
            try:
                yield b"x" * 100
                await asyncio.Event().wait()  # Hang forever
            finally:
                closed.set()

        return gen()

    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait({"type": "http.request", "body": b"", "more_body": False})
    sent: list[ASGISendEvent] = []

    async def receive() -> ASGIReceiveEvent:
        return await queue.get()

    async def send(message: ASGISendEvent) -> None:
        sent.append(message)
        if message["type"] == "http.response.body" and message["more_body"]:
            queue.put_nowait({"type": "http.disconnect"})

    connection = ASGIHTTPConnection(app, _make_scope())
    await asyncio.wait_for(connection(receive, send), timeout=1)
    assert closed.is_set()
