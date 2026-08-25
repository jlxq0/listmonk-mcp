"""Bind-address resolution and listener separation.

The assertion that carries the security weight is
:func:`test_metrics_never_defaults_to_all_interfaces`: a metrics listener that
falls back to ``0.0.0.0`` is reachable through the same Service that carries
``/mcp``, which is the thing the two-listener split exists to prevent.
"""

from __future__ import annotations

import asyncio

import pytest
from starlette.applications import Starlette

from listmonk_mcp import transport


class FakeServer:
    """Stands in for uvicorn.Server. Records whether it was asked to exit."""

    def __init__(
        self,
        runtime: float,
        error: Exception | None = None,
        ignore_should_exit: bool = False,
    ) -> None:
        self.runtime = runtime
        self.error = error
        self.ignore_should_exit = ignore_should_exit
        self.should_exit = False

    async def serve(self) -> None:
        deadline = self.runtime
        elapsed = 0.0
        step = 0.005
        while elapsed < deadline:
            if self.should_exit and not self.ignore_should_exit:
                break
            await asyncio.sleep(step)
            elapsed += step
        if self.error is not None:
            raise self.error


class TestResolveBindAddr:
    def test_defaults_to_all_interfaces_on_3000(self) -> None:
        assert transport.resolve_bind_addr(None) == ("0.0.0.0", 3000)

    def test_empty_string_falls_back_to_default(self) -> None:
        assert transport.resolve_bind_addr("") == ("0.0.0.0", 3000)

    def test_explicit_value_wins(self) -> None:
        assert transport.resolve_bind_addr("127.0.0.1:8080") == ("127.0.0.1", 8080)

    def test_ipv6_host_is_unbracketed(self) -> None:
        assert transport.resolve_bind_addr("[::]:3000") == ("::", 3000)

    @pytest.mark.parametrize(
        "bad", ["3000", "host:", "host:abc", "host:0", "host:65536", ":3000"]
    )
    def test_rejects_malformed(self, bad: str) -> None:
        with pytest.raises(ValueError):
            transport.resolve_bind_addr(bad)


class TestResolveMetricsBindAddr:
    def test_metrics_never_defaults_to_all_interfaces(self) -> None:
        host, port = transport.resolve_metrics_bind_addr(None, None)
        assert host != "0.0.0.0"
        assert (host, port) == ("127.0.0.1", 9090)

    def test_pod_ip_is_used_when_no_explicit_value(self) -> None:
        assert transport.resolve_metrics_bind_addr(None, "10.0.0.5") == (
            "10.0.0.5",
            9090,
        )

    def test_explicit_value_beats_pod_ip(self) -> None:
        assert transport.resolve_metrics_bind_addr("10.1.1.1:9999", "10.0.0.5") == (
            "10.1.1.1",
            9999,
        )

    def test_blank_explicit_value_falls_through_to_pod_ip(self) -> None:
        assert transport.resolve_metrics_bind_addr("   ", "10.0.0.5") == (
            "10.0.0.5",
            9090,
        )

    def test_ipv6_pod_ip_is_bracketed_before_parsing(self) -> None:
        assert transport.resolve_metrics_bind_addr(None, "fd00::1") == ("fd00::1", 9090)

    def test_all_interfaces_only_when_asked_for_by_name(self) -> None:
        assert transport.resolve_metrics_bind_addr("0.0.0.0:9090", None) == (
            "0.0.0.0",
            9090,
        )


class TestBindAddrsFromEnv:
    def test_bare_environment_gives_public_wide_and_metrics_loopback(self) -> None:
        public, internal = transport.bind_addrs_from_env({})
        assert public == ("0.0.0.0", 3000)
        assert internal == ("127.0.0.1", 9090)

    def test_pod_ip_moves_only_the_metrics_listener(self) -> None:
        public, internal = transport.bind_addrs_from_env({"POD_IP": "10.42.0.7"})
        assert public == ("0.0.0.0", 3000)
        assert internal == ("10.42.0.7", 9090)

    def test_both_overridable(self) -> None:
        public, internal = transport.bind_addrs_from_env(
            {
                "LISTMONK_MCP_BIND_ADDR": "127.0.0.1:4000",
                "LISTMONK_MCP_METRICS_BIND_ADDR": "127.0.0.1:9191",
                "POD_IP": "10.42.0.7",
            }
        )
        assert public == ("127.0.0.1", 4000)
        assert internal == ("127.0.0.1", 9191)

    def test_the_two_listeners_are_distinct(self) -> None:
        public, internal = transport.bind_addrs_from_env({"POD_IP": "10.42.0.7"})
        assert public != internal


class TestMetricsApp:
    def test_serves_metrics_and_nothing_else(self) -> None:
        app = transport.create_metrics_app()
        paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
        assert paths == {"/metrics"}


class TestServe:
    """Shutdown coordination between the two listeners.

    A fake stands in for uvicorn.Server: the real one binds sockets, and the
    behaviour under test is the coordination, not the HTTP.
    """

    @pytest.fixture
    def anyio_backend(self) -> str:
        return "asyncio"

    @staticmethod
    def _fake_pair(
        monkeypatch: pytest.MonkeyPatch, first: FakeServer, second: FakeServer
    ) -> None:
        servers = iter((first, second))

        def factory(config: object) -> FakeServer:
            return next(servers)

        monkeypatch.setattr(transport.uvicorn, "Server", factory)
        monkeypatch.setattr(transport.uvicorn, "Config", lambda *a, **k: object())

    @pytest.mark.anyio
    async def test_one_listener_exiting_stops_the_other(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        public = FakeServer(runtime=0.01)
        internal = FakeServer(runtime=100.0)
        self._fake_pair(monkeypatch, public, internal)

        await transport.serve(
            Starlette(),
            public_addr=("127.0.0.1", 1),
            metrics_addr=("127.0.0.1", 2),
        )

        assert internal.should_exit, (
            "a process serving metrics but not MCP passes its liveness probe "
            "while doing nothing useful"
        )

    @pytest.mark.anyio
    async def test_a_bind_failure_on_the_metrics_listener_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        public = FakeServer(runtime=100.0)
        internal = FakeServer(runtime=0.01, error=OSError("address already in use"))
        self._fake_pair(monkeypatch, public, internal)

        with pytest.raises(OSError, match="address already in use"):
            await transport.serve(
                Starlette(),
                public_addr=("127.0.0.1", 1),
                metrics_addr=("127.0.0.1", 2),
            )

    @pytest.mark.anyio
    async def test_a_failure_during_the_shutdown_window_is_not_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The public listener stops first; the metrics listener then fails while
        # winding down. `gather(return_exceptions=True)` would eat this.
        public = FakeServer(runtime=0.01)
        internal = FakeServer(runtime=0.05, error=RuntimeError("shutdown blew up"))
        self._fake_pair(monkeypatch, public, internal)

        with pytest.raises(RuntimeError, match="shutdown blew up"):
            await transport.serve(
                Starlette(),
                public_addr=("127.0.0.1", 1),
                metrics_addr=("127.0.0.1", 2),
            )

    @pytest.mark.anyio
    async def test_a_listener_that_never_stops_is_cancelled_and_reported(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(transport, "SHUTDOWN_TIMEOUT", 0.05)
        public = FakeServer(runtime=0.01)
        internal = FakeServer(runtime=100.0, ignore_should_exit=True)
        self._fake_pair(monkeypatch, public, internal)

        with caplog.at_level("WARNING"):
            await transport.serve(
                Starlette(),
                public_addr=("127.0.0.1", 1),
                metrics_addr=("127.0.0.1", 2),
            )

        assert "did not stop" in caplog.text
        assert "metrics-listener" in caplog.text
