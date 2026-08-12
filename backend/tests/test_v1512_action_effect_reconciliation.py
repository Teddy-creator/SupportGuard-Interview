from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from sqlalchemy import exc

from supportguard.services.action_effect_reconciliation import (
    ActionEffectReconciliationRunner,
)
from supportguard.services.runtime_queue import RuntimeReconciler


class _CandidateRows:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> _CandidateRows:
        return self

    def all(self) -> list[dict[str, object]]:
        return self._rows


class _FakeSession:
    def __init__(
        self,
        *,
        candidates: list[dict[str, object]],
        results: Mapping[str, list[object]],
        calls: list[dict[str, object]],
    ) -> None:
        self._candidates = candidates
        self._results = results
        self._calls = calls

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def begin(self) -> _FakeSession:
        return self

    async def execute(self, statement: object, parameters: object) -> _CandidateRows:
        self._calls.append({"kind": "candidates", "sql": str(statement), "parameters": parameters})
        return _CandidateRows(self._candidates)

    async def scalar(self, statement: object, parameters: object) -> object:
        assert isinstance(parameters, dict)
        call = {"kind": "prepare", "sql": str(statement), "parameters": dict(parameters)}
        self._calls.append(call)
        job_id = str(parameters["job_id"])
        queued = self._results[job_id]
        if not queued:
            raise AssertionError(f"unexpected capability call for {job_id}")
        result = queued.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _factory(
    *,
    candidates: list[dict[str, object]],
    results: Mapping[str, list[object]],
    calls: list[dict[str, object]],
) -> Any:
    def create() -> _FakeSession:
        return _FakeSession(candidates=candidates, results=results, calls=calls)

    return create


@pytest.mark.asyncio
async def test_action_effect_runner_reports_authoritative_dispositions_without_redis() -> None:
    candidates = [
        {"job_id": "job_queued", "job_status": "queued", "status_version": 1},
        {"job_id": "job_executed", "job_status": "succeeded", "status_version": 2},
        {"job_id": "job_zero", "job_status": "succeeded", "status_version": 3},
        {"job_id": "job_pending", "job_status": "succeeded", "status_version": 4},
        {"job_id": "job_stale", "job_status": "succeeded", "status_version": 5},
        {"job_id": "job_other", "job_status": "succeeded", "status_version": 6},
    ]
    results: dict[str, list[object]] = {
        "job_executed": [
            {
                "result": "terminal_reconciled",
                "resolution": "executed",
                "job_id": "job_executed",
            }
        ],
        "job_zero": [
            {
                "result": "terminal_reconciled",
                "resolution": "confirmed_zero_effect",
                "job_id": "job_zero",
            }
        ],
        "job_pending": [{"result": "verification_pending", "job_id": "job_pending"}],
        "job_stale": [{"result": "stale"}],
        "job_other": [{"result": "not_action_effect"}],
    }
    calls: list[dict[str, object]] = []
    runner = ActionEffectReconciliationRunner(
        _factory(candidates=candidates, results=results, calls=calls)
    )

    report = await runner.reconcile_once(batch_size=6)

    assert report.candidate_count == 6
    assert report.attempted == 5
    assert report.resolved == 2
    assert report.resolved_executed == 1
    assert report.resolved_zero_effect == 1
    assert report.pending == 1
    assert report.stale == 1
    assert report.not_applicable == 2
    assert report.transient_retries == 0
    assert report.handled_job_ids == (
        "job_executed",
        "job_zero",
        "job_pending",
        "job_stale",
    )
    prepare_calls = [call for call in calls if call["kind"] == "prepare"]
    assert len(prepare_calls) == 5
    assert all(
        call["parameters"]["reason"] == "action_effect_reconciliation"
        for call in prepare_calls
    )
    assert all("reconcile_intents" not in str(call["sql"]).lower() for call in calls)
    assert all("redis" not in str(call["sql"]).lower() for call in calls)


@pytest.mark.asyncio
async def test_action_effect_runner_retries_only_transient_transactions_with_a_hard_bound() -> None:
    class RetryFault(Exception):
        def __init__(self, sqlstate: str) -> None:
            self.sqlstate = sqlstate
            super().__init__(sqlstate)

    candidates = [{"job_id": "job_pending", "job_status": "succeeded", "status_version": 8}]
    results: dict[str, list[object]] = {
        "job_pending": [
            exc.DBAPIError("prepare", {}, RetryFault("40001"), False),
            exc.DBAPIError("prepare", {}, RetryFault("40P01"), False),
            {"result": "verification_pending", "job_id": "job_pending"},
        ]
    }
    calls: list[dict[str, object]] = []
    runner = ActionEffectReconciliationRunner(
        _factory(candidates=candidates, results=results, calls=calls)
    )

    report = await runner.reconcile_candidates(candidates)

    assert report.pending == 1
    assert report.transient_retries == 2
    assert len([call for call in calls if call["kind"] == "prepare"]) == 3


@pytest.mark.asyncio
async def test_action_effect_runner_fails_closed_on_unproven_terminal_resolution() -> None:
    candidates = [{"job_id": "job_unknown", "job_status": "succeeded", "status_version": 1}]
    results: dict[str, list[object]] = {
        "job_unknown": [
            {
                "result": "terminal_reconciled",
                "resolution": "resource_changed",
                "job_id": "job_unknown",
            }
        ]
    }
    runner = ActionEffectReconciliationRunner(
        _factory(candidates=candidates, results=results, calls=[])
    )

    with pytest.raises(RuntimeError, match="resolution invalid"):
        await runner.reconcile_candidates(candidates)


@pytest.mark.asyncio
async def test_action_effect_runner_fails_closed_on_capability_identity_mismatch() -> None:
    candidates = [{"job_id": "job_expected", "job_status": "succeeded", "status_version": 1}]
    results: dict[str, list[object]] = {
        "job_expected": [
            {
                "result": "terminal_reconciled",
                "resolution": "executed",
                "job_id": "job_other",
            }
        ]
    }
    runner = ActionEffectReconciliationRunner(
        _factory(candidates=candidates, results=results, calls=[])
    )

    with pytest.raises(RuntimeError, match="mismatched job"):
        await runner.reconcile_candidates(candidates)


@pytest.mark.asyncio
async def test_runtime_reconciler_keeps_verification_pending_out_of_redis_recovery() -> None:
    candidates = [{"job_id": "job_pending", "job_status": "succeeded", "status_version": 4}]
    results: dict[str, list[object]] = {
        "job_pending": [{"result": "verification_pending", "job_id": "job_pending"}]
    }
    calls: list[dict[str, object]] = []
    reconciler = RuntimeReconciler(
        _factory(candidates=candidates, results=results, calls=calls),
        None,
    )

    async def forbidden_observation(prepared: dict[str, Any]) -> dict[str, Any]:
        del prepared
        raise AssertionError("verification_pending must not observe Redis")

    reconciler._observe_reconcile_intent = forbidden_observation  # type: ignore[method-assign]

    assert await reconciler._reconcile_postgresql(redelivery_grace_seconds=0) == 0
    assert [call["kind"] for call in calls] == ["candidates", "prepare"]
    assert calls[-1]["parameters"]["reason"] == "action_effect_reconciliation"


@pytest.mark.asyncio
async def test_action_effect_runner_rejects_unbounded_work() -> None:
    runner = ActionEffectReconciliationRunner(  # type: ignore[arg-type]
        lambda: None,
    )
    with pytest.raises(ValueError, match="batch outside bound"):
        await runner.reconcile_once(batch_size=501)
