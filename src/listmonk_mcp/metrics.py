"""Prometheus metrics, exposed on a separate cluster-internal listener.

The listener is separate from the public one (see :mod:`listmonk_mcp.transport`)
so that the routable Service never surfaces ``/metrics``. That reason is about
what is reachable from outside the cluster, not about the language, so it holds
here exactly as it does in the Rust servers in this fleet.

Label discipline: every label is low-cardinality. Never label by subscriber,
email address, campaign body or credential. Bounded by tool name (~70) and
outcome class (two). Per-subscriber detail is what the Listmonk activity log is
for; mixing it into metrics breaks the Prometheus storage model and leaks
recipient identity to anyone who can reach the endpoint.

The text exposition is hand-written rather than taken from ``prometheus-client``
because the format below is a few dozen lines and adding a dependency to a
locked, deployed image is a larger cost than maintaining them.
"""

from __future__ import annotations

import threading
from typing import Final

_NAMESPACE: Final = "listmonk_mcp"

# Buckets tuned for tool-call latency: spans local argument validation through
# a round-trip to Listmonk's HTTP API.
_TOOL_LATENCY_BUCKETS: Final[tuple[float, ...]] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

_lock = threading.Lock()
_tool_calls: dict[tuple[str, str], int] = {}
_tool_latency_buckets: dict[str, list[int]] = {}
_tool_latency_sum: dict[str, float] = {}
_tool_latency_count: dict[str, int] = {}
_build_info: dict[str, str] = {}


def set_build_info(version: str, revision: str) -> None:
    """Record the build identity exposed as ``listmonk_mcp_build_info``."""
    with _lock:
        _build_info.clear()
        _build_info.update(version=version, revision=revision)


def record_tool_call(tool: str, outcome: str, elapsed_seconds: float) -> None:
    """Record one finished tool call.

    ``outcome`` is a class, not a message: ``ok`` or ``error``. Exception text
    is unbounded and user-controlled, so it never becomes a label.
    """
    with _lock:
        _tool_calls[(tool, outcome)] = _tool_calls.get((tool, outcome), 0) + 1

        buckets = _tool_latency_buckets.setdefault(
            tool, [0] * (len(_TOOL_LATENCY_BUCKETS) + 1)
        )
        for index, bound in enumerate(_TOOL_LATENCY_BUCKETS):
            if elapsed_seconds <= bound:
                buckets[index] += 1
        buckets[-1] += 1

        _tool_latency_sum[tool] = _tool_latency_sum.get(tool, 0.0) + elapsed_seconds
        _tool_latency_count[tool] = _tool_latency_count.get(tool, 0) + 1


def reset() -> None:
    """Drop all recorded samples. Test-only."""
    with _lock:
        _tool_calls.clear()
        _tool_latency_buckets.clear()
        _tool_latency_sum.clear()
        _tool_latency_count.clear()
        _build_info.clear()


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(**pairs: str) -> str:
    if not pairs:
        return ""
    body = ",".join(f'{k}="{_escape_label_value(v)}"' for k, v in pairs.items())
    return "{" + body + "}"


def _format_float(value: float) -> str:
    # Prometheus wants `+Inf`, not Python's `inf`.
    if value == float("inf"):
        return "+Inf"
    return repr(value)


def render() -> str:
    """Render the Prometheus text exposition format (version 0.0.4)."""
    lines: list[str] = []

    with _lock:
        build_info = dict(_build_info)
        tool_calls = dict(_tool_calls)
        latency_buckets = {k: list(v) for k, v in _tool_latency_buckets.items()}
        latency_sum = dict(_tool_latency_sum)
        latency_count = dict(_tool_latency_count)

    if build_info:
        lines.append(
            f"# HELP {_NAMESPACE}_build_info Build identity of the running server."
        )
        lines.append(f"# TYPE {_NAMESPACE}_build_info gauge")
        lines.append(f"{_NAMESPACE}_build_info{_labels(**build_info)} 1")

    lines.append(
        f"# HELP {_NAMESPACE}_tool_calls_total "
        "Total MCP tool calls served. Labels: tool, outcome."
    )
    lines.append(f"# TYPE {_NAMESPACE}_tool_calls_total counter")
    for (tool, outcome), count in sorted(tool_calls.items()):
        labels = _labels(tool=tool, outcome=outcome)
        lines.append(f"{_NAMESPACE}_tool_calls_total{labels} {count}")

    lines.append(
        f"# HELP {_NAMESPACE}_tool_latency_seconds "
        "Wall-clock latency of MCP tool calls, in seconds."
    )
    lines.append(f"# TYPE {_NAMESPACE}_tool_latency_seconds histogram")
    for tool in sorted(latency_buckets):
        buckets = latency_buckets[tool]
        bounds = (*_TOOL_LATENCY_BUCKETS, float("inf"))
        for bound, cumulative in zip(bounds, buckets, strict=True):
            labels = _labels(tool=tool, le=_format_float(bound))
            lines.append(f"{_NAMESPACE}_tool_latency_seconds_bucket{labels} {cumulative}")
        tool_labels = _labels(tool=tool)
        lines.append(
            f"{_NAMESPACE}_tool_latency_seconds_sum{tool_labels} "
            f"{latency_sum.get(tool, 0.0)!r}"
        )
        lines.append(
            f"{_NAMESPACE}_tool_latency_seconds_count{tool_labels} "
            f"{latency_count.get(tool, 0)}"
        )

    return "\n".join(lines) + "\n"


CONTENT_TYPE: Final = "text/plain; version=0.0.4; charset=utf-8"
