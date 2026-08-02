# Proxy Hopper

A rotating HTTPS proxy server. It sits between your application and the internet, routes outbound requests through a pool of external proxy IP addresses, retries on failure by rotating to a different IP, and automatically quarantines IPs that keep failing.

Clients integrate via **forwarding mode**: set an `X-Proxy-Hopper-Target` header to the real destination and send the request to Proxy Hopper as if it *were* that destination. Because Proxy Hopper owns the full HTTPS request end-to-end, it can retry on 429/5xx responses — something an opaque CONNECT tunnel cannot do. (Older HTTP-proxy and CONNECT-tunnel integration modes existed early in this project's history and were deliberately removed — see [CLEANUP.md](CLEANUP.md) if you find stray references to them.)

```
Your app  ── X-Proxy-Hopper-Target: https://api.example.com ──►  Proxy Hopper  ──►  external proxy IP  ──►  api.example.com
```

```python
import requests

session = requests.Session()
session.headers["X-Proxy-Hopper-Target"] = "https://example.com"
resp = session.get("http://localhost:8080/api/endpoint")
```

```bash
curl -H "X-Proxy-Hopper-Target: https://example.com" http://localhost:8080/api/endpoint
```

For the full config reference, auth setup, managed-auth/token-server integration, metrics, and architecture notes, see **[python_modules/proxy-hopper/README.md](python_modules/proxy-hopper/README.md)** — that package README is the canonical deep-dive; this file is an index.

---

## What's in this repo

Proxy Hopper isn't one process — it's six independently-installable pieces sharing a common storage/event contract:

| Piece | What it does |
|---|---|
| [`proxy-hopper`](python_modules/proxy-hopper/) | Core engine: the TCP listener, request routing, retry/quarantine logic, config, auth, identity. Start here. |
| [`proxy-hopper-redis`](python_modules/proxy-hopper-redis/) | Redis implementation of the storage backend, for multi-instance HA (`pip install "proxy-hopper[redis]"`) |
| [`proxy-hopper-webserver`](python_modules/proxy-hopper-webserver/) | Separate FastAPI process: GraphQL admin API, live SSE event stream, and serves the built admin UI. Only needed if you want the admin API/UI. |
| [`admin-ui`](admin-ui/) | React/Vite/Tailwind admin frontend — manage targets/providers/pools, watch a live request log |
| [`proxy-hopper-token-server`](python_modules/proxy-hopper-token-server/) | Optional library + server for target APIs that need managed auth tokens (OAuth, session cookies, etc.) — see [TODO.md](TODO.md), currently has a known broken import |
| [`proxy-hopper-testserver`](python_modules/proxy-hopper-testserver/) | Test-only fake proxy + fake upstream used by the integration test suite. Not shipped. |

Install just `proxy-hopper` for the proxy itself. Add `proxy-hopper-webserver` (+ build `admin-ui`) if you want the admin API/UI. Add `proxy-hopper-token-server` only if a target needs managed auth.

### Three systems that sound similar but aren't

- **Auth** (`auth/` in core) — gates who may send traffic *to* Proxy Hopper (API keys, local JWT, OIDC).
- **Identity** (`identity/` in core) — a persistent browser fingerprint + cookie jar Proxy Hopper presents *to the target site*, bound to one (IP, target) pair.
- **Token server** (`proxy-hopper-token-server`) — fetches and refreshes auth tokens *for the target API itself* (e.g. an OAuth-protected upstream), separate from both of the above.

## Repository layout

```
python_modules/
├── proxy-hopper/              # Core engine — start here
├── proxy-hopper-redis/        # Redis backend
├── proxy-hopper-webserver/    # Admin API (GraphQL + SSE), serves admin-ui build
├── proxy-hopper-token-server/ # Managed-auth token server library + CLI
├── proxy-hopper-testserver/   # Test-only fakes (not shipped)
└── tests/                     # Cross-backend contract tests (memory + Redis, parametrized)
admin-ui/                      # React admin frontend
charts/proxy-hopper/           # Helm chart (proxy + optional admin + optional token-server)
docker/                        # Dockerfiles for the published images
examples/
├── docker-compose/            # local-backend, local-redis, auth-api-keys, auth-oidc, token-server (placeholder)
├── kubernetes/                # Raw manifests, non-Helm alternative
└── token-server/              # Complete, runnable token-server example — see its own README
monitoring/grafana/dashboards/ # Admin and user-facing Grafana dashboards
docker-test/, kube-test/       # Gitignored local scratch environments — not shipped, not official examples
```

## Running tests

```bash
# Core unit tests
cd python_modules/proxy-hopper && uv run pytest

# Redis backend tests
cd python_modules/proxy-hopper-redis && uv run pytest

# Admin webserver tests
cd python_modules/proxy-hopper-webserver && uv run pytest

# Cross-backend contract tests (memory + Redis, parametrized)
cd python_modules/tests && uv run pytest

# Integration tests (fake proxy + fake upstream)
cd python_modules/proxy-hopper-testserver && uv run pytest
```

`ci.yml` runs this full matrix on every PR and every branch push.

## Branches and releases — read this before assuming `main` is current

- **`main`** only moves on deliberate release; it is not "the current state" of the project day-to-day. This project isn't in production yet, so `main` gets updated in bursts rather than continuously — expect that to change once it is.
- **`next`** is the real integration branch. Feature branches merge into `next`; pushing to `next` publishes preview Docker images and a preview Helm chart to `ghcr.io`.
- Pushing to `main` cuts a stable release: version bump, `CHANGELOG.md` update, `:latest` images, stable Helm chart.
- Work on a `feat/*` branch off `next`, merge back to `next` when done. Don't expect to find the newest work by checking out `main`.

## More docs

- [python_modules/proxy-hopper/README.md](python_modules/proxy-hopper/README.md) — full config reference, auth, managed auth, metrics, architecture
- [proxy-hopper-docs](../proxy-hopper-docs/) (sibling repo) — the public Mintlify docs site. Updated after each feature ships to `next`, so it can lag slightly behind the newest work on a feature branch.
- [TODO.md](TODO.md) — known work still to do
- [CLEANUP.md](CLEANUP.md) — things flagged for removal/simplification (stale docs, dead code, superseded examples)
- [CONTRIBUTING.md](CONTRIBUTING.md) — commit conventions and branch model
- [PROJECT_REVIEW.md](../PROJECT_REVIEW.md) (workspace root) — a full architecture/history write-up with the project owner's own annotations; useful if you want more depth than this README
