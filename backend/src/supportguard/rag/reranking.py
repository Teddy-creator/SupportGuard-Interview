from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from supportguard.rag.types import RankedChunk


class Reranker(Protocol):
    def rerank(self, query: str, candidates: Sequence[RankedChunk]) -> list[RankedChunk]: ...


class CrossEncoderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    adapter_schema: Literal["cross-encoder-adapter.v1"] = "cross-encoder-adapter.v1"
    enabled: Literal[False] = False
    model_name: None = None
    execution: Literal["not_implemented"] = "not_implemented"


class CrossEncoderTrace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trace_schema: Literal["cross-encoder-trace.v1"] = "cross-encoder-trace.v1"
    enabled: Literal[False] = False
    executed: Literal[False] = False
    candidate_count: int
    reason: Literal["disabled_pending_valid_dev"] = "disabled_pending_valid_dev"


class CrossEncoderReranker:
    """Default-off contract only; Phase 3 must not load or run a model."""

    def __init__(self, config: CrossEncoderConfig | None = None) -> None:
        self.config = config or CrossEncoderConfig()

    def trace(self, candidates: Sequence[RankedChunk]) -> CrossEncoderTrace:
        return CrossEncoderTrace(candidate_count=len(candidates))

    def rerank(self, query: str, candidates: Sequence[RankedChunk]) -> list[RankedChunk]:
        del query, candidates
        raise RuntimeError("cross_encoder_disabled_pending_valid_dev")
