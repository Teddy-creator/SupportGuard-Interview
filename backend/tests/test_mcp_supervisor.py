from __future__ import annotations

import asyncio
from typing import Any

import pytest

from supportguard.mcp import read_server
from supportguard.mcp.runtime import (
    FROZEN_SCHEMA_HASHES,
    ManagedServer,
    MCPManager,
    classify_mcp_failure,
    is_retryable_mcp_failure,
)
from supportguard.rag.embeddings import DeterministicEmbedding


class BlockingSession:
    async def call_tool(self, _name: str, _arguments: dict[str, Any]) -> None:
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_supervisor_shutdown_cancels_and_drains_pending_calls() -> None:
    server = ManagedServer("read", "unused")
    server.session = BlockingSession()  # type: ignore[assignment]
    server.state = "ready"
    call = asyncio.create_task(server.call("query_account", {}, 30))
    await asyncio.sleep(0)
    assert server.health().pending_calls == 1
    await server.stop()
    result = await asyncio.gather(call, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    assert server.state == "closed"
    assert server.health().pending_calls == 0
    assert server.transition_history[-2:] == ["draining", "closed"]


@pytest.mark.asyncio
async def test_supervisor_rejects_calls_while_draining() -> None:
    server = ManagedServer("action", "unused")
    server.state = "draining"
    with pytest.raises(RuntimeError, match="not ready"):
        await server.call("propose_refund", {}, 0.1)


@pytest.mark.asyncio
async def test_rehandshake_does_not_restart_a_newer_healthy_generation() -> None:
    manager = MCPManager()
    server = manager.servers["read"]
    server.state = "ready"
    server.generation = 2
    server.schema_hash = FROZEN_SCHEMA_HASHES["read"]
    server.session = object()  # type: ignore[assignment]
    runner = asyncio.create_task(asyncio.Event().wait())
    server._runner_task = runner
    try:
        assert await manager.rehandshake("read", failed_generation=1) == 2
        assert server.generation == 2
        assert server.reconnects == 0
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
async def test_read_mcp_embedding_is_lazy_and_single_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedding = DeterministicEmbedding()
    builds = 0

    def build(_settings: object) -> DeterministicEmbedding:
        nonlocal builds
        builds += 1
        return embedding

    monkeypatch.setattr(read_server, "_embedding", None)
    monkeypatch.setattr(read_server, "_embedding_lock", asyncio.Lock())
    monkeypatch.setattr(read_server, "get_settings", lambda: object())
    monkeypatch.setattr(read_server, "build_embedding_provider", build)

    first, second = await asyncio.gather(
        read_server._embedding_provider(),
        read_server._embedding_provider(),
    )

    assert first is embedding
    assert second is embedding
    assert await read_server._embedding_provider() is embedding
    assert builds == 1


@pytest.mark.asyncio
async def test_read_mcp_lifespan_does_not_eagerly_load_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builds = 0

    class Settings:
        app_env = "development"
        database_url = "sqlite+aiosqlite:///:memory:"
        mcp_read_database_url = None

        def model_copy(self, *, update: dict[str, object]) -> Settings:
            del update
            return self

    class Engine:
        async def dispose(self) -> None:
            return None

    def forbidden_build(_settings: object) -> DeterministicEmbedding:
        nonlocal builds
        builds += 1
        return DeterministicEmbedding()

    monkeypatch.setattr(read_server, "get_settings", Settings)
    monkeypatch.setattr(read_server, "create_engine", lambda _settings: Engine())
    monkeypatch.setattr(
        read_server,
        "create_session_factory",
        lambda _engine, *, settings: object(),
    )

    async def current_schema(
        _factory: object,
        *,
        service: str,
        current_metadata_fixture: bool = False,
    ) -> None:
        assert service == "read_mcp"
        assert current_metadata_fixture is False

    monkeypatch.setattr(read_server, "require_current_runtime_schema", current_schema)
    monkeypatch.setattr(
        read_server,
        "create_scoped_session_factory",
        lambda _engine: object(),
    )
    monkeypatch.setattr(read_server, "build_embedding_provider", forbidden_build)

    async with read_server.server_lifespan(None):  # type: ignore[arg-type]
        assert read_server._embedding is None
        assert read_server._embedding_lock is not None
        assert builds == 0

    assert read_server._embedding is None
    assert read_server._embedding_lock is None
    assert builds == 0


@pytest.mark.asyncio
async def test_read_mcp_embedding_fails_closed_without_lifespan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(read_server, "_embedding", None)
    monkeypatch.setattr(read_server, "_embedding_lock", None)

    with pytest.raises(RuntimeError, match="lifespan is not initialized"):
        await read_server._embedding_provider()


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (TimeoutError("deadline"), True),
        (EOFError("stdio closed"), True),
        (RuntimeError("read MCP is not ready"), True),
        (RuntimeError("MCP schema hash drift"), False),
        (RuntimeError("stale fence"), False),
        (PermissionError("denied"), False),
        (ValueError("invalid arguments"), False),
    ],
)
def test_reconnect_classifier_is_fail_closed(error: Exception, retryable: bool) -> None:
    assert is_retryable_mcp_failure(error) is retryable


@pytest.mark.parametrize(
    ("error", "family"),
    [
        (TimeoutError("private deadline"), "timeout"),
        (EOFError("private stdio"), "stdio_closed"),
        (RuntimeError("child not running"), "child_exit"),
        (RuntimeError("schema hash drift"), "schema_mismatch"),
        (RuntimeError("rehandshake failed"), "rehandshake_failed"),
        (RuntimeError("lease lost"), "lease_lost"),
        (ValueError("private unknown detail"), "unknown"),
    ],
)
def test_transport_failure_family_is_stable_and_does_not_expose_message(
    error: Exception,
    family: str,
) -> None:
    assert classify_mcp_failure(error) == family
    assert "private" not in family
