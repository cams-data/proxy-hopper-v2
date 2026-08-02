# proxy-hopper-token-server

A small library + CLI for implementing the token server side of Proxy Hopper's **managed auth** feature. If a target you're proxying to requires a per-request `Authorization` header (or `Cookie`, or any other auth header) that needs to be periodically fetched and refreshed — OAuth access tokens, session cookies, rotating API keys — this package lets you write just the token-acquisition logic and get a correctly-shaped HTTP server for free.

Proxy Hopper's core calls your token server before forwarding each request to a target configured with `authManaged: true`, and injects whatever headers it returns into the upstream request. Full protocol reference: [`python_modules/proxy-hopper/README.md`](../proxy-hopper/README.md#managed-auth--token-server).

```
Proxy Hopper                         Your token server
     │                                        │
     │  POST /token  {target, ip, port,       │
     │                cursor, profile}        │
     ├───────────────────────────────────────►│  your TokenProvider.get_token()
     │                                        │  fetches/refreshes a token
     │  { headers, expires_at, cursor }        │
     │◄───────────────────────────────────────┤
     │                                        │
     │  injects `headers` into the            │
     │  upstream request, caches until        │
     │  expires_at (minus a refresh window)   │
```

## Why this exists instead of hand-rolling a FastAPI app

You *can* implement the `POST /token` + `GET /health` contract yourself in any framework — see [`examples/token-server/`](../../examples/token-server/) for a from-scratch reference. This library exists so you don't have to get the wire format, timeout handling, or error responses right by hand: implement one async method (`TokenProvider.get_token`), and `TokenServer` gives you a tested FastAPI app, a CLI runner, and (via `ProxyHopperClient`) a correct way to route the token-fetch request itself through Proxy Hopper on a pinned IP.

## Installation

```bash
pip install proxy-hopper-token-server
```

Requires Python 3.12+.

## Quick start

**1. Implement a `TokenProvider`:**

```python
# mytokens.py
from datetime import UTC, datetime, timedelta
from proxy_hopper_token_server import TokenProvider, TokenRequest, TokenResponse

class MyTokens(TokenProvider):
    async def get_token(self, req: TokenRequest) -> TokenResponse:
        # req.target — the Proxy Hopper target name this token is for
        # req.ip, req.port — the upstream proxy IP that will use this token
        # req.cursor — opaque dict you returned last call for this (target, ip); {} on first call
        # req.profile — the browser fingerprint Proxy Hopper is using for this IP
        token = await fetch_a_real_token()  # your actual auth call
        return TokenResponse(
            headers={"Authorization": f"Bearer {token}"},
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            cursor=req.cursor,  # unchanged — return updated state here if you need it
        )

provider = MyTokens()
```

**2. Run it:**

```bash
# Via the CLI — resolves module:attribute and serves it
ph-token-server start mytokens:provider --port 9000

# Or embed it in your own script
python -c "
from proxy_hopper_token_server import TokenServer
from mytokens import provider
TokenServer(provider, port=9000).run()
"
```

**3. Point Proxy Hopper at it** — in `config.yaml`:

```yaml
server:
  authServer:
    url: "http://localhost:9000"

targets:
  - name: my-api
    regex: 'api\.example\.com'
    ipPool: my-pool
    authManaged: true
```

That's the whole integration. Proxy Hopper calls `POST /token` before forwarding each request through this target, caches the response per `(target, ip)`, and refreshes it before `expires_at`.

## The wire contract

If you're implementing this in another language, or just want the raw shape: your server must expose

- **`POST /token`** — request body:

  ```json
  {
    "target": "my-api",
    "ip": "203.0.113.10",
    "port": 3128,
    "cursor": {},
    "profile": {
      "user_agent": "Mozilla/5.0 ...",
      "accept": "text/html",
      "accept_language": "en-US",
      "accept_encoding": "gzip",
      "extra": {}
    },
    "proxy_url": "http://proxy-hopper:8080"
  }
  ```

  `proxy_url` is only present when `server.authServer.exposeProxyUrl: true` is set in the Proxy Hopper config. Response body:

  ```json
  {
    "headers": {"Authorization": "Bearer eyJ..."},
    "expires_at": "2026-08-02T12:00:00+00:00",
    "cursor": {}
  }
  ```

- **`GET /health`** — must return `2xx` once ready. Proxy Hopper calls this at startup before pre-warming tokens for all configured (target, ip) pairs.

Using this library, `TokenServer`/`create_app` builds exactly this contract for you — you never touch HTTP directly, only `TokenRequest`/`TokenResponse` dataclasses.

### The cursor mechanism

`cursor` is an opaque JSON object Proxy Hopper stores per `(target, ip)` pair and echoes back on every subsequent `/token` call for that pair — a tiny key-value slot for anything you need to correlate calls (a refresh token, a session ID, a call counter) without running your own database. Return the incoming cursor unchanged if you don't need it.

```python
async def get_token(self, req: TokenRequest) -> TokenResponse:
    refresh_token = req.cursor.get("refresh_token")
    if refresh_token:
        access, new_refresh, expires = await exchange_refresh_token(refresh_token)
    else:
        access, new_refresh, expires = await fresh_login()
    return TokenResponse(
        headers={"Authorization": f"Bearer {access}"},
        expires_at=expires,
        cursor={"refresh_token": new_refresh},
    )
```

## Routing token acquisition through Proxy Hopper (`ProxyHopperClient`)

Some auth endpoints tie a session or token to the IP address that requested it. If your `get_token()` implementation needs to make its *own* outbound HTTP call (e.g. logging in) from the same proxy IP that will later use the resulting token, use `ProxyHopperClient` with `X-ProxyHopper-Force-IP` pinning:

```python
from proxy_hopper_token_server import ProxyHopperClient, TokenProvider, TokenRequest, TokenResponse

class MyTokens(TokenProvider):
    async def get_token(self, req: TokenRequest) -> TokenResponse:
        client = ProxyHopperClient(proxy_url=req.proxy_url)  # requires exposeProxyUrl: true
        resp = await client.post(
            "https://auth.example.com/login",
            via_ip=f"{req.ip}:{req.port}",
            headers={"User-Agent": req.profile.user_agent},
            data=b'{"user": "...", "pass": "..."}',
        )
        body = await resp.read()
        ...
```

`ProxyHopperClient` sends the request to Proxy Hopper's own address using the same header-based forwarding mode every other client uses (`X-Proxy-Hopper-Target` + `X-ProxyHopper-Force-IP`) — it does **not** use a classic HTTP-proxy or CONNECT-tunnel request, because Proxy Hopper's core doesn't implement those modes. `req.proxy_url` is only populated when `server.authServer.exposeProxyUrl: true` is set; without it, make the request with a plain HTTP client instead (no IP pinning).

`client.post()`/`.get()` return a `ProxyHopperResponse` (`.status`, `.headers`, `await .read()`, `.text()`) — the body is fully buffered before the underlying connection closes, so it's safe to read after the call returns and safe to read more than once.

## CLI reference

```
ph-token-server start IMPORT_PATH [OPTIONS]

  IMPORT_PATH               'module.path:AttributeName' — resolves to a
                             TokenServer instance, TokenProvider instance,
                             or TokenProvider subclass (instantiated with no args)

  --host TEXT                Interface to bind [default: 0.0.0.0]
  --port INT                 Port to listen on [default: 9000]
  --workers INT               uvicorn worker processes [default: 1]
  --log-level CHOICE         trace|debug|info|warning|error [default: info]
  --timeout FLOAT             Hard timeout (seconds) per get_token() call [default: 30.0]
```

CLI flags override whatever the resolved instance was constructed with.

```bash
ph-token-server start myapp.tokens:MyProvider
ph-token-server start myapp.tokens:provider --port 9001
ph-token-server start myapp.tokens:server --workers 4
```

## Hosting behind your own ASGI server

For local development with autoreload, or if you want to embed the token endpoints in a larger FastAPI app, use `TokenServer.build_app()` to get the plain ASGI app instead of calling `.run()`:

```python
# main.py
from proxy_hopper_token_server import TokenServer
from mytokens import provider

server = TokenServer(provider)
app = server.build_app()
```

```bash
uvicorn main:app --reload --port 9000
```

## Testing your provider

`TokenServer.build_app()` / `create_app()` return a plain FastAPI app, so test it with `fastapi.testclient.TestClient` like any other FastAPI app — no network, no real Proxy Hopper instance needed:

```python
from fastapi.testclient import TestClient
from proxy_hopper_token_server._internal.app import create_app
from mytokens import provider

def test_token_issued():
    app = create_app(provider)
    with TestClient(app) as client:
        resp = client.post("/token", json={
            "target": "my-api", "ip": "1.2.3.4", "port": 3128, "cursor": {},
            "profile": {"user_agent": "", "accept": "", "accept_language": "", "accept_encoding": ""},
        })
    assert resp.status_code == 200
    assert "Authorization" in resp.json()["headers"]
```

This package's own [`tests/`](tests/) directory is a good reference: `test_server.py` covers the HTTP layer (success, provider errors, timeouts, multi-target routing), `test_client.py` covers `ProxyHopperClient`'s request shape, `test_loader.py` covers CLI import-path resolution, and `test_cli.py` covers the CLI's flag wiring.

## Multiple providers, one server

Pass a `dict[str, TokenProvider]` instead of a single provider to route by target name:

```python
TokenServer({
    "target-a": ProviderA(),
    "target-b": ProviderB(),
})
```

A request for a target with no matching key returns `404`.

## Failure behavior

Any exception raised from `get_token()` becomes a `500` with `{"error": "provider_error", "detail": str(exc)}`; exceeding the configured `timeout` becomes a `504` with `{"error": "timeout", ...}`. Proxy Hopper treats repeated failures as `AUTH_BROKEN` for that IP (see the core README's [Broken-state and recovery](../proxy-hopper/README.md#broken-state-and-recovery) section) — the proxy IP itself is not quarantined, only the auth layer for it.

## Deployment

- [`examples/token-server/`](../../examples/token-server/) — a complete, runnable example (self-signed JWTs, Docker Compose, adapting-to-your-auth-mechanism recipes)
- [`examples/token-server/auckland_council.py`](../../examples/token-server/auckland_council.py) — a real-world `TokenProvider` using `ProxyHopperClient` for IP-pinned token acquisition and a session cookie carried in the cursor
- [Helm chart](../../charts/proxy-hopper/) — `tokenServer.enabled: true` deploys a Deployment/Service for a token-server image you provide and wires `authServer.url` automatically
- [Kubernetes example](../../examples/kubernetes/) — raw manifests, non-Helm alternative

## Development

```bash
cd python_modules/proxy-hopper-token-server
uv sync --extra dev
uv run pytest
```
