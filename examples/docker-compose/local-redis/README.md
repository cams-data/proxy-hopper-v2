# Docker Compose — Redis backend

Two-service Proxy Hopper deployment using Redis for shared IP pool state. Suitable for production workloads where you need pool state to survive restarts, or where you want to scale out multiple proxy replicas sharing a single IP pool.

## Files

```
local-redis/
├── Dockerfile          # Installs proxy-hopper + proxy-hopper-redis from GitHub Release
├── docker-compose.yml  # proxy-hopper + Redis services
├── config.yaml         # Full example config with ipPools (edit before starting)
└── README.md
```

## Quick start

**1. Edit `config.yaml`**

Replace the placeholder IP addresses with your real external proxy IPs. The `ipPools` section lets you define an IP list once and reference it from multiple targets:

```yaml
ipPools:
  - name: general-pool
    ipList:
      - "your-proxy-1.example.com:3128"
      - "your-proxy-2.example.com:3128"

targets:
  - name: general
    regex: '.*'
    ipPool: general-pool
    minRequestInterval: 1s   # one request per IP per second
    maxQueueWait: 30s
    numRetries: 3
    ipFailuresUntilQuarantine: 5
    quarantineTime: 2m
```

**2. Build and start**

```bash
cd examples/docker-compose/local-redis
docker compose up --build
```

By default this installs `v0.1.0`. To use a different release:

```bash
PROXY_HOPPER_VERSION=0.2.0 docker compose up --build
```

Proxy Hopper will wait for Redis to pass its healthcheck before starting.

**3. Send a request through the proxy**

```bash
# HTTP proxy mode
curl --proxy http://localhost:8080 https://example.com

# Forwarding mode (full retry support for HTTPS APIs)
curl -H "X-Proxy-Hopper-Target: https://example.com" \
     http://localhost:8080/api/endpoint
```

```python
import requests

# HTTP proxy mode
resp = requests.get("https://example.com", proxies={"https": "http://localhost:8080"})

# Forwarding mode — set a session header, use normal paths
session = requests.Session()
session.headers["X-Proxy-Hopper-Target"] = "https://example.com"
resp = session.get("http://localhost:8080/api/endpoint")
```

---

## Performance — uvloop

By default the Dockerfile installs [uvloop](https://github.com/MagicStack/uvloop), a fast drop-in replacement for asyncio's event loop. Proxy Hopper detects it automatically at startup — no config change needed.

To opt out:

```bash
docker compose build --build-arg UVLOOP=false
```

uvloop is Linux-only and has no effect on Windows or macOS.

---

## Configuration

### Priority order

Settings are resolved in this order (highest wins):

| Priority | Source |
|---|---|
| 1 | CLI flags in the Compose `command:` |
| 2 | `server:` block in `config.yaml` |
| 3 | `PROXY_HOPPER_*` env vars set in `docker-compose.yml` |
| 4 | Built-in defaults |

### config.yaml — IP pools

```yaml
ipPools:
  - name: pool-name
    ipList:
      - "host:port"
      - "host"         # port from defaultProxyPort
```

Multiple targets can share a pool definition. Each target still maintains its own independent rotation state — sharing a pool does not mean IPs are shared at runtime.

### config.yaml — target fields

| Field | Default | Description |
|---|---|---|
| `name` | required | Label used in logs and metrics |
| `regex` | required | Python regex matched against the full request URL |
| `ipList` | required* | Proxy addresses — `host:port` or bare host |
| `ipPool` | required* | Name of a shared `ipPools` entry |
| `proxyUsername` | — | Username for HTTP Basic auth sent to the external proxy |
| `proxyPassword` | — | Password for HTTP Basic auth sent to the external proxy |
| `defaultProxyPort` | `8080` | Port applied to bare IPs without an explicit port |
| `minRequestInterval` | `1s` | **Primary rate-limit knob.** How long an IP is unavailable after any request. |
| `maxQueueWait` | `30s` | How long a request waits for a free IP before failing |
| `numRetries` | `3` | Retry attempts using a different IP on failure |
| `ipFailuresUntilQuarantine` | `5` | Consecutive failures before an IP is quarantined |
| `quarantineTime` | `120s` | How long a quarantined IP sits out |

\* Exactly one of `ipList` or `ipPool` per target. Duration values accept `s`, `m`, `h` suffixes or bare seconds.

### config.yaml — server settings

The `server:` block provides defaults that env vars and CLI flags can override. In this Docker Compose example `backend` and `redisUrl` are intentionally set via `environment:` in `docker-compose.yml` so the same config file can be used across environments.

```yaml
server:
  host: 0.0.0.0
  port: 8080
  logLevel: INFO        # TRACE / DEBUG / INFO / WARNING / ERROR
  logFormat: json       # json recommended for log aggregators
  logFile: null         # path, or omit for stderr
  # backend and redisUrl set via PROXY_HOPPER_* in docker-compose.yml
  metrics: false        # set true to expose /metrics
  metricsPort: 9090
  probe: true           # background IP health prober — on by default
  probeInterval: 60
  probeTimeout: 10
  probeUrls:
    - https://1.1.1.1
    - https://www.google.com
  modes:                # interaction modes to enable (default: all three)
    - connect_tunnel    # HTTPS CONNECT tunnel
    - http_proxy        # traditional HTTP proxy
    - forwarding        # URL-rewriting: /https/host/path
```

### Environment variables

Set in a `.env` file alongside `docker-compose.yml` or in the `environment:` block of `docker-compose.yml`:

```bash
# .env
PROXY_HOPPER_LOG_LEVEL=INFO
PROXY_HOPPER_LOG_FORMAT=json
PROXY_HOPPER_METRICS=true
PROXY_HOPPER_METRICS_PORT=9090
PROXY_HOPPER_PROBE=true
PROXY_HOPPER_PROBE_INTERVAL=60
PROXY_HOPPER_PROBE_URLS=https://1.1.1.1,https://www.google.com
```

### Enabling Prometheus metrics

Uncomment the metrics lines in `docker-compose.yml`:

```yaml
ports:
  - "8080:8080"
  - "9090:9090"
environment:
  PROXY_HOPPER_METRICS: "true"
  PROXY_HOPPER_METRICS_PORT: "9090"
  PROXY_HOPPER_PROBE: "true"
```

When running multiple replicas, each exposes its own `/metrics` endpoint. Configure Prometheus with Docker service discovery or static targets listing all replicas.

---

## Scaling replicas

Because pool state lives in Redis, you can run multiple Proxy Hopper replicas safely. Each IP in the pool is checked out to exactly one replica at a time (Redis `BLPOP` atomicity).

```bash
docker compose up --build --scale proxy-hopper=3
```

> **Note:** When scaling, remove the host port mapping from `docker-compose.yml` and put a load balancer (nginx, Traefik, HAProxy) in front of the replicas.

### Example with Traefik

```yaml
services:
  traefik:
    image: traefik:v3
    command:
      - "--providers.docker=true"
      - "--entrypoints.proxy.address=:8080"
    ports:
      - "8080:8080"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro

  proxy-hopper:
    # remove the `ports:` block and add Traefik labels instead:
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.proxy.entrypoints=proxy"
      - "traefik.http.services.proxy.loadbalancer.server.port=8080"
    deploy:
      replicas: 3
```

---

## Redis configuration

The Redis service in `docker-compose.yml` is configured for a lightweight production-ready setup:

| Setting | Value | Reason |
|---|---|---|
| `save 60 1` | RDB snapshot every 60s if ≥1 key changed | Survives container restarts |
| `maxmemory 256mb` | Hard cap | Prevents Redis from consuming unbounded memory |
| `maxmemory-policy allkeys-lru` | Evict least-recently-used keys | Graceful degradation under memory pressure |

For production, consider replacing the bundled Redis service with a managed Redis instance (AWS ElastiCache, GCP Memorystore, Redis Cloud) and pointing `PROXY_HOPPER_REDIS_URL` at it.
