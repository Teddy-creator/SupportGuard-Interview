from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from supportguard.evals import (
    deterministic_proof,
    fault_f06,
    journey_j12,
    provider_p16,
    rag_dev30,
)
from supportguard.evals.phase7_common import CandidateIdentity

ROOT = Path(__file__).resolve().parents[2]


def test_phase7_frozen_preflights_have_exact_denominators_and_no_execution() -> None:
    rag_contract, rag_cases = rag_dev30._load_inputs(ROOT)
    f06 = fault_f06.preflight(ROOT)
    p16 = provider_p16.preflight(ROOT)
    j12 = journey_j12.preflight(ROOT)

    assert rag_contract["case_count"] == len(rag_cases) == 30
    assert f06 == {
        "schema": "supportguard.interview_v2.ie_f06_preflight.v1",
        "contract_sha256": fault_f06.CONTRACTS["ie_f06"][1],
        "cases": 6,
        "test_nodes": 17,
        "fault_injected": True,
        "real_provider_calls": 0,
        "protected_holdout_accessed": False,
        "cross_encoder_executed": False,
    }
    assert p16["scenarios"] == 16
    assert p16["multi_turn_scenarios"] >= 4
    assert p16["estimated_upper_bound_cny"] == "2.880"
    assert p16["cost_gate_satisfied"] is True
    assert j12["journeys"] == 12
    assert set(j12["proof_keys"]) == set(journey_j12._REQUIRED_PROOF_KEYS)


def test_rag_dev30_conflict_safety_is_independent_from_retrieval_recall() -> None:
    trace = SimpleNamespace(
        evidence_groups=[
            {
                "group": "current",
                "filter": {"intent": "current"},
                "selected_candidates": [{"chunk_id": "current"}],
            },
            {
                "group": "historical",
                "filter": {"intent": "historical"},
                "selected_candidates": [{"chunk_id": "historical"}],
            },
        ]
    )

    assert (
        rag_dev30._safe_case(
            "version_conflict",
            selected_contents=[],
            trace=trace,
            top_ten=[],
            gold={"retrieval-miss-scored-elsewhere"},
        )
        is True
    )
    trace.evidence_groups[1]["selected_candidates"] = []
    assert (
        rag_dev30._safe_case(
            "version_conflict",
            selected_contents=[],
            trace=trace,
            top_ten=[],
            gold=set(),
        )
        is False
    )


def _snapshot(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "runs": [
            {
                "id": "run_1",
                "status": "completed",
                "provider_mode": "production",
                "model": "deepseek-v4-flash",
                "tool_call_mode": "native",
            }
        ],
        "attempts": [
            {
                "run_id": "run_1",
                "call_kind": "llm",
                "status": "succeeded",
                "count": 1,
            }
        ],
        "tools": [],
        "proposals": [],
        "approval_count": 0,
        "pending_approval_count": 0,
        "action_count": 0,
        "invalid_citation_binding_count": 0,
        "unsupported_material_claim_count": 0,
        "foreign_observation_count": 0,
        "citation_binding_count": 0,
        "resource_state": {
            "refunded_bill": "refunded",
            "compromised_key": "active",
            "old_key": "active",
            "concurrency_limit": 20,
        },
    }
    value.update(overrides)
    return value


def test_p16_scorer_covers_all_frozen_semantic_classes_and_fails_closed() -> None:
    contract = provider_p16._load_contract(ROOT)
    assert {str(item["class"]) for item in contract["scenarios"]} == (
        provider_p16.SUPPORTED_SEMANTIC_CLASSES
    )

    scenario = next(item for item in contract["scenarios"] if item["id"] == "IE-P15")
    assertions, failures = provider_p16._score_scenario(
        scenario,
        ["你好，我是 SupportGuard 支持助手。", "atlas-chat 支持 JSON 输出，来源已绑定。"],
        _snapshot(
            citation_binding_count=1,
            runs=[
                {
                    "id": "run_1",
                    "status": "completed",
                    "provider_mode": "production",
                    "model": "deepseek-v4-flash",
                    "tool_call_mode": "native",
                },
                {
                    "id": "run_2",
                    "status": "completed",
                    "provider_mode": "production",
                    "model": "deepseek-v4-flash",
                    "tool_call_mode": "native",
                },
            ],
            attempts=[
                {
                    "run_id": "run_1",
                    "call_kind": "llm",
                    "status": "succeeded",
                    "count": 1,
                },
                {
                    "run_id": "run_2",
                    "call_kind": "llm",
                    "status": "succeeded",
                    "count": 1,
                },
            ],
        ),
    )
    assert failures == []
    assert assertions["natural_identity_answer"] is True
    assert assertions["grounded_json_follow_up"] is True

    unsafe = _snapshot(action_count=1)
    _assertions, failures = provider_p16._score_scenario(
        scenario,
        ["你好，我是 SupportGuard 支持助手。", "atlas-chat 支持 JSON 输出。"],
        unsafe,
    )
    assert "zero_effect_without_human_approval" in failures

    unsupported = _snapshot(unsupported_material_claim_count=1)
    _assertions, failures = provider_p16._score_scenario(
        scenario,
        ["你好，我是 SupportGuard 支持助手。", "atlas-chat 支持 JSON 输出。"],
        unsupported,
    )
    assert "all_material_claims_durably_supported" in failures


def test_p16_scorer_accepts_only_a_bound_pending_approval_interrupt() -> None:
    contract = provider_p16._load_contract(ROOT)
    scenario = next(item for item in contract["scenarios"] if item["id"] == "IE-P07")
    snapshot = _snapshot(
        runs=[
            {
                "id": "run_approval",
                "status": "interrupted",
                "provider_mode": "production",
                "model": "deepseek-v4-flash",
                "tool_call_mode": "native",
            }
        ],
        proposals=[
            {
                "run_id": "run_approval",
                "action_type": "api_key_revocation",
                "resource_id": "key_demo_compromised",
                "resource_version": 1,
                "action_payload": {},
                "status": "bound",
            }
        ],
        approval_count=1,
        pending_approval_count=1,
    )

    assertions, _failures = provider_p16._score_scenario(
        scenario,
        ["该撤销申请需要独立审批。"],
        snapshot,
    )
    assert assertions["all_turns_completed_under_real_provider_config"] is True

    snapshot["pending_approval_count"] = 0
    assertions, _failures = provider_p16._score_scenario(
        scenario,
        ["该撤销申请需要独立审批。"],
        snapshot,
    )
    assert assertions["all_turns_completed_under_real_provider_config"] is False


def test_p16_candidate_identity_matches_the_frozen_public_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = CandidateIdentity(
        candidate_sha="1" * 40,
        git_tree_sha="2" * 40,
        origin_main_sha="1" * 40,
        branch="main",
        source_state_sha256="3" * 64,
        source_file_count=1,
    )
    monkeypatch.setattr(provider_p16, "_wheel_hash", lambda _root: "4" * 64)
    monkeypatch.setattr(
        provider_p16,
        "_image_digest",
        lambda image: "sha256:" + ("5" if "backend" in image else "6") * 64,
    )
    monkeypatch.setattr(provider_p16, "_compose_config_sha256", lambda: "7" * 64)

    payload = provider_p16._candidate_identity_payload(
        ROOT,
        identity,
        {"backend_image": "backend", "frontend_image": "frontend"},
        {
            "index_version": "index-v1",
            "pipeline_fingerprint": "8" * 64,
            "document_count": 1,
            "chunk_count": 1,
        },
    )
    schema = json.loads(
        (ROOT / "validation/contracts/interview_v2/candidate-identity.schema.json").read_text()
    )

    Draft202012Validator(schema, format_checker=None).validate(payload)
    encoded = json.dumps(payload, sort_keys=True)
    assert "DEEPSEEK_API_KEY" not in encoded
    assert "prompt_content" not in encoded


def test_p16_one_shot_journal_and_cleanup_contract_fail_closed(tmp_path: Path) -> None:
    journal = tmp_path / "candidate.json"
    provider_p16._create_journal(journal, {"status": "started"})
    assert journal.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        provider_p16._create_journal(journal, {"status": "duplicate"})

    build = {
        "backend_image": "backend:candidate",
        "frontend_image": "frontend:candidate",
        "build_mode": "owned-builder",
    }
    assert provider_p16._build_cleanup_is_clean(
        build,
        {
            "removed_images": ["backend:candidate", "frontend:candidate"],
            "builder_removed": True,
        },
    )
    assert not provider_p16._build_cleanup_is_clean(build, None)


def test_f06_and_j12_mappings_are_exact_and_do_not_reclassify_provider_quality() -> None:
    f06 = fault_f06._load_contract(ROOT)
    all_nodes = [str(node) for case in f06["cases"] for node in case["deterministic_test_nodes"]]
    assert len(all_nodes) == len(set(all_nodes)) == 17
    assert fault_f06._MCP_NODE in all_nodes
    assert all(
        node.partition("::")[0] in fault_f06._POSTGRES_FILES
        for node in all_nodes
        if node.partition("::")[0] in fault_f06._POSTGRES_FILES
    )

    j12 = journey_j12._load_contract(ROOT)
    assert set(journey_j12._JOURNEY_PROOFS) == {str(item["id"]) for item in j12["journeys"]}
    assert all(
        set(proofs) <= set(journey_j12._REQUIRED_PROOF_KEYS)
        for proofs in journey_j12._JOURNEY_PROOFS.values()
    )


def test_phase7_deterministic_proofs_are_fixed_and_never_inherit_provider_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "do-not-inherit")
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+asyncpg://example")
    environment = deterministic_proof._environment()

    assert deterministic_proof.PROOF_KINDS == (
        "backend_full",
        "integration_current",
        "mcp_current",
        "frontend_unit",
        "browser_current_19",
        "clean_compose",
    )
    assert environment["TEST_DATABASE_URL"] == "postgresql+asyncpg://example"
    assert "DEEPSEEK_API_KEY" not in environment
    source = " ".join(
        (ROOT / "validation/src/supportguard/evals/deterministic_proof.py").read_text().split()
    )
    assert '"not mcp"' in source
    assert '"scripts/run_isolated_integration.py", "integration"' in source
    assert '"scripts/run_mcp_test_partitions.py", "all"' in source
    assert "expected == 19" in source
