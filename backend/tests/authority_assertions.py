from __future__ import annotations


def assert_v2_supersedes_historical_authority(agents: str) -> None:
    """Assert the current authority without rewriting historical evidence facts."""
    assert "docs/interview-edition-simplification-v2.0.md" in agents
    assert "approved and frozen authority" in agents
    assert "Historical v1.2～v1.7 specifications" in agents
    assert "are read-only" in agents
    assert "do not authorize new work or historical Gate / Parity reruns" in agents
    assert "6255c8c0eb0dcedd877bfbf16a9695dad2a0c9eb" in agents
