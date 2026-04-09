# Docker Compose — in-memory backend

Single-container Proxy Hopper deployment using the built-in in-memory IP pool. Suitable for development, testing, and single-host production workloads where pool state does not need to survive restarts.

## Files

```
local-backend/
├── Dockerfile          # Builds the proxy-hopper image from source
├── docker-compose.yml  # Single-service Compose definition
├── config.yaml         # Target definitions (edit before starting)
└── README.md
```

## Quick start

**1. Add your proxy IPs to `config.yaml`**

Replace the placeholder addresses in `config.yaml` with your real external proxy IP addresses:

```yaml
targets:
  - name: general
    regex: '.*'
    ipList:
      - "your-proxy-1.example.com:3128"
      - "your-proxy-2.example.com:3128"
```

**2. Build and start**

```bash
cd examples/docker-compose/local-backend
docker compose up --build
```

By default this installs `v0.1.0`. To use a different release, set `PROXY_HOPPER_VERSION`:

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

## Configuration

### Environment variables

Override any option via a `.env` file or shell environment:

```bash
# .env
LOG_LEVEL=DEBUG
LOG_FORMAT=json
PROXY_PORT=8080
METRICS_PORT=9090
```

All `PROXY_HOPPER_*` variables are passed directly to the container. See the [proxy-hopper README](../../../python_modules/proxy-hopper/README.md) for the full list.

### Enabling Prometheus metrics

Uncomment the metrics lines in `docker-compose.yml`:

```yaml
environment:
  PROXY_HOPPER_METRICS: "true"
  PROXY_HOPPER_METRICS_PORT: "9090"
```

Then scrape `http://localhost:9090/metrics`.

## Limitations

The in-memory backend stores all IP pool state in the process. This means:

- **State is lost on restart** — IPs return to full availability, quarantines are cleared
- **No horizontal scaling** — running multiple replicas gives each instance its own independent pool, which can cause the same IP to be used concurrently by different instances

For shared pool state across restarts or replicas, use the [Redis backend example](../local-redis/).
