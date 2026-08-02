# proxy-hopper-tests

Cross-backend contract tests for proxy-hopper. Every test in this package runs automatically against **all registered backend implementations** — currently the in-memory backend and the Redis backend (via fakeredis).

This package contains no library code. It exists solely to house tests that verify the `IPPoolStore` interface contract and the `IdentityQueue` business-logic layer independent of any specific storage implementation.

## Purpose

The contract test approach solves a specific problem: when you add a new backend (or modify an existing one), you want confidence that it behaves identically to every other backend at the interface level. Instead of duplicating tests per backend, every test here is written once and parametrized to run against each registered backend.

```
test_backend_contract.py::TestPoolQueue::test_push_then_pop[memory]   PASSED
test_backend_contract.py::TestPoolQueue::test_push_then_pop[redis]    PASSED
test_pool_contract.py::TestAcquire::test_returns_address_string[memory]  PASSED
test_pool_contract.py::TestAcquire::test_returns_address_string[redis]   PASSED
```

## Running

```bash
cd python_modules/tests
uv run pytest
```

That's it. Both backends are tested in a single command, with no external services required (Redis tests use [fakeredis](https://github.com/cunla/fakeredis-py)).

## Test files

### `test_backend_contract.py`

Tests the `IPPoolStore` interface (`pool_store.py`) — a thin wrapper around the raw `Backend` primitives (queue/counter/sorted-set/KV) that gives them pool-domain meaning. Verifies that each backend correctly implements:

- **`claim_init`** — first caller returns `True`, subsequent callers return `False`; independent targets don't interfere
- **Pool queue** — FIFO ordering, `push_identity_uuid` / `pop_identity_uuid`, timeout behaviour, `pool_size`
- **Failure counter, quarantine, identity KV, IP→UUID lookups, retired-address set** — see `IPPoolStore`'s own docstrings in `pool_store.py` for the full method list this suite exercises

### `test_pool_contract.py`

Tests the `IdentityQueue` business-logic layer (`pool.py`), which sits above `IPPoolStore`. Uses the same parametrized backend fixture so every pool behaviour is verified on each backend. Verifies:

- **`acquire`** — returns a UUID and identity, drains the pool in order, returns `None` on timeout
- **`record_success`** — resets failure count, returns the identity to the pool after `min_request_interval` cooldown
- **`record_failure`** — increments failure count; below threshold returns the identity to pool; at threshold quarantines the IP and keeps it out of pool
- **Quarantine sweep** — releases expired entries back to the pool with failures reset; leaves unexpired entries alone; safe when quarantine is empty
- **`get_status`** — reports correct available-IP count and quarantined-IP list

### `test_app_metrics_contract.py`

Tests `AppMetricsStore` (`app_metrics.py`) — the in-process per-target request metrics store used when Prometheus isn't configured — and the `Backend.counter_increment_by` primitive it depends on, both against every registered backend.

## Adding a new backend

Register a factory in `conftest.py` that wraps your raw `Backend` implementation in `IPPoolStore` and returns `(pool_store, is_real_redis)`:

```python
from my_package import MyNewBackend
from proxy_hopper.pool_store import IPPoolStore

def _make_my_backend() -> tuple[IPPoolStore, bool]:
    return IPPoolStore(MyNewBackend(...)), False   # any test-safe configuration

_BACKEND_FACTORIES = {
    "memory": _make_memory_backend,
    "redis":  _make_redis_backend,
    "mine":   _make_my_backend,    # ← add this
}
```

All existing contract tests will immediately run against the new backend with no further changes.

## Design notes

**Why a separate package?**

pytest collects conftest files using Python module names. If both `proxy-hopper/tests/conftest.py` and `proxy-hopper-redis/tests/conftest.py` were collected from a shared root, they would both resolve to `tests.conftest` and collide. A separate `tests/` package with its own `pyproject.toml` sidesteps this entirely — each test suite is run independently from its own directory.

**Why fakeredis?**

The Redis contract tests need to verify Redis-specific atomicity behaviour (BLPOP, SETNX, ZRANGEBYSCORE+ZREM) without requiring a running Redis server in CI or local development. fakeredis provides a fully in-process Redis implementation that is compatible with the `redis-py` async client. If `REDIS_URL` is set in the environment (e.g. a CI service container), real Redis is used instead, and tests marked `real_redis` (which exercise features fakeredis doesn't support, like Lua scripting) run too.

**Backend fixture injection**

The `_make_redis_backend()` factory injects a fakeredis client into the raw `RedisBackend` before wrapping it in `IPPoolStore` and starting it. `RedisBackend.start()` only creates a real connection if `self._redis is None`, so the injected fake takes precedence:

```python
def _make_redis_backend() -> tuple[IPPoolStore, bool]:
    raw = RedisBackend(_REDIS_URL if _REDIS_URL else "redis://localhost:6379/0")
    if not _REDIS_URL:
        fake_server = fakeredis.FakeServer()
        raw._redis = fakeredis.FakeRedis(server=fake_server, decode_responses=True)
        return IPPoolStore(raw), False
    return IPPoolStore(raw), True
```
