"""Forced redaction of cron pre-run script results (fork-only module).

``cron.scheduler_script._run_job_script`` redacts a script's stdout and stderr under the
operator's ``security.redact_secrets`` setting and leaves URL credentials in place. Its early
returns (path resolution, launch failure, timeout) carry the script path or the exception text
unredacted. A script result is injected into the agent prompt and can be delivered to a chat, so
this fork treats the runner as a safety boundary: every result, on every return path, is redacted
with ``force=True`` and ``redact_url_credentials=True``.

The same holds for what this module logs. The log formatter redacts under the operator's
setting, not by force, so no exception text is ever handed to a log call here unredacted: a
raised runner is logged with its force-redacted message, a failing redactor with its type only.

``cron/scheduler_script.py`` rebinds its ``_run_job_script`` through
:func:`bind_forced_script_redaction`. That rebinding is the only fork edit in the upstream file,
so the logic here survives an upstream rewrite of the runner body.
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Any, Callable

from agent.redact import redact_sensitive_text
from cron.jobs import _ensure_cron_dir

# Log-record parity with the scheduler, like the sibling split modules.
logger = logging.getLogger("cron.scheduler")

REDACTION_FAILURE = "[REDACTED - cron script result unavailable]"
SCRIPTS_DIR_UNAVAILABLE = "Blocked: Hermes scripts directory is unavailable"
HOME_RESOLVER_MISSING = (
    "Blocked: cron.scheduler._get_hermes_home is unavailable, so the scripts directory cannot be "
    "resolved; cron/script_redaction.py needs updating for this Hermes version"
)

ScriptRunner = Callable[..., tuple[bool, str]]
_ExcInfo = tuple[type[BaseException], BaseException, TracebackType | None]


class _WithheldError(Exception):
    """Stands in, in a log record, for an exception whose own text must not be written."""


def _safe_exc_info(exc: BaseException, safe_text: str) -> _ExcInfo:
    """``exc_info`` for a log call: the real traceback under a stand-in that carries ``safe_text``.

    ``exc_info=True`` would have the handler print ``str(exc)`` as it is. Frames are kept because
    they name code, not values; the message is replaced by text the caller already made safe.
    """
    return _WithheldError, _WithheldError(safe_text), exc.__traceback__


def force_redact_script_text(output: object) -> str:
    """Force-redact one script result; a redactor failure yields a constant, never the text."""
    try:
        return redact_sensitive_text(str(output), force=True, redact_url_credentials=True)
    except Exception as exc:
        # The redactor is what failed, so its own message cannot be made safe: log the type only.
        kind = type(exc).__name__
        logger.warning(
            "Cron script result redaction failed (%s); result withheld",
            kind,
            exc_info=_safe_exc_info(exc, f"{kind}: message withheld"),
        )
        return REDACTION_FAILURE


def bind_forced_script_redaction(runner: ScriptRunner) -> ScriptRunner:
    """Wrap a script runner so every ``(success, output)`` it yields is force-redacted."""

    def _redacted_runner(*args: Any, **kwargs: Any) -> tuple[bool, str]:
        # Late import, and the only one: ``cron.scheduler`` imports ``cron.scheduler_script``,
        # which imports this module, so the name cannot be bound at module level. Resolving it
        # per call also honours a patched or per-profile resolver. A rename upstream fails the
        # job with a message that names what is missing instead of an AttributeError.
        try:
            from cron.scheduler import _get_hermes_home
        except ImportError:
            return False, HOME_RESOLVER_MISSING

        # The runner resolves the scripts directory before its own ``try``; an unusable home
        # (unresolvable, over-long, read-only) would raise with the home path in the message.
        # Refuse here with a constant that names no path.
        try:
            _ensure_cron_dir(_get_hermes_home() / "scripts")
        except (OSError, RuntimeError, ValueError):
            return False, SCRIPTS_DIR_UNAVAILABLE
        try:
            result = runner(*args, **kwargs)
            success, output = result[0], result[1]
        except Exception as exc:
            # A raised runner must not skip the scrub. Guaranteed here: the returned text and the
            # log record both carry the force-redacted message, never ``str(exc)`` itself.
            scrubbed = force_redact_script_text(exc)
            kind = type(exc).__name__
            logger.warning(
                "Cron script runner raised %s; reporting a redacted failure: %s",
                kind,
                scrubbed,
                exc_info=_safe_exc_info(exc, f"{kind}: {scrubbed}"),
            )
            return False, scrubbed
        return bool(success), force_redact_script_text(output)

    return _redacted_runner
