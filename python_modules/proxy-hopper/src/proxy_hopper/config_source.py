"""FileConfigSource — deterministic multi-file provider/pool/target merge.

Pure function, no I/O side effects beyond reading files. See
CONFIG_RECONCILER_SCOPE.md §4 for the full design rationale; this module
implements it directly.

Full re-scan and re-merge every call
-------------------------------------
There is no "patch just the file that changed" code path. ``scan_config_source``
always walks every matching file and rebuilds the merged result from scratch.
This is deliberate: a single merge function is the only thing that ever
decides what the config *should* be, so a cold boot and a live poll cycle
cannot drift from each other by construction.

Discovery order
---------------
Recursively walk the root, collect every ``*.yaml``/``*.yml`` file, sort the
matches by their root-relative POSIX path (forward slashes, plain
lexicographic string sort — not locale-aware, not filesystem-order-dependent).
If the root itself is a single file (the pre-existing single-file deployment
shape), that one file is the whole "scan" — no directory walk.

Merge semantics
---------------
Walk the sorted file list in order. For each ``(entity_type, name)`` parsed
out of a file, the first file in sort order to define that name wins; every
later file (or later same-file entry — treated identically, see below)
defining the same name is a *shadowed duplicate*: logged as a warning, never
applied, never fatal. A file that fails to parse is skipped and logged as an
error; it contributes nothing, but does not abort merging the rest.

One deliberate deviation from single-file ``load_config`` today: a duplicate
provider/pool name *within one file* currently raises ``ValueError`` there.
Here it goes through the same shadowed-duplicate warning path as a
cross-file duplicate, on the reasoning in §4 of the scope doc: precedence is
fully deterministic either way, so there's nothing to *fail* over — the
resolution is unambiguous whether the duplicate was discovered within one
file or across two. This only changes behavior for YAML that was already
relying on an unchecked latent ambiguity (two same-named entries in one
file); normal single-file configs are unaffected.

What this module does NOT do
-----------------------------
No cross-file *referential* validation (e.g. "this pool references a
provider that doesn't exist anywhere") — that's out of scope per §2 of the
doc, deferred to the existing lenient warn-and-skip cascade code
(``repository.py``'s ``_resolve_pool_ips``). Consequently this module never
constructs ``TargetConfig`` objects at all: that requires a non-empty
``resolved_ips``, which requires resolving pool → provider references across
the *entire* merged config, which is squarely Phase 4's job
(``ProxyRepository.reconcile()``). Target entries here are returned as
``MergedTargetSpec`` — normalised kwargs plus an unresolved ``pool_ref``
string — deliberately not yet a validated ``TargetConfig``. This also means
target-level validation (regex syntax, numeric field types, etc.) does not
happen here; it happens when Phase 4 constructs the real ``TargetConfig``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

import yaml

from .config import BasicAuth, IpPool, ProxyProvider
from .config import _normalise_pool_to_model, _normalise_provider, _normalise_target

_YAML_SUFFIXES = (".yaml", ".yml")

T = TypeVar("T")


@dataclass(frozen=True)
class MergedProvider:
    provider: ProxyProvider
    source_file: str


@dataclass(frozen=True)
class MergedPool:
    pool: IpPool
    source_file: str


@dataclass(frozen=True)
class MergedTargetSpec:
    """A target definition merged from file(s), not yet resolved against a pool.

    ``fields`` holds normalised kwargs suitable for
    ``TargetConfig(pool_name=pool_ref, resolved_ips=..., **fields)`` once
    Phase 4 has computed ``resolved_ips`` — it deliberately excludes
    ``pool_name``/``resolved_ips`` themselves, mirroring how ``load_config``
    pops the pool reference out before constructing ``TargetConfig``.
    """

    fields: dict
    pool_ref: str
    source_file: str


@dataclass(frozen=True)
class MergedFileConfig:
    """The result of merging every provider/pool/target across a file source."""

    providers: list[MergedProvider]
    pools: list[MergedPool]
    target_specs: list[MergedTargetSpec]

    @property
    def is_empty(self) -> bool:
        return not (self.providers or self.pools or self.target_specs)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _discover_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    matches = [
        p for p in root.rglob("*")
        if p.is_file() and p.suffix in _YAML_SUFFIXES
    ]
    matches.sort(key=lambda p: p.relative_to(root).as_posix())
    return matches


def _display_path(root: Path, file: Path) -> str:
    if root.is_file():
        return root.name
    return file.relative_to(root).as_posix()


# ---------------------------------------------------------------------------
# Per-file parsing — reuses the same normalisation helpers load_config uses
# ---------------------------------------------------------------------------

def _parse_providers_from_raw(raw: dict) -> list[ProxyProvider]:
    providers = []
    for p_raw in raw.get("proxyProviders", []):
        normalised = _normalise_provider(p_raw)
        if "auth" in normalised and isinstance(normalised["auth"], dict):
            normalised["auth"] = BasicAuth(**normalised["auth"])
        normalised.setdefault("static", True)
        providers.append(ProxyProvider(**normalised))
    return providers


def _parse_pools_from_raw(raw: dict) -> list[IpPool]:
    pools = []
    for pool_raw in raw.get("ipPools", []):
        pool = _normalise_pool_to_model(pool_raw)
        if not pool.name:
            raise ValueError("ipPool entry is missing a 'name' field")
        pools.append(pool)
    return pools


def _parse_target_specs_from_raw(raw: dict) -> list[tuple[dict, str]]:
    specs = []
    for t_raw in raw.get("targets", []):
        normalised = _normalise_target(t_raw)
        if not normalised.get("name"):
            raise ValueError("target entry is missing a 'name' field")
        target_name = normalised["name"]
        pool_ref = normalised.pop("ip_pool", None) or normalised.pop("pool_name", None)
        if pool_ref is None:
            raise ValueError(
                f"Target '{target_name}' must specify 'ipPool' referencing an ipPool. "
                "Inline ipList is not supported."
            )
        normalised.setdefault("static", True)
        specs.append((normalised, pool_ref))
    return specs


# ---------------------------------------------------------------------------
# Merge — first-file-in-sort-order wins, later duplicates are warnings
# ---------------------------------------------------------------------------

class _Merger(Generic[T]):
    """Accumulates (name -> (entity, source_file)) with first-wins precedence."""

    def __init__(self, entity_label: str) -> None:
        self.entity_label = entity_label
        self.merged: dict[str, tuple[T, str]] = {}
        self.warnings: list[str] = []

    def add(self, name: str, entity: T, source_file: str) -> None:
        if name in self.merged:
            _, winner_file = self.merged[name]
            self.warnings.append(
                f"{self.entity_label} '{name}' already defined in '{winner_file}', "
                f"ignoring redefinition in '{source_file}'"
            )
            return
        self.merged[name] = (entity, source_file)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def scan_config_source(root: Path | str) -> tuple[MergedFileConfig, list[str], list[str]]:
    """Scan *root* (a file or a directory) and return a deterministic merge.

    Returns ``(merged_config, warnings, errors)``. Never raises for
    per-file problems (malformed YAML, a bad entity) -- those become entries
    in ``errors`` and that file's contribution is skipped. Raises only if
    *root* itself does not exist, since that's a deployment misconfiguration
    rather than a file-content problem.

    An empty ``merged_config`` with an empty ``errors`` list means "nothing
    configured" (e.g. true first boot). An empty ``merged_config`` with a
    non-empty ``errors`` list means every file failed to parse -- Phase 4's
    empty-result guard (§4) should treat these two cases differently.
    """
    root = Path(root)
    if not root.exists():
        raise FileNotFoundError(f"config source path does not exist: {root}")

    files = _discover_files(root)

    errors: list[str] = []
    providers = _Merger[ProxyProvider]("provider")
    pools = _Merger[IpPool]("ipPool")
    targets = _Merger[tuple[dict, str]]("target")

    for file in files:
        display_path = _display_path(root, file)
        try:
            with open(file) as fh:
                raw = yaml.safe_load(fh) or {}
            file_providers = _parse_providers_from_raw(raw)
            file_pools = _parse_pools_from_raw(raw)
            file_target_specs = _parse_target_specs_from_raw(raw)
        except Exception as exc:
            errors.append(f"{display_path}: failed to parse — {exc}")
            continue

        for provider in file_providers:
            providers.add(provider.name, provider, display_path)
        for pool in file_pools:
            pools.add(pool.name, pool, display_path)
        for fields, pool_ref in file_target_specs:
            targets.add(fields["name"], (fields, pool_ref), display_path)

    merged_config = MergedFileConfig(
        providers=[
            MergedProvider(provider=p, source_file=f) for p, f in providers.merged.values()
        ],
        pools=[
            MergedPool(pool=p, source_file=f) for p, f in pools.merged.values()
        ],
        target_specs=[
            MergedTargetSpec(fields=fields, pool_ref=pool_ref, source_file=f)
            for (fields, pool_ref), f in targets.merged.values()
        ],
    )
    warnings = providers.warnings + pools.warnings + targets.warnings
    return merged_config, warnings, errors
