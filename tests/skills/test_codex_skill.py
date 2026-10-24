"""Behavior contracts for the bundled Codex skill."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SKILL_PATH = REPO / "skills" / "autonomous-ai-agents" / "codex" / "SKILL.md"
DOCS_PATH = (
    REPO
    / "website"
    / "docs"
    / "user-guide"
    / "skills"
    / "bundled"
    / "autonomous-ai-agents"
    / "autonomous-ai-agents-codex.md"
)


def _gateway_section(text: str) -> str:
    start = text.find("## Commit review from a gateway")
    assert start != -1, "document gateway commit review before the rules"
    end = text.find("\n## ", start + 1)
    assert end != -1
    return text[start:end]


def test_gateway_commit_review_rejects_sandbox_flag() -> None:
    """A gateway review must not pass --sandbox to the review subcommand."""
    section = _gateway_section(SKILL_PATH.read_text(encoding="utf-8"))
    assert "codex exec review --commit" in section
    assert "rejects `--sandbox`" in section
    assert "pty=true" in section
    assert "missing ledger claim is not a code defect" in section
    assert _gateway_section(DOCS_PATH.read_text(encoding="utf-8")) == section
