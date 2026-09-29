from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest
from hypercorn.typing import ASGIReceiveEvent
from hypercorn.typing import ASGISendEvent
from hypercorn.typing import HTTPScope
from hypercorn.typing import WebsocketScope
from werkzeug.datastructures import Headers

from quart import ClientDisconnectedError
from quart import Quart
from quart import request
from quart import Response
from quart import ResponseCompletion
from quart.asgi import _convert_os_error
from quart.asgi import _convert_version
from quart.asgi import _handle_exception
from quart.asgi import ASGIHTTPConnection
from quart.asgi import ASGIWebsocketConnection
from quart.utils import encode_headers


@pytest.mark.parametrize(
    "headers, expected", [([(b"host", b"quart")], "quart"), ([], "")]
)
async def test_http_1_0_host_header(headers: list, expected: str) -> None:
    app = Quart(__name__)
    scope: HTTPScope = {
        "type": "http",
        "asgi": {},
        "http_version": "1.0",
        "method": "GET",
        "scheme": "https",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 80),
        "server": None,
        "extensions": {},
        "state": {},  # type: ignore[typeddict-item]
    }
    connection = ASGIHTTPConnection(app, scope)
    request = connection._create_request_from_scope(lambda: None)  # type: ignore
    assert request.headers["host"] == expected


async def test_http_completion() -> None:
    # Ensure that the connecion callable returns on completion
    app = Quart(__name__)
    scope: HTTPScope = {
        "type": "http",
        "asgi": {},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"quart")],
        "client": ("127.0.0.1", 80),
        "server": None,
        "extensions": {},
        "state": {},  # type: ignore[typeddict-item]
    }
    connection = ASGIHTTPConnection(app, scope)

    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait({"type": "http.request", "body": b"", "more_body": False})

    async def receive() -> ASGIReceiveEvent:
        # This will block after returning the first and only entry
        return await queue.get()

    async def send(message: ASGISendEvent) -> None:
        queue.put_nowait({"type": "http.disconnect"})

    # This test fails if a timeout error is raised here
    await asyncio.wait_for(connection(receive, send), timeout=1)


@pytest.mark.parametrize(
    "request_message",
    [
        {"type": "http.request", "body": b"", "more_body": False},
        {"type": "http.request", "more_body": False},
    ],
)
async def test_http_request_without_body(request_message: dict) -> None:
    app = Quart(__name__)

    scope: HTTPScope = {
        "type": "http",
        "asgi": {},
        "http_version": "1.0",
        "method": "GET",
        "scheme": "https",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"quart")],
        "client": ("127.0.0.1", 80),
        "server": None,
        "extensions": {},
        "state": {},  # type: ignore[typeddict-item]
    }
    connection = ASGIHTTPConnection(app, scope)
    request = connection._create_request_from_scope(lambda: None)  # type: ignore

    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait(request_message)

    async def receive() -> ASGIReceiveEvent:
        # This will block after returning the first and only entry
        return await queue.get()

    body = None

    async def _handle_request() -> None:
        nonlocal body
        body = await asyncio.wait_for(request.body, timeout=1)
        await queue.put({"type": "http.disconnect"})

    # This test fails with a timeout error if the request body is not received
    # within 1 second
    async with asyncio.TaskGroup() as task_group:
        request_task = task_group.create_task(_handle_request())
        task_group.create_task(
            connection.handle_messages(request, receive, request_task)
        )

    assert body == b""


async def test_websocket_completion() -> None:
    # Ensure that the connecion callable returns on completion
    app = Quart(__name__)
    scope: WebsocketScope = {
        "type": "websocket",
        "asgi": {},
        "http_version": "1.1",
        "scheme": "wss",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"quart")],
        "client": ("127.0.0.1", 80),
        "server": None,
        "subprotocols": [],
        "extensions": {"websocket.http.response": {}},
        "state": {},  # type: ignore[typeddict-item]
    }
    connection = ASGIWebsocketConnection(app, scope)

    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait({"type": "websocket.connect"})

    async def receive() -> ASGIReceiveEvent:
        # This will block after returning the first and only entry
        return await queue.get()

    async def send(message: ASGISendEvent) -> None:
        queue.put_nowait({"type": "websocket.disconnect"})

    # This test fails if a timeout error is raised here
    await asyncio.wait_for(connection(receive, send), timeout=1)


def test_http_path_from_absolute_target() -> None:
    app = Quart(__name__)
    scope: HTTPScope = {
        "type": "http",
        "asgi": {},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "http://quart/path",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"quart")],
        "client": ("127.0.0.1", 80),
        "server": None,
        "extensions": {},
        "state": {},  # type: ignore[typeddict-item]
    }
    connection = ASGIHTTPConnection(app, scope)
    request = connection._create_request_from_scope(lambda: None)  # type: ignore
    assert request.path == "/path"


@pytest.mark.parametrize(
    "path, expected",
    [("/app", "/ "), ("/", "/ "), ("/app/", "/"), ("/app/2", "/2"), ("/x/app/2", "/ ")],
)
def test_http_path_with_root_path(path: str, expected: str) -> None:
    app = Quart(__name__)
    scope: HTTPScope = {
        "type": "http",
        "asgi": {},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "/app",
        "headers": [(b"host", b"quart")],
        "client": ("127.0.0.1", 80),
        "server": None,
        "extensions": {},
        "state": {},  # type: ignore[typeddict-item]
    }
    connection = ASGIHTTPConnection(app, scope)
    request = connection._create_request_from_scope(lambda: None)  # type: ignore
    assert request.path == expected


def test_websocket_path_from_absolute_target() -> None:
    app = Quart(__name__)
    scope: WebsocketScope = {
        "type": "websocket",
        "asgi": {},
        "http_version": "1.1",
        "scheme": "wss",
        "path": "ws://quart/path",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"quart")],
        "client": ("127.0.0.1", 80),
        "server": None,
        "subprotocols": [],
        "extensions": {"websocket.http.response": {}},
        "state": {},  # type: ignore[typeddict-item]
    }
    connection = ASGIWebsocketConnection(app, scope)
    websocket = connection._create_websocket_from_scope(lambda: None)  # type: ignore
    assert websocket.path == "/path"


@pytest.mark.parametrize(
    "path, expected",
    [("/app", "/ "), ("/", "/ "), ("/app/", "/"), ("/app/2", "/2"), ("/x/app/2", "/ ")],
)
def test_websocket_path_with_root_path(path: str, expected: str) -> None:
    app = Quart(__name__)
    scope: WebsocketScope = {
        "type": "websocket",
        "asgi": {},
        "http_version": "1.1",
        "scheme": "wss",
        "path": path,
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "/app",
        "headers": [(b"host", b"quart")],
        "client": ("127.0.0.1", 80),
        "server": None,
        "subprotocols": [],
        "extensions": {"websocket.http.response": {}},
        "state": {},  # type: ignore[typeddict-item]
    }
    connection = ASGIWebsocketConnection(app, scope)
    websocket = connection._create_websocket_from_scope(lambda: None)  # type: ignore
    assert websocket.path == expected


@pytest.mark.parametrize(
    "scope, headers, subprotocol, has_headers",
    [
        ({}, Headers(), None, False),
        ({}, Headers(), "abc", False),
        ({"asgi": {"spec_version": "2.1"}}, Headers({"a": "b"}), None, True),
        ({"asgi": {"spec_version": "2.1.1"}}, Headers({"a": "b"}), None, True),
    ],
)
async def test_websocket_accept_connection(
    scope: dict, headers: Headers, subprotocol: str | None, has_headers: bool
) -> None:
    connection = ASGIWebsocketConnection(Quart(__name__), scope)  # type: ignore
    mock_send = AsyncMock()
    await connection.accept_connection(mock_send, headers, subprotocol)

    if has_headers:
        mock_send.assert_called_with(
            {
                "subprotocol": subprotocol,
                "type": "websocket.accept",
                "headers": encode_headers(headers),
            }
        )
    else:
        mock_send.assert_called_with(
            {"headers": [], "subprotocol": subprotocol, "type": "websocket.accept"}
        )


async def test_websocket_accept_connection_warns(
    websocket_scope: WebsocketScope,
) -> None:
    connection = ASGIWebsocketConnection(Quart(__name__), websocket_scope)

    async def mock_send(message: ASGISendEvent) -> None:
        pass

    with pytest.warns(UserWarning):
        await connection.accept_connection(mock_send, Headers({"a": "b"}), None)


def test__convert_version() -> None:
    assert _convert_version("2.1") == [2, 1]


def test_http_asgi_scope_from_request() -> None:
    app = Quart(__name__)
    scope = {
        "headers": [(b"host", b"quart")],
        "http_version": "1.0",
        "method": "GET",
        "scheme": "https",
        "path": "/",
        "query_string": b"",
        "test_result": "PASSED",
    }
    connection = ASGIHTTPConnection(app, scope)  # type: ignore
    request = connection._create_request_from_scope(lambda: None)  # type: ignore
    assert request.scope["test_result"] == "PASSED"  # type: ignore


@pytest.mark.parametrize(
    "debug, testing, raises",
    [
        (True, False, False),
        (True, True, False),
        (False, True, False),
        (False, False, True),
    ],
)
async def test__handle_exception(debug: bool, testing: bool, raises: bool) -> None:
    app = Mock()
    app.config = {}
    app.debug = debug
    app.testing = testing

    if raises:
        with pytest.raises(ValueError):
            await _handle_exception(app, ValueError())
    else:
        await _handle_exception(app, ValueError())


def _completion_scope(method: str = "GET") -> HTTPScope:
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
        "headers": [(b"host", b"quart")],
        "client": ("127.0.0.1", 80),
        "server": None,
        "extensions": {},
        "state": {},  # type: ignore[typeddict-item]
    }


class _ChunkedSend:
    """A send callable that records messages and fails on a chosen body chunk."""

    def __init__(self, fail_at: int | None = None) -> None:
        self.fail_at = fail_at
        self.messages: list[ASGISendEvent] = []
        self._body_chunks = 0

    async def __call__(self, message: ASGISendEvent) -> None:
        self.messages.append(message)
        if message["type"] == "http.response.body":
            chunk = self._body_chunks
            self._body_chunks += 1
            if chunk == self.fail_at:
                raise OSError("Client disconnected")


async def test_response_completion_completed() -> None:
    app = Quart(__name__)
    completions = []
    seen_paths = []

    @app.route("/")
    async def index() -> Response:
        async def body() -> AsyncGenerator[bytes, None]:
            yield b"abc"
            yield b"de"

        response = Response(body())

        @response.add_completion_callback
        def record(completion: ResponseCompletion) -> None:
            completions.append(completion)
            # The original request context must be available
            seen_paths.append(request.path)

        return response

    test_client = app.test_client()
    response = await test_client.get("/")
    assert await response.get_data() == b"abcde"
    assert completions == [
        ResponseCompletion(bytes_sent=5, state="completed", error=None)
    ]
    assert seen_paths == ["/"]


async def test_response_completion_head_request() -> None:
    app = Quart(__name__)
    completions = []

    @app.route("/")
    async def index() -> Response:
        response = Response(b"data")
        response.add_completion_callback(completions.append)
        return response

    test_client = app.test_client()
    await test_client.head("/")
    assert len(completions) == 1
    assert completions[0].state == "completed"
    assert completions[0].bytes_sent == 4


async def test_response_completion_empty_response() -> None:
    app = Quart(__name__)
    completions = []

    @app.route("/")
    async def index() -> Response:
        response = Response(b"")
        response.add_completion_callback(completions.append)
        return response

    test_client = app.test_client()
    await test_client.get("/")
    assert completions == [
        ResponseCompletion(bytes_sent=0, state="completed", error=None)
    ]


async def test_response_completion_send_failure() -> None:
    app = Quart(__name__)
    connection = ASGIHTTPConnection(app, _completion_scope())
    request_ = connection._create_request_from_scope(lambda: None)  # type: ignore

    closed = False

    async def body() -> AsyncGenerator[bytes, None]:
        nonlocal closed
        try:
            yield b"aaa"
            yield b"bbb"
            yield b"ccc"
        finally:
            closed = True

    response = Response(body())
    completions = []
    response.add_completion_callback(completions.append)

    send = _ChunkedSend(fail_at=1)  # Fail sending the second chunk
    with pytest.raises(ClientDisconnectedError):
        await connection._send_response(_convert_os_error(send), response, request_)

    assert len(completions) == 1
    completion = completions[0]
    assert completion.state == "disconnected"
    # The failed chunk must not be counted as sent
    assert completion.bytes_sent == 3
    # The body iterator must have been closed
    assert closed


async def test_response_completion_body_exception() -> None:
    app = Quart(__name__)
    connection = ASGIHTTPConnection(app, _completion_scope())
    request_ = connection._create_request_from_scope(lambda: None)  # type: ignore

    closed = False

    async def body() -> AsyncGenerator[bytes, None]:
        nonlocal closed
        try:
            yield b"aaa"
            yield b"bbb"
            raise RuntimeError("Body failed")
        finally:
            closed = True

    response = Response(body())
    completions = []
    response.add_completion_callback(completions.append)

    with pytest.raises(RuntimeError):
        await connection._send_response(_ChunkedSend(), response, request_)

    assert len(completions) == 1
    completion = completions[0]
    assert completion.state == "error"
    assert isinstance(completion.error, RuntimeError)
    assert completion.bytes_sent == 6
    # The body iterator must have been closed
    assert closed


async def test_response_completion_callback_error_does_not_mask_send_error() -> None:
    app = Quart(__name__)
    connection = ASGIHTTPConnection(app, _completion_scope())
    request_ = connection._create_request_from_scope(lambda: None)  # type: ignore

    calls = []

    def callback(completion: ResponseCompletion) -> None:
        calls.append(completion)
        raise ValueError("Callback error")

    response = Response(b"data")
    response.add_completion_callback(callback)

    send = _ChunkedSend(fail_at=0)
    with pytest.raises(ClientDisconnectedError):
        await connection._send_response(_convert_os_error(send), response, request_)

    assert len(calls) == 1
    assert calls[0].state == "disconnected"
    assert calls[0].bytes_sent == 0


async def test_response_completion_notified_once() -> None:
    response = Response(b"data")
    completions = []
    response.add_completion_callback(completions.append)

    completion = ResponseCompletion(bytes_sent=4, state="completed")
    await response.notify_completion(completion)
    await response.notify_completion(ResponseCompletion(bytes_sent=0, state="error"))
    assert completions == [completion]
