import pytest

from supportguard.contracts.event_channels import ticket_event_channel


def test_ticket_event_channels_are_tenant_and_ticket_isolated() -> None:
    first = ticket_event_channel("tenant_a", "ticket_1")

    assert first == ticket_event_channel("tenant_a", "ticket_1")
    assert first != ticket_event_channel("tenant_a", "ticket_2")
    assert first != ticket_event_channel("tenant_b", "ticket_1")
    assert first.startswith("supportguard:ticket-events:v2:")
    assert "tenant_a" not in first
    assert "ticket_1" not in first


@pytest.mark.parametrize("tenant_id,ticket_id", [("", "ticket"), ("tenant", "")])
def test_ticket_event_channel_requires_both_scope_dimensions(
    tenant_id: str,
    ticket_id: str,
) -> None:
    with pytest.raises(ValueError, match="ticket_event_channel_scope_required"):
        ticket_event_channel(tenant_id, ticket_id)
