"""Opt-in fixture migration for the frozen managed-store acceptance test.

Load with ``-p tests.cron.g01c_managed_store_fixture``. The frozen test builds
``tmp_path/selected-venv`` and patches the parent's selector. Back that path
with a disposable committed generation so the real child can select it too,
without replacing the test function or weakening any of its assertions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def committed_frozen_managed_store(
    request: pytest.FixtureRequest, _isolate_hermes_home: None,
) -> None:
    if request.node.name != "test_posix_managed_store_script_runs_on_venv_with_live_checkout":
        return

    from cron import scheduler_script
    from pm.environments import install_state_dir, runtime_facts_path

    home = request.getfixturevalue("cron_env")
    tmp_path = request.getfixturevalue("tmp_path")
    repo = Path(scheduler_script.__file__).resolve().parents[1]
    state = install_state_dir(repo)
    assert state.resolve().is_relative_to(home.resolve())
    generation = state / "environments" / "frozen-managed-store"
    venv = generation / "venv"
    venv.mkdir(parents=True)
    (generation / ".lease-managed").touch()
    (tmp_path / "selected-venv").symlink_to(venv, target_is_directory=True)
    runtime_facts_path(repo).write_text(json.dumps(
        {"packages": {"venv": {"environment": str(venv)}}}), encoding="utf-8")
