from __future__ import annotations

import asyncio
import gzip
import zlib
from collections.abc import AsyncGenerator

import pytest
from hypercorn.typing import HTTPScope

from quart import Quart
from quart import Response
from quart import ResponseReturnValue
from quart.testing import make_test_headers_path_and_query_string
from quart.testing import make_test_scope
from quart.testing.connections import TestHTTPConnection as HTTPConnection


@pytest.fixture(name="app")
def _app() -> Quart:
    app = Quart(__name__)
    app.config["RESPONSE_COMPRESSION"] = True

    @app.route("/")
    async def index() -> ResponseReturnValue:
        return "x" * 1000

    @app.route("/small")
    async def small() -> ResponseReturnValue:
        return "tiny"

    @app.route("/encoded")
    async def encoded() -> ResponseReturnValue:
        return Response("z" * 1000, headers={"Content-Encoding": "identity"})

    @app.route("/no-entity")
    async def no_entity() -> ResponseReturnValue:
        return "", 204

    @app.route("/stream")
    async def stream() -> ResponseReturnValue:
        async def generator() -> AsyncGenerator[bytes, None]:
            yield b"a" * 100
            yield b"b" * 100

        return generator()

    @app.route("/empty-stream")
    async def empty_stream() -> ResponseReturnValue:
        async def generator() -> AsyncGenerator[bytes, None]:
            return
            yield b""

        return generator()

    @app.route("/headers")
    async def headers() -> ResponseReturnValue:
        response = Response("h" * 1000)
        response.set_etag("tag")
        response.headers["Vary"] = "User-Agent"
        return response

    @app.route("/vary-star")
    async def vary_star() -> ResponseReturnValue:
        return Response("s" * 1000, headers={"Vary": "*"})

    return app


def _make_scope(app: Quart, method: str = "GET") -> HTTPScope:
    headers, path, query_string = make_test_headers_path_and_query_string(
        app, "/", {"Accept-Encoding": "gzip"}
    )
    return make_test_scope(
        "http", path, method, headers, query_string, "http", "", "1.1", None
    )


async def _raw_get(app: Quart, path: str) -> HTTPConnection:
    headers, path, query_string = make_test_headers_path_and_query_string(
        app, path, {"Accept-Encoding": "gzip"}
    )
    scope = make_test_scope(
        "http", path, "GET", headers, query_string, "http", "", "1.1", None
    )
    connection = HTTPConnection(app, scope)
    async with connection:
        await connection.send_complete()
    return connection


@pytest.mark.parametrize(
    "accept_encoding, compressed",
    [
        ("gzip", True),
        ("GZip", True),
        ("gzip;q=0.5", True),
        ("gzip;q=0", False),
        ("gzip;q=0, *;q=1", False),
        ("*", True),
        ("*;q=0", False),
        ("br, gzip", True),
        ("identity", False),
        ("identity;q=0", False),
        ("br", False),
        (None, False),
    ],
)
async def test_gzip_negotiation(
    app: Quart, accept_encoding: str | None, compressed: bool
) -> None:
    headers = {} if accept_encoding is None else {"Accept-Encoding": accept_encoding}
    response = await app.test_client().get("/", headers=headers)
    assert response.status_code == 200
    if compressed:
        assert response.headers["Content-Encoding"] == "gzip"
        assert gzip.decompress(await response.get_data()) == b"x" * 1000
    else:
        assert "Content-Encoding" not in response.headers
        assert (await response.get_data()) == b"x" * 1000


async def test_compression_disabled_by_default() -> None:
    app = Quart(__name__)

    @app.route("/")
    async def index() -> ResponseReturnValue:
        return "x" * 1000

    response = await app.test_client().get("/", headers={"Accept-Encoding": "gzip"})
    assert "Content-Encoding" not in response.headers
    assert (await response.get_data()) == b"x" * 1000


async def test_small_known_body_not_compressed(app: Quart) -> None:
    response = await app.test_client().get(
        "/small", headers={"Accept-Encoding": "gzip"}
    )
    assert "Content-Encoding" not in response.headers
    assert response.headers["Content-Length"] == "4"
    assert (await response.get_data()) == b"tiny"


async def test_already_encoded_response_not_compressed(app: Quart) -> None:
    response = await app.test_client().get(
        "/encoded", headers={"Accept-Encoding": "gzip"}
    )
    assert response.headers["Content-Encoding"] == "identity"
    assert (await response.get_data()) == b"z" * 1000


async def test_no_entity_status_not_compressed(app: Quart) -> None:
    response = await app.test_client().get(
        "/no-entity", headers={"Accept-Encoding": "gzip"}
    )
    assert response.status_code == 204
    assert "Content-Encoding" not in response.headers


async def test_unknown_length_stream_compressed(app: Quart) -> None:
    connection = await _raw_get(app, "/stream")
    assert connection.headers["Content-Encoding"] == "gzip"
    assert "Content-Length" not in connection.headers
    assert gzip.decompress(bytes(connection.response_data)) == b"a" * 100 + b"b" * 100


async def test_empty_stream_compressed(app: Quart) -> None:
    response = await app.test_client().get(
        "/empty-stream", headers={"Accept-Encoding": "gzip"}
    )
    assert response.headers["Content-Encoding"] == "gzip"
    assert gzip.decompress(await response.get_data()) == b""


async def test_compressed_response_headers(app: Quart) -> None:
    connection = await _raw_get(app, "/headers")
    assert connection.headers["Content-Encoding"] == "gzip"
    assert "Content-Length" not in connection.headers
    assert connection.headers["ETag"] == 'W/"tag"'
    vary = {part.strip() for part in connection.headers["Vary"].split(",")}
    assert vary == {"User-Agent", "Accept-Encoding"}


async def test_vary_star_unchanged(app: Quart) -> None:
    response = await app.test_client().get(
        "/vary-star", headers={"Accept-Encoding": "gzip"}
    )
    assert response.headers["Content-Encoding"] == "gzip"
    assert response.headers["Vary"] == "*"


async def test_head_request_not_compressed(app: Quart) -> None:
    response = await app.test_client().head("/", headers={"Accept-Encoding": "gzip"})
    assert "Content-Encoding" not in response.headers
    assert response.headers["Content-Length"] == "1000"


async def test_first_chunk_sent_before_stream_completes() -> None:
    app = Quart(__name__)
    app.config["RESPONSE_COMPRESSION"] = True
    blocker = asyncio.Event()

    @app.route("/")
    async def index() -> ResponseReturnValue:
        async def generator() -> AsyncGenerator[bytes, None]:
            yield b"a" * 100
            await blocker.wait()
            yield b"b" * 100

        return generator()

    async with HTTPConnection(app, _make_scope(app)) as connection:
        await connection.send_complete()
        # The first chunk must arrive without waiting for the
        # generator to complete i.e. the stream isn't buffered.
        first = await asyncio.wait_for(connection.receive(), timeout=1)
        assert first != b""
        blocker.set()
    data = first + bytes(connection.response_data)
    assert gzip.decompress(data) == b"a" * 100 + b"b" * 100


async def test_disconnect_closes_upstream_iterator() -> None:
    app = Quart(__name__)
    app.config["RESPONSE_COMPRESSION"] = True
    blocker = asyncio.Event()
    closed = False

    @app.route("/")
    async def index() -> ResponseReturnValue:
        async def generator() -> AsyncGenerator[bytes, None]:
            nonlocal closed
            try:
                yield b"a" * 100
                await blocker.wait()
                yield b"b" * 100
            finally:
                closed = True

        return generator()

    async with HTTPConnection(app, _make_scope(app)) as connection:
        await connection.send_complete()
        await asyncio.wait_for(connection.receive(), timeout=1)
        await connection.disconnect()
    assert closed


async def test_exception_during_compression_wire_result() -> None:
    app = Quart(__name__)
    app.config["RESPONSE_COMPRESSION"] = True

    @app.route("/")
    async def index() -> ResponseReturnValue:
        async def generator() -> AsyncGenerator[bytes, None]:
            yield b"a" * 100
            raise RuntimeError("boom")

        return generator()

    connection = HTTPConnection(app, _make_scope(app))
    with pytest.raises(ExceptionGroup) as exc_info:
        async with connection:
            await connection.send_complete()
            first = await asyncio.wait_for(connection.receive(), timeout=1)
    assert any(isinstance(error, RuntimeError) for error in exc_info.value.exceptions)
    # The headers and first compressed chunk are already on the wire
    # when the error aborts the stream.
    assert connection.status_code == 200
    assert connection.headers["Content-Encoding"] == "gzip"
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    assert decompressor.decompress(first) == b"a" * 100
