# CLAUDE.md

Guidance for Claude Code sessions working in this repo. Read this before doing anything non-trivial — this project has a few gotchas that aren't obvious from the code alone.

## What this is

Proxy Hopper is a rotating HTTPS proxy: clients send it a request with an `X-Proxy-Hopper-Target` header naming the real destination, and it forwards that request through a rotating pool of external proxy IPs, retrying on failure and quarantining broken IPs. Full detail: [README.md](README.md) and [python_modules/proxy-hopper/README.md](python_modules/proxy-hopper/README.md).

For a from-scratch architecture read (written by reading the whole codebase, then corrected by the project owner), see `../PROJECT_REVIEW.md` at the workspace root. It's the deepest single source of "why does this look like this."

## The one thing to internalize before anything else: `main` is not current

This repo isn't in production yet, so `main` only moves on deliberate release and can be weeks behind. **`next` is the real integration branch.** Feature branches (`feat/*`) branch off `next` and merge back into it. If you're asked to "check the current state of the project" or "what were we last working on," look at `next` and any active `feat/*` branch — never assume `main` reflects recent work.

Pushing to `next` publishes preview Docker images + a preview Helm chart to `ghcr.io` — it is not a dry-run branch, treat merges to it as real releases-in-waiting. Pushing to `main` cuts an actual stable release (version bump, `CHANGELOG.md`, `:latest` images).

The docs site (sibling repo `../proxy-hopper-docs/`) is meant to be updated after each feature ships to `next` — if it's missing something that's on `next`, that's a real gap to flag, not expected staleness.

## Three systems with similar-sounding names — don't conflate them

- **Auth** (`python_modules/proxy-hopper/src/proxy_hopper/auth/`) — who's allowed to send traffic *to* Proxy Hopper (API keys / local JWT / OIDC). Gates the proxy port and the admin API.
- **Identity** (`.../identity/`) — a browser fingerprint + cookie jar Proxy Hopper presents *to the target site*, bound to one (proxy IP, target) pair. Nothing to do with who's allowed to use Proxy Hopper.
- **Token server** (`python_modules/proxy-hopper-token-server/`) — a separate optional package that fetches/refreshes auth tokens *for the target API itself* (e.g. the upstream requires OAuth). Pairs with the `X-Proxy-Hopper-Force-IP` header so token acquisition and token use share an egress IP.

If a task mentions "auth," ask which of these three it actually means before touching code.

## Architecture in one paragraph

Everything storage-related goes through an abstract `Backend` (queue/counter/zset/kv/lock/rolling-log/pub-sub), implemented by `MemoryBackend` (core, single-process) or `RedisBackend` (`proxy-hopper-redis`, HA). `IdentityQueue`/`IPPoolStore` (`pool.py`, `pool_store.py`) own rotation/quarantine/identity policy on top of the Backend; `ProxyRepository` (`repository.py`) owns provider/pool/target CRUD + hot-reload pub/sub. The core `proxy-hopper` package is a raw asyncio TCP listener with no HTTP framework — it only implements one `RequestHandler`, `ForwardingHandler`. The admin GraphQL API, SSE event stream, and admin-ui hosting live in a *separate* package and process, `proxy-hopper-webserver` (FastAPI) — core has an intentional `ImportError` stub where GraphQL code used to be, redirecting you there. `admin-ui` is a React/Vite/Tailwind SPA that talks to `proxy-hopper-webserver` over GraphQL + SSE.

Config cascades `proxyProviders` → `ipPools` → `targets`. Every provider/pool/target has `static` (YAML-owned, API can't touch it) and `mutable` (can the API ever edit it — narrower, and currently only checked on *update*, not on `remove_*` — see the gotcha below).

## Known gotchas — check before you rely on these

- **`proxy-hopper-token-server` currently fails to import.** `_internal/app.py` does `from ._handler import create_token_router` but the module is `handler.py`. Its own test suite fails at collection. This is almost certainly exactly where the last session stopped (last commit added an end-to-end example that works around it by not using the library at all). See [TODO.md](TODO.md) item 1.
- **`static`/`mutable` asymmetry**: `update_target`/`update_provider`/`update_pool` in `repository.py` check both `static` and `mutable`, but `remove_target`/`remove_provider`/`remove_pool` only check `static`. A `static=False, mutable=False` entity currently *cannot be edited but can still be deleted* via the API. Confirm this is intentional before changing behavior around it — it wasn't when this was investigated.
- **Don't trust the top-level `README.md` from before 2026-08-02** for describing integration modes — it used to (incorrectly) describe three modes. It's been rewritten; if you see a *new* copy claiming HTTP-proxy or CONNECT-tunnel modes exist, that's regressed, not a feature.
- **`python_modules/proxy-hopper/src/proxy_hopper/graphql/`** (five files besides `__init__.py`) is dead code — stale duplicates from before the webserver package split. Don't edit these thinking they're live; the real GraphQL code is in `proxy-hopper-webserver/src/proxy_hopper_webserver/graphql/`.
- Three different token-server examples exist under `examples/`; see [CLEANUP.md](CLEANUP.md) for which is canonical and why.

## Commands

```bash
# Core engine tests
cd python_modules/proxy-hopper && uv run pytest

# Redis backend tests
cd python_modules/proxy-hopper-redis && uv run pytest

# Admin webserver tests
cd python_modules/proxy-hopper-webserver && uv run pytest

# Token-server package tests (currently fails at collection — see gotchas above)
cd python_modules/proxy-hopper-token-server && uv run pytest

# Cross-backend contract tests (memory + Redis, parametrized — add a new backend by adding one factory entry in conftest.py)
cd python_modules/tests && uv run pytest

# Integration tests (fake proxy + fake upstream, no real network)
cd python_modules/proxy-hopper-testserver && uv run pytest

# Admin UI
cd admin-ui && npm install && npm run dev
```

`docker-test/` and `kube-test/` are gitignored, developer-local scratch environments (compose stack, ArgoCD app) — not official examples, don't treat their contents as documented product surface or assume they're current.

## Companion docs in this repo

- [TODO.md](TODO.md) — prioritized, actionable work list
- [CLEANUP.md](CLEANUP.md) — things flagged for removal/simplification, with file paths
- [CONTRIBUTING.md](CONTRIBUTING.md) — commit conventions (Conventional Commits → automatic semver) and branch model. Note: its release-flow description is stale (mentions `release-please`; actual pipeline is `git-cliff` + `github-tag-action`) — see CLEANUP.md.
