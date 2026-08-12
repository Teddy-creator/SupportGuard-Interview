from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import exc, text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

MAX_ACTION_EFFECT_BATCH = 500
MAX_ACTION_EFFECT_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class ActionEffectReconciliationReport:
    """Bounded, customer-safe observations from one reconciliation pass."""

    candidate_count: int
    attempted: int
    resolved_executed: int
    resolved_zero_effect: int
    pending: int
    stale: int
    not_applicable: int
    transient_retries: int
    handled_job_ids: tuple[str, ...]

    @property
    def resolved(self) -> int:
        return self.resolved_executed + self.resolved_zero_effect


class ActionEffectReconciliationRunner:
    """Converge durable unknown Action effects through the restricted PG capability.

    A ``RuntimeJob`` with ``outcome='verification_pending'`` is the durable intent.
    The runner never executes an Action and never consults Redis delivery state. The
    database capability is solely responsible for locking and authoritatively
    comparing the bound Approval, BusinessAction, and business resource.
    """

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        max_attempts: int = MAX_ACTION_EFFECT_ATTEMPTS,
    ) -> None:
        if max_attempts < 1 or max_attempts > MAX_ACTION_EFFECT_ATTEMPTS:
            raise ValueError("action effect reconciliation attempts outside bound")
        self.factory = factory
        self.max_attempts = max_attempts

    async def reconcile_once(
        self,
        *,
        batch_size: int = 100,
    ) -> ActionEffectReconciliationReport:
        if batch_size < 1 or batch_size > MAX_ACTION_EFFECT_BATCH:
            raise ValueError("action effect reconciliation batch outside bound")
        async with self.factory() as session, session.begin():
            candidates = (
                (
                    await session.execute(
                        text("SELECT * FROM supportguard_reconciler_candidates(:batch_size)"),
                        {"batch_size": batch_size},
                    )
                )
                .mappings()
                .all()
            )
        return await self.reconcile_candidates(candidates)

    async def reconcile_candidates(
        self,
        candidates: Sequence[Mapping[str, Any] | RowMapping],
    ) -> ActionEffectReconciliationReport:
        """Process a previously bounded candidate snapshot.

        ``RuntimeReconciler`` uses this entry point so delivery recovery and
        Action-effect reconciliation share one candidate read. A handled Action
        job is returned explicitly and must not enter the Redis observation path.
        """

        if len(candidates) > MAX_ACTION_EFFECT_BATCH:
            raise ValueError("action effect reconciliation candidates outside bound")
        attempted = 0
        resolved_executed = 0
        resolved_zero_effect = 0
        pending = 0
        stale = 0
        not_applicable = 0
        transient_retries = 0
        handled_job_ids: list[str] = []

        for candidate in candidates:
            # Unknown effects are terminal succeeded jobs. Queued/retry/leased/dead
            # jobs remain exclusively owned by delivery/terminal reconciliation.
            if candidate.get("job_status") != "succeeded":
                not_applicable += 1
                continue
            job_id = candidate.get("job_id")
            status_version = candidate.get("status_version")
            if not isinstance(job_id, str) or not isinstance(status_version, int):
                raise RuntimeError("action effect reconciliation candidate identity invalid")
            attempted += 1
            result, retry_count = await self._reconcile_candidate(
                job_id=job_id,
                status_version=status_version,
            )
            transient_retries += retry_count
            disposition = result.get("result")
            if disposition == "not_action_effect":
                not_applicable += 1
                continue
            if disposition in {"verification_pending", "terminal_reconciled"} and (
                result.get("job_id") != job_id
            ):
                raise RuntimeError(
                    "action effect reconciliation capability returned a mismatched job"
                )
            handled_job_ids.append(job_id)
            if disposition == "verification_pending":
                pending += 1
                continue
            if disposition == "stale":
                stale += 1
                continue
            if disposition != "terminal_reconciled":
                raise RuntimeError(
                    f"action effect reconciliation disposition invalid: {disposition!r}"
                )
            resolution = result.get("resolution")
            if resolution == "executed":
                resolved_executed += 1
            elif resolution == "confirmed_zero_effect":
                resolved_zero_effect += 1
            else:
                raise RuntimeError(
                    f"action effect reconciliation resolution invalid: {resolution!r}"
                )

        return ActionEffectReconciliationReport(
            candidate_count=len(candidates),
            attempted=attempted,
            resolved_executed=resolved_executed,
            resolved_zero_effect=resolved_zero_effect,
            pending=pending,
            stale=stale,
            not_applicable=not_applicable,
            transient_retries=transient_retries,
            handled_job_ids=tuple(handled_job_ids),
        )

    async def _reconcile_candidate(
        self,
        *,
        job_id: str,
        status_version: int,
    ) -> tuple[dict[str, Any], int]:
        retries = 0
        for attempt in range(self.max_attempts):
            try:
                async with self.factory() as session, session.begin():
                    result = await session.scalar(
                        text(
                            "SELECT supportguard_reconciler_prepare("
                            ":job_id,:expected_job_version,:reason)"
                        ),
                        {
                            "job_id": job_id,
                            "expected_job_version": status_version,
                            "reason": "action_effect_reconciliation",
                        },
                    )
                if not isinstance(result, dict):
                    raise RuntimeError(
                        "action effect reconciliation capability returned an invalid result"
                    )
                return result, retries
            except exc.DBAPIError as error:
                sqlstate = getattr(error.orig, "sqlstate", None)
                if sqlstate not in {"40001", "40P01"} or attempt + 1 >= self.max_attempts:
                    raise
                retries += 1
        raise RuntimeError("action effect reconciliation retry bound invariant")
