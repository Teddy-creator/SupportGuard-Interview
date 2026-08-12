from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError

from supportguard.db.models import AgentRun
from supportguard.runtime import AppRuntime
from supportguard.runtime.worker import AgentJobHandler
from supportguard.services.runtime_jobs import FinalizerCommitUnknown, JobLease


class _FakeTransaction:
    def __init__(self, *, commit_error: BaseException | None = None) -> None:
        self.commit_error = commit_error
        self.is_active = False
        self.rolled_back = False

    async def start(self) -> _FakeTransaction:
        self.is_active = True
        return self

    async def commit(self) -> None:
        if self.commit_error is not None:
            raise self.commit_error
        self.is_active = False

    async def rollback(self) -> None:
        self.is_active = False
        self.rolled_back = True


class _FakeSession:
    def __init__(self, transaction: _FakeTransaction) -> None:
        self.transaction = transaction
        self.run = SimpleNamespace(
            id="run_commit_unknown",
            tenant_id="tenant_demo",
            ticket_id="ticket_commit_unknown",
            customer_id="cust_demo",
        )

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def begin(self) -> _FakeTransaction:
        return self.transaction

    async def get(self, model: object, identity: str) -> object | None:
        assert model is AgentRun
        return self.run if identity == self.run.id else None


class _FakeFactory:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session

    def __call__(self) -> _FakeSession:
        return self.session


def _lease(*, action_effect: bool) -> JobLease:
    return JobLease(
        job_id="job_commit_unknown",
        run_id="run_commit_unknown",
        tenant_id="tenant_demo",
        owner="worker-commit-unknown",
        fencing_token=3,
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
        kind="approval_resume" if action_effect else "agent_start",
        approval_id="approval_commit_unknown" if action_effect else None,
        ticket_id="ticket_commit_unknown",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action_effect", "expected_mode"),
    ((True, "action_effect"), (False, "finalizer_only")),
)
async def test_transport_loss_during_finalizer_commit_has_typed_recovery_mode(
    monkeypatch: pytest.MonkeyPatch,
    *,
    action_effect: bool,
    expected_mode: str,
) -> None:
    commit_error = DBAPIError(
        "COMMIT",
        {},
        ConnectionError("connection lost after commit write"),
        connection_invalidated=True,
    )
    transaction = _FakeTransaction(commit_error=commit_error)
    handler = AgentJobHandler(
        cast(Any, _FakeFactory(_FakeSession(transaction))),
        cast(AppRuntime, object()),
    )

    async def no_scope(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr("supportguard.runtime.finalizer.set_local_scope", no_scope)

    async def finalizer_body(session: object) -> str:
        del session
        return "completed"

    with pytest.raises(FinalizerCommitUnknown) as raised:
        await handler._commit_finalizer_transaction(
            _lease(action_effect=action_effect),
            finalizer_body,  # type: ignore[arg-type]
            recovery_mode=expected_mode,  # type: ignore[arg-type]
        )
    assert raised.value.recovery_mode == expected_mode
    assert transaction.rolled_back is False


@pytest.mark.asyncio
async def test_deferred_constraint_commit_error_is_not_commit_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit_error = IntegrityError(
        "COMMIT",
        {},
        RuntimeError("deferred foreign key violation"),
        connection_invalidated=False,
    )
    transaction = _FakeTransaction(commit_error=commit_error)
    handler = AgentJobHandler(
        cast(Any, _FakeFactory(_FakeSession(transaction))),
        cast(AppRuntime, object()),
    )

    async def no_scope(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr("supportguard.runtime.finalizer.set_local_scope", no_scope)

    async def finalizer_body(session: object) -> None:
        del session

    with pytest.raises(IntegrityError):
        await handler._commit_finalizer_transaction(
            _lease(action_effect=True),
            finalizer_body,  # type: ignore[arg-type]
            recovery_mode="action_effect",
        )
