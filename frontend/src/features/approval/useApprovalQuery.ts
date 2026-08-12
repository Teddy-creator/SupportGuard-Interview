import { useCallback, useEffect, useRef, useState } from "react";

import { api, errorMessage, isAbortError } from "../../productApi";
import type { Approval, ApprovalDetail } from "../../productTypes";

export type ApprovalListState = "loading" | "ready" | "error";

export function useApprovalQuery({
  selected,
  tenantId,
  editingDecision,
}: {
  selected?: string;
  tenantId: string;
  editingDecision: boolean;
}) {
  const [items, setItems] = useState<Approval[]>([]);
  const [listState, setListState] = useState<ApprovalListState>("loading");
  const [detail, setDetail] = useState<ApprovalDetail | null>(null);
  const [listError, setListError] = useState("");
  const [detailError, setDetailError] = useState("");
  const [listRefreshEpoch, setListRefreshEpoch] = useState(0);
  const scopeRequest = useRef<AbortController | null>(null);
  const scopeEpoch = useRef(0);
  const detailRefreshKey = useRef("");
  const activeScope = useRef({ tenantId, approvalId: selected });

  useEffect(() => {
    scopeEpoch.current += 1;
    activeScope.current = { tenantId, approvalId: selected };
  }, [selected, tenantId]);

  const loadItems = useCallback(
    async (signal?: AbortSignal) => {
      const requestTenantId = tenantId;
      const requestScopeEpoch = scopeEpoch.current;
      const nextItems = await api<Approval[]>("/approvals", { signal });
      if (
        scopeEpoch.current !== requestScopeEpoch ||
        activeScope.current.tenantId !== requestTenantId
      )
        return;
      setItems(nextItems);
      setListState("ready");
      setListError("");
    },
    [tenantId],
  );

  const loadDetail = useCallback(
    async (approvalId: string, signal?: AbortSignal) => {
      const requestTenantId = tenantId;
      const requestScopeEpoch = scopeEpoch.current;
      const nextDetail = await api<ApprovalDetail>(
        `/approvals/${encodeURIComponent(approvalId)}`,
        { signal },
      );
      if (
        scopeEpoch.current !== requestScopeEpoch ||
        activeScope.current.tenantId !== requestTenantId ||
        activeScope.current.approvalId !== approvalId
      )
        return;
      setDetail(nextDetail);
      setDetailError("");
    },
    [tenantId],
  );

  useEffect(() => {
    const controller = new AbortController();
    scopeRequest.current = controller;
    let inFlight = false;
    const refresh = async () => {
      if (inFlight || controller.signal.aborted) return;
      const requestTenantId = tenantId;
      inFlight = true;
      try {
        await loadItems(controller.signal);
      } catch (value) {
        if (
          !isAbortError(value) &&
          activeScope.current.tenantId === requestTenantId
        ) {
          setListState("error");
          setListError(errorMessage(value));
        }
      } finally {
        inFlight = false;
      }
    };
    void refresh();
    const interval = window.setInterval(() => void refresh(), 1000);
    return () => {
      controller.abort();
      window.clearInterval(interval);
      if (scopeRequest.current === controller) scopeRequest.current = null;
    };
  }, [listRefreshEpoch, loadItems, tenantId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setDetail(null);
    setDetailError("");
    detailRefreshKey.current = "";
    if (!selected) return;
    const approvalId = selected;
    const requestTenantId = tenantId;
    const controller = new AbortController();
    void loadDetail(approvalId, controller.signal).catch((value) => {
      if (
        !isAbortError(value) &&
        activeScope.current.tenantId === requestTenantId &&
        activeScope.current.approvalId === approvalId
      )
        setDetailError(errorMessage(value));
    });
    return () => controller.abort();
  }, [loadDetail, selected, tenantId]);

  useEffect(() => {
    if (!selected || !detail || editingDecision) return;
    const summary = items.find((item) => item.id === selected);
    if (!summary) return;
    const refreshRequired =
      summary.status !== detail.status ||
      summary.actionable !== detail.actionable ||
      summary.allowed_actions.join(",") !== detail.allowed_actions.join(",");
    if (!refreshRequired) return;
    const refreshKey = [
      tenantId,
      selected,
      summary.status,
      String(summary.actionable),
      summary.allowed_actions.join(","),
    ].join(":");
    if (detailRefreshKey.current === refreshKey) return;
    detailRefreshKey.current = refreshKey;
    const controller = new AbortController();
    void loadDetail(selected, controller.signal).catch((value) => {
      if (!isAbortError(value)) setDetailError(errorMessage(value));
    });
    return () => controller.abort();
  }, [detail, editingDecision, items, loadDetail, selected, tenantId]);

  const retryList = useCallback(() => {
    setListError("");
    setListState("loading");
    setListRefreshEpoch((value) => value + 1);
  }, []);

  const clearScope = useCallback(() => {
    scopeEpoch.current += 1;
    scopeRequest.current?.abort();
    setItems([]);
    setListState("loading");
    setDetail(null);
    setListError("");
    setDetailError("");
  }, []);

  return {
    items,
    listState,
    detail,
    listError,
    detailError,
    setListError,
    setDetailError,
    loadItems,
    loadDetail,
    retryList,
    clearScope,
  };
}
