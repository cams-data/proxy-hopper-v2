# proxy-hopper

Rotating HTTPS proxy server. Route outbound traffic through a pool of external proxy IP addresses — with retries, failure tracking, and automatic quarantine of broken IPs.

Clients integrate via the **forwarding mode**: set `X-Proxy-Hopper-Target` to the real destination and send requests to proxy-hopper as if it were the target server. Proxy-hopper owns the full HTTPS request, enabling retries across different IPs on 429 / 5xx responses — something a CONNECT tunnel cannot do.

## Installation

```bash
# Docker (recommended)
docker pull ghcr.io/cams-data/proxy-hopper:latest

# From source
pip install proxy-hopper
```

Requires Python 3.11+.

## Usage

### 1. Write a config file

Configuration lives in a single YAML file covering proxy providers, IP pools, targets, and optional server defaults.

```yaml
# config.yaml

# ---------------------------------------------------------------------------
# Proxy Providers (optional)
# ---------------------------------------------------------------------------
# Named proxy suppliers — each with its own credentials and region tag.
# Providers are referenced from ipPools via ipRequests.

proxyProviders:
  - name: provider-au
    auth:
      type: basic
      username: user
      password: secret
    ipList:
      - "10.0.0.1:3128"
      - "10.0.0.2:3128"
    regionTag: Australia

  - name: provider-ca
    auth:
      type: basic
      username: user
      password: secret
    ipList:
      - "10.1.0.1:3128"
      - "10.1.0.2:3128"
    regionTag: Canada

# ---------------------------------------------------------------------------
# IP Pools (optional)
# ---------------------------------------------------------------------------
# Named pools referenced by targets.  Draw IPs from providers via ipRequests
# (randomly samples `count` IPs) or list them inline.

ipPools:
  - name: shared-pool
    ipRequests:
      - provider: provider-au
        count: 3
      - provider: provider-ca
        count: 3

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
    ipList:                       # … or provide IPs inline (no provider metadata)
      - "10.2.0.1:3128"
      - "10.2.0.2:3128"
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
# Docker
docker run -v $(pwd)/config.yaml:/config.yaml \
  ghcr.io/cams-data/proxy-hopper:latest \
  proxy-hopper run --config /config.yaml

# From source
proxy-hopper run --config config.yaml
```

### 3. Configure your HTTP client

Set `X-Proxy-Hopper-Target` to the target scheme + host, then send requests to proxy-hopper as if it were the target server. No proxy settings needed — path, query string, method, and body pass through unchanged.

```python
# requests
import requests
session = requests.Session()
session.headers["X-Proxy-Hopper-Target"] = "https://api.example.com"

resp = session.get("http://localhost:8080/v1/endpoint", params={"q": "search"})
# → forwards to https://api.example.com/v1/endpoint?q=search
```

```python
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

### 4. Per-request control headers

All `X-Proxy-Hopper-*` headers are stripped before the request reaches the upstream server.

| Header | Description |
|---|---|
| `X-Proxy-Hopper-Target: https://api.example.com` | **Required.** Target scheme + host (+ optional base path). |
| `X-Proxy-Hopper-Tag: <string>` | Optional label propagated to Prometheus metrics as the `tag` label. Use it to break down metrics by endpoint or use-case. |
| `X-Proxy-Hopper-Retries: <int>` | Override the target's `numRetries` for this request only. Must be a non-negative integer; invalid values fall back to the target default. |

**Tag example** — identify which endpoints burn through IPs fastest:

```python
session.headers["X-Proxy-Hopper-Tag"] = "search-api"
# proxy_hopper_requests_total{target="google-apis", outcome="rate_limited", tag="search-api"}
```

**Retries example** — disable retries for idempotency-sensitive calls:

```python
session.headers["X-Proxy-Hopper-Retries"] = "0"
```

### 5. Validate a config file

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

### Proxy provider fields

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | required | Unique identifier referenced from `ipPools.ipRequests` |
| `auth` | block | — | Optional — omit entirely for open or IP-whitelisted proxies |
| `auth.type` | string | `basic` | Auth type — currently `basic` |
| `auth.username` | string | required if auth set | Username for HTTP Basic auth sent to this provider's proxies |
| `auth.password` | string | `""` | Password for HTTP Basic auth |
| `ipList` | list | required | Proxy addresses from this provider — `scheme://host:port`, `host:port`, or bare host |
| `regionTag` | string | — | Region label attached to metrics (e.g. `Australia`) — enables per-region observability |

### IP pool fields

```yaml
ipPools:
  - name: pool-name
    # Draw from providers (randomly sampled):
    ipRequests:
      - provider: provider-name
        count: 5          # how many IPs to randomly select from that provider's list
    # Or list IPs inline (no provider metadata):
    ipList:
      - "host:port"
```

`ipRequests` and `ipList` can be combined in the same pool. Multiple targets can reference the same pool — each target maintains independent rotation state.

### Target fields

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | required | Label used in logs and metrics |
| `regex` | string | required | Python regex matched against the full request URL |
| `ipPool` | string | required* | Name of a shared `ipPools` entry |
| `ipList` | list | required* | Inline proxy addresses — `host:port` or bare host (uses `defaultProxyPort`) |
| `defaultProxyPort` | int | `8080` | Port applied to bare IPs in `ipList` |
| `minRequestInterval` | duration | `1s` | **Primary rate-limit knob.** How long an IP is held off the pool after any request before it can be reused. |
| `maxQueueWait` | duration | `30s` | How long a request waits for a free IP before failing |
| `numRetries` | int | `3` | Retry attempts using a different IP on failure (overridable per-request with `X-Proxy-Hopper-Retries`) |
| `ipFailuresUntilQuarantine` | int | `5` | Consecutive failures before an IP is quarantined |
| `quarantineTime` | duration | `120s` | How long a quarantined IP sits out before returning to the pool |

\* Exactly one of `ipPool` or `ipList` must be provided per target.

Credentials are defined on `proxyProviders` — targets inherit auth from whichever provider contributed each IP. Inline `ipList` targets have no credentials.

Duration values accept a suffix (`1s`, `5m`, `2h`) or a bare number (seconds).

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
│    └── ForwardingHandler  X-Proxy-Hopper-Target → retry     │
└──────────────────────────┬───────────────────────────────────┘
                           │ submit(PendingRequest)
        ┌──────────────────▼──────────────────────┐
        │    TargetManager  (one per target)       │
        │    asyncio queue + dispatcher            │
        │    aiohttp outbound requests + retries   │
        └──────────────────┬──────────────────────┘
                           │ acquire / record_success / record_failure
        ┌──────────────────▼──────────────────────┐
        │    IPPool  (one per target)              │
        │    quarantine policy, cooldown sweeps    │
        └──────────────────┬──────────────────────┘
                           │ push / pop / counters
        ┌──────────────────▼──────────────────────┐
        │    IPPoolBackend                         │
        │    Memory | Redis                        │
        └─────────────────────────────────────────┘
```

**Key design points:**

- **`RequestHandler` ABC** — the forwarding mode is a `RequestHandler` subclass. Adding a new mode (auth injection, path rewriting, custom protocols) requires only a new subclass registered in `handlers.py` — `ProxyServer` needs no changes.
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

Requires `proxy-hopper-redis`, installable as an extra:

```bash
pip install "proxy-hopper[redis]"
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
| `proxy_hopper_requests_total` | Counter | `target`, `outcome`, `tag` | Total proxied requests |
| `proxy_hopper_request_duration_seconds` | Histogram | `target` | Outbound request latency |
| `proxy_hopper_responses_total` | Counter | `target`, `status_code`, `tag` | Upstream HTTP responses by status code |
| `proxy_hopper_retries_total` | Counter | `target` | Retry attempts |
| `proxy_hopper_retry_exhaustions_total` | Counter | `target` | Requests that exhausted all retries |
| `proxy_hopper_queue_depth` | Gauge | `target` | Requests waiting for an IP |
| `proxy_hopper_queue_wait_seconds` | Histogram | `target` | Time spent waiting in queue |
| `proxy_hopper_queue_expired_total` | Counter | `target` | Requests dropped due to queue timeout |
| `proxy_hopper_active_connections` | Gauge | — | Open client connections |
| `proxy_hopper_available_ips` | Gauge | `target` | IPs currently in pool |
| `proxy_hopper_quarantined_ips` | Gauge | `target` | IPs currently quarantined |
| `proxy_hopper_ip_quarantine_events_total` | Counter | `target`, `address`, `provider`, `region` | Quarantine events per IP |
| `proxy_hopper_ip_failure_count` | Gauge | `target`, `address`, `provider`, `region` | Consecutive failure count per IP |
| `proxy_hopper_probe_success_total` | Counter | `address`, `provider`, `region` | Successful background probes |
| `proxy_hopper_probe_failure_total` | Counter | `address`, `provider`, `region`, `reason` | Failed background probes |
| `proxy_hopper_probe_duration_seconds` | Histogram | `address`, `provider`, `region` | Background probe latency |
| `proxy_hopper_ip_reachable` | Gauge | `address`, `provider`, `region` | `1` if IP passed last probe, `0` if not |

`outcome` values: `success`, `rate_limited`, `server_error`, `connection_error`, `no_match`.

The `tag` label is set from the `X-Proxy-Hopper-Tag` request header (empty string if not provided). Use it to break down metrics by API endpoint:

```promql
# Rate-limited requests by endpoint
sum by (tag) (rate(proxy_hopper_requests_total{outcome="rate_limited"}[5m]))
```

The `provider` and `region` labels on IP-level and probe metrics come from `proxyProviders` — enabling per-provider and per-region queries such as `avg by (region) (proxy_hopper_probe_duration_seconds)`.

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
