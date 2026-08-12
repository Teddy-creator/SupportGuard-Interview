from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from current_predicate_facts import record_predicate_operands
from supportguard.rag.citations import CitationPublicationConflict, CitationPublicationValidator
from supportguard.rag.temporal import (
    TEMPORAL_SELECTOR_ADAPTER,
    AsOfSelector,
    CurrentSelector,
    VersionAsOfSelector,
    VersionSelector,
    build_temporal_selector,
)
from supportguard.rag.types import RetrievalFilter, RetrievalScopeSnapshot

TRACE_TIME = datetime(2026, 7, 14, 8, 9, 10, tzinfo=UTC)
HISTORICAL_TIME = datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC)
PUBLICATION_TIME = datetime(2026, 7, 15, tzinfo=UTC)


def _filter(*, intent: str, selector: object, version: str | None = None) -> RetrievalFilter:
    return RetrievalFilter.model_validate(
        {
            "intent": intent,
            "statuses": ["active"] if intent == "current" else ["active", "deprecated"],
            "version": version,
            "minimum_authority": 50,
            "plan": "pro",
            "region": None,
            "effective_at": HISTORICAL_TIME if intent == "historical" else TRACE_TIME,
            "logical_time": TRACE_TIME,
            "index_version": "index-v1",
            "corpus_snapshot_id": "ingest-v1",
            "scope_snapshot": RetrievalScopeSnapshot(
                tenant_id="tenant",
                customer_id="customer",
                subscription_id="subscription",
                subscription_version=1,
                plan="pro",
            ),
            "eligibility_policy_version": "evidence-eligibility.v1",
            "pipeline_contract_hash": "0" * 64,
            "temporal_selector": selector,
        }
    )


def test_four_temporal_selector_modes_have_exact_claim_time_semantics() -> None:
    current = build_temporal_selector(
        trace_logical_time=TRACE_TIME,
        historical_version=None,
        explicit_as_of=None,
    )
    as_of = build_temporal_selector(
        trace_logical_time=TRACE_TIME,
        historical_version=None,
        explicit_as_of=HISTORICAL_TIME,
    )
    version = build_temporal_selector(
        trace_logical_time=TRACE_TIME,
        historical_version="v1.0",
        explicit_as_of=None,
    )
    version_as_of = build_temporal_selector(
        trace_logical_time=TRACE_TIME,
        historical_version="1.0",
        explicit_as_of=HISTORICAL_TIME,
    )
    assert current == CurrentSelector(claim_effective_time=TRACE_TIME)
    assert as_of == AsOfSelector(
        explicit_as_of=HISTORICAL_TIME,
        claim_effective_time=HISTORICAL_TIME,
    )
    assert version == VersionSelector(historical_version="1.0")
    assert version_as_of == VersionAsOfSelector(
        historical_version="1.0",
        explicit_as_of=HISTORICAL_TIME,
        claim_effective_time=HISTORICAL_TIME,
    )
    operands = {
        "modes": [current.mode, as_of.mode, version.mode, version_as_of.mode],
        "current_claim_time": current.claim_effective_time.isoformat(),
        "as_of_claim_time": as_of.claim_effective_time.isoformat(),
        "as_of_explicit_time": as_of.explicit_as_of.isoformat(),
        "version_value": version.historical_version,
        "version_claim_time_is_none": version.claim_effective_time is None,
        "version_as_of_value": version_as_of.historical_version,
        "version_as_of_claim_time": version_as_of.claim_effective_time.isoformat(),
        "version_as_of_explicit_time": version_as_of.explicit_as_of.isoformat(),
        "distinct_time_axis_count": len(
            {TRACE_TIME.isoformat(), HISTORICAL_TIME.isoformat(), PUBLICATION_TIME.isoformat()}
        ),
    }
    for predicate_id in (
        "temporal_selector_four_modes_exact",
        "historical_version_exact",
        "version_date_consistent",
    ):
        record_predicate_operands(
            requirement_id="C6-P0-13",
            predicate_id=predicate_id,
            subject_kind="temporal_selector_contract",
            operands=operands,
        )
    record_predicate_operands(
        requirement_id="C6-P0-12",
        predicate_id="three_time_axes_separate",
        subject_kind="temporal_selector_contract",
        operands=operands,
    )


def test_temporal_selector_rejects_extra_or_inconsistent_fields() -> None:
    with pytest.raises(ValidationError):
        TEMPORAL_SELECTOR_ADAPTER.validate_python(
            {
                "mode": "current",
                "claim_effective_time": TRACE_TIME,
                "historical_version": "1.0",
            }
        )
    with pytest.raises(ValidationError, match="must equal explicit_as_of"):
        AsOfSelector(
            explicit_as_of=HISTORICAL_TIME,
            claim_effective_time=TRACE_TIME,
        )


@pytest.mark.parametrize(
    ("group", "filters", "expected_statuses", "expected_time"),
    [
        (
            "current",
            _filter(intent="current", selector=CurrentSelector(claim_effective_time=TRACE_TIME)),
            {"active"},
            PUBLICATION_TIME,
        ),
        (
            "historical",
            _filter(
                intent="historical",
                selector=AsOfSelector(
                    explicit_as_of=HISTORICAL_TIME,
                    claim_effective_time=HISTORICAL_TIME,
                ),
            ),
            {"active", "deprecated"},
            HISTORICAL_TIME,
        ),
        (
            "historical",
            _filter(
                intent="historical",
                selector=VersionSelector(historical_version="1.0"),
                version="1.0",
            ),
            {"active", "deprecated"},
            None,
        ),
        (
            "historical",
            _filter(
                intent="historical",
                selector=VersionAsOfSelector(
                    historical_version="1.0",
                    explicit_as_of=HISTORICAL_TIME,
                    claim_effective_time=HISTORICAL_TIME,
                ),
                version="1.0",
            ),
            {"active", "deprecated"},
            HISTORICAL_TIME,
        ),
    ],
)
def test_publication_uses_each_selector_time_axis(
    group: str,
    filters: RetrievalFilter,
    expected_statuses: set[str],
    expected_time: datetime | None,
) -> None:
    statuses, claim_time = CitationPublicationValidator._publication_temporal_contract(
        group=group,
        filters=filters,
        trace_logical_time=TRACE_TIME,
        publication_checked_at=PUBLICATION_TIME,
    )
    assert statuses == expected_statuses
    assert claim_time == expected_time


def test_publication_rejects_graph_group_that_disagrees_with_selector() -> None:
    filters = _filter(intent="current", selector=CurrentSelector(claim_effective_time=TRACE_TIME))
    with pytest.raises(CitationPublicationConflict, match="temporal_selector_invalid"):
        CitationPublicationValidator._publication_temporal_contract(
            group="historical",
            filters=filters,
            trace_logical_time=TRACE_TIME,
            publication_checked_at=PUBLICATION_TIME,
        )
