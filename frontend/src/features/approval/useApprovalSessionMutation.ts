import { useCallback, useState } from "react";

import { api, createDemoSession, errorMessage } from "../../productApi";
import type { SessionContext } from "../../productTypes";

export function useApprovalSessionMutation({
  session,
  csrf,
  onSession,
  onNavigate,
  onSessionFailure,
  clearApprovalScope,
  resumeApprovalScope,
}: {
  session: SessionContext;
  csrf: string;
  onSession: (csrf: string, session: SessionContext) => void;
  onNavigate: (path: string, replace?: boolean) => void;
  onSessionFailure: (message: string) => void;
  clearApprovalScope: () => void;
  resumeApprovalScope: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const switchTenant = useCallback(
    async (tenantId: string) => {
      const previous = session.active_tenant.id;
      if (tenantId === previous) return;
      clearApprovalScope();
      setBusy(true);
      setError("");
      let recoveredCsrf = csrf;
      try {
        const nextCsrf = await createDemoSession("approver", {
          tenantId,
          subjectId: session.principal.id,
        });
        recoveredCsrf = nextCsrf;
        const next = await api<SessionContext>("/session");
        onSession(next.csrf_token ?? nextCsrf, next);
        onNavigate("/approvals", true);
      } catch (value) {
        try {
          const truth = await api<SessionContext>("/session");
          onSession(truth.csrf_token ?? recoveredCsrf, truth);
          onNavigate("/approvals", true);
          if (truth.active_tenant.id === previous) resumeApprovalScope();
        } catch {
          onSessionFailure(
            "无法确认切换后的真实租户，已关闭当前工作区以防止跨租户数据混用。",
          );
          return;
        }
        setError(errorMessage(value));
      } finally {
        setBusy(false);
      }
    },
    [
      clearApprovalScope,
      csrf,
      onNavigate,
      onSession,
      onSessionFailure,
      resumeApprovalScope,
      session.active_tenant.id,
      session.principal.id,
    ],
  );

  const switchToCustomer = useCallback(async () => {
    setBusy(true);
    setError("");
    let recoveredCsrf = csrf;
    try {
      const nextCsrf = await createDemoSession("customer");
      recoveredCsrf = nextCsrf;
      const next = await api<SessionContext>("/session");
      onSession(next.csrf_token ?? nextCsrf, next);
      onNavigate("/conversations/new", true);
    } catch (value) {
      try {
        const truth = await api<SessionContext>("/session");
        onSession(truth.csrf_token ?? recoveredCsrf, truth);
        onNavigate(
          truth.principal.role === "approver"
            ? "/approvals"
            : "/conversations/new",
          true,
        );
      } catch {
        onSessionFailure(
          "无法确认切换后的真实身份，已关闭当前工作区以防止跨会话数据混用。",
        );
        return;
      }
      setError(errorMessage(value));
    } finally {
      setBusy(false);
    }
  }, [csrf, onNavigate, onSession, onSessionFailure]);

  return { busy, error, setError, switchTenant, switchToCustomer };
}
