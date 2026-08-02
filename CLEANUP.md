# CLEANUP.md

Things that exist in this repo (and the workspace around it) purely as leftover confusion — dead code, stale docs, superseded examples, orphaned config fields. Nothing here has been deleted automatically; this is a documented list for a deliberate pass. Cross-reference [TODO.md](TODO.md) for the fixes that aren't pure deletions.

## A. Stale references to the two dropped integration modes

Proxy Hopper originally supported three ways to send it traffic: HTTP proxy mode, CONNECT tunnel mode, and forwarding mode. Commit `2f62d55` (`feat!: drop HTTP proxy and CONNECT tunnel modes, add X-Proxy-Hopper-Tag/Retries headers`) removed the first two — forwarding mode (`X-Proxy-Hopper-Target` header) is the only one the code implements today. The following still describe or configure the removed modes:

- **`examples/docker-compose/local-backend/README.md`** — lines ~48-51 and ~61-62 show `curl --proxy http://localhost:8080 ...` / `requests.get(..., proxies=...)` snippets labeled "HTTP proxy mode," which no longer works. Lines ~139-141 show a `modes:` config block listing `connect_tunnel` / `http_proxy` / `forwarding` — **this field doesn't exist in `config/models.py` at all anymore**; the whole block is dead.
- **`examples/docker-compose/local-redis/README.md`** — same two problems, same shape (HTTP-proxy-mode snippets + a `modes:`/`connect_tunnel` block).
- **`examples/docker-compose/auth-api-keys/README.md`** — "HTTP proxy mode" curl snippets (two occurrences).
- **`examples/docker-compose/auth-oidc/README.md`** — same.
- **(Root, rewritten 2026-08-02)** The old top-level `README.md` had a full three-mode comparison table and code samples for all three. That's been replaced — flagging here only so a stray `git stash`/old-branch merge doesn't silently reintroduce it.

**Recommended fix**: in each example README, delete the "HTTP proxy mode" snippet and keep only the forwarding-mode one; delete the `modes:`/`connect_tunnel` lines from the example `docker-compose.yml`/config snippets entirely.

**Not code, but worth a decision**: the workspace-root file `Proxy Hopper - Proxy Hopper.html` (a saved browser snapshot of an old docs-site landing page) still describes the three-mode story. It's inert — not part of any build — but someone opening it cold could mistake it for current. Recommend deleting it, or moving it into `archive/` alongside the retired `ROADMAP.md`. Left as-is for now since it's outside git and irreversible to remove without a backup; ask before deleting.

## B. Dead code

- **`python_modules/proxy-hopper/src/proxy_hopper/graphql/`** — `queries.py`, `mutations.py`, `types.py`, `inputs.py`, `context.py` are stale, unused duplicates of the real GraphQL implementation, which now lives in `proxy-hopper-webserver/src/proxy_hopper_webserver/graphql/`. The sibling `__init__.py` in the same core directory already raises `ImportError` telling callers to use the webserver package instead — so these five files are unreachable through the package's own public API. Confirmed nothing outside this directory imports them directly (only the redirect `__init__.py` is imported). **Recommendation: delete the five files**, keep the `__init__.py` redirect stub.
- **`python_modules/proxy-hopper/src/proxy_hopper/identity/store.py`** — entire file content is a comment stating `IdentityStore` was removed and replaced by `IdentityQueue`/`IPPoolStore`. Not imported anywhere. **Recommendation: delete the file.**
- **`python_modules/proxy-hopper/src/proxy_hopper/backend/base.py`** — `IPPoolBackend = Backend` (line 271) is a deprecated alias for the `Backend` ABC. Grep for `IPPoolBackend` usage outside this file before deleting; if nothing external imports it, remove it.
- **`python_modules/proxy-hopper-redis/src/proxy_hopper_redis/backend.py`** — `RedisIPPoolBackend` (line ~331) is the Redis-side counterpart of the same deprecated alias. Same treatment: confirm no external imports, then delete.
- **`python_modules/proxy-hopper/src/proxy_hopper/models.py`** — `IPState` and `ReturnReason` dataclasses look like leftovers from a design predating `Identity`/`IdentityQueue`. Grep for constructors of either before deleting — if nothing builds an `IPState` in the current pipeline, they're dead.

## C. Stale docs / config

- **`ROADMAP.md`** (workspace root) — retired 2026-08-02 per project owner decision (all three items — auth, GraphQL, front-end — have shipped). Moved to `../archive/ROADMAP.md` with a header note; not deleted outright since the workspace root isn't under version control and an outright delete would be unrecoverable.
- **`CONTRIBUTING.md`** — its release-flow section describes a `release-please` Release-PR workflow that the actual CI (`next.yml`, `release.yml`) no longer uses (they use `git-cliff` + `github-tag-action`, tagging directly on push). Needs a rewrite to match reality. See TODO.md item 8.
- **Remote branch `origin/release-please--branches--next`** — leftover from the old release-please setup. Dangling, safe to delete once someone with push access confirms. Not deleted here (remote branch deletion is a shared/irreversible action outside this review's scope).
- **`python_modules/proxy-hopper/README.md` line 17** — "Requires Python 3.11+" is stale; `pyproject.toml` requires `>=3.12` since commit `68d1038`. One-line fix, see TODO.md item 7.

## D. Branch hygiene (not executed — listed for your call)

Confirmed via `git merge-base --is-ancestor <branch> next` that these local branches are fully merged into `next` and carry no unique commits:

- `drop-legacy-modes`
- `feat/authentication`
- `feat/code-tidy`
- `feat/ip-pool-entity`

Safe to `git branch -d` any of these locally. Not done automatically — deleting branches wasn't part of what was asked, and it's a one-way door for anyone who had them checked out elsewhere.

## E. Examples confusion — token-server, specifically

**Update 2026-08-02**: the library bugs referenced below (broken import, broken `ProxyHopperClient` protocol) are fixed — see TODO.md item 1 and `python_modules/proxy-hopper-token-server/README.md`. `auckland_council.py`'s docstring/path issue is also fixed. The two remaining examples now both work and deliberately demonstrate different things; only the generic Compose placeholder still needs a pointer fix.

There are effectively **three "token server" examples** plus one generic placeholder, and it's easy to land on the wrong one:

1. **`examples/token-server/token_server/main.py`** (+ its `Dockerfile`, `docker-compose.yml`, `README.md`) — added in the *literal last commit* of the project so far. A complete, polished, hand-rolled FastAPI implementation of the `/token` + `/health` contract, with a genuinely good README (quick start, adapting-to-your-auth-mechanism recipes, production checklist). **It does not import `proxy_hopper_token_server` at all** — it reimplements the wire contract from scratch, and still doesn't (deliberately — see its module docstring, and `pyproject.toml`'s comment) so the Docker/Compose deployment stays dependency-free. Good as a "roll your own, no library dependency" reference.
2. **`examples/token-server/auckland_council.py`** — a real scraper example demonstrating the *intended* pattern: using the actual `proxy_hopper_token_server` library (`TokenProvider`, `ProxyHopperClient` for IP-pinned token acquisition, a session cookie carried in the cursor). **Fixed and verified 2026-08-02**: its docstring now points at the correct module path (`auckland_council:provider`, not `examples.token_server.auckland_council:provider`), and `examples/token-server/pyproject.toml` gained a `dev` extra that installs `proxy-hopper-token-server` as a local editable path dependency, so `cd examples/token-server && uv sync --extra dev && uv run ph-token-server start auckland_council:provider` now actually resolves and runs (confirmed by import + CLI-resolve checks; the provider itself still needs a live Proxy Hopper instance and network access to the real Auckland Council site to fully exercise). Good as a "use the library, get IP-pinned acquisition for free" reference.
3. **`examples/docker-compose/token-server/`** — a generic, deployment-topology-only placeholder (`image: your-registry/your-token-server:latest`). No real server code by design. **Fixed 2026-08-02**: added a pointer at the top of its README clarifying it's a topology template, not a runnable server, and directing readers to `examples/token-server/` (runnable) and the `proxy-hopper-token-server` library.
