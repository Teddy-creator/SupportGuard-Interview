from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.sql import Select

from supportguard.api.projections import _published_knowledge_sources


class _ScalarRows:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def all(self) -> list[object]:
        return self._rows


class _CitationSession:
    def __init__(self, *, canonical_message_id: str | None) -> None:
        claim = SimpleNamespace(
            id="claim_1",
            claim_text="当前账户状态正常。",
            support_refs={
                "citation_binding_ids": [],
                "observation_source_ids": ["account:cust_demo"],
            },
        )
        observation = SimpleNamespace(
            id="observation_1",
            payload={
                "tool_name": "query_account",
                "status": "ok",
                "observed_at": "2026-07-28T10:00:00+00:00",
                "freshness_status": "fresh",
                "source_refs": [{"source_id": "account:cust_demo"}],
                "data": {"status": "active", "security_status": "normal"},
            },
        )
        self._scalar_rows = iter(
            (
                _ScalarRows([claim]),
                _ScalarRows([observation]),
            )
        )
        self.canonical_message_id = canonical_message_id
        self.message_query_parameters: dict[str, Any] | None = None

    async def scalars(self, _statement: Select[Any]) -> _ScalarRows:
        return next(self._scalar_rows)

    async def scalar(self, statement: Select[Any]) -> str | None:
        compiled = statement.compile()
        self.message_query_parameters = dict(compiled.params)
        return self.canonical_message_id


@pytest.mark.asyncio
async def test_citations_bind_only_to_the_canonical_answer_publication() -> None:
    session = _CitationSession(canonical_message_id="msg_answer")

    projected = await _published_knowledge_sources(  # type: ignore[arg-type]
        session,
        "run_exact",
    )

    assert session.message_query_parameters is not None
    assert "assistant:run_exact" in session.message_query_parameters.values()
    assert len(projected) == 1
    assert projected[0]["message_id"] == "msg_answer"
    assert projected[0]["source_type"] == "business_fact"


@pytest.mark.asyncio
async def test_citations_do_not_fall_through_to_runtime_failure_assistant_message() -> None:
    session = _CitationSession(canonical_message_id=None)

    projected = await _published_knowledge_sources(  # type: ignore[arg-type]
        session,
        "run_failed",
    )

    assert session.message_query_parameters is not None
    assert "assistant:run_failed" in session.message_query_parameters.values()
    assert projected == []
