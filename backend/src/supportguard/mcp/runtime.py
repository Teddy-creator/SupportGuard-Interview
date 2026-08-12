from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Literal, Protocol

import anyio
from mcp import ClientSession

from supportguard.contracts.process_identity import process_birth_identity
from supportguard.mcp.client import raw_mcp_session
from supportguard.tools.capabilities import ACTION_PROPOSAL_CAPABILITIES, READ_CAPABILITIES

ServerName = Literal["read", "action"]
SupervisorState = Literal[
    "stopped",
    "starting",
    "handshaking",
    "ready",
    "degraded",
    "reconnecting",
    "failed",
    "draining",
    "closed",
]

EXPECTED_TOOLS: dict[ServerName, frozenset[str]] = {
    "read": READ_CAPABILITIES,
    "action": ACTION_PROPOSAL_CAPABILITIES,
}
FROZEN_SCHEMA_HASHES: dict[ServerName, str] = {
    "read": "55d4af013b70b2ed46a10046c63c8de888c455152e54dc5be4b05543706ca06c",
    "action": "b9508627d4959b332b082582b1cba31899c905d0b7934fc4168d7ce89fe70136",
}


@dataclass
class ServerHealth:
    state: SupervisorState
    process: str
    session: str
    schema: str
    schema_hash: str | None
    reconnects: int
    last_error: str | None
    pending_calls: int
    generation: int
    pid: int | None
    process_group: int | None
    process_birth_identity: dict[str, object] | None
    shutdown_sequence: tuple[str, ...]
    pid_alive: bool
    runner_task_state: str
    runner_error_type: str | None


@dataclass(frozen=True)
class MCPCallResult:
    value: Any
    attempts: int
    lifecycle: dict[str, Any] = field(default_factory=dict)


class MCPTransportFailure(RuntimeError):
    """Secret-safe physical transport failure with durable lifecycle data."""

    def __init__(self, lifecycle: dict[str, Any]) -> None:
        super().__init__("mcp_transport_failed")
        self.lifecycle = lifecycle
        self.error_family = str(lifecycle.get("error_family", "unknown"))


class ToolTransport(Protocol):
    async def call(
        self,
        server_name: ServerName,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        reconnect_once: bool,
    ) -> MCPCallResult: ...

    async def rehandshake(
        self,
        server_name: ServerName,
        *,
        failed_generation: int | None = None,
    ) -> int: ...


class ManagedServer:
    def __init__(self, name: ServerName, module: str) -> None:
        self.name = name
        self.module = module
        self.session: ClientSession | None = None
        self.schema_hash: str | None = None
        self.reconnects = 0
        self.last_error: str | None = None
        self.started_at: float | None = None
        self.state: SupervisorState = "stopped"
        self.generation = 0
        self.pending_calls: set[asyncio.Task[Any]] = set()
        self._lifecycle_lock = asyncio.Lock()
        self._reconnect_lock = asyncio.Lock()
        self._call_gate = asyncio.Semaphore(3 if name == "read" else 1)
        self.transition_history: list[SupervisorState] = ["stopped"]
        self.pid: int | None = None
        self.process_group: int | None = None
        self.process_birth_identity: dict[str, object] | None = None
        self.handshake_phases: tuple[str, ...] = ()
        self.last_pid: int | None = None
        self.shutdown_sequence: list[str] = []
        self._pid_file: Path | None = None
        self._runner_task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._runner_error: BaseException | None = None

    def _transition(self, state: SupervisorState) -> None:
        self.state = state
        self.transition_history.append(state)

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self.state == "ready":
                return
            self._transition("starting")
            pid_fd, pid_path = tempfile.mkstemp(
                prefix=f"supportguard-mcp-{os.getpid()}-{self.name}-", suffix=".pid"
            )
            os.close(pid_fd)
            self._pid_file = Path(pid_path)
            self._stop_event = asyncio.Event()
            ready: asyncio.Future[ClientSession] = asyncio.get_running_loop().create_future()
            self._runner_error = None
            self._runner_task = asyncio.create_task(
                self._run_session(ready=ready, stop_event=self._stop_event)
            )
            try:
                async with asyncio.timeout(10):
                    self.session = await asyncio.shield(ready)
                self.handshake_phases = ("initialize",)
                self._capture_process_identity()
                self._transition("handshaking")
                await self._verify_schema()
                self.handshake_phases = ("initialize", "discovery", "schema_verified")
            except BaseException:
                self._transition("failed")
                await self._stop_unlocked()
                raise
            self.started_at = monotonic()
            self.last_error = None
            self.generation += 1
            self._transition("ready")

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            await self._stop_unlocked()

    def cancel_pending_calls(self) -> None:
        if self.state not in {"closed", "stopped"}:
            self._transition("draining")
        for task in tuple(self.pending_calls):
            task.cancel()

    async def _stop_unlocked(self) -> None:
        self._transition("draining")
        pending = tuple(self.pending_calls)
        self.cancel_pending_calls()
        if pending:
            with suppress(BaseException):
                async with asyncio.timeout(5):
                    await asyncio.gather(*pending, return_exceptions=True)
        self.session = None
        self.started_at = None
        pid = self.pid
        runner, self._runner_task = self._runner_task, None
        stop_event, self._stop_event = self._stop_event, None
        if stop_event is not None:
            self.shutdown_sequence.append("STDIO_CLOSE")
            stop_event.set()
        if runner is not None and not runner.done():
            with suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(runner), timeout=0.75)
        if pid is not None and _pid_alive(pid):
            await self._terminate_owned_process(pid)
        if runner is not None and not runner.done():
            with suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(runner), timeout=5)
        if runner is not None and not runner.done():
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        if pid is not None and _pid_alive(pid):
            self._transition("failed")
            raise RuntimeError(f"{self.name} MCP child PID {pid} did not terminate")
        self.last_pid = pid
        self.pid = None
        self.process_group = None
        self.process_birth_identity = None
        self.handshake_phases = ()
        if self._pid_file is not None:
            self._pid_file.unlink(missing_ok=True)
            self._pid_file = None
        self._transition("closed")

    async def _run_session(
        self,
        *,
        ready: asyncio.Future[ClientSession],
        stop_event: asyncio.Event,
    ) -> None:
        try:
            async with raw_mcp_session(self.module, pid_file=self._pid_file) as session:
                await session.initialize()
                if not ready.done():
                    ready.set_result(session)
                await stop_event.wait()
        except BaseException as exc:
            self._runner_error = exc
            if not ready.done():
                ready.set_exception(exc)

    def _capture_process_identity(self) -> None:
        if self._pid_file is None or not self._pid_file.is_file():
            raise RuntimeError(f"{self.name} MCP child did not publish a PID")
        try:
            pid = int(self._pid_file.read_text(encoding="ascii").strip())
            process_group = os.getpgid(pid)
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"{self.name} MCP child published an invalid PID") from exc
        if pid <= 1 or process_group != pid or not _pid_alive(pid):
            raise RuntimeError(f"{self.name} MCP child process identity is not isolated")
        self.pid = pid
        self.process_group = process_group
        self.process_birth_identity = process_birth_identity(pid).payload()

    async def _terminate_owned_process(self, pid: int) -> None:
        for name, sig, timeout_seconds in (
            ("SIGINT", signal.SIGINT, 0.5),
            ("SIGTERM", signal.SIGTERM, 0.75),
            ("SIGKILL", signal.SIGKILL, 0.75),
        ):
            if not _pid_alive(pid):
                return
            self.shutdown_sequence.append(name)
            try:
                os.killpg(pid, sig)
            except ProcessLookupError:
                return
            deadline = monotonic() + timeout_seconds
            while _pid_alive(pid) and monotonic() < deadline:  # noqa: ASYNC110
                await asyncio.sleep(0.02)

    async def reconnect(self, *, failed_generation: int) -> None:
        async with self._reconnect_lock:
            if self.generation != failed_generation and self.state == "ready":
                return
            self._transition("reconnecting")
            await self.stop()
            self.reconnects += 1
            await self.start()

    async def _verify_schema(self) -> None:
        if self.session is None:
            raise RuntimeError(f"{self.name} MCP session is not initialized")
        result = await self.session.list_tools()
        names = {tool.name for tool in result.tools}
        if names != EXPECTED_TOOLS[self.name]:
            raise RuntimeError(
                f"{self.name} MCP schema mismatch: expected {sorted(EXPECTED_TOOLS[self.name])}, "
                f"got {sorted(names)}"
            )
        schema_payload = [
            {
                "name": tool.name,
                "input": tool.inputSchema,
                "output": tool.outputSchema,
            }
            for tool in sorted(result.tools, key=lambda item: item.name)
        ]
        self.schema_hash = hashlib.sha256(
            json.dumps(schema_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if self.schema_hash != FROZEN_SCHEMA_HASHES[self.name]:
            raise RuntimeError(
                f"{self.name} MCP schema hash drift: expected "
                f"{FROZEN_SCHEMA_HASHES[self.name]}, got {self.schema_hash}"
            )

    async def call(self, tool_name: str, arguments: dict[str, Any], timeout_seconds: float) -> Any:
        if self.state != "ready" or self.session is None:
            raise RuntimeError(f"{self.name} MCP is not ready")
        async with self._call_gate:
            session = self.session
            task = asyncio.create_task(session.call_tool(tool_name, arguments))
            self.pending_calls.add(task)
            try:
                async with asyncio.timeout(timeout_seconds):
                    return await task
            finally:
                self.pending_calls.discard(task)

    def health(self) -> ServerHealth:
        runner_alive = self._runner_task is not None and not self._runner_task.done()
        ready = self.session is not None and self.schema_hash is not None and runner_alive
        return ServerHealth(
            state=self.state,
            process="running" if self.pid is not None and _pid_alive(self.pid) else "stopped",
            session="ready" if ready else "unavailable",
            schema="verified" if ready else "unverified",
            schema_hash=self.schema_hash,
            reconnects=self.reconnects,
            last_error=self.last_error,
            pending_calls=len(self.pending_calls),
            generation=self.generation,
            pid=self.pid,
            process_group=self.process_group,
            process_birth_identity=self.process_birth_identity,
            shutdown_sequence=tuple(self.shutdown_sequence),
            pid_alive=bool(self.pid is not None and _pid_alive(self.pid)),
            runner_task_state=_task_state(self._runner_task),
            runner_error_type=(
                type(self._runner_error).__name__ if self._runner_error is not None else None
            ),
        )


class MCPManager:
    def __init__(self, *, timeout_seconds: float = 10.0) -> None:
        self.timeout_seconds = timeout_seconds
        self.servers: dict[ServerName, ManagedServer] = {
            "read": ManagedServer("read", "supportguard.mcp.read_server"),
            "action": ManagedServer("action", "supportguard.mcp.action_server"),
        }

    async def start(self) -> None:
        started: list[ManagedServer] = []
        try:
            for server in self.servers.values():
                await server.start()
                started.append(server)
        except BaseException:
            for server in reversed(started):
                await server.stop()
            raise

    async def stop(self) -> None:
        servers = tuple(self.servers.values())
        for server in servers:
            server.cancel_pending_calls()
        await asyncio.gather(*(server.stop() for server in servers))

    async def rehandshake(
        self,
        server_name: ServerName,
        *,
        failed_generation: int | None = None,
    ) -> int:
        """Replace one failed session and prove initialize/discovery/schema again."""

        server = self.servers[server_name]
        observed_generation = server.generation if failed_generation is None else failed_generation
        try:
            await server.reconnect(failed_generation=observed_generation)
        except BaseException as exc:
            server.last_error = type(exc).__name__
            server._transition("failed")
            raise RuntimeError(f"{server_name} MCP rehandshake failed") from exc
        health = server.health()
        if (
            server.generation <= observed_generation
            or health.state != "ready"
            or health.session != "ready"
            or health.schema != "verified"
            or health.schema_hash != FROZEN_SCHEMA_HASHES[server_name]
        ):
            server._transition("failed")
            raise RuntimeError(f"{server_name} MCP rehandshake did not become ready")
        return server.generation

    async def call(
        self,
        server_name: ServerName,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        reconnect_once: bool,
    ) -> MCPCallResult:
        if tool_name not in EXPECTED_TOOLS[server_name]:
            raise RuntimeError(f"tool {tool_name} is not allowed on {server_name} MCP")
        server = self.servers[server_name]
        started_at = datetime.now(UTC)
        started = monotonic()
        arguments_hash = hashlib.sha256(
            json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()

        def lifecycle(*, outcome: str, error_family: str | None, attempts: int) -> dict[str, Any]:
            health = server.health()
            phases = list(server.handshake_phases)
            if server.reconnects:
                phases.append("rehandshake")
            phases.extend(("call", "terminal"))
            return {
                "schema_version": "mcp-transport-lifecycle.v1",
                "server": server_name,
                "tool_name": tool_name,
                "arguments_hash": arguments_hash,
                "phase_sequence": phases,
                "started_at": started_at.isoformat(),
                "completed_at": datetime.now(UTC).isoformat(),
                "duration_ms": max(0, int((monotonic() - started) * 1000)),
                "configured_timeout_seconds": self.timeout_seconds,
                "outcome": outcome,
                "error_family": error_family,
                "physical_attempts": attempts,
                "session_generation": health.generation,
                "pid": health.pid,
                "process_group": health.process_group,
                "process_birth_identity": health.process_birth_identity,
                "supervisor_state": health.state,
                "pending_calls": health.pending_calls,
                "reconnect_count": health.reconnects,
                "pid_alive": bool(health.pid is not None and _pid_alive(health.pid)),
                "runner_task_state": _task_state(server._runner_task),
                "runner_error_type": (
                    type(server._runner_error).__name__
                    if server._runner_error is not None
                    else None
                ),
                "session_ready": health.session == "ready",
                "schema_verified": health.schema == "verified",
            }

        try:
            if server.session is None or (
                server._runner_task is not None and server._runner_task.done()
            ):
                raise RuntimeError(f"{server_name} MCP transport closed")
            value = await server.call(tool_name, arguments, self.timeout_seconds)
            return MCPCallResult(
                value=value,
                attempts=1,
                lifecycle=lifecycle(outcome="succeeded", error_family=None, attempts=1),
            )
        except Exception as exc:
            server.last_error = type(exc).__name__
            if not reconnect_once or not is_retryable_mcp_failure(exc):
                server._transition("degraded")
                failure_lifecycle = lifecycle(
                    outcome="failed",
                    error_family=classify_mcp_failure(exc),
                    attempts=1,
                )
                failure_lifecycle.update(_safe_protocol_failure(exc))
                raise MCPTransportFailure(failure_lifecycle) from exc
            await self.rehandshake(server_name)
            try:
                value = await server.call(tool_name, arguments, self.timeout_seconds)
            except Exception as retry_exc:
                server.last_error = type(retry_exc).__name__
                server._transition("degraded")
                failure_lifecycle = lifecycle(
                    outcome="failed",
                    error_family=classify_mcp_failure(retry_exc),
                    attempts=2,
                )
                failure_lifecycle.update(_safe_protocol_failure(retry_exc))
                raise MCPTransportFailure(failure_lifecycle) from retry_exc
            return MCPCallResult(
                value=value,
                attempts=2,
                lifecycle=lifecycle(outcome="succeeded", error_family=None, attempts=2),
            )

    def health(self) -> dict[str, dict[str, object]]:
        return {
            name: {
                "process": value.process,
                "state": value.state,
                "session": value.session,
                "schema": value.schema,
                "schema_hash": value.schema_hash,
                "reconnects": value.reconnects,
                "last_error": value.last_error,
                "pending_calls": value.pending_calls,
                "generation": value.generation,
                "pid": value.pid,
                "process_group": value.process_group,
                "process_birth_identity": value.process_birth_identity,
                "shutdown_sequence": list(value.shutdown_sequence),
                "pid_alive": value.pid_alive,
                "runner_task_state": value.runner_task_state,
                "runner_error_type": value.runner_error_type,
            }
            for name, value in (
                (server_name, server.health()) for server_name, server in self.servers.items()
            )
        }


def is_retryable_mcp_failure(exc: Exception) -> bool:
    if isinstance(
        exc,
        (
            TimeoutError,
            ConnectionError,
            EOFError,
            BrokenPipeError,
            asyncio.IncompleteReadError,
            anyio.BrokenResourceError,
            anyio.ClosedResourceError,
            anyio.EndOfStream,
        ),
    ):
        return True
    if isinstance(exc, RuntimeError):
        message = str(exc).lower()
        forbidden = ("schema", "permission", "forbidden", "fence", "not allowed")
        if any(token in message for token in forbidden):
            return False
        return any(
            token in message
            for token in ("not ready", "not running", "session closed", "transport closed")
        )
    return False


def classify_mcp_failure(exc: BaseException) -> str:
    """Map private exception details to one stable, non-sensitive family."""

    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, (EOFError, asyncio.IncompleteReadError, anyio.EndOfStream)):
        return "stdio_closed"
    if isinstance(exc, (BrokenPipeError, anyio.BrokenResourceError, anyio.ClosedResourceError)):
        return "stdio_closed"
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    message = str(exc).lower()
    if "schema" in message:
        return "schema_mismatch"
    if "rehandshake" in message:
        return "rehandshake_failed"
    if "fence" in message or "lease" in message:
        return "lease_lost"
    if "transport closed" in message or "session closed" in message:
        return "stdio_closed"
    if "not ready" in message or "not running" in message:
        return "child_exit"
    return "unknown"


def _safe_protocol_failure(exc: BaseException) -> dict[str, object]:
    """Project protocol metadata without persisting exception text or payloads."""

    code: int | None = None
    error = getattr(exc, "error", None)
    raw_code = getattr(error, "code", None)
    if isinstance(raw_code, int) and not isinstance(raw_code, bool):
        code = raw_code
    protocol_categories: dict[int, str] = {
        -32700: "parse_error",
        -32600: "invalid_request",
        -32601: "method_not_found",
        -32602: "invalid_params",
        -32603: "internal_error",
    }
    category = protocol_categories.get(code) if code is not None else None
    if category is None:
        category = "server_error" if code is not None and -32099 <= code <= -32000 else "unknown"

    chain: list[str] = []
    current: BaseException | None = exc
    while current is not None and len(chain) < 4:
        name = type(current).__name__
        if name not in chain:
            chain.append(name)
        next_error = current.__cause__ or current.__context__
        current = next_error if isinstance(next_error, BaseException) else None
    return {
        "exception_type": type(exc).__name__,
        "cause_chain": chain,
        "protocol_error_code": code,
        "protocol_category": category,
    }


def _task_state(task: asyncio.Task[Any] | None) -> str:
    if task is None:
        return "absent"
    if task.cancelled():
        return "cancelled"
    return "done" if task.done() else "running"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
