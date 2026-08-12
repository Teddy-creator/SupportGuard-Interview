import re
from dataclasses import dataclass

_EXACT = re.compile(
    r"(?:\b(?:401|429|500)\b|\b[A-Za-z][A-Za-z0-9_-]*(?:-[A-Za-z0-9_-]+)+\b|"
    r"/v\d+/[A-Za-z0-9_./-]+|\b(?:bill|acct|req)_[A-Za-z0-9_-]+\b)",
    re.IGNORECASE,
)
_WHITESPACE = re.compile(r"\s+")
_RUNTIME_RESOURCE_ID = re.compile(
    r"\b(?:acct|bill|key|req|sub)_[0-9a-f]{32}\b",
    re.IGNORECASE,
)
_TERMS = {
    "限流": "频率限制",
    "并发数": "并发限制",
    "密钥": "API Key",
    "接口": "API",
}


@dataclass(frozen=True)
class NormalizedQuery:
    original: str
    normalized: str
    exact_tokens: tuple[str, ...]


def normalize_query(query: str) -> NormalizedQuery:
    compact = _WHITESPACE.sub(" ", query.strip())
    # Customer-scoped UUID-shaped references are useful to business read tools,
    # but they have no semantic value in a shared policy corpus. Excluding only
    # this strict runtime shape prevents a random resource identity from
    # perturbing embeddings or keyword ranking while preserving the original
    # query for audit and every human-authored business term.
    semantic = _WHITESPACE.sub(" ", _RUNTIME_RESOURCE_ID.sub(" ", compact)).strip()
    exact = tuple(dict.fromkeys(match.group(0) for match in _EXACT.finditer(semantic)))
    normalized = semantic
    for source, target in _TERMS.items():
        normalized = normalized.replace(source, target)
    return NormalizedQuery(original=query, normalized=normalized, exact_tokens=exact)
