from __future__ import annotations

import zlib
from types import TracebackType
from typing import Any
from typing import TYPE_CHECKING

from werkzeug.datastructures import Accept
from werkzeug.exceptions import NotAcceptable

from .wrappers.response import ResponseBody

if TYPE_CHECKING:
    from .wrappers.request import Request
    from .wrappers.response import Response

#: Statuses that must not carry a message body.
_NO_ENTITY_STATUSES = frozenset({204, 304})
#: Statuses carrying a partial body that must not be transformed.
_NO_TRANSFORM_STATUSES = frozenset({206})


def _encoding_quality(accept: Accept, encoding: str) -> float | None:
    """Return the qvalue for the encoding in an Accept-Encoding value.

    An explicit entry takes precedence over a ``*`` wildcard entry, as
    required by RFC 9110. Returns None if the encoding is not
    mentioned at all.
    """
    wildcard = None
    for item, quality in accept:
        item = item.lower()
        if item == encoding:
            return quality
        elif item == "*" and wildcard is None:
            wildcard = quality
    return wildcard


def choose_content_encoding(accept: Accept) -> str | None:
    """Choose a content-coding based on the Accept-Encoding header.

    Only gzip is supported, so this returns "gzip" if it is acceptable
    (qvalue greater than 0) and None otherwise.
    """
    quality = _encoding_quality(accept, "gzip")
    if quality is not None and quality > 0:
        return "gzip"
    return None


def identity_acceptable(accept: Accept) -> bool:
    """Return True if the identity (unencoded) content-coding is acceptable.

    The identity coding is acceptable unless it is explicitly refused
    via ``identity;q=0`` or a ``*;q=0`` wildcard.
    """
    quality = _encoding_quality(accept, "identity")
    return quality is None or quality > 0


class GZipResponseBody(ResponseBody):
    """Wraps another response body, gzip-compressing it as it streams.

    The compression is incremental - each upstream chunk is compressed
    and emitted as it arrives, so the response is streamed to the
    client rather than buffered.
    """

    def __init__(self, body: ResponseBody, *, compresslevel: int = 6) -> None:
        self.body = body
        self.compresslevel = compresslevel
        self._compressor: Any = None
        self._iterator: Any = None
        self._flushed = False

    async def __aenter__(self) -> GZipResponseBody:
        self._compressor = zlib.compressobj(
            self.compresslevel, zlib.DEFLATED, zlib.MAX_WBITS | 16
        )
        wrapped = await self.body.__aenter__()
        self._iterator = wrapped.__aiter__()
        return self

    async def __aexit__(
        self, exc_type: type, exc_value: BaseException, tb: TracebackType
    ) -> None:
        # Discard the compressor and ensure the upstream body is
        # closed, e.g. when the client disconnects mid-stream.
        self._compressor = None
        await self.body.__aexit__(exc_type, exc_value, tb)

    def __aiter__(self) -> GZipResponseBody:
        return self

    async def __anext__(self) -> bytes:
        while True:
            if self._compressor is None or self._flushed:
                raise StopAsyncIteration
            try:
                data = await self._iterator.__anext__()
            except StopAsyncIteration:
                self._flushed = True
                tail = self._compressor.flush()
                if tail:
                    return tail
                raise
            if isinstance(data, str):
                data = data.encode()
            chunk = self._compressor.compress(data)
            if chunk:
                return chunk


def compress_response(
    request: Request,
    response: Response,
    *,
    min_size: int = 500,
    compresslevel: int = 6,
) -> Response:
    """Compress the response body based on the request's Accept-Encoding.

    The response is returned unchanged (sent as the identity coding)
    if any of the following apply,

    - the request method is HEAD;
    - the status must not carry a body (1xx, 204, 304) or carries a
      partial body (206);
    - the response already has a Content-Encoding;
    - the response has a ``no-transform`` cache directive;
    - the client does not accept gzip;
    - the Content-Length is known and smaller than ``min_size``
      (responses of unknown length are always compressed if gzip is
      acceptable, without buffering them to measure their size).

    Otherwise the body is gzip-compressed as it streams, the
    Content-Length and any strong ETag are removed, and Accept-Encoding
    is merged into the Vary header.

    Raises:
        NotAcceptable: If the client refuses the identity coding and
            no acceptable content-coding can be offered.
    """
    if (
        request.method == "HEAD"
        or response.status_code < 200
        or response.status_code in _NO_ENTITY_STATUSES | _NO_TRANSFORM_STATUSES
        or "Content-Encoding" in response.headers
        or "no-transform" in response.cache_control
    ):
        return response

    accept = request.accept_encodings
    encoding = choose_content_encoding(accept)
    if encoding is None:
        if not identity_acceptable(accept):
            raise NotAcceptable(
                "The identity content-coding is refused and no other "
                "content-coding can be offered"
            )
        return response

    content_length = response.content_length
    if (
        content_length is not None
        and content_length < min_size
        and identity_acceptable(accept)
    ):
        # Too small to be worth compressing.
        return response

    response.headers["Content-Encoding"] = encoding
    if "Content-Length" in response.headers:
        del response.headers["Content-Length"]
    etag = response.headers.get("ETag")
    if etag is not None and not etag.startswith("W/"):
        del response.headers["ETag"]
    if "*" not in response.vary:
        response.vary.add("Accept-Encoding")
    response.response = GZipResponseBody(
        response.response, compresslevel=compresslevel
    )
    return response
