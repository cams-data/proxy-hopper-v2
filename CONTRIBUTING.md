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

### Pre-release (merge to `next`)

1. Open a PR from your feature branch into `next`
2. CI runs the full test matrix — all three suites × three Python versions
3. Merge the PR
4. `release-please` opens or updates a **Release PR** against `next` with a bumped version and generated changelog
5. When that Release PR is merged, GitHub Actions builds the wheels and publishes a **pre-release** (`0.2.0-pre.1`) to GitHub Releases

### Production release (merge `next` into `main`)

1. Open a PR from `next` into `main`
2. CI runs again
3. Merge the PR
4. `release-please` opens or updates a **Release PR** against `main`
5. When that Release PR is merged, GitHub Actions builds the wheels and publishes a **full release** (`0.2.0`) to GitHub Releases

### Installing from a GitHub Release

```bash
# Latest production release
pip install https://github.com/your-org/proxy-hopper/releases/latest/download/proxy_hopper-0.2.0-py3-none-any.whl

# Specific pre-release
pip install https://github.com/your-org/proxy-hopper/releases/download/v0.2.0-pre.1/proxy_hopper-0.2.0.pre.1-py3-none-any.whl
```

## Running tests locally

```bash
# Core package
cd python_modules/proxy-hopper && uv run pytest

# Redis backend
cd python_modules/proxy-hopper-redis && uv run pytest

# Cross-backend contract tests (both backends, no Redis server required)
cd python_modules/tests && uv run pytest
```
