from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.contracts.canonical_json import canonical_json_hash
from supportguard.contracts.tools import ObservationEnvelope
from supportguard.db.models import (
    AgentCallAttempt,
    ToolInvocation,
    ToolObservation,
    ToolTransportAttempt,
    TurnGroup,
    new_id,
)
from supportguard.services.runtime_jobs import JobLease, RuntimeConflict, RuntimeJobRepository
from supportguard.tools.capabilities import registry_hash


def canonical_hash(value: object) -> str:
    return canonical_json_hash(value)


@dataclass(frozen=True, slots=True)
class InvocationSpec:
    provider_tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    ordinal: int
    arguments_hash: str | None = None


class ToolLedger:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def open_turn(
        self,
        lease: JobLease,
        *,
        segment_id: str,
        tool_round: int,
        decision: dict[str, Any],
        context_manifest: dict[str, Any],
        calls: list[InvocationSpec],
    ) -> tuple[TurnGroup, list[ToolInvocation]]:
        await RuntimeJobRepository(self.session).assert_fence(lease)
        if not calls:
            raise RuntimeConflict("empty_tool_turn")
        current = await self.session.scalar(
            select(func.max(TurnGroup.decision_ordinal)).where(TurnGroup.run_id == lease.run_id)
        )
        visible_schema_hash = str(
            context_manifest.get("injected_tool_schema_hash")
            or context_manifest.get("visible_tool_schema_hash")
            or registry_hash()
        )
        if len(visible_schema_hash) != 64:
            raise RuntimeConflict("visible_tool_schema_hash_invalid")
        turn = TurnGroup(
            tenant_id=lease.tenant_id,
            run_id=lease.run_id,
            job_id=lease.job_id,
            segment_id=segment_id,
            fencing_token=lease.fencing_token,
            decision_ordinal=int(current or 0) + 1,
            tool_round=tool_round,
            expected_invocations=len(calls),
            decision_hash=canonical_hash(decision),
            context_hash=canonical_hash(context_manifest),
            tool_schema_hash=visible_schema_hash,
            status="open",
        )
        self.session.add(turn)
        await self.session.flush()
        invocations = [
            ToolInvocation(
                tenant_id=lease.tenant_id,
                run_id=lease.run_id,
                job_id=lease.job_id,
                turn_group_id=turn.id,
                segment_id=segment_id,
                fencing_token=lease.fencing_token,
                provider_tool_call_id=call.provider_tool_call_id,
                logical_invocation_id=new_id("logical"),
                ordinal=call.ordinal,
                tool_name=call.tool_name,
                arguments_hash=call.arguments_hash or canonical_hash(call.arguments),
                requested_cost=1,
            )
            for call in calls
        ]
        self.session.add_all(invocations)
        await self.session.flush()
        return turn, invocations

    async def mark_executing(self, lease: JobLease, invocation_id: str) -> ToolInvocation:
        await RuntimeJobRepository(self.session).assert_fence(lease)
        invocation = await self.session.get(ToolInvocation, invocation_id, with_for_update=True)
        if (
            invocation is None
            or invocation.fencing_token != lease.fencing_token
            or invocation.lifecycle not in {"received", "validated", "authorized"}
        ):
            raise RuntimeConflict("invocation_not_executable")
        invocation.lifecycle = "executing"
        await self.session.flush()
        return invocation

    async def replay_terminal_observation(
        self,
        lease: JobLease,
        invocation_id: str,
        *,
        turn_id: str,
        provider_tool_call_id: str,
        tool_name: str,
        arguments_hash: str,
    ) -> tuple[ToolObservation, ObservationEnvelope] | None:
        """Return a durable terminal result without re-executing its MCP call.

        A worker can fail after the Observation transaction commits but before
        LangGraph checkpoints the completed tool node.  The replacement worker
        must rebuild that node output from the ledger while executing only the
        unresolved ordinals.  Terminal invocations intentionally retain their
        original fencing token; the current TurnGroup fence proves takeover
        ownership while the immutable call identity proves replay safety.
        """

        await RuntimeJobRepository(self.session).assert_fence(lease)
        turn = await self.session.get(TurnGroup, turn_id)
        invocation = await self.session.get(ToolInvocation, invocation_id)
        if (
            turn is None
            or invocation is None
            or turn.id != invocation.turn_group_id
            or turn.tenant_id != lease.tenant_id
            or turn.run_id != lease.run_id
            or turn.job_id != lease.job_id
            or turn.fencing_token != lease.fencing_token
            or invocation.tenant_id != lease.tenant_id
            or invocation.run_id != lease.run_id
            or invocation.job_id != lease.job_id
            or invocation.provider_tool_call_id != provider_tool_call_id
            or invocation.tool_name != tool_name
            or invocation.arguments_hash != arguments_hash
        ):
            raise RuntimeConflict("terminal_observation_replay_scope_mismatch")
        if invocation.lifecycle != "terminal":
            if invocation.fencing_token != lease.fencing_token:
                raise RuntimeConflict("pending_invocation_fence_mismatch")
            return None
        row = await self.session.scalar(
            select(ToolObservation).where(
                ToolObservation.invocation_id == invocation.id,
                ToolObservation.tenant_id == lease.tenant_id,
                ToolObservation.run_id == lease.run_id,
                ToolObservation.job_id == lease.job_id,
            )
        )
        if row is None:
            raise RuntimeConflict("terminal_observation_missing")
        try:
            observation_payload = dict(row.payload)
            observation_payload.pop("trusted_retrieval_intent", None)
            observation = ObservationEnvelope.model_validate(observation_payload)
        except Exception as exc:
            raise RuntimeConflict("terminal_observation_payload_invalid") from exc
        if (
            canonical_hash(row.payload) != row.content_hash
            or observation.run_id != lease.run_id
            or observation.tool_call_id != provider_tool_call_id
            or observation.tool_name != tool_name
        ):
            raise RuntimeConflict("terminal_observation_replay_integrity_mismatch")
        return row, observation

    async def next_transport_ordinal(
        self,
        lease: JobLease,
        invocation_id: str,
    ) -> int | None:
        """Return the next bounded physical-send ordinal for one invocation."""

        await RuntimeJobRepository(self.session).assert_fence(lease)
        invocation = await self.session.get(ToolInvocation, invocation_id)
        if (
            invocation is None
            or invocation.tenant_id != lease.tenant_id
            or invocation.run_id != lease.run_id
            or invocation.job_id != lease.job_id
            or invocation.fencing_token != lease.fencing_token
            or invocation.lifecycle == "terminal"
        ):
            raise RuntimeConflict("transport_ordinal_scope_mismatch")
        current = int(
            await self.session.scalar(
                select(func.max(ToolTransportAttempt.transport_ordinal)).where(
                    ToolTransportAttempt.invocation_id == invocation.id
                )
            )
            or 0
        )
        return current + 1 if current < 2 else None

    async def pending_invocation_count(
        self,
        lease: JobLease,
        turn_id: str,
    ) -> int:
        """Count unresolved ordinals under the current Turn takeover fence."""

        await RuntimeJobRepository(self.session).assert_fence(lease)
        turn = await self.session.get(TurnGroup, turn_id)
        if (
            turn is None
            or turn.tenant_id != lease.tenant_id
            or turn.run_id != lease.run_id
            or turn.job_id != lease.job_id
            or turn.fencing_token != lease.fencing_token
        ):
            raise RuntimeConflict("turn_group_pending_scope_mismatch")
        return int(
            await self.session.scalar(
                select(func.count(ToolInvocation.id)).where(
                    ToolInvocation.turn_group_id == turn.id,
                    ToolInvocation.lifecycle != "terminal",
                )
            )
            or 0
        )

    async def terminalize(
        self,
        lease: JobLease,
        invocation_id: str,
        *,
        outcome: str,
        observation: ObservationEnvelope,
        trusted_retrieval_intent: dict[str, Any] | None = None,
    ) -> ToolObservation:
        await RuntimeJobRepository(self.session).assert_fence(lease)
        invocation = await self.session.get(ToolInvocation, invocation_id, with_for_update=True)
        if invocation is None or invocation.fencing_token != lease.fencing_token:
            raise RuntimeConflict("invocation_fence_mismatch")
        payload = observation.model_dump(mode="json")
        if trusted_retrieval_intent is not None:
            if observation.tool_name != "search_knowledge":
                raise RuntimeConflict("trusted_retrieval_intent_tool_mismatch")
            payload["trusted_retrieval_intent"] = trusted_retrieval_intent
        digest = canonical_hash(payload)
        existing = await self.session.scalar(
            select(ToolObservation).where(ToolObservation.invocation_id == invocation.id)
        )
        if existing is not None:
            if existing.content_hash != digest or invocation.outcome != outcome:
                raise RuntimeConflict("terminal_observation_conflict")
            return existing
        if invocation.lifecycle == "terminal":
            raise RuntimeConflict("terminal_observation_missing")
        row = ToolObservation(
            tenant_id=lease.tenant_id,
            run_id=lease.run_id,
            job_id=lease.job_id,
            invocation_id=invocation.id,
            segment_id=invocation.segment_id,
            fencing_token=lease.fencing_token,
            status=observation.status,
            attempt_index=observation.attempt_index,
            content_hash=digest,
            payload=payload,
        )
        self.session.add(row)
        invocation.lifecycle = "terminal"
        invocation.outcome = outcome
        invocation.terminal_at = datetime.now(UTC)
        await self.session.flush()
        return row

    async def close_turn(self, lease: JobLease, turn_id: str) -> TurnGroup:
        await RuntimeJobRepository(self.session).assert_fence(lease)
        turn = await self.session.get(TurnGroup, turn_id, with_for_update=True)
        if turn is None or turn.fencing_token != lease.fencing_token:
            raise RuntimeConflict("turn_group_fence_mismatch")
        counts = (
            await self.session.execute(
                select(
                    func.count(ToolInvocation.id),
                    func.count(ToolObservation.id),
                )
                .select_from(ToolInvocation)
                .outerjoin(ToolObservation, ToolObservation.invocation_id == ToolInvocation.id)
                .where(ToolInvocation.turn_group_id == turn.id)
            )
        ).one()
        if tuple(counts) != (turn.expected_invocations, turn.expected_invocations):
            raise RuntimeConflict("turn_group_incomplete")
        turn.status = "closed"
        turn.closed_at = datetime.now(UTC)
        await self.session.flush()
        return turn

    async def takeover(self, lease: JobLease, turn_id: str) -> tuple[TurnGroup, list[str]]:
        """Fence-take over an existing decision without another Provider call."""

        await RuntimeJobRepository(self.session).assert_fence(lease)
        turn = await self.session.get(TurnGroup, turn_id, with_for_update=True)
        if turn is None or turn.run_id != lease.run_id or turn.job_id != lease.job_id:
            raise RuntimeConflict("turn_group_takeover_scope_mismatch")
        if turn.status not in {"open", "completing"}:
            raise RuntimeConflict("turn_group_not_takeoverable")
        if turn.fencing_token >= lease.fencing_token:
            raise RuntimeConflict("turn_group_takeover_fence_not_newer")
        invocations = list(
            (
                await self.session.scalars(
                    select(ToolInvocation)
                    .where(ToolInvocation.turn_group_id == turn.id)
                    .order_by(ToolInvocation.ordinal)
                    .with_for_update()
                )
            ).all()
        )
        pending_ids = [row.id for row in invocations if row.lifecycle != "terminal"]
        if pending_ids:
            attempts = list(
                (
                    await self.session.scalars(
                        select(AgentCallAttempt)
                        .where(
                            AgentCallAttempt.logical_invocation_id.in_(pending_ids),
                            AgentCallAttempt.status == "started",
                        )
                        .with_for_update()
                    )
                ).all()
            )
            now = datetime.now(UTC)
            for attempt in attempts:
                attempt.status = "unknown"
                attempt.error_code = "fence_takeover"
                attempt.runtime_provenance = {
                    **attempt.runtime_provenance,
                    "fence_takeover": {
                        "schema_version": "mcp-fence-takeover.v1",
                        "observed_at": now.isoformat(),
                        "previous_fencing_token": attempt.fencing_token,
                        "replacement_fencing_token": lease.fencing_token,
                        "replacement_lease_owner": lease.owner,
                        "replacement_lease_expires_at": lease.expires_at.isoformat(),
                    },
                }
                transport = await self.session.scalar(
                    select(ToolTransportAttempt)
                    .where(ToolTransportAttempt.agent_call_attempt_id == attempt.id)
                    .with_for_update()
                )
                if transport is not None and transport.status == "reserved":
                    transport.status = "unknown"
                    transport.error_code = "fence_takeover"
                    transport.completed_at = now
        for invocation in invocations:
            if invocation.lifecycle != "terminal":
                invocation.fencing_token = lease.fencing_token
                invocation.job_id = lease.job_id
                if invocation.lifecycle == "executing":
                    invocation.lifecycle = "authorized"
        turn.fencing_token = lease.fencing_token
        turn.job_id = lease.job_id
        await self.session.flush()
        return turn, pending_ids

    async def abort_pending(
        self,
        lease: JobLease,
        turn_id: str,
        *,
        ticket_id: str,
        reason: str,
    ) -> TurnGroup:
        """Terminalize every unresolved ordinal and close the Turn as aborted."""

        await RuntimeJobRepository(self.session).assert_fence(lease)
        turn = await self.session.get(TurnGroup, turn_id, with_for_update=True)
        if turn is None or turn.fencing_token != lease.fencing_token:
            raise RuntimeConflict("turn_group_abort_fence_mismatch")
        invocations = list(
            (
                await self.session.scalars(
                    select(ToolInvocation)
                    .where(ToolInvocation.turn_group_id == turn.id)
                    .order_by(ToolInvocation.ordinal)
                    .with_for_update()
                )
            ).all()
        )
        for invocation in invocations:
            if invocation.lifecycle == "terminal":
                continue
            attempts = int(
                await self.session.scalar(
                    select(func.count(AgentCallAttempt.id)).where(
                        AgentCallAttempt.logical_invocation_id == invocation.id
                    )
                )
                or 0
            )
            observation = ObservationEnvelope(
                tool_name=invocation.tool_name,
                tool_call_id=invocation.provider_tool_call_id,
                ticket_id=ticket_id,
                run_id=lease.run_id,
                attempt_index=max(1, attempts),
                status="unavailable",
                retryable=False,
                error_code=reason,
                safe_error_summary="The interrupted capability was safely aborted.",
                observed_at=datetime.now(UTC),
                duration_ms=0,
            )
            await self.terminalize(
                lease,
                invocation.id,
                outcome="cancelled",
                observation=observation,
            )
        turn.status = "aborted"
        turn.closed_at = datetime.now(UTC)
        await self.session.flush()
        return turn
