"""Shape of the two ASGI apps, and what each one does and does not expose."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from listmonk_mcp import metrics, server, transport


def _paths(app: Starlette) -> set[str]:
    return {getattr(route, "path", "") for route in app.routes}


@pytest.fixture
def public_app() -> Starlette:
    """The public app without its lifespan.

    ``create_http_app`` wires a lifespan that dials Listmonk. These tests are
    about routing, so they drive the app with ``TestClient`` outside a lifespan
    span; any test that needs the client is an integration test and lives with
    the live-server check in the pull request, not here.
    """
    return server.create_http_app()


class TestPublicApp:
    def test_mcp_endpoint_is_mounted_at_slash_mcp(self, public_app: Starlette) -> None:
        mounts = [r for r in public_app.routes if isinstance(r, Mount)]
        assert "/mcp" in {m.path for m in mounts}

    def test_health_is_present(self, public_app: Starlette) -> None:
        assert "/health" in _paths(public_app)

    def test_metrics_is_not_reachable_on_the_public_listener(
        self, public_app: Starlette
    ) -> None:
        # The whole reason for a second listener. If this ever passes on the
        # public app, `/metrics` is exposed through the routable Service.
        assert "/metrics" not in _paths(public_app)

    def test_health_precedes_the_mcp_mount(self, public_app: Starlette) -> None:
        # Starlette matches in order; a Mount at "/" would swallow "/health".
        paths = [getattr(route, "path", "") for route in public_app.routes]
        assert paths.index("/health") < paths.index("/mcp")


class TestHealthEndpoint:
    def test_reports_healthy_with_version_and_revision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(server.ENV_BUILD_REVISION, "cafebabe")
        app = Starlette(routes=[Route("/health", server.health_endpoint)])
        with TestClient(app) as client:
            response = client.get("/health")

        assert response.status_code == 200
        body: dict[str, Any] = response.json()
        assert body["status"] == "healthy"
        assert body["revision"] == "cafebabe"
        assert body["version"] == server.get_version()

    def test_revision_is_unknown_outside_a_built_image(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(server.ENV_BUILD_REVISION, raising=False)
        app = Starlette(routes=[Route("/health", server.health_endpoint)])
        with TestClient(app) as client:
            assert client.get("/health").json()["revision"] == "unknown"

    def test_carries_no_listmonk_credential_or_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # /health is publicly routable, so its body is a public document.
        monkeypatch.setenv("LISTMONK_MCP_URL", "https://listmonk.example.com")
        monkeypatch.setenv("LISTMONK_MCP_PASSWORD", "s3cr3t-token")
        app = Starlette(routes=[Route("/health", server.health_endpoint)])
        with TestClient(app) as client:
            raw = client.get("/health").text
        assert "s3cr3t-token" not in raw
        assert "listmonk.example.com" not in raw
        assert set(json.loads(raw)) == {"status", "version", "revision"}


class TestMetricsApp:
    @pytest.fixture(autouse=True)
    def clean_registry(self) -> Iterator[None]:
        metrics.reset()
        yield
        metrics.reset()

    def test_serves_the_prometheus_text_format(self) -> None:
        metrics.record_tool_call("list_lists", "ok", 0.02)
        with TestClient(transport.create_metrics_app()) as client:
            response = client.get("/metrics")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert 'listmonk_mcp_tool_calls_total{tool="list_lists",outcome="ok"} 1' in (
            response.text
        )

    def test_carries_no_mcp_endpoint(self) -> None:
        with TestClient(transport.create_metrics_app()) as client:
            assert client.post("/mcp").status_code == 404


class TestInstrumentation:
    @pytest.fixture(autouse=True)
    def clean_registry(self) -> Iterator[None]:
        metrics.reset()
        yield
        metrics.reset()

    @pytest.mark.anyio
    async def test_a_successful_tool_call_is_counted_ok(self) -> None:
        mcp_server = server.InstrumentedFastMCP("test")

        @mcp_server.tool()
        def echo(value: str) -> str:
            return value

        await mcp_server.call_tool("echo", {"value": "hi"})

        assert 'tool="echo",outcome="ok"} 1' in metrics.render()

    @pytest.mark.anyio
    async def test_a_failing_tool_call_is_counted_error_and_still_raises(self) -> None:
        mcp_server = server.InstrumentedFastMCP("test")

        @mcp_server.tool()
        def boom() -> str:
            raise RuntimeError("upstream is down")

        with pytest.raises(ToolError):
            await mcp_server.call_tool("boom", {})

        rendered = metrics.render()
        assert 'tool="boom",outcome="error"} 1' in rendered
        # The exception message is unbounded and attacker-influenced; it must
        # never become a label value.
        assert "upstream is down" not in rendered


class TestServerConstruction:
    def test_every_registered_tool_reaches_the_http_server(self) -> None:
        registered = set(server.mcp._tool_manager._tools)
        http_tools = set(server.create_http_server()._tool_manager._tools)
        assert registered == http_tools
        assert len(registered) > 60

    def test_stdio_and_http_servers_expose_the_same_tools(self) -> None:
        stdio_tools = set(server.create_production_server()._tool_manager._tools)
        http_tools = set(server.create_http_server()._tool_manager._tools)
        assert stdio_tools == http_tools

    def test_http_server_has_no_per_session_lifespan(self) -> None:
        # FastMCP runs its lifespan once per MCP session over streamable-HTTP,
        # so the Listmonk client is managed at the ASGI lifespan instead.
        assert server.create_http_server().settings.lifespan is None

    def test_stdio_server_keeps_its_lifespan(self) -> None:
        assert server.create_production_server().settings.lifespan is not None


class TestTransportSelection:
    def test_only_two_transports_are_accepted(self) -> None:
        assert server.VALID_TRANSPORTS == {"stdio", "streamable-http"}


class TestUnknownToolCardinality:
    """An unregistered tool name is caller-controlled, so it must not become a label.

    Without this, `tools/call` with a fresh random name on every request grows
    the metric registry without bound: one label set per request, retained for
    the life of the process.
    """

    @pytest.fixture(autouse=True)
    def clean_registry(self) -> Iterator[None]:
        metrics.reset()
        yield
        metrics.reset()

    @pytest.mark.anyio
    async def test_unknown_tool_names_collapse_to_one_label(self) -> None:
        mcp_server = server.InstrumentedFastMCP("test")

        for attempt in range(5):
            with pytest.raises(ToolError):
                await mcp_server.call_tool(f"no_such_tool_{attempt}", {})

        rendered = metrics.render()
        for attempt in range(5):
            assert f"no_such_tool_{attempt}" not in rendered
        expected = f'tool="{server.UNKNOWN_TOOL}",outcome="error"}} 5'
        assert expected in rendered

    @pytest.mark.anyio
    async def test_registered_tools_keep_their_own_label(self) -> None:
        mcp_server = server.InstrumentedFastMCP("test")

        @mcp_server.tool()
        def real_tool() -> str:
            return "ok"

        await mcp_server.call_tool("real_tool", {})
        rendered = metrics.render()
        assert 'tool="real_tool",outcome="ok"} 1' in rendered
        assert server.UNKNOWN_TOOL not in rendered


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
