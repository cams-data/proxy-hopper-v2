# proxy-hopper

Rotating HTTP/HTTPS proxy server. Run it as a local proxy, point your HTTP clients at it, and it automatically distributes outbound traffic across a pool of external proxy IP addresses — with retries, failure tracking, and automatic quarantine of broken IPs.

## Installation

```bash
pip install proxy-hopper
```

Requires Python 3.11+.

## Usage

### 1. Write a targets config

Targets are defined in a YAML file. Each target specifies a regex that matches URLs, and a list of proxy IPs to rotate through.

```yaml
# config.yaml
targets:
  - name: google-apis
    regex: '.*\.googleapis\.com.*'
    ipList:
      - "10.0.0.1:3128"
      - "10.0.0.2:3128"
    minRequestInterval: 500ms   # minimum time between uses of the same IP
    maxQueueWait: 30s           # how long a request waits for an available IP
    numRetries: 3               # retry count on 5xx or connection errors
    ipFailuresUntilQuarantine: 5
    quarantineTime: 2m

  - name: fallback
    regex: '.*'
    ipList:
      - "10.1.0.1:3128"
      - "10.1.0.2:3128"
      - "10.1.0.3:3128"
    minRequestInterval: 1s
    maxQueueWait: 30s
    numRetries: 2
    ipFailuresUntilQuarantine: 3
    quarantineTime: 5m
```

Targets are matched in order — the first matching target handles the request.

#### Config reference

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | required | Unique label used in logs and metrics |
| `regex` | string | required | Matched against the request URL (or `host:port` for CONNECT) |
| `ipList` | list | required | Proxy IP addresses — `host:port` or bare host (uses `defaultProxyPort`) |
| `defaultProxyPort` | int | `8080` | Port applied to bare IPs in `ipList` |
| `minRequestInterval` | duration | `1s` | Minimum cooldown before an IP is re-used |
| `maxQueueWait` | duration | `30s` | Maximum time a request waits for an available IP |
| `numRetries` | int | `3` | Retry attempts on retriable errors (429, 502, 503, 504, connection failure) |
| `ipFailuresUntilQuarantine` | int | `5` | Consecutive failures before an IP is quarantined |
| `quarantineTime` | duration | `120s` | How long a quarantined IP is held before being returned to the pool |

Duration values accept `s`, `m`, `h` suffixes or bare numbers (seconds).

### 2. Start the server

```bash
proxy-hopper run --config config.yaml
```

All options are also available as environment variables with the `PROXY_HOPPER_` prefix:

```bash
# Docker / Kubernetes style
PROXY_HOPPER_CONFIG=/etc/proxy-hopper/config.yaml \
PROXY_HOPPER_HOST=0.0.0.0 \
PROXY_HOPPER_PORT=8080 \
PROXY_HOPPER_LOG_LEVEL=INFO \
PROXY_HOPPER_LOG_FORMAT=json \
proxy-hopper run
```

#### All `run` options

| Flag | Env var | Default | Description |
|---|---|---|---|
| `--config` / `-c` | `PROXY_HOPPER_CONFIG` | required | Path to targets YAML |
| `--host` | `PROXY_HOPPER_HOST` | `0.0.0.0` | Bind address |
| `--port` | `PROXY_HOPPER_PORT` | `8080` | Proxy server port |
| `--log-level` | `PROXY_HOPPER_LOG_LEVEL` | `INFO` | `TRACE` \| `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` |
| `--log-format` | `PROXY_HOPPER_LOG_FORMAT` | `text` | `text` \| `json` (structured JSON for log collectors) |
| `--log-file` | `PROXY_HOPPER_LOG_FILE` | stderr | Write logs to a file |
| `--metrics` / `--no-metrics` | `PROXY_HOPPER_METRICS` | off | Enable Prometheus `/metrics` endpoint |
| `--metrics-port` | `PROXY_HOPPER_METRICS_PORT` | `9090` | Prometheus metrics port |
| `--backend` | `PROXY_HOPPER_BACKEND` | `memory` | `memory` \| `redis` |
| `--redis-url` | `PROXY_HOPPER_REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL |

### 3. Configure your HTTP client

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

### 4. Validate a config file

```bash
proxy-hopper validate --config config.yaml
# Config OK — 2 target(s) defined.
#   'google-apis': 2 IP(s), regex='.*\\.googleapis\\.com.*'
#   'fallback': 3 IP(s), regex='.*'
```

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  ProxyServer  (raw asyncio TCP — handles CONNECT)   │
└──────────────────────┬──────────────────────────────┘
                       │ regex match → one manager per target
          ┌────────────▼────────────┐
          │    TargetManager        │  asyncio request queue + dispatcher
          │    (one per target)     │  aiohttp outbound requests + retries
          └────────────┬────────────┘
                       │ acquire / record_success / record_failure
          ┌────────────▼────────────┐
          │       IPPool            │  business logic: quarantine policy,
          │    (one per target)     │  cooldown scheduling, sweep loop
          └────────────┬────────────┘
                       │ push_ip / pop_ip / increment_failures / …
          ┌────────────▼────────────┐
          │   IPPoolBackend         │  pure storage primitives
          │   Memory | Redis        │  shared across all targets
          └─────────────────────────┘
```

**Three-layer separation:**

- **`IPPoolBackend`** — pure storage interface (queue push/pop, atomic counters, sorted sets). No business logic. Two implementations: in-memory and Redis.
- **`IPPool`** — all policy decisions: when to quarantine, how long to wait, when to return an IP. Calls the backend exclusively through primitives.
- **`TargetManager`** — dispatches requests, runs the aiohttp forwarding, handles retries. Never touches the backend directly.

## Backends

### In-memory (default)

Single-process, no external dependencies. IP state lives in asyncio queues and dicts. Lost on restart.

```bash
proxy-hopper run --config config.yaml --backend memory
```

### Redis (HA / multi-instance)

Requires `proxy-hopper-redis`:

```bash
pip install proxy-hopper-redis
proxy-hopper run --config config.yaml --backend redis --redis-url redis://redis:6379/0
```

Multiple Proxy Hopper instances share pool state via Redis. Each IP is delivered to exactly one instance (BLPOP atomicity). Quarantine expiry uses ZRANGEBYSCORE + ZREM to prevent double-release across instances.

## Prometheus metrics

Enable with `--metrics`:

```bash
proxy-hopper run --config config.yaml --metrics --metrics-port 9090
```

| Metric | Type | Labels | Description |
|---|---|---|---|
| `proxy_hopper_requests_total` | Counter | `target`, `outcome` | Total proxied requests |
| `proxy_hopper_request_duration_seconds` | Histogram | `target` | Outbound request latency |
| `proxy_hopper_queue_depth` | Gauge | `target` | Requests waiting for an IP |
| `proxy_hopper_available_ips` | Gauge | `target` | IPs currently in pool |
| `proxy_hopper_quarantined_ips` | Gauge | `target` | IPs currently quarantined |

`outcome` values: `success`, `rate_limited`, `server_error`, `connection_error`, `no_match`.

## Logging

Log levels in increasing verbosity: `ERROR`, `WARNING`, `INFO` (default), `DEBUG`, `TRACE`.

| Level | What you see |
|---|---|
| `ERROR` | Backend connection failures, unrecoverable errors |
| `WARNING` | IPs quarantined, connection errors, requests dropped |
| `INFO` | Server start/stop, IP released from quarantine |
| `DEBUG` | Request dispatch (method, URL, IP), retry decisions, pool seeding |
| `TRACE` | Every queue push/pop, every Redis command, every connection open/close |

Use `--log-format json` in Docker/Kubernetes for structured output compatible with Fluentd, Datadog, GCP Cloud Logging, etc.

## Development

```bash
# Install with dev dependencies
cd python_modules/proxy-hopper
uv sync

# Run tests
uv run pytest

# Run cross-backend contract tests
cd ../tests && uv run pytest
```
