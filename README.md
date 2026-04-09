# Proxy Hopper

A rotating HTTP/HTTPS proxy server that routes outbound requests through a pool of external proxy IP addresses. Configure your HTTP client to point at Proxy Hopper as its proxy, and Proxy Hopper handles picking an IP, retrying on failure, and quarantining broken proxies automatically.

## How it works

```
Your app  ──HTTP/CONNECT──►  Proxy Hopper  ──►  external proxy IP  ──►  target site
                               (rotating)          (from your pool)
```

- Clients configure Proxy Hopper as their HTTP proxy using standard proxy settings
- Inbound requests are matched against a list of targets using regular expressions
- Each target has its own pool of external proxy IPs managed as a FIFO queue
- IPs that accumulate failures are quarantined for a configurable period, then released back
- All pool state can be held in-memory (single instance) or Redis (multi-instance HA)

## Packages

| Package | Description |
|---|---|
| [`proxy-hopper`](python_modules/proxy-hopper/) | Core proxy server, in-memory backend, CLI |
| [`proxy-hopper-redis`](python_modules/proxy-hopper-redis/) | Redis backend for HA multi-instance deployments |

## Quick start

```bash
pip install proxy-hopper
```

```yaml
# config.yaml
ipPools:
  - name: my-pool
    ipList:
      - "10.0.0.1:3128"
      - "10.0.0.2:3128"
      - "10.0.0.3:3128"

targets:
  - name: general
    regex: '.*'
    ipPool: my-pool
    minRequestInterval: 1s   # hold each IP off the pool for 1s between uses
    maxQueueWait: 30s        # fail if no IP free within 30s
    numRetries: 3
    ipFailuresUntilQuarantine: 5
    quarantineTime: 2m
```

```bash
proxy-hopper run --config config.yaml
```

Then point your HTTP client at `http://localhost:8080`:

```python
import requests
resp = requests.get("http://example.com", proxies={"http": "http://localhost:8080"})
```

## Repository layout

```
examples/
├── docker-compose/
│   ├── local-backend/     # Single container, in-memory pool
│   └── local-redis/       # proxy-hopper + Redis, scalable
└── kubernetes/            # Kubernetes manifests (Deployment, HPA, Redis StatefulSet)
python_modules/
├── proxy-hopper/          # Core package
├── proxy-hopper-redis/    # Redis backend add-on
└── tests/                 # Backend + pool contract tests (run against every backend)
```

## Deployment examples

| Example | Description |
|---|---|
| [docker-compose/local-backend](examples/docker-compose/local-backend/) | Single Docker container, in-memory pool — good for development and single-host deployments |
| [docker-compose/local-redis](examples/docker-compose/local-redis/) | Docker Compose with Redis, scalable to multiple replicas |
| [kubernetes/](examples/kubernetes/) | Full Kubernetes setup: Deployment, HPA, Redis StatefulSet, Services |

## Running tests

Each package has its own test suite:

```bash
# Core unit tests
cd python_modules/proxy-hopper && uv run pytest

# Redis backend tests
cd python_modules/proxy-hopper-redis && uv run pytest

# Cross-backend contract tests (memory + Redis, parametrized)
cd python_modules/tests && uv run pytest
```
