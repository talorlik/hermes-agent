"""The effective USER config: config.yaml + managed overlay + ``${VAR}`` expansion, no defaults.

``load_config()`` merges ``DEFAULT_CONFIG`` first, which is wrong for readers that treat a
missing key as "unset" (the gateway's presence-sensitive env bridge, ``cfg == {}`` sentinels,
cron model pinning) — so nine surfaces used to hand-roll raw-read → overlay → expand in
differing orders and none of them replayed the model-key canonicalization or the last-known-good
recovery ``load_config()`` gained. This module is that one primitive.

Order matches ``_load_config_impl``: the user layer is expanded BEFORE the managed overlay so a
managed ``${VAR}`` resolves against the process environment only (the managed merge expands it)
and can never be re-resolved through a profile's secret scope
(docs/design/managed-scope.md §4.1). ``read_user_config_raw`` stays the write-back primitive.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Dict, NamedTuple, Optional, Tuple

from hermes_cli import config as _config
from hermes_cli import managed_scope
from hermes_cli.config_read_errors import _warn_config_parse_failure
from hermes_cli.managed_scope import _LayerShape, _layer_shape
from utils import fast_safe_load, file_signature

# path -> raw user mapping from the last successful parse in this process; served (through the
# normal pipeline) when the file is later found mid-edit as broken YAML.
_LAST_GOOD_USER_RAW: Dict[str, Dict[str, Any]] = {}


class _CacheEntry(NamedTuple):
    """One effective mapping with what identifies the two files it was built from and what each
    looked like to the read behind it. Never handed out; ``effective`` leaves only as a copy."""

    # utils.file_signature of the user file; None when there was no file.
    user_sig: Optional[Tuple[int, int, int, int]]
    # Which managed file was read, in which state: the scope's own path beside its signature, so
    # two scopes whose files stat alike are still two entries.
    managed: managed_scope._ManagedSelection
    effective: Dict[str, Any]
    # Each layer retains its own resolver, even when both reference the same variable.
    env: _config._ConfigEnvSnapshot
    # The user file as seen by the parse that produced ``effective``, recorded only when that
    # parse was this loader's own (or there was no file). None when the mapping came from
    # ``_RAW_CONFIG_CACHE``: another reader normalized that one (a null root is stored as
    # ``{}``), so nothing here saw the file.
    user_shape: Optional[_LayerShape]
    # The managed file as seen by the read whose mapping was merged into ``effective``. None when
    # that merge failed, so ``effective`` does not carry the layer the shape would describe. Never
    # one with ``unreadable`` set: a result whose managed layer could not be read is not cached.
    managed_shape: Optional[_LayerShape]


_EFFECTIVE_CACHE: Dict[str, _CacheEntry] = {}


class ConfigRootNotMappingError(ValueError):
    """A strict read: the file parsed, but its root is not a mapping (an explicit null and an
    empty file included), so it cannot say which keys are absent."""


class ConfigSectionNotMappingError(ValueError):
    """``strict_section``: the file's root is a mapping, but it carries the named section as
    something other than a mapping (an explicit null included), so the section cannot say which of
    its keys are absent, whatever another layer puts in its place."""


class ConfigLayerUnreadableError(ValueError):
    """``strict_section``: the managed file is present but could not be read, parsed or merged, so
    it cannot say which keys it pins. Ordinary readers take such a file as no managed layer."""


def _require_shape(
    shape: Optional[_LayerShape],
    config_path: Optional[Path],
    strict_section: Optional[str],
) -> None:
    """Raise unless ``shape`` records a file that was read (or no file) with a mapping root in
    which ``strict_section``, when given, is absent or a mapping. No record at all is refused as
    well: a mapping this loader did not parse is not evidence of what the file's root was."""
    if shape is None:
        raise ConfigRootNotMappingError(
            f"{config_path}: top-level YAML is not known to be a mapping"
        )
    if shape.unreadable is not None:
        raise ConfigLayerUnreadableError(
            f"{config_path}: could not be read or parsed ({shape.unreadable})"
        )
    if shape.root is not None:
        raise ConfigRootNotMappingError(
            f"{config_path}: top-level YAML must be a mapping, got {shape.root}"
        )
    if strict_section is None:
        return
    for key, found in shape.non_mapping:
        if key == strict_section:
            raise ConfigSectionNotMappingError(
                f"{config_path}: the {strict_section} section must be a mapping, got {found}"
            )


def _require_layers(
    user_shape: Optional[_LayerShape],
    managed_shape: Optional[_LayerShape],
    config_path: Path,
    managed_path: Optional[Path],
    strict_section: Optional[str],
) -> None:
    """The strict verdict on both layers as written, each from the shape its own read recorded.
    The managed layer is judged only under ``strict_section``."""
    _require_shape(user_shape, config_path, strict_section)
    if strict_section is None:
        return
    if managed_shape is None:
        raise ConfigLayerUnreadableError(
            f"{managed_path}: the managed layer could not be merged"
        )
    _require_shape(managed_shape, managed_path, strict_section)


def _user_signature(
    config_path: Path,
) -> Tuple[Optional[Tuple[int, int, int, int]], Optional[OSError]]:
    """``(signature, error)`` from one stat of the user file: ``(sig, None)`` for a file,
    ``(None, None)`` when there is none, and ``(None, exc)`` when stat failed for any other
    reason. Only the second is the valid absence a None signature stands for elsewhere: a stat
    refused by permissions or I/O says nothing about whether the file is there, so a strict (or
    ``fail_closed``) read must not take it as an empty user layer, cached or not."""
    try:
        return file_signature(os.stat(config_path)), None
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, exc


def _effective(
    raw: Dict[str, Any], managed: managed_scope._ManagedConfig
) -> Tuple[Dict[str, Any], bool]:
    """The effective mapping for ``raw`` under the managed layer ``managed`` read, and whether
    that layer was merged. False only when the merge failed (fail-open, logged): the mapping is
    then the user layer alone."""
    expanded = _config._expand_env_vars(raw)
    user = expanded if isinstance(expanded, dict) else {}
    merged = managed.overlay(user)
    applied = merged is not None
    if not applied:
        merged = user
    return (
        _config._normalize_root_model_keys(merged if isinstance(merged, dict) else {}),
        applied,
    )


def _recover_user_raw(
    config_path: Path, path_key: str, exc: Exception
) -> Dict[str, Any]:
    """Last-known-good raw user mapping after a parse failure: this process's last good parse,
    else the newest ``good`` copy in backups/config/, else ``{}`` (warned as defaults)."""
    raw = _LAST_GOOD_USER_RAW.get(path_key)
    fallback = "last-known-good"
    if raw is None:
        from hermes_cli.config_backups import load_newest_good_backup

        raw = load_newest_good_backup(config_path)
        fallback = "last-known-good-backup"
    _warn_config_parse_failure(
        config_path, exc, fallback=fallback if raw is not None else "defaults"
    )
    return copy.deepcopy(raw) if raw is not None else {}


def load_user_config_effective(
    config_path: Optional[Path] = None,
    *,
    fail_closed: bool = False,
    strict_structure: bool = False,
    strict_section: Optional[str] = None,
) -> Dict[str, Any]:
    """User ``config.yaml`` → ``${VAR}`` expansion → managed overlay → model-key canonicalization.
    NO ``DEFAULT_CONFIG`` merge: a key absent from the file (and from the managed layer) is absent
    here, so ``{}`` sentinels and presence-sensitive bridges keep working. An absent file is an
    empty user layer (the managed layer still applies). Returns a fresh deepcopy.

    Broken YAML: ``fail_closed=True`` raises the parse error (for callers that keep their own
    last-good state); otherwise the last successfully parsed user file — in-process first, then
    the newest ``backups/config/*.good.*`` copy — is served through the same pipeline, so a
    mid-edit torn write never silently drops user overrides (same contract as ``load_config``).
    Cached on the user file's signature, the managed selection (which scope's file, and that
    file's signature) and the values of every referenced env var. The managed scope is resolved
    once per call: the selection that keys the cache is the one the managed read is made from.

    A root that parses but is not a mapping (scalar, list, explicit null, empty file) is an empty
    user layer for ordinary callers, ``fail_closed`` or not. ``strict_structure=True`` is the
    opt-in for readers that act on a key being ABSENT: the root is judged as parsed, before any
    normalization, and anything but a mapping raises ``ConfigRootNotMappingError`` (a
    ``ValueError``). A parse or read error is then raised as itself even without ``fail_closed``
    and last-good is never served; a missing file is still a valid empty user layer, but a file
    whose stat is refused for any other reason is not one, and that error is raised as itself
    (under ``fail_closed`` too) before any cached entry can stand in for it. A rejected root
    touches no cache, last-good state or backup, so ordinary callers read the file exactly as
    before.

    ``strict_section="cron"`` is the same opt-in one level down, for readers that act on a key
    being absent from that section: the root is held to ``strict_structure``, and the section as
    the user wrote it must be missing or a mapping, else ``ConfigSectionNotMappingError`` (a
    ``ValueError``). It is judged before the managed overlay, which would otherwise replace a
    null, scalar or list section with a mapping of its own. Such a file is still a good parse for
    every other reader, so it is cached and kept as last-good exactly as an ordinary read would.

    ``strict_section`` holds the managed layer to the same standard, also as written and before
    it is merged. No managed scope and no managed file are a valid empty layer. A managed file
    that is present must have a mapping root in which the section is missing or a mapping (the
    same two errors, naming the managed file), and one that cannot be read, parsed or merged is
    ``ConfigLayerUnreadableError`` (a ``ValueError``). Every other reader, ``strict_structure``
    alone included, keeps taking such a file as no managed layer, and the entry cached here is
    the one they would have cached.

    Each verdict comes from the same single read that yields the layer's mapping: the cache entry
    records the shape each read saw (``_LayerShape``) beside the effective mapping they produced,
    and a strict read is answered from an entry only when it carries that record. An entry built
    from the raw cache does not (``read_raw_config`` stores a null root as ``{}``), so a strict
    read then parses the file itself and never takes a mapping another reader parsed. The managed
    shape is recorded by the managed reader in the entry that holds its mapping
    (``managed_scope._read_managed_config``); no layer is read a second time to be judged.
    Deliberately conservative: a strict caller must treat any of these errors as "unknown", never
    as "key absent"."""
    if config_path is None:
        config_path = _config.get_config_path()
    path_key = str(config_path)
    strict = strict_structure or strict_section is not None
    with _config._CONFIG_LOCK:
        user_sig, stat_error = _user_signature(config_path)
        if stat_error is not None and (fail_closed or strict):
            # The file could not be examined, which is not the same as there being no file: a
            # None signature would match an entry cached for a genuinely absent file (and the
            # fallthrough below would read no file) and so certify an empty user layer nothing
            # saw. Raised as the read error it is, before any cache or layer can answer, the
            # same way an unreadable open is. Ordinary callers keep the fail-open reading.
            raise stat_error
        # Resolved once: this one selection keys the cache entry and names the file read below.
        selection = managed_scope._select_managed_config()
        cached = _EFFECTIVE_CACHE.get(path_key)
        if (
            cached is not None
            and cached.user_sig == user_sig
            and cached.managed == selection
            and cached.env.is_current()
        ):
            if not strict:
                return copy.deepcopy(cached.effective)
            # A strict read is answered only by an entry that records what the reads behind it
            # saw; without the record it falls through and reads the layers itself.
            if cached.user_shape is not None and (
                strict_section is None or cached.managed_shape is not None
            ):
                _require_layers(
                    cached.user_shape,
                    cached.managed_shape,
                    config_path,
                    selection.path,
                    strict_section,
                )
                return copy.deepcopy(cached.effective)

        raw: Dict[str, Any] = {}
        recovered = False
        # No file is a valid empty user layer. For a file, only the parse below records a shape;
        # a file whose stat was refused (ordinary callers only, see above) is read as no file but
        # recorded as nothing seen, so the entry can never certify an absence to a strict read.
        shape: Optional[_LayerShape] = (
            _LayerShape() if user_sig is None and stat_error is None else None
        )
        raw_hit = _config._RAW_CONFIG_CACHE.get(path_key)
        if (
            not strict
            and user_sig is not None
            and raw_hit is not None
            and raw_hit[:4] == user_sig
        ):
            raw = copy.deepcopy(
                raw_hit[4]
            )  # one parse per process, shared with read_raw_config()
            # A matching raw-cache read is the current user authority, just like a new parse.
            # Replace older recovery data and own a copy before expansion or callers mutate it.
            _LAST_GOOD_USER_RAW[path_key] = copy.deepcopy(raw)
        elif user_sig is not None:
            try:
                with open(config_path, encoding="utf-8-sig") as f:
                    loaded = fast_safe_load(f)
            except Exception as exc:
                if fail_closed or strict:
                    raise
                raw, recovered = _recover_user_raw(config_path, path_key, exc), True
            else:
                shape = _layer_shape(loaded)
                if strict and shape.root is not None:
                    # Raised before any cache, last-good or backup write: a rejected root is not
                    # a good parse, and ordinary callers must find their state untouched.
                    _require_shape(shape, config_path, None)
                raw = loaded if isinstance(loaded, dict) else {}
                _config._RAW_CONFIG_CACHE[path_key] = (*user_sig, copy.deepcopy(raw))
                _LAST_GOOD_USER_RAW[path_key] = copy.deepcopy(raw)
                # Same copy load_config keeps: a fresh process recovers from it (see _recover_user_raw).
                # Only for the ACTIVE home — a read of another profile's file (doctor, TUI cwd lookup)
                # must not create backups/ inside that profile.
                if config_path == _config.get_config_path():
                    from hermes_cli.config_backups import backup_config

                    backup_config(config_path, "good")

        # One managed read serves the env snapshot, the merge and the shape recorded below.
        managed = managed_scope._read_managed_config(selection)
        managed_mapping = managed.mapping()
        env_snapshot = _config._config_env_snapshot(raw, managed_mapping)
        effective, merged = _effective(raw, managed)
        managed_shape = managed.shape if merged else None
        # A recovered result is never cached under the corrupt file's signature: a later
        # ``fail_closed`` caller must still see the parse error, not a cache hit. Nor is a result
        # whose managed layer could not be read: the managed reader does not cache a failed read
        # (``_read_managed_config``), and an entry here would outlive it under an unchanged
        # signature, pinning the layer as unreadable for strict readers and as absent for the rest
        # until the file changed. The next call reads the layer again; a read that succeeds, or
        # fails for its shape, is cached as before.
        if recovered:
            _EFFECTIVE_CACHE.pop(path_key, None)
        elif managed.shape.unreadable is None:
            _EFFECTIVE_CACHE[path_key] = _CacheEntry(
                user_sig,
                selection,
                copy.deepcopy(effective),
                env_snapshot,
                shape,
                managed_shape,
            )
        if strict:
            # The user root was judged before the writes above; this is the verdict on the
            # user's section and on the managed layer, each from the shape of the same read (or
            # of no file) that produced ``effective``.
            _require_layers(
                shape, managed_shape, config_path, selection.path, strict_section
            )
        return effective
