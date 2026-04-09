# proxy-hopper-tests

Cross-backend contract tests for proxy-hopper. Every test in this package runs automatically against **all registered backend implementations** — currently the in-memory backend and the Redis backend (via fakeredis).

This package contains no library code. It exists solely to house tests that verify the `IPPoolBackend` interface contract and the `IPPool` business-logic layer independent of any specific storage implementation.

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

Tests the raw `IPPoolBackend` storage interface. Verifies that each backend correctly implements:

- **`init_target`** — first caller returns `True`, subsequent callers return `False`; independent targets don't interfere
- **IP pool queue** — FIFO ordering, `push_ip` / `pop_ip`, timeout behaviour, `pool_size`
- **Failure counter** — `increment_failures` returns the new count, `reset_failures` zeroes it, independent per IP, concurrent increments are consistent
- **Quarantine** — `quarantine_add` / `quarantine_list` / `quarantine_pop_expired`; expired entries are returned and removed; future entries are not returned; concurrent `pop_expired` calls cannot double-claim the same entry

### `test_pool_contract.py`

Tests the `IPPool` business-logic layer, which sits above the backend. Uses the same parametrized backend fixture so every pool behaviour is verified on each backend. Verifies:

- **`acquire`** — returns an address string, drains the pool in order, returns `None` on timeout
- **`record_success`** — resets failure count, returns IP to pool after `min_request_interval` cooldown
- **`record_failure`** — increments failure count; below threshold returns IP to pool; at threshold quarantines IP and keeps it out of pool
- **`_sweep_quarantine`** — releases expired entries back to pool with failures reset; leaves unexpired entries alone; safe on empty quarantine
- **`get_status`** — reports correct available IP count and quarantined IP list

## Adding a new backend

Register it in `conftest.py`:

```python
from my_package import MyNewBackend

def _make_my_backend() -> MyNewBackend:
    return MyNewBackend(...)   # any test-safe configuration

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

The Redis contract tests need to verify Redis-specific atomicity behaviour (BLPOP, SETNX, ZRANGEBYSCORE+ZREM) without requiring a running Redis server in CI or local development. fakeredis provides a fully in-process Redis implementation that is compatible with the `redis-py` async client.

**Backend fixture injection**

The `_make_redis_backend()` factory injects a fakeredis client before `start()` is called. The `RedisIPPoolBackend.start()` method only creates a real connection if `self._redis is None`, so the injected fake takes precedence:

```python
def _make_redis_backend() -> RedisIPPoolBackend:
    fake_server = fakeredis.FakeServer()
    backend = RedisIPPoolBackend()
    backend._redis = fakeredis.FakeRedis(server=fake_server, decode_responses=True)
    return backend
```
