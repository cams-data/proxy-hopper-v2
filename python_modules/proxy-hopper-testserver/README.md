# proxy-hopper-testserver

Controllable upstream server and mock proxy layer for proxy-hopper integration tests.

This package provides two in-process test doubles that let you drive proxy-hopper into specific states — quarantine, retry exhaustion, auth failures, slow networks — without needing real external proxies or services.

## Components

### `UpstreamServer`

A lightweight [aiohttp](https://docs.aiohttp.org/) HTTP server whose response behaviour is switched at runtime. Simulates the third-party API that proxy-hopper is routing traffic to.

```python
async with UpstreamServer() as server:
    url = server.url   # "http://127.0.0.1:PORT"

    server.set_mode("normal")                   # 200 JSON response
    server.set_mode("http_error", status=429)   # configurable HTTP error
    server.set_mode("http_error", status=503)
    server.set_mode("hang")                     # accept + never respond (triggers sock_read timeout)
    server.set_mode("close")                    # accept + immediately close
    server.set_mode("slow", delay=2.0)          # respond after N seconds

    print(server.request_count)   # total requests received across all modes
    server.reset()                # back to normal mode, counters zeroed
```

All modes are applied globally. Every request gets the same treatment until the mode is changed.

### `MockProxy`

A TCP server that simulates a single external proxy IP address. proxy-hopper is pointed at these addresses instead of real proxies.

```python
async with MockProxy() as proxy:
    address = proxy.address   # "127.0.0.1:PORT" — use in target ipList

    proxy.set_mode("forward")                    # relay requests to upstream (default)
    proxy.set_mode("refuse")                     # close the connection immediately
    proxy.set_mode("hang")                       # accept but never respond
    proxy.set_mode("close")                      # accept, read one line, then close
    proxy.set_mode("error_response", status=502) # return a fixed HTTP error from the proxy

    proxy.set_latency(0.5)   # inject 500ms before forwarding (FORWARD mode only)
    proxy.reset()             # back to forward mode, zero latency, zero counters

    print(proxy.connections_accepted)
    print(proxy.connections_refused)
    print(proxy.requests_forwarded)
```

In `FORWARD` mode, `MockProxy` reads the incoming proxy request, extracts the target URL, and relays it to the upstream using aiohttp — giving a complete end-to-end request path without needing a real network proxy.

### `MockProxyPool`

Manages a set of `MockProxy` instances as a named IP pool. Convenience wrapper for multi-IP tests.

```python
async with MockProxyPool(count=3) as pool:
    ip_list = pool.ip_list      # ["127.0.0.1:P1", "127.0.0.1:P2", "127.0.0.1:P3"]
    pool[0].set_mode("refuse")  # first proxy fails
    pool[1].set_mode("hang")    # second proxy hangs
    pool[2].set_mode("forward") # third proxy works

    pool.set_all_mode("refuse")          # put all proxies into the same mode
    pool.set_all_latency(0.2)            # inject latency on all proxies
    pool.reset_all()                     # reset all proxies to defaults
```

## Installation

This package is an internal test dependency — not published to PyPI. Install it alongside proxy-hopper in your test environment:

```bash
cd python_modules/proxy-hopper-testserver
uv sync
```

## Usage in tests

### Basic setup

```python
from proxy_hopper.backend.memory import MemoryIPPoolBackend
from proxy_hopper.config import TargetConfig, ResolvedIP
from proxy_hopper.target_manager import TargetManager
from proxy_hopper_testserver import MockProxyPool, UpstreamServer

@pytest_asyncio.fixture
async def upstream():
    async with UpstreamServer() as server:
        yield server
        server.reset()

@pytest_asyncio.fixture
async def proxies():
    async with MockProxyPool(count=3) as pool:
        yield pool
        pool.reset_all()
```

### Testing quarantine

```python
async def test_503s_quarantine_ip_at_threshold(backend, proxies, upstream):
    upstream.set_mode("http_error", status=503)
    threshold = 3

    cfg = TargetConfig(
        name="test",
        regex=r".*",
        resolved_ips=[ResolvedIP(host=h, port=p) for h, p in ...],
        ip_failures_until_quarantine=threshold,
        ...
    )
    mgr = TargetManager(cfg, backend)
    await mgr.start()

    for _ in range(threshold):
        await submit_and_wait(mgr, upstream.url + "/test")
        await asyncio.sleep(0.05)

    quarantined = await backend.quarantine_list(cfg.name)
    assert proxies[0].address in quarantined

    await mgr.stop()
```

### Testing authentication (end-to-end)

Auth is enforced in `ForwardingHandler`, which sits above `TargetManager`. To test it you need a full `ProxyServer`:

```python
from proxy_hopper.auth import create_access_token
from proxy_hopper.config import ApiKeyConfig, AuthConfig
from proxy_hopper.server import ProxyServer

async def _start_proxy(proxies, auth_config=None, runtime_secret=""):
    cfg = make_target_config(ip_list=proxies.ip_list)
    backend = MemoryIPPoolBackend()
    mgr = TargetManager(cfg, backend)
    server = ProxyServer(
        [mgr], host="127.0.0.1", port=0,
        auth_config=auth_config, runtime_secret=runtime_secret,
    )
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    return server, port

async def test_valid_api_key_returns_200(proxies, upstream):
    auth = AuthConfig(
        enabled=True,
        jwt_secret="my-32-byte-secret-xxxxxxxxxxxx",
        api_keys=[ApiKeyConfig(name="ci", key="ph_mykey", targets=["*"])],
    )
    server, port = await _start_proxy(proxies, auth_config=auth)
    try:
        async with aiohttp.ClientSession() as client:
            async with client.get(
                f"http://127.0.0.1:{port}/test",
                headers={
                    "X-Proxy-Hopper-Target": upstream.url,
                    "X-Proxy-Hopper-Auth": "Bearer ph_mykey",
                },
            ) as resp:
                assert resp.status == 200
    finally:
        await server.stop()
```

See [tests/test_auth_integration.py](tests/test_auth_integration.py) for the complete auth test matrix.

## Proxy protocol

`MockProxy` speaks the forwarding protocol that proxy-hopper uses internally: plain HTTP requests in absolute-form.

```
GET http://127.0.0.1:UPSTREAM_PORT/path HTTP/1.1
Host: 127.0.0.1:UPSTREAM_PORT
...
```

CONNECT tunnelling (HTTPS) is deliberately not implemented — integration tests use plain HTTP to keep the mock simple. This matches the behaviour of `ForwardingHandler`, which owns the TLS session itself and always makes a plain HTTP request to the external proxy.

## Test files

### `tests/test_integration.py`

End-to-end tests for the `TargetManager` layer. Each test runs twice — once against `MemoryIPPoolBackend` and once against `RedisIPPoolBackend` (using fakeredis).

| Class | What it tests |
|---|---|
| `TestUpstreamErrors` | 429/503 increment failure counter; success resets it; 500 is passed through without counting |
| `TestProxyLayerFailures` | refused/hung/closed proxy connections increment failure count |
| `TestRetryBehaviour` | retry uses a different IP; retry succeeds when first IP fails |
| `TestQuarantineLifecycle` | quarantined IP is released after `quarantine_time` expires |
| `TestLatency` | slow proxy/upstream does not increment failure count |
| `TestGracefulShutdown` | queued requests receive 503 when manager is stopped |

### `tests/test_auth_integration.py`

End-to-end tests for the `ForwardingHandler` auth check. Runs at `ProxyServer` level, using `aiohttp` in forwarding mode as the HTTP client.

| Class | What it tests |
|---|---|
| `TestAuthDisabled` | no auth config / `enabled=False` / disabled + garbage token all pass through |
| `TestMissingOrBadToken` | missing header, wrong key, garbage bearer, empty bearer → 401 |
| `TestApiKeyAuth` | valid key; wildcard and named targets; wrong target → 403; second of two keys; JWT-shaped key that is not registered → 401 |
| `TestJwtAuth` | valid JWT; viewer role; expired JWT → 401; wrong secret → 401; unknown role → 403 |
| `TestJwtTargetAccess` | custom role permitted/denied; wildcard role; empty targets; built-in admin allows any target |

## Running the tests

```bash
cd python_modules/proxy-hopper-testserver
uv run pytest
```

Both backends are covered without any external services. To run against a real Redis instance:

```bash
REDIS_URL=redis://localhost:6379/1 uv run pytest
```

Tests marked `@pytest.mark.real_redis` are skipped without `REDIS_URL` set.
