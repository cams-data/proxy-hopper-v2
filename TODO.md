# TODO

Living task list, roughly prioritized. Written 2026-08-02 from a full project review (`../PROJECT_REVIEW.md`) plus the project owner's own corrections. See [CLEANUP.md](CLEANUP.md) for the parallel list of things to *remove* rather than fix.

## P0 — blocking the current feature branch

1. ~~**Fix the broken import in `proxy-hopper-token-server`.**~~ **Done (2026-08-02).**
   `_internal/app.py` imported `._handler`; the module is `handler.py`. Fixed. While in there, found and fixed a second, more serious bug in the same package: `client.py`'s `ProxyHopperClient` sent requests using aiohttp's `proxy=` kwarg (classic HTTP-proxy/CONNECT semantics) — a mode Proxy Hopper's core dropped entirely. It now builds a proper forwarding-mode request (`X-Proxy-Hopper-Target` + `X-ProxyHopper-Force-IP`, sent directly to the proxy's own address) and returns a `ProxyHopperResponse` with the body already buffered (the old code returned a live `aiohttp.ClientResponse` after closing the session that produced it, which was unsafe to read from). Added `python_modules/proxy-hopper-token-server/README.md` (didn't exist before), 11 new tests (`test_client.py`, `test_cli.py`, plus timeout/validation/proxy_url cases in `test_server.py` — 22 total, all passing), a public `TokenServer.build_app()` for `uvicorn module:app`-style hosting, and fixed `examples/token-server/auckland_council.py`'s docstring (wrong module path) plus wired it up as a real, installable local dependency via `examples/token-server/pyproject.toml`'s new `dev` extra (`uv sync --extra dev` then `uv run ph-token-server start auckland_council:provider` now actually resolves and runs). The Docker-deployed `examples/token-server/token_server/` demo is untouched and still has zero dependency on the library.

2. **Finish and merge `feat/token-server` into `next`.**
   Confirmed with the project owner: this branch is meant to be finished, not exploratory. The library itself is now fixed and tested (see item 1). Before merging, run the full test matrix (`ci.yml`'s scope: core, redis, webserver, contract, integration tests) — the token-server package's own suite passes as of this fix, but hasn't been run as part of the full matrix together with the rest.

## P1 — real bugs found during review

3. **`admin-ui` queries a GraphQL field the backend doesn't define.**
   `admin-ui/src/graphql/queries.ts`'s `TARGET_METRICS_QUERY` (used in `TargetsPage.tsx`) queries `targetMetrics(name: ...)`, but `proxy_hopper_webserver/graphql/queries.py`'s `Query` type has no `target_metrics` field/resolver. Either add the resolver (if the metric data is meant to be exposed) or remove the dead query from the UI.

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

10. ~~**Sort out the token-server examples.**~~ **Partly done (2026-08-02).** `auckland_council.py`'s run-path docstring is fixed and it's now a real, resolvable dependency (see item 1). Still open: the generic `examples/docker-compose/token-server/` placeholder doesn't yet point readers at the real, runnable `examples/token-server/` — see CLEANUP.md section E for the one-line fix.

## Housekeeping / no rush

11. **Delete already-merged local branches**: `drop-legacy-modes`, `feat/authentication`, `feat/code-tidy`, `feat/ip-pool-entity` are all confirmed ancestors of `next` (verified via `git merge-base --is-ancestor`). Safe to delete locally; not done automatically here since branch deletion wasn't explicitly requested.

12. **Move the project under the Spatialytics GitHub org** when ready. Until then, don't assume `cams-data` URLs (image paths, docs links, badges) are permanent — grep before hardcoding new references to them.

13. Once the project is in production, revisit whether `main` should track `next` more closely — current bursty release cadence is a deliberate pre-production choice, not a problem to fix yet.
