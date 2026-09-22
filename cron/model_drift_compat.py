"""Compatibility shim: provides resolve_cron_model_drift_defaults when upstream removes it.

This module exists so the gateway cron bootstrap can survive upstream refactors that remove
resolve_cron_model_drift_defaults from hermes_cli.config. The scheduler tries hermes_cli.config first
(so it uses the canonical implementation when present), and falls back to this module on ImportError.

Historical context: resolve_cron_model_drift_defaults was added in commit 0469740ab3 (Sep 2026) to
support cron model drift detection, then moved/removed in upstream refactors. The fork needs it to
keep cron operational during merge windows.

Design: stdlib + typing only (no hermes_cli imports), so the fallback path has minimal dependencies
and can't fail on missing upstream symbols.
"""

import os
from typing import Any, Dict, Optional, Tuple


def _model_assignment_text(value: Any) -> str:
    """Return a trimmed scalar model/provider value, or empty for malformed data."""
    return value.strip() if isinstance(value, str) else ""


def resolve_cron_model_drift_defaults(
    config: Any,
    *,
    environ: Optional[Dict[str, str]] = None,
) -> Tuple[str, str]:
    """Resolve the global provider/model values cron compares against snapshots.

    Mirrors the scheduler's global-model precedence: a truthy configured model wins
    ``HERMES_MODEL``; the environment is only a fallback. Per-job and cron fleet defaults are
    handled by the caller/classifier because they suppress a drift axis rather than changing the
    global assignment.

    Args:
        config: The loaded config dict (typically from load_user_config_effective).
        environ: Environment dict override (defaults to os.environ).

    Returns:
        (provider, model) tuple where both are strings (possibly empty).
    """
    env = os.environ if environ is None else environ
    provider = ""
    model = _model_assignment_text(env.get("HERMES_MODEL", ""))
    model_config = config.get("model") if isinstance(config, dict) else None
    if isinstance(model_config, str):
        configured_model = model_config.strip()
        if configured_model:
            model = configured_model
    elif isinstance(model_config, dict):
        provider = _model_assignment_text(model_config.get("provider"))
        configured_model = _model_assignment_text(
            model_config.get("default")
            or model_config.get("model")
            or model_config.get("name")
        )
        if configured_model:
            model = configured_model
    return provider, model
