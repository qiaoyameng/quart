.. _streaming_response:

Streaming responses
===================

Quart supports responses that are meant to be streamed to the client,
rather than received in one block. If you are interested in streaming
the request data see :ref:`request_body` or for duplex streaming see
:ref:`websockets`.

To stream a response the view-function should return an asynchronous
generator that yields bytes. This generator can be returned with a
status code and headers as normal. For example to stream the time
every second,

.. code-block:: python

    @app.route('/')
    async def stream_time():
        async def async_generator():
            time = datetime.isoformat()
            yield time.encode()
        return async_generator(), 200, {'X-Something': 'value'}

With context
------------

If you want to make use of the ``request`` context whilst streaming
you will need to use the :func:`quart.helpers.stream_with_context`
decorator,

.. code-block:: python

    @app.route('/')
    async def stream_time():
        @stream_with_context
        async def async_generator():
            time = datetime.isoformat()
            yield time.encode()
        return async_generator(), 200, {'X-Something': 'value'}

Timeout
-------

Quart by default will timeout long responses to protect against
possible denial of service attacks, see :ref:`dos_mitigations`. This
may be undesired for streaming responses, e.g. an indefinite
stream. The timeout can be disabled globally, however this could make
other routes DOS vulnerable, therefore the recommendation is to set
the timeout attribute on a specific response to ``None``,

.. code-block:: python

    from quart import make_response

    @app.route('/sse')
    async def stream_time():
        ...
        response = await make_response(async_generator())
        response.timeout = None  # No timeout for this route
        return response

Completion callbacks
--------------------

As the response body is streamed after the ``after_request`` functions
have run, they cannot tell you whether the response was completely
sent to the client. Instead a completion callback can be registered on
the response, which is called (within the original request context)
once sending has finished. The callback receives a
:class:`~quart.ResponseCompletion` detailing how many body bytes were
actually passed to the ASGI server and the terminal state -
``"completed"``, ``"disconnected"``, or ``"error"``,

.. code-block:: python

    from quart import make_response

    @app.route('/export')
    async def export():
        response = await make_response(async_generator())

        @response.add_completion_callback
        async def log_completion(completion):
            current_app.logger.info(
                "Export %s after %d bytes", completion.state, completion.bytes_sent
            )

        return response

The callback runs after the headers and body have been sent, so it
cannot alter them. Each response notifies its callbacks at most once,
and any error raised by a callback is logged and suppressed so that it
cannot mask an error that occurred whilst sending.

Testing
-------

The test client :meth:`~quart.testing.client.QuartClient.get` and
associated methods will collate the entire streamed response. If you
want to test that the route actually streams the response, or to test
routes that stream until the client disconnects you will need to use
the :meth:`~quart.testing.client.QuartClient.request` method,

.. code-block:: python

    async def test_stream() -> None:
        test_client = app.test_client()
        async with test_client.request(..) as connection:
            data = await connection.receive()
            assert data ...
            assert connection.status_code == 200
            ...
            await connection.disconnect()  # For infinite streams

See also
--------

:ref:`server_sent_events`
