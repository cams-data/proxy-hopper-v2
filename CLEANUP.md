# CLEANUP.md

Things that exist in this repo (and the workspace around it) purely as leftover confusion — dead code, stale docs, superseded examples, orphaned config fields. Nothing here has been deleted automatically; this is a documented list for a deliberate pass. Cross-reference [TODO.md](TODO.md) for the fixes that aren't pure deletions.

## A. Stale references to the two dropped integration modes — done (2026-08-02)

Proxy Hopper originally supported three ways to send it traffic: HTTP proxy mode, CONNECT tunnel mode, and forwarding mode. Commit `2f62d55` (`feat!: drop HTTP proxy and CONNECT tunnel modes, add X-Proxy-Hopper-Tag/Retries headers`) removed the first two — forwarding mode (`X-Proxy-Hopper-Target` header) is the only one the code implements today.

Removed the "HTTP proxy mode" snippets and the dead `modes:`/`connect_tunnel` config block from `local-backend/README.md`, `local-redis/README.md`, `auth-api-keys/README.md`, and `auth-oidc/README.md`. `examples/token-server/README.md` had one more instance not originally listed here, also fixed.

This turned into a bigger fix than expected: `local-backend`'s and `local-redis`'s inline config snippets (and by extension, checking further, their actual `config.yaml` files, plus `auth-api-keys/config.yaml`, `auth-oidc/config.yaml`, and `examples/token-server/config.yaml`) put `ipList` directly on a target — a shape `loader.py` now hard-rejects (`"Inline ipList is no longer supported — declare IPs in a proxyProvider."`). Verified with `proxy-hopper validate --config` against every example `config.yaml` in the repo: 4 of 6 crashed on load. Fixed all 4 broken config files (added `proxyProviders`/`ipPools`, targets now reference `ipPool:`) and the READMEs' matching inline snippets/field tables, then re-validated all six clean. See TODO.md item 6.

**(Root)** The old top-level `README.md`'s three-mode comparison table was already replaced before this pass — no action needed, still flagging so a stray `git stash`/old-branch merge doesn't silently reintroduce it.

**Not code, still a decision**: the workspace-root file `Proxy Hopper - Proxy Hopper.html` (a saved browser snapshot of an old docs-site landing page) still describes the three-mode story. It's inert — not part of any build — but someone opening it cold could mistake it for current. Recommend deleting it, or moving it into `archive/` alongside the retired `ROADMAP.md`. Left as-is since it's outside git and irreversible to remove without a backup; ask before deleting.

## B. Dead code — done (2026-08-02)

- **`python_modules/proxy-hopper/src/proxy_hopper/graphql/`** — deleted `queries.py`, `mutations.py`, `types.py`, `inputs.py`, `context.py`, and `_auth.py` (a 6th file this list originally missed — a helper only imported by `queries.py`/`mutations.py`, part of the same dead cluster). Kept the `__init__.py` `ImportError` redirect stub. Confirmed via `grep` that nothing outside the directory imported any of the six.
- **`python_modules/proxy-hopper/src/proxy_hopper/identity/store.py`** — deleted. Confirmed unimported.
- **`python_modules/proxy-hopper/src/proxy_hopper/backend/base.py`**'s `IPPoolBackend = Backend` and **`backend/memory.py`**'s `MemoryIPPoolBackend = MemoryBackend` — deleted, plus their `backend/__init__.py` exports. Confirmed zero `.py` usage outside their own definitions.
- **`python_modules/proxy-hopper-redis/src/proxy_hopper_redis/backend.py`**'s `RedisIPPoolBackend = RedisBackend` — deleted, plus its `__init__.py` export. Same confirmation.
- **`python_modules/proxy-hopper/src/proxy_hopper/models.py`**'s `IPState` and `ReturnReason` — deleted, along with the now-unused `Enum`/`auto` import. Confirmed zero constructors anywhere.

None of the four aliases had `.py` usage outside their own definitions, but 3 READMEs (`proxy-hopper-redis/README.md`, `proxy-hopper-testserver/README.md`, `python_modules/tests/README.md`) documented them as the primary API with runnable-looking examples — fixed all three to use the real names. `proxy-hopper-redis/README.md`'s "Programmatic use" example was separately stale (constructed `TargetConfig(ip_list=...)`, imported a since-removed `IPPool` class) — rewrote it around the `Backend` primitives instead. `python_modules/tests/README.md` had the same vintage of staleness throughout — rewrote to match current fixture/class names (`IPPoolStore`, `IdentityQueue`) and added the previously-undocumented `test_app_metrics_contract.py`.

## C. Stale docs / config

- **`ROADMAP.md`** (workspace root) — retired 2026-08-02 per project owner decision (all three items — auth, GraphQL, front-end — have shipped). Moved to `../archive/ROADMAP.md` with a header note; not deleted outright since the workspace root isn't under version control and an outright delete would be unrecoverable.
- **`CONTRIBUTING.md`** — **done (2026-08-02).** Rewritten to describe the actual `git-cliff` + `github-tag-action` pipeline. See TODO.md item 8.
- **Remote branch `origin/release-please--branches--next`** — leftover from the old release-please setup. Still dangling, still needs someone with push access to confirm before deletion — not done here, that's a shared/irreversible action outside this review's scope.
- **`python_modules/proxy-hopper/README.md` line 17** — **done (2026-08-02).** Now says "Requires Python 3.12+". See TODO.md item 7.

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
