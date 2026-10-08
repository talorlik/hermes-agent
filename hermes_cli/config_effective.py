"""The effective USER config: config.yaml + managed overlay + ``${VAR}`` expansion, no defaults.

``load_config()`` merges ``DEFAULT_CONFIG`` first, which is wrong for readers that treat a
missing key as "unset" (the gateway's presence-sensitive env bridge, ``cfg == {}`` sentinels,
cron model pinning) — so nine surfaces used to hand-roll raw-read → overlay → expand in
differing orders and none of them replayed the model-key canonicalization or the last-known-good
recovery ``load_config()`` gained. This module is that one primitive.

Order matches ``_load_config_impl``: the user layer is expanded BEFORE the managed overlay so a
managed ``${VAR}`` resolves against the process environment only (``apply_managed_overlay``
expands it) and can never be re-resolved through a profile's secret scope
(docs/design/managed-scope.md §4.1). ``read_user_config_raw`` stays the write-back primitive.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from hermes_cli import config as _config
from hermes_cli import managed_scope
from hermes_cli.config_read_errors import _warn_config_parse_failure
from utils import fast_safe_load

# path -> raw user mapping from the last successful parse in this process; served (through the
# normal pipeline) when the file is later found mid-edit as broken YAML.
_LAST_GOOD_USER_RAW: dict[str, dict[str, Any]] = {}
# path -> (*user_signature, *managed_signature, effective, env_snapshot); see utils.file_signature.
_EFFECTIVE_CACHE: dict[str, tuple[Any, ...]] = {}


class _CacheEntry(NamedTuple):
    """One effective mapping with what identifies the two files it was built from and what each
    looked like to the read behind it. Never handed out; ``effective`` leaves only as a copy."""
    # utils.file_signature of the user file; None when there was no file.
    user_sig: Optional[Tuple[int, int, int, int]]
    # Which managed file was read, in which state: the scope's own path beside its signature, so
    # two scopes whose files stat alike are still two entries.
def _effective(raw: dict[str, Any]) -> dict[str, Any]:
    # Each layer retains its own resolver, even when both reference the same variable.
    expanded = _config._expand_env_vars(raw)
    # The user file as seen by the parse that produced ``effective``, recorded only when that
    # parse was this loader's own (or there was no file). None when the mapping came from
    # ``_RAW_CONFIG_CACHE``: another reader normalized that one (a null root is stored as
    # ``{}``), so nothing here saw the file.
    user_shape: Optional[_LayerShape]
    # The managed file as seen by the read whose mapping was merged into ``effective``. None when
    # that merge failed, so ``effective`` does not carry the layer the shape would describe. Never
    # one with ``unreadable`` set: a result whose managed layer could not be read is not cached.
    managed_shape: Optional[_LayerShape]
    merged = managed_scope.apply_managed_overlay(expanded if isinstance(expanded, dict) else {})
    return _config._normalize_root_model_keys(merged if isinstance(merged, dict) else {})


def _recover_user_raw(config_path: Path, path_key: str, exc: Exception) -> dict[str, Any]:
    """Last-known-good raw user mapping after a parse failure: this process's last good parse,
    else the newest ``good`` copy in backups/config/, else ``{}`` (warned as defaults)."""
    raw = _LAST_GOOD_USER_RAW.get(path_key)
    fallback = "last-known-good"
    if raw is None:
        from hermes_cli.config_backups import load_newest_good_backup
        raw = load_newest_good_backup(config_path)
        fallback = "last-known-good-backup"
    _warn_config_parse_failure(config_path, exc, fallback=fallback if raw is not None else "defaults")
    return copy.deepcopy(raw) if raw is not None else {}


def load_user_config_effective(config_path: Optional[Path] = None, *, fail_closed: bool = False) -> dict[str, Any]:
    """User ``config.yaml`` → ``${VAR}`` expansion → managed overlay → model-key canonicalization.
    NO ``DEFAULT_CONFIG`` merge: a key absent from the file (and from the managed layer) is absent
    here, so ``{}`` sentinels and presence-sensitive bridges keep working. An absent file is an
    empty user layer (the managed layer still applies). Returns a fresh deepcopy.

    Broken YAML: ``fail_closed=True`` raises the parse error (for callers that keep their own
    last-good state); otherwise the last successfully parsed user file — in-process first, then
    the newest ``backups/config/*.good.*`` copy — is served through the same pipeline, so a
    mid-edit torn write never silently drops user overrides (same contract as ``load_config``).
    Cached on the user + managed file signatures and the values of every referenced env var."""
    if config_path is None:
        config_path = _config.get_config_path()
    path_key = str(config_path)
    strict = strict_structure or strict_section is not None
    with _config._CONFIG_LOCK:
        user_sig, cache_sig = _config._load_config_cache_sig(config_path)
        cached = _EFFECTIVE_CACHE.get(path_key)
        if cached is not None and cache_sig is not None and cached[:8] == cache_sig:
            # saw; without the record it falls through and reads the layers itself.
            if all(_config._env_ref_lookup(k) == v for k, v in cached[9].items()):
                return copy.deepcopy(cached[8])

        raw: dict[str, Any] = {}
        recovered = False
        raw_hit = _config._RAW_CONFIG_CACHE.get(path_key)
        if user_sig is not None and raw_hit is not None and raw_hit[:4] == user_sig:
            raw = copy.deepcopy(raw_hit[4])  # one parse per process, shared with read_raw_config()
            # A matching raw-cache read is the current user authority, just like a new parse.
            # Replace older recovery data and own a copy before expansion or callers mutate it.
            _LAST_GOOD_USER_RAW.setdefault(path_key, copy.deepcopy(raw))
        elif user_sig is not None:
            try:
                with open(config_path, encoding="utf-8-sig") as f:
                    loaded = fast_safe_load(f)
            except Exception as exc:
                if fail_closed:
                    raise
                raw, recovered = _recover_user_raw(config_path, path_key, exc), True
            else:
                raw = loaded if isinstance(loaded, dict) else {}
                _config._RAW_CONFIG_CACHE[path_key] = (*user_sig, copy.deepcopy(raw))
                _LAST_GOOD_USER_RAW[path_key] = copy.deepcopy(raw)
                # Same copy load_config keeps: a fresh process recovers from it (see _recover_user_raw).
                # Only for the ACTIVE home — a read of another profile's file (doctor, TUI cwd lookup)
                # must not create backups/ inside that profile.
                if config_path == _config.get_config_path():
                    from hermes_cli.config_backups import backup_config
                    backup_config(config_path, "good")

        env_snapshot = _config._env_ref_snapshot(raw)
        managed = managed_scope.load_managed_config()
        if managed:
            _config._env_ref_snapshot(managed, env_snapshot)
        effective = _effective(raw)
        # A recovered result is never cached under the corrupt file's signature: a later
        # ``fail_closed`` caller must still see the parse error, not a cache hit.
        # (``_read_managed_config``), and an entry here would outlive it under an unchanged
        if cache_sig is not None and not recovered:
            _EFFECTIVE_CACHE[path_key] = (*cache_sig, copy.deepcopy(effective), env_snapshot)
        else:
            _EFFECTIVE_CACHE.pop(path_key, None)
        return effective
