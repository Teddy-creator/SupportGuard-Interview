from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.agent.contracts import CONTEXT_VERSION, runtime_provenance
from supportguard.db.models import (
    AgentCallAttempt,
    AgentRun,
    ToolInvocation,
    ToolTransportAttempt,
)
from supportguard.db.session import runtime_code_version
from supportguard.observability.metrics import ATTEMPT_LATENCY, ATTEMPT_OUTCOMES
from supportguard.services.runtime_jobs import JobLease, RuntimeConflict, RuntimeJobRepository

_SAFE_STRUCTURED_ERROR_PATH = re.compile(
    r"^(?:\$|[A-Za-z_][A-Za-z0-9_]*(?:\.(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]+))*)"
    r":[A-Za-z_][A-Za-z0-9_]*(?::[A-Za-z0-9_. -]+)?$"
)

MAX_LLM_CALLS = 6
MAX_TOOL_ATTEMPTS = 6
MAX_TOOL_ROUNDS = 2


@dataclass(frozen=True)
class ReservedAttempt:
    id: str
    kind: str
    ordinal: int
    logical_invocation_id: str | None = None
    transport_ordinal: int | None = None
    transport_attempt_id: str | None = None


class AttemptLedger:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def reserve(
        self,
        lease: JobLease,
        *,
        kind: str,
        logical_invocation_id: str | None = None,
        transport_ordinal: int | None = None,
        actual_provider: tuple[str, str, str] | None = None,
        repair_of_attempt_id: str | None = None,
    ) -> ReservedAttempt:
        await RuntimeJobRepository(self.session).assert_fence(lease)
        if kind == "structure_repair" and not repair_of_attempt_id:
            raise ValueError("structure repair requires repair_of_attempt_id")
        if kind != "structure_repair" and repair_of_attempt_id is not None:
            raise ValueError("repair_of_attempt_id is only valid for structure repair")
        run = await self.session.get(AgentRun, lease.run_id, with_for_update=True)
        if run is None:
            raise RuntimeConflict("run_not_found")
        if kind == "structure_repair":
            original = await self.session.get(AgentCallAttempt, repair_of_attempt_id)
            if (
                original is None
                or original.run_id != lease.run_id
                or original.job_id != lease.job_id
                or original.call_kind != "llm"
                or original.status != "failed"
            ):
                raise RuntimeConflict("structure_repair_origin_invalid")
            existing_repair = await self.session.scalar(
                select(AgentCallAttempt.id).where(
                    AgentCallAttempt.run_id == lease.run_id,
                    AgentCallAttempt.call_kind == "structure_repair",
                )
            )
            if existing_repair is not None:
                raise RuntimeConflict("structure_repair_already_used")
        if kind in {"llm", "structure_repair"}:
            if run.llm_calls >= MAX_LLM_CALLS:
                raise RuntimeConflict("llm_budget_exhausted")
            run.llm_calls += 1
        elif kind in {"read_mcp", "tool_preflight"}:
            if logical_invocation_id is None:
                raise ValueError("tool attempt requires logical_invocation_id")
            invocation = await self.session.get(
                ToolInvocation, logical_invocation_id, with_for_update=True
            )
            if (
                invocation is None
                or invocation.run_id != lease.run_id
                or invocation.job_id != lease.job_id
                or invocation.fencing_token != lease.fencing_token
                or invocation.lifecycle == "terminal"
            ):
                raise RuntimeConflict("tool_invocation_not_reservable")
            if kind == "read_mcp" and (transport_ordinal is None or transport_ordinal < 1):
                raise ValueError("read_mcp attempt requires positive transport_ordinal")
            if kind == "tool_preflight" and transport_ordinal is not None:
                raise ValueError("tool_preflight cannot reserve transport")
            if run.tool_attempts >= MAX_TOOL_ATTEMPTS:
                raise RuntimeConflict("tool_budget_exhausted")
            run.tool_attempts += 1
        else:
            raise ValueError(f"unknown attempt kind: {kind}")
        current = await self.session.scalar(
            select(func.max(AgentCallAttempt.ordinal)).where(
                AgentCallAttempt.run_id == lease.run_id,
                AgentCallAttempt.call_kind == kind,
            )
        )
        model, provider_mode, tool_call_mode = actual_provider or (
            run.model,
            run.provider_mode,
            run.tool_call_mode,
        )
        provenance = runtime_provenance(
            model=model,
            provider_mode=provider_mode,
            tool_call_mode=tool_call_mode,
            context_version=CONTEXT_VERSION,
            code_version=runtime_code_version(self.session),
        )
        provenance = {
            **provenance,
            "worker_execution": {
                "lease_owner": lease.owner,
                "fencing_token": lease.fencing_token,
                "job_id": lease.job_id,
            },
        }
        if kind == "structure_repair":
            provenance = {
                **provenance,
                "repair_of_attempt_id": repair_of_attempt_id,
                "repair_contract": "strict-structure-repair.v1",
            }
        attempt = AgentCallAttempt(
            tenant_id=lease.tenant_id,
            run_id=lease.run_id,
            job_id=lease.job_id,
            fencing_token=lease.fencing_token,
            call_kind=kind,
            ordinal=int(current or 0) + 1,
            logical_invocation_id=logical_invocation_id,
            transport_ordinal=transport_ordinal,
            status="started",
            runtime_provenance=provenance,
        )
        self.session.add(attempt)
        await self.session.flush()
        transport: ToolTransportAttempt | None = None
        if kind == "read_mcp":
            if logical_invocation_id is None or transport_ordinal is None:
                raise RuntimeConflict("transport_identity_missing")
            transport = ToolTransportAttempt(
                tenant_id=lease.tenant_id,
                run_id=lease.run_id,
                job_id=lease.job_id,
                invocation_id=logical_invocation_id,
                agent_call_attempt_id=attempt.id,
                fencing_token=lease.fencing_token,
                transport_ordinal=transport_ordinal,
                status="reserved",
            )
            self.session.add(transport)
            await self.session.flush()
        ATTEMPT_OUTCOMES.labels(kind, "started").inc()
        return ReservedAttempt(
            attempt.id,
            kind,
            attempt.ordinal,
            logical_invocation_id,
            transport_ordinal,
            transport.id if transport is not None else None,
        )

    async def reserve_tool_round(self, lease: JobLease) -> int:
        await RuntimeJobRepository(self.session).assert_fence(lease)
        run = await self.session.get(AgentRun, lease.run_id, with_for_update=True)
        if run is None:
            raise RuntimeConflict("run_not_found")
        if run.tool_rounds >= MAX_TOOL_ROUNDS:
            raise RuntimeConflict("tool_round_budget_exhausted")
        run.tool_rounds += 1
        await self.session.flush()
        return run.tool_rounds

    async def finish(
        self,
        lease: JobLease,
        reserved: ReservedAttempt,
        *,
        status: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: int | None = None,
        error_code: str | None = None,
        provider_transport_attempts: int | None = None,
        transport_lifecycle: dict[str, object] | None = None,
        structured_error_paths: Sequence[str] | None = None,
    ) -> None:
        job = await RuntimeJobRepository(self.session).assert_fence(lease)
        attempt = await self.session.get(AgentCallAttempt, reserved.id, with_for_update=True)
        if attempt is None or attempt.status != "started":
            raise RuntimeConflict("attempt_not_started")
        if status not in {"succeeded", "failed", "unknown"}:
            raise ValueError("invalid attempt outcome")
        paths: list[str] | None = None
        if structured_error_paths is not None:
            paths = list(structured_error_paths)
            if status != "failed" or reserved.kind not in {"llm", "structure_repair"}:
                raise ValueError("structured error paths require a failed Provider attempt")
            if not paths or len(paths) > 12:
                raise ValueError("structured error paths must contain between 1 and 12 items")
            if any(
                not isinstance(path, str)
                or len(path) > 200
                or _SAFE_STRUCTURED_ERROR_PATH.fullmatch(path) is None
                for path in paths
            ):
                raise ValueError("invalid structured error path")
        attempt.status = status
        attempt.prompt_tokens = max(0, prompt_tokens)
        attempt.completion_tokens = max(0, completion_tokens)
        attempt.latency_ms = latency_ms
        attempt.error_code = error_code
        if paths is not None:
            attempt.runtime_provenance = {
                **attempt.runtime_provenance,
                "structured_error_paths": paths,
            }
        if provider_transport_attempts is not None:
            if reserved.kind not in {"llm", "structure_repair"}:
                raise ValueError("provider transport attempts require an LLM attempt")
            if provider_transport_attempts not in {1, 2}:
                raise ValueError("provider transport attempts exceed the bounded retry contract")
            attempt.runtime_provenance = {
                **attempt.runtime_provenance,
                "provider_transport_attempts": provider_transport_attempts,
                "provider_retry_count": provider_transport_attempts - 1,
            }
        if reserved.transport_attempt_id is not None:
            transport = await self.session.get(
                ToolTransportAttempt, reserved.transport_attempt_id, with_for_update=True
            )
            if transport is None or transport.status not in {"reserved", "executing"}:
                raise RuntimeConflict("transport_attempt_not_active")
            transport.status = status
            transport.error_code = error_code
            transport.completed_at = datetime.now(UTC)
            if transport_lifecycle is not None:
                if transport_lifecycle.get("schema_version") != "mcp-transport-lifecycle.v1":
                    raise ValueError("invalid MCP transport lifecycle schema")
                forbidden = {"prompt", "payload", "arguments", "secret", "api_key"}
                if forbidden.intersection(transport_lifecycle):
                    raise ValueError("MCP transport lifecycle contains forbidden fields")
                expected_identity: dict[str, object] = {
                    "tenant_id": lease.tenant_id,
                    "run_id": lease.run_id,
                    "job_id": lease.job_id,
                    "fencing_token": lease.fencing_token,
                    "tool_attempt_id": reserved.id,
                    "transport_attempt_id": reserved.transport_attempt_id,
                    "transport_ordinal": reserved.transport_ordinal,
                }
                if any(
                    key in transport_lifecycle and transport_lifecycle[key] != expected
                    for key, expected in expected_identity.items()
                ):
                    raise RuntimeConflict("mcp_transport_lifecycle_identity_mismatch")
                attempt.runtime_provenance = {
                    **attempt.runtime_provenance,
                    "mcp_transport_lifecycle": {
                        **transport_lifecycle,
                        "worker_snapshot": {
                            "lease_owner": job.lease_owner,
                            "lease_expires_at": (
                                job.lease_expires_at.isoformat()
                                if job.lease_expires_at is not None
                                else None
                            ),
                            "heartbeat_at": (
                                job.heartbeat_at.isoformat()
                                if job.heartbeat_at is not None
                                else None
                            ),
                            "fencing_token": job.fencing_token,
                            "job_status": job.status,
                        },
                    },
                }
        await self.session.flush()
        ATTEMPT_OUTCOMES.labels(reserved.kind, status).inc()
        if latency_ms is not None:
            ATTEMPT_LATENCY.labels(reserved.kind).observe(max(0, latency_ms) / 1000)
