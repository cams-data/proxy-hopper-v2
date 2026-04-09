# Docker Compose — in-memory backend

Single-container Proxy Hopper deployment using the built-in in-memory IP pool. Suitable for development, testing, and single-host production workloads where pool state does not need to survive restarts.

## Files

```
local-backend/
├── Dockerfile          # Installs proxy-hopper from a GitHub Release wheel
├── docker-compose.yml  # Single-service Compose definition
├── config.yaml         # Full example config (edit before starting)
└── README.md
```

## Quick start

**1. Edit `config.yaml`**

Replace the placeholder IP addresses with your real external proxy IPs and tune the rate-limiting settings for your use case:

```yaml
targets:
  - name: general
    regex: '.*'
    ipList:
      - "your-proxy-1.example.com:3128"
      - "your-proxy-2.example.com:3128"
    minRequestInterval: 1s   # one request per IP per second
    maxQueueWait: 30s
    numRetries: 3
    ipFailuresUntilQuarantine: 5
    quarantineTime: 2m
```

**2. Build and start**

```bash
cd examples/docker-compose/local-backend
docker compose up --build
```

By default this installs `v0.1.0`. To use a different release:

```bash
PROXY_HOPPER_VERSION=0.2.0 docker compose up --build
```

**3. Send a request through the proxy**

```bash
curl --proxy http://localhost:8080 http://example.com
```

```python
import requests
resp = requests.get("http://example.com", proxies={"http": "http://localhost:8080"})
```

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

### config.yaml — target fields

| Field | Default | Description |
|---|---|---|
| `name` | required | Label used in logs and metrics |
| `regex` | required | Python regex matched against the full request URL |
| `ipList` | required* | Proxy addresses — `host:port` or bare host |
| `ipPool` | required* | Name of a shared `ipPools` entry |
| `defaultProxyPort` | `8080` | Port applied to bare IPs without an explicit port |
| `minRequestInterval` | `1s` | **Primary rate-limit knob.** How long an IP is unavailable after any request. |
| `maxQueueWait` | `30s` | How long a request waits for a free IP before failing |
| `numRetries` | `3` | Retry attempts using a different IP on failure |
| `ipFailuresUntilQuarantine` | `5` | Consecutive failures before an IP is quarantined |
| `quarantineTime` | `120s` | How long a quarantined IP sits out |

\* Exactly one of `ipList` or `ipPool` per target. Duration values accept `s`, `m`, `h` suffixes or bare seconds.

### config.yaml — server settings

The `server:` block in `config.yaml` provides defaults that env vars and CLI flags can override:

```yaml
server:
  host: 0.0.0.0
  port: 8080
  logLevel: INFO        # TRACE / DEBUG / INFO / WARNING / ERROR
  logFormat: text       # text or json
  logFile: null         # path, or omit for stderr
  backend: memory
  metrics: false        # set true to expose /metrics
  metricsPort: 9090
  probe: false          # background IP health prober
  probeInterval: 60
  probeTimeout: 10
  probeUrls:
    - https://1.1.1.1
    - https://www.google.com
```

### Environment variables

All `PROXY_HOPPER_*` variables map directly to server settings. Set them in a `.env` file alongside `docker-compose.yml` or export them in your shell:

```bash
# .env
PROXY_HOPPER_LOG_LEVEL=DEBUG
PROXY_HOPPER_LOG_FORMAT=json
PROXY_HOPPER_METRICS=true
PROXY_HOPPER_METRICS_PORT=9090
PROXY_HOPPER_PROBE=true
PROXY_HOPPER_PROBE_INTERVAL=60
PROXY_HOPPER_PROBE_URLS=https://1.1.1.1,https://www.google.com
```

### Enabling Prometheus metrics

Uncomment the metrics environment variables in `docker-compose.yml` and expose the metrics port:

```yaml
ports:
  - "8080:8080"
  - "9090:9090"
environment:
  PROXY_HOPPER_METRICS: "true"
  PROXY_HOPPER_METRICS_PORT: "9090"
```

Or add them to the `server:` block in `config.yaml`. Then scrape `http://localhost:9090/metrics`.

---

## Limitations of the in-memory backend

The in-memory backend holds all IP pool state in the process:

- **State is lost on restart** — IPs return to full availability, quarantines are cleared
- **No horizontal scaling** — each replica has its own independent pool, so the same IP can be checked out concurrently by different instances

For shared pool state across restarts or replicas, use the [Redis backend example](../local-redis/).
