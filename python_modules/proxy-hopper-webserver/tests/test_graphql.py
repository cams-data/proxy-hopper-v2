"""Tests for the GraphQL API — schema, queries, mutations, auth enforcement."""

from __future__ import annotations

import pytest
import pytest_asyncio

from proxy_hopper.auth import AuthenticatedUser
from proxy_hopper.backend.memory import MemoryBackend
from proxy_hopper.config import AuthConfig, IpPool, IpRequest, ProxyProvider, ResolvedIP, TargetConfig
from proxy_hopper.config_store.memory import MemoryConfigStore
from proxy_hopper.repository import ProxyRepository
from proxy_hopper_webserver.graphql import schema
from proxy_hopper_webserver.graphql.context import Context


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def backend():
    b = MemoryBackend()
    await b.start()
    yield b
    await b.stop()


@pytest_asyncio.fixture
async def repo(backend):
    return ProxyRepository(config_store=MemoryConfigStore(), backend=backend)


def _ctx(
    repo: ProxyRepository,
    role: str = "admin",
    auth_enabled: bool = False,
    app_metrics=None,
    ip_health=None,
    prometheus_url: str | None = None,
    read_only: bool = False,
) -> Context:
    user = AuthenticatedUser(sub=role, role=role, is_api_key=False)
    auth_config = AuthConfig(enabled=auth_enabled)
    return Context(
        repo=repo, user=user, auth_config=auth_config,
        app_metrics=app_metrics, ip_health=ip_health, prometheus_url=prometheus_url,
        read_only=read_only,
    )


async def _run(
    query: str,
    repo: ProxyRepository,
    role: str = "admin",
    variables: dict | None = None,
    app_metrics=None,
    ip_health=None,
    prometheus_url: str | None = None,
    read_only: bool = False,
):
    result = await schema.execute(
        query,
        context_value=_ctx(
            repo, role=role, app_metrics=app_metrics, ip_health=ip_health,
            prometheus_url=prometheus_url, read_only=read_only,
        ),
        variable_values=variables,
    )
    return result


def _make_target(name="t", pool_name="pool", ip_list=None, **kwargs) -> TargetConfig:
    ips = ip_list or ["1.1.1.1:3128"]
    resolved = []
    for entry in ips:
        host, _, port_str = entry.rpartition(":")
        resolved.append(ResolvedIP(host=host, port=int(port_str)))
    return TargetConfig(name=name, regex=r".*", pool_name=pool_name, resolved_ips=resolved, **kwargs)


def _make_pool(name="pool", provider="prov", count=1) -> IpPool:
    return IpPool(name=name, ip_requests=[IpRequest(provider=provider, count=count)])


# ---------------------------------------------------------------------------
# Query — targets
# ---------------------------------------------------------------------------

class TestQueryTargets:
    async def test_empty_returns_empty_list(self, repo):
        result = await _run("{ targets { name } }", repo)
        assert result.errors is None
        assert result.data["targets"] == []

    async def test_returns_seeded_target(self, repo):
        await repo.add_target(_make_target("api", "pool"))
        result = await _run("{ targets { name regex mutable poolName } }", repo)
        assert result.errors is None
        targets = result.data["targets"]
        assert len(targets) == 1
        assert targets[0]["name"] == "api"
        assert targets[0]["poolName"] == "pool"
        assert targets[0]["mutable"] is True

    async def test_target_by_name_found(self, repo):
        await repo.add_target(_make_target("api"))
        result = await _run('{ target(name: "api") { name } }', repo)
        assert result.errors is None
        assert result.data["target"]["name"] == "api"

    async def test_target_by_name_not_found_returns_null(self, repo):
        result = await _run('{ target(name: "ghost") { name } }', repo)
        assert result.errors is None
        assert result.data["target"] is None

    async def test_resolved_ips_exposed(self, repo):
        cfg = _make_target("t", ip_list=["10.0.0.1:3128", "10.0.0.2:3128"])
        await repo.add_target(cfg)
        result = await _run("{ targets { resolvedIps { host port } } }", repo)
        assert result.errors is None
        ips = result.data["targets"][0]["resolvedIps"]
        hosts = {ip["host"] for ip in ips}
        assert hosts == {"10.0.0.1", "10.0.0.2"}


# ---------------------------------------------------------------------------
# Query — pools
# ---------------------------------------------------------------------------

class TestQueryPools:
    async def test_empty_returns_empty_list(self, repo):
        result = await _run("{ pools { name } }", repo)
        assert result.errors is None
        assert result.data["pools"] == []

    async def test_returns_added_pool(self, repo):
        await repo.add_pool(_make_pool("shared", "prov", 3))
        result = await _run("{ pools { name ipRequests { provider count } mutable } }", repo)
        assert result.errors is None
        pools = result.data["pools"]
        assert len(pools) == 1
        assert pools[0]["name"] == "shared"
        assert pools[0]["ipRequests"][0]["provider"] == "prov"
        assert pools[0]["ipRequests"][0]["count"] == 3

    async def test_pool_by_name_found(self, repo):
        await repo.add_pool(_make_pool("p"))
        result = await _run('{ pool(name: "p") { name } }', repo)
        assert result.errors is None
        assert result.data["pool"]["name"] == "p"

    async def test_pool_by_name_not_found_returns_null(self, repo):
        result = await _run('{ pool(name: "ghost") { name } }', repo)
        assert result.errors is None
        assert result.data["pool"] is None


# ---------------------------------------------------------------------------
# Query — providers
# ---------------------------------------------------------------------------

class TestQueryProviders:
    async def test_empty_returns_empty_list(self, repo):
        result = await _run("{ providers { name } }", repo)
        assert result.errors is None
        assert result.data["providers"] == []

    async def test_returns_added_provider(self, repo):
        p = ProxyProvider(name="prov", ip_list=["1.1.1.1:3128"])
        await repo.add_provider(p)
        result = await _run("{ providers { name ipList mutable hasAuth } }", repo)
        assert result.errors is None
        providers = result.data["providers"]
        assert providers[0]["name"] == "prov"
        assert providers[0]["hasAuth"] is False

    async def test_has_auth_true_when_auth_set(self, repo):
        from proxy_hopper.config import BasicAuth
        p = ProxyProvider(
            name="secure",
            ip_list=["1.1.1.1:3128"],
            auth=BasicAuth(username="u", password="p"),
        )
        await repo.add_provider(p)
        result = await _run('{ provider(name: "secure") { hasAuth } }', repo)
        assert result.errors is None
        assert result.data["provider"]["hasAuth"] is True

    async def test_provider_by_name_not_found_returns_null(self, repo):
        result = await _run('{ provider(name: "ghost") { name } }', repo)
        assert result.errors is None
        assert result.data["provider"] is None


# ---------------------------------------------------------------------------
# Query — status
# ---------------------------------------------------------------------------

class TestQueryStatus:
    async def test_status_returns_caller_info(self, repo):
        result = await _run("{ status { authEnabled userSub userRole } }", repo, role="viewer")
        assert result.errors is None
        s = result.data["status"]
        assert s["authEnabled"] is False
        assert s["userSub"] == "viewer"
        assert s["userRole"] == "viewer"


# ---------------------------------------------------------------------------
# Query — targetMetrics
# ---------------------------------------------------------------------------

TARGET_METRICS_QUERY = """
query($name: String!) {
  targetMetrics(name: $name) {
    name totalRequests successRequests failedRequests avgLatencyMs lastRequestAt
  }
}
"""


class TestQueryTargetMetrics:
    async def test_returns_null_when_neither_source_configured(self, repo):
        """Neither app_metrics nor prometheus_url set on Context — matches
        a freshly-embedded admin server with server.prometheusUrl unset but
        somehow no AppMetricsStore either (shouldn't normally happen via the
        real cli.py wiring, but the resolver must degrade gracefully)."""
        result = await _run(TARGET_METRICS_QUERY, repo, variables={"name": "t"})
        assert result.errors is None
        assert result.data["targetMetrics"] is None

    async def test_app_metrics_tier_returns_recorded_snapshot(self, backend, repo):
        from proxy_hopper.app_metrics import AppMetricsStore

        store = AppMetricsStore(backend)
        await store.record("t", success=True, elapsed_seconds=0.1)
        await store.record("t", success=False, elapsed_seconds=0.3)

        result = await _run(
            TARGET_METRICS_QUERY, repo, variables={"name": "t"}, app_metrics=store
        )
        assert result.errors is None
        m = result.data["targetMetrics"]
        assert m["name"] == "t"
        assert m["totalRequests"] == 2
        assert m["successRequests"] == 1
        assert m["failedRequests"] == 1
        assert m["avgLatencyMs"] == 200.0
        assert m["lastRequestAt"] is not None

    async def test_app_metrics_tier_zero_snapshot_for_unknown_target(self, backend, repo):
        from proxy_hopper.app_metrics import AppMetricsStore

        store = AppMetricsStore(backend)
        result = await _run(
            TARGET_METRICS_QUERY, repo, variables={"name": "never-seen"}, app_metrics=store
        )
        assert result.errors is None
        m = result.data["targetMetrics"]
        assert m["totalRequests"] == 0
        assert m["lastRequestAt"] is None

    async def test_prometheus_tier_takes_priority_over_app_metrics(self, backend, repo, monkeypatch):
        """When prometheus_url is set, the resolver must use it instead of
        app_metrics — even if an AppMetricsStore with real data is also on
        the context (the two are mutually exclusive by cli.py's own wiring,
        but the resolver's priority order is what's under test here)."""
        from proxy_hopper.app_metrics import AppMetricsStore, TargetMetricsSnapshot

        store = AppMetricsStore(backend)
        await store.record("t", success=True, elapsed_seconds=0.1)

        prom_snapshot = TargetMetricsSnapshot(
            name="t", total_requests=999, success_requests=900,
            failed_requests=99, avg_latency_ms=42.5, last_request_at=None,
        )

        async def fake_query(prometheus_url, target):
            assert prometheus_url == "http://prom:9090"
            assert target == "t"
            return prom_snapshot

        monkeypatch.setattr(
            "proxy_hopper_webserver.prometheus_query.query_target_metrics", fake_query
        )

        result = await _run(
            TARGET_METRICS_QUERY, repo, variables={"name": "t"},
            app_metrics=store, prometheus_url="http://prom:9090",
        )
        assert result.errors is None
        m = result.data["targetMetrics"]
        assert m["totalRequests"] == 999  # from Prometheus, not the AppMetricsStore's 1
        assert m["avgLatencyMs"] == 42.5
        assert m["lastRequestAt"] is None


# ---------------------------------------------------------------------------
# Query — providerIpHealth / poolIpHealth
# ---------------------------------------------------------------------------

PROVIDER_IP_HEALTH_QUERY = """
query($providerName: String!) {
  providerIpHealth(providerName: $providerName) {
    address provider status lastCheckAt reason
  }
}
"""

POOL_IP_HEALTH_QUERY = """
query($poolName: String!) {
  poolIpHealth(poolName: $poolName) {
    address provider status lastCheckAt reason
  }
}
"""


class TestQueryProviderIpHealth:
    async def test_unknown_provider_returns_empty_list(self, repo):
        result = await _run(
            PROVIDER_IP_HEALTH_QUERY, repo, variables={"providerName": "ghost"}
        )
        assert result.errors is None
        assert result.data["providerIpHealth"] == []

    async def test_neither_source_configured_returns_unknown_rows(self, repo):
        await repo.add_provider(ProxyProvider(name="p", ip_list=["1.1.1.1:3128", "2.2.2.2:3128"]))
        result = await _run(
            PROVIDER_IP_HEALTH_QUERY, repo, variables={"providerName": "p"}
        )
        assert result.errors is None
        rows = result.data["providerIpHealth"]
        assert {r["address"] for r in rows} == {"1.1.1.1:3128", "2.2.2.2:3128"}
        assert all(r["status"] is None for r in rows)

    async def test_ip_health_store_tier_returns_recorded_status(self, backend, repo):
        from proxy_hopper.ip_health import IpHealthStore

        await repo.add_provider(ProxyProvider(name="p", ip_list=["1.1.1.1:3128", "2.2.2.2:3128"]))
        store = IpHealthStore(backend)
        await store.record("1.1.1.1:3128", success=True, provider="p")
        await store.record("2.2.2.2:3128", success=False, provider="p", reason="timeout")

        result = await _run(
            PROVIDER_IP_HEALTH_QUERY, repo, variables={"providerName": "p"}, ip_health=store,
        )
        assert result.errors is None
        by_address = {r["address"]: r for r in result.data["providerIpHealth"]}
        assert by_address["1.1.1.1:3128"]["status"] == "up"
        assert by_address["2.2.2.2:3128"]["status"] == "down"
        assert by_address["2.2.2.2:3128"]["reason"] == "timeout"

    async def test_prometheus_tier_takes_priority_over_ip_health_store(self, backend, repo, monkeypatch):
        from proxy_hopper.ip_health import IpHealthSnapshot, IpHealthStore

        await repo.add_provider(ProxyProvider(name="p", ip_list=["1.1.1.1:3128"]))
        store = IpHealthStore(backend)
        await store.record("1.1.1.1:3128", success=False, provider="p", reason="timeout")

        async def fake_query(prometheus_url, addresses):
            assert prometheus_url == "http://prom:9090"
            assert addresses == ["1.1.1.1:3128"]
            return {"1.1.1.1:3128": IpHealthSnapshot(
                address="1.1.1.1:3128", provider="p", status="up",
                last_check_at="2026-01-01T00:00:00+00:00", reason=None,
            )}

        monkeypatch.setattr(
            "proxy_hopper_webserver.prometheus_query.query_ip_health", fake_query
        )

        result = await _run(
            PROVIDER_IP_HEALTH_QUERY, repo, variables={"providerName": "p"},
            ip_health=store, prometheus_url="http://prom:9090",
        )
        assert result.errors is None
        row = result.data["providerIpHealth"][0]
        assert row["status"] == "up"  # from Prometheus, not the store's "down"


class TestQueryPoolIpHealth:
    async def test_unknown_pool_returns_empty_list(self, repo):
        result = await _run(POOL_IP_HEALTH_QUERY, repo, variables={"poolName": "ghost"})
        assert result.errors is None
        assert result.data["poolIpHealth"] == []

    async def test_returns_rows_for_pools_resolved_member_ips(self, repo):
        await repo.add_provider(ProxyProvider(name="p", ip_list=["1.1.1.1:3128", "2.2.2.2:3128", "3.3.3.3:3128"]))
        await repo.add_pool(_make_pool("pool", "p", count=2))

        result = await _run(POOL_IP_HEALTH_QUERY, repo, variables={"poolName": "pool"})
        assert result.errors is None
        rows = result.data["poolIpHealth"]
        # First-N selection — only the first 2 of the provider's 3 IPs.
        assert {r["address"] for r in rows} == {"1.1.1.1:3128", "2.2.2.2:3128"}
        assert all(r["provider"] == "p" for r in rows)

    async def test_ip_health_store_tier_returns_recorded_status(self, backend, repo):
        from proxy_hopper.ip_health import IpHealthStore

        await repo.add_provider(ProxyProvider(name="p", ip_list=["1.1.1.1:3128"]))
        await repo.add_pool(_make_pool("pool", "p", count=1))
        store = IpHealthStore(backend)
        await store.record("1.1.1.1:3128", success=True, provider="p")

        result = await _run(
            POOL_IP_HEALTH_QUERY, repo, variables={"poolName": "pool"}, ip_health=store,
        )
        assert result.errors is None
        assert result.data["poolIpHealth"][0]["status"] == "up"


# ---------------------------------------------------------------------------
# Mutation — targets
# ---------------------------------------------------------------------------

ADD_TARGET = """
mutation($input: TargetInput!) {
  addTarget(input: $input) { name regex mutable poolName resolvedIps { host port } }
}
"""

UPDATE_TARGET = """
mutation($input: TargetInput!) {
  updateTarget(input: $input) { name minRequestInterval poolName }
}
"""

REMOVE_TARGET = """
mutation($name: String!) { removeTarget(name: $name) }
"""


class TestMutationAddTarget:
    async def test_add_persists_and_returns_target(self, repo):
        await repo.add_provider(ProxyProvider(name="prov", ip_list=["1.1.1.1:3128"]))
        await repo.add_pool(_make_pool("pool", "prov", 1))
        result = await _run(ADD_TARGET, repo, variables={
            "input": {"name": "t", "regex": ".*", "poolName": "pool"}
        })
        assert result.errors is None
        t = result.data["addTarget"]
        assert t["name"] == "t"
        assert t["poolName"] == "pool"
        assert t["mutable"] is True
        assert t["resolvedIps"][0]["host"] == "1.1.1.1"

    async def test_add_with_unknown_pool_returns_error(self, repo):
        result = await _run(ADD_TARGET, repo, variables={
            "input": {"name": "t", "regex": ".*", "poolName": "nonexistent"}
        })
        assert result.errors is not None

    async def test_add_duplicate_returns_error(self, repo):
        await repo.add_provider(ProxyProvider(name="prov", ip_list=["1.1.1.1:3128"]))
        await repo.add_pool(_make_pool("pool", "prov", 1))
        await repo.add_target(_make_target("dup", "pool"))
        result = await _run(ADD_TARGET, repo, variables={
            "input": {"name": "dup", "regex": ".*", "poolName": "pool"}
        })
        assert result.errors is not None

    async def test_add_respects_custom_settings(self, repo):
        await repo.add_provider(ProxyProvider(name="prov", ip_list=["1.1.1.1:3128"]))
        await repo.add_pool(_make_pool("pool", "prov", 1))
        result = await _run(ADD_TARGET, repo, variables={
            "input": {
                "name": "t", "regex": ".*", "poolName": "pool",
                "minRequestInterval": 5.0, "numRetries": 1,
            }
        })
        assert result.errors is None
        cfg = await repo.get_target("t")
        assert cfg.min_request_interval == 5.0
        assert cfg.num_retries == 1


class TestMutationUpdateTarget:
    async def test_update_changes_stored_value(self, repo):
        await repo.add_provider(ProxyProvider(name="prov", ip_list=["1.1.1.1:3128"]))
        await repo.add_pool(_make_pool("pool", "prov", 1))
        await repo.add_target(_make_target("t", "pool"))
        result = await _run(UPDATE_TARGET, repo, variables={
            "input": {"name": "t", "regex": ".*", "poolName": "pool", "minRequestInterval": 9.0}
        })
        assert result.errors is None
        assert result.data["updateTarget"]["minRequestInterval"] == 9.0

    async def test_update_nonexistent_returns_error(self, repo):
        await repo.add_provider(ProxyProvider(name="prov", ip_list=["1.1.1.1:3128"]))
        await repo.add_pool(_make_pool("pool", "prov", 1))
        result = await _run(UPDATE_TARGET, repo, variables={
            "input": {"name": "ghost", "regex": ".*", "poolName": "pool"}
        })
        assert result.errors is not None

    async def test_update_immutable_target_returns_error(self, repo):
        await repo.add_provider(ProxyProvider(name="prov", ip_list=["1.1.1.1:3128"]))
        await repo.add_pool(_make_pool("pool", "prov", 1))
        await repo.add_target(_make_target("frozen", "pool", mutable=False))
        result = await _run(UPDATE_TARGET, repo, variables={
            "input": {"name": "frozen", "regex": ".*", "poolName": "pool"}
        })
        assert result.errors is not None


class TestMutationRemoveTarget:
    async def test_remove_returns_true(self, repo):
        await repo.add_target(_make_target("t"))
        result = await _run(REMOVE_TARGET, repo, variables={"name": "t"})
        assert result.errors is None
        assert result.data["removeTarget"] is True

    async def test_remove_absent_target_is_noop(self, repo):
        result = await _run(REMOVE_TARGET, repo, variables={"name": "ghost"})
        assert result.errors is None
        assert result.data["removeTarget"] is True


# ---------------------------------------------------------------------------
# Mutation — pools
# ---------------------------------------------------------------------------

ADD_POOL = """
mutation($input: IpPoolInput!) {
  addPool(input: $input) { name ipRequests { provider count } mutable }
}
"""

UPDATE_POOL = """
mutation($input: IpPoolInput!) {
  updatePool(input: $input) { name ipRequests { count } }
}
"""

REMOVE_POOL = """
mutation($name: String!) { removePool(name: $name) }
"""


class TestMutationAddPool:
    async def test_add_persists_and_returns_pool(self, repo):
        result = await _run(ADD_POOL, repo, variables={
            "input": {"name": "p", "ipRequests": [{"provider": "prov", "count": 3}]}
        })
        assert result.errors is None
        p = result.data["addPool"]
        assert p["name"] == "p"
        assert p["ipRequests"][0]["count"] == 3

    async def test_add_duplicate_returns_error(self, repo):
        await repo.add_pool(_make_pool("p"))
        result = await _run(ADD_POOL, repo, variables={
            "input": {"name": "p", "ipRequests": [{"provider": "prov", "count": 1}]}
        })
        assert result.errors is not None


class TestMutationUpdatePool:
    async def test_update_changes_count(self, repo):
        await repo.add_provider(ProxyProvider(name="prov", ip_list=["1.1.1.1:3128"]))
        await repo.add_pool(_make_pool("p", "prov", 1))
        result = await _run(UPDATE_POOL, repo, variables={
            "input": {"name": "p", "ipRequests": [{"provider": "prov", "count": 1}]}
        })
        assert result.errors is None
        assert result.data["updatePool"]["ipRequests"][0]["count"] == 1

    async def test_update_nonexistent_returns_error(self, repo):
        result = await _run(UPDATE_POOL, repo, variables={
            "input": {"name": "ghost", "ipRequests": [{"provider": "prov", "count": 1}]}
        })
        assert result.errors is not None


class TestMutationRemovePool:
    async def test_remove_returns_true(self, repo):
        await repo.add_pool(_make_pool("p"))
        result = await _run(REMOVE_POOL, repo, variables={"name": "p"})
        assert result.errors is None
        assert result.data["removePool"] is True


# ---------------------------------------------------------------------------
# Mutation — providers
# ---------------------------------------------------------------------------

ADD_PROVIDER = """
mutation($input: ProviderInput!) {
  addProvider(input: $input) { name ipList mutable hasAuth }
}
"""

UPDATE_PROVIDER = """
mutation($input: ProviderInput!) {
  updateProvider(input: $input) { name ipList }
}
"""

REMOVE_PROVIDER = """
mutation($name: String!) { removeProvider(name: $name) }
"""

ADD_IP_TO_PROVIDER = """
mutation($provider: String!, $address: String!) {
  addIpToProvider(provider: $provider, address: $address) { name ipList }
}
"""

REMOVE_IP_FROM_PROVIDER = """
mutation($provider: String!, $address: String!) {
  removeIpFromProvider(provider: $provider, address: $address) { name ipList }
}
"""


class TestMutationAddProvider:
    async def test_add_persists_and_returns_provider(self, repo):
        result = await _run(ADD_PROVIDER, repo, variables={
            "input": {"name": "prov", "ipList": ["1.1.1.1:3128", "2.2.2.2:3128"]}
        })
        assert result.errors is None
        p = result.data["addProvider"]
        assert p["name"] == "prov"
        assert len(p["ipList"]) == 2
        assert p["hasAuth"] is False

    async def test_add_with_auth_sets_has_auth(self, repo):
        result = await _run(ADD_PROVIDER, repo, variables={
            "input": {
                "name": "secure",
                "ipList": ["1.1.1.1:3128"],
                "auth": {"username": "u", "password": "secret"},
            }
        })
        assert result.errors is None
        assert result.data["addProvider"]["hasAuth"] is True

    async def test_add_duplicate_returns_error(self, repo):
        await repo.add_provider(ProxyProvider(name="prov", ip_list=["1.1.1.1:3128"]))
        result = await _run(ADD_PROVIDER, repo, variables={
            "input": {"name": "prov", "ipList": ["2.2.2.2:3128"]}
        })
        assert result.errors is not None


class TestMutationUpdateProvider:
    async def test_update_changes_ip_list(self, repo):
        await repo.add_provider(ProxyProvider(name="p", ip_list=["1.1.1.1:3128"]))
        result = await _run(UPDATE_PROVIDER, repo, variables={
            "input": {"name": "p", "ipList": ["9.9.9.9:3128"]}
        })
        assert result.errors is None
        assert result.data["updateProvider"]["ipList"] == ["9.9.9.9:3128"]

    async def test_update_nonexistent_returns_error(self, repo):
        result = await _run(UPDATE_PROVIDER, repo, variables={
            "input": {"name": "ghost", "ipList": ["1.1.1.1:3128"]}
        })
        assert result.errors is not None

    async def test_update_immutable_provider_returns_error(self, repo):
        await repo.add_provider(ProxyProvider(name="locked", ip_list=["1.1.1.1:3128"], mutable=False))
        result = await _run(UPDATE_PROVIDER, repo, variables={
            "input": {"name": "locked", "ipList": ["9.9.9.9:3128"]}
        })
        assert result.errors is not None


class TestMutationRemoveProvider:
    async def test_remove_returns_true(self, repo):
        await repo.add_provider(ProxyProvider(name="p", ip_list=["1.1.1.1:3128"]))
        result = await _run(REMOVE_PROVIDER, repo, variables={"name": "p"})
        assert result.errors is None
        assert result.data["removeProvider"] is True


class TestMutationAddIpToProvider:
    async def test_appends_ip(self, repo):
        await repo.add_provider(ProxyProvider(name="p", ip_list=["1.1.1.1:3128"]))
        result = await _run(ADD_IP_TO_PROVIDER, repo, variables={
            "provider": "p", "address": "2.2.2.2:3128"
        })
        assert result.errors is None
        assert "2.2.2.2:3128" in result.data["addIpToProvider"]["ipList"]

    async def test_duplicate_returns_error(self, repo):
        await repo.add_provider(ProxyProvider(name="p", ip_list=["1.1.1.1:3128"]))
        result = await _run(ADD_IP_TO_PROVIDER, repo, variables={
            "provider": "p", "address": "1.1.1.1:3128"
        })
        assert result.errors is not None

    async def test_unknown_provider_returns_error(self, repo):
        result = await _run(ADD_IP_TO_PROVIDER, repo, variables={
            "provider": "ghost", "address": "1.1.1.1:3128"
        })
        assert result.errors is not None


class TestMutationRemoveIpFromProvider:
    async def test_removes_ip(self, repo):
        await repo.add_provider(ProxyProvider(name="p", ip_list=["1.1.1.1:3128", "2.2.2.2:3128"]))
        result = await _run(REMOVE_IP_FROM_PROVIDER, repo, variables={
            "provider": "p", "address": "1.1.1.1:3128"
        })
        assert result.errors is None
        assert "1.1.1.1:3128" not in result.data["removeIpFromProvider"]["ipList"]

    async def test_last_ip_returns_error(self, repo):
        await repo.add_provider(ProxyProvider(name="p", ip_list=["1.1.1.1:3128"]))
        result = await _run(REMOVE_IP_FROM_PROVIDER, repo, variables={
            "provider": "p", "address": "1.1.1.1:3128"
        })
        assert result.errors is not None


# ---------------------------------------------------------------------------
# Permission enforcement
# ---------------------------------------------------------------------------

class TestPermissions:
    async def test_viewer_can_query(self, repo):
        user = AuthenticatedUser(sub="v", role="viewer", is_api_key=False)
        ctx = Context(repo=repo, user=user, auth_config=AuthConfig(enabled=True))
        result = await schema.execute("{ targets { name } }", context_value=ctx)
        assert result.errors is None

    async def test_viewer_cannot_mutate(self, repo):
        user = AuthenticatedUser(sub="v", role="viewer", is_api_key=False)
        ctx = Context(repo=repo, user=user, auth_config=AuthConfig(enabled=True))
        result = await schema.execute(
            'mutation { addPool(input: {name:"p", ipRequests:[{provider:"x", count:1}]}) { name } }',
            context_value=ctx,
        )
        assert result.errors is not None
        assert any("denied" in str(e).lower() or "permission" in str(e).lower() for e in result.errors)

    async def test_operator_can_mutate(self, repo):
        user = AuthenticatedUser(sub="op", role="operator", is_api_key=False)
        ctx = Context(repo=repo, user=user, auth_config=AuthConfig(enabled=True))
        result = await schema.execute(
            'mutation { addPool(input: {name:"p", ipRequests:[{provider:"x", count:1}]}) { name } }',
            context_value=ctx,
        )
        assert result.errors is None

    async def test_auth_disabled_skips_checks(self, repo):
        user = AuthenticatedUser(sub="v", role="viewer", is_api_key=False)
        ctx = Context(repo=repo, user=user, auth_config=AuthConfig(enabled=False))
        result = await schema.execute(
            'mutation { addPool(input: {name:"p", ipRequests:[{provider:"x", count:1}]}) { name } }',
            context_value=ctx,
        )
        assert result.errors is None


# ---------------------------------------------------------------------------
# Read-only mode (server.adminReadOnly) — the "no database, YAML-only"
# deployment kind. A deployment-level lockout, independent of and checked
# before role-based auth (see TestPermissions above).
# ---------------------------------------------------------------------------

class TestReadOnlyMode:
    async def test_blocks_mutation(self, repo):
        result = await _run(
            'mutation { addPool(input: {name:"p", ipRequests:[{provider:"x", count:1}]}) { name } }',
            repo, read_only=True,
        )
        assert result.errors is not None
        assert any("read-only" in str(e).lower() for e in result.errors)

    async def test_does_not_persist_the_rejected_mutation(self, repo):
        await _run(
            'mutation { addPool(input: {name:"p", ipRequests:[{provider:"x", count:1}]}) { name } }',
            repo, read_only=True,
        )
        assert await repo.get_pool("p") is None

    async def test_allows_queries(self, repo):
        await repo.add_pool(_make_pool("existing"))
        result = await _run("{ pools { name } }", repo, read_only=True)
        assert result.errors is None
        assert result.data["pools"] == [{"name": "existing"}]

    async def test_allows_status_query_and_reports_read_only(self, repo):
        result = await _run("{ status { readOnly } }", repo, read_only=True)
        assert result.errors is None
        assert result.data["status"]["readOnly"] is True

    async def test_blocks_even_with_admin_role_and_auth_enabled(self, repo):
        """Read-only is a deployment-level lockout, not a role — an admin
        with auth enabled is blocked exactly the same as anyone else."""
        user = AuthenticatedUser(sub="admin", role="admin", is_api_key=False)
        ctx = Context(
            repo=repo, user=user, auth_config=AuthConfig(enabled=True), read_only=True,
        )
        result = await schema.execute(
            'mutation { addPool(input: {name:"p", ipRequests:[{provider:"x", count:1}]}) { name } }',
            context_value=ctx,
        )
        assert result.errors is not None
        assert any("read-only" in str(e).lower() for e in result.errors)

    async def test_default_is_not_read_only(self, repo):
        """Confirms read_only=False (the default) leaves today's behavior
        unchanged — mutations succeed exactly as before this feature."""
        result = await _run(
            'mutation { addPool(input: {name:"p", ipRequests:[{provider:"x", count:1}]}) { name } }',
            repo,
        )
        assert result.errors is None

