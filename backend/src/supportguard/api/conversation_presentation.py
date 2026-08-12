from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from supportguard.conversation_text import (
    conversation_title,
    is_standalone_greeting,
)

_TERMINAL_ACTIVITY_LABELS = frozenset(
    {
        "已回答",
        "已给出有限结论",
        "需要补充信息",
        "请求未执行",
        "本轮未完成",
    }
)


def _customer_messages(turns: Iterable[object]) -> Iterable[str]:
    for raw_turn in turns:
        if not isinstance(raw_turn, Mapping):
            continue
        raw_messages = raw_turn.get("messages")
        if not isinstance(raw_messages, list):
            continue
        for raw_message in raw_messages:
            if (
                isinstance(raw_message, Mapping)
                and raw_message.get("role") == "customer"
                and isinstance(raw_message.get("content"), str)
            ):
                yield str(raw_message["content"])


def display_conversation_title(
    stored_title: object,
    *,
    turns: Iterable[object] = (),
) -> str:
    """Replace a greeting-only title with the first visible support question."""

    current = str(stored_title) if isinstance(stored_title, str) else ""
    if current and not is_standalone_greeting(current):
        return conversation_title(current)
    for candidate in _customer_messages(turns):
        if not is_standalone_greeting(candidate):
            return conversation_title(candidate)
    return conversation_title(current)


def apply_conversation_detail_presentation(value: dict[str, Any]) -> dict[str, Any]:
    """Apply title and completed-action copy without changing domain state."""

    turns = value.get("turns")
    projected_turns = turns if isinstance(turns, list) else []
    value["title"] = display_conversation_title(value.get("title"), turns=projected_turns)

    latest_turn = max(
        (item for item in projected_turns if isinstance(item, dict)),
        key=lambda item: (int(item.get("ordinal") or 0), str(item.get("id") or "")),
        default=None,
    )
    if latest_turn is None or value.get("activity_label") not in _TERMINAL_ACTIVITY_LABELS:
        return value
    pending_actions = value.get("pending_actions")
    if not isinstance(pending_actions, list):
        return value
    if any(
        isinstance(item, dict)
        and item.get("status") == "executed"
        and item.get("turn_id") == latest_turn.get("id")
        for item in pending_actions
    ):
        value["activity_label"] = "操作已完成"
    return value
