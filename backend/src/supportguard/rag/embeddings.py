from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    from supportguard.config import Settings


class EmbeddingProvider(Protocol):
    dimensions: int

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def embedding_identity(provider: EmbeddingProvider) -> dict[str, object]:
    return {
        "model": getattr(provider, "model_name", type(provider).__name__),
        "revision": getattr(provider, "revision", "unknown"),
        "tokenizer": getattr(provider, "tokenizer", "unknown"),
        "pooling": getattr(provider, "pooling", "unknown"),
        "normalization": getattr(provider, "normalization", "unknown"),
        "dimensions": provider.dimensions,
        "query_prefix": "query: ",
        "passage_prefix": "passage: ",
        "chunker": "markdown-heading-window-v2",
        "indexed_text_schema": "title+section_path+content.v2",
    }


def embedding_fingerprint(provider: EmbeddingProvider) -> str:
    encoded = json.dumps(
        embedding_identity(provider),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def configured_embedding_identity(
    settings: Settings, *, testing: bool = False
) -> dict[str, object]:
    """Return the query-time contract without loading a model into this process."""

    deterministic = (
        testing
        or settings.app_env == "test"
        or settings.embedding_mode == "deterministic-fixture"
    )
    if settings.app_env == "production" and deterministic:
        raise RuntimeError("production runtime cannot enable deterministic fixture embeddings")
    if deterministic:
        return embedding_identity(DeterministicEmbedding())
    return {
        "model": settings.embedding_model,
        "revision": settings.embedding_revision,
        "tokenizer": "multilingual-e5-small-tokenizer",
        "pooling": "sentence-transformers-mean",
        "normalization": "l2",
        "dimensions": 384,
        "query_prefix": "query: ",
        "passage_prefix": "passage: ",
        "chunker": "markdown-heading-window-v2",
        "indexed_text_schema": "title+section_path+content.v2",
    }


def configured_embedding_fingerprint(settings: Settings, *, testing: bool = False) -> str:
    encoded = json.dumps(
        configured_embedding_identity(settings, testing=testing),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


class DeterministicEmbedding:
    """Offline fixture with stable lexical features; never used in production."""

    dimensions = 384
    model_name = "deterministic-e5-fixture"
    revision = "v1"
    tokenizer = "deterministic-lexical.v1"
    pooling = "hashed-sum"
    normalization = "l2"

    @staticmethod
    def token_count(text: str) -> int:
        return len(re.findall(r"[A-Za-z0-9_./:-]+|[\u4e00-\u9fff]|[^\s]", text)) + 2

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        tokens = re.findall(r"[a-z0-9_./:-]+|[\u4e00-\u9fff]{1,4}", text.lower())
        for token in tokens:
            digest = hashlib.sha256(token.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            vector[index] += 1.0 if digest[4] % 2 else -1.0
        return _normalize(vector)

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(f"passage: {text}") for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(f"query: {text}")


class MCPOnlyEmbedding:
    """Fail-closed placeholder when retrieval is owned by the Read MCP process."""

    dimensions = 384

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        raise RuntimeError("worker main process cannot embed documents; use search_knowledge MCP")

    def embed_query(self, text: str) -> list[float]:
        raise RuntimeError("worker main process cannot embed queries; use search_knowledge MCP")


class E5SmallEmbedding:
    dimensions = 384

    def __init__(
        self,
        model_name: str = "intfloat/multilingual-e5-small",
        revision: str = "614241f622f53c4eeff9890bdc4f31cfecc418b3",
        *,
        local_files_only: bool = False,
    ) -> None:
        from sentence_transformers import SentenceTransformer

        self.model_name = model_name
        self.revision = revision
        self.tokenizer = "multilingual-e5-small-tokenizer"
        self.pooling = "sentence-transformers-mean"
        self.normalization = "l2"
        self._model: Any = SentenceTransformer(
            model_name,
            revision=revision,
            local_files_only=local_files_only,
        )

    def token_count(self, text: str) -> int:
        encoded = self._model.tokenizer(text, add_special_tokens=True, truncation=False)
        return len(encoded["input_ids"])

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        values = self._model.encode(
            [f"passage: {text}" for text in texts], normalize_embeddings=True
        )
        return [cast(list[float], row.tolist()) for row in values]

    def embed_query(self, text: str) -> list[float]:
        value = self._model.encode(f"query: {text}", normalize_embeddings=True)
        return cast(list[float], value.tolist())


def build_embedding_provider(
    settings: Settings, *, testing: bool = False
) -> DeterministicEmbedding | E5SmallEmbedding:
    deterministic = (
        testing
        or settings.app_env == "test"
        or settings.embedding_mode == "deterministic-fixture"
    )
    if settings.app_env == "production" and deterministic:
        raise RuntimeError("production runtime cannot enable deterministic fixture embeddings")
    if deterministic:
        return DeterministicEmbedding()
    return E5SmallEmbedding(
        settings.embedding_model,
        settings.embedding_revision,
        local_files_only=settings.app_env == "production",
    )
