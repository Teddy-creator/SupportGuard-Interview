import type {
  ApprovalDecision,
  ApprovalDetail,
  ApprovalEditableField,
} from "./productTypes";

export function approvalEditableField(
  actionType: string,
): ApprovalEditableField | null {
  if (actionType === "refund") return "refund_reason";
  if (actionType === "entitlement_change") return "target_concurrency";
  return null;
}

export function approvalDecisionAllowed(
  detail: ApprovalDetail,
  decision: ApprovalDecision,
): boolean {
  if (decision === "approve")
    return detail.allowed_actions.includes("approve");
  if (decision === "reject")
    return detail.allowed_actions.includes("reject");
  return (
    approvalEditableField(detail.action_type) !== null &&
    detail.allowed_actions.includes("edit_and_approve")
  );
}

export function validTargetConcurrency(value: string): boolean {
  const normalized = value.trim();
  if (!/^[0-9]+$/.test(normalized)) return false;
  const parsed = Number(normalized);
  return (
    Number.isSafeInteger(parsed) &&
    parsed >= 1 &&
    parsed <= 1_000_000
  );
}
