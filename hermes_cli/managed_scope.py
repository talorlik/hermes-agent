"""Managed scope — IT-pushed, user-immutable config & env layer.

DISTINCT from ``hermes_cli.config.is_managed()`` / ``HERMES_MANAGED`` (a coarse package-manager
write-lock that blocks all mutation); this layer injects specific immutable values. The two are
independent and may coexist. v1 enforcement is filesystem permissions only (see
``docs/design/managed-scope.md`` §7); ``get_managed_dir()`` is the single seam for adding
macOS / Windows native locations later.
"""

from __future__ import annotations

import copy
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


# Stale-module bridge: this module binds ``utils.file_signature`` at import time, so a fresh
# import in a post-pull updater process (pre-handoff purge keeps root modules cached) dies
# unless the stale ``utils`` is dropped first. See hermes_cli.stale_modules.
from hermes_cli.stale_modules import drop_stale_root_modules

drop_stale_root_modules()

from utils import fast_safe_load, file_signature

logger = logging.getLogger(__name__)

# POSIX default. Other-platform locations belong ONLY inside get_managed_dir().
_DEFAULT_MANAGED_DIR = Path("/etc/hermes")

_PARSE_FAILED = (
    "managed scope: failed to parse %s: %s — IGNORING this managed file. "
    "Admin policy from this file is NOT being applied. Fix and restart."
)


@dataclass(frozen=True)
class _LayerShape:
    """What one config layer looked like to the read that produced its mapping, before any
    normalization, expansion or overlay. Frozen, and built only from that read's own result, so
    a strict reader judges the file as written from the cache entry that serves the mapping."""

    # None for a mapping root (and for no file at all); otherwise what the root was instead.
    root: Optional[str] = None
    # Top-level keys whose value is not a mapping, each with what the value was.
    non_mapping: Tuple[Tuple[Any, str], ...] = ()
    # None when the file was read and parsed (or there is none); otherwise what failed.
    unreadable: Optional[str] = None


def _layer_shape(loaded: Any) -> _LayerShape:
    if not isinstance(loaded, dict):
        return _LayerShape(
            root="null (or an empty file)" if loaded is None else type(loaded).__name__
        )
    return _LayerShape(
        non_mapping=tuple(
            (key, "null" if value is None else type(value).__name__)
            for key, value in loaded.items()
            if not isinstance(value, dict)
        )
    )


@dataclass(frozen=True)
class _ManagedSelection:
    """The managed ``config.yaml`` one read is about, resolved exactly once for that read: the
    file of the scope ``get_managed_dir()`` selected, and what ``stat`` found there. Equal
    selections name the same file in the same state, so this is the identity a cache keys the
    managed layer on; a bare stat signature cannot tell one scope's file from another's."""

    # None when no managed scope is selected.
    path: Optional[Path] = None
    # ``utils.file_signature`` of the file; None when there is no file (or no scope).
    signature: Optional[Tuple[int, int, int, int]] = None
    # Set when ``stat`` failed for any reason but the file not existing: present, state unknown.
    stat_error: Optional[str] = None


@dataclass(frozen=True)
class _ManagedConfig:
    """One read of the managed ``config.yaml``: which file (``selection``), what it looked like
    as written (``shape``) and the mapping it yields, all from the same parse and held in the
    same cache entry. The mapping is ``{}`` for no file and, fail-open, for a file that is not a
    usable mapping; only ``shape`` tells those apart. Frozen; the mapping is private and only
    ever leaves as a copy."""

    selection: _ManagedSelection
    shape: _LayerShape = _LayerShape()
    _mapping: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def mapping(self) -> dict:
        return copy.deepcopy(self._mapping)

    def overlay(self, config: dict) -> Optional[dict]:
        """``config`` with this read's own mapping merged over it (``_merge_managed``). ``None``,
        logged, when the merge failed: ``config`` then has to stand without the managed layer."""
        try:
            return _merge_managed(config, self.mapping())
        except Exception:  # noqa: BLE001 — the caller decides whether a missing layer is fatal
            logger.warning(
                "managed scope: failed to apply config overlay", exc_info=True
            )
            return None


_CACHE_LOCK = threading.Lock()
# path_key -> the last parsed read of that managed config.yaml
_CONFIG_CACHE: Dict[str, _ManagedConfig] = {}
# path_key -> (*file_signature, parsed)
_ENV_CACHE: Dict[str, tuple] = {}


def _under_pytest() -> bool:
    """True inside the test suite: ignore the system ``/etc/hermes`` so a real managed scope on a
    dev/CI box can't leak policy into the suite. An explicit ``HERMES_MANAGED_DIR`` still wins."""
    return "PYTEST_CURRENT_TEST" in os.environ


def get_managed_dir() -> Optional[Path]:
    """Resolve the managed-scope directory, or None when no scope is present.

    Priority: ``$HERMES_MANAGED_DIR`` (IT-only bootstrap override; never persisted to any .env;
    honored only when non-empty AND the directory exists), then ``/etc/hermes`` when it exists.
    A missing directory resolves to None — the common case, so it must be cheap + side-effect-free.
    """
    override = os.environ.get("HERMES_MANAGED_DIR", "").strip()
    if override:
        p = Path(override)
    elif _under_pytest():
        return None
    else:
        p = _DEFAULT_MANAGED_DIR
    return p if p.is_dir() else None


def invalidate_managed_cache() -> None:
    """Drop cached managed config/env. For tests and post-edit reloads."""
    with _CACHE_LOCK:
        _CONFIG_CACHE.clear()
        _ENV_CACHE.clear()


def _cached_read(path: Path, cache: Dict[str, tuple], parse):
    """Shared stat-signature-keyed read; returns a deepcopy of the parsed value.

    ``None`` when the file is absent or fails to parse (fail-open). A parse failure is logged
    LOUDLY — the admin needs to know their policy isn't applied — but never raises, so a malformed
    managed file can't brick startup.
    """
    try:
        st = path.stat()
    except OSError:
        return None  # absent
    key = file_signature(st)
    path_key = str(path)
    with _CACHE_LOCK:
        hit = cache.get(path_key)
        if hit is not None and hit[: len(key)] == key:
            return copy.deepcopy(hit[len(key)])
    try:
        parsed = parse(path)
    except Exception as exc:  # noqa: BLE001 — fail-open, but LOUD
        logger.warning(_PARSE_FAILED, path, exc)
        return None
    with _CACHE_LOCK:
        cache[path_key] = (*key, copy.deepcopy(parsed))
    return parsed


def _load_managed_file(name: str, cache: Dict[str, tuple], parse) -> dict:
    managed_dir = get_managed_dir()
    if managed_dir is None:
        return {}
    parsed = _cached_read(managed_dir / name, cache, parse)
    return parsed if isinstance(parsed, dict) else {}


def _select_managed_config() -> _ManagedSelection:
    """Resolve the managed scope and stat its ``config.yaml``, once."""
    managed_dir = get_managed_dir()
    if managed_dir is None:
        return _ManagedSelection()
    path = managed_dir / "config.yaml"
    try:
        st = path.stat()
    except FileNotFoundError:
        return _ManagedSelection(path)
    except OSError as exc:
        return _ManagedSelection(path, stat_error=type(exc).__name__)
    return _ManagedSelection(path, file_signature(st))


def _read_managed_config(
    selection: Optional[_ManagedSelection] = None,
) -> _ManagedConfig:
    """The managed ``config.yaml`` as one read: its mapping and its shape as written, from the
    same parse. ``selection`` names the file to read; omitted, it is resolved here.

    Never raises (fail-open): no scope and no file are a valid empty layer, and a file that cannot
    be read or parsed, or whose root is not a mapping, yields an empty mapping whose ``shape``
    records why. A parsed file is cached under its selection and served from that entry until the
    selection changes. A failed read is logged LOUDLY — the admin needs to know their policy isn't
    applied — and never cached, so a transient failure cannot pin "no policy".
    """
    if selection is None:
        selection = _select_managed_config()
    if selection.stat_error is not None:
        return _ManagedConfig(selection, _LayerShape(unreadable=selection.stat_error))
    if selection.path is None or selection.signature is None:
        return _ManagedConfig(selection)  # absent
    path_key = str(selection.path)
    with _CACHE_LOCK:
        hit = _CONFIG_CACHE.get(path_key)
        if hit is not None and hit.selection == selection:
            return hit
    try:
        loaded = fast_safe_load(selection.path.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001 — fail-open, but LOUD
        logger.warning(_PARSE_FAILED, selection.path, exc)
        return _ManagedConfig(selection, _LayerShape(unreadable=type(exc).__name__))
    managed = _ManagedConfig(
        selection, _layer_shape(loaded), loaded if isinstance(loaded, dict) else {}
    )
    with _CACHE_LOCK:
        _CONFIG_CACHE[path_key] = managed
    return managed


def load_managed_config() -> dict:
    """Parsed managed config.yaml, or {} when absent/malformed (fail-open)."""
    return _read_managed_config().mapping()


def load_managed_env() -> Dict[str, str]:
    """Parsed managed .env (KEY=VALUE), or {} when absent (fail-open)."""
    return _load_managed_file(".env", _ENV_CACHE, _parse_managed_env)


def _parse_managed_env(path: Path) -> Dict[str, str]:
    from agent.secret_scope import load_env_file

    path.read_text(
        encoding="utf-8-sig"
    )  # load_env_file swallows decode errors; an admin file must fail LOUD
    return load_env_file(path)


def apply_managed_overlay(config: dict) -> dict:
    """Overlay administrator-pinned config values on top of an already-built dict.

    ``${VAR}`` refs in the managed config expand against the PROCESS env only, so a user cannot
    shadow a managed literal via a ref they control; a bare root ``model: x/y`` string is promoted
    to ``model.default`` so it can't clobber the dict shape callers expect; managed values
    deep-merge ON TOP per leaf while sibling keys stay user-controlled. Fail-open: returns
    ``config`` unchanged when no scope is present or on any error. Mutates and returns ``config``.
    """
    try:
        return _merge_managed(config, load_managed_config())
    except Exception:  # noqa: BLE001 — overlay must never break a caller
        logger.warning("managed scope: failed to apply config overlay", exc_info=True)
        return config


def _merge_managed(config: dict, managed: dict) -> dict:
    """The overlay itself (see ``apply_managed_overlay``): ``managed`` expanded, normalized and
    deep-merged over ``config``. Raises on failure; mutates and returns ``config``."""
    if not managed:
        return config
    # Imported lazily to avoid an import cycle (config imports managed_scope).
    from hermes_cli.config import (
        _deep_merge,
        _expand_env_vars,
        _normalize_root_model_keys,
    )

    managed_expanded = _normalize_root_model_keys(_expand_env_vars(managed))
    # _normalize_root_model_keys only promotes the string when root provider/base_url
    # keys exist to migrate; handle the bare case here (matches cli.py) so _deep_merge
    # never replaces the caller's ``model`` dict with a string.
    if isinstance(managed_expanded.get("model"), str):
        managed_expanded = dict(managed_expanded)
        managed_expanded["model"] = {"default": managed_expanded["model"]}
    return _deep_merge(config, managed_expanded)


def _flatten_keys(d: dict, prefix: str = "") -> set:
    keys: set = set()
    for k, v in d.items():
        dotted = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict) and v:
            keys |= _flatten_keys(v, dotted)
        else:
            keys.add(dotted)
    return keys


def managed_config_keys() -> set:
    """Dotted leaf keys pinned by the managed config (e.g. {'model.default'})."""
    return _flatten_keys(load_managed_config())


def is_key_managed(dotted_key: str) -> bool:
    """True if the exact dotted config key is pinned by the managed layer."""
    return dotted_key in managed_config_keys()


def is_env_managed(name: str) -> bool:
    """True if the env var name is pinned by the managed .env layer."""
    return name in load_managed_env()
