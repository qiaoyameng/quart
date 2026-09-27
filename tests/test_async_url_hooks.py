from __future__ import annotations

from typing import Any

import pytest

from quart import async_url_for
from quart import Blueprint
from quart import g
from quart import Quart
from quart import ResponseReturnValue
from quart import url_for
from quart import websocket


async def test_async_url_value_preprocessor() -> None:
    app = Quart(__name__)

    @app.url_value_preprocessor
    async def preprocessor(endpoint: str | None, view_args: dict) -> None:
        view_args["value"] = view_args["value"].upper()

    @app.route("/<value>/")
    async def route(value: str) -> ResponseReturnValue:
        return value

    test_client = app.test_client()
    response = await test_client.get("/hello/")
    assert (await response.get_data()) == b"HELLO"


async def test_sync_url_value_preprocessor() -> None:
    app = Quart(__name__)

    @app.url_value_preprocessor
    def preprocessor(endpoint: str | None, view_args: dict) -> None:
        view_args["value"] = "sync"

    @app.route("/<value>/")
    async def route(value: str) -> ResponseReturnValue:
        return value

    test_client = app.test_client()
    response = await test_client.get("/hello/")
    assert (await response.get_data()) == b"sync"


async def test_async_url_value_preprocessor_nested_blueprint_order() -> None:
    app = Quart(__name__)
    calls: list[str] = []

    @app.url_value_preprocessor
    async def app_preprocessor(endpoint: str | None, view_args: dict) -> None:
        calls.append("app")
        view_args["value"] += "a"

    parent = Blueprint("parent", __name__)
    child = Blueprint("child", __name__)

    @parent.url_value_preprocessor
    async def parent_preprocessor(endpoint: str | None, view_args: dict) -> None:
        calls.append("parent")
        view_args["value"] += "p"

    @child.url_value_preprocessor
    async def child_preprocessor(endpoint: str | None, view_args: dict) -> None:
        calls.append("child")
        view_args["value"] += "c"

    @child.route("/<value>/")
    async def route(value: str) -> ResponseReturnValue:
        return value

    parent.register_blueprint(child)
    app.register_blueprint(parent)

    test_client = app.test_client()
    response = await test_client.get("/-/")
    assert (await response.get_data()) == b"-apc"
    assert calls == ["app", "parent", "child"]


async def test_async_url_value_preprocessor_exception_handling() -> None:
    app = Quart(__name__)

    class UnknownTenantError(Exception):
        pass

    @app.url_value_preprocessor
    async def preprocessor(endpoint: str | None, view_args: dict) -> None:
        raise UnknownTenantError(view_args["tenant"])

    @app.errorhandler(UnknownTenantError)
    async def handler(error: UnknownTenantError) -> ResponseReturnValue:
        return str(error), 409

    @app.route("/<tenant>/")
    async def route(tenant: str) -> ResponseReturnValue:
        return tenant

    test_client = app.test_client()
    response = await test_client.get("/acme/")
    assert response.status_code == 409
    assert (await response.get_data()) == b"acme"


async def test_async_url_value_preprocessor_websocket() -> None:
    app = Quart(__name__)

    @app.url_value_preprocessor
    async def preprocessor(endpoint: str | None, view_args: dict) -> None:
        view_args["value"] = "altered"

    @app.websocket("/ws/<value>/")
    async def ws(value: str) -> None:
        await websocket.send(value)

    test_client = app.test_client()
    async with test_client.websocket("/ws/original/") as test_websocket:
        result = await test_websocket.receive()
    assert result == "altered"


async def test_async_url_defaults() -> None:
    app = Quart(__name__)

    @app.url_defaults
    async def defaults(endpoint: str, values: dict) -> None:
        values.setdefault("value", "hello")

    @app.route("/param/<value>")
    async def param(value: str) -> ResponseReturnValue:
        return value

    async with app.test_request_context("/"):
        assert await async_url_for("param") == "/param/hello"
        assert await async_url_for("param", value="explicit") == "/param/explicit"


async def test_async_url_defaults_with_sync_function() -> None:
    app = Quart(__name__)

    @app.url_defaults
    def defaults(endpoint: str, values: dict) -> None:
        values.setdefault("value", "hello")

    @app.route("/param/<value>")
    async def param(value: str) -> ResponseReturnValue:
        return value

    async with app.test_request_context("/"):
        assert await async_url_for("param") == "/param/hello"


async def test_url_for_raises_with_async_url_defaults() -> None:
    app = Quart(__name__)

    @app.url_defaults
    async def defaults(endpoint: str, values: dict) -> None:
        values.setdefault("value", "hello")

    @app.route("/param/<value>")
    async def param(value: str) -> ResponseReturnValue:
        return value

    async with app.test_request_context("/"):
        with pytest.raises(RuntimeError, match="async_url_for"):
            url_for("param")


async def test_request_and_build_consistency() -> None:
    # The same blueprint hooks serve the incoming request (resolving
    # the tenant alias in the path) and the reverse URL building.
    app = Quart(__name__)
    blueprint = Blueprint("tenant", __name__, url_prefix="/<tenant_alias>")

    tenants = {"acme": 1, "globex": 2}

    async def lookup_tenant(alias: str) -> int:
        return tenants[alias]

    @blueprint.url_value_preprocessor
    async def resolve_tenant(endpoint: str | None, view_args: dict) -> None:
        alias = view_args.pop("tenant_alias")
        g.tenant_alias = alias
        view_args["tenant_id"] = await lookup_tenant(alias)

    @blueprint.url_defaults
    async def inject_tenant(endpoint: str, values: dict) -> None:
        values.setdefault("tenant_alias", g.tenant_alias)

    @blueprint.route("/page/")
    async def page(tenant_id: int) -> ResponseReturnValue:
        implicit = await async_url_for(".page")
        explicit = await async_url_for(".page", tenant_alias="globex")
        return {"tenant_id": tenant_id, "implicit": implicit, "explicit": explicit}

    app.register_blueprint(blueprint)

    test_client = app.test_client()
    response = await test_client.get("/acme/page/")
    assert response.status_code == 200
    assert await response.get_json() == {
        "tenant_id": 1,
        "implicit": "/acme/page/",
        "explicit": "/globex/page/",
    }
