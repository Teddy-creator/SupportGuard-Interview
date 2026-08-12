from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from supportguard.agent.nodes.intake import IntakeNodes
from supportguard.agent.schemas import Classification
from supportguard.agent.state import AgentState
from supportguard.config import Settings
from supportguard.providers.base import (
    ProviderCallResult,
    ProviderUsage,
    canonical_transport_record,
)
from supportguard.providers.deepseek import ProviderStructuredOutputError

ROOT = Path(__file__).resolve().parents[2]
INTAKE_SOURCE = ROOT / "backend/src/supportguard/agent/nodes/intake.py"


class _RecordingProvider:
    mode = "fake"
    model = "phase4-intake-fixture"
    tool_call_mode = "structured_output"

    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def generate(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _IntakeHost:
    session = None
    history_loader = None

    def __init__(
        self,
        provider: _RecordingProvider,
    ) -> None:
        self.provider = provider
        self.settings = Settings(_env_file=None, app_env="test")
        self.events: list[tuple[str, dict[str, Any], str | None]] = []
        self.finishes: list[dict[str, Any]] = []
        self.reservation_count = 0

    def _canonical_action_query_classification(
        self,
        classification: Classification,
        **_kwargs: Any,
    ) -> Classification:
        return classification

    def _decision_error_paths(self, failure: Exception) -> list[str]:
        return list(getattr(failure, "error_paths", ()))

    async def _event(
        self,
        _state: AgentState,
        event_type: str,
        payload: dict[str, Any],
        *,
        visibility: str | None = None,
        **_kwargs: Any,
    ) -> None:
        self.events.append((event_type, payload, visibility))

    def _exception_transport_attempts(self, failure: Exception) -> int | None:
        value = getattr(failure, "transport_attempts", None)
        return value if isinstance(value, int) else None

    async def _finish_external(self, _reservation: Any, **kwargs: Any) -> None:
        self.finishes.append(kwargs)

    async def _persist_context_ledger(self, *_args: Any, **_kwargs: Any) -> str:
        return "context_fixture"

    def _provider_failure_error_code(self, _failure: BaseException) -> str:
        return "provider_fixture_failure"

    async def _reserve_external(self, *_args: Any, **_kwargs: Any) -> tuple[object, Any]:
        self.reservation_count += 1
        return object(), SimpleNamespace(id=f"attempt_{self.reservation_count}")

    def _resolve_existing_action_replay(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def _safe_stop(
        self,
        _state: AgentState,
        reason: str,
        *,
        error_code: str | None = None,
        **_kwargs: Any,
    ) -> AgentState:
        return cast(
            AgentState,
            {
                "candidate": {"response_type": "safe_stop"},
                "safe_stop_reason": reason,
                "error_code": error_code,
            },
        )

    def _trace(self, *_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {"trace_id": "trace_intake"}


def _state(message: str, **updates: Any) -> AgentState:
    state: AgentState = {
        "tenant_id": "tenant_demo",
        "ticket_id": "ticket_intake",
        "customer_id": "cust_demo",
        "run_id": "run_intake",
        "trace_id": "trace_intake",
        "user_message": message,
        "redacted_message": message,
        "classification_context": [],
        "current_actions": [],
        "llm_calls": 0,
        "structure_repair_used": False,
    }
    state.update(updates)
    return state


def _classification(*, issue_type: str = "api_diagnostics") -> Classification:
    return Classification(
        issue_type=issue_type,
        risk="low",
        policy_boundary="allowed",
        requested_action="none",
        requested_concurrency_limit=None,
        needs_realtime_facts=True,
        support_subject="customer_problem",
        rationale="The customer request requires bounded support classification.",
    )


def test_phase4_intake_has_named_bounded_stages_without_impl_wrappers() -> None:
    tree = ast.parse(INTAKE_SOURCE.read_text())
    intake = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "IntakeNodes"
    )
    methods = {
        node.name: node
        for node in intake.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    required_stages = {
        "_prepare_classification",
        "_publish_missing_action_admission",
        "_classify_with_provider",
        "_repair_structured_classification",
        "_stop_after_provider_failure",
        "_publish_provider_classification",
    }
    assert required_stages <= methods.keys()
    assert methods["classify"].end_lineno - methods["classify"].lineno + 1 < 120
    assert max(node.end_lineno - node.lineno + 1 for node in methods.values()) < 200
    assert not any(name.endswith("_impl") for name in methods)


@pytest.mark.asyncio
async def test_phase4_intake_redacts_pii_and_pins_missing_action_after_provider() -> None:
    provider = _RecordingProvider(
        [
            ProviderCallResult(
                output=_classification(),
                attempts=1,
                usage=ProviderUsage(prompt_tokens=4, completion_tokens=2),
                trace_metadata={},
                transport=canonical_transport_record({"input": "missing-action"}),
            )
        ]
    )
    host = _IntakeHost(provider)
    nodes = IntakeNodes(cast(Any, host))
    state = _state("邮箱 user@example.com，请帮我退款。")

    redacted = await nodes.redact(state)
    state.update(redacted)
    result = await nodes.classify(state)

    assert state["redacted_message"] == "邮箱 [REDACTED_EMAIL]，请帮我退款。"
    assert result["classification"]["requested_action"] == "refund"
    assert result["action_admission"]["missing_fields"] == ["billing_record_id"]
    assert result["llm_calls"] == 1
    assert len(provider.calls) == 1
    assert host.reservation_count == 1
    assert host.events[-1][1]["grants_action_authority"] is False


@pytest.mark.asyncio
async def test_phase4_intake_canonicalizes_an_immediate_action_correction() -> None:
    provider = _RecordingProvider(
        [
            ProviderCallResult(
                output=_classification(),
                attempts=1,
                usage=ProviderUsage(prompt_tokens=4, completion_tokens=2),
                trace_metadata={},
                transport=canonical_transport_record({"input": "action-correction"}),
            )
        ]
    )
    host = _IntakeHost(provider)
    nodes = IntakeNodes(cast(Any, host))
    state = _state(
        "请改成 40，按正常审批流程处理。",
        classification_context=[
            {
                "role": "customer",
                "content": "不要把并发提高到 80。",
                "message_id": "message_previous",
            }
        ],
    )

    result = await nodes.classify(state)

    assert result["classification"]["issue_type"] == "entitlement_change"
    assert result["classification"]["requested_action"] == "entitlement_change"
    assert result["classification"]["requested_concurrency_limit"] is None
    assert host.events[-1][1]["deterministic_current_action"] == "entitlement_change"
    assert host.events[-1][1]["grants_action_authority"] is False


@pytest.mark.asyncio
async def test_phase4_intake_repair_preserves_context_route_and_attempt_accounting() -> None:
    transport = canonical_transport_record({"input": "classification"})
    provider = _RecordingProvider(
        [
            ProviderStructuredOutputError(
                error_paths=("issue_type:missing",),
                transport=transport,
                usage=ProviderUsage(prompt_tokens=5, completion_tokens=2),
            ),
            ProviderCallResult(
                output=_classification(),
                attempts=1,
                usage=ProviderUsage(prompt_tokens=4, completion_tokens=3),
                trace_metadata={},
                transport=transport,
            ),
        ]
    )
    host = _IntakeHost(provider)
    nodes = IntakeNodes(cast(Any, host))
    state = _state(
        "429是什么意思？",
        classification_context=[
            {
                "role": "customer",
                "content": "atlas-chat 返回了 concurrency_limit_exceeded",
                "message_id": "message_previous",
            }
        ],
    )

    result = await nodes.classify(state)

    assert result["classification"]["issue_type"] == "api_diagnostics"
    assert result["llm_calls"] == 2
    assert result["structure_repair_used"] is True
    assert result["latest_provider_attempt_id"] == "attempt_2"
    assert result["classification_context"][0]["message_id"] == "message_previous"
    assert [item["status"] for item in host.finishes] == ["failed", "succeeded"]
    assert provider.calls[1]["trace_metadata"]["repair_of_attempt_id"] == "attempt_1"


@pytest.mark.asyncio
async def test_phase4_intake_current_actions_are_context_not_authority() -> None:
    transport = canonical_transport_record({"input": "current-action-query"})
    provider = _RecordingProvider(
        [
            ProviderCallResult(
                output=_classification(issue_type="billing_refund"),
                attempts=1,
                usage=ProviderUsage(),
                trace_metadata={},
                transport=transport,
            )
        ]
    )
    host = _IntakeHost(provider)
    nodes = IntakeNodes(cast(Any, host))
    current_actions = [
        {
            "schema_version": "conversation-action-state.v1",
            "approval_id": "approval_rejected",
            "origin_run_id": "run_origin",
            "origin_turn_id": "turn_origin",
            "action_type": "refund",
            "resource_type": "billing_record",
            "resource_id": "bill_rejected",
            "resource_version": 2,
            "approval_status": "rejected",
            "projection_status": "rejected",
            "status_version": 3,
            "actionable": False,
            "allowed_customer_actions": [],
            "decision_class": "reject",
            "customer_safe_reason_code": "approval_rejected_no_effect",
            "execution_state": "not_executed",
            "business_action_id": None,
            "updated_at": datetime(2026, 8, 12, tzinfo=UTC).isoformat(),
            "source_event_id": "event_rejected",
            "source_event_hash": "a" * 64,
            "grants_action_authority": False,
        }
    ]
    action_query = {
        "schema_version": "conversation-action-state-query.v1",
        "resolution": "selected",
        "approval_id": "approval_rejected",
        "query_kind": "reason",
        "grants_action_authority": False,
    }

    result = await nodes.classify(
        _state(
            "为什么拒绝了？",
            current_actions=current_actions,
            classification_context=[
                {
                    "role": "action",
                    "message_id": "message_action_rejected",
                    "approval_id": "approval_rejected",
                    "content": "退款申请已拒绝。",
                }
            ],
        )
    )

    provider_input = json.loads(provider.calls[0]["user"])
    assert provider_input["trusted_current_actions"] == current_actions
    assert provider_input["current_actions_grant_action_authority"] is False
    assert result["action_state_query"] == action_query
    event = next(payload for name, payload, _visibility in host.events if name == "classification")
    assert event["deterministic_action_state_query"] == action_query
    assert event["grants_action_authority"] is False
    assert "structure_repair_used" not in event
