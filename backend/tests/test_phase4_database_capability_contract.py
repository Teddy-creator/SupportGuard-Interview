from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from supportguard.db.interview_baseline import (
    CURRENT_BASELINE_MANIFEST_SHA256,
    I200_BASELINE_MANIFEST_SHA256,
    I201_BASELINE_MANIFEST_SHA256,
)
from supportguard.db.reference_contract import CURRENT_PRODUCT_DATABASE_HEAD
from supportguard.db.role_contract import (
    FUNCTION_GRANTS,
    INTERVIEW_RETIRED_FUNCTION_GRANTS,
    V126_RETIRED_FUNCTION_GRANTS,
    expected_function_grants,
)
from supportguard.db.security_contract import (
    BASELINE_IDENTITY,
    CURRENT_INTERVIEW_DATABASE_REVISION,
    INTERVIEW_BASELINE_ROOT_REVISION,
    INTERVIEW_ESCALATION_RETIREMENT_REVISION,
    PHASE4_ESCALATION_RETIREMENT_CATALOG_DELTA,
    PHASE7_REFUND_FENCE_CATALOG_DELTA,
)


def _migration(filename: str) -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "alembic_baseline" / "versions" / filename
    spec = importlib.util.spec_from_file_location(f"migration_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_i200_and_i201_artifacts_remain_immutable_while_i202_is_current() -> None:
    assert BASELINE_IDENTITY.revision == INTERVIEW_BASELINE_ROOT_REVISION
    assert INTERVIEW_BASELINE_ROOT_REVISION == "i200_baseline_0001"
    assert CURRENT_PRODUCT_DATABASE_HEAD == CURRENT_INTERVIEW_DATABASE_REVISION
    assert INTERVIEW_ESCALATION_RETIREMENT_REVISION == "i201_retire_escalation"
    assert CURRENT_INTERVIEW_DATABASE_REVISION == "i202_refund_fence_authority"
    assert I200_BASELINE_MANIFEST_SHA256 == (
        "9007f1da6c8e85dcfb03d2ebc7d2e6aa2397882160f1d7df8d148dd7648bfe80"
    )
    assert CURRENT_BASELINE_MANIFEST_SHA256 == (
        "17cf263a4899241399deed8acfd678b06531f40192372ebc52e6547e851464d7"
    )
    assert I201_BASELINE_MANIFEST_SHA256 == (
        "9d4faabe4e2705803aea84316aabca8467f357a164560fa7123de23afae82e49"
    )
    assert CURRENT_BASELINE_MANIFEST_SHA256 != I200_BASELINE_MANIFEST_SHA256


def test_i201_is_forward_only_and_removes_both_escalation_entry_paths() -> None:
    migration = _migration("i201_retire_escalation.py")
    assert migration.revision == INTERVIEW_ESCALATION_RETIREMENT_REVISION
    assert migration.down_revision == INTERVIEW_BASELINE_ROOT_REVISION
    sql = migration._RESTRICT_ACTION_CAPABILITIES_SQL
    assert "'propose_refund'" in sql
    assert "'propose_api_key_revocation'" in sql
    assert "'propose_entitlement_change'" in sql
    assert "create_support_escalation" not in sql
    with pytest.raises(RuntimeError, match="downgrade_forbidden"):
        migration.downgrade()


def test_i202_uses_fenced_runtime_identity_not_ticket_projection_status() -> None:
    migration = _migration("i202_refund_fence_authority.py")
    assert migration.revision == CURRENT_INTERVIEW_DATABASE_REVISION
    assert migration.down_revision == INTERVIEW_ESCALATION_RETIREMENT_REVISION
    sql = migration._AUTHORIZE_REFUND_BY_FENCE_SQL
    assert "r.active_job_id=j.id" in sql
    assert "r.active_fencing_token=j.fencing_token" in sql
    assert "i.status='executing'" in sql
    assert "supportguard_action_observation_bound" in sql
    assert "v_ticket.status NOT IN" not in sql
    with pytest.raises(RuntimeError, match="downgrade_forbidden"):
        migration.downgrade()


def test_current_grants_retire_escalation_without_rewriting_historical_denominator() -> None:
    historical = {item.signature for item in FUNCTION_GRANTS}
    v126_retired = {item.signature for item in V126_RETIRED_FUNCTION_GRANTS}
    phase4_retired = {item.signature for item in INTERVIEW_RETIRED_FUNCTION_GRANTS}
    current = expected_function_grants()
    escalation = "supportguard_action_mcp_create_support_escalation(jsonb,jsonb)"
    assert len(historical) == 62
    assert len(current) == 60
    assert phase4_retired == {escalation}
    assert escalation in historical
    assert escalation not in current
    assert set(current) == historical - v126_retired - phase4_retired


def test_i201_catalog_delta_is_exactly_generic_definition_and_direct_acl() -> None:
    rows = PHASE4_ESCALATION_RETIREMENT_CATALOG_DELTA
    assert len(rows) == 2
    assert {(item.category, item.change) for item in rows} == {
        ("function", "definition"),
        ("function", "acl"),
    }
    assert {item.identity for item in rows} == {
        (
            "public.supportguard_action_mcp_create_support_escalation("
            "p_model_arguments jsonb, p_trusted_context jsonb)"
        ),
        (
            "public.supportguard_action_mcp_execute("
            "p_capability_name text, p_model_arguments jsonb, p_trusted_context jsonb)"
        ),
    }


def test_i202_catalog_delta_is_one_exact_function_definition() -> None:
    rows = PHASE7_REFUND_FENCE_CATALOG_DELTA
    assert len(rows) == 1
    assert rows[0].category == "function"
    assert rows[0].change == "definition"
    assert rows[0].identity == (
        "public.supportguard_action_mcp_execute("
        "p_capability_name text, p_model_arguments jsonb, p_trusted_context jsonb)"
    )
