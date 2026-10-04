"""Read-only tree integrity audit shared by ``hermes update`` and publish gates.

A conflict resolution can leave a tree whose files each look plausible but do not compile
(orphan signature tails, docstrings without their ``def``) or compile and still fail at import.
The 9-file critical-syntax guard and the 4-module import probe never reached the scheduler or
worker-tool modules, so such a tree could be published or reported as a successful update. This
audit compiles every tracked Python file and imports the modules the scheduler and worker tools
load, in a child that sees none of the caller's profile, secret or home state.

Stdlib-only at import time so it can run with ``-m`` against a checkout whose other modules are
the thing under suspicion. Success is derived from the structured report, never from log text.
Anything the audit cannot establish (git enumeration failure, no Python files, a probe that
exits nonzero or speaks the wrong protocol) is a failure, not a pass.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPORT_SCHEMA = 2

# Modules whose import must succeed for the scheduler, the worker tools and the CLI to boot.
# A name whose file is absent from the tree is recorded in ``skipped_modules``: a refactor may
# legitimately remove it, but the gap is visible to the caller.
INTEGRITY_IMPORT_MODULES = (
    "hermes_cli.main", "run_agent", "model_tools", "toolsets",
    "cron.jobs", "cron.scheduler", "cron.incidents", "tools.kanban_tools",
)

# Directory names never scanned when the tree is not a git checkout (ZIP/archive installs).
_WALK_EXCLUDED_DIRS = frozenset({
    ".git", "venv", ".venv", "node_modules", "__pycache__", "site-packages", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "build", "dist", ".eggs",
})

# The only inherited variables the probe child sees. Everything profile-, secret- or
# runtime-path-shaped (HERMES_*, HOME, XDG_*, tokens) is rebuilt or dropped.
_ENV_PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT", "COMSPEC", "PATHEXT")

_IMPORT_PROBE_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class SyntaxFailure:
    path: str
    line: int | None
    error: str


@dataclass(frozen=True)
class ImportFailure:
    module: str
    kind: str
    detail: str


@dataclass
class IntegrityReport:
    root: str
    scan: str = "walk"
    strict: bool = True
    python_files: int = 0
    syntax_failures: list[SyntaxFailure] = field(default_factory=list)
    import_failures: list[ImportFailure] = field(default_factory=list)
    # Third-party modules that were absent. Failures in strict mode; recorded here, with
    # ``import_complete=False``, when the caller deliberately runs before dependency install.
    missing_dependencies: list[ImportFailure] = field(default_factory=list)
    skipped_modules: list[str] = field(default_factory=list)
    import_checked: bool = False
    probe_error: str | None = None
    enumeration_error: str | None = None

    @property
    def import_complete(self) -> bool:
        return (self.import_checked and not self.missing_dependencies
                and not self.skipped_modules and not self.import_failures
                and not self.probe_error)

    @property
    def ok(self) -> bool:
        return not (self.syntax_failures or self.import_failures or self.probe_error
                    or self.enumeration_error)

    def to_dict(self) -> dict:
        return {
            "schema": REPORT_SCHEMA,
            "ok": self.ok,
            "root": self.root,
            "scan": self.scan,
            "strict": self.strict,
            "python_files": self.python_files,
            "syntax_failures": [asdict(f) for f in self.syntax_failures],
            "import_failures": [asdict(f) for f in self.import_failures],
            "missing_dependencies": [asdict(f) for f in self.missing_dependencies],
            "skipped_modules": list(self.skipped_modules),
            "import_checked": self.import_checked,
            "import_complete": self.import_complete,
            "probe_error": self.probe_error,
            "enumeration_error": self.enumeration_error,
        }


def _git_env() -> dict[str, str]:
    # Hooks, aliases and includes from the invoking user's git config must not run in an audit.
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_OPTIONAL_LOCKS="0")
    return env


def _tracked_python_files(root: Path) -> tuple[list[str] | None, str | None]:
    """``(paths, None)`` for a git checkout, ``(None, None)`` for an archive tree, and
    ``(None, error)`` when a git checkout cannot be enumerated (never a silent fallback)."""
    if not (root / ".git").exists():
        return None, None
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", "*.py"], cwd=root, capture_output=True,
            env=_git_env(), timeout=60, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"git ls-files failed: {type(exc).__name__}: {exc}"
    paths = [p for p in result.stdout.decode("utf-8", "surrogateescape").split("\0") if p]
    return sorted(paths), None


def _walked_python_files(root: Path) -> list[str]:
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _WALK_EXCLUDED_DIRS]
        rel_dir = Path(dirpath).relative_to(root)
        found.extend((rel_dir / name).as_posix() for name in filenames if name.endswith(".py"))
    return sorted(found)


def audit_syntax(root: Path, report: IntegrityReport) -> None:
    """Compile every Python file of the tree into *report*; nothing is written to *root*.

    ``compile`` rather than ``ast.parse``: a top-level ``return``/``await`` or a misplaced
    ``from __future__`` import builds an AST but is rejected by the compiler, and the
    compiler is what runs the file.
    """
    tracked, error = _tracked_python_files(root)
    if error:
        report.enumeration_error = error
        return
    report.scan = "git" if tracked is not None else "walk"
    relpaths = tracked if tracked is not None else _walked_python_files(root)
    if not relpaths:
        report.enumeration_error = "no Python files found: the tree under audit is empty or wrong"
        return
    for rel in relpaths:
        path = root / rel
        if not path.is_file():
            # A tracked path with no file is evidence we cannot read, not a deletion to ignore.
            report.syntax_failures.append(SyntaxFailure(rel, None, "tracked file is missing"))
            continue
        report.python_files += 1
        try:
            source = path.read_bytes()
            with warnings.catch_warnings():
                # Invalid-escape SyntaxWarnings in valid files are lint, not damage.
                warnings.simplefilter("ignore")
                compile(source, rel, "exec", dont_inherit=True)
        except SyntaxError as exc:
            report.syntax_failures.append(SyntaxFailure(rel, exc.lineno, exc.msg))
        except (OSError, ValueError, RecursionError, MemoryError) as exc:
            # ValueError: null bytes; RecursionError/MemoryError: pathological nesting.
            report.syntax_failures.append(SyntaxFailure(rel, None, f"{type(exc).__name__}: {exc}"))


def _module_present(root: Path, module: str) -> bool:
    base = root.joinpath(*module.split("."))
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


def _probe_source(
    modules: tuple[str, ...], first_party: tuple[str, ...], marker: str, root: str
) -> str:
    return (
        # Before any candidate import: this interpreter must not write bytecode into the tree.
        "import sys\n"
        "sys.dont_write_bytecode = True\n"
        "import importlib, json, os\n"
        # Importing hermes_cli.main runs the startup dotenv load, which pulls external secret
        # sources unless argv says ``update``; the probe checks importability only.
        "sys.argv = ['hermes', 'update']\n"
        "rows = []\n"
        f"for name in {modules!r}:\n"
        "    try:\n"
        "        mod = importlib.import_module(name)\n"
        # A module that resolved outside the candidate (an installed copy, a namespace-package
        # shadow) proves nothing about the candidate.
        f"        origin = os.path.realpath(getattr(mod, '__file__', '') or '')\n"
        f"        if not origin.startswith({os.path.realpath(root) + os.sep!r}):\n"
        "            rows.append((name, 'ShadowedModule', 'imported from ' + origin))\n"
        "    except ModuleNotFoundError as exc:\n"
        "        missing = (getattr(exc, 'name', '') or '').split('.')[0]\n"
        f"        third_party = not (missing in {first_party!r} or missing.startswith('hermes_'))\n"
        "        rows.append((name, 'missing-dependency' if third_party else 'ModuleNotFoundError', str(exc)))\n"
        "    except BaseException as exc:\n"
        "        rows.append((name, type(exc).__name__, str(exc)))\n"
        f"sys.stdout.write('\\n{marker}' + json.dumps(rows))\n"
    )


def _probe_env(sandbox: Path) -> dict[str, str]:
    """A child environment with no live profile, secret or runtime-path state.

    ``HERMES_HOME`` alone is not isolation: bootstrap and profile lookup also read ``HOME``,
    the XDG dirs and every ``HERMES_*`` override (kanban db, root home, profile, session).
    """
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
    home = sandbox / "home"
    for name in ("home/hermes", "tmp", "xdg-config", "xdg-cache", "xdg-state", "locks"):
        (sandbox / name).mkdir(parents=True, exist_ok=True)
    env.update(
        HOME=str(home), USERPROFILE=str(home), HERMES_HOME=str(home / "hermes"),
        HERMES_DISABLE_LAZY_INSTALLS="1", HERMES_GATEWAY_LOCK_DIR=str(sandbox / "locks"),
        TMPDIR=str(sandbox / "tmp"), TMP=str(sandbox / "tmp"), TEMP=str(sandbox / "tmp"),
        XDG_CONFIG_HOME=str(sandbox / "xdg-config"), XDG_CACHE_HOME=str(sandbox / "xdg-cache"),
        XDG_STATE_HOME=str(sandbox / "xdg-state"), PYTHONDONTWRITEBYTECODE="1",
        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    return env


def _parse_rows(output: str, marker: str, requested: tuple[str, ...]) -> list[tuple[str, str, str]]:
    """Exact protocol: one marker, then a JSON list of ``[module, kind, detail]`` strings."""
    if output.count(marker) != 1:
        raise ValueError("marker missing or repeated")
    rows = json.loads(output.split(marker, 1)[1])
    if not isinstance(rows, list):
        raise ValueError("payload is not a list")
    parsed = []
    for row in rows:
        if (not isinstance(row, list) or len(row) != 3
                or not all(isinstance(v, str) for v in row) or row[0] not in requested):
            raise ValueError("malformed row")
        parsed.append((row[0], row[1], row[2]))
    return parsed


def audit_imports(
    root: Path, report: IntegrityReport, *, modules: tuple[str, ...] | None = None,
    python: str | Path | None = None, strict: bool = True,
) -> None:
    """Import *modules* in a sandboxed child and record failures into *report*."""
    from hermes_cli._launchers import runtime_command
    from hermes_constants import FIRST_PARTY_MODULE_ROOTS

    wanted = tuple(modules or INTEGRITY_IMPORT_MODULES)
    present = tuple(m for m in wanted if _module_present(root, m))
    report.skipped_modules = [m for m in wanted if m not in present]
    if strict:
        # A publish gate certifies a fully importable candidate: a requested module that is
        # not in the tree is a gap, not a refactor to wave through.
        report.import_failures.extend(
            ImportFailure(m, "MissingModule", "module is not present in the tree")
            for m in report.skipped_modules)
    if not present:
        return  # nothing importable to probe; ``import_checked`` stays False for the caller to see
    marker = f"__HERMES_INTEGRITY_{secrets.token_hex(16)}__"
    sandbox = Path(tempfile.mkdtemp(prefix="hermes-integrity-"))
    try:
        env = _probe_env(sandbox)
        command = runtime_command(
            root, code=_probe_source(present, tuple(sorted(FIRST_PARTY_MODULE_ROOTS)), marker, str(root)),
            python=python, home=env["HERMES_HOME"])
        # ``-I`` ignores PYTHONDONTWRITEBYTECODE; ``-B`` does not.
        command.insert(1, "-B")
        report.import_checked = True
        try:
            result = subprocess.run(
                command, cwd=str(root), capture_output=True, text=True, encoding="utf-8",
                errors="replace", env=env, timeout=_IMPORT_PROBE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            report.probe_error = "import probe timed out before reporting import health"
            return
        except (OSError, subprocess.SubprocessError) as exc:
            report.probe_error = f"import probe could not start: {exc}"
            return
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-1:] or [""]
        report.probe_error = f"import probe exited {result.returncode}: {tail[0]}"
        return
    try:
        rows = _parse_rows(result.stdout or "", marker, present)
    except (TypeError, ValueError):
        report.probe_error = "import probe reported a malformed health payload"
        return
    for module, kind, detail in rows:
        if kind == "missing-dependency":
            failure = ImportFailure(module, "ModuleNotFoundError", detail)
            (report.import_failures if strict else report.missing_dependencies).append(failure)
        else:
            report.import_failures.append(ImportFailure(module, kind, detail))


def audit_tree(
    root: str | Path, *, check_imports: bool = True, modules: tuple[str, ...] | None = None,
    python: str | Path | None = None, strict: bool = True,
) -> IntegrityReport:
    """Audit *root* read-only. ``strict=False`` tolerates third-party modules that are not
    installed yet (pre-dependency update stages) and says so via ``import_complete``."""
    root = Path(root).resolve()
    report = IntegrityReport(root=str(root), strict=strict)
    if not root.is_dir():
        report.enumeration_error = f"not a directory: {root}"
        return report
    audit_syntax(root, report)
    # Importing a tree that does not compile only repeats the syntax finding.
    if check_imports and not (report.syntax_failures or report.enumeration_error):
        audit_imports(root, report, modules=modules, python=python, strict=strict)
    return report


def failure_lines(
    root: str | Path, *, strict: bool = False, check_imports: bool = True,
) -> list[str] | None:
    """One line per finding for a failing audit of *root*, ``None`` when it passes.

    The single seam every updater boundary goes through (pre-push, post-pull, ZIP download, strict
    gate after dependency preparation). Pre-dependency gates use ``strict=False``; the pre-publication
    and post-dependency gates pass ``strict=True``; a downloaded-but-not-installed tree is compile-only
    (``check_imports=False``).
    """
    report = audit_tree(root, strict=strict, check_imports=check_imports)
    return None if report.ok else _render_text(report).splitlines()[1:] or ["integrity audit failed"]


def _render_text(report: IntegrityReport) -> str:
    lines = [f"tree integrity: {'OK' if report.ok else 'FAILED'} "
             f"({report.python_files} python files, scan={report.scan}, "
             f"import_complete={report.import_complete})"]
    lines += [f"  syntax  {f.path}:{f.line}: {f.error}" for f in report.syntax_failures]
    lines += [f"  import  {f.module}: {f.kind}: {f.detail}" for f in report.import_failures]
    lines += [f"  deps    {f.module}: {f.detail}" for f in report.missing_dependencies]
    for label, value in (("probe", report.probe_error), ("scan", report.enumeration_error)):
        if value:
            lines.append(f"  {label:<7} {value}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Exit 0 clean, 1 findings, 2 the audit itself could not establish a verdict."""
    parser = argparse.ArgumentParser(prog="python -m hermes_cli.update_cmd_integrity", description=__doc__)
    parser.add_argument("--root", required=True, help="checkout or archive tree to audit")
    parser.add_argument("--no-imports", action="store_true", help="compile audit only")
    parser.add_argument("--allow-missing-deps", action="store_true",
                        help="pre-dependency mode: missing third-party modules are reported, not failed")
    parser.add_argument("--python", help="interpreter for the import probe child")
    parser.add_argument("--json", action="store_true", help="print one JSON report on stdout")
    args = parser.parse_args(argv)
    try:
        report = audit_tree(args.root, check_imports=not args.no_imports, python=args.python,
                            strict=not args.allow_missing_deps)
    except Exception as exc:  # noqa: BLE001 - the contract is a deterministic verdict, never a traceback
        report = IntegrityReport(root=str(args.root), strict=not args.allow_missing_deps)
        report.probe_error = f"audit crashed: {type(exc).__name__}: {exc}"
    print(json.dumps(report.to_dict()) if args.json else _render_text(report))
    if report.probe_error or report.enumeration_error:
        return 2
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
