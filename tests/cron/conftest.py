"""Cron-test fixtures.

Provides a default ``HERMES_MODEL`` for cron run_job tests so each one
doesn't have to spell out a model. The global conftest blanks
HERMES_MODEL hermetically; without this autouse fixture every cron test
that exercises ``run_job`` would hit the fail-fast guard added in
``cron/scheduler.py`` (see issue #23979) and have to be rewritten.

Tests that specifically need ``HERMES_MODEL`` unset — model-resolution
edge cases — call ``monkeypatch.delenv("HERMES_MODEL", raising=False)``
inside the test, which overrides this fixture's value for that scope.
"""

import pytest


@pytest.fixture()
def make_cron_provider():
    """Factory for minimal CronScheduler test doubles.

    ``make_cron_provider(register_job=...)`` returns a real ``CronScheduler``
    subclass instance whose ``register_job`` is the given callable — so tests
    exercising the creation-registration contract share one stub instead of
    redefining inline spy/failing classes, and an ABC rename breaks them
    loudly instead of silently passing a duck-type.
    """
    from cron.scheduler_provider import CronScheduler

    def _make(register_job=None, name="stub"):
        class _StubProvider(CronScheduler):
            @property
            def name(self):  # pragma: no cover - trivial
                return name

            def start(self, stop_event, **kw):  # pragma: no cover - unused
                pass

            def register_job(self, job):
                if register_job is not None:
                    return register_job(job)
                return None

        return _StubProvider()

    return _make


@pytest.fixture(autouse=True)
def _no_managed_store(tmp_path, monkeypatch):
    """Point PM's store at an empty dir: script runs must not select the host install's
    dependency venv (POSIX cron scripts run on it when a store is committed)."""
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "no-pm-store"))


@pytest.fixture(autouse=True)
def _default_cron_test_model(monkeypatch):
    """Pin a default HERMES_MODEL so cron run_job tests have a resolvable model."""
    monkeypatch.setenv("HERMES_MODEL", "test-cron-default-model")
    yield


@pytest.fixture(autouse=True)
def _reset_session_context_vars():
    """Restore session ContextVars around cron tests that call run_job directly.

    Production confines each cron run to a copied context, but direct unit tests
    share the pytest context. ``run_job`` intentionally clears ordinary session
    variables to explicit empty values, which would otherwise shadow legacy env
    fallbacks used by later approval tests in the same process.
    """
    from gateway.session_context import _UNSET, _VAR_MAP

    def _reset_all():
        for var in _VAR_MAP.values():
            var.set(_UNSET)

    _reset_all()
    yield
    _reset_all()


@pytest.fixture(autouse=True)
def _bind_media_owner_roots_to_the_test_home(_isolate_hermes_home, monkeypatch):
    """The media owner (``gateway.platforms.base``) reads its home/root and the static allow roots
    at import. A module imported by an earlier test keeps that test's (or the launch) home, so a later
    test enumerates the stale tree and trips the home I/O guard before reaching the contract it is
    about. Rebind the owner's constants to THIS test's isolated home. Path authorization, the
    credential denylist and the home guard themselves are untouched."""
    import sys

    base = sys.modules.get("gateway.platforms.base")
    if base is None or not hasattr(base, "_HERMES_ROOT"):
        return
    from hermes_constants import get_default_hermes_root, get_hermes_home

    home = get_hermes_home()
    monkeypatch.setattr(base, "_HERMES_HOME", home)
    monkeypatch.setattr(base, "_HERMES_ROOT", get_default_hermes_root())
    legacy = ("image_cache", "audio_cache", "video_cache", "document_cache", "browser_screenshots")
    monkeypatch.setattr(base, "MEDIA_DELIVERY_SAFE_ROOTS", (
        *(home / "cache" / d for d in ("images", "audio", "videos", "documents", "screenshots")),
        *(home / d for d in legacy),
        *(home / "cache" / d for d in base._MEDIA_DELIVERY_CACHE_SUBDIRS)))
