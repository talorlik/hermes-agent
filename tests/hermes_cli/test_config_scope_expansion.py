"""Managed policy expansion keeps process authority across scopes and recovery."""

from __future__ import annotations

import builtins
import copy
import errno
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator

import pytest

SHARED = "G01C_SHARED_ENDPOINT"
USER = "G01C_USER_ENDPOINT"
ONLY = "G01C_PROFILE_ONLY"
CHAIN = "G01C_CHAIN"
SECOND = "G01C_SECOND"
BROKEN = "approvals: [unterminated\n"
LOADERS = ("managed", "effective", "ordinary", "readonly")


@dataclass
class ConfigWorld:
    path: Path
    managed_path: Path
    config: ModuleType
    effective: ModuleType
    managed: ModuleType
    scope: ModuleType
    yaml: ModuleType

    def write(self, user: dict[str, Any], managed: dict[str, Any]) -> None:
        self.path.write_text(self.yaml.safe_dump(user), encoding="utf-8")
        self.managed_path.write_text(self.yaml.safe_dump(managed), encoding="utf-8")

    def read(self, loader: str) -> dict[str, Any]:
        if loader == "managed":
            raw = self.yaml.safe_load(self.path.read_text(encoding="utf-8"))
            return self.managed._merge_managed(
                self.config._expand_env_vars(raw), self.managed.load_managed_config()
            )
        if loader == "effective":
            return self.effective.load_user_config_effective(self.path)
        if loader == "readonly":
            return self.config.load_config_readonly()
        return self.config.load_config()

    @contextmanager
    def bind(
        self, values: dict[str, str], *, mode: str = "multiplex"
    ) -> Iterator[None]:
        multiplex = self.scope.set_multiplex_context(mode == "multiplex")
        token = self.scope.set_secret_scope(
            values,
            profile_home=str(self.path.parent / "routed") if mode == "routed" else None,
        )
        try:
            yield
        finally:
            self.scope.reset_secret_scope(token)
            self.scope.reset_multiplex_context(multiplex)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ConfigWorld:
    home, managed = tmp_path / "home", tmp_path / "managed"
    home.mkdir()
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    # Real modules are bound after fixture isolation, never during collection.
    from agent import secret_scope
    from hermes_cli import config, config_effective, managed_scope
    import hermes_yaml

    for name in (SHARED, USER, ONLY, CHAIN, SECOND):
        monkeypatch.delenv(name, raising=False)
    return ConfigWorld(
        home / "config.yaml",
        managed / "config.yaml",
        config,
        config_effective,
        managed_scope,
        secret_scope,
        hermes_yaml,
    )


def _ref(syntax: str, name: str) -> str:
    return "${" + ("env:" if syntax == "env" else "") + name + "}"


@pytest.mark.parametrize("syntax", ("bare", "env"))
@pytest.mark.parametrize("loader", LOADERS)
@pytest.mark.parametrize("prime", (False, True), ids=("cold-scoped", "unscoped-primed"))
def test_layer_authority_survives_scope_and_process_rotation(
    world: ConfigWorld,
    monkeypatch: pytest.MonkeyPatch,
    syntax: str,
    loader: str,
    prime: bool,
) -> None:
    shared, user, only = (_ref(syntax, name) for name in (SHARED, USER, ONLY))
    authored = {
        "boundary": {
            "user": shared,
            "user_only": user,
            "user_nested": [{"value": shared}],
        }
    }
    policy = {
        "boundary": {
            "managed": shared,
            "profile_only": only,
            "nested": [{"value": shared}, shared, 7, False, None],
            "single_pass": _ref(syntax, CHAIN),
            "unsupported": "${vault:item}",
            "empty_ref": "${env:}",
            shared: "literal-key",
        }
    }
    world.write(authored, policy)
    monkeypatch.setenv(SHARED, "https://process-P1.invalid")
    monkeypatch.setenv(USER, "https://process-user.invalid")
    monkeypatch.setenv(CHAIN, _ref(syntax, SECOND))
    monkeypatch.setenv(SECOND, "must-not-expand")
    if prime:
        assert world.read(loader)["boundary"]["managed"] == "https://process-P1.invalid"

    observed, expected = [], []
    for profile in ("A", "B", "A"):
        values = {
            SHARED: f"https://profile-{profile}.invalid",
            USER: f"https://user-{profile}.invalid",
            ONLY: "https://profile-only.invalid",
            CHAIN: "profile-chain",
        }
        with world.bind(values):
            for process in (
                "https://process-P1.invalid",
                "https://process-P2.invalid",
                None,
                "",
                "https://process-P1.invalid",
            ):
                if process is None:
                    monkeypatch.delenv(SHARED)
                else:
                    monkeypatch.setenv(SHARED, process)
                result = world.read(loader)
                leaf = result["boundary"]
                observed.append(copy.deepcopy(leaf))
                want = copy.deepcopy(policy["boundary"])
                want.update(authored["boundary"])
                want.update(
                    user=values[SHARED],
                    user_only=values[USER],
                    user_nested=[{"value": values[SHARED]}],
                    managed=shared if process is None else process,
                    profile_only=only,
                    nested=[
                        {"value": shared if process is None else process},
                        shared if process is None else process,
                        7,
                        False,
                        None,
                    ],
                    single_pass=_ref(syntax, SECOND),
                )
                expected.append(want)
                again = world.read(loader)
                if loader == "readonly":
                    assert again is result
                elif loader in ("effective", "ordinary"):
                    assert again == result and again is not result
                    result["boundary"]["nested"].append("caller-mutation")
                    assert world.read(loader) == again
    assert world.managed.load_managed_config() == policy
    assert (
        world.yaml.safe_load(world.managed_path.read_text(encoding="utf-8")) == policy
    )
    assert observed == expected


@pytest.mark.parametrize("syntax", ("bare", "env"))
@pytest.mark.parametrize("loader", LOADERS)
@pytest.mark.parametrize("mode", ("multiplex", "routed", "legacy"))
def test_empty_scope_user_fallback_does_not_change_managed_authority(
    world: ConfigWorld,
    monkeypatch: pytest.MonkeyPatch,
    syntax: str,
    loader: str,
    mode: str,
) -> None:
    ref = _ref(syntax, SHARED)
    world.write({"boundary": {"user": ref}}, {"boundary": {"managed": ref}})
    monkeypatch.setenv(SHARED, "https://process.invalid")
    with world.bind({}, mode=mode):
        result = world.read(loader)["boundary"]
        assert result["managed"] == "https://process.invalid"
        assert result["user"] == (
            "https://process.invalid" if mode == "legacy" else ref
        )


@pytest.mark.parametrize("syntax", ("bare", "env"))
@pytest.mark.parametrize("loader", ("effective", "ordinary", "readonly"))
@pytest.mark.parametrize("recovery", ("memory", "backup", "none", "read-error"))
def test_recovery_retains_authority_and_validates_each_layers_dependencies(
    world: ConfigWorld,
    monkeypatch: pytest.MonkeyPatch,
    syntax: str,
    loader: str,
    recovery: str,
) -> None:
    from hermes_cli.config_backups import backup_config
    from hermes_cli.config_read_errors import FailedConfigRead
    from utils import file_signature

    shared, only = (_ref(syntax, name) for name in (SHARED, ONLY))
    user = {"boundary": {"user": shared}, "approvals": {"deny": ["curl*evil*"]}}
    policy = {
        "boundary": {
            "managed": shared,
            "profile_only": only,
            "single_pass": _ref(syntax, CHAIN),
        }
    }
    world.write(user, policy)
    monkeypatch.setenv(SHARED, "https://process-P1.invalid")
    monkeypatch.setenv(CHAIN, _ref(syntax, SECOND))
    monkeypatch.setenv(SECOND, "must-not-expand")
    if recovery == "memory":
        assert world.read(loader)["approvals"]["deny"] == ["curl*evil*"]
    elif recovery in ("backup", "read-error"):
        assert backup_config(world.path, "good") is not None

    failed_open = [True]
    if recovery == "read-error":

        def open_with_fault(file: Any, *args: Any, **kwargs: Any) -> Any:
            if failed_open[0] and str(file) == str(world.path):
                raise OSError(errno.EIO, "synthetic config read failure")
            return builtins.open(file, *args, **kwargs)

        monkeypatch.setattr(world.config, "open", open_with_fault, raising=False)
        monkeypatch.setattr(world.effective, "open", open_with_fault, raising=False)
    else:
        world.path.write_text(BROKEN, encoding="utf-8")
    failed_signature = file_signature(world.path.stat())
    observed, expected = [], []
    for profile in ("A", "B", "A"):
        with world.bind({
            SHARED: f"https://profile-{profile}.invalid",
            ONLY: "https://profile-only.invalid",
            SECOND: "profile-second",
        }):
            for process in (
                "https://process-P1.invalid",
                "https://process-P2.invalid",
                None,
                "",
                "https://process-P1.invalid",
            ):
                if process is None:
                    monkeypatch.delenv(SHARED)
                else:
                    monkeypatch.setenv(SHARED, process)
                result = world.read(loader)
                leaf = result["boundary"]
                observed.append(copy.deepcopy(leaf))
                want = {
                    "managed": shared if process is None else process,
                    "profile_only": only,
                    "single_pass": _ref(syntax, SECOND),
                }
                if recovery != "none":
                    want["user"] = f"https://profile-{profile}.invalid"
                    assert result["approvals"]["deny"] == ["curl*evil*"]
                expected.append(want)
                if loader != "effective":
                    assert isinstance(result, FailedConfigRead)
                    again = world.read(loader)
                    if loader == "readonly":
                        assert again is result
                    with pytest.raises(RuntimeError, match="not saved"):
                        world.config.save_config(result)
                else:
                    with pytest.raises((world.yaml.YAMLError, OSError)):
                        world.effective.load_user_config_effective(
                            world.path, fail_closed=True
                        )
    assert file_signature(world.path.stat()) == failed_signature
    assert world.managed.load_managed_config() == policy
    assert observed == expected
    if recovery == "read-error":
        failed_open[0] = False
        assert type(world.read(loader)) is dict


@pytest.mark.parametrize("layer", ("user", "managed"))
@pytest.mark.parametrize("malformed", ("root", "section", "parse"))
def test_strict_shape_provenance_survives_scope_cache_rebuilds(
    world: ConfigWorld,
    monkeypatch: pytest.MonkeyPatch,
    layer: str,
    malformed: str,
) -> None:
    ref = _ref("env", SHARED)
    world.write({"boundary": {"user": ref}}, {"boundary": {"managed": ref}})
    path = world.path if layer == "user" else world.managed_path
    path.write_text(
        {
            "root": "null\n",
            "section": "boundary: null\n",
            "parse": BROKEN,
        }[malformed],
        encoding="utf-8",
    )
    monkeypatch.setenv(SHARED, "https://process.invalid")
    error = {
        "root": world.effective.ConfigRootNotMappingError,
        "section": world.effective.ConfigSectionNotMappingError,
        "parse": (
            world.yaml.YAMLError
            if layer == "user"
            else world.effective.ConfigLayerUnreadableError
        ),
    }[malformed]
    for profile in ("A", "B", "A"):
        with world.bind({SHARED: f"https://profile-{profile}.invalid"}):
            permissive = world.read("effective")
            with pytest.raises(error):
                world.effective.load_user_config_effective(
                    world.path,
                    strict_section="boundary",
                )
            assert world.read("effective") == permissive


@pytest.mark.parametrize("syntax", ("bare", "env"))
def test_save_keeps_user_templates_and_recovery_keeps_saved_user_provenance(
    world: ConfigWorld,
    monkeypatch: pytest.MonkeyPatch,
    syntax: str,
) -> None:
    ref = _ref(syntax, USER)
    world.write(
        {"boundary": {"user": ref}, "approvals": {"deny": ["curl*evil*"]}},
        {"boundary": {"managed": _ref(syntax, ONLY)}},
    )
    with world.bind({USER: "https://user-A.invalid"}):
        loaded = world.config.load_config()
        with world.bind({USER: "https://user-B.invalid"}):
            loaded["display"]["skin"] = "mono"
            world.config.save_config(loaded)
        raw = world.yaml.safe_load(world.path.read_text(encoding="utf-8"))
        assert raw["boundary"]["user"] == ref
        assert "managed" not in raw["boundary"]
    world.path.write_text(BROKEN, encoding="utf-8")
    with world.bind({USER: "https://user-C.invalid", ONLY: "profile-only"}):
        recovered = world.config.load_config()
        assert recovered["boundary"]["user"] == "https://user-C.invalid"
        assert recovered["boundary"]["managed"] == _ref(syntax, ONLY)
        assert recovered["approvals"]["deny"] == ["curl*evil*"]
        assert recovered["display"]["skin"] == "mono"


@pytest.mark.parametrize("loader", ("ordinary", "readonly"))
@pytest.mark.parametrize("state", ("cold", "warm", "memory", "backup", "none"))
def test_managed_read_retries_without_blessing_partial_load(
    world: ConfigWorld,
    monkeypatch: pytest.MonkeyPatch,
    loader: str,
    state: str,
) -> None:
    from hermes_cli.config_backups import backup_config
    from hermes_cli.config_read_errors import FailedConfigRead
    from utils import file_signature

    user = {
        "boundary": {"user": _ref("env", SHARED)},
        "approvals": {"deny": ["user-deny"]},
        "security": {"redact_secrets": False},
    }
    policy = {
        "boundary": {"managed": _ref("env", SHARED)},
        "approvals": {"deny": ["admin-deny"]},
    }
    world.write(user, {})
    monkeypatch.setenv(SHARED, "https://process-P1.invalid")
    with world.bind({SHARED: "https://profile-A.invalid"}):
        if state in ("warm", "memory"):
            assert world.read(loader)["approvals"]["deny"] == ["user-deny"]
        elif state == "backup":
            assert backup_config(world.path, "good") is not None
        # Warm entries must be invalidated by a real policy edit before the failed read.
        world.managed_path.write_text(world.yaml.safe_dump(policy), encoding="utf-8")
        if state in ("memory", "backup", "none"):
            world.path.write_text(BROKEN, encoding="utf-8")
        signatures = tuple(
            file_signature(path.stat()) for path in (world.path, world.managed_path)
        )
        real_open = Path.open
        failing = True
        attempts = 0

        def open_managed(path: Path, *args: Any, **kwargs: Any) -> Any:
            nonlocal attempts
            if path == world.managed_path:
                attempts += 1
                if failing:
                    raise OSError(errno.EIO, "synthetic managed read failure")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", open_managed)
        for _ in range(2):
            partial = world.read(loader)
            assert "managed" not in partial.get("boundary", {})
            if state != "none":
                assert partial["approvals"]["deny"] == ["user-deny"]
                assert partial["security"]["redact_secrets"] is False
                assert partial["boundary"]["user"] == "https://profile-A.invalid"
        assert attempts > 0  # The failure occurs at the actual managed file read.
        failing = False
        # Same scope, env and file metadata: success must be retried without another trigger.
        recovered = world.read(loader)
        assert recovered["approvals"]["deny"] == ["admin-deny"]
        assert recovered["boundary"]["managed"] == "https://process-P1.invalid"
        for process in ("https://process-P2.invalid", "", "https://process-P1.invalid"):
            monkeypatch.setenv(SHARED, process)
            recovered = world.read(loader)
            assert recovered["boundary"]["managed"] == process
            assert recovered["approvals"]["deny"] == ["admin-deny"]
            if state != "none":
                assert recovered["boundary"]["user"] == "https://profile-A.invalid"
                assert recovered["security"]["redact_secrets"] is False
            else:
                assert "user" not in recovered["boundary"]
            reads = attempts
            again = world.read(loader)
            assert attempts == reads
            if loader == "readonly":
                assert again is recovered
            else:
                assert again == recovered and again is not recovered
            if state in ("memory", "backup", "none"):
                assert isinstance(recovered, FailedConfigRead)
                with pytest.raises(RuntimeError, match="not saved"):
                    world.config.save_config(recovered)
        assert (
            tuple(
                file_signature(path.stat()) for path in (world.path, world.managed_path)
            )
            == signatures
        )


@pytest.mark.parametrize("raw_reader", ("read_raw_config", "read_raw_config_readonly"))
@pytest.mark.parametrize("backup", (False, True), ids=("no-backup", "older-backup"))
def test_effective_raw_cache_success_refreshes_recovery_provenance(
    world: ConfigWorld,
    monkeypatch: pytest.MonkeyPatch,
    raw_reader: str,
    backup: bool,
) -> None:
    from hermes_cli.config_backups import load_newest_good_backup
    from utils import file_signature

    # The alternate filename exercises the same raw-cache seam without creating backups.
    if not backup:
        world.path = world.path.with_name("without-backup.yaml")
        monkeypatch.setattr(world.config, "get_config_path", lambda: world.path)
        # Effective reads of an inactive file intentionally do not write a backup.
        active_path = world.path.parent / "config.yaml"
    else:
        active_path = world.path
    user = {
        "approvals": {"deny": ["old-rule"]},
        "boundary": {"user": _ref("env", SHARED)},
        "security": {"redact_secrets": False},
    }
    policy = {"boundary": {"managed": _ref("env", SHARED)}}
    world.write(user, policy)
    monkeypatch.setenv(SHARED, "https://process-P1.invalid")
    with world.bind({SHARED: "https://profile-A.invalid"}):
        with monkeypatch.context() as active:
            active.setattr(world.config, "get_config_path", lambda: active_path)
            assert world.read("effective")["approvals"]["deny"] == ["old-rule"]
        old_backup = load_newest_good_backup(world.path)
        assert (old_backup is not None) is backup
        user["approvals"]["deny"].append("new-rule")
        world.path.write_text(world.yaml.safe_dump(user), encoding="utf-8")
        raw = getattr(world.config, raw_reader)()
        assert raw["approvals"]["deny"] == ["old-rule", "new-rule"]
        fresh = world.read("effective")
        assert fresh["approvals"]["deny"] == ["old-rule", "new-rule"]
        # A mutable caller owns its result; expansion/recovery must also own their copies.
        fresh["approvals"]["deny"].clear()
        if raw_reader == "read_raw_config":
            raw["approvals"]["deny"].clear()
        assert world.config.read_raw_config()["approvals"]["deny"] == [
            "old-rule",
            "new-rule",
        ]
        assert (
            world.effective._LAST_GOOD_USER_RAW[str(world.path)]
            is not (world.config._RAW_CONFIG_CACHE[str(world.path)][4])
        )
        world.path.write_text(BROKEN, encoding="utf-8")
        signature = file_signature(world.path.stat())
        for process in ("https://process-P1.invalid", "https://process-P2.invalid", ""):
            monkeypatch.setenv(SHARED, process)
            recovered = world.read("effective")
            assert recovered["approvals"]["deny"] == ["old-rule", "new-rule"]
            assert recovered["security"]["redact_secrets"] is False
            assert recovered["boundary"] == {
                "user": "https://profile-A.invalid",
                "managed": process,
            }
            recovered["approvals"]["deny"].clear()
            with pytest.raises(world.yaml.YAMLError):
                world.effective.load_user_config_effective(world.path, fail_closed=True)
            with pytest.raises(RuntimeError, match="not saved"):
                world.config.save_config(recovered)
        assert file_signature(world.path.stat()) == signature
        assert load_newest_good_backup(world.path) == old_backup
