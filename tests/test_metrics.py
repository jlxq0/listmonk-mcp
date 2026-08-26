"""Prometheus exposition: cumulative buckets, label escaping, no cardinality leak."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from listmonk_mcp import metrics


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    metrics.reset()
    yield
    metrics.reset()


def _sample(text: str, prefix: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith(prefix)]


class TestToolCalls:
    def test_counter_accumulates_per_tool_and_outcome(self) -> None:
        metrics.record_tool_call("list_subscribers", "ok", 0.01)
        metrics.record_tool_call("list_subscribers", "ok", 0.02)
        metrics.record_tool_call("list_subscribers", "error", 0.03)
        metrics.record_tool_call("create_campaign", "ok", 0.04)

        rendered = metrics.render()
        assert (
            'listmonk_mcp_tool_calls_total{tool="list_subscribers",outcome="ok"} 2'
            in rendered
        )
        assert (
            'listmonk_mcp_tool_calls_total{tool="list_subscribers",outcome="error"} 1'
            in rendered
        )
        assert (
            'listmonk_mcp_tool_calls_total{tool="create_campaign",outcome="ok"} 1'
            in rendered
        )

    def test_families_are_declared_even_with_no_samples(self) -> None:
        rendered = metrics.render()
        assert "# TYPE listmonk_mcp_tool_calls_total counter" in rendered
        assert "# TYPE listmonk_mcp_tool_latency_seconds histogram" in rendered


class TestLatencyHistogram:
    def test_buckets_are_cumulative(self) -> None:
        metrics.record_tool_call("t", "ok", 0.03)

        buckets = _sample(metrics.render(), "listmonk_mcp_tool_latency_seconds_bucket")
        counts = [int(line.rsplit(" ", 1)[1]) for line in buckets]
        assert counts == sorted(counts), "a histogram's buckets must never decrease"
        # 0.03 falls above le=0.025 and at or below le=0.05.
        assert 'le="0.025"} 0' in "\n".join(buckets)
        assert 'le="0.05"} 1' in "\n".join(buckets)

    def test_boundary_value_lands_in_its_own_bucket(self) -> None:
        # Prometheus buckets are `le` — less than *or equal to*.
        metrics.record_tool_call("t", "ok", 0.05)
        assert 'le="0.05"} 1' in metrics.render()

    def test_inf_bucket_uses_prometheus_spelling_and_holds_every_sample(self) -> None:
        metrics.record_tool_call("t", "ok", 0.001)
        metrics.record_tool_call("t", "ok", 999.0)

        rendered = metrics.render()
        assert 'le="+Inf"} 2' in rendered
        assert "le=\"inf\"" not in rendered
        assert "listmonk_mcp_tool_latency_seconds_count{tool=\"t\"} 2" in rendered

    def test_sum_tracks_observed_seconds(self) -> None:
        metrics.record_tool_call("t", "ok", 0.25)
        metrics.record_tool_call("t", "ok", 0.75)
        assert 'listmonk_mcp_tool_latency_seconds_sum{tool="t"} 1.0' in metrics.render()


class TestLabelEscaping:
    def test_quotes_backslashes_and_newlines_are_escaped(self) -> None:
        metrics.record_tool_call('we"ird\\tool\nname', "ok", 0.0)
        rendered = metrics.render()
        assert 'tool="we\\"ird\\\\tool\\nname"' in rendered
        # An unescaped newline would split one sample into two malformed lines.
        for line in rendered.splitlines():
            assert line.startswith("#") or line.count(" ") >= 1


class TestBuildInfo:
    def test_absent_until_set(self) -> None:
        assert "listmonk_mcp_build_info" not in metrics.render()

    def test_exposed_as_a_gauge_of_one(self) -> None:
        metrics.set_build_info("0.2.0", "deadbeef")
        rendered = metrics.render()
        assert "# TYPE listmonk_mcp_build_info gauge" in rendered
        assert (
            'listmonk_mcp_build_info{version="0.2.0",revision="deadbeef"} 1' in rendered
        )


class TestExposition:
    def test_content_type_is_the_text_format(self) -> None:
        assert metrics.CONTENT_TYPE.startswith("text/plain")
        assert "version=0.0.4" in metrics.CONTENT_TYPE

    def test_output_ends_with_a_newline(self) -> None:
        # Prometheus rejects a body whose final line is unterminated.
        assert metrics.render().endswith("\n")
