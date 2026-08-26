"""Behavior contracts for the bundled Claude Code skill."""

from __future__ import annotations

import re
from pathlib import Path

SKILL_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "autonomous-ai-agents"
    / "claude-code"
    / "SKILL.md"
)


def test_stale_auth_status_requires_api_probe_and_recovery() -> None:
    """A status-only check must not authorize a long delegated build."""
    skill_text = SKILL_PATH.read_text(encoding="utf-8")
    pitfall = re.search(
        r"^\d+\. \*\*`claude auth status` can be stale.*?(?=^\d+\. |^## |\Z)",
        skill_text,
        flags=re.MULTILINE | re.DOTALL,
    )

    assert pitfall, "document stale OAuth status before long delegated builds"
    guidance = pitfall.group(0)
    assert "claude -p" in guidance and "--max-turns 1" in guidance
    assert "401" in guidance and "claude auth login" in guidance
    assert "fall back" in guidance and "current model" in guidance
