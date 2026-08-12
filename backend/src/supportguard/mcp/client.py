from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from supportguard.contracts.mcp_lifecycle import (
    OWNER_NODE_ENV,
    PARTITION_ENV,
    PARTITION_LEADER_ENV,
    REGISTRY_ENV,
    write_closed_record,
    write_record,
)

ROOT = Path(__file__).resolve().parents[4]


@dataclass(frozen=True, slots=True)
class _RegistryIdentity:
    partition_id: str
    module: str
    pid: int
    partition_leader_pid: int
    owner_node: str


class _ObservedClientSession:
    def __init__(
        self,
        session: ClientSession,
        *,
        module: str,
        pid_file: Path | None,
        registry_raw: str | None,
    ) -> None:
        self._session = session
        self._module = module
        self._pid_file = pid_file
        self._registry_raw = registry_raw
        self._registry_identity: _RegistryIdentity | None = None
        self.confirmed: dict[str, object] | None = None
        self.discovery_count = 0
        self.call_count = 0

    async def initialize(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._session.initialize(*args, **kwargs)
        if self._registry_raw:
            self._registry_identity = self._register()
        return result

    async def list_tools(self, *args: Any, **kwargs: Any) -> Any:
        self.discovery_count += 1
        result = await self._session.list_tools(*args, **kwargs)
        if self._registry_raw and self.confirmed is None:
            if self._registry_identity is None:
                raise RuntimeError("mcp_registry_initialize_missing")
            self.confirmed = write_record(
                registry=Path(self._registry_raw),
                state="confirmed",
                schema_hash=_schema_hash(result.tools),
                partition_id=self._registry_identity.partition_id,
                module=self._registry_identity.module,
                pid=self._registry_identity.pid,
                partition_leader_pid=self._registry_identity.partition_leader_pid,
                owner_node=self._registry_identity.owner_node,
            )
        return result

    async def call_tool(self, *args: Any, **kwargs: Any) -> Any:
        self.call_count += 1
        return await self._session.call_tool(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    def _register(self) -> _RegistryIdentity:
        registry_raw = self._registry_raw
        partition_id = os.getenv(PARTITION_ENV)
        leader_raw = os.getenv(PARTITION_LEADER_ENV)
        owner_node = os.getenv(OWNER_NODE_ENV)
        if (
            registry_raw is None
            or not partition_id
            or not leader_raw
            or not owner_node
            or self._pid_file is None
        ):
            raise RuntimeError("mcp_registry_owner_context_missing")
        leader_pid = os.getpid() if leader_raw == "self" else int(leader_raw)
        pid_text = self._pid_file.read_text(encoding="ascii").strip()
        if not pid_text.isdigit():
            raise RuntimeError("mcp_registry_pid_registration_timeout")
        identity = _RegistryIdentity(
            partition_id=partition_id,
            module=self._module,
            pid=int(pid_text),
            partition_leader_pid=leader_pid,
            owner_node=owner_node,
        )
        write_record(
            registry=Path(registry_raw),
            state="registered",
            schema_hash=None,
            partition_id=identity.partition_id,
            module=identity.module,
            pid=identity.pid,
            partition_leader_pid=identity.partition_leader_pid,
            owner_node=identity.owner_node,
        )
        return identity


def _schema_hash(tools: list[Any]) -> str:
    payload = [
        {"name": tool.name, "input": tool.inputSchema, "output": tool.outputSchema}
        for tool in sorted(tools, key=lambda item: item.name)
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def child_environment(module: str, *, pid_file: Path | None = None) -> dict[str, str]:
    """Build a capability-specific environment without inheriting application secrets."""
    source = os.environ
    environment = {
        "PYTHONPATH": str(ROOT / "backend" / "src"),
        "PYTHONUNBUFFERED": "1",
        "SUPPORTGUARD_DISABLE_DOTENV": "1",
    }
    for name in (
        "APP_ENV",
        "EMBEDDING_MODE",
        "EMBEDDING_MODEL",
        "EMBEDDING_REVISION",
        "HF_HOME",
        "TRANSFORMERS_OFFLINE",
        "HOME",
        "TMPDIR",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
    ):
        if value := source.get(name):
            environment[name] = value
    database_variable = (
        "MCP_READ_DATABASE_URL" if module.endswith("read_server") else "MCP_ACTION_DATABASE_URL"
    )
    database_url = source.get(database_variable)
    if not database_url:
        if source.get("APP_ENV") == "production":
            raise RuntimeError(f"{database_variable} is required in production")
        database_url = source.get("DATABASE_URL")
    if database_url:
        environment["DATABASE_URL"] = database_url
        # Override any capability URL from the child's cwd .env as well; the
        # explicit parent capability URL is the authoritative subprocess target.
        environment[database_variable] = database_url
    if pid_file is not None:
        environment["SUPPORTGUARD_MCP_PID_FILE"] = str(pid_file)
    return environment


@asynccontextmanager
async def read_mcp_session() -> AsyncIterator[ClientSession]:
    async with mcp_session("supportguard.mcp.read_server") as session:
        yield session


@asynccontextmanager
async def action_mcp_session() -> AsyncIterator[ClientSession]:
    async with mcp_session("supportguard.mcp.action_server") as session:
        yield session


@asynccontextmanager
async def mcp_session(module: str, *, pid_file: Path | None = None) -> AsyncIterator[ClientSession]:
    """Compatibility session that owns initialize for historical callers."""

    async with raw_mcp_session(module, pid_file=pid_file) as session:
        await session.initialize()
        if os.getenv(REGISTRY_ENV):
            await session.list_tools()
        yield session


@asynccontextmanager
async def raw_mcp_session(
    module: str, *, pid_file: Path | None = None
) -> AsyncIterator[ClientSession]:
    """Open one stdio session without initialize, discovery, or tool calls."""

    registry_raw = os.getenv(REGISTRY_ENV)
    owned_pid_file = False
    if registry_raw and pid_file is None:
        descriptor, raw_path = tempfile.mkstemp(prefix="supportguard-mcp-observed-", suffix=".pid")
        os.close(descriptor)
        pid_file = Path(raw_path)
        owned_pid_file = True
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", module],
        env=child_environment(module, pid_file=pid_file),
    )
    observed: _ObservedClientSession | None = None
    try:
        async with (
            stdio_client(parameters) as (read, write),
            ClientSession(read, write) as session,
        ):
            observed = _ObservedClientSession(
                session,
                module=module,
                pid_file=pid_file,
                registry_raw=registry_raw,
            )
            yield cast(ClientSession, observed)
    finally:
        if registry_raw and observed is not None and observed.confirmed is not None:
            write_closed_record(
                registry=Path(registry_raw),
                confirmed=observed.confirmed,
                discovery_count=observed.discovery_count,
                call_count=observed.call_count,
            )
        if owned_pid_file and pid_file is not None:
            pid_file.unlink(missing_ok=True)


def structured_result(result: Any) -> dict[str, object]:
    if isinstance(result, dict):
        return dict(result)
    if result.isError:
        message = result.content[0].text if result.content else "MCP tool failed"
        raise RuntimeError(message)
    if result.structuredContent is not None:
        return dict(result.structuredContent)
    if not result.content:
        raise RuntimeError("MCP returned no content")
    import json

    return dict(json.loads(result.content[0].text))
