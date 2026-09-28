from __future__ import annotations

import zlib
from collections.abc import AsyncIterator
from types import TracebackType

from .wrappers.request import Request
from .wrappers.response import Response
from .wrappers.response import ResponseBody

# Statuses that must not carry a message body, and hence cannot be
# compressed.
_NO_ENTITY_STATUSES = frozenset({204, 304})


def _parse_accept_encoding(value: str) -> dict[str, float]:
    """Parse an Accept-Encoding header into a {coding: quality} mapping."""
    encodings: dict[str, float] = {}
    for item in value.split(","):
        coding, _, parameters = item.strip().partition(";")
        coding = coding.strip().lower()
        if coding == "":
            continue
        quality = 1.0
        for parameter in parameters.split(";"):
            name, _, raw_quality = parameter.strip().partition("=")
            if name.strip().lower() == "q":
                try:
                    quality = float(raw_quality)
                except ValueError:
                    quality = 0.0
        encodings[coding] = min(max(quality, 0.0), 1.0)
    return encodings


def gzip_is_acceptable(request: Request) -> bool:
    """Check if the request allows a gzip encoded response.

    An explicit ``gzip`` entry wins over any wildcard entry, a
    wildcard only applies if gzip isn't mentioned, and ``q=0``
    forbids the coding. Without an Accept-Encoding header only the
    identity coding is assumed acceptable.
    """
    header = request.headers.get("Accept-Encoding")
    if header is None:
        return False
    encodings = _parse_accept_encoding(header)
    if "gzip" in encodings:
        return encodings["gzip"] > 0
    return encodings.get("*", 0.0) > 0


class GzipResponseBody(ResponseBody):
    """Wraps a response body gzip-compressing the data as it streams.

    Each upstream chunk is flushed through the compressor so that
    data is sent as it is generated rather than being buffered. On
    exit - including when the client disconnects mid-stream - the
    compressor is discarded and the wrapped body is exited, which
    closes the upstream iterator.
    """

    def __init__(self, body: ResponseBody, *, compresslevel: int = 6) -> None:
        self.body = body
        self._compresslevel = compresslevel
        self._compressor: zlib.Compress | None = None

    async def __aenter__(self) -> GzipResponseBody:
        await self.body.__aenter__()
        self._compressor = zlib.compressobj(
            self._compresslevel, zlib.DEFLATED, 16 + zlib.MAX_WBITS
        )
        return self

    async def __aexit__(
        self, exc_type: type, exc_value: BaseException, tb: TracebackType
    ) -> None:
        self._compressor = None
        await self.body.__aexit__(exc_type, exc_value, tb)

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._generate()

    async def _generate(self) -> AsyncIterator[bytes]:
        compressor = self._compressor
        if compressor is None:
            return
        async for chunk in self.body:
            if isinstance(chunk, str):
                chunk = chunk.encode()
            if not chunk:
                continue
            data = compressor.compress(chunk) + compressor.flush(zlib.Z_SYNC_FLUSH)
            if data:
                yield data
        tail = compressor.flush()
        if tail:
            yield tail


def should_compress_response(
    request: Request, response: Response, min_size: int
) -> bool:
    """Check if the response should be gzip compressed.

    Responses are not compressed if the request is a HEAD request,
    the status cannot carry a body (1xx, 204, 304), the response is
    already content encoded, is a range response, forbids
    transformation, or has a known length smaller than ``min_size``.
    Streaming responses of unknown length are compressed without
    buffering to determine their size.
    """
    if not gzip_is_acceptable(request):
        return False
    if request.method == "HEAD":
        return False
    if response.status_code < 200 or response.status_code in _NO_ENTITY_STATUSES:
        return False
    if response.headers.get("Content-Encoding") is not None:
        return False
    if "Content-Range" in response.headers:
        return False
    if "no-transform" in response.headers.get("Cache-Control", "").lower():
        return False
    content_length = response.content_length
    if content_length is not None and content_length < min_size:
        return False
    return True


def compress_response(response: Response, *, compresslevel: int = 6) -> None:
    """Compress the response body, adjusting the headers to match.

    The Content-Length is removed as the compressed length is
    unknown whilst streaming, any strong ETag is weakened as it
    cannot validate the encoded body, and Accept-Encoding is merged
    into the Vary header.
    """
    response.response = GzipResponseBody(response.response, compresslevel=compresslevel)
    response.headers["Content-Encoding"] = "gzip"
    response.headers.pop("Content-Length", None)
    etag = response.headers.get("ETag")
    if etag is not None and not etag.startswith("W/"):
        response.headers["ETag"] = f"W/{etag}"
    if "*" not in response.headers.get("Vary", ""):
        response.vary.add("Accept-Encoding")
