from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    CUSTOMER_NOT_FOUND = "customer_not_found"
    SUBSCRIPTION_NOT_FOUND = "subscription_not_found"
    USAGE_NOT_FOUND = "usage_not_found"
    TICKET_NOT_FOUND = "ticket_not_found"
    TICKET_SCOPE_VIOLATION = "ticket_scope_violation"
    TICKET_STATE_CONFLICT = "ticket_state_conflict"
    BILLING_RECORD_NOT_FOUND = "billing_record_not_found"
    BILLING_SCOPE_VIOLATION = "billing_scope_violation"
    BILLING_NOT_CHARGED = "billing_not_charged"
    NOT_DUPLICATE_CHARGE = "not_duplicate_charge"
    REFUND_LIMIT_EXCEEDED = "refund_limit_exceeded"
    APPROVAL_NOT_FOUND = "approval_not_found"
    APPROVAL_STATE_CONFLICT = "approval_state_conflict"
    APPROVAL_SNAPSHOT_MISMATCH = "approval_snapshot_mismatch"
    APPROVAL_BINDING_INVALID = "approval_binding_invalid"
    CHECKPOINT_NOT_INTERRUPTED = "checkpoint_not_interrupted"
    APPROVAL_STALE = "approval_stale"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"


class DomainError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def observation_status_for_error(code: ErrorCode) -> str:
    if code in {
        ErrorCode.BILLING_SCOPE_VIOLATION,
        ErrorCode.TICKET_SCOPE_VIOLATION,
    }:
        return "denied"
    if code in {
        ErrorCode.CUSTOMER_NOT_FOUND,
        ErrorCode.SUBSCRIPTION_NOT_FOUND,
        ErrorCode.USAGE_NOT_FOUND,
        ErrorCode.TICKET_NOT_FOUND,
        ErrorCode.BILLING_RECORD_NOT_FOUND,
    }:
        return "not_found"
    if code in {
        ErrorCode.TICKET_STATE_CONFLICT,
        ErrorCode.APPROVAL_STATE_CONFLICT,
        ErrorCode.APPROVAL_SNAPSHOT_MISMATCH,
        ErrorCode.IDEMPOTENCY_CONFLICT,
    }:
        return "conflict"
    return "invalid_input"
