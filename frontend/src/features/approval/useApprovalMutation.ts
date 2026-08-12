import { useCallback, useEffect, useState } from "react";

import {
  approvalDecisionAllowed,
  approvalEditableField,
  validTargetConcurrency,
} from "../../approvalEditing";
import { mutationIdentity } from "../../idempotency";
import {
  api,
  errorMessage,
  isAbortError,
  ProductApiError,
} from "../../productApi";
import type {
  ApprovalDecision,
  ApprovalDetail,
  ApprovalEditChanges,
} from "../../productTypes";
import { useIdempotentMutation } from "../../useIdempotentMutation";

export function useApprovalMutation({
  approvalId,
  tenantId,
  csrf,
}: {
  approvalId?: string;
  tenantId: string;
  csrf: string;
}) {
  const [busy, setBusy] = useState(false);
  const [decision, setDecision] = useState<ApprovalDecision>("approve");
  const [reason, setReason] = useState("");
  const [refundReason, setRefundReason] = useState("");
  const [targetConcurrency, setTargetConcurrency] = useState("");
  const [error, setError] = useState("");
  const [fieldError, setFieldError] = useState("");
  const mutation = useIdempotentMutation();
  const resetMutation = mutation.reset;

  const reset = useCallback(() => {
    setDecision("approve");
    setReason("");
    setRefundReason("");
    setTargetConcurrency("");
    setError("");
    setFieldError("");
    resetMutation();
  }, [resetMutation]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    reset();
  }, [approvalId, reset, tenantId]);

  const selectDecision = useCallback(
    (detail: ApprovalDetail | null, next: ApprovalDecision) => {
      if (!detail || !approvalDecisionAllowed(detail, next)) return;
      if (next === decision) return;
      resetMutation();
      setDecision(next);
      setReason("");
      setRefundReason("");
      setTargetConcurrency("");
      setError("");
      setFieldError("");
    },
    [decision, resetMutation],
  );

  const decide = useCallback(
    async (
      detail: ApprovalDetail | null,
      action: ApprovalDecision,
      refresh: {
        list: () => Promise<void>;
        detail: (approvalId: string) => Promise<void>;
        onListError: (message: string) => void;
        onDetailError: (message: string) => void;
      },
    ) => {
      if (
        !approvalId ||
        !detail ||
        busy ||
        !approvalDecisionAllowed(detail, action)
      )
        return;
      let editChanges: ApprovalEditChanges | null = null;
      if (action === "edit-and-approve") {
        const editableField = approvalEditableField(detail.action_type);
        if (
          editableField === "refund_reason" &&
          refundReason.trim().length >= 5
        )
          editChanges = { refund_reason: refundReason.trim() };
        else if (
          editableField === "target_concurrency" &&
          validTargetConcurrency(targetConcurrency)
        )
          editChanges = {
            target_concurrency: Number(targetConcurrency.trim()),
          };
        else {
          setFieldError(
            editableField === "target_concurrency"
              ? "目标并发必须是 1 到 1000000 之间的整数。"
              : "修改后的退款理由至少需要 5 个字符。",
          );
          return;
        }
      }
      setBusy(true);
      setError("");
      setFieldError("");
      try {
        const body =
          action === "edit-and-approve"
            ? {
                ...(reason.trim() ? { reason: reason.trim() } : {}),
                changes: editChanges,
              }
            : { reason: reason.trim() };
        const identity = mutationIdentity({
          tenantId,
          resource: approvalId,
          operation: action,
          payload: body,
        });
        await mutation.run(identity, (key) =>
          api(
            `/approvals/${encodeURIComponent(approvalId)}/${action}`,
            {
              method: "POST",
              headers: { "Idempotency-Key": key },
              body: JSON.stringify(body),
            },
            csrf,
          ),
        );
        setDecision("approve");
        setReason("");
        setRefundReason("");
        setTargetConcurrency("");
        setFieldError("");
        const [listRefresh, detailRefresh] = await Promise.allSettled([
          refresh.list(),
          refresh.detail(approvalId),
        ]);
        if (
          listRefresh.status === "rejected" &&
          !isAbortError(listRefresh.reason)
        )
          refresh.onListError(errorMessage(listRefresh.reason));
        if (
          detailRefresh.status === "rejected" &&
          !isAbortError(detailRefresh.reason)
        )
          refresh.onDetailError(errorMessage(detailRefresh.reason));
      } catch (value) {
        if (
          action === "edit-and-approve" &&
          value instanceof ProductApiError &&
          value.status === 422
        )
          setFieldError(errorMessage(value));
        else setError(errorMessage(value));
      } finally {
        setBusy(false);
      }
    },
    [
      approvalId,
      busy,
      csrf,
      mutation,
      reason,
      refundReason,
      targetConcurrency,
      tenantId,
    ],
  );

  return {
    busy,
    decision,
    reason,
    refundReason,
    targetConcurrency,
    error,
    fieldError,
    retryable: mutation.retryable,
    editing:
      busy ||
      decision !== "approve" ||
      reason.trim().length > 0 ||
      refundReason.trim().length > 0 ||
      targetConcurrency.trim().length > 0,
    setReason,
    setRefundReason,
    setTargetConcurrency,
    setError,
    setFieldError,
    selectDecision,
    decide,
    reset,
  };
}
