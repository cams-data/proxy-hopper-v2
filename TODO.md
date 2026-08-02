# TODO

Living task list, roughly prioritized. Written 2026-08-02 from a full project review (`../PROJECT_REVIEW.md`) plus the project owner's own corrections. See [CLEANUP.md](CLEANUP.md) for the parallel list of things to *remove* rather than fix.

## P0 — blocking the current feature branch

1. ~~**Fix the broken import in `proxy-hopper-token-server`.**~~ **Done (2026-08-02).**
   `_internal/app.py` imported `._handler`; the module is `handler.py`. Fixed. While in there, found and fixed a second, more serious bug in the same package: `client.py`'s `ProxyHopperClient` sent requests using aiohttp's `proxy=` kwarg (classic HTTP-proxy/CONNECT semantics) — a mode Proxy Hopper's core dropped entirely. It now builds a proper forwarding-mode request (`X-Proxy-Hopper-Target` + `X-ProxyHopper-Force-IP`, sent directly to the proxy's own address) and returns a `ProxyHopperResponse` with the body already buffered (the old code returned a live `aiohttp.ClientResponse` after closing the session that produced it, which was unsafe to read from). Added `python_modules/proxy-hopper-token-server/README.md` (didn't exist before), 11 new tests (`test_client.py`, `test_cli.py`, plus timeout/validation/proxy_url cases in `test_server.py` — 22 total, all passing), a public `TokenServer.build_app()` for `uvicorn module:app`-style hosting, and fixed `examples/token-server/auckland_council.py`'s docstring (wrong module path) plus wired it up as a real, installable local dependency via `examples/token-server/pyproject.toml`'s new `dev` extra (`uv sync --extra dev` then `uv run ph-token-server start auckland_council:provider` now actually resolves and runs). The Docker-deployed `examples/token-server/token_server/` demo is untouched and still has zero dependency on the library.

2. ~~**Finish and merge `feat/token-server` into `next`.**~~ **Done (2026-08-02).** Full local matrix run before merge: 611 passed, 11 skipped (`real_redis`-marker, no live Redis in this environment), 0 failed. Merged via [PR #12](https://github.com/cams-data/proxy-hopper-v2/pull/12), tagged `v0.19.0-pre.0`.

## P1 — real bugs found during review

3. ~~**`admin-ui` queries a GraphQL field the backend doesn't define.**~~ **Done (2026-08-02).**
   Built the two-tier `targetMetrics` resolver as planned. New `python_modules/proxy-hopper/src/proxy_hopper/app_metrics.py` (`AppMetricsStore`) records total/success/failed counts + rolling avg latency + last-request time into the existing `Backend`, written from `target_manager.py`'s two request-completion `finally` blocks (same call site/semantics as the existing Prometheus instrumentation) — this needed a new `Backend.counter_increment_by` primitive (`base.py` default fallback + real `MemoryBackend`/`RedisBackend` overrides, INCRBY for Redis). New `server.prometheusUrl` config field (+ `--prometheus-url` CLI flag): when set, in-process recording is skipped entirely and `proxy_hopper_webserver/prometheus_query.py` queries Prometheus server-side (4 parallel PromQL instant queries) instead — never exposes Prometheus to the browser directly, keeps the existing auth/RBAC boundary intact. Both tiers feed the same `targetMetrics` GraphQL field via `TargetMetricsType`/`target_metrics_to_gql`; the admin-ui's existing query needed **zero changes** — it already asked for exactly this shape. Turned out embedded admin (item 14) wasn't strictly a blocker after all: any topology where admin shares a backend with the proxy works (Redis, any topology; memory, only when embedded) — the two features compose rather than one gating the other. Tests: `test_app_metrics_contract.py` (24, both backends), 3 new in `test_target_manager.py`, 10 new in webserver's `test_graphql.py`/`test_prometheus_query.py`.

4. **Decide the `static`/`mutable` semantics and fix the asymmetry.**
   In `python_modules/proxy-hopper/src/proxy_hopper/repository.py`, `update_target`/`update_provider`/`update_pool` check both `existing.static` and `existing.mutable` (raising if either blocks the write). But `remove_target`/`remove_provider`/`remove_pool` only check `static` — so a `static=False, mutable=False` entity currently can't be *edited* through the API but *can* be deleted through it. Confirm whether that's intentional (the project owner wasn't sure either — "is there code using both?" — yes: see `repository.py` lines ~215–320). If not intentional, add the same `mutable` check to the three remove paths.

## P2 — docs and hygiene

5. **Update the docs site (`../proxy-hopper-docs/`) for the token-server feature** once it lands on `next` — per the project owner, the docs site is meant to be updated after each feature ships to `next`, and right now it has zero mentions of "token" despite the feature being complete and Helm-supported. This is expected staleness until (2) lands, not a bug — but don't forget it once it does.

6. **Fix the stale example READMEs that still show removed integration modes.** These will actively mislead someone following them — the curl/python snippets show a "HTTP proxy mode" that no longer exists, and two of them show a `modes:`/`connect_tunnel` config field that isn't in `config/models.py` at all anymore:
   - `examples/docker-compose/local-backend/README.md`
   - `examples/docker-compose/local-redis/README.md`
   - `examples/docker-compose/auth-api-keys/README.md`
   - `examples/docker-compose/auth-oidc/README.md`

   See [CLEANUP.md](CLEANUP.md) for the exact line ranges.

7. **Fix the stale Python version claim.** `python_modules/proxy-hopper/README.md` line 17 says "Requires Python 3.11+"; `pyproject.toml` requires `>=3.12` (dropped in commit `68d1038`). One-line fix.

8. **Rewrite `CONTRIBUTING.md`'s release-flow section.** It currently describes a `release-please` Release-PR workflow. The actual pipeline (`.github/workflows/next.yml`, `release.yml`) uses `git-cliff` + `github-tag-action` directly, tagging straight on push with no Release-PR step. There's a dangling remote branch `origin/release-please--branches--next` left over from the old approach — safe to delete once someone with push access confirms nobody needs it.

9. **Delete the dead code listed in [CLEANUP.md](CLEANUP.md)** — stale GraphQL duplicates in core, the `identity/store.py` stub, deprecated backend aliases. Low risk, pure clutter reduction.

10. ~~**Sort out the token-server examples.**~~ **Done (2026-08-02).** `auckland_council.py`'s run-path docstring is fixed and it's now a real, resolvable dependency (see item 1). The generic `examples/docker-compose/token-server/` placeholder now points readers at the real, runnable `examples/token-server/` and the library.

## Housekeeping / no rush

11. **Delete already-merged local branches**: `drop-legacy-modes`, `feat/authentication`, `feat/code-tidy`, `feat/ip-pool-entity` are all confirmed ancestors of `next` (verified via `git merge-base --is-ancestor`). Safe to delete locally; not done automatically here since branch deletion wasn't explicitly requested.

12. **Move the project under the Spatialytics GitHub org** when ready. Until then, don't assume `cams-data` URLs (image paths, docs links, badges) are permanent — grep before hardcoding new references to them.

13. Once the project is in production, revisit whether `main` should track `next` more closely — current bursty release cadence is a deliberate pre-production choice, not a problem to fix yet.

14. ~~**Embedded single-process admin mode (`proxy-hopper run --admin`).**~~ **Done (2026-08-02).** Found while scoping the metrics feature (item 3): `proxy-hopper admin` is *always* a separate process from `proxy-hopper run`, and with `backend: memory` each process builds its own private `MemoryBackend()` — there's no shared memory across processes, so a separately-run admin server with the memory backend only ever shows YAML-seeded state, never live runtime state (pool rotation, quarantine, admin-made edits). Considered leader election + REST between admin and a "leader" proxy node; rejected — the memory backend can only ever have one node by construction (multi-replica already requires Redis), so there's nothing to elect. Instead: `proxy-hopper run --admin` now runs the admin FastAPI app in the same process/event loop as the proxy, sharing one `repo`/`event_bus`/`backend` object directly — no IPC needed since there's no process boundary. New CLI flags `--admin`/`--admin-host`/`--admin-port` (the underlying `server.admin`/`admin_host`/`admin_port` config fields already existed, unused, in `ServerConfig` — this was apparently scaffolded once and never wired up). Redis-backed deployments keep running admin as a genuinely separate process/pod as before — that case already works correctly and doesn't need this. Fixed the standalone `admin` command's misleading docstring while in there ("Connects to the same backend as the proxy runners" was only true for Redis). Merged via [PR #13](https://github.com/cams-data/proxy-hopper-v2/pull/13), tagged `v0.20.0-pre.0`.

## Housekeeping (newly found)

15. **`python_modules/proxy-hopper/README.md`'s Server fields table was missing `admin`/`adminHost`/`adminPort` rows** — item 14 added the CLI flags and a prose "Admin API" section but never updated this table. Fixed alongside item 3/14's docs pass (2026-08-02), along with adding the new `prometheusUrl` row.
