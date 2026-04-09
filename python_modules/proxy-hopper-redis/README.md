# proxy-hopper-redis

Redis-backed IP pool backend for [proxy-hopper](../proxy-hopper/). Enables high-availability, multi-instance deployments where several Proxy Hopper processes share the same IP pool via Redis.

## When to use this

The default in-memory backend is sufficient for single-process deployments. Use `proxy-hopper-redis` when you need:

- **Horizontal scaling** — multiple Proxy Hopper instances behind a load balancer, sharing one IP pool
- **Persistence across restarts** — pool state survives process restarts (IP availability, quarantine timers)
- **Distributed rate-limit enforcement** — each external proxy IP is issued to exactly one request at a time, across all instances

## Installation

```bash
pip install proxy-hopper-redis
```

Requires `proxy-hopper>=0.1.0` and a Redis server (5.0+).

## Usage

```bash
proxy-hopper run --config config.yaml --backend redis --redis-url redis://redis:6379/0
```

Or via environment variables:

```bash
PROXY_HOPPER_BACKEND=redis \
PROXY_HOPPER_REDIS_URL=redis://redis:6379/0 \
proxy-hopper run --config config.yaml
```

## Redis data model

All keys are namespaced under `ph:{target}:` to avoid collisions when sharing a Redis instance across environments.

| Key pattern | Type | Description |
|---|---|---|
| `ph:{target}:pool` | List | Available IP addresses (RPUSH / BLPOP) |
| `ph:{target}:failures:{ip}` | String | Consecutive failure count per IP (INCR / SET) |
| `ph:{target}:quarantine` | Sorted Set | Quarantined IPs; score = release epoch |
| `ph:{target}:initialized` | String | Init lock (SETNX); expires after 24h |

## Concurrency safety

| Operation | Redis primitive | Safety guarantee |
|---|---|---|
| IP checkout | `BLPOP` | Exactly one consumer receives each IP |
| Failure count | `INCR` | Atomic; no lost updates under concurrent access |
| Quarantine release | `ZRANGEBYSCORE` + `ZREM` | Only the instance that wins `ZREM` processes each entry |
| Pool seeding | `SETNX` on init key | Exactly one instance seeds the pool on startup |

## Docker Compose example

```yaml
services:
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"

  proxy-hopper:
    image: proxy-hopper:latest
    environment:
      PROXY_HOPPER_CONFIG: /etc/proxy-hopper/config.yaml
      PROXY_HOPPER_BACKEND: redis
      PROXY_HOPPER_REDIS_URL: redis://redis:6379/0
      PROXY_HOPPER_LOG_FORMAT: json
      PROXY_HOPPER_METRICS: "true"
    volumes:
      - ./config.yaml:/etc/proxy-hopper/config.yaml:ro
    ports:
      - "8080:8080"
      - "9090:9090"
    depends_on:
      - redis
    deploy:
      replicas: 3   # All three instances share the Redis IP pool
```

## Architecture

`RedisIPPoolBackend` implements the same `IPPoolBackend` interface as the in-memory backend — it is a pure storage layer with no business logic. All quarantine policy, cooldown scheduling, and failure thresholds live in `IPPool` (in `proxy-hopper`).

```
proxy-hopper (IPPool)                proxy-hopper-redis (RedisIPPoolBackend)
─────────────────────                ───────────────────────────────────────
record_failure(address)    ──────►   increment_failures(target, address) → INCR
  if failures >= threshold ──────►   quarantine_add(target, address, release_at) → ZADD
acquire(timeout)           ──────►   pop_ip(target, timeout) → BLPOP
_sweep_quarantine()        ──────►   quarantine_pop_expired(target, now) → ZRANGEBYSCORE+ZREM
record_success(address)    ──────►   reset_failures(target, address) → SET 0
                                     push_ip(target, address) → RPUSH
```

## Programmatic use

If you're integrating directly rather than using the CLI:

```python
from proxy_hopper_redis import RedisIPPoolBackend

backend = RedisIPPoolBackend("redis://localhost:6379/0")
await backend.start()   # connects and pings Redis

# Use via IPPool (recommended)
from proxy_hopper.pool import IPPool
from proxy_hopper.config import TargetConfig

config = TargetConfig(
    name="my-target",
    regex=r".*example\.com.*",
    ip_list=["10.0.0.1:3128", "10.0.0.2:3128"],
    min_request_interval=1.0,
    max_queue_wait=30.0,
    num_retries=3,
    ip_failures_until_quarantine=5,
    quarantine_time=120.0,
)
pool = IPPool(config, backend)
await pool.start()

address = await pool.acquire(timeout=10.0)   # "10.0.0.1:3128"
await pool.record_success(address)

await pool.stop()
await backend.stop()
```

## Testing with fakeredis

The test suite uses [fakeredis](https://github.com/cunla/fakeredis-py) to run without a real Redis server. You can do the same in your own tests by injecting a fake client before calling `start()`:

```python
import fakeredis.aioredis as fakeredis
from proxy_hopper_redis import RedisIPPoolBackend

backend = RedisIPPoolBackend()
backend._redis = fakeredis.FakeRedis(decode_responses=True)
await backend.start()   # pings the fake server — no real Redis needed
```

## Development

```bash
cd python_modules/proxy-hopper-redis
uv sync
uv run pytest

# Cross-backend contract tests (run against both memory and Redis)
cd ../tests && uv run pytest
```
