"""Invariants for ``hermes_cli.config_effective.load_user_config_effective`` — the one loader every
defaults-free config reader (gateway runtime, TUI gateway, cron, ``hermes send`` bridge, doctor,
bootstrap modules) goes through."""

import os
import textwrap

import pytest

import hermes_yaml as yaml


@pytest.fixture
def homes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    monkeypatch.setenv("FIXTURE_USER_KEY", "user-secret")
    monkeypatch.setenv("FIXTURE_MANAGED_URL", "https://managed.example")
    _reset_caches()
    return home, managed


def _reset_caches():
    import hermes_cli.config as cfg
    from hermes_cli import config_effective, managed_scope

    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    config_effective._EFFECTIVE_CACHE.clear()
    config_effective._LAST_GOOD_USER_RAW.clear()
    managed_scope.invalidate_managed_cache()


def _write(path, body):
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    _reset_caches()


USER_YAML = """
    model:
      name: user/model
      api_key: ${FIXTURE_USER_KEY}
    provider: custom
    display:
      skin: user-skin
    """
MANAGED_YAML = """
    model:
      base_url: ${FIXTURE_MANAGED_URL}
    display:
      skin: managed-skin
    """


def test_effective_is_user_plus_managed_plus_env_with_no_defaults(homes):
    """Contract as a fixture: given user config.yaml X, managed overlay Y and env Z, the effective
    dict is exactly this literal — ``${VAR}`` expanded on both layers, managed keys winning,
    root ``provider`` migrated under ``model``, and no DEFAULT_CONFIG key introduced (a missing
    key stays missing). Per-message gateway reads (and the system prompt built from them) are
    pinned by this shape, not by re-running the implementation's primitives."""
    from hermes_cli.config_effective import load_user_config_effective

    home, managed = homes
    _write(home / "config.yaml", USER_YAML)
    _write(managed / "config.yaml", MANAGED_YAML)

    effective = load_user_config_effective(home / "config.yaml")

    assert effective == {
        "model": {
            "default": "user/model",
            "provider": "custom",
            "api_key": "user-secret",
            "base_url": "https://managed.example",
        },
        "display": {"skin": "managed-skin"},
    }


def test_broken_yaml_serves_last_good_and_fail_closed_raises(homes):
    """A torn mid-edit write must not silently drop user overrides: the fail-open path serves the last
    successfully parsed user file through the same pipeline; ``fail_closed`` surfaces the error to
    callers that keep their own last-good state."""
    from hermes_cli.config_effective import load_user_config_effective

    home, _ = homes
    _write(home / "config.yaml", USER_YAML)
    good = load_user_config_effective(home / "config.yaml")

    (home / "config.yaml").write_text("model: [unterminated", encoding="utf-8")
    _reset_caches_keep_last_good()

    assert load_user_config_effective(home / "config.yaml") == good
    with pytest.raises(
        yaml.YAMLError
    ):  # the type _refresh_fallback_model's own last-good path keys on
        load_user_config_effective(home / "config.yaml", fail_closed=True)


def test_good_backup_is_written_only_for_the_active_home(homes, tmp_path):
    """Reading ANOTHER profile's config (doctor, TUI cwd lookup) is a read: it must not create
    ``backups/config/`` inside that profile. The active home keeps the last-good copy."""
    from hermes_cli.config_effective import load_user_config_effective

    home, _ = homes
    other = tmp_path / "other-profile"
    other.mkdir()
    _write(home / "config.yaml", USER_YAML)
    _write(other / "config.yaml", USER_YAML)

    load_user_config_effective(other / "config.yaml")
    load_user_config_effective(home / "config.yaml")

    assert not (other / "backups").exists()
    assert list((home / "backups" / "config").glob("config.yaml.good.*"))


def _reset_caches_keep_last_good():
    import hermes_cli.config as cfg
    from hermes_cli import config_effective

    cfg._RAW_CONFIG_CACHE.clear()
    config_effective._EFFECTIVE_CACHE.clear()


# --- strict_structure: opt-in validation of the raw root, before normalization -------------------

# USER_YAML + MANAGED_YAML + the fixture env, as pinned by the contract test above.
USER_PLUS_MANAGED_EFFECTIVE = {
    "model": {
        "default": "user/model",
        "provider": "custom",
        "api_key": "user-secret",
        "base_url": "https://managed.example",
    },
    "display": {"skin": "managed-skin"},
}

# Roots that parse but are not a mapping. The falsy ones (empty file, null, false, 0, []) are
# listed beside the truthy ones because ``read_raw_config`` stores each of them as ``{}``.
NON_MAPPING_ROOTS = {
    "scalar-text": "just-a-scalar\n",
    "scalar-int": "42\n",
    "scalar-zero": "0\n",
    "list": "- model\n- provider\n",
    "list-empty": "[]\n",
    "true": "true\n",
    "false": "false\n",
    "null": "null\n",
    "tilde": "~\n",
    "empty-file": "",
}
_ROOTS = pytest.mark.parametrize(
    "root", list(NON_MAPPING_ROOTS.values()), ids=list(NON_MAPPING_ROOTS)
)
# Which non-strict reader saw the file first in this process, if any.
_PRIMING = pytest.mark.parametrize(
    "priming", ["cold", "permissive", "fail-closed", "raw"]
)


def _prime(home, priming):
    """Read the active home's config.yaml first, the way an earlier non-strict reader would. Each of
    these readers serves a non-mapping root as an empty mapping and may cache it as one."""
    import hermes_cli.config as cfg
    from hermes_cli.config_effective import load_user_config_effective

    if priming == "permissive":
        load_user_config_effective(home / "config.yaml")
    elif priming == "fail-closed":
        load_user_config_effective(home / "config.yaml", fail_closed=True)
    elif priming == "raw":
        assert (
            cfg.get_config_path() == home / "config.yaml"
        )  # read_raw_config() takes no path
        cfg.read_raw_config()


def _swap(path, body):
    """Replace the file with a new inode and leave every cache alone: the signature is guaranteed to
    change, so a stale entry can only be served by a reader that ignores it."""
    fresh = path.with_name(path.name + ".swap")
    fresh.write_text(body, encoding="utf-8")
    os.replace(fresh, path)


def _good_backups(home):
    return sorted(
        p.name for p in (home / "backups" / "config").glob("config.yaml.good.*")
    )


def _count_user_parses(monkeypatch):
    """Every YAML parse made through the parser bindings of the two modules that read a user
    config.yaml; the managed layer parses through ``managed_scope``'s own binding."""
    import hermes_cli.config as cfg
    from hermes_cli import config_effective

    parses = []
    for module in (cfg, config_effective):

        def counting(
            stream, *args, _real=module.fast_safe_load, _name=module.__name__, **kwargs
        ):
            parses.append(_name)
            return _real(stream, *args, **kwargs)

        monkeypatch.setattr(module, "fast_safe_load", counting)
    return parses


@_ROOTS
def test_ordinary_callers_read_a_non_mapping_root_as_an_empty_user_layer(homes, root):
    """The existing contract, pinned so the opt-in below cannot change it: without
    ``strict_structure`` a root that is not a mapping is an empty user layer, ``fail_closed`` or not."""
    from hermes_cli.config_effective import load_user_config_effective

    home, _ = homes
    (home / "config.yaml").write_text(root, encoding="utf-8")

    assert load_user_config_effective(home / "config.yaml") == {}
    assert load_user_config_effective(home / "config.yaml", fail_closed=True) == {}


@_PRIMING
@_ROOTS
def test_strict_structure_rejects_a_non_mapping_root_whatever_was_cached(
    homes, root, priming
):
    """``strict_structure`` judges the root as parsed, before it is normalized, and raises on its own
    authority: ``fail_closed`` is not needed to surface it. An entry a permissive or raw reader
    cached for the same file carries no proof of the root's shape and is never the answer."""
    from hermes_cli.config_effective import load_user_config_effective

    home, _ = homes
    path = home / "config.yaml"
    path.write_text(root, encoding="utf-8")
    _prime(home, priming)

    for fail_closed in (False, True):
        with pytest.raises(ValueError, match="mapping"):
            load_user_config_effective(
                path, fail_closed=fail_closed, strict_structure=True
            )

    # The rejection changed nothing for ordinary callers ...
    assert load_user_config_effective(path) == {}
    assert load_user_config_effective(path, fail_closed=True) == {}
    # ... and what they just cached is still not evidence of a mapping root.
    with pytest.raises(ValueError, match="mapping"):
        load_user_config_effective(path, strict_structure=True)


def test_strict_structure_rejects_the_root_before_the_managed_overlay_can_fill_it(
    homes,
):
    from hermes_cli.config_effective import load_user_config_effective

    home, managed = homes
    _write(home / "config.yaml", "- not\n- a mapping\n")
    _write(managed / "config.yaml", MANAGED_YAML)

    # Permissive: the managed layer alone, a perfectly good-looking mapping.
    permissive = load_user_config_effective(home / "config.yaml")
    assert permissive["display"] == {"skin": "managed-skin"}
    with pytest.raises(ValueError, match="mapping"):
        load_user_config_effective(home / "config.yaml", strict_structure=True)


@pytest.mark.parametrize("fail_closed", [False, True])
def test_strict_structure_reads_a_missing_file_as_an_empty_user_layer(
    homes, fail_closed
):
    """No file is a legitimate empty user layer, unlike a file that is empty or null; the managed
    layer still applies."""
    from hermes_cli.config_effective import load_user_config_effective

    home, managed = homes
    path = home / "config.yaml"
    assert not path.exists()
    assert (
        load_user_config_effective(path, fail_closed=fail_closed, strict_structure=True)
        == {}
    )

    _write(managed / "config.yaml", MANAGED_YAML)
    effective = load_user_config_effective(
        path, fail_closed=fail_closed, strict_structure=True
    )
    assert effective["display"] == {"skin": "managed-skin"}
    assert effective["model"]["base_url"] == "https://managed.example"
    assert effective == load_user_config_effective(path)


@_PRIMING
def test_strict_structure_returns_the_ordinary_effective_mapping_for_a_valid_root(
    homes, priming
):
    from hermes_cli.config_effective import load_user_config_effective

    home, managed = homes
    path = home / "config.yaml"
    _write(path, USER_YAML)
    _write(managed / "config.yaml", MANAGED_YAML)
    _prime(home, priming)

    assert (
        load_user_config_effective(path, strict_structure=True)
        == USER_PLUS_MANAGED_EFFECTIVE
    )
    assert (
        load_user_config_effective(path, fail_closed=True, strict_structure=True)
        == USER_PLUS_MANAGED_EFFECTIVE
    )
    assert load_user_config_effective(path) == USER_PLUS_MANAGED_EFFECTIVE


@_PRIMING
def test_strict_structure_accepts_an_explicit_empty_mapping(homes, priming):
    """``{}`` is a mapping root with nothing in it: valid, where an empty file is not."""
    from hermes_cli.config_effective import load_user_config_effective

    home, _ = homes
    _write(home / "config.yaml", "{}\n")
    _prime(home, priming)

    assert load_user_config_effective(home / "config.yaml", strict_structure=True) == {}


def test_strict_structure_parses_once_per_miss_and_serves_a_valid_root_from_cache(
    homes, tmp_path, monkeypatch
):
    """One parse decides both the shape and the content of a strict miss; the repeat is a cache hit,
    and each caller gets its own copy."""
    from hermes_cli.config_effective import load_user_config_effective

    # Not the active home, so no last-good backup is written beside the read.
    other = tmp_path / "other-profile"
    other.mkdir()
    path = other / "config.yaml"
    _write(path, USER_YAML)
    parses = _count_user_parses(monkeypatch)

    first = load_user_config_effective(path, strict_structure=True)
    assert len(parses) == 1, parses
    second = load_user_config_effective(path, strict_structure=True)
    assert len(parses) == 1, parses
    assert first == second
    assert first["model"]["api_key"] == "user-secret"

    first["model"]["default"] = "mutated"
    first["injected"] = True
    second["display"].clear()
    third = load_user_config_effective(path, strict_structure=True)
    assert len(parses) == 1, parses
    assert third["model"]["default"] == "user/model"
    assert third["display"] == {"skin": "user-skin"}
    assert "injected" not in third
    assert third == load_user_config_effective(path)


def test_strict_structure_decides_an_invalid_root_from_one_parse(
    homes, tmp_path, monkeypatch
):
    """The parse that is rejected is the only one: no second, separate read of the file decides the
    shape of content it did not produce."""
    from hermes_cli.config_effective import load_user_config_effective

    other = tmp_path / "other-profile"
    other.mkdir()
    path = other / "config.yaml"
    _write(path, "- not\n- a mapping\n")
    parses = _count_user_parses(monkeypatch)

    with pytest.raises(ValueError, match="mapping"):
        load_user_config_effective(path, strict_structure=True)

    assert len(parses) == 1, parses


def test_strict_structure_cache_follows_env_and_managed_edits(homes, monkeypatch):
    """A strict hit obeys the same invalidation as a permissive one: a referenced ``${VAR}`` changing
    on either layer, or the managed file changing, is a miss."""
    from hermes_cli.config_effective import load_user_config_effective

    home, managed = homes
    path = home / "config.yaml"
    _write(path, USER_YAML)
    _write(managed / "config.yaml", MANAGED_YAML)
    assert (
        load_user_config_effective(path, strict_structure=True)
        == USER_PLUS_MANAGED_EFFECTIVE
    )

    monkeypatch.setenv("FIXTURE_USER_KEY", "rotated-secret")
    assert (
        load_user_config_effective(path, strict_structure=True)["model"]["api_key"]
        == "rotated-secret"
    )

    monkeypatch.setenv("FIXTURE_MANAGED_URL", "https://moved.example")
    assert (
        load_user_config_effective(path, strict_structure=True)["model"]["base_url"]
        == "https://moved.example"
    )

    _swap(managed / "config.yaml", "display:\n  skin: repinned-skin\n")
    effective = load_user_config_effective(path, strict_structure=True)
    assert effective["display"] == {"skin": "repinned-skin"}
    assert "base_url" not in effective["model"]


@pytest.mark.parametrize("fail_closed", [False, True])
def test_strict_structure_raises_the_parse_error_itself_and_never_serves_last_good(
    homes, fail_closed
):
    """Broken YAML under ``strict_structure`` is the parser's own error, not the structural one, and
    it is raised without ``fail_closed``. The permissive last-good contract is the same afterwards."""
    from hermes_cli.config_effective import load_user_config_effective

    home, _ = homes
    path = home / "config.yaml"
    _write(path, USER_YAML)
    good = load_user_config_effective(path)

    path.write_text("model: [unterminated", encoding="utf-8")
    _reset_caches_keep_last_good()

    with pytest.raises(yaml.YAMLError):
        load_user_config_effective(path, fail_closed=fail_closed, strict_structure=True)

    assert load_user_config_effective(path) == good
    with pytest.raises(yaml.YAMLError):
        load_user_config_effective(path, fail_closed=True)


def test_strict_rejection_leaves_last_good_state_alone(homes):
    """A root that strict mode rejects is not a good parse: it replaces neither this process's
    last-good mapping nor the on-disk good copy a fresh process recovers from."""
    from hermes_cli.config_effective import load_user_config_effective

    home, _ = homes
    path = home / "config.yaml"
    _write(path, USER_YAML)
    good = load_user_config_effective(path)
    backups = _good_backups(home)
    assert backups

    _swap(path, "- no longer\n- a mapping\n")
    with pytest.raises(ValueError, match="mapping"):
        load_user_config_effective(path, strict_structure=True)
    assert _good_backups(home) == backups

    # The next torn write is recovered from the file that parsed as a mapping ...
    _swap(path, "model: [unterminated")
    assert load_user_config_effective(path) == good
    # ... in a fresh process too, where only the on-disk copy is left.
    _reset_caches()
    assert load_user_config_effective(path) == good


def test_permissive_reads_are_unchanged_once_a_strictly_rejected_file_is_fixed(homes):
    """No trace of the rejection outlives the file that caused it: the repaired file reads as the same
    defaults-free effective mapping through every mode."""
    from hermes_cli.config_effective import load_user_config_effective

    home, managed = homes
    path = home / "config.yaml"
    _write(managed / "config.yaml", MANAGED_YAML)
    _swap(path, "false\n")
    with pytest.raises(ValueError, match="mapping"):
        load_user_config_effective(path, strict_structure=True)

    _swap(path, textwrap.dedent(USER_YAML))

    assert load_user_config_effective(path) == USER_PLUS_MANAGED_EFFECTIVE
    assert (
        load_user_config_effective(path, fail_closed=True)
        == USER_PLUS_MANAGED_EFFECTIVE
    )
    assert (
        load_user_config_effective(path, strict_structure=True)
        == USER_PLUS_MANAGED_EFFECTIVE
    )


@pytest.mark.parametrize("layer", ["user", "managed"])
@pytest.mark.parametrize(
    "initial,error_name",
    [
        ("cron:\n  retention_days: 11\n", None),
        ("cron: null\n", "ConfigSectionNotMappingError"),
        ("null\n", "ConfigRootNotMappingError"),
    ],
)
def test_strict_verdict_and_mapping_share_the_parse_before_a_replacement(
    homes: tuple,
    monkeypatch: pytest.MonkeyPatch,
    layer: str,
    initial: str,
    error_name: str | None,
) -> None:
    from hermes_cli import config as cfg, config_effective as effective, managed_scope

    home, managed = homes
    # An explicit alternate filename avoids unrelated active-home backup reads.
    path = home / "observed.yaml"
    path.write_text("{}\n", encoding="utf-8")
    target = path if layer == "user" else managed / "config.yaml"
    target.write_text(initial, encoding="utf-8")
    replacement = "cron:\n  retention_days: 97\n" if error_name else "cron: null\n"
    owner = effective if layer == "user" else managed_scope
    real_parse = owner.fast_safe_load
    parses = []

    def replace_after_parse(stream: object, *args: object, **kwargs: object) -> object:
        parsed = real_parse(stream, *args, **kwargs)
        parses.append(parsed)
        if len(parses) == 1:
            _swap(target, replacement)
        return parsed

    with monkeypatch.context() as patch:
        patch.setattr(owner, "fast_safe_load", replace_after_parse)
        if layer == "user":
            patch.setattr(cfg, "fast_safe_load", replace_after_parse)
        if error_name:
            with pytest.raises(getattr(effective, error_name)) as caught:
                effective.load_user_config_effective(path, strict_section="cron")
            assert str(target) in str(caught.value)
        else:
            assert effective.load_user_config_effective(
                path, strict_section="cron"
            ) == {"cron": {"retention_days": 11}}
        assert len(parses) == 1

    # The next call must observe the replacement without any cache clearing.
    if error_name:
        assert effective.load_user_config_effective(path, strict_section="cron") == {
            "cron": {"retention_days": 97}
        }
    else:
        assert effective.load_user_config_effective(path) == {"cron": None}
        with pytest.raises(effective.ConfigSectionNotMappingError):
            effective.load_user_config_effective(path, strict_section="cron")


@pytest.mark.parametrize("invalid_section", [False, True])
def test_effective_mutation_cannot_rewrite_cached_user_provenance(
    homes: tuple, invalid_section: bool
) -> None:
    from hermes_cli import config as cfg, config_effective as effective

    home, managed = homes
    path = home / "config.yaml"
    body = "cron: null\n" if invalid_section else "cron:\n  tags: [original]\n"
    path.write_text(body, encoding="utf-8")
    (managed / "config.yaml").write_text(
        "cron:\n  wrap_response: false\n", encoding="utf-8"
    )
    expected = {"cron": {"wrap_response": False}}
    if not invalid_section:
        expected["cron"]["tags"] = ["original"]

    returned = effective.load_user_config_effective(path)
    assert returned == expected
    returned["cron"].setdefault("tags", []).append("caller")
    returned["cron"].pop("wrap_response")
    returned["caller_only"] = True

    assert effective.load_user_config_effective(path) == expected
    assert effective.load_user_config_effective(path, strict_structure=True) == expected
    assert cfg.read_raw_config() == {
        "cron": None if invalid_section else {"tags": ["original"]}
    }
    if invalid_section:
        with pytest.raises(effective.ConfigSectionNotMappingError) as caught:
            effective.load_user_config_effective(path, strict_section="cron")
        assert str(path) in str(caught.value)
    else:
        assert (
            effective.load_user_config_effective(path, strict_section="cron")
            == expected
        )


@pytest.mark.parametrize("priming", ["raw", "permissive", "strict"])
@pytest.mark.parametrize(
    "invalid,error_kind",
    [
        ("null\n", "root"),
        ("cron: null\n", "section"),
        ("cron: [unterminated\n", "parse"),
    ],
)
def test_user_valid_invalid_valid_cycle_preserves_strict_provenance(
    homes: tuple, priming: str, invalid: str, error_kind: str
) -> None:
    from hermes_cli import config as cfg, config_effective as effective

    home, managed = homes
    path = home / "config.yaml"
    path.write_text("cron:\n  retention_days: 11\n", encoding="utf-8")
    (managed / "config.yaml").write_text(
        "cron:\n  wrap_response: false\n", encoding="utf-8"
    )
    good = {"cron": {"retention_days": 11, "wrap_response": False}}
    assert cfg.read_raw_config() == {"cron": {"retention_days": 11}}
    assert effective.load_user_config_effective(path) == good
    assert effective.load_user_config_effective(path, strict_section="cron") == good

    _swap(path, invalid)
    if priming == "raw":
        cfg.read_raw_config()
    elif priming == "permissive":
        effective.load_user_config_effective(path)
    error_type = {
        "root": effective.ConfigRootNotMappingError,
        "section": effective.ConfigSectionNotMappingError,
        "parse": yaml.YAMLError,
    }[error_kind]
    with pytest.raises(error_type):
        effective.load_user_config_effective(path, strict_section="cron")
    fallback = good if error_kind == "parse" else {"cron": {"wrap_response": False}}
    assert effective.load_user_config_effective(path) == fallback
    with pytest.raises(error_type):
        effective.load_user_config_effective(path, strict_section="cron")

    _swap(path, "cron:\n  retention_days: 29\n")
    repaired = {"cron": {"retention_days": 29, "wrap_response": False}}
    assert cfg.read_raw_config() == {"cron": {"retention_days": 29}}
    assert effective.load_user_config_effective(path) == repaired
    assert effective.load_user_config_effective(path, strict_section="cron") == repaired


@pytest.mark.parametrize("operation", ["stat", "open"])
@pytest.mark.parametrize(
    "strict_options", [{"strict_structure": True}, {"strict_section": "cron"}]
)
def test_strict_user_access_failure_is_not_an_absent_file(
    homes: tuple,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    strict_options: dict,
) -> None:
    import builtins

    from hermes_cli import config_effective as effective

    home, _ = homes
    path = home / "config.yaml"
    path.write_text("cron:\n  retention_days: 11\n", encoding="utf-8")
    real_access = effective.os.stat if operation == "stat" else builtins.open

    def deny_target(target: object, *args: object, **kwargs: object) -> object:
        if target == path or target == str(path):
            raise PermissionError("unavailable user configuration")
        return real_access(target, *args, **kwargs)

    with monkeypatch.context() as patch:
        if operation == "stat":
            patch.setattr(effective.os, "stat", deny_target)
        else:
            patch.setattr(effective, "open", deny_target, raising=False)
        with pytest.raises(PermissionError, match="unavailable user configuration"):
            effective.load_user_config_effective(path, **strict_options)

    assert effective.load_user_config_effective(path, **strict_options) == {
        "cron": {"retention_days": 11}
    }
