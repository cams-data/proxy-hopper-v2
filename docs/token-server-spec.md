# Proxy Hopper Token Server — Feature Specification

## 1. Problem Statement

Some targets require per-request bearer tokens (or other session credentials) that must be fetched
before each proxied request is made. A naive approach — generating tokens on the client side —
defeats the purpose of IP rotation: all requests share the same token, making it trivial to
correlate them as originating from a single logical client.

The correct approach is to tie each token to the upstream proxy IP that will use it. A token
fetched through IP `1.2.3.4` is only ever used by requests routed through `1.2.3.4`. This requires
Proxy Hopper itself to manage the token lifecycle: acquisition, caching, refresh, and failure
recovery.

Because token acquisition is target-specific (every target has a different auth flow), Proxy Hopper
cannot implement the logic itself. Instead it defines a protocol and delegates to a lightweight
user-provided **token server** that handles the target-specific token logic. Proxy Hopper manages
all the plumbing: state, locking, refresh scheduling, and failure quarantine.

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│  Proxy Hopper                                                   │
│                                                                 │
│  ┌──────────────┐   token needed?   ┌──────────────────────┐   │
│  │  Request     │──────────────────▶│  Token Manager       │   │
│  │  Handler     │◀──────────────────│  (per target+ip)     │   │
│  └──────────────┘  inject headers   └──────┬───────────────┘   │
│                                            │         │         │
│                                 POST /token│         │read/    │
│                                            │         │write    │
└────────────────────────────────────────────┼─────────┼─────────┘
                                             │         │
                                             ▼         ▼
              ┌───────────────────────┐   ┌──────────────────────┐
              │  User Token Server    │   │  Redis               │
              │  (proxy-hopper-       │   │  · token cache       │
              │   token-server)       │   │  · cursor store      │
              │                       │   │  · refresh locks     │
              │  class MyProvider(    │   │  · broken-ip set     │
              │    TokenProvider):    │   └──────────────────────┘
              │                       │
              │    async def          │
              │      get_token(req):  │
              │        ...            │
              └───────────────────────┘
```

### Key design principles

- **Token server is stateless.** All per-IP, per-target state (including the cursor) is stored by
  Proxy Hopper in Redis. The token server receives the current cursor on every call and returns an
  updated one. It never needs its own database.
- **Tokens are scoped to (target, ip).** A token fetched via `1.2.3.4` is only injected into
  requests routed through that IP.
- **Distributed-safe.** Redis locks prevent multiple Proxy Hopper replicas from simultaneously
  refreshing the same token.
- **Failure isolation.** If the token server fails for a given IP, that IP enters a broken state
  and is excluded from routing until auth recovers. The proxy runner never uses an expired or
  absent token.
- **Transport-agnostic.** The token server response returns a `headers` dict. Proxy Hopper injects
  it verbatim — works for `Authorization: Bearer`, `Cookie:`, or any custom header scheme.

---

## 3. New Package: `proxy-hopper-token-server`

A lightweight Python library that users install in their own project. It provides:

1. The `TokenRequest` / `TokenResponse` data classes that match Proxy Hopper's protocol.
2. The `TokenProvider` abstract base class with a single method to override.
3. The `AuthServer` runner that wires the provider to an HTTP server.
4. A `ProxyHopperClient` helper that lets the token server route its own requests through a
   specific upstream proxy IP via the new IP-pinning header (see §5).

### 3.1 Data classes

```python
@dataclass
class Profile:
    """Full identity profile for the request, as Proxy Hopper would send it."""
    user_agent: str
    accept: str
    accept_language: str
    accept_encoding: str
    # Any additional fingerprint fields Proxy Hopper has on record for this IP.
    # Passed as-is so the token server can construct realistic browser-like requests.
    extra: dict[str, str] = field(default_factory=dict)


@dataclass
class TokenRequest:
    target: str          # target name as configured in Proxy Hopper
    ip: str              # upstream proxy host
    port: int            # upstream proxy port
    cursor: dict         # opaque state blob; {} on first call for this ip+target
    profile: Profile     # full identity fingerprint for this IP
    proxy_url: str | None  # ProxyHopper proxy endpoint, for routing token-fetch
                           # requests through the same IP (see §3.4). None if
                           # IP-pinning is disabled in config.


@dataclass
class TokenResponse:
    headers: dict[str, str]  # headers Proxy Hopper will inject into every request
                              # routed through this IP to this target.
                              # e.g. {"Authorization": "Bearer abc123"}
    expires_at: datetime      # UTC. Proxy Hopper will refresh before this time.
    cursor: dict              # updated opaque state; stored and returned on next call.
                              # Return the same cursor unchanged if nothing needs updating.
```

### 3.2 `TokenProvider` base class

```python
class TokenProvider(ABC):
    @abstractmethod
    async def get_token(self, request: TokenRequest) -> TokenResponse:
        """
        Fetch or refresh an auth token for the given IP+target combination.

        Called by Proxy Hopper:
          - On startup, for every auth-managed target/IP combination.
          - Whenever the token age reaches `(expires_at - refresh_threshold)`.

        The implementation is responsible for the actual token acquisition logic.
        It may make HTTP requests directly, or use `request.proxy_url` combined
        with `ProxyHopperClient` to route those requests through the same proxy IP.

        Raising any exception will mark the IP as auth-broken (see §6).
        """
        ...
```

### 3.3 `TokenServer` runner

```python
class TokenServer:
    def __init__(
        self,
        provider: TokenProvider | dict[str, TokenProvider],
        host: str = "0.0.0.0",
        port: int = 9000,
        timeout: float = 30.0,   # seconds; Proxy Hopper must match or be lower
    ):
        """
        provider: a single TokenProvider used for all targets, or a dict
                  mapping target names to specific providers.
        """
        ...

    def run(self) -> None:
        """Start the server synchronously (blocks). Suitable for __main__ scripts."""
        ...

    async def start(self) -> None:
        """Start the server as a coroutine. For embedding in an existing event loop."""
        ...
```

**Multi-target registration:**

```python
server = TokenServer(
    provider={
        "auckland-council": AucklandCouncilTokens(),
        "linz-api": LinzTokens(),
    },
    port=9000,
)
server.run()
```

If a single `TokenProvider` instance is given, it handles all targets and must use
`request.target` to branch its logic.

### 3.4 `ProxyHopperClient` — IP-pinned HTTP helper

Token acquisition for some targets must itself originate from the same IP that will use the token.
For example, if the target's auth endpoint checks the requesting IP against a session, tokens
fetched from the real server IP would be invalidated when used from the proxy IP.

This helper lets the token server route its own requests through a specific upstream proxy IP by
going through Proxy Hopper and using the new IP-pinning header (see §5):

```python
class ProxyHopperClient:
    """
    Thin aiohttp wrapper that routes requests through Proxy Hopper,
    pinning to a specific upstream proxy IP via X-ProxyHopper-Force-IP.
    """

    def __init__(self, proxy_url: str):
        """
        proxy_url: Proxy Hopper's proxy listener, e.g. "http://proxy-hopper:8085"
        """
        ...

    async def request(
        self,
        method: str,
        url: str,
        via_ip: str,           # "host:port" of the upstream proxy IP to pin to
        headers: dict | None = None,
        data: bytes | None = None,
        timeout: float = 10.0,
        **kwargs,
    ) -> aiohttp.ClientResponse:
        """
        Sends `method url` through Proxy Hopper, which will route it exclusively
        through the upstream proxy at `via_ip`.

        X-ProxyHopper-Force-IP is injected automatically and stripped by Proxy
        Hopper before the request reaches the upstream target.
        """
        ...

    async def get(self, url: str, via_ip: str, **kwargs) -> aiohttp.ClientResponse: ...
    async def post(self, url: str, via_ip: str, **kwargs) -> aiohttp.ClientResponse: ...
```

**Usage in a provider:**

```python
class AucklandCouncilTokens(TokenProvider):
    async def get_token(self, req: TokenRequest) -> TokenResponse:
        client = ProxyHopperClient(proxy_url=req.proxy_url)
        via = f"{req.ip}:{req.port}"

        # This request goes: token server → PH → upstream proxy at req.ip → target
        resp = await client.post(
            "https://www.aucklandcouncil.govt.nz/en/.../find-property.html",
            via_ip=via,
            headers={
                "User-Agent": req.profile.user_agent,
                "accept": "text/x-component",
                "next-action": "0085094deec5539b4c1957dddebcf440d92db43f11",
            },
            data=b"[]",
        )
        token = extract_token(await resp.read())

        return TokenResponse(
            headers={"Authorization": f"Bearer {token}"},
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            cursor={},
        )
```

---

## 4. Changes to `proxy-hopper` Core

### 4.1 Config changes

**`server` block** — new fields:

```yaml
server:
  # ... existing fields ...
  authServer:
    url: "http://localhost:9000"   # base URL of the user token server
    timeoutSeconds: 15             # hard timeout on each token server call
    refreshThresholdSeconds: 120   # refresh token this many seconds before expiry
    retryIntervalSeconds: 60       # how long to wait before retrying a broken IP
    maxRetries: 10                 # after this many consecutive failures, quarantine
    exposeProxyUrl: true           # whether to include proxy_url in TokenRequest
                                   # (enables IP-pinned token fetching via §3.4)
                                   # default: true
```

**`targets` block** — new field per target:

```yaml
targets:
  - name: auckland-council
    regex: "aucklandcouncil.govt.nz"
    poolName: pool-1
    authManaged: true        # ← new flag; delegates token management to token server
    # ... other fields unchanged ...
```

No per-target token server URL. One token server handles all auth-managed targets. The target name
is passed in `TokenRequest.target` so the token server can branch on it.

### 4.2 Config model changes

```python
@dataclass
class AuthServerConfig:
    url: str
    timeout_seconds: float = 15.0
    refresh_threshold_seconds: float = 120.0
    retry_interval_seconds: float = 60.0
    max_retries: int = 10
    expose_proxy_url: bool = True


@dataclass
class ServerConfig:
    # ... existing fields ...
    auth_server: AuthServerConfig | None = None


@dataclass
class TargetConfig:
    # ... existing fields ...
    auth_managed: bool = False
```

### 4.3 `TokenManager` — new internal component

Responsible for the full lifecycle of tokens for auth-managed targets. One instance per Proxy Hopper
process; it manages all (target, ip) pairs.

**Responsibilities:**

- Maintain an in-memory cache (backed by Redis) of current tokens per (target, ip).
- On startup: kick off background pre-warm tasks for all auth-managed targets × all IPs in their
  pools.
- Before each proxied request: return the current valid headers for the (target, ip) pair, or
  block briefly while a refresh is in progress.
- Schedule background refresh: track expiry times and trigger refresh at
  `expires_at - refresh_threshold`.
- Lock coordination: use Redis locks to ensure only one replica refreshes a given (target, ip) pair
  at a time.
- Failure handling: on token server error, mark the IP as `auth_broken` for that target;
  schedule periodic retry.

### 4.4 Redis key layout

All keys are namespaced under `ph:auth:` to avoid collisions with existing Proxy Hopper keys.

| Key | Type | Value | TTL |
|-----|------|-------|-----|
| `ph:auth:token:{target}:{ip}:{port}` | Hash | `headers` (JSON), `expires_at` (ISO8601), `cursor` (JSON) | `expires_at + 10m` (safety margin) |
| `ph:auth:lock:{target}:{ip}:{port}` | String | `replica-id` | 30s (auto-expire prevents deadlock) |
| `ph:auth:broken:{target}:{ip}:{port}` | String | failure count | none (cleared on recovery) |
| `ph:auth:retry_at:{target}:{ip}:{port}` | String | ISO8601 timestamp | none |

### 4.5 IP state machine (auth perspective)

```
         startup / pool add
               │
               ▼
          ┌─────────┐
          │ PENDING │  No token yet; excluded from routing.
          └────┬────┘
               │ token server returns token
               ▼
          ┌─────────┐
          │  VALID  │◀──────────────────────────────┐
          └────┬────┘                               │
               │ within refresh_threshold            │ token server returns token
               ▼                                    │
        ┌────────────┐                              │
        │ REFRESHING │  Still routing using the     │
        └────┬───────┘  old token while refresh     │
             │          is in progress.             │
             │ error                                │
             ▼                                      │
       ┌──────────────┐                             │
       │  AUTH_BROKEN │  Excluded from routing.     │
       └──────┬───────┘  retry_interval timer.      │
              │                                     │
              │ timer fires; retry attempt ─────────┘
              │
              │ max_retries exceeded
              ▼
        ┌────────────┐
        │ QUARANTINED│  Existing quarantine logic takes over.
        └────────────┘
```

States `PENDING`, `AUTH_BROKEN`, and `QUARANTINED` all prevent the IP from being selected by the
pool routing logic.

---

## 5. IP-Pinning Header — `X-ProxyHopper-Force-IP`

A new request header that instructs Proxy Hopper to route a request through a specific upstream
proxy IP rather than selecting one via normal pool logic.

**Header name:** `X-ProxyHopper-Force-IP`  
**Value:** `host:port` — must exactly match an IP already registered in the target's pool.

```
X-ProxyHopper-Force-IP: 1.2.3.4:8080
```

**Behaviour:**

- Proxy Hopper strips this header from the outgoing request before it reaches the upstream proxy
  (it is a control-plane header, never forwarded).
- If the specified IP is not in the target's pool, or is in `AUTH_BROKEN` / `QUARANTINED` state,
  Proxy Hopper returns `502 Bad Gateway` with an explanatory message.
- If IP-pinning is used on a non-auth-managed target, it works as a routing hint only — no token
  injection occurs.

**Security:** IP-pinning should be restricted to requests authenticated by the proxy's own auth
layer (API key or JWT). Unauthenticated clients must not be able to pin IPs. This is configurable:

```yaml
server:
  authServer:
    allowUnauthenticatedIpPin: false   # default: false
```

---

## 6. Token Refresh Flow — Detailed

### 6.1 Normal path (token valid)

```
Request arrives for target T, IP selected: I
  │
  ├─ Redis get ph:auth:token:T:I
  │    hit, expires_at > now + refresh_threshold
  │
  └─ Inject headers into outgoing request. Done.
```

### 6.2 Refresh path (token near expiry or absent)

```
Request arrives for target T, IP selected: I
  │
  ├─ Redis get ph:auth:token:T:I
  │    miss, OR expires_at <= now + refresh_threshold
  │
  ├─ Redis SET NX ph:auth:lock:T:I  (TTL=30s)
  │
  ├─ Lock acquired?
  │    YES ──▶ Call token server POST /token
  │            {target, ip, port, cursor, profile, proxy_url}
  │            │
  │            ├─ Success ──▶ Write ph:auth:token:T:I to Redis
  │            │              Release lock
  │            │              Inject headers. Done.
  │            │
  │            └─ Error / timeout
  │                       ──▶ Increment ph:auth:broken:T:I
  │                           Set ph:auth:retry_at:T:I = now + retry_interval
  │                           Release lock
  │                           If broken count >= max_retries: quarantine IP
  │                           Return 502 to caller
  │
  └─ Lock NOT acquired (another replica is refreshing)
       │
       ├─ Poll Redis for ph:auth:token:T:I (up to 5s, 200ms intervals)
       │
       ├─ Token appeared ──▶ Inject headers. Done.
       │
       └─ Timeout waiting ──▶ Return 502 to caller
                              (do not use an expired token)
```

### 6.3 Background refresh scheduler

The `TokenManager` runs a background async task that:

1. Every `refresh_threshold / 2` seconds, scans all tracked (target, ip) pairs.
2. For any where `expires_at - now <= refresh_threshold`, triggers a proactive refresh
   (same flow as §6.2, without blocking an in-flight request).
3. For any in `AUTH_BROKEN` state where `now >= retry_at`, triggers a recovery attempt.
4. On startup: immediately attempts to pre-warm tokens for all auth-managed (target, ip) pairs
   as a background task (non-blocking — the proxy starts accepting requests immediately).

### 6.4 Lock TTL and deadlock prevention

The Redis lock has a hard TTL of 30 seconds. This ensures that if a Proxy Hopper replica crashes
mid-refresh, the lock is released automatically. The TTL should be well above the token server
`timeoutSeconds` (e.g., `lock_ttl = timeout_seconds * 1.5`).

---

## 7. Auth Server Protocol

### Endpoint

```
POST /token
Content-Type: application/json
```

### Request body

```json
{
  "target": "auckland-council",
  "ip": "203.0.113.42",
  "port": 8080,
  "cursor": {},
  "profile": {
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ...",
    "accept": "text/html,application/xhtml+xml,...",
    "accept_language": "en-US,en;q=0.9",
    "accept_encoding": "gzip, deflate, br",
    "extra": {
      "sec-ch-ua": "\"Chromium\";v=\"124\"",
      "sec-ch-ua-platform": "\"Windows\""
    }
  },
  "proxy_url": "http://proxy-hopper:8085"
}
```

`cursor` is `{}` on the first call for a given (target, ip) pair. On subsequent calls it is
whatever the token server returned in the previous response. Proxy Hopper treats it as an opaque
blob — it stores and forwards it without inspection.

`proxy_url` is omitted (null) if `exposeProxyUrl: false` in server config.

### Success response — 200

```json
{
  "headers": {
    "Authorization": "Bearer eyJhbGciOiJSUzI1NiJ9..."
  },
  "expires_at": "2024-11-01T14:30:00Z",
  "cursor": {
    "session_id": "sess_abc123",
    "nonce": 42
  }
}
```

`headers` is injected verbatim into every proxied request for this (target, ip) pair until the
token is next refreshed.

`cursor` replaces whatever was stored. Return the incoming cursor unchanged if no state update
is needed.

### Error response — any non-200

```json
{
  "error": "upstream_auth_failed",
  "detail": "Token endpoint returned 429 Too Many Requests"
}
```

Any non-200 response (or a network error / timeout) is treated as a failure and triggers the
broken-state logic (§4.5).

### Health check endpoint

```
GET /health → 200 {"ok": true}
```

Proxy Hopper may probe this on startup to verify the token server is reachable before accepting
requests for auth-managed targets.

---

## 8. CLI — `ph-token-server`

The package registers a `ph-token-server` command. Users point it at a Python import path and it
handles everything else — no `if __name__ == "__main__"` boilerplate required.

### Usage

```
ph-token-server start <import_path> [OPTIONS]
```

`import_path` is a `module:attribute` string resolving to either:

- A `TokenServer` instance — used as-is.
- A `TokenProvider` instance or class — wrapped in a `TokenServer` automatically.

**Examples:**

```bash
# Provider instance — handles all targets
ph-token-server start myapp.tokens:provider

# Provider class — instantiated with no args
ph-token-server start myapp.tokens:MyProvider

# Pre-built TokenServer instance (gives full control over host/port/timeout)
ph-token-server start myapp.tokens:server

# Override host/port at the command line
ph-token-server start myapp.tokens:MyProvider --host 127.0.0.1 --port 9001

# Multiple workers via uvicorn
ph-token-server start myapp.tokens:MyProvider --workers 4
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `0.0.0.0` | Interface to bind. |
| `--port` | `9000` | Port to listen on. |
| `--workers` | `1` | Number of uvicorn worker processes. |
| `--log-level` | `info` | `trace` / `debug` / `info` / `warning` / `error`. |
| `--timeout` | `30.0` | Hard timeout (seconds) for each `get_token` call. Proxy Hopper config must be ≤ this value. |

### Resolution rules for `import_path`

```
myapp.tokens:MyProvider
    │              │
    │              └─ attribute name within the module
    └─ dotted module path (must be importable from the current working directory)
```

1. Import the module.
2. Resolve the attribute.
3. If it is a `TokenServer` instance → use directly (CLI flags `--host`/`--port`/`--workers`
   override the instance's defaults).
4. If it is a `TokenProvider` instance → wrap: `TokenServer(provider=instance)`.
5. If it is a `TokenProvider` subclass → instantiate with no args, then wrap.
6. Anything else → exit with a descriptive error.

### Typical project layout

```
my-token-server/
├── pyproject.toml              # depends on proxy-hopper-token-server
└── myapp/
    └── tokens.py
```

```python
# myapp/tokens.py
from proxy_hopper_token_server import TokenProvider, TokenRequest, TokenResponse

class AucklandCouncilTokens(TokenProvider):
    async def get_token(self, req: TokenRequest) -> TokenResponse:
        ...

# Can also expose a pre-configured server instance:
# from proxy_hopper_token_server import TokenServer
# server = TokenServer(provider=AucklandCouncilTokens(), port=9001)
```

```bash
ph-token-server start myapp.tokens:AucklandCouncilTokens
# → Serving on http://0.0.0.0:9000
```

---

## 9. Package Structure — `proxy-hopper-token-server`

```
python_modules/
└── proxy-hopper-token-server/
    ├── pyproject.toml
    └── src/
        └── proxy_hopper_token_server/
            ├── __init__.py          # public API re-exports
            ├── models.py            # TokenRequest, TokenResponse, Profile
            ├── provider.py          # TokenProvider ABC
            ├── server.py            # TokenServer runner (FastAPI + uvicorn)
            ├── client.py            # ProxyHopperClient (aiohttp wrapper)
            ├── cli.py               # ph-token-server Click CLI
            └── _internal/
                ├── app.py           # FastAPI app factory
                ├── handler.py       # /token route implementation
                └── loader.py        # import_path resolution logic
```

**`pyproject.toml`:**

```toml
[project]
name = "proxy-hopper-token-server"
dependencies = [
    "click>=8.0",
    "fastapi>=0.111",
    "uvicorn>=0.30",
    "aiohttp>=3.9",
    "pydantic>=2.0",
]

[project.scripts]
ph-token-server = "proxy_hopper_token_server.cli:main"
```

No dependency on `proxy-hopper` itself — the library is a standalone server toolkit. The protocol
is defined by the JSON schema above, not by shared Python types.

---

## 9. Cursor Design Guidelines

The cursor is an arbitrary JSON-serialisable dict. Token server implementors should store in it
whatever is needed to avoid starting from scratch on each token refresh. Examples:

| Use case | Cursor fields |
|----------|---------------|
| Session cookie that must be re-used | `{"session_cookie": "abc123"}` |
| PKCE state / code verifier | `{"code_verifier": "...", "state": "..."}` |
| Rate-limit counter per IP | `{"request_count": 42, "window_start": "2024-..."}` |
| Stateless (token fetched fresh each time) | `{}` |

**Constraints:**

- Must be JSON-serialisable (no custom Python objects).
- Proxy Hopper imposes a size limit of **64 KB** per cursor (Redis storage).
- Sensitive data (passwords, raw tokens) should not be stored in the cursor — use the `headers`
  field for the token itself. The cursor is stored in Redis and may appear in logs.

---

## 10. Observability

### Logging

Proxy Hopper will emit structured log events for all auth lifecycle events:

| Event | Level | Fields |
|-------|-------|--------|
| Token pre-warm started | INFO | target, ip, port |
| Token acquired | DEBUG | target, ip, expires_at |
| Token refresh triggered | DEBUG | target, ip, reason (scheduled/forced) |
| Token server timeout | WARNING | target, ip, timeout_seconds |
| Token server error | WARNING | target, ip, status_code, error |
| IP marked auth_broken | ERROR | target, ip, failure_count |
| IP quarantined (auth) | ERROR | target, ip |
| Auth recovery attempt | INFO | target, ip, attempt_number |
| IP recovered from auth_broken | INFO | target, ip |

### Metrics

New Prometheus metrics exposed on the existing metrics endpoint:

```
proxy_hopper_auth_token_refreshes_total{target, ip, status="success|failure"}
proxy_hopper_auth_token_refresh_duration_seconds{target, ip}
proxy_hopper_auth_broken_ips_current{target}
proxy_hopper_auth_server_request_duration_seconds
```

---

## 11. Non-Goals (Explicit Out of Scope)

- **Token server HA / load balancing.** The token server is user-managed. Proxy Hopper connects to
  a single URL. Users who need HA for their token server can put it behind a load balancer.
- **Multiple token servers** (one per target). A single token server URL is configured globally.
  The target name in `TokenRequest` is sufficient for the token server to branch on.
- **Token sharing across targets.** A token is always scoped to one (target, ip) pair. Even if
  two targets share the same upstream proxy pool, their tokens are managed independently.
- **Non-Redis backends.** The token cache and lock mechanism require Redis. This feature is only
  available when `proxy-hopper-redis` is installed and a Redis backend is configured.
- **Modifying the token mid-flight** (e.g., request signing). The `headers` dict is injected
  once at request start. Per-request dynamic signing is out of scope for this feature.

---

## 12. Open Questions

| # | Question | Notes |
|---|----------|-------|
| 1 | Should `X-ProxyHopper-Force-IP` require auth? | Recommend yes; unauthenticated callers should not be able to pin IPs. Config flag proposed. |
| 2 | Should the cursor be encrypted at rest in Redis? | Low priority; cursors should not contain secrets by convention (§9). |
| 3 | Pre-warm on startup: all IPs, or on-demand? | Spec says background pre-warm for all registered (target, ip) pairs, non-blocking. Revisit if pool sizes are very large. |
| 4 | Should `TokenResponse.expires_at` be optional? | If absent, Proxy Hopper could default to `now + refresh_threshold`. Token servers that don't know expiry could return null. |
| 5 | Emit an event/webhook when an IP is quarantined due to auth failure? | Useful for alerting. Out of scope for MVP. |

---

## 13. Implementation Phases

### Phase 1 — Protocol and library

- [ ] Define `TokenRequest`, `TokenResponse`, `Profile` models in `proxy-hopper-token-server`
- [ ] Implement `TokenProvider` ABC
- [ ] Implement `TokenServer` runner (FastAPI + uvicorn)
- [ ] Implement `ProxyHopperClient` aiohttp wrapper
- [ ] Implement `ph-token-server` CLI (`start` command, import path resolution)
- [ ] Unit tests for the library (mock provider, assert correct JSON in/out)
- [ ] Unit tests for CLI (import path resolution edge cases)
- [ ] Example implementation: `examples/token-server/auckland_council.py`

### Phase 2 — `X-ProxyHopper-Force-IP` header

- [ ] Parse and strip header in request handler
- [ ] Route to pinned IP, bypassing pool selection
- [ ] Return 502 if IP not found or in broken/quarantined state
- [ ] Auth gate for unauthenticated clients
- [ ] Tests

### Phase 3 — `TokenManager` and Redis integration

- [ ] Redis key schema implementation
- [ ] Token fetch with lock acquire / wait / piggyback logic
- [ ] Background refresh scheduler
- [ ] Startup pre-warm
- [ ] `AUTH_BROKEN` state and retry logic
- [ ] Quarantine escalation after `max_retries`
- [ ] Config parsing for `authServer` block and `authManaged` flag
- [ ] Integration tests (mock token server, real Redis via testcontainers)

### Phase 4 — Observability

- [ ] Structured log events (§10)
- [ ] Prometheus metrics (§10)
- [ ] Health check probe of token server at startup

### Phase 5 — Documentation and examples

- [ ] `docker-test` example extended with a token server container
- [ ] Full `docker-compose` example: proxy runner + admin + token server + Redis
- [ ] README section: "Auth-managed targets"
- [ ] `proxy-hopper-token-server` package README with quickstart
