"""Tests for the GraphQL API — schema, queries, mutations, auth enforcement."""

from __future__ import annotations

import pytest
import pytest_asyncio

from proxy_hopper.auth import AuthenticatedUser
from proxy_hopper.backend.memory import MemoryBackend
from proxy_hopper.config import AuthConfig, ProxyProvider, ResolvedIP
from proxy_hopper.graphql import schema
from proxy_hopper.graphql.context import Context
from proxy_hopper.repository import ProxyRepository, _build_target


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
    return ProxyRepository(backend)


def _ctx(repo: ProxyRepository, role: str = "admin", auth_enabled: bool = False) -> Context:
    """Build a Context with a user of the given role."""
    user = AuthenticatedUser(sub=role, role=role, is_api_key=False)
    auth_config = AuthConfig(enabled=auth_enabled)
    return Context(repo=repo, user=user, auth_config=auth_config)


async def _run(query: str, repo: ProxyRepository, role: str = "admin", variables: dict | None = None):
    result = await schema.execute(
        query,
        context_value=_ctx(repo, role=role),
        variable_values=variables,
    )
    return result


# ---------------------------------------------------------------------------
# Query — targets
# ---------------------------------------------------------------------------

class TestQueryTargets:
    async def test_empty_returns_empty_list(self, repo):
        result = await _run("{ targets { name } }", repo)
        assert result.errors is None
        assert result.data["targets"] == []

    async def test_returns_seeded_target(self, repo):
        cfg = _build_target("api", r".*api.*", ["1.1.1.1:3128"])
        await repo.add_target(cfg)

        result = await _run("{ targets { name regex mutable } }", repo)
        assert result.errors is None
        targets = result.data["targets"]
        assert len(targets) == 1
        assert targets[0]["name"] == "api"
        assert targets[0]["mutable"] is True

    async def test_target_by_name_found(self, repo):
        await repo.add_target(_build_target("api", r".*", ["1.1.1.1:3128"]))
        result = await _run('{ target(name: "api") { name } }', repo)
        assert result.errors is None
        assert result.data["target"]["name"] == "api"

    async def test_target_by_name_not_found_returns_null(self, repo):
        result = await _run('{ target(name: "ghost") { name } }', repo)
        assert result.errors is None
        assert result.data["target"] is None

    async def test_resolved_ips_exposed(self, repo):
        cfg = _build_target("t", r".*", ["10.0.0.1:3128", "10.0.0.2:3128"])
        await repo.add_target(cfg)
        result = await _run("{ targets { resolvedIps { host port } } }", repo)
        assert result.errors is None
        ips = result.data["targets"][0]["resolvedIps"]
        hosts = {ip["host"] for ip in ips}
        assert hosts == {"10.0.0.1", "10.0.0.2"}


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
# Mutation — targets
# ---------------------------------------------------------------------------

ADD_TARGET = """
mutation($input: TargetInput!) {
  addTarget(input: $input) { name regex mutable resolvedIps { host port } }
}
"""

UPDATE_TARGET = """
mutation($input: TargetInput!) {
  updateTarget(input: $input) { name minRequestInterval }
}
"""

REMOVE_TARGET = """
mutation($name: String!) { removeTarget(name: $name) }
"""


class TestMutationAddTarget:
    async def test_add_persists_and_returns_target(self, repo):
        result = await _run(ADD_TARGET, repo, variables={
            "input": {"name": "t", "regex": ".*", "ipList": ["1.1.1.1:3128"]}
        })
        assert result.errors is None
        t = result.data["addTarget"]
        assert t["name"] == "t"
        assert t["mutable"] is True
        assert t["resolvedIps"][0]["host"] == "1.1.1.1"

    async def test_add_duplicate_returns_error(self, repo):
        await repo.add_target(_build_target("dup", r".*", ["1.1.1.1:3128"]))
        result = await _run(ADD_TARGET, repo, variables={
            "input": {"name": "dup", "regex": ".*", "ipList": ["2.2.2.2:3128"]}
        })
        assert result.errors is not None

    async def test_add_respects_custom_pool_settings(self, repo):
        result = await _run(ADD_TARGET, repo, variables={
            "input": {
                "name": "t", "regex": ".*", "ipList": ["1.1.1.1:3128"],
                "minRequestInterval": 5.0, "numRetries": 1,
            }
        })
        assert result.errors is None
        cfg = await repo.get_target("t")
        assert cfg.min_request_interval == 5.0
        assert cfg.num_retries == 1


class TestMutationUpdateTarget:
    async def test_update_changes_stored_value(self, repo):
        await repo.add_target(_build_target("t", r".*", ["1.1.1.1:3128"], min_request_interval=1.0))
        result = await _run(UPDATE_TARGET, repo, variables={
            "input": {"name": "t", "regex": ".*", "ipList": ["1.1.1.1:3128"], "minRequestInterval": 9.0}
        })
        assert result.errors is None
        assert result.data["updateTarget"]["minRequestInterval"] == 9.0

    async def test_update_nonexistent_returns_error(self, repo):
        result = await _run(UPDATE_TARGET, repo, variables={
            "input": {"name": "ghost", "regex": ".*", "ipList": ["1.1.1.1:3128"]}
        })
        assert result.errors is not None

    async def test_update_immutable_target_returns_error(self, repo):
        await repo.add_target(_build_target("frozen", r".*", ["1.1.1.1:3128"], mutable=False))
        result = await _run(UPDATE_TARGET, repo, variables={
            "input": {"name": "frozen", "regex": ".*", "ipList": ["9.9.9.9:3128"]}
        })
        assert result.errors is not None


class TestMutationRemoveTarget:
    async def test_remove_returns_true(self, repo):
        await repo.add_target(_build_target("t", r".*", ["1.1.1.1:3128"]))
        result = await _run(REMOVE_TARGET, repo, variables={"name": "t"})
        assert result.errors is None
        assert result.data["removeTarget"] is True

    async def test_remove_absent_target_is_noop(self, repo):
        result = await _run(REMOVE_TARGET, repo, variables={"name": "ghost"})
        assert result.errors is None
        assert result.data["removeTarget"] is True


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
        p = ProxyProvider(name="prov", ip_list=["1.1.1.1:3128"])
        await repo.add_provider(p)
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


# ---------------------------------------------------------------------------
# Mutation — IP helpers
# ---------------------------------------------------------------------------

ADD_IP = "mutation { addIp(target: $t, address: $a) { resolvedIps { host } } }"

class TestMutationIpHelpers:
    async def test_add_ip(self, repo):
        await repo.add_target(_build_target("t", r".*", ["1.1.1.1:3128"]))
        result = await _run(
            'mutation { addIp(target: "t", address: "2.2.2.2:3128") { resolvedIps { host } } }',
            repo,
        )
        assert result.errors is None
        hosts = {ip["host"] for ip in result.data["addIp"]["resolvedIps"]}
        assert "2.2.2.2" in hosts

    async def test_remove_ip(self, repo):
        await repo.add_target(_build_target("t", r".*", ["1.1.1.1:3128", "2.2.2.2:3128"]))
        result = await _run(
            'mutation { removeIp(target: "t", address: "1.1.1.1:3128") { resolvedIps { host } } }',
            repo,
        )
        assert result.errors is None
        hosts = [ip["host"] for ip in result.data["removeIp"]["resolvedIps"]]
        assert "1.1.1.1" not in hosts

    async def test_swap_ip(self, repo):
        await repo.add_target(_build_target("t", r".*", ["1.1.1.1:3128"]))
        result = await _run(
            'mutation { swapIp(target: "t", oldAddress: "1.1.1.1:3128", newAddress: "9.9.9.9:3128") { resolvedIps { host } } }',
            repo,
        )
        assert result.errors is None
        hosts = [ip["host"] for ip in result.data["swapIp"]["resolvedIps"]]
        assert "9.9.9.9" in hosts
        assert "1.1.1.1" not in hosts

    async def test_remove_last_ip_returns_error(self, repo):
        await repo.add_target(_build_target("t", r".*", ["1.1.1.1:3128"]))
        result = await _run(
            'mutation { removeIp(target: "t", address: "1.1.1.1:3128") { resolvedIps { host } } }',
            repo,
        )
        assert result.errors is not None


# ---------------------------------------------------------------------------
# Permission enforcement
# ---------------------------------------------------------------------------

class TestPermissions:
    async def test_viewer_can_query(self, repo):
        """Viewer role (read-only) can run queries when auth is enabled."""
        user = AuthenticatedUser(sub="v", role="viewer", is_api_key=False)
        ctx = Context(repo=repo, user=user, auth_config=AuthConfig(enabled=True))
        result = await schema.execute("{ targets { name } }", context_value=ctx)
        assert result.errors is None

    async def test_viewer_cannot_mutate(self, repo):
        """Viewer role must be denied write mutations."""
        user = AuthenticatedUser(sub="v", role="viewer", is_api_key=False)
        ctx = Context(repo=repo, user=user, auth_config=AuthConfig(enabled=True))
        result = await schema.execute(
            'mutation { addTarget(input: {name:"t", regex:".*", ipList:["1.1.1.1:3128"]}) { name } }',
            context_value=ctx,
        )
        assert result.errors is not None
        assert any("denied" in str(e).lower() or "permission" in str(e).lower() for e in result.errors)

    async def test_operator_can_mutate(self, repo):
        """Operator role has write permission."""
        user = AuthenticatedUser(sub="op", role="operator", is_api_key=False)
        ctx = Context(repo=repo, user=user, auth_config=AuthConfig(enabled=True))
        result = await schema.execute(
            'mutation { addTarget(input: {name:"t", regex:".*", ipList:["1.1.1.1:3128"]}) { name } }',
            context_value=ctx,
        )
        assert result.errors is None

    async def test_auth_disabled_skips_checks(self, repo):
        """When auth is disabled, even a restricted role can mutate."""
        user = AuthenticatedUser(sub="v", role="viewer", is_api_key=False)
        ctx = Context(repo=repo, user=user, auth_config=AuthConfig(enabled=False))
        result = await schema.execute(
            'mutation { addTarget(input: {name:"t", regex:".*", ipList:["1.1.1.1:3128"]}) { name } }',
            context_value=ctx,
        )
        assert result.errors is None
