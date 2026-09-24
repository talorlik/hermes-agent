"""Regression: AIAgent forwards side_agent into init_agent as a real bool attr."""

from run_agent import AIAgent


def _construct(**kwargs):
    return AIAgent(
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-test-side-agent",
        model="test/model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        skip_background_review=True,
        **kwargs,
    )


def test_side_agent_defaults_false():
    agent = _construct()
    assert agent.side_agent is False


def test_side_agent_true():
    agent = _construct(side_agent=True)
    assert agent.side_agent is True
