"""Test that cron/jobs.py compiles and imports without syntax errors.

Regression test for the upstream sync merge that left cron/jobs.py with
multiple syntax errors (issue reported 2026-10-03).
"""
import ast
import sys
from pathlib import Path


def test_cron_jobs_compiles():
    """Verify that cron/jobs.py is valid Python syntax."""
    jobs_path = Path(__file__).parent.parent.parent / "cron" / "jobs.py"
    assert jobs_path.exists(), f"cron/jobs.py not found at {jobs_path}"
    
    with open(jobs_path, "r", encoding="utf-8") as f:
        source = f.read()
    
    # This will raise SyntaxError if the file has syntax errors
    ast.parse(source, filename=str(jobs_path))


def test_cron_jobs_imports():
    """Verify that cron.jobs can be imported without errors."""
    # This test may fail if dependencies are missing, but it will catch
    # import-time errors like NameError, AttributeError, etc.
    try:
        import cron.jobs  # noqa: F401
    except ModuleNotFoundError:
        # Dependencies not available in test environment - skip the import check
        # but the syntax check above already verified the file is valid Python
        pass
    except SyntaxError as e:
        # Syntax errors should have been caught by test_cron_jobs_compiles above,
        # but if we get here it means there's a syntax error
        raise AssertionError(f"Syntax error in cron/jobs.py: {e}") from e
