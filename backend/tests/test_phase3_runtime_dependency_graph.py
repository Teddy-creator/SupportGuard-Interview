from supportguard.contracts.errors import RuntimeConflict as ContractRuntimeConflict
from supportguard.services.commands import activate_next_turn as commands_activate_next_turn
from supportguard.services.runtime_jobs import RuntimeConflict as RuntimeJobsConflict
from supportguard.services.turn_activation import activate_next_turn


def test_runtime_compatibility_exports_preserve_canonical_identity() -> None:
    assert RuntimeJobsConflict is ContractRuntimeConflict
    assert commands_activate_next_turn is activate_next_turn


def test_runtime_conflict_preserves_stable_code() -> None:
    conflict = ContractRuntimeConflict("ticket_fifo_blocked")

    assert str(conflict) == "ticket_fifo_blocked"
    assert conflict.code == "ticket_fifo_blocked"
