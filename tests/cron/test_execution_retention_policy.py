"""Execution-ledger retention policy.

Default: each job keeps its newest ``PER_JOB_TERMINAL_EXECUTIONS`` terminal attempts plus
everything that finished inside the 30-day floor; there is no ledger-wide bound. An explicit
``cron.max_terminal_executions`` in the config of the profile that owns the ledger adds a fair
ledger-wide ceiling that overrides the floor. Claimed, running, detached-running and deferred rows
are never retention candidates.

A finish stamp that is missing (legacy NULL) is aged from the claim instant; one that is present
but unreadable is not, because an old claim does not prove an old finish.

Every configured value here goes through a real ``config.yaml`` beside the ledger and the resolver
is never patched. The only patched policy inputs are the production constants, including
``MAX_TERMINAL_EXECUTIONS``, the ceiling used when the profile's file does not carry the key.

"Does not carry the key" is a statement about a file that is a mapping whose ``cron`` section, if
present, is a mapping too. A root or a ``cron`` section of any other shape (an empty file and an
explicit null included) cannot say whether the key is absent, so it retains evidence exactly as
an unparseable file does, whichever reader warmed the process's config caches first.

The managed layer is held to the same standard. No managed file is a valid empty layer; one that
is present but is not such a mapping, or cannot be read or parsed, is not "no managed layer" for
retention, although it stays exactly that for every ordinary reader.

No file beside the ledger is a valid empty layer only when the file is known to be missing. One
that cannot be examined (its stat is refused) is not a missing file, and retains evidence.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

import hermes_yaml
from hermes_time import now as _now

_TERMINAL = ("completed", "failed", "unknown")


def _home(tmp_path: Path, name: str = "home", cap_yaml: str | None = None) -> Path:
    """A profile home; ``cap_yaml`` is the literal YAML scalar written for the ceiling key."""
    home = tmp_path / name
    home.mkdir()
    if cap_yaml is not None:
        (home / "config.yaml").write_text(
            f"cron:\n  max_terminal_executions: {cap_yaml}\n", encoding="utf-8"
        )
    return home


def _ledger(monkeypatch, home: Path):
    import cron.executions as executions

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", home / "cron" / "executions.db")
    executions.list_executions()  # creates the schema so rows can be seeded directly
    return executions


def _seed(path: Path, rows) -> None:
    """Insert ``(id, job_id, status, claimed_at, finished_at)`` rows verbatim."""
    conn = sqlite3.connect(path)
    try:
        conn.executemany(
            "INSERT INTO executions (id, job_id, source, process_id, pid, status,"
            " claimed_at, finished_at) VALUES (?, ?, 'builtin', 'seed', 1, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _ids(path: Path, *, terminal_only: bool = True) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT id, status FROM executions").fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows if not terminal_only or row[1] in _TERMINAL}


def _prune(executions) -> None:
    """Run retention exactly as a terminal write does, without adding a row."""
    with executions._transaction() as conn:
        executions._prune_unlocked(conn)


def _ago(**delta) -> str:
    return (_now() - timedelta(**delta)).isoformat()


def _finish_new(executions, job_id: str) -> str:
    row = executions.create_execution(job_id, source="builtin")
    assert executions.finish_execution(row["id"], success=True) is not None
    return row["id"]


# --- configuration: default, disabled, enabled, invalid ------------------------------------------


def _shipped_default_yaml() -> str:
    from hermes_cli.config import DEFAULT_CONFIG

    # The value the product ships, serialized the way a user would write it.
    return hermes_yaml.safe_dump(DEFAULT_CONFIG["cron"]["max_terminal_executions"]).split(
        "\n"
    )[0]


@pytest.mark.parametrize("cap_yaml", [None, "null", "~", "shipped-default"])
def test_no_ceiling_keeps_recent_history_past_any_ledger_wide_count(
    monkeypatch, tmp_path, cap_yaml
):
    """Absent key, explicit null and the shipped default all leave the floor in force."""
    if cap_yaml == "shipped-default":
        cap_yaml = _shipped_default_yaml()
    home = _home(tmp_path, cap_yaml=cap_yaml)
    executions = _ledger(monkeypatch, home)
    recent = _ago(days=1)
    # One more recent row than the former ledger-wide bound, spread over distinct jobs.
    seeded = [
        (f"r-{i:04d}", f"job-{i:04d}", "completed", recent, recent) for i in range(1001)
    ]
    _seed(executions.EXECUTIONS_FILE, seeded)

    _finish_new(executions, "trigger")

    assert {row[0] for row in seeded} <= _ids(executions.EXECUTIONS_FILE)


def test_enabled_ceiling_bounds_the_ledger_and_overrides_the_floor(
    monkeypatch, tmp_path
):
    home = _home(tmp_path, cap_yaml="3")
    executions = _ledger(monkeypatch, home)
    rows = [
        (f"r-{i}", f"job-{i}", "completed", _ago(hours=10 - i), _ago(hours=10 - i))
        for i in range(8)
    ]
    _seed(executions.EXECUTIONS_FILE, rows)

    _prune(executions)

    # Every row is hours old, far inside the 30-day floor; the explicit ceiling still wins and
    # keeps the three most recently finished.
    assert _ids(executions.EXECUTIONS_FILE) == {"r-5", "r-6", "r-7"}


def test_zero_ceiling_removes_terminal_history_through_the_public_write_path(
    monkeypatch, tmp_path
):
    home = _home(tmp_path, cap_yaml="0")
    executions = _ledger(monkeypatch, home)
    _seed(
        executions.EXECUTIONS_FILE,
        [("old", "job-a", "failed", _ago(days=2), _ago(days=2))],
    )

    _finish_new(executions, "job-b")

    assert _ids(executions.EXECUTIONS_FILE) == set()


@pytest.mark.parametrize(
    "cap_yaml",
    ["true", "false", "-1", "2.5", '"2"', "two", "[2]", "{limit: 2}", "1e3", "' 2 '"],
)
def test_invalid_ceiling_retains_evidence_and_says_why(
    monkeypatch, tmp_path, caplog, cap_yaml
):
    """A bool, a non-integer, a negative or a numeric string never becomes a ceiling."""
    home = _home(tmp_path, cap_yaml=cap_yaml)
    executions = _ledger(monkeypatch, home)
    recent = _ago(hours=1)
    rows = [(f"r-{i}", f"job-{i}", "completed", recent, recent) for i in range(5)]
    _seed(executions.EXECUTIONS_FILE, rows)

    with caplog.at_level(logging.WARNING, logger="cron.executions"):
        _finish_new(executions, "trigger")

    assert {f"r-{i}" for i in range(5)} <= _ids(executions.EXECUTIONS_FILE)
    assert any(
        "max_terminal_executions" in record.getMessage()
        and str(home / "config.yaml") in record.getMessage()
        for record in caplog.records
    ), [record.getMessage() for record in caplog.records]


def test_unreadable_config_retains_evidence(monkeypatch, tmp_path):
    home = _home(tmp_path)
    (home / "config.yaml").write_text("cron: [unterminated\n", encoding="utf-8")
    executions = _ledger(monkeypatch, home)
    recent = _ago(hours=1)
    _seed(
        executions.EXECUTIONS_FILE,
        [(f"r-{i}", f"job-{i}", "completed", recent, recent) for i in range(4)],
    )

    _finish_new(executions, "trigger")

    assert {f"r-{i}" for i in range(4)} <= _ids(executions.EXECUTIONS_FILE)


# --- fallback constant: used only when the owning profile's file does not carry the key ----------

_ABSENT = object()
_ALL_FIVE = {f"r-{i}" for i in range(5)}


def _spread_rows(count: int):
    """``count`` completed rows, one per job, all inside the floor; ``r-0`` finished last."""
    return [
        (f"r-{i}", f"job-{i}", "completed", _ago(hours=i + 2), _ago(hours=i + 1))
        for i in range(count)
    ]


def _raw_ceiling(home: Path):
    """The key as the product's default-free reader reports it; ``_ABSENT`` when the file does not
    carry it. ``DEFAULT_CONFIG`` is not merged here, so absent and explicit null stay distinct."""
    from hermes_cli.config_effective import load_user_config_effective

    cron_cfg = load_user_config_effective(home / "config.yaml", fail_closed=True).get(
        "cron"
    )
    if isinstance(cron_cfg, dict) and "max_terminal_executions" in cron_cfg:
        return cron_cfg["max_terminal_executions"]
    return _ABSENT


def test_shipped_fallback_is_disabled_and_agrees_with_the_shipped_config_default():
    import cron.executions as executions
    from hermes_cli.config import DEFAULT_CONFIG

    # No ledger-wide bound ships by either route: neither the constant nor the config default.
    assert executions.MAX_TERMINAL_EXECUTIONS is None
    assert DEFAULT_CONFIG["cron"]["max_terminal_executions"] is None


@pytest.mark.parametrize(
    "config_text", [None, "{}\n", "cron: {}\n", "cron:\n  wrap_response: false\n"]
)
def test_absent_key_takes_the_fallback_constant(monkeypatch, tmp_path, config_text):
    """No file, an empty mapping, an empty ``cron`` mapping or a ``cron`` mapping without the key:
    the module constant is the ceiling. It is patched without ``raising=False``, so it has to be
    a real production attribute. An empty file and a bare ``cron:`` are a null root and a null
    section, not mappings; they are pinned as invalid structure further down."""
    home = _home(tmp_path)
    if config_text is not None:
        (home / "config.yaml").write_text(config_text, encoding="utf-8")
    executions = _ledger(monkeypatch, home)
    assert _raw_ceiling(home) is _ABSENT
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 2)
    _seed(executions.EXECUTIONS_FILE, _spread_rows(5))

    _prune(executions)

    assert _ids(executions.EXECUTIONS_FILE) == {"r-0", "r-1"}


@pytest.mark.parametrize("cap_yaml", ["null", "~"])
def test_explicit_null_disables_the_ceiling_over_a_patched_fallback(
    monkeypatch, tmp_path, cap_yaml
):
    home = _home(tmp_path, cap_yaml=cap_yaml)
    executions = _ledger(monkeypatch, home)
    # Present and null, which is not the same answer as absent.
    assert _raw_ceiling(home) is None
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 2)
    _seed(executions.EXECUTIONS_FILE, _spread_rows(5))

    _prune(executions)

    assert _ids(executions.EXECUTIONS_FILE) == _ALL_FIVE


@pytest.mark.parametrize("fallback", [None, 0, 4, True])
def test_selected_home_ceiling_governs_whatever_the_fallback_constant_says(
    monkeypatch, tmp_path, fallback
):
    home = _home(tmp_path, cap_yaml="2")
    executions = _ledger(monkeypatch, home)
    assert _raw_ceiling(home) == 2
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", fallback)
    _seed(executions.EXECUTIONS_FILE, _spread_rows(5))

    _prune(executions)

    assert _ids(executions.EXECUTIONS_FILE) == {"r-0", "r-1"}


def test_invalid_configured_value_never_falls_through_to_the_fallback(
    monkeypatch, tmp_path
):
    home = _home(tmp_path, cap_yaml="true")
    executions = _ledger(monkeypatch, home)
    assert _raw_ceiling(home) is True
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 2)
    _seed(executions.EXECUTIONS_FILE, _spread_rows(5))

    _prune(executions)

    assert _ids(executions.EXECUTIONS_FILE) == _ALL_FIVE


def test_unreadable_config_never_falls_through_to_the_fallback(monkeypatch, tmp_path):
    """A file that does not parse cannot say whether the key is absent or null."""
    from hermes_cli.config_effective import load_user_config_effective

    home = _home(tmp_path)
    (home / "config.yaml").write_text("cron: [unterminated\n", encoding="utf-8")
    executions = _ledger(monkeypatch, home)
    # Whichever parser the reader uses, a strict read of this file has to refuse it.
    try:
        load_user_config_effective(home / "config.yaml", fail_closed=True)
    except Exception as exc:
        parse_error: Exception | None = exc
    else:
        parse_error = None
    assert parse_error is not None
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 2)
    _seed(executions.EXECUTIONS_FILE, _spread_rows(5))

    _prune(executions)

    assert _ids(executions.EXECUTIONS_FILE) == _ALL_FIVE


@pytest.mark.parametrize("fallback", [True, False, -1, 2.5, "2"])
def test_invalid_fallback_constant_retains_evidence_and_says_why(
    monkeypatch, tmp_path, caplog, fallback
):
    home = _home(tmp_path)
    executions = _ledger(monkeypatch, home)
    assert _raw_ceiling(home) is _ABSENT
    # Warnings are reported once per process; start from a clean record.
    monkeypatch.setattr(executions, "_reported_ceiling_problems", set())
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", fallback)
    _seed(executions.EXECUTIONS_FILE, _spread_rows(5))

    with caplog.at_level(logging.WARNING, logger="cron.executions"):
        _prune(executions)

    assert _ids(executions.EXECUTIONS_FILE) == _ALL_FIVE
    assert any(
        "MAX_TERMINAL_EXECUTIONS" in record.getMessage() for record in caplog.records
    ), [record.getMessage() for record in caplog.records]


def test_ledger_outside_a_profile_layout_takes_only_the_fallback_constant(
    monkeypatch, tmp_path
):
    import cron.executions as executions

    # Not ``<home>/cron/executions.db``: no profile owns this ledger, so the file one level up is
    # not its configuration and its ceiling of 0 must not be applied.
    (tmp_path / "config.yaml").write_text(
        "cron:\n  max_terminal_executions: 0\n", encoding="utf-8"
    )
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", store / "executions.db")
    executions.list_executions()
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 2)
    _seed(executions.EXECUTIONS_FILE, _spread_rows(5))

    _prune(executions)

    assert _ids(executions.EXECUTIONS_FILE) == {"r-0", "r-1"}


# --- structure: a root or ``cron`` section that is not a mapping is not an absent key ------------

_ALL_THREE = {"r-0", "r-1", "r-2"}
# Which reader touched the file before retention ran: none, the ordinary defaults-free loader, or
# ``read_raw_config`` with the ledger's home as the active one.
_PRIMING = ["cold", "effective", "raw"]

# Files that cannot say whether the key is absent. The falsy shapes (empty file, null, false, 0,
# []) are listed beside the truthy ones because the raw reader stores each of them as an empty
# mapping, where a truthy non-mapping is refused.
_INVALID_STRUCTURE = {
    "root-scalar-text": "just-a-scalar\n",
    "root-scalar-int": "42\n",
    "root-scalar-zero": "0\n",
    "root-list": "- cron\n- max_terminal_executions\n",
    "root-list-empty": "[]\n",
    "root-true": "true\n",
    "root-false": "false\n",
    "root-null": "null\n",
    "root-tilde": "~\n",
    "root-empty-file": "",
    "cron-scalar-text": "cron: nightly\n",
    "cron-scalar-int": "cron: 1\n",
    "cron-scalar-zero": "cron: 0\n",
    "cron-list": "cron:\n  - max_terminal_executions: 1\n",
    "cron-list-empty": "cron: []\n",
    "cron-true": "cron: true\n",
    "cron-false": "cron: false\n",
    "cron-null": "cron: null\n",
    "cron-tilde": "cron: ~\n",
    "cron-bare": "cron:\n",
    "yaml-syntax-error": "cron: [unterminated\n",
}

# id -> (config.yaml text, ``None`` for no file; rows that survive a fallback constant of 1).
# ``r-0`` finished last, so a ceiling of 1 keeps exactly it.
_VALID_STRUCTURE = {
    "missing-file": (None, {"r-0"}),
    "root-empty-mapping": ("{}\n", {"r-0"}),
    "cron-empty-mapping": ("cron: {}\n", {"r-0"}),
    "cron-without-the-key": ("cron:\n  wrap_response: false\n", {"r-0"}),
    "other-section-only": ("display:\n  skin: mono\n", {"r-0"}),
    "leaf-null": ("cron:\n  max_terminal_executions: null\n", _ALL_THREE),
    "leaf-tilde": ("cron:\n  max_terminal_executions: ~\n", _ALL_THREE),
    "leaf-zero": ("cron:\n  max_terminal_executions: 0\n", set()),
    "leaf-one": ("cron:\n  max_terminal_executions: 1\n", {"r-0"}),
    # Not the fallback's answer: the configured leaf is what governs.
    "leaf-two": ("cron:\n  max_terminal_executions: 2\n", {"r-0", "r-1"}),
}


def _reset_config_caches() -> None:
    import hermes_cli.config as cfg
    from hermes_cli import config_effective, managed_scope

    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    config_effective._EFFECTIVE_CACHE.clear()
    config_effective._LAST_GOOD_USER_RAW.clear()
    managed_scope.invalidate_managed_cache()


@pytest.fixture
def config_caches():
    """The process-wide config caches, emptied before and after the test so the primed read is
    the only earlier one a case sees."""
    _reset_config_caches()
    yield
    _reset_config_caches()


def _prime_config_caches(monkeypatch, home: Path, priming: str) -> None:
    """Read the home's ``config.yaml`` first, the way an earlier reader in the same process
    would. Both primed readers normalize a non-mapping root to an empty mapping."""
    import hermes_cli.config as cfg
    from hermes_cli.config_effective import load_user_config_effective

    config_path = home / "config.yaml"
    if priming == "effective":
        load_user_config_effective(config_path)
    elif priming == "raw":
        # read_raw_config() takes no path: it reads the active home's file.
        monkeypatch.setenv("HERMES_HOME", str(home))
        assert cfg.get_config_path().resolve() == config_path.resolve()
        cfg.read_raw_config()


def _structure_case(
    monkeypatch, tmp_path, config_text: str | None, priming: str, fallback=1
):
    """A home whose ``config.yaml`` is ``config_text`` verbatim, three recent completed rows from
    three jobs, a patched fallback constant and config caches warmed per ``priming``."""
    home = _home(tmp_path)
    if config_text is not None:
        (home / "config.yaml").write_text(config_text, encoding="utf-8")
    executions = _ledger(monkeypatch, home)
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", fallback)
    # Warnings are reported once per process; start from a clean record.
    monkeypatch.setattr(executions, "_reported_ceiling_problems", set())
    _seed(executions.EXECUTIONS_FILE, _spread_rows(3))
    _prime_config_caches(monkeypatch, home, priming)
    return home, executions


def _ceiling_warnings(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "cron.executions" and record.levelno >= logging.WARNING
    ]


@pytest.mark.parametrize("priming", _PRIMING)
@pytest.mark.parametrize(
    "config_text", list(_INVALID_STRUCTURE.values()), ids=list(_INVALID_STRUCTURE)
)
def test_invalid_structure_never_falls_through_to_the_fallback(
    monkeypatch, tmp_path, caplog, config_caches, config_text, priming
):
    """A finite fallback would cut three recent rows to one. A root or ``cron`` section that is
    not a mapping is not an absent key, so every row stays, cached reads included."""
    home, executions = _structure_case(monkeypatch, tmp_path, config_text, priming)

    with caplog.at_level(logging.WARNING, logger="cron.executions"):
        _prune(executions)

    assert _ids(executions.EXECUTIONS_FILE) == _ALL_THREE
    assert executions.terminal_execution_ceiling() is None
    warnings = _ceiling_warnings(caplog)
    assert any(
        "max_terminal_executions" in message and str(home / "config.yaml") in message
        for message in warnings
    ), warnings


@pytest.mark.parametrize("priming", _PRIMING)
@pytest.mark.parametrize(
    "config_text,kept", list(_VALID_STRUCTURE.values()), ids=list(_VALID_STRUCTURE)
)
def test_valid_structure_applies_the_configured_leaf_or_the_fallback(
    monkeypatch, tmp_path, caplog, config_caches, config_text, kept, priming
):
    """A genuinely absent key takes the fallback of 1 and deletes down to the newest row; a
    configured leaf, explicit null included, governs instead. Neither is reported as a problem."""
    _, executions = _structure_case(monkeypatch, tmp_path, config_text, priming)

    with caplog.at_level(logging.WARNING, logger="cron.executions"):
        _prune(executions)

    assert _ids(executions.EXECUTIONS_FILE) == kept
    assert _ceiling_warnings(caplog) == []


@pytest.mark.parametrize("priming", _PRIMING)
@pytest.mark.parametrize("cap_yaml", ["true", '"1"', "-1", "1.5", "[1]"])
def test_invalid_leaf_never_falls_through_to_a_finite_fallback(
    monkeypatch, tmp_path, caplog, config_caches, cap_yaml, priming
):
    home, executions = _structure_case(
        monkeypatch,
        tmp_path,
        f"cron:\n  max_terminal_executions: {cap_yaml}\n",
        priming,
    )

    with caplog.at_level(logging.WARNING, logger="cron.executions"):
        _prune(executions)

    assert _ids(executions.EXECUTIONS_FILE) == _ALL_THREE
    warnings = _ceiling_warnings(caplog)
    assert any(
        "max_terminal_executions" in message and str(home / "config.yaml") in message
        for message in warnings
    ), warnings


@pytest.mark.parametrize("priming", _PRIMING)
@pytest.mark.parametrize(
    "config_text",
    [None, "{}\n", "cron: {}\n"],
    ids=["missing-file", "root-empty-mapping", "cron-empty-mapping"],
)
@pytest.mark.parametrize("fallback", [True, "1", -1, 1.5])
def test_invalid_fallback_constant_retains_evidence_for_every_valid_absence(
    monkeypatch, tmp_path, caplog, config_caches, fallback, config_text, priming
):
    _, executions = _structure_case(
        monkeypatch, tmp_path, config_text, priming, fallback=fallback
    )

    with caplog.at_level(logging.WARNING, logger="cron.executions"):
        _prune(executions)

    assert _ids(executions.EXECUTIONS_FILE) == _ALL_THREE
    warnings = _ceiling_warnings(caplog)
    assert any("MAX_TERMINAL_EXECUTIONS" in message for message in warnings), warnings


@pytest.mark.parametrize("priming", _PRIMING)
@pytest.mark.parametrize("case", ["root-list", "cron-list", "cron-scalar-text"])
def test_invalid_structure_retains_recent_rows_through_the_public_write_path(
    monkeypatch, tmp_path, config_caches, case, priming
):
    _, executions = _structure_case(
        monkeypatch, tmp_path, _INVALID_STRUCTURE[case], priming
    )

    trigger = _finish_new(executions, "trigger")

    assert _ids(executions.EXECUTIONS_FILE) == _ALL_THREE | {trigger}


@pytest.mark.parametrize("priming", _PRIMING)
@pytest.mark.parametrize(
    "case", ["missing-file", "root-empty-mapping", "cron-empty-mapping"]
)
def test_absent_key_cuts_recent_rows_through_the_public_write_path(
    monkeypatch, tmp_path, config_caches, case, priming
):
    """The other side of the distinction: with the key legitimately absent, a fallback of 1 keeps
    only the attempt that just finished."""
    _, executions = _structure_case(
        monkeypatch, tmp_path, _VALID_STRUCTURE[case][0], priming
    )

    trigger = _finish_new(executions, "trigger")

    assert _ids(executions.EXECUTIONS_FILE) == {trigger}


# --- provenance: the user's ``cron`` section is judged as written, before the managed overlay ----

# A mapping root whose ``cron`` section is present but is not a mapping.
_INVALID_USER_CRON = {
    "user-null": "cron: null\n",
    "user-scalar": "cron: nightly\n",
    "user-list": "cron:\n  - max_terminal_executions: 1\n",
}
# A well-formed managed layer. Merged over the user's layer, each replaces the unusable section
# with a mapping: the first leaves the key absent, the second pins a ceiling of 1.
_MANAGED_OVERLAYS = {
    "managed-unrelated": "cron:\n  wrap_response: false\n",
    "managed-cap1": "cron:\n  max_terminal_executions: 1\n",
}
# ``_PRIMING`` plus the strict reader itself as the earlier reader.
_OVERLAY_PRIMING = ["cold", "raw", "effective", "strict_structure"]


@pytest.fixture
def managed_dir(tmp_path, monkeypatch, config_caches):
    """An empty managed scope selected the way the product selects one: ``HERMES_MANAGED_DIR``."""
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    return managed


def _prime_overlay_caches(monkeypatch, home: Path, priming: str) -> None:
    if priming != "strict_structure":
        _prime_config_caches(monkeypatch, home, priming)
        return
    from hermes_cli.config_effective import load_user_config_effective

    # The strict reader may serve this file or refuse it; either way it has read it first, and
    # whatever it left in the caches is what retention finds.
    with contextlib.suppress(ValueError):
        load_user_config_effective(
            home / "config.yaml", fail_closed=True, strict_structure=True
        )


@pytest.mark.parametrize("priming", _OVERLAY_PRIMING)
@pytest.mark.parametrize(
    "managed_text", list(_MANAGED_OVERLAYS.values()), ids=list(_MANAGED_OVERLAYS)
)
@pytest.mark.parametrize(
    "user_text", list(_INVALID_USER_CRON.values()), ids=list(_INVALID_USER_CRON)
)
def test_original_user_cron_overlay_provenance(
    monkeypatch, tmp_path, managed_dir, user_text, managed_text, priming
):
    """The user's own ``cron`` section cannot say whether the key is absent, and a managed layer
    that supplies a mapping in its place does not answer for it. Neither the managed ceiling of 1
    nor the fallback of 1 is applied: the finish is durable and every recent row stays."""
    home = _home(tmp_path)
    executions = _ledger(monkeypatch, home)
    # No file beside the ledger, an empty managed scope, no fallback: there is no ceiling yet.
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", None)
    assert executions.terminal_execution_ceiling() is None
    _seed(executions.EXECUTIONS_FILE, _spread_rows(2))

    (home / "config.yaml").write_text(user_text, encoding="utf-8")
    (managed_dir / "config.yaml").write_text(managed_text, encoding="utf-8")
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 1)
    _reset_config_caches()
    _prime_overlay_caches(monkeypatch, home, priming)

    claimed = executions.create_execution("trigger", source="builtin")
    finished = executions.finish_execution(claimed["id"], success=True)

    assert finished is not None
    assert (finished["id"], finished["status"]) == (claimed["id"], "completed")
    durable = {
        row["id"]: row["status"] for row in _all_rows(executions.EXECUTIONS_FILE)
    }
    assert durable == {
        "r-0": "completed",
        "r-1": "completed",
        claimed["id"]: "completed",
    }


# --- provenance: the managed layer is judged as written too, before it is merged -----------------

# Stands for a managed ``config.yaml`` that is a directory: present, and unreadable as a file.
_DIRECTORY = object()
# Managed files that cannot say whether they pin the key. The ordinary managed loader serves every
# one above the ``cron`` cases as no managed layer at all, which over a valid user file that omits
# the key reads as "key absent".
_INVALID_MANAGED = {
    "managed-root-null": "null\n",
    "managed-root-empty-file": "",
    "managed-root-scalar": "just-a-scalar\n",
    "managed-root-list": "- cron\n- max_terminal_executions\n",
    "managed-yaml-syntax-error": "cron: [unterminated\n",
    "managed-undecodable": b"cron: \xff\n",
    "managed-is-a-directory": _DIRECTORY,
    "managed-cron-null": "cron: null\n",
    "managed-cron-scalar": "cron: nightly\n",
    "managed-cron-list": "cron:\n  - max_terminal_executions: 1\n",
}
# ``_OVERLAY_PRIMING`` plus the reader retention itself uses as the earlier reader.
_MANAGED_PRIMING = [*_OVERLAY_PRIMING, "strict_section"]


def _write_managed(managed: Path, content) -> None:
    target = managed / "config.yaml"
    if content is _DIRECTORY:
        target.mkdir()
    elif isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")


def _prime_managed_caches(monkeypatch, home: Path, priming: str) -> None:
    strict = {
        "strict_structure": {"strict_structure": True},
        "strict_section": {"strict_section": "cron"},
    }.get(priming)
    if strict is None:
        _prime_config_caches(monkeypatch, home, priming)
        return
    from hermes_cli.config_effective import load_user_config_effective

    # Whatever a strict reader makes of the managed file (serve it, refuse it as a ``ValueError``,
    # or raise the read or parse error itself), it has read both layers first, and whatever it
    # left in the caches is what retention finds.
    with contextlib.suppress(ValueError, OSError, hermes_yaml.YAMLError):
        load_user_config_effective(home / "config.yaml", fail_closed=True, **strict)


@pytest.mark.parametrize("priming", _MANAGED_PRIMING)
@pytest.mark.parametrize(
    "managed_content", list(_INVALID_MANAGED.values()), ids=list(_INVALID_MANAGED)
)
def test_managed_provenance_retains_terminal_history(
    monkeypatch, tmp_path, managed_dir, managed_content, priming
):
    """The user's file is a valid mapping that omits the key; the managed file is present and
    cannot say whether it pins one. That is not an absent managed layer, so the fallback of 1 is
    not applied: the finish is durable and every recent row stays."""
    home = _home(tmp_path)
    (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    executions = _ledger(monkeypatch, home)
    # A valid user file, an empty managed scope, no fallback: there is no ceiling yet.
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", None)
    assert executions.terminal_execution_ceiling() is None
    _seed(executions.EXECUTIONS_FILE, _spread_rows(2))

    _write_managed(managed_dir, managed_content)
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 1)
    _reset_config_caches()
    _prime_managed_caches(monkeypatch, home, priming)

    claimed = executions.create_execution("trigger", source="builtin")
    finished = executions.finish_execution(claimed["id"], success=True)

    assert finished is not None
    assert (finished["id"], finished["status"]) == (claimed["id"], "completed")
    durable = {
        row["id"]: row["status"] for row in _all_rows(executions.EXECUTIONS_FILE)
    }
    assert durable == {
        "r-0": "completed",
        "r-1": "completed",
        claimed["id"]: "completed",
    }


# --- provenance: a user file that cannot be examined is not a missing one -----------------------

# Whether an ordinary read cached the user's file while it could still be examined.
_STAT_PRIMING = ["cold", "effective"]


def _watch_user_stat(monkeypatch, config_path: Path, *, deny: bool) -> list[str]:
    """Route ``os.stat`` through a wrapper that records every call naming exactly ``config_path``
    and, with ``deny``, refuses it with ``PermissionError``. Every other path, and this one
    without ``deny``, reaches the real ``os.stat`` with its arguments untouched."""
    real_stat = os.stat
    watched = os.fspath(config_path)
    calls: list[str] = []

    def stat(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and os.fspath(path) == watched:
            calls.append(watched)
            if deny:
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), watched)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    return calls


@pytest.mark.parametrize("priming", _STAT_PRIMING)
def test_user_config_stat_error_retains_terminal_history(
    monkeypatch, tmp_path, managed_dir, priming
):
    """The file beside the ledger is a valid mapping that omits the key, but it can no longer be
    examined: its stat is refused, as under a home that lost its search permission. That is not a
    missing file, so the fallback of 1 is not applied, whether or not an ordinary read cached the
    file first: the finish is durable and every recent row stays."""
    home = _home(tmp_path)
    config_path = home / "config.yaml"
    config_path.write_text("{}\n", encoding="utf-8")
    executions = _ledger(monkeypatch, home)
    # A valid user file, an empty managed scope, no fallback: there is no ceiling yet.
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", None)
    assert executions.terminal_execution_ceiling() is None
    _seed(executions.EXECUTIONS_FILE, _spread_rows(2))

    _reset_config_caches()
    _prime_config_caches(monkeypatch, home, priming)
    refused = _watch_user_stat(monkeypatch, config_path, deny=True)
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 1)

    claimed = executions.create_execution("trigger", source="builtin")
    finished = executions.finish_execution(claimed["id"], success=True)

    assert finished is not None
    assert (finished["id"], finished["status"]) == (claimed["id"], "completed")
    # The refusal is what retention met: the case is not decided by a read that never happened.
    assert refused
    durable = {
        row["id"]: row["status"] for row in _all_rows(executions.EXECUTIONS_FILE)
    }
    assert durable == {
        "r-0": "completed",
        "r-1": "completed",
        claimed["id"]: "completed",
    }


@pytest.mark.parametrize("priming", _STAT_PRIMING)
def test_missing_user_config_still_takes_the_fallback_ceiling(
    monkeypatch, tmp_path, managed_dir, priming
):
    """The other side of the distinction, through the same wrapper: the file is gone, the real
    stat says so, and a missing file is a valid empty layer. The fallback of 1 stays in force and
    keeps only the attempt that just finished, whether or not the file was cached while it was
    still there."""
    home = _home(tmp_path)
    config_path = home / "config.yaml"
    config_path.write_text("{}\n", encoding="utf-8")
    executions = _ledger(monkeypatch, home)
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", None)
    assert executions.terminal_execution_ceiling() is None
    _seed(executions.EXECUTIONS_FILE, _spread_rows(2))

    _reset_config_caches()
    _prime_config_caches(monkeypatch, home, priming)
    config_path.unlink()
    looked_up = _watch_user_stat(monkeypatch, config_path, deny=False)
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 1)

    claimed = executions.create_execution("trigger", source="builtin")
    finished = executions.finish_execution(claimed["id"], success=True)

    assert finished is not None
    assert (finished["id"], finished["status"]) == (claimed["id"], "completed")
    assert looked_up
    durable = {
        row["id"]: row["status"] for row in _all_rows(executions.EXECUTIONS_FILE)
    }
    assert durable == {claimed["id"]: "completed"}


# --- magnitude: every non-negative integer is a ceiling, whatever SQLite can bind ----------------

# id -> (ceiling; how many of the three terminal rows a finish leaves it keeps, newest first).
# The first three are at and past the largest integer SQLite binds; the next three sit around the
# row count; the last three are the controls: delete everything, no ceiling, not a ceiling.
_CEILINGS = {
    "int64-max": (2**63 - 1, 3),
    "int64-max-plus-one": (2**63, 3),
    "googol": (10**100, 3),
    "above-count": (4, 3),
    "equals-count": (3, 3),
    "below-count": (2, 2),
    "zero": (0, 0),
    "null": (None, 3),
    "bool": (True, 3),
}


def _yaml_scalar(value) -> str:
    """``value`` as a user writes it: ``null``, ``true`` or the integer's own digits."""
    return "null" if value is None else str(value).lower()


@pytest.mark.parametrize("source", ["configured", "fallback"])
@pytest.mark.parametrize("ceiling,kept", list(_CEILINGS.values()), ids=list(_CEILINGS))
def test_unbounded_integer_ceiling_public_finish(
    monkeypatch, tmp_path, config_caches, ceiling, kept, source
):
    """A ceiling the ledger cannot reach is still a valid one: it deletes nothing, and it never
    costs the terminal write that ran retention. The finish commits as completed and no row is
    left claimed, from the configured key and from the fallback constant alike."""
    configured = source == "configured"
    home = _home(tmp_path, cap_yaml=_yaml_scalar(ceiling) if configured else None)
    executions = _ledger(monkeypatch, home)
    # A configured leaf governs over a fallback that would cut the ledger to one row; with no
    # file the constant itself is the value under test.
    monkeypatch.setattr(
        executions, "MAX_TERMINAL_EXECUTIONS", 1 if configured else ceiling
    )
    read_back = _raw_ceiling(home)
    if configured:
        # The file carries exactly this value: an int of any size stays an int.
        assert type(read_back) is type(ceiling)
        assert read_back == ceiling
    else:
        assert read_back is _ABSENT
    _seed(executions.EXECUTIONS_FILE, _spread_rows(2))

    claimed = executions.create_execution("trigger", source="builtin")
    finished = executions.finish_execution(claimed["id"], success=True)

    assert finished is not None
    assert (finished["id"], finished["status"]) == (claimed["id"], "completed")
    newest_first = [claimed["id"], "r-0", "r-1"]
    durable = {
        row["id"]: row["status"] for row in _all_rows(executions.EXECUTIONS_FILE)
    }
    assert durable == dict.fromkeys(newest_first[:kept], "completed")


# --- magnitude: a value too large to print is still judged, never raised ------------------------

# 4000 hex digits are about 4800 decimal digits, past CPython's default int/str conversion limit:
# repr() of the integer, and of any list or mapping that holds it, raises ValueError. A hex
# literal is parsed without that limit, so YAML and Python alike yield the integer itself. The
# negative one, and the containers, are invalid ceilings; the positive one is a valid ceiling the
# ledger never reaches. None of them may cost the terminal write that ran retention.
_HUGE_HEX = "0x" + "f" * 4000
_HUGE_INT = 1 << 16000
_UNPRINTABLE_KINDS = ["negative", "list", "mapping", "positive"]
_UNPRINTABLE_SOURCES = ["user", "managed", "fallback"]


def _unprintable_yaml(kind: str) -> str:
    """A ``cron`` section whose ceiling is the raw hex scalar, as a user or admin writes it."""
    scalar = {
        "negative": f"-{_HUGE_HEX}",
        "list": f"[-{_HUGE_HEX}]",
        "mapping": f"{{limit: -{_HUGE_HEX}}}",
        "positive": _HUGE_HEX,
    }[kind]
    return f"cron:\n  max_terminal_executions: {scalar}\n"


def _unprintable_fallback(kind: str):
    """The same shape as a Python value for the fallback constant."""
    return {
        "negative": -_HUGE_INT,
        "list": [-_HUGE_INT],
        "mapping": {"limit": -_HUGE_INT},
        "positive": _HUGE_INT,
    }[kind]


@pytest.mark.parametrize("kind", _UNPRINTABLE_KINDS, ids=_UNPRINTABLE_KINDS)
@pytest.mark.parametrize("source", _UNPRINTABLE_SOURCES, ids=_UNPRINTABLE_SOURCES)
def test_unprintable_policy_value_never_invalidates_the_public_finish(
    monkeypatch, tmp_path, managed_dir, source, kind
):
    """An invalid ceiling retains evidence; it never raises out of the terminal write. A value
    whose repr() itself raises, from the user's file, the managed file or the fallback constant,
    must leave the finish durable and completed, with every recent row in place."""
    home = _home(tmp_path)
    (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    executions = _ledger(monkeypatch, home)
    # A valid user file, an empty managed scope, no fallback: there is no ceiling yet.
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", None)
    assert executions.terminal_execution_ceiling() is None
    _seed(executions.EXECUTIONS_FILE, _spread_rows(2))

    if source == "user":
        (home / "config.yaml").write_text(_unprintable_yaml(kind), encoding="utf-8")
    elif source == "managed":
        _write_managed(managed_dir, _unprintable_yaml(kind))
    else:
        monkeypatch.setattr(
            executions, "MAX_TERMINAL_EXECUTIONS", _unprintable_fallback(kind)
        )
    _reset_config_caches()

    claimed = executions.create_execution("trigger", source="builtin")
    raised: str | None = None
    finished = None
    try:
        finished = executions.finish_execution(claimed["id"], success=True)
    except Exception as exc:
        raised = type(exc).__name__

    # Fresh read, not the write's own return value: what the ledger durably holds.
    durable = executions.get_execution(claimed["id"])
    assert durable is not None
    assert (durable["status"], durable["error"]) == ("completed", None), (
        f"finish was not durable; finish_execution raised {raised}"
    )
    assert raised is None
    assert finished is not None
    assert (finished["id"], finished["status"]) == (claimed["id"], "completed")
    rows = {row["id"]: row["status"] for row in _all_rows(executions.EXECUTIONS_FILE)}
    assert rows == {
        "r-0": "completed",
        "r-1": "completed",
        claimed["id"]: "completed",
    }


# --- suspension: a policy that cannot be established deletes nothing at all ----------------------

# id -> (user config.yaml; managed config.yaml, ``None`` for no managed file; policy unusable).
_AGED_PRUNING_POLICIES = {
    "user-cron-null": ("cron: null\n", None, True),
    "user-root-list": ("- cron\n", None, True),
    "user-yaml-syntax-error": ("cron: [unterminated\n", None, True),
    "user-leaf-bool": ("cron:\n  max_terminal_executions: true\n", None, True),
    "managed-root-null": ("{}\n", "null\n", True),
    "managed-root-list": ("{}\n", "- cron\n", True),
    "managed-yaml-syntax-error": ("{}\n", "cron: [unterminated\n", True),
    "managed-is-a-directory": ("{}\n", _DIRECTORY, True),
    "managed-cron-null": ("{}\n", "cron: null\n", True),
    "managed-leaf-negative": ("{}\n", "cron:\n  max_terminal_executions: -1\n", True),
    # The controls: an explicit null is a usable policy with no ceiling, from either layer.
    "user-explicit-null": ("cron:\n  max_terminal_executions: null\n", None, False),
    "managed-explicit-null": (
        "{}\n",
        "cron:\n  max_terminal_executions: null\n",
        False,
    ),
}
_AGED = {"aged-a", "aged-b", "aged-trigger"}


@pytest.mark.parametrize(
    "user_text,managed_content,unusable",
    list(_AGED_PRUNING_POLICIES.values()),
    ids=list(_AGED_PRUNING_POLICIES),
)
def test_invalid_policy_suspends_aged_quota_pruning(
    monkeypatch, tmp_path, managed_dir, user_text, managed_content, unusable
):
    """Every seeded row is beyond its job's quota of 0 and finished before the 30-day floor, so
    the default policy deletes it on the next terminal write. An explicit null ceiling leaves
    that policy in force; a policy that cannot be established deletes nothing instead."""
    home = _home(tmp_path)
    (home / "config.yaml").write_text(user_text, encoding="utf-8")
    if managed_content is not None:
        _write_managed(managed_dir, managed_content)
    executions = _ledger(monkeypatch, home)
    monkeypatch.setattr(executions, "PER_JOB_TERMINAL_EXECUTIONS", 0)
    monkeypatch.setattr(executions, "TERMINAL_RETENTION_FLOOR_DAYS", 30)
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", None)
    _seed(
        executions.EXECUTIONS_FILE,
        [
            ("aged-a", "job-a", "completed", _ago(days=61), _ago(days=60)),
            ("aged-b", "job-b", "failed", _ago(days=91), _ago(days=90)),
            ("aged-trigger", "trigger", "unknown", _ago(days=46), _ago(days=45)),
        ],
    )

    trigger = _finish_new(executions, "trigger")

    kept = {trigger} | _AGED if unusable else {trigger}
    assert _ids(executions.EXECUTIONS_FILE) == kept


# --- profile binding: the ledger's own home decides, never the ambient one -----------------------


def _expected_kept(cap, written: int) -> int:
    return written if cap is None else min(cap, written)


@pytest.mark.parametrize(
    "selected_cap,ambient_cap", [(None, 0), (None, 2), (2, None), (0, None), (1, 3)]
)
def test_selected_profile_policy_governs_its_own_ledger_a_b_a(
    monkeypatch, tmp_path, selected_cap, ambient_cap
):
    """Poisoned ambient home: the process env points at B while a tick is scoped to A.

    Uses the production ticker scope and real ``<home>/cron/executions.db`` files; the ledger
    override is unset, so both the ledger location and the policy path are the product's own.
    """
    import cron.executions as executions
    from cron.scheduler_provider import _profile_cron_scope

    def cap_yaml(cap):
        return None if cap is None else str(cap)

    selected = _home(tmp_path, "selected", cap_yaml(selected_cap))
    ambient = _home(tmp_path, "ambient", cap_yaml(ambient_cap))
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", None)
    monkeypatch.setenv("HERMES_HOME", str(ambient))
    selected_db = selected / "cron" / "executions.db"
    ambient_db = ambient / "cron" / "executions.db"

    with _profile_cron_scope(selected):
        for index in range(4):
            _finish_new(executions, f"a-{index}")
    assert len(_ids(selected_db)) == _expected_kept(selected_cap, 4)
    assert not ambient_db.exists()

    for index in range(4):
        _finish_new(executions, f"b-{index}")
    assert len(_ids(ambient_db)) == _expected_kept(ambient_cap, 4)
    assert len(_ids(selected_db)) == _expected_kept(selected_cap, 4)

    with _profile_cron_scope(selected):
        _finish_new(executions, "a-again")
    assert len(_ids(selected_db)) == _expected_kept(selected_cap, 5)
    assert len(_ids(ambient_db)) == _expected_kept(ambient_cap, 4)
    # Rows never crossed ledgers in either direction.
    selected_jobs = {row["job_id"] for row in _all_rows(selected_db)}
    ambient_jobs = {row["job_id"] for row in _all_rows(ambient_db)}
    assert not any(job.startswith("b-") for job in selected_jobs)
    assert not any(job.startswith("a-") for job in ambient_jobs)


def _all_rows(path: Path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM executions")]
    finally:
        conn.close()


@pytest.mark.parametrize("store_cap,ambient_cap", [(None, 1), (1, None)])
def test_store_only_scope_never_applies_one_homes_policy_to_another_homes_ledger(
    monkeypatch, tmp_path, store_cap, ambient_cap
):
    """``use_cron_store`` routes the job store and does not bind the Hermes home. Whichever
    ledger file receives the rows, the ceiling applied is the one configured beside that file."""
    import cron.executions as executions
    from cron.jobs import use_cron_store

    store = _home(tmp_path, "store", None if store_cap is None else str(store_cap))
    ambient = _home(
        tmp_path, "ambient", None if ambient_cap is None else str(ambient_cap)
    )
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", None)
    monkeypatch.setenv("HERMES_HOME", str(ambient))

    with use_cron_store(store):
        for index in range(3):
            _finish_new(executions, f"job-{index}")

    caps = {store: store_cap, ambient: ambient_cap}
    written = {home: home / "cron" / "executions.db" for home in caps}
    owners = [home for home, db in written.items() if db.exists()]
    assert len(owners) == 1
    assert len(_ids(written[owners[0]])) == _expected_kept(caps[owners[0]], 3)


@pytest.mark.parametrize("invalid_home", ["selected", "ambient"])
@pytest.mark.parametrize(
    "invalid_text",
    ["- cron\n", "cron: []\n", "cron:\n", ""],
    ids=["root-list", "cron-list-empty", "cron-bare", "root-empty-file"],
)
def test_structure_is_judged_for_the_ledgers_own_home_never_the_ambient_one(
    monkeypatch, tmp_path, config_caches, invalid_text, invalid_home
):
    """One home's file is not a usable mapping, the other's legitimately omits the key. Each
    ledger answers for the file beside it: the first retains, the second takes the fallback."""
    import cron.executions as executions
    from cron.scheduler_provider import _profile_cron_scope

    homes = {name: _home(tmp_path, name) for name in ("selected", "ambient")}
    for name, home in homes.items():
        (home / "config.yaml").write_text(
            invalid_text if name == invalid_home else "cron: {}\n", encoding="utf-8"
        )
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", None)
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 1)
    monkeypatch.setenv("HERMES_HOME", str(homes["ambient"]))

    with _profile_cron_scope(homes["selected"]):
        for index in range(3):
            _finish_new(executions, f"a-{index}")
    for index in range(3):
        _finish_new(executions, f"b-{index}")

    kept = {
        name: len(_ids(home / "cron" / "executions.db")) for name, home in homes.items()
    }
    assert kept == {name: 3 if name == invalid_home else 1 for name in homes}


# --- ordering: terminal finish instants, deterministically ---------------------------------------


def test_per_job_quota_ranks_by_finish_instant_not_claim_instant(monkeypatch, tmp_path):
    executions = _ledger(monkeypatch, _home(tmp_path))
    monkeypatch.setattr(executions, "PER_JOB_TERMINAL_EXECUTIONS", 1)
    _seed(
        executions.EXECUTIONS_FILE,
        [
            # Claimed first, finished last: the job's newest evidence.
            ("long-running", "job", "completed", _ago(days=40), _ago(minutes=5)),
            # Claimed later, finished long ago: the overflow that aged out.
            ("quick", "job", "completed", _ago(days=35), _ago(days=34)),
        ],
    )

    _prune(executions)

    assert _ids(executions.EXECUTIONS_FILE) == {"long-running"}


def test_ceiling_ranks_by_finish_instant_not_claim_instant(monkeypatch, tmp_path):
    executions = _ledger(monkeypatch, _home(tmp_path, cap_yaml="1"))
    _seed(
        executions.EXECUTIONS_FILE,
        [
            ("long-running", "job-a", "completed", _ago(days=3), _ago(minutes=5)),
            ("quick", "job-b", "completed", _ago(days=2), _ago(days=1)),
        ],
    )

    _prune(executions)

    assert _ids(executions.EXECUTIONS_FILE) == {"long-running"}


# A fall-back fold: 01:30 EDT happens before 01:10 EST although it sorts after it as text.
_EARLIER_INSTANT = "2025-11-02T01:30:00-04:00"
_LATER_INSTANT = "2025-11-02T01:10:00-05:00"


def test_per_job_quota_compares_offsets_as_instants(monkeypatch, tmp_path):
    executions = _ledger(monkeypatch, _home(tmp_path))
    monkeypatch.setattr(executions, "PER_JOB_TERMINAL_EXECUTIONS", 1)
    _seed(
        executions.EXECUTIONS_FILE,
        [
            ("earlier", "job", "completed", _EARLIER_INSTANT, _EARLIER_INSTANT),
            ("later", "job", "completed", _LATER_INSTANT, _LATER_INSTANT),
        ],
    )

    _prune(executions)

    assert _ids(executions.EXECUTIONS_FILE) == {"later"}


def test_ceiling_compares_offsets_as_instants(monkeypatch, tmp_path):
    executions = _ledger(monkeypatch, _home(tmp_path, cap_yaml="1"))
    _seed(
        executions.EXECUTIONS_FILE,
        [
            ("earlier", "job-a", "completed", _EARLIER_INSTANT, _EARLIER_INSTANT),
            ("later", "job-b", "completed", _LATER_INSTANT, _LATER_INSTANT),
        ],
    )

    _prune(executions)

    assert _ids(executions.EXECUTIONS_FILE) == {"later"}


def test_legacy_null_finish_is_aged_from_its_claim_instant(monkeypatch, tmp_path):
    executions = _ledger(monkeypatch, _home(tmp_path))
    monkeypatch.setattr(executions, "PER_JOB_TERMINAL_EXECUTIONS", 1)
    _seed(
        executions.EXECUTIONS_FILE,
        [
            ("legacy-recent", "job", "completed", _ago(days=1), None),
            ("finished-old", "job", "completed", _ago(days=61), _ago(days=60)),
            ("legacy-old", "job", "failed", _ago(days=90), None),
        ],
    )

    _prune(executions)

    # The NULL-finish row claimed yesterday is the job's newest evidence, not its oldest; the
    # NULL-finish row claimed 90 days ago is provably aged overflow.
    assert _ids(executions.EXECUTIONS_FILE) == {"legacy-recent"}


def test_ceiling_treats_legacy_null_finish_as_its_claim_instant(monkeypatch, tmp_path):
    executions = _ledger(monkeypatch, _home(tmp_path, cap_yaml="1"))
    _seed(
        executions.EXECUTIONS_FILE,
        [
            ("legacy-recent", "job-a", "completed", _ago(days=1), None),
            ("finished-old", "job-b", "completed", _ago(days=61), _ago(days=60)),
        ],
    )

    _prune(executions)

    assert _ids(executions.EXECUTIONS_FILE) == {"legacy-recent"}


def test_unparseable_instants_are_kept_by_default_and_do_not_displace_dated_rows(
    monkeypatch, tmp_path
):
    executions = _ledger(monkeypatch, _home(tmp_path))
    monkeypatch.setattr(executions, "PER_JOB_TERMINAL_EXECUTIONS", 1)
    _seed(
        executions.EXECUTIONS_FILE,
        [
            # Age cannot be proved: never deleted without an explicit ceiling.
            ("undated", "job", "unknown", "not-a-time", "also-not-a-time"),
            # The only dated row holds the quota slot even though "n" sorts after "2" as text.
            ("dated-old", "job", "completed", _ago(days=61), _ago(days=60)),
            # A finish stamp that is present but unreadable is not a legacy missing one: the claim
            # instant does not stand in for it, so this row's age is unproved as well.
            (
                "bad-finish-old-claim",
                "job",
                "failed",
                _ago(days=90),
                "31/02/2026 25:61",
            ),
        ],
    )

    _prune(executions)

    assert _ids(executions.EXECUTIONS_FILE) == {
        "undated",
        "dated-old",
        "bad-finish-old-claim",
    }


def test_ceiling_evicts_unparseable_instants_before_dated_rows(monkeypatch, tmp_path):
    executions = _ledger(monkeypatch, _home(tmp_path, cap_yaml="1"))
    _seed(
        executions.EXECUTIONS_FILE,
        [
            ("undated", "job-a", "unknown", "not-a-time", "also-not-a-time"),
            ("dated-old", "job-b", "completed", _ago(days=61), _ago(days=60)),
        ],
    )

    _prune(executions)

    assert _ids(executions.EXECUTIONS_FILE) == {"dated-old"}


# Finish stamps that are present but carry no calendar date. SQLite's julianday() rejects the
# first three and accepts the last two (a bare time is read as that time on 2000-01-01, a bare
# number as a Julian day), which this ledger never writes and which would otherwise read as
# decades old.
_MALFORMED_FINISH = ["damaged-finish", "31/02/2026 25:61", "", "12:00", "2460000.5"]


@pytest.mark.parametrize("finished_at", _MALFORMED_FINISH)
def test_malformed_finish_with_an_old_claim_is_kept_by_default(
    monkeypatch, tmp_path, finished_at
):
    executions = _ledger(monkeypatch, _home(tmp_path))
    monkeypatch.setattr(executions, "PER_JOB_TERMINAL_EXECUTIONS", 1)
    _seed(
        executions.EXECUTIONS_FILE,
        [
            # Claimed 90 days ago and beyond the job's quota. When it finished is unknown: a
            # long-running attempt may have finished today, so the claim proves nothing.
            ("bad-finish-old-claim", "job", "failed", _ago(days=90), finished_at),
            ("recent", "job", "completed", _ago(hours=2), _ago(hours=1)),
        ],
    )

    _prune(executions)

    assert _ids(executions.EXECUTIONS_FILE) == {"bad-finish-old-claim", "recent"}


def test_malformed_finish_never_takes_a_quota_slot_from_a_dated_attempt(
    monkeypatch, tmp_path
):
    executions = _ledger(monkeypatch, _home(tmp_path))
    monkeypatch.setattr(executions, "PER_JOB_TERMINAL_EXECUTIONS", 1)
    _seed(
        executions.EXECUTIONS_FILE,
        [
            # Claimed an hour ago: aged from its claim, this row would rank newest and push the dated
            # row out of the job's single quota slot.
            (
                "bad-finish-recent-claim",
                "job",
                "failed",
                _ago(hours=1),
                "damaged-finish",
            ),
            ("dated-old", "job", "completed", _ago(days=61), _ago(days=60)),
            ("dated-older", "job", "completed", _ago(days=71), _ago(days=70)),
        ],
    )

    _prune(executions)

    assert _ids(executions.EXECUTIONS_FILE) == {"bad-finish-recent-claim", "dated-old"}


@pytest.mark.parametrize("cap_yaml,kept", [("1", {"dated-old"}), ("0", set())])
def test_explicit_ceiling_overrides_the_floor_for_malformed_finish(
    monkeypatch, tmp_path, cap_yaml, kept
):
    """The default policy keeps the malformed row; an enabled ceiling is an explicit choice to
    bound the ledger and evicts the undatable row ahead of a dated one."""
    executions = _ledger(monkeypatch, _home(tmp_path, cap_yaml=cap_yaml))
    _seed(
        executions.EXECUTIONS_FILE,
        [
            (
                "bad-finish-recent-claim",
                "job-a",
                "failed",
                _ago(hours=1),
                "damaged-finish",
            ),
            ("dated-old", "job-b", "completed", _ago(days=61), _ago(days=60)),
        ],
    )

    _prune(executions)

    assert _ids(executions.EXECUTIONS_FILE) == kept


def test_ceiling_orders_malformed_finish_rows_by_text_not_by_claim(
    monkeypatch, tmp_path
):
    executions = _ledger(monkeypatch, _home(tmp_path, cap_yaml="1"))
    _seed(
        executions.EXECUTIONS_FILE,
        [
            # The more recent claim carries the lower-sorting finish text.
            ("m-a", "job-a", "failed", _ago(hours=1), "damaged-a"),
            ("m-b", "job-b", "failed", _ago(days=5), "damaged-b"),
        ],
    )

    _prune(executions)

    # Neither finish instant is known, so the claim decides nothing: raw finish text, then id.
    assert _ids(executions.EXECUTIONS_FILE) == {"m-b"}


def test_ceiling_breaks_identical_instants_by_id(monkeypatch, tmp_path):
    executions = _ledger(monkeypatch, _home(tmp_path, cap_yaml="2"))
    same = _ago(hours=1)
    _seed(
        executions.EXECUTIONS_FILE,
        [
            (row_id, f"job-{row_id}", "completed", same, same)
            for row_id in ("b", "d", "a", "c")
        ],
    )

    _prune(executions)

    assert _ids(executions.EXECUTIONS_FILE) == {"c", "d"}


# --- floor boundary and fairness -----------------------------------------------------------------


def test_floor_boundary_keeps_just_inside_and_prunes_just_outside(
    monkeypatch, tmp_path
):
    executions = _ledger(monkeypatch, _home(tmp_path))
    monkeypatch.setattr(executions, "PER_JOB_TERMINAL_EXECUTIONS", 0)
    floor = executions.TERMINAL_RETENTION_FLOOR_DAYS
    _seed(
        executions.EXECUTIONS_FILE,
        [
            (
                "inside",
                "job",
                "completed",
                _ago(days=floor + 5),
                _ago(days=floor, hours=-3),
            ),
            (
                "outside",
                "job",
                "completed",
                _ago(days=floor + 5),
                _ago(days=floor, hours=3),
            ),
        ],
    )

    _prune(executions)

    assert _ids(executions.EXECUTIONS_FILE) == {"inside"}


def test_ceiling_takes_from_the_noisiest_job_before_a_quiet_jobs_only_evidence(
    monkeypatch, tmp_path
):
    executions = _ledger(monkeypatch, _home(tmp_path, cap_yaml="3"))
    rows = [
        (f"noisy-{i}", "noisy", "completed", _ago(minutes=i + 1), _ago(minutes=i + 1))
        for i in range(5)
    ]
    # Older than every noisy row: a ledger-wide newest-N order would evict it first.
    rows.append(("quiet-only", "quiet", "failed", _ago(days=10), _ago(days=10)))
    _seed(executions.EXECUTIONS_FILE, rows)

    _prune(executions)

    assert _ids(executions.EXECUTIONS_FILE) == {"quiet-only", "noisy-0", "noisy-1"}


def test_ceiling_below_job_count_keeps_the_most_recently_finished_jobs(
    monkeypatch, tmp_path
):
    executions = _ledger(monkeypatch, _home(tmp_path, cap_yaml="2"))
    _seed(
        executions.EXECUTIONS_FILE,
        [
            (
                f"job-{i}-newest",
                f"job-{i}",
                "completed",
                _ago(hours=i + 1),
                _ago(hours=i + 1),
            )
            for i in range(4)
        ]
        + [("job-0-older", "job-0", "completed", _ago(hours=9), _ago(hours=9))],
    )

    _prune(executions)

    assert _ids(executions.EXECUTIONS_FILE) == {"job-0-newest", "job-1-newest"}


# --- obligations that are never retention candidates ---------------------------------------------


@pytest.mark.parametrize("cap_yaml", [None, "0"])
def test_live_and_deferred_obligations_survive_every_mode(
    monkeypatch, tmp_path, cap_yaml
):
    executions = _ledger(monkeypatch, _home(tmp_path, cap_yaml=cap_yaml))
    monkeypatch.setattr(executions, "PER_JOB_TERMINAL_EXECUTIONS", 0)

    claimed = executions.create_execution("claimed-job", source="builtin")
    running = executions.create_execution("running-job", source="builtin")
    assert executions.mark_execution_running(running["id"]) is not None
    detached = executions.create_execution("detached-job", source="builtin")
    assert executions.mark_execution_running(detached["id"]) is not None
    assert executions.register_detached_run(detached["id"], run_id="run-1") is not None
    deferred = executions.create_execution("deferred-job", source="builtin")
    assert (
        executions.defer_execution(
            deferred["id"],
            reason="lock held",
            occurrence_key="deferred-job:slot",
            retry_at=_ago(minutes=-5),
        )
        is not None
    )
    expected = {
        claimed["id"]: "claimed",
        running["id"]: "running",
        detached["id"]: "running",
        deferred["id"]: "deferred",
    }
    # Age every obligation far past the floor so only its state protects it.
    conn = sqlite3.connect(executions.EXECUTIONS_FILE)
    try:
        conn.execute("UPDATE executions SET claimed_at=?", (_ago(days=120),))
        conn.execute(
            "UPDATE executions SET finished_at=? WHERE finished_at IS NOT NULL",
            (_ago(days=119),),
        )
        conn.commit()
    finally:
        conn.close()
    _seed(
        executions.EXECUTIONS_FILE,
        [("aged-terminal", "claimed-job", "completed", _ago(days=100), _ago(days=99))],
    )

    _finish_new(executions, "trigger")

    survivors = {
        row["id"]: row["status"]
        for row in _all_rows(executions.EXECUTIONS_FILE)
        if row["id"] in expected
    }
    assert survivors == expected
    assert "aged-terminal" not in _ids(executions.EXECUTIONS_FILE)
    assert executions.find_detached_run("run-1")["id"] == detached["id"]


def test_dead_owner_terminalization_prunes_aged_per_job_overflow_by_default(
    monkeypatch, tmp_path
):
    executions = _ledger(monkeypatch, _home(tmp_path))
    monkeypatch.setattr(executions, "PER_JOB_TERMINAL_EXECUTIONS", 1)
    _seed(
        executions.EXECUTIONS_FILE,
        [
            ("aged", "job", "completed", _ago(days=61), _ago(days=60)),
            ("other-aged", "other-job", "completed", _ago(days=61), _ago(days=60)),
        ],
    )
    lost = executions.create_execution("job", source="builtin")
    monkeypatch.setattr(executions, "_PROCESS_ID", "replacement-gateway")
    monkeypatch.setattr(executions, "_owner_is_live", lambda _pid, _started: False)

    assert executions.terminalize_dead_owner(lost["id"], reason="worker exited 9")

    # The lost attempt is now the job's newest evidence; the aged row is that job's overflow.
    # Another job's single aged row sits inside its own quota and stays.
    assert _ids(executions.EXECUTIONS_FILE) == {lost["id"], "other-aged"}
