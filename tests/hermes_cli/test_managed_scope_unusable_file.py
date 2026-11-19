"""An unusable managed config.yaml is no managed layer: the ordinary loaders stay fail-open.

Fork-only (G-CONFIG-SCOPE). These three cases came out of the fork's edits to upstream's
test_managed_scope_config.py and test_managed_scope_loaders.py, which are upstream's bytes again.
They pass against upstream's hermes_cli.managed_scope and hermes_cli.config_effective as they are,
and upstream has no test for a managed file of this shape.
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


def _write(path, body):
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    import hermes_cli.config as cfg
    from hermes_cli import managed_scope

    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    managed_scope.invalidate_managed_cache()


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


# The same shapes over the keys the standalone readers consume.
UNUSABLE_MANAGED_TIMEZONE = {
    "root-null": "null\n",
    "root-empty-file": "",
    "root-scalar": "just-a-scalar\n",
    "root-list": "- timezone\n- cron\n",
    "yaml-syntax-error": "timezone: [unterminated\n",
    "undecodable": b"timezone: \xff\n",
    "is-a-directory": None,
}


@pytest.mark.parametrize(
    "body", list(UNUSABLE_MANAGED_TIMEZONE.values()), ids=list(UNUSABLE_MANAGED_TIMEZONE)
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
