"""Streamable-HTTP transport: two listeners, resolved bind addresses.

The deployment contract every server in this fleet meets:

* one **public** listener, default ``0.0.0.0:3000``, carrying ``/mcp`` and
  ``/health``. This is what the HTTPRoute targets.
* one **internal** listener, default ``127.0.0.1:9090``, carrying ``/metrics``
  and nothing else.

The split exists because ``/metrics`` must not be publicly routable. Serving
both from one listener would be less code and would put the metrics endpoint
behind the same public hostname as the MCP endpoint, which is the thing the
split prevents.

:func:`resolve_metrics_bind_addr` therefore never falls back to ``0.0.0.0``. A
pod that wants to be scraped sets ``POD_IP`` (the downward API supplies it) and
gets ``{POD_IP}:9090`` — reachable from inside the cluster, not from the
Service. A pod that sets neither gets loopback, which is scrape-proof and safe.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Final

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from . import metrics

logger = logging.getLogger(__name__)

ENV_BIND_ADDR: Final = "LISTMONK_MCP_BIND_ADDR"
ENV_METRICS_BIND_ADDR: Final = "LISTMONK_MCP_METRICS_BIND_ADDR"
ENV_POD_IP: Final = "POD_IP"

DEFAULT_BIND_ADDR: Final = "0.0.0.0:3000"
DEFAULT_METRICS_HOST: Final = "127.0.0.1"
DEFAULT_METRICS_PORT: Final = 9090

# Seconds to wait for the second listener after the first has stopped. Kept
# under a typical Kubernetes terminationGracePeriodSeconds of 30 so the
# process reports its own timeout rather than being SIGKILLed mid-report.
SHUTDOWN_TIMEOUT: Final = 25


def _split_host_port(addr: str, env_name: str) -> tuple[str, int]:
    """Split ``host:port``, tolerating a bracketed IPv6 host."""
    stripped = addr.strip()
    if not stripped:
        raise ValueError(f"{env_name} is empty")

    if stripped.startswith("["):
        closing = stripped.find("]")
        if closing == -1 or not stripped[closing + 1 :].startswith(":"):
            raise ValueError(f"invalid {env_name}: {addr}")
        host = stripped[1:closing]
        port_str = stripped[closing + 2 :]
    else:
        host, sep, port_str = stripped.rpartition(":")
        if not sep:
            raise ValueError(f"invalid {env_name}: {addr} (expected host:port)")

    if not host:
        raise ValueError(f"invalid {env_name}: {addr} (empty host)")
    try:
        port = int(port_str)
    except ValueError as exc:
        raise ValueError(f"invalid {env_name}: {addr} (port not an integer)") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"invalid {env_name}: {addr} (port out of range)")

    return host, port


def resolve_bind_addr(explicit: str | None = None) -> tuple[str, int]:
    """Resolve the public listener address. Defaults to ``0.0.0.0:3000``."""
    return _split_host_port(explicit or DEFAULT_BIND_ADDR, ENV_BIND_ADDR)


def resolve_metrics_bind_addr(
    explicit: str | None = None, pod_ip: str | None = None
) -> tuple[str, int]:
    """Resolve the metrics listener address.

    Priority: explicit env, then ``{POD_IP}:9090``, then ``127.0.0.1:9090``.
    Never returns ``0.0.0.0`` unless it was asked for by name.
    """
    if explicit and explicit.strip():
        return _split_host_port(explicit, ENV_METRICS_BIND_ADDR)
    if pod_ip and pod_ip.strip():
        ip = pod_ip.strip()
        host = f"[{ip}]" if ":" in ip else ip
        return _split_host_port(f"{host}:{DEFAULT_METRICS_PORT}", ENV_POD_IP)
    return DEFAULT_METRICS_HOST, DEFAULT_METRICS_PORT


def bind_addrs_from_env(
    env: dict[str, str] | None = None,
) -> tuple[tuple[str, int], tuple[str, int]]:
    """Resolve both listener addresses from the environment."""
    source = os.environ if env is None else env
    public = resolve_bind_addr(source.get(ENV_BIND_ADDR))
    internal = resolve_metrics_bind_addr(
        source.get(ENV_METRICS_BIND_ADDR), source.get(ENV_POD_IP)
    )
    return public, internal


async def _metrics_endpoint(request: Request) -> Response:
    return Response(metrics.render(), media_type=metrics.CONTENT_TYPE)


def create_metrics_app() -> Starlette:
    """The internal listener's app: ``/metrics`` and nothing else."""
    return Starlette(routes=[Route("/metrics", _metrics_endpoint, methods=["GET"])])


async def serve(
    public_app: Starlette,
    *,
    public_addr: tuple[str, int],
    metrics_addr: tuple[str, int],
    log_level: str = "info",
) -> None:
    """Run the public and metrics listeners concurrently until one stops.

    If either listener exits — a bind failure, or a signal — the other is asked
    to shut down too. A process serving metrics but not MCP passes its liveness
    probe while doing nothing useful, which is worse than crashing.
    """
    public_host, public_port = public_addr
    metrics_host, metrics_port = metrics_addr

    public_server = uvicorn.Server(
        uvicorn.Config(
            public_app,
            host=public_host,
            port=public_port,
            log_level=log_level.lower(),
        )
    )
    metrics_server = uvicorn.Server(
        uvicorn.Config(
            create_metrics_app(),
            host=metrics_host,
            port=metrics_port,
            log_level=log_level.lower(),
        )
    )
    # Both servers install a SIGTERM handler and the last one to install wins,
    # so only one of them sees the signal. The FIRST_COMPLETED wait below is
    # what makes that safe: whichever server the signal reaches exits, and its
    # exit shuts the other one down.

    logger.info("MCP listener on http://%s:%d/mcp", public_host, public_port)
    logger.info("metrics listener on http://%s:%d/metrics", metrics_host, metrics_port)

    tasks = [
        asyncio.create_task(public_server.serve(), name="public-listener"),
        asyncio.create_task(metrics_server.serve(), name="metrics-listener"),
    ]
    try:
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        public_server.should_exit = True
        metrics_server.should_exit = True

        if pending:
            _, still_running = await asyncio.wait(pending, timeout=SHUTDOWN_TIMEOUT)
            if still_running:
                # Cancelling uvicorn mid-serve interrupts the ASGI lifespan, so
                # the Listmonk client may not be closed cleanly. Say so rather
                # than exiting 0 as though shutdown had been orderly.
                names = ", ".join(sorted(t.get_name() for t in still_running))
                logger.warning(
                    "%s did not stop within %ds; cancelling. Graceful shutdown "
                    "did not complete.",
                    names,
                    SHUTDOWN_TIMEOUT,
                )
                for task in still_running:
                    task.cancel()
                await asyncio.gather(*still_running, return_exceptions=True)

        # Raise the first real failure, in listener order so the result is not
        # at the mercy of set iteration. Covers the listener that finished
        # during the shutdown window as well as the one that finished first —
        # a bind failure on either is why the process is exiting.
        for task in tasks:
            if task.done() and not task.cancelled():
                task.result()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
