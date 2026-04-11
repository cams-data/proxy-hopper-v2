# Proxy Hopper

A rotating HTTP/HTTPS proxy server that routes outbound requests through a pool of external proxy IP addresses. Configure your HTTP client to point at Proxy Hopper as its proxy, and Proxy Hopper handles picking an IP, retrying on failure, and quarantining broken proxies automatically.

## How it works

```
Your app  ──────────────────►  Proxy Hopper  ──►  external proxy IP  ──►  target site
          HTTP proxy / CONNECT   (rotating)          (from your pool)
          or URL-forwarding
```

- Inbound requests are matched against a list of targets using regular expressions
- Each target has its own pool of external proxy IPs managed as a FIFO queue
- IPs that accumulate failures are quarantined for a configurable period, then released back
- All pool state can be held in-memory (single instance) or Redis (multi-instance HA)

### Interaction modes

Proxy Hopper supports three ways for clients to send requests. All three use the same IP rotation and retry logic.

| Mode | How to use | Best for |
|---|---|---|
| **HTTP proxy** | Set `http_proxy=http://proxy-hopper:8080` | Any HTTP client with proxy support |
| **CONNECT tunnel** | Set `https_proxy=http://proxy-hopper:8080` | HTTPS via standard proxy settings |
| **URL forwarding** | Change base URL to `http://proxy-hopper:8080/https/api.example.com` | Full retry on HTTPS requests; one-line integration change |

> **Why forwarding mode?** CONNECT tunnels are opaque byte relays — Proxy Hopper cannot retry a mid-flight HTTPS failure. Forwarding mode lets Proxy Hopper own the full HTTPS request, enabling retries and IP rotation even on 429/5xx responses from the target API.

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
proxyProviders:
  - name: my-provider
    auth:
      type: basic
      username: user
      password: secret
    ipList:
      - "10.0.0.1:3128"
      - "10.0.0.2:3128"
      - "10.0.0.3:3128"
    regionTag: US-East

ipPools:
  - name: my-pool
    ipRequests:
      - provider: my-provider
        count: 3

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

Then use one of the three integration modes:

```python
# HTTP/HTTPS proxy mode (standard)
import requests
resp = requests.get("https://example.com", proxies={"https": "http://localhost:8080"})

# Forwarding mode (full retry support — set a header, use normal URLs)
session = requests.Session()
session.headers["X-Proxy-Hopper-Target"] = "https://example.com"
resp = session.get("http://localhost:8080/api/endpoint")
```

```bash
# HTTP proxy mode
curl --proxy http://localhost:8080 https://example.com

# Forwarding mode
curl -H "X-Proxy-Hopper-Target: https://example.com" \
     http://localhost:8080/api/endpoint
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
