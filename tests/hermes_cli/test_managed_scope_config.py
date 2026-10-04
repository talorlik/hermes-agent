"""Config integration tests — managed scope wins over user config at the leaf."""

import textwrap

import pytest


@pytest.fixture
def homes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    import hermes_cli.config as cfg
    from hermes_cli import managed_scope

    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    managed_scope.invalidate_managed_cache()
    return home, managed


def _write(path, body):
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    import hermes_cli.config as cfg
    from hermes_cli import managed_scope

    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    managed_scope.invalidate_managed_cache()


def test_managed_beats_user(homes):
    from hermes_cli.config import load_config, cfg_get

    home, managed = homes
    _write(home / "config.yaml", "model:\n  default: user/model\n")
    _write(managed / "config.yaml", "model:\n  default: managed/model\n")
    assert cfg_get(load_config(), "model", "default") == "managed/model"


def test_managed_list_wins_wholesale(homes):
    """D3: a managed list value replaces the user's wholesale."""
    from hermes_cli.config import load_config, cfg_get

    home, managed = homes
    _write(home / "config.yaml", "toolsets:\n  enabled: [a, b, c]\n")
    _write(managed / "config.yaml", "toolsets:\n  enabled: [x]\n")
    assert cfg_get(load_config(), "toolsets", "enabled") == ["x"]


def test_user_cannot_shadow_managed_literal_via_envref(homes, monkeypatch):
    """A managed literal must NOT be expandable via a ${VAR} the user controls.

    The managed value is a plain literal 'managed/locked' with no ${...}, so a
    user-defined env var has nothing to substitute. This asserts the managed
    literal survives verbatim regardless of user env, and that managed wins.
    """
    from hermes_cli.config import load_config, cfg_get

    home, managed = homes
    monkeypatch.setenv("EVIL", "user/override")
    _write(home / "config.yaml", "model:\n  default: ${EVIL}\n")
    _write(managed / "config.yaml", "model:\n  default: managed/locked\n")
    assert cfg_get(load_config(), "model", "default") == "managed/locked"


def test_managed_nested_dict_default_flattens_on_load(homes):
    """A dict-valued managed ``model.default`` must flatten on load.

    ``load_config()`` merges the managed overlay after its single
    normalization pass, so a managed ``model.default: {provider: ...,
    model: ...}`` used to reach runtime readers as a raw dict. The overlay
    is now normalized before merging (parity with
    ``managed_scope.apply_managed_overlay``), so the merged config exposes a
    string ``default`` paired with the nested ``provider``.
    """
    from hermes_cli.config import load_config, cfg_get

    home, managed = homes
    _write(home / "config.yaml", "model:\n  default: user/model\n")
    _write(
        managed / "config.yaml",
        "model:\n  default:\n    provider: nous\n    model: managed/nested\n",
    )
    cfg = load_config()
    assert cfg_get(cfg, "model", "default") == "managed/nested"
    assert cfg_get(cfg, "model", "provider") == "nous"


def test_managed_bare_string_model_flattens_to_default_on_load(homes):
    """A bare ``model: <string>`` in the managed file stays a dict shape.

    Mirrors the existing managed-overlay contract: a bare string model must
    merge as ``model.default`` so readers that do
    ``cfg["model"]["default"]`` keep working (never a bare string at
    ``cfg["model"]``).
    """
    from hermes_cli.config import load_config, cfg_get

    home, managed = homes
    _write(home / "config.yaml", "model:\n  default: user/model\n")
    _write(managed / "config.yaml", "model: managed/bare\n")
    cfg = load_config()
    assert cfg_get(cfg, "model", "default") == "managed/bare"


# A managed config.yaml that is not a usable mapping: a root of another shape, a file that does
# not parse, one that cannot be read. ``None`` stands for a config.yaml that is a directory.
UNUSABLE_MANAGED = {
    "root-null": "null\n",
    "root-empty-file": "",
    "root-scalar": "just-a-scalar\n",
    "root-list": "- model\n- display\n",
    "yaml-syntax-error": "model: [unterminated\n",
    "undecodable": b"model: \xff\n",
    "is-a-directory": None,
}
_UNUSABLE = pytest.mark.parametrize(
    "body", list(UNUSABLE_MANAGED.values()), ids=list(UNUSABLE_MANAGED)
)


def _write_unusable_managed(managed, body):
    target = managed / "config.yaml"
    if body is None:
        target.mkdir()
    elif isinstance(body, bytes):
        target.write_bytes(body)
    else:
        target.write_text(body, encoding="utf-8")
    import hermes_cli.config as cfg
    from hermes_cli import managed_scope

    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    managed_scope.invalidate_managed_cache()


@_UNUSABLE
def test_ordinary_managed_loader_reads_an_unusable_managed_file_as_no_layer(
    homes, body
):
    """Fail-open is the ordinary contract: an unusable managed file pins nothing and never raises,
    on the first read and on the repeat. Only an opt-in strict reader may judge it differently."""
    from hermes_cli import managed_scope

    _, managed = homes
    _write_unusable_managed(managed, body)

    assert managed_scope.load_managed_config() == {}
    assert managed_scope.load_managed_config() == {}
    assert managed_scope.managed_config_keys() == set()
    assert not managed_scope.is_key_managed("model.default")
    assert managed_scope.apply_managed_overlay({
        "model": {"default": "user/model"}
    }) == {"model": {"default": "user/model"}}


@_UNUSABLE
def test_load_config_keeps_user_values_under_an_unusable_managed_file(homes, body):
    from hermes_cli.config import load_config, cfg_get

    home, managed = homes
    _write(home / "config.yaml", "model:\n  default: user/model\n")
    _write_unusable_managed(managed, body)

    assert cfg_get(load_config(), "model", "default") == "user/model"


@pytest.mark.parametrize("invalid_section", [False, True])
def test_managed_public_copy_cannot_mutate_mapping_or_shape(
    homes: tuple, invalid_section: bool
) -> None:
    from hermes_cli import config_effective as effective, managed_scope

    home, managed = homes
    target = managed / "config.yaml"
    cron = "cron: null\n" if invalid_section else "cron:\n  retention_days: 11\n"
    target.write_text(cron + "toolsets:\n  enabled: [terminal]\n", encoding="utf-8")
    expected = {
        "cron": None if invalid_section else {"retention_days": 11},
        "toolsets": {"enabled": ["terminal"]},
    }
    returned = managed_scope.load_managed_config()
    assert returned == expected
    returned["toolsets"]["enabled"].append("caller")
    if isinstance(returned["cron"], dict):
        returned["cron"]["retention_days"] = 99
    returned["cron"] = {} if invalid_section else None
    returned["caller_only"] = True

    assert managed_scope.load_managed_config() == expected
    path = home / "config.yaml"
    assert effective.load_user_config_effective(path) == expected
    if invalid_section:
        with pytest.raises(effective.ConfigSectionNotMappingError) as caught:
            effective.load_user_config_effective(path, strict_section="cron")
        assert str(target) in str(caught.value)
    else:
        assert (
            effective.load_user_config_effective(path, strict_section="cron")
            == expected
        )


@pytest.mark.parametrize("invalid_second", [False, True])
def test_managed_scope_identity_survives_equal_file_signatures(
    homes: tuple, monkeypatch: pytest.MonkeyPatch, invalid_second: bool
) -> None:
    from hermes_cli import config_effective as effective, managed_scope

    home, first = homes
    second = first.with_name("second-managed")
    second.mkdir()
    (first / "config.yaml").write_text(
        "cron:\n  retention_days: 11\n", encoding="utf-8"
    )
    second_body = "null\n" if invalid_second else "cron:\n  retention_days: 22\n"
    (second / "config.yaml").write_text(second_body, encoding="utf-8")
    signature = managed_scope.file_signature((first / "config.yaml").stat())
    # Deliberately equal signatures isolate path identity from ordinary edit detection.
    monkeypatch.setattr(managed_scope, "file_signature", lambda stat: signature)
    path = home / "config.yaml"

    for selected, value in ((first, 11), (second, 22), (first, 11)):
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(selected))
        invalid = selected == second and invalid_second
        expected = {} if invalid else {"cron": {"retention_days": value}}
        assert managed_scope.load_managed_config() == expected
        assert effective.load_user_config_effective(path) == expected
        if invalid:
            with pytest.raises(effective.ConfigRootNotMappingError) as caught:
                effective.load_user_config_effective(path, strict_section="cron")
            assert str(selected / "config.yaml") in str(caught.value)
        else:
            assert (
                effective.load_user_config_effective(path, strict_section="cron")
                == expected
            )


@pytest.mark.parametrize("invalid_second", [False, True])
def test_effective_read_captures_an_alternating_managed_selector_once(
    homes: tuple, monkeypatch: pytest.MonkeyPatch, invalid_second: bool
) -> None:
    from pathlib import Path

    from hermes_cli import config_effective as effective, managed_scope

    home, first = homes
    second = first.with_name("alternating-managed")
    second.mkdir()
    (first / "config.yaml").write_text(
        "cron:\n  retention_days: 11\n", encoding="utf-8"
    )
    second_body = "cron: null\n" if invalid_second else "cron:\n  retention_days: 22\n"
    (second / "config.yaml").write_text(second_body, encoding="utf-8")
    selections = iter((first, second, first))
    calls = []

    def alternate() -> Path:
        selected = next(selections)
        calls.append(selected)
        return selected

    monkeypatch.setattr(managed_scope, "get_managed_dir", alternate)
    path = home / "config.yaml"
    assert effective.load_user_config_effective(path, strict_section="cron") == {
        "cron": {"retention_days": 11}
    }
    assert calls == [first]
    if invalid_second:
        with pytest.raises(effective.ConfigSectionNotMappingError) as caught:
            effective.load_user_config_effective(path, strict_section="cron")
        assert str(second / "config.yaml") in str(caught.value)
    else:
        assert effective.load_user_config_effective(path, strict_section="cron") == {
            "cron": {"retention_days": 22}
        }
    assert calls == [first, second]
    assert effective.load_user_config_effective(path, strict_section="cron") == {
        "cron": {"retention_days": 11}
    }
    assert calls == [first, second, first]
