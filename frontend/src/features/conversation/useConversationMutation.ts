import {
  type FormEvent,
  type MutableRefObject,
  useCallback,
  useEffect,
  useState,
} from "react";

import { mutationIdentity } from "../../idempotency";
import {
  api,
  createDemoSession,
  errorMessage,
  ProductApiError,
} from "../../productApi";
import type {
  CommandAccepted,
  ProductAction,
  SessionContext,
} from "../../productTypes";
import { navigate } from "../../routing";
import { useIdempotentMutation } from "../../useIdempotentMutation";
import { omitKey } from "./conversationState";

type LoadConversation = (id?: string | null) => Promise<void>;

export function useConversationMutation({
  selectedId,
  session,
  csrf,
  onSession,
  onSessionFailure,
  scopeTransitioning,
  loadConversation,
  loadList,
  clearQueryScope,
  retryResource,
  clearViewScope,
}: {
  selectedId: string | null;
  session: SessionContext;
  csrf: string;
  onSession: (csrf: string, session: SessionContext) => void;
  onSessionFailure: (message: string) => void;
  scopeTransitioning: MutableRefObject<boolean>;
  loadConversation: LoadConversation;
  loadList: () => Promise<void>;
  clearQueryScope: () => void;
  retryResource: () => void;
  clearViewScope: () => void;
}) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [withdrawing, setWithdrawing] = useState(false);
  const [actionErrors, setActionErrors] = useState<Record<string, string>>({});
  const messageMutation = useIdempotentMutation();
  const withdrawalMutation = useIdempotentMutation();
  const lifecycleMutation = useIdempotentMutation();
  const resetMessageMutation = messageMutation.reset;
  const resetWithdrawalMutation = withdrawalMutation.reset;
  const resetLifecycleMutation = lifecycleMutation.reset;
  const draftKey = `${session.active_tenant.id}:${selectedId ?? "new"}`;
  const draft = drafts[draftKey] ?? "";
  const setDraft = useCallback(
    (value: string) =>
      setDrafts((current) =>
        value ? { ...current, [draftKey]: value } : omitKey(current, draftKey),
      ),
    [draftKey],
  );

  useEffect(() => {
    setError("");
    setActionErrors({});
    resetMessageMutation();
    resetWithdrawalMutation();
    resetLifecycleMutation();
  }, [
    resetLifecycleMutation,
    resetMessageMutation,
    resetWithdrawalMutation,
    selectedId,
    session.active_tenant.id,
  ]);

  const submit = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      const message = draft.trim();
      if (!message || busy || messageMutation.busy) return;
      setBusy(true);
      setError("");
      try {
        const identity = mutationIdentity({
          tenantId: session.active_tenant.id,
          resource: selectedId ?? "new_conversation",
          operation: selectedId ? "append_message" : "create_conversation",
          payload: { message },
        });
        const accepted = await messageMutation.run(identity, (key) =>
          selectedId
            ? api<CommandAccepted>(
                `/conversations/${encodeURIComponent(selectedId)}/messages`,
                {
                  method: "POST",
                  headers: { "Idempotency-Key": key },
                  body: JSON.stringify({ message }),
                },
                csrf,
              )
            : api<CommandAccepted>(
                "/conversations",
                {
                  method: "POST",
                  headers: { "Idempotency-Key": key },
                  body: JSON.stringify({ message }),
                },
                csrf,
              ),
        );
        setDraft("");
        if (!selectedId)
          navigate(
            `/conversations/${encodeURIComponent(accepted.ticket_id)}`,
            true,
          );
        else await Promise.all([loadConversation(selectedId), loadList()]);
      } catch (value) {
        setError(errorMessage(value));
      } finally {
        setBusy(false);
      }
    },
    [
      busy,
      csrf,
      draft,
      loadConversation,
      loadList,
      messageMutation,
      selectedId,
      session.active_tenant.id,
      setDraft,
    ],
  );

  const withdraw = useCallback(
    async (action: ProductAction) => {
      if (
        !selectedId ||
        !window.confirm("确认撤回这项申请？撤回不会执行任何业务动作。")
      )
        return;
      setWithdrawing(true);
      setActionErrors((current) => omitKey(current, action.id));
      try {
        const payload = { reason: "客户从会话中明确撤回申请" };
        const identity = mutationIdentity({
          tenantId: session.active_tenant.id,
          resource: `${selectedId}:${action.id}`,
          operation: "withdraw_action",
          payload,
        });
        await withdrawalMutation.run(identity, (key) =>
          api(
            `/conversations/${encodeURIComponent(selectedId)}/actions/${encodeURIComponent(action.id)}/withdraw`,
            {
              method: "POST",
              headers: { "Idempotency-Key": key },
              body: JSON.stringify(payload),
            },
            csrf,
          ),
        );
        await loadConversation(selectedId);
      } catch (value) {
        setActionErrors((current) => ({
          ...current,
          [action.id]: errorMessage(value),
        }));
        if (value instanceof ProductApiError && value.status === 409) {
          try {
            await loadConversation(selectedId);
          } catch {
            // Keep the original action-scoped conflict visible.
          }
        }
      } finally {
        setWithdrawing(false);
      }
    },
    [
      csrf,
      loadConversation,
      selectedId,
      session.active_tenant.id,
      withdrawalMutation,
    ],
  );

  const transitionLifecycle = useCallback(
    async (next: "archive" | "restore") => {
      if (!selectedId) return;
      setBusy(true);
      setError("");
      try {
        const identity = mutationIdentity({
          tenantId: session.active_tenant.id,
          resource: selectedId,
          operation: next,
        });
        await lifecycleMutation.run(identity, (key) =>
          api(
            `/conversations/${encodeURIComponent(selectedId)}/${next}`,
            {
              method: "POST",
              headers: { "Idempotency-Key": key },
            },
            csrf,
          ),
        );
        await Promise.all([loadConversation(selectedId), loadList()]);
      } catch (value) {
        setError(errorMessage(value));
      } finally {
        setBusy(false);
      }
    },
    [
      csrf,
      lifecycleMutation,
      loadConversation,
      loadList,
      selectedId,
      session.active_tenant.id,
    ],
  );

  const switchRole = useCallback(
    async (role: "customer" | "approver") => {
      setError("");
      scopeTransitioning.current = true;
      clearQueryScope();
      clearViewScope();
      let recoveredCsrf = csrf;
      try {
        const nextCsrf = await createDemoSession(role);
        recoveredCsrf = nextCsrf;
        const next = await api<SessionContext>("/session");
        onSession(next.csrf_token ?? nextCsrf, next);
        navigate(
          role === "approver" ? "/approvals" : "/conversations/new",
          true,
        );
      } catch (value) {
        try {
          const truth = await api<SessionContext>("/session");
          onSession(truth.csrf_token ?? recoveredCsrf, truth);
          navigate(
            truth.principal.role === "approver"
              ? "/approvals"
              : selectedId
                ? `/conversations/${encodeURIComponent(selectedId)}`
                : "/conversations/new",
            true,
          );
          if (truth.principal.role === "customer") {
            scopeTransitioning.current = false;
            retryResource();
            void loadList();
          }
        } catch {
          onSessionFailure(
            "无法确认切换后的真实身份，已关闭当前工作区以防止跨会话数据混用。",
          );
          return;
        }
        setError(errorMessage(value));
      }
    },
    [
      clearQueryScope,
      clearViewScope,
      csrf,
      loadList,
      onSession,
      onSessionFailure,
      retryResource,
      scopeTransitioning,
      selectedId,
    ],
  );

  return {
    actionErrors,
    busy,
    draft,
    error,
    messageMutation,
    withdrawing,
    setActionErrors,
    setDraft,
    setError,
    submit,
    switchRole,
    transitionLifecycle,
    withdraw,
  };
}
