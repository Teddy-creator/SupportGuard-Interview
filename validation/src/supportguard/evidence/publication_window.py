from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _fingerprint(statement: str) -> str:
    normalized = " ".join(statement.lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def _retrieval_kind(statement: str) -> str | None:
    normalized = statement.lower()
    if "<=>" in normalized or "<->" in normalized:
        return "pgvector_candidate_query"
    if "ts_rank" in normalized or "websearch_to_tsquery" in normalized:
        return "fts_candidate_query"
    return None


@dataclass(slots=True)
class PublicationObservationWindow:
    """Runner-owned raw observation window for publication or resume validation."""

    session: AsyncSession
    target_invocation_id: str
    provider_attempt_or_resume_id: str
    runner_nonce: str = field(default_factory=lambda: secrets.token_hex(16))
    collector_nonce: str = field(default_factory=lambda: secrets.token_hex(16))
    raw_events: list[dict[str, Any]] = field(default_factory=list)
    _sequence: int = 0
    _engine: Any = None
    _started_at: str | None = None
    _ended_at: str | None = None

    def _append(self, kind: str, payload: dict[str, Any]) -> None:
        self._sequence += 1
        self.raw_events.append(
            {
                "sequence": self._sequence,
                "kind": kind,
                "captured_at": _now(),
                "target_invocation_id": self.target_invocation_id,
                "provider_attempt_or_resume_id": self.provider_attempt_or_resume_id,
                "runner_nonce": self.runner_nonce,
                "collector_nonce": self.collector_nonce,
                "payload": payload,
            }
        )

    def _before_cursor_execute(
        self,
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        kind = _retrieval_kind(statement)
        if kind is not None:
            self._append(kind, {"statement_fingerprint": _fingerprint(statement)})

    async def __aenter__(self) -> PublicationObservationWindow:
        if self._started_at is not None:
            raise RuntimeError("publication_observation_window_reused")
        bind = self.session.get_bind()
        self._engine = getattr(bind, "sync_engine", bind)
        self._started_at = _now()
        self._append("window_start", {"watermark": 0})
        event.listen(self._engine, "before_cursor_execute", self._before_cursor_execute)
        return self

    async def __aexit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        event.remove(self._engine, "before_cursor_execute", self._before_cursor_execute)
        self._ended_at = _now()
        self._append("window_end", {"watermark": self._sequence})

    def record_external_call(self, kind: str, *, operation: str) -> None:
        if kind not in {"embedding_provider_call", "reranker_call"}:
            raise ValueError("publication_external_call_kind_invalid")
        if self._started_at is None or self._ended_at is not None:
            raise RuntimeError("publication_external_call_outside_window")
        self._append(kind, {"operation": operation})

    def report(self) -> dict[str, Any]:
        if self._started_at is None or self._ended_at is None:
            raise RuntimeError("publication_observation_window_incomplete")
        expected = list(range(1, len(self.raw_events) + 1))
        if [int(item["sequence"]) for item in self.raw_events] != expected:
            raise RuntimeError("publication_observation_sequence_incomplete")
        return {
            "schema_version": "publication-observation.v1",
            "target_invocation_id": self.target_invocation_id,
            "provider_attempt_or_resume_id": self.provider_attempt_or_resume_id,
            "runner_nonce": self.runner_nonce,
            "collector_nonce": self.collector_nonce,
            "window_started_at": self._started_at,
            "window_ended_at": self._ended_at,
            "start_watermark": 1,
            "end_watermark": len(self.raw_events),
            "embedding_provider_call_count": sum(
                event["kind"] == "embedding_provider_call" for event in self.raw_events
            ),
            "retrieval_query_count": sum(
                event["kind"] in {"pgvector_candidate_query", "fts_candidate_query"}
                for event in self.raw_events
            ),
            "reranker_call_count": sum(
                event["kind"] == "reranker_call" for event in self.raw_events
            ),
            "raw_events": list(self.raw_events),
        }
