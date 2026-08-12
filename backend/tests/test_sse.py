from datetime import UTC, datetime

from supportguard.agent.persistence import GENESIS_EVENT_HASH
from supportguard.api.sse import encode_event
from supportguard.db.models import AgentEvent


def test_sse_uses_ticket_sequence_as_replay_cursor() -> None:
    event = AgentEvent(
        id="event_sse",
        tenant_id="tenant_demo",
        run_id="run_sse",
        ticket_id="ticket_sse",
        customer_id="cust_demo",
        sequence=3,
        ticket_sequence=7,
        run_sequence=3,
        step_index=2,
        event_type="tool_observation",
        visibility="customer",
        payload={"status": "ok"},
        parent_event_hash=GENESIS_EVENT_HASH,
        event_hash="a" * 64,
        correlation_id="run_sse",
        causation_id=None,
        created_at=datetime(2026, 7, 12, tzinfo=UTC),
    )
    encoded = encode_event(event)
    assert encoded.startswith("id: 7\nevent: tool_observation\n")
    assert '"ticket_sequence":7' in encoded
    assert '"run_sequence":3' in encoded


def test_sse_encodes_typed_capability_event() -> None:
    encoded = encode_event(
        {
            "ticket_sequence": 9,
            "run_id": "run_capability",
            "run_sequence": 4,
            "event_type": "final_outcome",
            "status": "completed",
            "payload": {"outcome": "resolved"},
            "created_at": "2026-07-15T08:00:00+00:00",
        }
    )
    assert encoded.startswith("id: 9\nevent: final_outcome\n")
    assert '"status":"completed"' in encoded
    assert '"outcome":"resolved"' in encoded


def test_sse_discards_raw_event_payload_and_only_keeps_bounded_refresh_metadata() -> None:
    encoded = encode_event(
        {
            "ticket_sequence": 10,
            "run_id": "run_safe_sse",
            "run_sequence": 5,
            "event_type": "tool_observation",
            "status": "completed",
            "payload": {
                "tool_name": "search_knowledge",
                "source_count": 2,
                "data": {"secret": "Bearer must-not-leak"},
                "error_code": "provider_raw_exception",
                "outcome": "failed:PrivateProviderException",
                "raw_payload": "<html>502 private upstream</html>",
                "prompt": "private prompt",
            },
            "created_at": "2026-07-28T12:00:00+00:00",
        }
    )

    assert '"payload":{"tool_name":"search_knowledge","source_count":2}' in encoded
    for poison in (
        "Bearer must-not-leak",
        "provider_raw_exception",
        "PrivateProviderException",
        "<html>502 private upstream</html>",
        "private prompt",
    ):
        assert poison not in encoded
