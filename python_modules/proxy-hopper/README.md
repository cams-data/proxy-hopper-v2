# proxy-hopper

Rotating HTTP/HTTPS proxy server. Route outbound traffic through a pool of external proxy IP addresses — with retries, failure tracking, and automatic quarantine of broken IPs. Supports three client integration modes: standard HTTP proxy, HTTPS CONNECT tunnel, and URL-forwarding (full retry support for HTTPS).

## Installation

```bash
pip install proxy-hopper
```

Requires Python 3.11+.

## Usage

### 1. Write a config file

Configuration lives in a single YAML file covering targets, IP pools, and optional server defaults.

```yaml
# config.yaml

# ---------------------------------------------------------------------------
# IP Pools (optional)
# ---------------------------------------------------------------------------
# Define a named IP list once and reference it from multiple targets.
# Saves repeating the same IPs when several targets share a fleet.

ipPools:
  - name: shared-pool
    ipList:
      - "10.0.0.1:3128"
      - "10.0.0.2:3128"
      - "10.0.0.3:3128"

# ---------------------------------------------------------------------------
# Targets (required)
# ---------------------------------------------------------------------------
# Evaluated top-to-bottom — the first regex match handles the request.

targets:
  - name: google-apis
    regex: '.*\.googleapis\.com.*'
    ipPool: shared-pool           # reference a named pool …
    minRequestInterval: 5s        # one request per IP per 5s — respects rate limits
    maxQueueWait: 30s
    numRetries: 3
    ipFailuresUntilQuarantine: 5
    quarantineTime: 10m

  - name: fallback
    regex: '.*'
    ipList:                       # … or provide IPs inline
      - "10.1.0.1:3128"
      - "10.1.0.2:3128"
    minRequestInterval: 1s
    maxQueueWait: 30s
    numRetries: 2
    ipFailuresUntilQuarantine: 3
    quarantineTime: 5m

# ---------------------------------------------------------------------------
# Server settings (optional — all have defaults)
# ---------------------------------------------------------------------------
# Overridden by PROXY_HOPPER_* env vars, which are in turn overridden by
# CLI flags.  Only set here what you want baked into this config file.

server:
  host: 0.0.0.0
  port: 8080
  logLevel: INFO      # TRACE / DEBUG / INFO / WARNING / ERROR
  logFormat: text     # text or json
  backend: memory     # memory (default) or redis
```

### 2. Start the server

```bash
proxy-hopper run --config config.yaml
```

### 3. Configure your HTTP client

Proxy Hopper supports three integration modes. All three use the same IP rotation and retry logic.

#### HTTP proxy / CONNECT tunnel (standard)

Configure your client to use `http://localhost:8080` as its proxy. Works with any HTTP library that supports proxy settings.

```python
# requests
import requests
session = requests.Session()
session.proxies = {"http": "http://localhost:8080", "https": "http://localhost:8080"}
resp = session.get("https://example.com")

# aiohttp
import aiohttp
async with aiohttp.ClientSession() as session:
    async with session.get("https://example.com", proxy="http://localhost:8080") as resp:
        print(resp.status)
```

```bash
curl --proxy http://localhost:8080 https://example.com
```

#### URL-forwarding mode (recommended for HTTPS APIs)

Set the `X-Proxy-Hopper-Target` header to the target scheme and host, then send requests to proxy-hopper as if it were the target server. No proxy settings needed — path, query string, method, and body pass through unchanged.

```python
# requests — set a session-level header, then use normal URLs
import requests
session = requests.Session()
session.headers["X-Proxy-Hopper-Target"] = "https://api.example.com"

resp = session.get("http://localhost:8080/v1/endpoint", params={"q": "search"})
# → forwards to https://api.example.com/v1/endpoint?q=search

# aiohttp
import aiohttp
async with aiohttp.ClientSession(
    headers={"X-Proxy-Hopper-Target": "https://api.example.com"}
) as session:
    async with session.get("http://localhost:8080/v1/endpoint") as resp:
        print(resp.status)
```

```bash
curl -H "X-Proxy-Hopper-Target: https://api.example.com" \
     http://localhost:8080/v1/endpoint
```

The header value may include a base path (`https://api.example.com/v2`) which is prepended to the request path. `urljoin` and all standard URL-building tools work normally — the header is never affected by URL manipulation.

> **Why forwarding mode?** HTTPS CONNECT tunnels are opaque byte relays — Proxy Hopper cannot intercept or retry a mid-flight failure. In forwarding mode, Proxy Hopper owns the full HTTPS request, enabling retries across different IPs on 429 or 5xx responses from the target API.

### 4. Validate a config file

```bash
proxy-hopper validate --config config.yaml
# Config OK — 2 target(s) defined.
#   'google-apis': 3 IP(s), regex='.*\\.googleapis\\.com.*'
#   'fallback': 2 IP(s), regex='.*'
# Server defaults: host=0.0.0.0, port=8080, backend=memory
```

---

## Config reference

### Config priority

Settings are resolved in this order (highest wins):

| Priority | Source |
|---|---|
| 1 | CLI flags (`--port`, `--log-level`, etc.) |
| 2 | `server:` block in the YAML config file |
| 3 | `PROXY_HOPPER_*` environment variables |
| 4 | Built-in defaults |

### Target fields

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | required | Label used in logs and metrics |
| `regex` | string | required | Python regex matched against the full request URL |
| `ipList` | list | required* | Proxy addresses — `host:port` or bare host (uses `defaultProxyPort`) |
| `ipPool` | string | required* | Name of a shared `ipPools` entry — alternative to `ipList` |
| `proxyUsername` | string | — | Username for HTTP Basic auth sent to the external proxy |
| `proxyPassword` | string | — | Password for HTTP Basic auth sent to the external proxy |
| `defaultProxyPort` | int | `8080` | Port applied to bare IPs in `ipList` |
| `minRequestInterval` | duration | `1s` | **Primary rate-limit knob.** How long an IP is held off the pool after any request before it can be reused. |
| `maxQueueWait` | duration | `30s` | How long a request waits for a free IP before failing |
| `numRetries` | int | `3` | Retry attempts using a different IP on failure |
| `ipFailuresUntilQuarantine` | int | `5` | Consecutive failures before an IP is quarantined |
| `quarantineTime` | duration | `120s` | How long a quarantined IP sits out before returning to the pool |

\* Exactly one of `ipList` or `ipPool` must be provided per target.

`proxyUsername` / `proxyPassword` are needed when the external proxy requires HTTP Basic auth on CONNECT or forwarded requests (common with Squid-based providers that use both IP whitelisting and credential auth as fallback). Leave unset for open or IP-only proxies.

Duration values accept a suffix (`1s`, `5m`, `2h`) or a bare number (seconds).

### IP pools

```yaml
ipPools:
  - name: pool-name
    ipList:
      - "host:port"
      - "host"           # port from defaultProxyPort
```

Multiple targets can reference the same pool. Each target still maintains its own independent rotation state — sharing a pool definition does not mean IPs are shared at runtime.

### Server fields

All server fields can also be set as `PROXY_HOPPER_*` env vars (e.g. `PROXY_HOPPER_PORT=9000`) or overridden with CLI flags.

| Field (YAML) | Env var | Default | Description |
|---|---|---|---|
| `host` | `PROXY_HOPPER_HOST` | `0.0.0.0` | Bind address |
| `port` | `PROXY_HOPPER_PORT` | `8080` | Proxy server port |
| `logLevel` | `PROXY_HOPPER_LOG_LEVEL` | `INFO` | `TRACE` \| `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` |
| `logFormat` | `PROXY_HOPPER_LOG_FORMAT` | `text` | `text` \| `json` |
| `logFile` | `PROXY_HOPPER_LOG_FILE` | stderr | Path to log file |
| `backend` | `PROXY_HOPPER_BACKEND` | `memory` | `memory` \| `redis` |
| `redisUrl` | `PROXY_HOPPER_REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL |
| `metrics` | `PROXY_HOPPER_METRICS` | `false` | Enable Prometheus `/metrics` |
| `metricsPort` | `PROXY_HOPPER_METRICS_PORT` | `9090` | Metrics server port |
| `probe` | `PROXY_HOPPER_PROBE` | `true` | Enable background IP health prober |
| `probeInterval` | `PROXY_HOPPER_PROBE_INTERVAL` | `60` | Seconds between probe rounds |
| `probeTimeout` | `PROXY_HOPPER_PROBE_TIMEOUT` | `10` | Per-probe HTTP timeout (seconds) |
| `probeUrls` | `PROXY_HOPPER_PROBE_URLS` | Cloudflare + Google | Endpoints to probe through each IP. Comma-separated as env var. |
| `modes` | `PROXY_HOPPER_MODES` | all | Enabled interaction modes. Comma-separated as env var. Valid values: `connect_tunnel`, `http_proxy`, `forwarding`. |

### CLI flags

CLI flags cover the most operationally useful overrides. All others are set via YAML or env vars.

```
proxy-hopper run --config config.yaml [OPTIONS]

  --config / -c PATH       Path to YAML config file  [required]
  --host TEXT              Bind address
  --port INT               Proxy server port
  --log-level CHOICE       TRACE|DEBUG|INFO|WARNING|ERROR
  --log-format CHOICE      text|json
  --log-file PATH          Write logs to file instead of stderr
  --metrics / --no-metrics Enable Prometheus /metrics
  --metrics-port INT       Metrics server port
  --backend CHOICE         memory|redis
  --redis-url TEXT         Redis connection URL
  --probe / --no-probe     Enable background IP health prober
  --probe-interval FLOAT   Seconds between probe rounds
  --probe-timeout FLOAT    Per-probe HTTP timeout
  --probe-urls TEXT        Comma-separated probe endpoints
```

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  ProxyServer  (raw asyncio TCP)                              │
│                                                              │
│  _dispatch → RequestHandler (ABC)                           │
│    ├── ConnectTunnelHandler   CONNECT method → blind relay   │
│    ├── ForwardingHandler      /https/host/path → full retry  │
│    └── HttpProxyHandler       http://... → full retry        │
└──────────────────────────────┬───────────────────────────────┘
                               │ submit(PendingRequest)
          ┌────────────────────▼────────────────────┐
          │    TargetManager  (one per target)       │
          │    asyncio queue + dispatcher            │
          │    aiohttp outbound requests + retries   │
          └────────────────────┬────────────────────┘
                               │ acquire / record_success / record_failure
          ┌────────────────────▼────────────────────┐
          │    IPPool  (one per target)              │
          │    quarantine policy, cooldown sweeps    │
          └────────────────────┬────────────────────┘
                               │ push / pop / counters
          ┌────────────────────▼────────────────────┐
          │    IPPoolBackend                         │
          │    Memory | Redis                        │
          └─────────────────────────────────────────┘
```

**Key design points:**

- **`RequestHandler` ABC** — each interaction mode is a self-contained class. Adding a new mode (GraphQL gateway, gRPC, etc.) requires only a new subclass registered in `handlers.py` — `ProxyServer` needs no changes.
- **`ConnectTunnelHandler`** is the only mode that cannot retry mid-flight failures, because the client has already committed its TLS state once the tunnel is established. Use forwarding mode for full retry support over HTTPS.
- **`IPPoolBackend`** — pure storage interface. Two implementations: in-memory and Redis.
- **`IPPool`** — all quarantine and cooldown policy. Never touches the backend directly.
- **`TargetManager`** — dispatches requests, runs aiohttp forwarding, handles retries. Never touches the backend directly.

---

## Backends

### In-memory (default)

Single-process, no external dependencies. IP state lives in asyncio queues and dicts. Lost on restart.

```bash
proxy-hopper run --config config.yaml
```

### Redis (HA / multi-instance)

Requires `proxy-hopper-redis`:

```bash
pip install proxy-hopper-redis
proxy-hopper run --config config.yaml --backend redis --redis-url redis://redis:6379/0
```

Multiple Proxy Hopper instances share pool state via Redis. Each IP is delivered to exactly one instance (BLPOP atomicity). Quarantine expiry uses ZRANGEBYSCORE + ZREM to prevent double-release across instances.

---

## Prometheus metrics

Enable the metrics server:

```bash
proxy-hopper run --config config.yaml --metrics --metrics-port 9090
# or via YAML:  server: { metrics: true, metricsPort: 9090 }
```

| Metric | Type | Labels | Description |
|---|---|---|---|
| `proxy_hopper_requests_total` | Counter | `target`, `outcome` | Total proxied requests |
| `proxy_hopper_request_duration_seconds` | Histogram | `target` | Outbound request latency |
| `proxy_hopper_queue_depth` | Gauge | `target` | Requests waiting for an IP |
| `proxy_hopper_available_ips` | Gauge | `target` | IPs currently in pool |
| `proxy_hopper_quarantined_ips` | Gauge | `target` | IPs currently quarantined |
| `proxy_hopper_probe_success_total` | Counter | `address` | Successful background probes |
| `proxy_hopper_probe_failure_total` | Counter | `address`, `reason` | Failed background probes |
| `proxy_hopper_probe_duration_seconds` | Histogram | `address` | Background probe latency |
| `proxy_hopper_ip_reachable` | Gauge | `address` | `1` if IP passed last probe, `0` if not |

`outcome` values: `success`, `rate_limited`, `server_error`, `connection_error`, `no_match`.

Probe `reason` values: `timeout`, `proxy_unreachable`, `connection_error`, `http_error`.

---

## Logging

Log levels in increasing verbosity: `ERROR`, `WARNING`, `INFO` (default), `DEBUG`, `TRACE`.

| Level | What you see |
|---|---|
| `ERROR` | Backend connection failures, unrecoverable errors |
| `WARNING` | IPs quarantined, connection errors, requests dropped |
| `INFO` | Server start/stop, IP released from quarantine |
| `DEBUG` | Request dispatch (method, URL, IP), retry decisions, pool seeding |
| `TRACE` | Every queue push/pop, every Redis command, every connection open/close |

Use `--log-format json` (or `server: { logFormat: json }`) in Docker/Kubernetes for structured output compatible with Fluentd, Datadog, GCP Cloud Logging, etc.

---

## Development

```bash
# Install with dev dependencies
cd python_modules/proxy-hopper
uv sync --all-extras

# Run tests
uv run pytest

# Run cross-backend contract tests
cd ../tests && uv run pytest
```
