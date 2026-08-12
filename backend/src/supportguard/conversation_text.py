from __future__ import annotations

import re

_SURROUNDING_PUNCTUATION = re.compile(
    r"^[\s,.!?;:'\"，。！？；：、~～·]+|[\s,.!?;:'\"，。！？；：、~～·]+$"
)
_STANDALONE_GREETINGS = frozenset(
    {
        "hello",
        "hello there",
        "hi",
        "hey",
        "good morning",
        "good afternoon",
        "good evening",
        "你好",
        "您好",
        "嗨",
        "哈喽",
        "早上好",
        "下午好",
        "晚上好",
    }
)


def normalized_conversation_text(message: str) -> str:
    """Normalize bounded user copy without changing its semantic content."""

    return " ".join(message.split())


def is_standalone_greeting(message: str) -> bool:
    """Recognize a bounded social opener without swallowing a support request."""

    normalized = normalized_conversation_text(message).casefold()
    normalized = _SURROUNDING_PUNCTUATION.sub("", normalized)
    return len(normalized) <= 32 and normalized in _STANDALONE_GREETINGS


def conversation_title(message: str, *, fallback: str = "未命名对话") -> str:
    """Return the stable, redacted display title used by product projections."""

    normalized = normalized_conversation_text(message)
    return normalized[:80] if normalized else fallback
