"""Each standalone config loader (gateway, TUI/desktop, cron) must honor managed scope.

These loaders build their own config dict instead of routing through
hermes_cli.config.load_config, so the managed overlay has to be wired into each.
This is the regression guard for the whole bug class (a managed display.skin was
silently ignored by the TUI; the same gap existed in the gateway and cron).
"""

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


def _seed(home, managed, *, user, mgd):
    (home / "config.yaml").write_text(textwrap.dedent(user), encoding="utf-8")
    (managed / "config.yaml").write_text(textwrap.dedent(mgd), encoding="utf-8")
    import hermes_cli.config as cfg
    from hermes_cli import managed_scope

    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    managed_scope.invalidate_managed_cache()


def test_timezone_honors_managed(homes, monkeypatch):
    home, managed = homes
    # hermes_time checks an env override first; ensure it's unset so config wins.
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    _seed(
        home, managed, user="timezone: America/New_York\n", mgd="timezone: Asia/Tokyo\n"
    )
    import hermes_time

    assert hermes_time._resolve_timezone_name() == "Asia/Tokyo"


def test_gateway_env_bridge_honors_managed(homes, monkeypatch):
    """The gateway config→env bridge must bridge MANAGED values, not user ones.

    gateway/run.py bridges config.yaml settings into os.environ at startup and on
    every turn (HERMES_TIMEZONE, HERMES_REDACT_SECRETS, HERMES_MAX_ITERATIONS,
    ...). A managed value must win at that env layer too — otherwise the bridge
    writes the user's value into the env that the whole process then reads. This
    is the regression that manual verification caught (managed timezone was
    overridden by the user's value via the env bridge).

    We assert on the managed-overlaid config the bridge consumes (rather than the
    os.environ side effect, which leaks across same-process tests under the
    runner) — the bridge writes whatever this dict carries, so a managed value
    here proves the env var gets the managed value.
    """
    home, managed = homes
    _seed(
        home, managed, user="timezone: America/New_York\n", mgd="timezone: Asia/Tokyo\n"
    )
    from hermes_cli import managed_scope

    managed_scope.invalidate_managed_cache()
    # The bridge loads config.yaml, expands env, then applies this overlay before
    # writing HERMES_TIMEZONE = cfg["timezone"]. Prove the overlay flips the value.
    import hermes_yaml as yaml

    raw = yaml.safe_load((home / "config.yaml").read_text())
    bridged = managed_scope.apply_managed_overlay(raw)
    assert bridged.get("timezone") == "Asia/Tokyo"


# A managed config.yaml that is not a usable mapping; ``None`` stands for one that is a directory.
UNUSABLE_MANAGED = {
    "root-null": "null\n",
    "root-empty-file": "",
    "root-scalar": "just-a-scalar\n",
    "root-list": "- timezone\n- cron\n",
    "yaml-syntax-error": "timezone: [unterminated\n",
    "undecodable": b"timezone: \xff\n",
    "is-a-directory": None,
}


@pytest.mark.parametrize(
    "body", list(UNUSABLE_MANAGED.values()), ids=list(UNUSABLE_MANAGED)
)
def test_effective_loader_keeps_the_user_layer_under_an_unusable_managed_file(
    homes, body
):
    """The defaults-free loader the standalone readers share stays fail-open on the managed layer:
    an unusable managed file is no managed layer, ``fail_closed`` (a user-file contract) or not."""
    from hermes_cli import managed_scope
    from hermes_cli.config_effective import load_user_config_effective

    home, managed = homes
    target = managed / "config.yaml"
    if body is None:
        target.mkdir()
    elif isinstance(body, bytes):
        target.write_bytes(body)
    else:
        target.write_text(body, encoding="utf-8")
    managed_scope.invalidate_managed_cache()
    path = home / "config.yaml"

    assert load_user_config_effective(path) == {}

    path.write_text(
        "timezone: America/New_York\ncron:\n  wrap_response: false\n", encoding="utf-8"
    )
    expected = {"timezone": "America/New_York", "cron": {"wrap_response": False}}
    assert load_user_config_effective(path) == expected
    assert load_user_config_effective(path, fail_closed=True) == expected


@pytest.mark.parametrize("priming", ["permissive", "raw-managed"])
@pytest.mark.parametrize(
    "invalid,error_kind",
    [
        ("null\n", "root"),
        ("cron: null\n", "section"),
        ("cron: [unterminated\n", "parse"),
    ],
)
def test_managed_valid_invalid_valid_cycle_preserves_strict_provenance(
    homes: tuple, priming: str, invalid: str, error_kind: str
) -> None:
    from hermes_cli import config_effective as effective, managed_scope

    home, managed = homes
    _seed(
        home,
        managed,
        user="cron:\n  max_concurrent: 9\n  wrap_response: false\n",
        mgd="cron:\n  max_concurrent: 1\n",
    )
    path = home / "config.yaml"
    target = managed / "config.yaml"
    fresh = target.with_name("config.yaml.swap")
    good = {"cron": {"max_concurrent": 1, "wrap_response": False}}
    if priming == "raw-managed":
        assert managed_scope.load_managed_config() == {"cron": {"max_concurrent": 1}}
    else:
        assert effective.load_user_config_effective(path) == good
    assert effective.load_user_config_effective(path, strict_section="cron") == good

    # Atomic replacement changes the signature without manually invalidating either cache.
    fresh.write_text(invalid, encoding="utf-8")
    fresh.replace(target)
    error_type = {
        "root": effective.ConfigRootNotMappingError,
        "section": effective.ConfigSectionNotMappingError,
        "parse": effective.ConfigLayerUnreadableError,
    }[error_kind]
    raw_invalid = {"cron": None} if error_kind == "section" else {}
    # The unchanged ordinary merger ignores None over an existing dict section.
    fallback = {"cron": {"max_concurrent": 9, "wrap_response": False}}
    if priming == "raw-managed":
        assert managed_scope.load_managed_config() == raw_invalid
    else:
        assert effective.load_user_config_effective(path) == fallback
    with pytest.raises(error_type) as caught:
        effective.load_user_config_effective(path, strict_section="cron")
    assert str(target) in str(caught.value)
    assert effective.load_user_config_effective(path) == fallback
    assert effective.load_user_config_effective(path, fail_closed=True) == fallback
    with pytest.raises(error_type):
        effective.load_user_config_effective(path, strict_section="cron")

    fresh.write_text("cron:\n  max_concurrent: 2\n", encoding="utf-8")
    fresh.replace(target)
    repaired = {"cron": {"max_concurrent": 2, "wrap_response": False}}
    assert effective.load_user_config_effective(path, strict_section="cron") == repaired
    assert managed_scope.load_managed_config() == {"cron": {"max_concurrent": 2}}
    assert effective.load_user_config_effective(path) == repaired


def test_managed_open_failure_recovers_without_a_signature_change(
    homes: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pathlib import Path

    from hermes_cli import config_effective as effective, managed_scope

    home, managed = homes
    path = home / "config.yaml"
    target = managed / "config.yaml"
    body = "cron:\n  max_concurrent: 1\n"
    target.write_text(body, encoding="utf-8")
    signature = managed_scope.file_signature(target.stat())
    real_open = Path.open

    def deny_managed_open(self: Path, *args: object, **kwargs: object) -> object:
        if self == target:
            raise PermissionError("transient managed open failure")
        return real_open(self, *args, **kwargs)

    # Patch the actual open used by Path.read_text, leaving stat and user reads intact.
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", deny_managed_open)
        assert effective.load_user_config_effective(path) == {}
        with pytest.raises(effective.ConfigLayerUnreadableError) as caught:
            effective.load_user_config_effective(path, strict_section="cron")
        assert str(target) in str(caught.value)
        assert "PermissionError" in str(caught.value)

    assert target.read_text(encoding="utf-8") == body
    assert managed_scope.file_signature(target.stat()) == signature
    expected = {"cron": {"max_concurrent": 1}}
    assert effective.load_user_config_effective(path, strict_section="cron") == expected
    assert effective.load_user_config_effective(path) == expected


@pytest.mark.parametrize("env_value,literal", [("1", None), ("0", 0), ("null", 7)])
def test_strict_managed_leaves_preserve_expanded_strings_and_yaml_literals(
    homes: tuple, monkeypatch: pytest.MonkeyPatch, env_value: str, literal: int | None
) -> None:
    from hermes_cli.config_effective import load_user_config_effective

    home, managed = homes
    monkeypatch.setenv("FIXTURE_MANAGED_CRON_CAP", env_value)
    yaml_literal = "null" if literal is None else str(literal)
    _seed(
        home,
        managed,
        user="cron:\n  max_concurrent: 9\n  retention_days: 99\n  wrap_response: false\n",
        mgd=(
            "cron:\n  max_concurrent: ${FIXTURE_MANAGED_CRON_CAP}\n"
            f"  retention_days: {yaml_literal}\n"
        ),
    )
    expected = {
        "cron": {
            "max_concurrent": env_value,
            "retention_days": literal,
            "wrap_response": False,
        }
    }
    # Strictness validates the original root/section shape, not scheduler leaf domains.
    result = load_user_config_effective(home / "config.yaml", strict_section="cron")
    assert result == expected
    assert isinstance(result["cron"]["max_concurrent"], str)
    assert type(result["cron"]["retention_days"]) is type(literal)
    assert load_user_config_effective(home / "config.yaml") == expected
