"""Process-boundary guard: no ``hermes_cli`` test may re-exec the real owning install.

``retarget_to_owning_install`` launches ``<owner>/venv python -m hermes_cli.main <sys.argv[1:]>`` with the
owning install as cwd. Under pytest that argv is pytest's own, and for an update test it could one day be a
real ``update``. The suite-wide fixture in ``conftest.py`` removes the seam; these tests prove it holds and
that the production function still redirects when it is allowed to.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import update_owning_install


def test_default_fixture_makes_the_owner_lookup_inert(tmp_path):
    assert update_owning_install.owning_install_root(tmp_path) is None


def test_retarget_returns_without_spawning_anything(tmp_path, monkeypatch):
    spawned = []
    monkeypatch.setattr(subprocess, "call", lambda *a, **k: spawned.append((a, k)) or 0)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append((a, k)))

    update_owning_install.retarget_to_owning_install(tmp_path)

    assert spawned == []


def test_production_retarget_still_redirects_to_a_real_owner(tmp_path, monkeypatch):
    """With the seam allowed, the redirect is intact: owner as cwd and PYTHONPATH, argv forwarded."""
    owner = tmp_path / "owner"
    (owner / "hermes_cli").mkdir(parents=True)
    calls = []
    monkeypatch.setattr(update_owning_install, "owning_install_root", lambda root: owner)
    monkeypatch.setattr(subprocess, "call", lambda argv, cwd, env: calls.append((argv, cwd, env)) or 7)
    monkeypatch.setattr(sys, "argv", ["hermes", "update"])

    with pytest.raises(SystemExit) as raised:
        update_owning_install.retarget_to_owning_install(tmp_path / "dev-tree")

    assert raised.value.code == 7
    [(argv, cwd, env)] = calls
    assert argv[1:] == ["-m", "hermes_cli.main", "update"]
    assert Path(cwd) == owner and env["PYTHONPATH"] == str(owner)
