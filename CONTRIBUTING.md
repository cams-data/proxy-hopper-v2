# Contributing

## Branch model

```
feature/my-thing  ──► PR ──►  next  ──► PR ──►  main
                              (pre-release)      (release)
```

- `main` — production releases. Protected: PR + passing CI required.
- `next` — integration branch for the next release. Protected: PR + passing CI required.
- Feature branches — anything goes. Branch from `next`, not from `main`.

## Commit messages

This project uses [Conventional Commits](https://www.conventionalcommits.org/). Commit messages drive automatic versioning — no manual version bumps needed.

| Prefix | Effect | Example |
|---|---|---|
| `fix:` | Patch release `0.1.0 → 0.1.1` | `fix: handle empty quarantine list on sweep` |
| `feat:` | Minor release `0.1.0 → 0.2.0` | `feat: add per-IP request rate limiting` |
| `feat!:` or `BREAKING CHANGE:` in body | Major release `0.1.0 → 1.0.0` | `feat!: rename ipList to ip_list in config` |
| `docs:` | No version bump | `docs: add Kubernetes deployment example` |
| `chore:` | No version bump | `chore: update dependencies` |
| `refactor:` | No version bump | `refactor: extract sweep logic into helper` |
| `test:` | No version bump | `test: add concurrent pop_ip contract test` |
| `ci:` | No version bump | `ci: pin uv version in workflows` |
| `perf:` | Patch release | `perf: reduce quarantine sweep interval` |

**Scope is optional** but useful for the monorepo:

```
feat(redis): add connection retry with exponential backoff
fix(pool): prevent double-release when sweep races with record_failure
```

### Breaking changes

Add a `BREAKING CHANGE:` footer to trigger a major bump regardless of prefix:

```
feat: overhaul config schema

BREAKING CHANGE: `ipList` is now `ip_list` in config.yaml.
Update all config files before upgrading.
```

## Release flow

Releases are fully automated by CI, driven directly by Conventional Commits — there is no Release PR step and no manual version bump. Tagging, changelog generation, wheel/sdist builds, Docker images, and the Helm chart all happen in one workflow run triggered by a push to `next` or `main`. (This replaced an earlier `release-please`-based flow; if you see that name mentioned anywhere else, it's stale.)

### Pre-release (merge to `next`)

1. Open a PR from your feature branch into `next`
2. CI runs the full test matrix (three suites × three Python versions) — required to pass before merge
3. Merge the PR — the push to `next` triggers [`.github/workflows/next.yml`](.github/workflows/next.yml)
4. [`github-tag-action`](https://github.com/mathieudutour/github-tag-action) reads the Conventional Commits since the last tag and pushes a new pre-release tag directly (e.g. `v0.2.0-pre.1`) — no PR, no manual step. If nothing since the last tag warrants a version bump, this is a no-op and the rest of the workflow is skipped.
5. On a new tag: [`git-cliff`](https://git-cliff.org/) generates release notes, wheels/sdists are built for `proxy-hopper`, `proxy-hopper-redis`, and `proxy-hopper-webserver` (with the admin-ui bundled in), and a GitHub **pre-release** is published
6. Multi-arch (amd64 + arm64) Docker images are built and pushed to `ghcr.io`: `proxy-hopper:<version>` / `:<version>-redis`, plus floating `:preview` / `:preview-redis` tags
7. The Helm chart is version-bumped to match and pushed to `ghcr.io` as an OCI artifact

### Production release (merge `next` into `main`)

1. Open a PR from `next` into `main`
2. CI re-runs the full test matrix as part of [`.github/workflows/release.yml`](.github/workflows/release.yml)'s own `test` job — the tag step won't run if it fails
3. Merge the PR — the push to `main` triggers the rest of the workflow
4. `github-tag-action` pushes a new **stable** tag (e.g. `v0.2.0`, no `-pre` suffix)
5. `git-cliff` regenerates `CHANGELOG.md` and commits it straight back to `main` (`chore(changelog): ... [skip ci]`)
6. Wheels/sdists are built and a full GitHub release is published
7. Docker images are pushed with `:<version>` / `:<version>-redis`, plus floating `:latest` / `:latest-redis` tags
8. The Helm chart is packaged and pushed to `ghcr.io`

### Installing from a GitHub Release

```bash
# Latest production release
pip install https://github.com/cams-data/proxy-hopper-v2/releases/latest/download/proxy_hopper-0.2.0-py3-none-any.whl

# Specific pre-release
pip install https://github.com/cams-data/proxy-hopper-v2/releases/download/v0.2.0-pre.1/proxy_hopper-0.2.0.pre.1-py3-none-any.whl
```

Or use the published Docker images or Helm chart directly — see [python_modules/proxy-hopper/README.md](python_modules/proxy-hopper/README.md) and [charts/proxy-hopper/](charts/proxy-hopper/).

## Running tests locally

```bash
# Core package
cd python_modules/proxy-hopper && uv run pytest

# Redis backend
cd python_modules/proxy-hopper-redis && uv run pytest

# Cross-backend contract tests (both backends, no Redis server required)
cd python_modules/tests && uv run pytest
```
