"""Tests for the Prometheus server-side query client used by targetMetrics
when server.prometheusUrl is configured (see graphql/queries.py).

Mocks httpx.AsyncClient.get directly rather than the network — this is a
thin, sequential/parallel query-and-aggregate function, not something that
benefits from a real HTTP mock server.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from proxy_hopper_webserver.prometheus_query import query_target_metrics


def _prom_response(value: float | None) -> MagicMock:
    """A response object shaped like Prometheus's instant-query API."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    if value is None:
        resp.json.return_value = {"status": "success", "data": {"resultType": "vector", "result": []}}
    else:
        resp.json.return_value = {
            "status": "success",
            "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1234567890, str(value)]}]},
        }
    return resp


def _mock_get_by_query(values_by_substring: dict[str, float | None]):
    """Return an AsyncMock for AsyncClient.get that picks a canned response
    by matching a substring in the `query` param — lets each of the four
    PromQL queries get its own value without depending on call order."""
    async def fake_get(url, params=None, **kwargs):
        promql = params["query"]
        for substring, value in values_by_substring.items():
            if substring in promql:
                return _prom_response(value)
        raise AssertionError(f"Unexpected PromQL query: {promql!r}")
    return AsyncMock(side_effect=fake_get)


class TestQueryTargetMetrics:
    async def test_success_computes_expected_snapshot(self):
        mock_get = _mock_get_by_query({
            'outcome="success"': 80.0,
            "proxy_hopper_requests_total": 100.0,
            "duration_seconds_sum": 25.0,
            "duration_seconds_count": 100.0,
        })
        with patch.object(httpx.AsyncClient, "get", mock_get):
            snap = await query_target_metrics("http://prom:9090", "my-target")

        assert snap.name == "my-target"
        assert snap.total_requests == 100
        assert snap.success_requests == 80
        assert snap.failed_requests == 20
        assert snap.avg_latency_ms == 250.0  # 25s / 100 requests = 0.25s = 250ms
        assert snap.last_request_at is None  # Prometheus tier never populates this

    async def test_empty_results_return_all_zero(self):
        mock_get = _mock_get_by_query({
            'outcome="success"': None,
            "proxy_hopper_requests_total": None,
            "duration_seconds_sum": None,
            "duration_seconds_count": None,
        })
        with patch.object(httpx.AsyncClient, "get", mock_get):
            snap = await query_target_metrics("http://prom:9090", "unknown-target")

        assert snap.total_requests == 0
        assert snap.success_requests == 0
        assert snap.failed_requests == 0
        assert snap.avg_latency_ms == 0.0

    async def test_one_query_failing_does_not_blank_the_others(self):
        """A single Prometheus query erroring (timeout, 500, malformed body)
        must not take down the whole panel — the other three numbers should
        still come through."""
        async def fake_get(url, params=None, **kwargs):
            promql = params["query"]
            if "duration_seconds_sum" in promql:
                raise httpx.ConnectError("boom")
            if 'outcome="success"' in promql:
                return _prom_response(80.0)
            if "proxy_hopper_requests_total" in promql:
                return _prom_response(100.0)
            if "duration_seconds_count" in promql:
                return _prom_response(100.0)
            raise AssertionError(f"unexpected query {promql!r}")

        with patch.object(httpx.AsyncClient, "get", AsyncMock(side_effect=fake_get)):
            snap = await query_target_metrics("http://prom:9090", "t")

        assert snap.total_requests == 100
        assert snap.success_requests == 80
        assert snap.failed_requests == 20
        assert snap.avg_latency_ms == 0.0  # latency_sum failed → treated as 0

    async def test_success_greater_than_total_is_capped(self):
        """Defensive: a momentary counter-scrape skew between the two series
        must never produce a negative failed_requests."""
        mock_get = _mock_get_by_query({
            'outcome="success"': 120.0,  # inconsistent with total, deliberately
            "proxy_hopper_requests_total": 100.0,
            "duration_seconds_sum": 10.0,
            "duration_seconds_count": 100.0,
        })
        with patch.object(httpx.AsyncClient, "get", mock_get):
            snap = await query_target_metrics("http://prom:9090", "t")

        assert snap.total_requests == 100
        assert snap.success_requests == 100  # capped at total
        assert snap.failed_requests == 0

    async def test_target_name_with_quote_is_escaped_in_promql(self):
        """A target name containing a double-quote must not break out of the
        PromQL label-selector string."""
        captured_queries: list[str] = []

        async def fake_get(url, params=None, **kwargs):
            captured_queries.append(params["query"])
            return _prom_response(0.0)

        with patch.object(httpx.AsyncClient, "get", AsyncMock(side_effect=fake_get)):
            await query_target_metrics("http://prom:9090", 'evil"target')

        assert all('evil\\"target' in q for q in captured_queries)

    async def test_trailing_slash_on_url_is_handled(self):
        captured_urls: list[str] = []

        async def fake_get(url, params=None, **kwargs):
            captured_urls.append(url)
            return _prom_response(0.0)

        with patch.object(httpx.AsyncClient, "get", AsyncMock(side_effect=fake_get)):
            await query_target_metrics("http://prom:9090/", "t")

        assert all(not url.startswith("http://prom:9090//") for url in captured_urls)
