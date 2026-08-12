import pytest

from supportguard.mcp.client import child_environment


def test_read_mcp_environment_is_capability_scoped(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-cross-process-boundary")
    monkeypatch.setenv("DATABASE_URL", "postgresql://broad")
    monkeypatch.setenv("MCP_READ_DATABASE_URL", "postgresql://read-only")
    monkeypatch.setenv("EMBEDDING_MODE", "deterministic-fixture")
    monkeypatch.setenv("EMBEDDING_REVISION", "fixture-revision")

    environment = child_environment("supportguard.mcp.read_server")

    assert environment["DATABASE_URL"] == "postgresql://read-only"
    assert environment["EMBEDDING_MODE"] == "deterministic-fixture"
    assert environment["EMBEDDING_REVISION"] == "fixture-revision"
    assert "DEEPSEEK_API_KEY" not in environment
    assert "MCP_ACTION_DATABASE_URL" not in environment


def test_action_mcp_environment_is_capability_scoped(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-cross-process-boundary")
    monkeypatch.setenv("DATABASE_URL", "postgresql://broad")
    monkeypatch.setenv("MCP_ACTION_DATABASE_URL", "postgresql://action-only")

    environment = child_environment("supportguard.mcp.action_server")

    assert environment["DATABASE_URL"] == "postgresql://action-only"
    assert "DEEPSEEK_API_KEY" not in environment
    assert "MCP_READ_DATABASE_URL" not in environment


def test_production_mcp_environment_never_falls_back_to_broad_database(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql://owner")
    monkeypatch.delenv("MCP_READ_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="MCP_READ_DATABASE_URL is required"):
        child_environment("supportguard.mcp.read_server")
