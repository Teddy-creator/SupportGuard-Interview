from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.contracts.finalizer import FINALIZER_TERMINAL_STATES
from supportguard.db.models import SupportTicket
from supportguard.services.approval_lifecycle import (
    activate_next_turn_and_converge_ticket,
)
from supportguard.services.commands import activate_next_turn
from supportguard.services.runtime_jobs import RuntimeConflict


def _restartable_pre_effect_head_paths(paths: tuple[str, ...]) -> bool:
    """Allow a fresh read-only run only for aggregate resource-version drift.

    Tool, capability, proposal, budget, context, marker, checkpoint and state
    hashes remain strict terminal conflicts.  Recomputing from a new fence is
    safe only when the discarded Segment has no effect and the changed fields
    are the version heads of the Ticket, Run or RuntimeJob it already owns.
    """

    prefix = "expected_heads.expected_domain_resource_versions."
    allowed_kinds = {"ticket", "run", "job"}
    return bool(paths) and all(
        path.startswith(prefix) and path.removeprefix(prefix).split(":", 1)[0] in allowed_kinds
        for path in paths
    )


def _validated_finalizer_terminal(
    *,
    segment_kind: str,
    outcome: str,
    state: dict[str, Any],
) -> str | None:
    """Validate a completed Segment's public terminal without inventing one."""

    if outcome != "completed":
        return None
    final = state.get("final")
    if not isinstance(final, dict):
        raise RuntimeConflict("segment_final_state_missing")
    terminal = final.get("terminal_state")
    if not isinstance(terminal, str) or terminal not in FINALIZER_TERMINAL_STATES:
        raise RuntimeConflict("segment_terminal_state_invalid")
    human_action = str(state.get("human_decision", {}).get("action", ""))
    if terminal == "manual_takeover" and not (
        segment_kind == "approval_resume" and human_action == "manual_takeover"
    ):
        raise RuntimeConflict("segment_manual_takeover_forbidden")
    return terminal


async def _owner_finish_activates_next_turn(session: AsyncSession) -> bool:
    """Restricted PostgreSQL workers delegate Ticket activation to the owner capability."""

    return bool(
        session.get_bind().dialect.name == "postgresql"
        and await session.scalar(text("SELECT session_user")) == "supportguard_worker"
    )


async def _activate_and_converge_application_fallback(
    session: AsyncSession,
    *,
    ticket: SupportTicket,
    trace_id: str,
    default_status: str,
) -> None:
    """Finish one Ticket lane without duplicating the PostgreSQL owner kernel."""

    if await _owner_finish_activates_next_turn(session):
        return
    if session.get_bind().dialect.name == "postgresql":
        # Preserve the existing privileged/admin test path. Production workers
        # use the owner capability above; only SQLite needs application-side
        # aggregate convergence.
        await activate_next_turn(session, ticket=ticket, trace_id=trace_id)
        return
    await activate_next_turn_and_converge_ticket(
        session,
        ticket=ticket,
        trace_id=trace_id,
        default_status=default_status,
    )


def stable_hash(value: dict[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _final_message_source_refs(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Project only source identities actually published by the FinalResponse."""

    final = state.get("final")
    if not isinstance(final, dict):
        return []
    allowed_ids = {
        str(value)
        for value in (
            list(final.get("business_source_ids", [])) + list(final.get("knowledge_chunk_ids", []))
        )
        if value
    }
    projected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for observation in state.get("tool_observations", []):
        if not isinstance(observation, dict):
            continue
        for source in observation.get("source_refs", []):
            if not isinstance(source, dict):
                continue
            source_id = str(source.get("source_id") or "")
            if not source_id or source_id not in allowed_ids or source_id in seen:
                continue
            projected.append(dict(source))
            seen.add(source_id)
    return projected[:3]
