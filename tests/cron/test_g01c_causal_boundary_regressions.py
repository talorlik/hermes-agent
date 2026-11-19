"""Cron script interpreter selection on a PM-managed install, and the served-profile env control.

These are the tests of the fork's G01C causal-boundary file that pass against the current tree.
The cron script runner starts the interpreter selected at argv-build time and takes no lease:
startup ``.pth`` hooks run without one, argv names the selected interpreter with lazy installs
off, and a script keeps running on the generation it imported even after a collector removes it.
The control proves a ``-S`` child given only ``served_profile_child_env`` cannot import
``hermes_cli`` from outside the checkout.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import cron.scheduler_delivery as delivery


LAUNCH_ENV = {
    "HERMES_MODEL": "root-model",
    "HERMES_LANGUAGE": "en",
    "TERMINAL_ENV": "docker",
    "TERMINAL_DOCKER_IMAGE": "root-only-image",
    "DISCORD_ALLOWED_USERS": "root-operator",  # an authorization gate (#113270)
    "DISCORD_IGNORED_CHANNELS": "999",
    "ANTHROPIC_API_KEY": "sk-root",
}


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """A root-profile gateway (fresh tmp HERMES_HOME) and a second profile to deliver into."""
    root = tmp_path / "hermes"
    beta = root / "profiles" / "beta"
    beta.mkdir(parents=True)
    (root / ".env").write_text(
        "\n".join(f"{key}={value}" for key, value in LAUNCH_ENV.items()) + "\n", encoding="utf-8")
    (beta / ".env").write_text("ANTHROPIC_API_KEY=sk-beta\nHERMES_LANGUAGE=ja\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    for key, value in LAUNCH_ENV.items():
        monkeypatch.setenv(key, value)
    return root, beta


_HERMES_CLI_ORIGIN_PROBE = (
    "import importlib.util, sys; spec = importlib.util.find_spec('hermes_cli'); "
    "sys.exit(3) if spec is None or not spec.origin else print(spec.origin)"
)


def _repo_root():
    from pathlib import Path
    return Path(delivery.__file__).resolve().parents[1]


def _probe_hermes_cli_import(env: dict, cwd) -> subprocess.CompletedProcess:
    """Real child under ``-S``: no site-packages, no ``.pth``; only *env* and *cwd* can supply
    ``hermes_cli``. Runs from *cwd* (the target home), as the delivery child's destination is."""
    import sys
    return subprocess.run(
        [sys.executable, "-S", "-c", _HERMES_CLI_ORIGIN_PROBE],
        env=env, cwd=str(cwd), capture_output=True, text=True, timeout=120, check=False)


def test_bot_chat_served_profile_env_alone_cannot_import_hermes_cli_outside_checkout_control(fleet):
    """Negative control: the env ``served_profile_child_env`` builds for the target, with no
    overlay, does not let a ``-S`` child in the target home find ``hermes_cli``. Proves the probe
    is sensitive: a GREEN regression below is evidence of the delivered import path, not of the
    host's site installs.

    Expected PASS before and after the repair.
    """
    import os
    from tools.environments import local as local_env

    _root, beta = fleet
    env = local_env.served_profile_child_env(target_home=beta, inherit_credentials=True)
    assert str(_repo_root()) not in (env.get("PYTHONPATH") or "").split(os.pathsep), (
        "the scrub left the checkout on PYTHONPATH; this control cannot discriminate")

    probe = _probe_hermes_cli_import(env, beta)

    assert probe.returncode != 0, (
        f"probe found hermes_cli at {probe.stdout.strip()!r} with no delivered import path; "
        "the -S probe is not sensitive on this host")


@pytest.fixture
def pm_script_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a ``scripts/`` dir; PM install state for this checkout lives
    beneath it (``pm.environments.install_state_dir``), so nothing here touches a live root."""
    home = tmp_path / ".hermes"
    (home / "scripts").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("PYTHONPATH", raising=False)
    return home


def _lease_managed_generation(generations, name: str, *, with_venv: bool):
    """A disposable lease-managed PM generation directory under the test's install state."""
    import sys
    from pathlib import Path

    generation = generations / name
    generation.mkdir(parents=True)
    (generation / ".lease-managed").touch()
    if with_venv:
        venv = generation / "venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").symlink_to(sys.executable)
        (venv / "pyvenv.cfg").write_text(
            f"home = {Path(sys.base_prefix) / 'bin'}\ninclude-system-site-packages = false\n",
            encoding="utf-8")
    return generation


@pytest.mark.platforms("posix")
def test_pm_cron_script_startup_pth_does_not_require_a_lease(
    pm_script_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upstream starts the selected interpreter, so .pth runs without a Hermes lease."""
    import json
    import sys
    from pathlib import Path

    from cron import scheduler_script
    from hermes_cli.runtime_state import leases_held
    from pm.environments import install_state_dir, runtime_facts_path, site_packages

    # The canonical runner scrubs inherited variables, so set these in the test
    # as well: both the startup hook and activation run without host Git config.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    repo = Path(scheduler_script.__file__).resolve().parents[1]
    generation = _lease_managed_generation(
        install_state_dir(repo) / "environments", "startup", with_venv=True)
    deps = site_packages(generation / "venv")
    deps.mkdir(parents=True)
    runtime_facts_path(repo).write_text(json.dumps(
        {"packages": {"venv": {"environment": str(generation / "venv")}}}),
        encoding="utf-8")
    events = pm_script_home / "startup-events.jsonl"
    body = pm_script_home / "body-ran"
    (deps / "lease_startup_probe.py").write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "before_activation = 'pm.environments' not in sys.modules\n"
        f"sys.path.insert(0, {str(repo)!r})\n"
        "from hermes_cli.runtime_state import leases_held\n"
        f"held = leases_held(Path({str(generation)!r}))\n"
        f"with open({str(events)!r}, 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'held': held, 'before_activation': before_activation, "
        "'git_global': os.environ.get('GIT_CONFIG_GLOBAL'), "
        "'git_system': os.environ.get('GIT_CONFIG_SYSTEM')}) + '\\n')\n",
        encoding="utf-8")
    (deps / "startup.pth").write_text("import lease_startup_probe\n", encoding="utf-8")
    script = pm_script_home / "scripts" / "startup_probe.py"
    script.write_text(
        "from pathlib import Path\n"
        f"Path({str(body)!r}).touch()\n"
        "print('leased startup completed')\n", encoding="utf-8")
    monkeypatch.setattr(
        "hermes_cli._launchers.resolve_store_python", lambda repo: Path(sys.executable))

    success, output = scheduler_script._run_job_script(script.name)
    observations = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    assert success is True, (
        f"{output}; startup={observations}; script_body_ran={body.exists()}")
    assert observations == [{
        "held": False, "before_activation": True,
        "git_global": "/dev/null", "git_system": "/dev/null",
    }]
    assert body.is_file()
    assert output == "leased startup completed"
    assert leases_held(generation) is False


@pytest.mark.platforms("posix")
def test_pm_cron_script_uses_the_selected_interpreter_not_a_collectable_store_bootstrap(
    pm_script_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upstream runs the selected interpreter at argv-build time.

    The old store-python bootstrap leased the generation at spawn. That
    signature does not align with the current upstream function, so the
    integrate gate will not publish it. Pin the contract that does publish:
    argv is the selected interpreter, and lazy installs are off.
    """
    import sys
    from pathlib import Path

    from cron import scheduler_script
    from pm.environments import install_state_dir, project_python, runtime_facts_path, site_packages

    repo = Path(scheduler_script.__file__).resolve().parents[1]
    generation = _lease_managed_generation(
        install_state_dir(repo) / "environments", "selected", with_venv=True)
    site_packages(generation / "venv").mkdir(parents=True)
    runtime_facts_path(repo).write_text(
        '{"packages": {"venv": {"environment": "%s"}}}' % (generation / "venv"),
        encoding="utf-8")
    monkeypatch.setattr(
        "hermes_cli._launchers.resolve_store_python", lambda repo: Path(sys.executable))
    script = pm_script_home / "scripts" / "selection_probe_script.py"
    script.write_text("print('selected')\n", encoding="utf-8")

    argv, env_overlay = scheduler_script._posix_cron_script_argv(script)

    assert argv[0] == str(project_python(repo))
    assert env_overlay == {"HERMES_DISABLE_LAZY_INSTALLS": "1"}
    assert argv[2] == scheduler_script._POSIX_SCRIPT_BOOTSTRAP


@pytest.mark.platforms("posix")
def test_pm_cron_script_runs_on_the_interpreter_selected_at_start(
    pm_script_home, monkeypatch,
):
    """The published runner uses the interpreter selected at start.

    It does not take a lease. A collector may remove that generation after
    the script selects another one. The script still finishes on the import
    it already made, and PYTHONPATH stays empty.
    """
    import json
    import sys
    import textwrap
    from pathlib import Path

    from cron import scheduler_script
    from hermes_cli.runtime_state import leases_held
    from pm.environments import install_state_dir, runtime_facts_path, site_packages

    repo_root = Path(scheduler_script.__file__).resolve().parents[1]
    state = install_state_dir(repo_root)
    assert state.resolve().is_relative_to(pm_script_home.resolve()), state
    generations = state / "environments"
    _lease_managed_generation(generations, "gen-a", with_venv=False)
    gen_b = _lease_managed_generation(generations, "gen-b", with_venv=True)
    gen_c = _lease_managed_generation(generations, "gen-c", with_venv=True)
    deps = site_packages(gen_b / "venv")
    deps.mkdir(parents=True)
    (deps / "probe_pkg.py").write_text("VALUE = 'generation-b'\n", encoding="utf-8")
    facts = runtime_facts_path(repo_root)
    facts.write_text(json.dumps(
        {"packages": {"venv": {"environment": str(gen_b / "venv")}}}), encoding="utf-8")
    monkeypatch.setattr(
        "hermes_cli._launchers.resolve_store_python", lambda repo: Path(sys.executable))

    script = pm_script_home / "scripts" / "lease_probe.py"
    script.write_text(textwrap.dedent(f"""\
        import json, os, subprocess, sys
        import probe_pkg
        # An installer commits generation C while this script is still running on B.
        with open({str(facts)!r}, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(
                {{"packages": {{"venv": {{"environment": {str(gen_c / "venv")!r}}}}}}}))
        # The collector is its own process, as PM maintenance is; it must find B leased.
        collector = subprocess.run([sys.executable, "-c", (
            "import json, sys; sys.path.insert(0, {str(repo_root)!r}); "
            "from pathlib import Path; "
            "from hermes_cli.runtime_state import collect_generations, leases_held; "
            "held = leases_held(Path({str(gen_b)!r})); "
            "removed = collect_generations(Path({str(repo_root)!r}), min_age_seconds=0); "
            "print(json.dumps({{'held': held, 'removed': sorted(p.name for p in removed)}}))")],
            capture_output=True, text=True)
        print(json.dumps({{
            "value": probe_pkg.VALUE,
            "pkg_in_b": os.path.abspath(probe_pkg.__file__).startswith({str(gen_b.resolve())!r}),
            "path0": sys.path[0],
            "pythonpath": os.environ.get("PYTHONPATH", ""),
            "collector_rc": collector.returncode,
            "collector_out": collector.stdout.strip(),
            "collector_err": collector.stderr.strip()[-800:],
        }}))
        """), encoding="utf-8")

    success, output = scheduler_script._run_job_script("lease_probe.py")
    assert success is True, output
    report = json.loads(output)
    assert report["value"] == "generation-b"
    assert report["pkg_in_b"] is True, "script did not import from the committed generation"
    assert Path(report["path0"]).resolve() == script.parent.resolve()
    assert report["pythonpath"] == ""
    assert report["collector_rc"] == 0, report["collector_err"]
    collector = json.loads(report["collector_out"])
    # Upstream runs the selected interpreter directly. It does not take a
    # lease, so a collector may remove B after the script selects C. The
    # script still finishes because it already imported B.
    assert collector["held"] is False
    assert "gen-a" in collector["removed"]
    assert leases_held(gen_b) is False
