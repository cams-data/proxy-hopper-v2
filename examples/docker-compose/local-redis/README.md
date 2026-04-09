# Docker Compose — Redis backend

Two-service Proxy Hopper deployment using Redis for shared IP pool state. Suitable for production workloads where you need pool state to survive restarts, or where you want to scale out multiple proxy replicas that share a single IP pool.

## Files

```
local-redis/
├── Dockerfile          # Builds the proxy-hopper image with redis add-on
├── docker-compose.yml  # proxy-hopper + Redis services
├── config.yaml         # Target definitions (edit before starting)
└── README.md
```

## Quick start

**1. Add your proxy IPs to `config.yaml`**

Replace the placeholder addresses with your real external proxy IPs:

```yaml
targets:
  - name: general
    regex: '.*'
    ipList:
      - "your-proxy-1.example.com:3128"
      - "your-proxy-2.example.com:3128"
```

**2. Build and start**

Run from the repo root:

```bash
docker compose -f examples/docker-compose/local-redis/docker-compose.yml up --build
```

Or change into this directory first:

```bash
cd examples/docker-compose/local-redis
docker compose up --build
```

Proxy Hopper will wait for Redis to pass its healthcheck before starting.

**3. Send a request through the proxy**

```bash
curl --proxy http://localhost:8080 http://example.com
```

```python
import requests
resp = requests.get("http://example.com", proxies={"http": "http://localhost:8080"})
```

## Scaling replicas

Because pool state lives in Redis, you can run multiple Proxy Hopper replicas safely. Each IP in the pool is checked out to exactly one replica at a time (Redis `BLPOP` atomicity), preventing the same IP from being used concurrently.

```bash
docker compose up --build --scale proxy-hopper=3
```

> **Note:** When scaling, remove the host port mapping from `docker-compose.yml` and put a load balancer (nginx, Traefik, HAProxy) in front of the replicas, otherwise Docker will refuse to bind the same host port to multiple containers.

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

The default `LOG_FORMAT` in this example is `json` — appropriate for production environments where logs are collected by Fluentd, Datadog, or a similar aggregator.

All `PROXY_HOPPER_*` variables are passed directly to the container. See the [proxy-hopper README](../../../python_modules/proxy-hopper/README.md) for the full list.

### Enabling Prometheus metrics

Uncomment the metrics lines in `docker-compose.yml`:

```yaml
environment:
  PROXY_HOPPER_METRICS: "true"
  PROXY_HOPPER_METRICS_PORT: "9090"
```

When running multiple replicas, each exposes its own `/metrics` endpoint. Configure your Prometheus scrape config to discover all replicas using Docker service discovery or static targets.

## Redis configuration

The Redis service in `docker-compose.yml` is configured for a lightweight production-ready setup:

| Setting | Value | Reason |
|---|---|---|
| `save 60 1` | RDB snapshot every 60s if ≥1 key changed | Survives container restarts |
| `maxmemory 256mb` | Hard cap | Prevents Redis from consuming unbounded memory |
| `maxmemory-policy allkeys-lru` | Evict least-recently-used keys | Graceful degradation under memory pressure |

Pool keys have a 24-hour TTL on the init lock (`ph:{target}:initialized`) so the pool will automatically re-seed after a full Redis flush or keyspace expiry.

For production, consider replacing the bundled Redis service with a managed Redis instance (AWS ElastiCache, GCP Memorystore, Redis Cloud) and pointing `PROXY_HOPPER_REDIS_URL` at it.
