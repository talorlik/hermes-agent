"""Every hermes_cli module must import cleanly from this checkout.

Fork-sync merges have twice left hermes_cli.update_cmd importing names upstream
had already retired (_clear_windows_venv_holders_or_exit from update_cmd_windows,
the deleted update_cmd_deps module) and hermes_cli._update_takeover calling
begin_update_receipt(previous=, correlation_id=) against a receipt copy that took
no arguments. Neither break surfaced in the targeted suites, because nothing
imported the broken module. This sweep does.

The sweep runs in one subprocess so a module that calls sys.exit at import
(psutil_android, an Android-only shim) cannot poison pytest. -I drops the
checkout off sys.path, so the sweep inserts this tree first; otherwise it would
import the venv's editable install of a different checkout. A scratch
HERMES_HOME keeps the sweep off the live profile. The rest of the environment is
inherited, and HERMES_DISABLE_LAZY_INSTALLS is set, because a stripped
environment makes an import-time dependency probe try to install and block.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Import-time exits that are the module's documented behaviour on this platform.
KNOWN_IMPORT_EXITS = {
    # Android-only psutil shim: refuses to load anywhere else via sys.exit(1).
    "hermes_cli.psutil_android": "SystemExit",
}

_SWEEP = r"""
import importlib, json, pkgutil, sys
sys.path.insert(0, sys.argv[1])
import hermes_cli
assert hermes_cli.__file__.startswith(sys.argv[1]), hermes_cli.__file__
failures = {}
names = sorted(m.name for m in pkgutil.walk_packages(hermes_cli.__path__, "hermes_cli."))
# psutil_android exits at import by handing off to upstream's updater child, which
# runs for minutes. Skip it here; the allowlist records that exit as expected.
skip = {"hermes_cli.psutil_android"}
for name in names:
    if name in skip:
        continue
    try:
        importlib.import_module(name)
    except BaseException as exc:  # SystemExit at import is a finding too
        failures[name] = f"{type(exc).__name__}: {exc}"
print(json.dumps({"count": len(names), "failures": failures}))
"""


def _run_sweep(hermes_home: Path) -> dict:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("GIT_CONFIG_GLOBAL", None)
    env["HERMES_HOME"] = str(hermes_home)
    env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    proc = subprocess.run(
        [sys.executable, "-I", "-c", _SWEEP, str(REPO_ROOT)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert proc.returncode == 0, f"sweep driver crashed:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def sweep(tmp_path_factory: pytest.TempPathFactory) -> dict:
    return _run_sweep(tmp_path_factory.mktemp("hermes-home"))


def test_every_hermes_cli_module_imports(sweep: dict) -> None:
    unexpected = {
        name: detail
        for name, detail in sweep["failures"].items()
        if not detail.startswith(KNOWN_IMPORT_EXITS.get(name, "\0"))
    }
    assert sweep["count"] > 400, sweep["count"]
    assert not unexpected, "hermes_cli modules failed to import:\n" + "\n".join(
        f"  {name}: {detail}" for name, detail in sorted(unexpected.items())
    )


def test_sweep_reports_an_injected_import_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The sweep must name the failing module, not stop at the first error."""
    broken = tmp_path / "hermes_cli_broken"
    pkg = broken / "hermes_cli"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "healthy.py").write_text("VALUE = 1\n", encoding="utf-8")
    (pkg / "broken.py").write_text("raise ImportError('injected break')\n", encoding="utf-8")
    code = (
        "import importlib, json, pkgutil, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import hermes_cli\n"
        "failures = {}\n"
        "names = sorted(m.name for m in pkgutil.walk_packages(hermes_cli.__path__, 'hermes_cli.'))\n"
        "for name in names:\n"
        "    try:\n"
        "        importlib.import_module(name)\n"
        "    except BaseException as exc:\n"
        "        failures[name] = f'{type(exc).__name__}: {exc}'\n"
        "print(json.dumps(failures))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", code, str(broken)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    failures = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "hermes_cli.broken" in failures
    assert "injected break" in failures["hermes_cli.broken"]
    assert "hermes_cli.healthy" not in failures


def test_takeover_receipt_api_accepts_previous() -> None:
    """_update_takeover.main calls begin_update_receipt(previous=, correlation_id=).

    The fork's stale update_receipt copy took no arguments, so the handoff raised
    TypeError after the import had already succeeded.
    """
    import inspect

    from hermes_cli import update_receipt

    params = inspect.signature(update_receipt.begin_update_receipt).parameters
    assert {"previous", "correlation_id"} <= set(params)
