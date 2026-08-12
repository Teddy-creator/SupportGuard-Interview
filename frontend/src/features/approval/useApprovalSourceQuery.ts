import { useCallback, useEffect, useRef, useState } from "react";

import { api, errorMessage } from "../../productApi";
import type { ApprovalSource } from "../../productTypes";
import {
  mergeSourcePages,
  validateInitialSource,
  validateOlderSource,
} from "./sourceProjection";

export function useApprovalSourceQuery({
  approvalId,
  ticketId,
  tenantId,
}: {
  approvalId?: string;
  ticketId?: string;
  tenantId: string;
}) {
  const [source, setSource] = useState<ApprovalSource | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [error, setError] = useState("");
  const requestEpoch = useRef(0);
  const activeScope = useRef({ approvalId, tenantId });

  useEffect(() => {
    requestEpoch.current += 1;
    activeScope.current = { approvalId, tenantId };
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setSource(null);
    setLoading(false);
    setLoadingOlder(false);
    setError("");
  }, [approvalId, tenantId]);

  const openSource = useCallback(async () => {
    if (!approvalId || !ticketId) return;
    const requestApprovalId = approvalId;
    const requestTenantId = tenantId;
    const epoch = ++requestEpoch.current;
    setSource(null);
    setError("");
    setLoading(true);
    try {
      const projection = await api<ApprovalSource>(
        `/approvals/${encodeURIComponent(requestApprovalId)}/source`,
      );
      if (
        requestEpoch.current !== epoch ||
        activeScope.current.approvalId !== requestApprovalId ||
        activeScope.current.tenantId !== requestTenantId
      )
        return;
      const validated = validateInitialSource(
        projection,
        requestApprovalId,
        ticketId,
      );
      if (!validated) {
        setError("来源记录与当前审批不一致，已阻止显示。请刷新后重试。");
        return;
      }
      setSource(validated);
    } catch (value) {
      if (requestEpoch.current === epoch) setError(errorMessage(value));
    } finally {
      if (
        requestEpoch.current === epoch &&
        activeScope.current.approvalId === requestApprovalId &&
        activeScope.current.tenantId === requestTenantId
      )
        setLoading(false);
    }
  }, [approvalId, tenantId, ticketId]);

  const loadOlder = useCallback(async () => {
    if (
      !approvalId ||
      !ticketId ||
      !source ||
      loadingOlder ||
      !source.has_more ||
      source.next_before_sequence === null ||
      source.next_before_message_id === null
    )
      return;
    const currentSource = source;
    const originTurnId = currentSource.origin_turn_id;
    const beforeSequence = currentSource.next_before_sequence!;
    const beforeMessageId = currentSource.next_before_message_id!;
    const requestApprovalId = approvalId;
    const requestTenantId = tenantId;
    const epoch = ++requestEpoch.current;
    setError("");
    setLoadingOlder(true);
    try {
      const params = new URLSearchParams({
        before_sequence: String(beforeSequence),
        before_message_id: beforeMessageId,
        limit: "100",
      });
      const projection = await api<ApprovalSource>(
        `/approvals/${encodeURIComponent(requestApprovalId)}/source?${params}`,
      );
      if (
        requestEpoch.current !== epoch ||
        activeScope.current.approvalId !== requestApprovalId ||
        activeScope.current.tenantId !== requestTenantId
      )
        return;
      const validated = validateOlderSource(
        projection,
        requestApprovalId,
        ticketId,
        originTurnId,
        beforeSequence,
        beforeMessageId,
      );
      if (!validated) {
        setError("更早的来源记录未通过绑定或游标校验，已保留当前安全窗口。");
        return;
      }
      const merged = mergeSourcePages(currentSource, validated);
      if (!merged) {
        setError("更早的来源记录与当前窗口冲突，已保留当前安全窗口。");
        return;
      }
      setSource(merged);
    } catch (value) {
      if (requestEpoch.current === epoch) setError(errorMessage(value));
    } finally {
      if (
        requestEpoch.current === epoch &&
        activeScope.current.approvalId === requestApprovalId &&
        activeScope.current.tenantId === requestTenantId
      )
        setLoadingOlder(false);
    }
  }, [approvalId, loadingOlder, source, tenantId, ticketId]);

  const cancel = useCallback(() => {
    requestEpoch.current += 1;
    setLoading(false);
    setLoadingOlder(false);
  }, []);

  return {
    source,
    loading,
    loadingOlder,
    error,
    openSource,
    loadOlder,
    cancel,
  };
}
