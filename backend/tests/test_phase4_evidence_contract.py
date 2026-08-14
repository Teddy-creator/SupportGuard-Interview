from __future__ import annotations

import ast
import hashlib
import inspect
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from supportguard.agent.evidence import decide_evidence, derive_evidence_requirements
from supportguard.agent.evidence_contracts import EvidenceDecision, EvidenceRequirements

NOW = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
RUN_ID = "run-evidence-v1"
TENANT_ID = "tenant-evidence-v1"
CUSTOMER_ID = "customer-evidence-v1"
PROVIDER_ATTEMPT_ID = "provider-attempt-evidence-v1"
ROOT = Path(__file__).resolve().parents[2]


def _scope_hash(*, tenant_id: str = TENANT_ID, customer_id: str = CUSTOMER_ID) -> str:
    return hashlib.sha256(
        json.dumps(
            {"customer_id": customer_id, "tenant_id": tenant_id},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _observation(
    *,
    tool_name: str,
    source_ids: tuple[str, ...],
    data: dict[str, Any],
    suffix: str,
    run_id: str = RUN_ID,
    tenant_id: str = TENANT_ID,
    customer_id: str = CUSTOMER_ID,
    observed_at: datetime = NOW,
    fresh_until: datetime | None = None,
    freshness_status: str = "fresh",
) -> dict[str, Any]:
    scope_hash = _scope_hash(tenant_id=tenant_id, customer_id=customer_id)
    return {
        "observation_id": f"observation-{suffix}",
        "tool_call_id": f"call-{suffix}",
        "tool_name": tool_name,
        "run_id": run_id,
        "tenant_id": tenant_id,
        "customer_id": customer_id,
        "scope_hash": scope_hash,
        "status": "ok",
        "freshness_status": freshness_status,
        "observed_at": observed_at.isoformat(),
        "fresh_until": (fresh_until or NOW + timedelta(minutes=5)).isoformat(),
        "resource_version": "resource-v1",
        "source_refs": [
            {
                "source_type": "business_record",
                "source_id": source_id,
                "observed_at": observed_at.isoformat(),
            }
            for source_id in source_ids
        ],
        "trusted_scope": {
            "tenant_id": tenant_id,
            "customer_id": customer_id,
            "scope_hash": scope_hash,
        },
        "data": data,
    }


def _knowledge_observation(
    *,
    refusal_reason: str | None = None,
) -> dict[str, Any]:
    return _observation(
        tool_name="search_knowledge",
        source_ids=("knowledge:api-errors:c001",),
        suffix="knowledge",
        data={
            "evidence": [
                {
                    "evidence_id": "policy:c001",
                    "chunk_id": "api-errors:c001",
                    "supporting_span_eligible": True,
                }
            ],
            "conflict": refusal_reason is not None,
            "refusal_reason": refusal_reason,
        },
    )


def _citation_binding(
    *,
    binding_id: str = "citation-policy",
    provider_attempt_id: str = PROVIDER_ATTEMPT_ID,
) -> dict[str, str]:
    return {
        "citation_binding_id": binding_id,
        "provider_attempt_id": provider_attempt_id,
        "evidence_id": "policy:c001",
        "document_id": "api-errors",
        "chunk_id": "api-errors:c001",
        "content_hash": "a" * 64,
        "locator_hash": "b" * 64,
    }


def _decide(
    *,
    requirements: EvidenceRequirements,
    observations: list[dict[str, Any]],
    citation_bindings: list[dict[str, Any]] | None = None,
    evidence_conflict: bool = False,
    can_replan: bool = True,
    explainable_comparison: bool = False,
) -> EvidenceDecision:
    return decide_evidence(
        requirements=requirements,
        observations=observations,
        citation_bindings=citation_bindings or [],
        run_id=RUN_ID,
        tenant_id=TENANT_ID,
        customer_id=CUSTOMER_ID,
        provider_attempt_id=PROVIDER_ATTEMPT_ID,
        evidence_conflict=evidence_conflict,
        can_replan=can_replan,
        explainable_comparison=explainable_comparison,
        now=NOW,
    )


def test_decision_is_frozen_and_produced_before_candidate_response() -> None:
    decision = _decide(
        requirements=EvidenceRequirements(required_groups=("knowledge", "account")),
        observations=[
            _knowledge_observation(),
            _observation(
                tool_name="query_account",
                source_ids=("account:current",),
                suffix="account",
                data={"account_status": "active"},
            ),
        ],
        citation_bindings=[_citation_binding()],
    )

    assert decision.result == "accept"
    assert decision.satisfied_groups == ("knowledge", "account")
    assert [item.evidence_id for item in decision.eligible_citations] == ["policy:c001"]
    assert "candidate" not in inspect.signature(decide_evidence).parameters
    assert "material_claims" not in EvidenceDecision.model_fields
    with pytest.raises(ValidationError, match="frozen"):
        decision.result = "terminal"  # type: ignore[misc]


def test_request_semantics_derive_requirements_without_a_candidate() -> None:
    requirements = derive_evidence_requirements(
        issue_type="api_diagnostics",
        requested_action="refund",
        specified_request=True,
        additional_groups=("account",),
    )

    assert requirements.required_groups == (
        "knowledge",
        "request_trace",
        "billing_record",
        "account",
    )


def test_non_current_citation_fails_closed_with_stable_reason() -> None:
    decision = _decide(
        requirements=EvidenceRequirements(required_groups=("knowledge",)),
        observations=[_knowledge_observation()],
        citation_bindings=[_citation_binding(provider_attempt_id="provider-attempt-old")],
    )

    assert decision.result == "replan"
    assert decision.error_code == "citation_binding_incomplete"
    assert decision.eligible_citations == ()
    assert "citation_binding_not_current:citation-policy" in decision.insufficient_reasons


def test_context_binding_projects_only_the_seven_authoritative_fields() -> None:
    binding: dict[str, Any] = _citation_binding()
    binding["legacy_display_label"] = "not evidence identity"
    decision = _decide(
        requirements=EvidenceRequirements(required_groups=("knowledge",)),
        observations=[_knowledge_observation()],
        citation_bindings=[binding],
    )

    assert decision.result == "accept"
    assert decision.eligible_citations[0].model_dump() == _citation_binding()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("tenant_id", "tenant-foreign"),
        ("customer_id", "customer-foreign"),
        ("scope_hash", "f" * 64),
    ),
)
def test_top_level_scope_must_match_trusted_scope_and_current_identity(
    field: str,
    value: str,
) -> None:
    observation = _observation(
        tool_name="query_account",
        source_ids=("account:current",),
        suffix="scope-mismatch",
        data={"account_status": "active"},
    )
    observation[field] = value
    decision = _decide(
        requirements=EvidenceRequirements(required_groups=("account",)),
        observations=[observation],
        can_replan=False,
    )

    assert decision.result == "terminal"
    assert decision.fresh_scoped_observations == ()
    assert decision.missing_groups == ("account",)


@pytest.mark.parametrize(
    ("observed_at", "fresh_until"),
    (
        (NOW + timedelta(seconds=1), NOW + timedelta(minutes=5)),
        (NOW + timedelta(minutes=5), NOW + timedelta(minutes=1)),
    ),
)
def test_future_or_inverted_observation_time_fails_closed(
    observed_at: datetime,
    fresh_until: datetime,
) -> None:
    decision = _decide(
        requirements=EvidenceRequirements(required_groups=("account",)),
        observations=[
            _observation(
                tool_name="query_account",
                source_ids=("account:current",),
                suffix="bad-time",
                observed_at=observed_at,
                fresh_until=fresh_until,
                data={"account_status": "active"},
            )
        ],
        can_replan=False,
    )

    assert decision.result == "terminal"
    assert "observation_not_fresh_at_decision_time" in decision.insufficient_reasons
    assert decision.stale_groups == ("account",)


def test_duplicate_source_inside_one_observation_fails_closed() -> None:
    decision = _decide(
        requirements=EvidenceRequirements(required_groups=("account",)),
        observations=[
            _observation(
                tool_name="query_account",
                source_ids=("account:current", "account:current"),
                suffix="duplicate-source",
                data={"account_status": "active"},
            )
        ],
        can_replan=False,
    )

    assert decision.result == "terminal"
    assert "observation_source_identity_duplicate" in decision.insufficient_reasons


def test_repeated_observation_identity_is_ambiguous() -> None:
    observation = _observation(
        tool_name="query_account",
        source_ids=("account:current",),
        suffix="duplicate",
        data={"account_status": "active"},
    )
    repeated_observation = _decide(
        requirements=EvidenceRequirements(required_groups=("account",)),
        observations=[observation, dict(observation)],
        can_replan=False,
    )
    assert "observation_identity_ambiguous:observation-duplicate" in (
        repeated_observation.insufficient_reasons
    )


def test_same_authoritative_source_can_support_distinct_observations() -> None:
    decision = _decide(
        requirements=EvidenceRequirements(required_groups=("account", "subscription")),
        observations=[
            _observation(
                tool_name="query_account",
                source_ids=("subscription:current",),
                suffix="account-view",
                data={"account_status": "active"},
            ),
            _observation(
                tool_name="query_subscription",
                source_ids=("subscription:current",),
                suffix="subscription-view",
                data={"plan": "pro"},
            ),
        ],
        can_replan=False,
    )

    assert decision.result == "accept"
    assert decision.satisfied_groups == ("account", "subscription")


def test_one_source_id_cannot_fork_across_source_types() -> None:
    account = _observation(
        tool_name="query_account",
        source_ids=("shared:current",),
        suffix="account-source",
        data={"account_status": "active"},
    )
    subscription = _observation(
        tool_name="query_subscription",
        source_ids=("shared:current",),
        suffix="subscription-source",
        data={"plan": "pro"},
    )
    subscription["source_refs"][0]["source_type"] = "tool_result"

    decision = _decide(
        requirements=EvidenceRequirements(required_groups=("account", "subscription")),
        observations=[account, subscription],
        can_replan=False,
    )

    assert decision.result == "terminal"
    assert "observation_source_identity_conflict:shared:current" in (decision.insufficient_reasons)


def test_two_binding_ids_cannot_alias_one_evidence_identity() -> None:
    decision = _decide(
        requirements=EvidenceRequirements(required_groups=("knowledge",)),
        observations=[_knowledge_observation()],
        citation_bindings=[
            _citation_binding(binding_id="citation-one"),
            _citation_binding(binding_id="citation-two"),
        ],
        can_replan=False,
    )

    assert decision.result == "terminal"
    assert decision.eligible_citations == ()
    assert {
        "citation_evidence_identity_ambiguous:citation-one",
        "citation_evidence_identity_ambiguous:citation-two",
    } <= set(decision.insufficient_reasons)


def test_one_evidence_id_cannot_fork_into_conflicting_citation_identity() -> None:
    divergent = _citation_binding(binding_id="citation-two")
    divergent.update(
        {
            "document_id": "forged-document",
            "chunk_id": "forged-document:c999",
            "locator_hash": "c" * 64,
        }
    )
    decision = _decide(
        requirements=EvidenceRequirements(required_groups=("knowledge",)),
        observations=[_knowledge_observation()],
        citation_bindings=[
            _citation_binding(binding_id="citation-one"),
            divergent,
        ],
        can_replan=False,
    )

    assert decision.result == "terminal"
    assert decision.eligible_citations == ()
    assert {
        "citation_evidence_identity_ambiguous:citation-one",
        "citation_evidence_identity_ambiguous:citation-two",
    } <= set(decision.insufficient_reasons)


def test_only_explicit_version_comparison_conflict_is_explainable() -> None:
    requirements = EvidenceRequirements(required_groups=("knowledge",))
    observations = [_knowledge_observation(refusal_reason="unresolved_published_version_conflict")]
    blocked = _decide(
        requirements=requirements,
        observations=observations,
        citation_bindings=[_citation_binding()],
        evidence_conflict=True,
        can_replan=False,
    )
    explained = _decide(
        requirements=requirements,
        observations=observations,
        citation_bindings=[_citation_binding()],
        evidence_conflict=True,
        can_replan=False,
        explainable_comparison=True,
    )

    assert blocked.error_code == "evidence_conflict"
    assert blocked.result == "terminal"
    assert explained.result == "accept"
    assert explained.conflict_reasons == ("unresolved_published_version_conflict",)


def test_evidence_owner_remains_pure_and_core_decisions_stay_bounded() -> None:
    source = (ROOT / "backend/src/supportguard/agent/evidence.py").read_text()
    tree = ast.parse(source)
    imported_roots = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    forbidden = ("sqlalchemy", "supportguard.db", "supportguard.mcp", "supportguard.providers")
    assert not any(name.startswith(forbidden) for name in imported_roots)

    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for function_name in ("decide_evidence", "assess_terminal_evidence"):
        function = functions[function_name]
        assert function.end_lineno is not None
        assert function.end_lineno - function.lineno + 1 < 200


def test_decision_constructor_rejects_incomplete_group_disposition() -> None:
    with pytest.raises(ValidationError, match="exactly one disposition"):
        EvidenceDecision(
            run_id=RUN_ID,
            tenant_id=TENANT_ID,
            customer_id=CUSTOMER_ID,
            provider_attempt_id=PROVIDER_ATTEMPT_ID,
            requirements=EvidenceRequirements(required_groups=("account",)),
            sufficient=True,
            result="accept",
        )


def test_decision_constructor_rejects_duplicate_group_disposition() -> None:
    with pytest.raises(ValidationError, match="disposition must be unique"):
        EvidenceDecision(
            run_id=RUN_ID,
            tenant_id=TENANT_ID,
            customer_id=CUSTOMER_ID,
            provider_attempt_id=PROVIDER_ATTEMPT_ID,
            requirements=EvidenceRequirements(required_groups=("account",)),
            sufficient=True,
            result="accept",
            satisfied_groups=("account", "account"),
        )
