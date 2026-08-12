from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

import supportguard.actions.service as action_service
import supportguard.agent.action_specs as compatibility_facade
from supportguard.approvals.coordinator import ApprovalCoordinator
from supportguard.contracts.errors import RuntimeConflict
from supportguard.services.actions import RuntimeActionExecutor, action_resource_id
from supportguard.services.approval_lifecycle import (
    ACTION_RESOURCE_TYPES,
    canonical_approval_identity_values,
)


def test_proposal_eligibility_uses_bounded_named_stages() -> None:
    path = Path("backend/src/supportguard/agent/proposal_assembler.py")
    tree = ast.parse(path.read_text())
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    stages = (
        "_validate_candidate_context",
        "_validate_observation_bindings",
        "_proposal_field_binding_error",
    )

    assert set(stages) <= functions.keys()
    for name in (*stages, "evaluate_action_candidate_eligibility"):
        function = functions[name]
        assert function.end_lineno is not None
        assert function.end_lineno - function.lineno + 1 < 200

    orchestrator = ast.get_source_segment(
        path.read_text(), functions["evaluate_action_candidate_eligibility"]
    )
    assert orchestrator is not None
    assert [orchestrator.index(name) for name in stages] == sorted(
        orchestrator.index(name) for name in stages
    )
    assert "_impl" not in orchestrator


def test_action_service_is_the_single_three_action_contract_owner() -> None:
    assert tuple(action_service.ACTION_SPECS) == (
        "refund",
        "api_key_revocation",
        "entitlement_change",
    )
    assert len({item.proposal_action for item in action_service.ACTION_SPECS.values()}) == len(
        action_service.ACTION_SPECS
    )
    assert len({item.policy_capability for item in action_service.ACTION_SPECS.values()}) == len(
        action_service.ACTION_SPECS
    )
    assert len(
        {item.runtime_effect_capability for item in action_service.ACTION_SPECS.values()}
    ) == len(action_service.ACTION_SPECS)

    for action_type, spec in action_service.ACTION_SPECS.items():
        resource_obligations = [
            item
            for item in spec.obligations
            if item.kind == "resource" and item.observed_resource_field is not None
        ]
        assert len(resource_obligations) == 1
        resource_obligation = resource_obligations[0]
        assert resource_obligation.capabilities == (spec.primary_read_capability,)
        assert resource_obligation.observed_resource_field == spec.resource_field
        assert any(
            item.source == "observation"
            and item.obligation_id == resource_obligation.obligation_id
            and item.target_field == spec.resource_field
            and item.source_path == spec.resource_field
            for item in spec.proposal_fields
        )
        assert action_service.get_action_spec_or_none(action_type) is spec
        assert action_service.get_action_spec_by_proposal(spec.proposal_action) is spec
        assert action_service.get_action_spec_by_policy_capability(spec.policy_capability) is spec
        assert (
            action_service.get_action_spec_by_runtime_effect_capability(
                spec.runtime_effect_capability
            )
            is spec
        )


def test_action_service_unknown_reverse_lookups_fail_closed() -> None:
    assert action_service.get_action_spec_or_none("unknown") is None
    assert action_service.get_action_spec_by_proposal("unknown") is None
    assert action_service.get_action_spec_by_policy_capability("unknown") is None
    assert action_service.get_action_spec_by_runtime_effect_capability("unknown") is None


@pytest.mark.parametrize(
    ("action_type", "proposal_action", "resource_type", "resource_id", "arguments"),
    [
        (
            "refund",
            "refund_proposal",
            "billing_record_id",
            "bill_42",
            {"billing_record_id": "bill_42", "refund_reason": "duplicate charge"},
        ),
        (
            "api_key_revocation",
            "api_key_revocation_proposal",
            "api_key_id",
            "key_42",
            {"api_key_id": "key_42", "reason": "credential was exposed"},
        ),
        (
            "entitlement_change",
            "entitlement_change_proposal",
            "subscription_id",
            "sub_42",
            {
                "subscription_id": "sub_42",
                "change_type": "quota_change",
                "target": {"rpm_limit": 1200},
                "reason": "approved capacity change",
            },
        ),
    ],
)
def test_three_actions_share_one_typed_candidate_decision_effect_chain(
    action_type: str,
    proposal_action: str,
    resource_type: str,
    resource_id: str,
    arguments: dict[str, object],
) -> None:
    candidate = action_service.build_action_candidate(
        proposal_action=proposal_action,
        action_type=action_type,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_version=2,
        trusted_arguments=arguments,
        observation_binding=[{"tool_name": "current_resource", "resource_id": resource_id}],
        citation_binding_ids=["citation_42"],
    )
    decision = action_service.build_approval_decision(
        command={"action": "approve", "approver_id": "approver_42"},
        proposal_result={
            "approval_id": "approval_42",
            "idempotency_key": "approval:42",
        },
    )
    effect = action_service.build_runtime_effect_result(
        candidate=candidate,
        decision=decision,
        payload={
            "status": "succeeded",
            "business_action_id": "action_42",
            "reused": False,
        },
    )

    spec = action_service.get_action_spec_or_none(action_type)
    assert spec is not None
    assert candidate.policy_capability == spec.policy_capability
    assert candidate.runtime_effect_capability == spec.runtime_effect_capability
    assert decision.approval_id == "approval_42"
    assert effect.action_type == action_type
    assert effect.resource_id == resource_id
    assert effect.business_action_id == "action_42"


def test_typed_action_chain_rejects_cross_action_and_approval_identity_drift() -> None:
    with pytest.raises(ValueError, match="identity is unsupported"):
        action_service.build_action_candidate(
            proposal_action="refund_proposal",
            action_type="api_key_revocation",
            resource_type="billing_record_id",
            resource_id="bill_42",
            resource_version=2,
            trusted_arguments={
                "billing_record_id": "bill_42",
                "refund_reason": "duplicate charge",
            },
            observation_binding=[{"resource_id": "bill_42"}],
            citation_binding_ids=[],
        )

    with pytest.raises(ValueError, match="identity changed"):
        action_service.build_approval_decision(
            command={
                "action": "approve",
                "approval_id": "approval_foreign",
            },
            proposal_result={
                "approval_id": "approval_42",
                "idempotency_key": "approval:42",
            },
        )

    candidate = action_service.build_action_candidate(
        proposal_action="refund_proposal",
        action_type="refund",
        resource_type="billing_record_id",
        resource_id="bill_42",
        resource_version=2,
        trusted_arguments={
            "billing_record_id": "bill_42",
            "refund_reason": "duplicate charge",
        },
        observation_binding=[{"resource_id": "bill_42"}],
        citation_binding_ids=[],
    )
    decision = action_service.build_approval_decision(
        command={"action": "approve"},
        proposal_result={
            "approval_id": "approval_42",
            "idempotency_key": "approval:42",
        },
    )
    with pytest.raises(ValueError, match="effect identity changed"):
        action_service.build_runtime_effect_result(
            candidate=candidate,
            decision=decision,
            payload={
                "approval_id": "approval_foreign",
                "status": "succeeded",
            },
        )


def test_approval_decision_keeps_command_and_proposal_idempotency_scopes_distinct() -> None:
    decision = action_service.build_approval_decision(
        command={
            "action": "approve",
            "approval_id": "approval_42",
            "idempotency_key": "approval:proposal_42",
        },
        proposal_result={
            "approval_id": "approval_42",
            "idempotency_key": "proposal:draft_42",
        },
    )

    assert decision.idempotency_key == "approval:proposal_42"
    assert decision.command["idempotency_key"] == "approval:proposal_42"


def test_agent_action_specs_is_an_identity_preserving_compatibility_facade() -> None:
    public_symbols = (
        "ACTION_SPECS",
        "ActionSpec",
        "EvidenceObligationSpec",
        "ProposalFieldBinding",
        "TerminalOutcomeRule",
        "get_action_spec",
        "get_action_spec_by_proposal",
        "get_action_spec_by_runtime_effect_capability",
        "get_action_spec_or_none",
        "get_action_spec_by_policy_capability",
    )

    for symbol in public_symbols:
        assert getattr(compatibility_facade, symbol) is getattr(action_service, symbol)


def test_production_code_does_not_import_the_compatibility_facade() -> None:
    source_root = Path("backend/src/supportguard")
    facade_imports = [
        path
        for path in source_root.rglob("*.py")
        if path != source_root / "agent" / "action_specs.py"
        and "supportguard.agent.action_specs" in path.read_text()
    ]

    assert facade_imports == []


def test_action_contract_has_one_definition_site() -> None:
    source_root = Path("backend/src/supportguard")
    action_spec_sites = [
        path for path in source_root.rglob("*.py") if "class ActionSpec(" in path.read_text()
    ]
    registry_sites = [
        path for path in source_root.rglob("*.py") if "ACTION_SPECS: Final[" in path.read_text()
    ]

    expected = [Path("backend/src/supportguard/actions/service.py")]
    assert action_spec_sites == expected
    assert registry_sites == expected


def test_current_finalizer_uses_one_runtime_executor_for_all_three_actions() -> None:
    finalizer_source = Path("backend/src/supportguard/services/segment_finalization.py").read_text()
    executor_source = Path("backend/src/supportguard/services/actions.py").read_text()

    assert "RefundRuntime" not in finalizer_source
    assert finalizer_source.count("RuntimeActionExecutor(self.session).execute(") == 1
    for action_type in ("refund", "api_key_revocation", "entitlement_change"):
        assert f'approval.action_type == "{action_type}"' in executor_source


def test_action_service_is_the_proposal_approval_effect_pipeline_owner() -> None:
    service_path = Path("backend/src/supportguard/actions/service.py")
    service_source = service_path.read_text()
    service_tree = ast.parse(service_source)
    service = next(
        node
        for node in service_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ActionService"
    )
    service_methods = {
        node.name: node
        for node in service.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert {"propose", "execute"} <= service_methods.keys()
    for method in service_methods.values():
        assert method.end_lineno is not None
        assert method.end_lineno - method.lineno + 1 < 200
    assert "gateway.call_action(" in service_source
    assert "finish_action_capability(" in service_source
    assert "build_runtime_effect_result(" in service_source
    assert "supportguard.providers" not in service_source
    assert "supportguard.memory" not in service_source

    adapter_path = Path("backend/src/supportguard/agent/nodes/approval.py")
    adapter_source = adapter_path.read_text()
    adapter_tree = ast.parse(adapter_source)
    approval_nodes = next(
        node
        for node in adapter_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ApprovalNodes"
    )
    adapter_methods = {
        node.name: node
        for node in approval_nodes.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name in ("create_proposal", "execute_approved_action"):
        method = adapter_methods[name]
        assert method.end_lineno is not None
        assert method.end_lineno - method.lineno + 1 < 150
        method_source = ast.get_source_segment(adapter_source, method)
        assert method_source is not None
        assert "ActionService()." in method_source
        assert "gateway.call_action(" not in method_source
        assert "build_runtime_effect_result(" not in method_source


def test_legacy_refund_runtime_is_a_thin_runtime_executor_delegate() -> None:
    compatibility_path = Path("backend/src/supportguard/approvals/service.py")
    compatibility_source = compatibility_path.read_text()
    compatibility_tree = ast.parse(compatibility_source)
    refund_runtime = next(
        node
        for node in compatibility_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "RefundRuntime"
    )
    execute_refund = next(
        node
        for node in refund_runtime.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "execute_refund"
    )
    execute_source = ast.get_source_segment(compatibility_source, execute_refund)

    assert execute_source is not None
    assert execute_refund.end_lineno is not None
    assert execute_refund.end_lineno - execute_refund.lineno + 1 < 100
    assert "executor.execute_legacy_refund(" in execute_source
    assert "executor.execute(" in execute_source
    for duplicate_effect_owner in (
        "BusinessAction(",
        "BillingRecord(",
        "ActionLifecycleService",
        "self.session.add(",
        "activate_next_turn_and_converge_ticket",
    ):
        assert duplicate_effect_owner not in execute_source

    executor_path = Path("backend/src/supportguard/services/actions.py")
    executor_tree = ast.parse(executor_path.read_text())
    runtime_executor = next(
        node
        for node in executor_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "RuntimeActionExecutor"
    )
    owned_methods = {
        node.name
        for node in runtime_executor.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert {"execute", "execute_legacy_refund", "_refund"} <= owned_methods


@pytest.mark.parametrize(
    ("action_type", "resource_field", "resource_id"),
    [
        ("refund", "billing_record_id", "bill_42"),
        ("api_key_revocation", "api_key_id", "key_42"),
        ("entitlement_change", "subscription_id", "sub_42"),
    ],
)
def test_runtime_resource_resolution_is_derived_from_action_spec(
    action_type: str,
    resource_field: str,
    resource_id: str,
) -> None:
    assert ACTION_RESOURCE_TYPES[action_type] == resource_field
    assert action_resource_id(action_type, {resource_field: resource_id}) == resource_id

    approval = SimpleNamespace(
        action_type=action_type,
        resource_type=resource_field,
        resource_id=resource_id,
        business_version=2,
    )
    revision = SimpleNamespace(resource_version=2)
    assert RuntimeActionExecutor._resource_id(approval, revision) == resource_id


@pytest.mark.parametrize(
    ("action_type", "payload"),
    [("unknown", {"billing_record_id": "bill_42"}), ("refund", {})],
)
def test_runtime_resource_resolution_preserves_fail_closed_error(
    action_type: str,
    payload: dict[str, str],
) -> None:
    with pytest.raises(RuntimeConflict, match="approval_resource_missing"):
        action_resource_id(action_type, payload)


def test_canonical_approval_identity_rejects_unknown_action() -> None:
    run = SimpleNamespace(tenant_id="tenant_42", turn_id="turn_42")

    with pytest.raises(RuntimeConflict, match="approval_canonical_identity_invalid"):
        canonical_approval_identity_values(
            tenant_id="tenant_42",
            customer_id="customer_42",
            action_type="unknown",
            resource_id="resource_42",
            resource_version=1,
            run=run,
        )


@pytest.mark.asyncio
async def test_unknown_action_policy_binding_fails_before_database_access() -> None:
    class FailOnExecute:
        async def execute(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("unknown action must fail before database access")

    snapshot = SimpleNamespace(
        action_type="unknown",
        policy_binding={
            "schema_version": "deterministic-policy-binding.v1",
            "capability_invocation_id": "invocation_42",
        },
    )

    assert not await ApprovalCoordinator._validate_policy_binding(  # noqa: SLF001
        FailOnExecute(),
        snapshot,
    )
