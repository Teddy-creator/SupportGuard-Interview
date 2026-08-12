import { ApprovalDetailPanel, ApprovalList } from "../ApprovalUi";
import { useApprovalMutation } from "../features/approval/useApprovalMutation";
import { useApprovalQuery } from "../features/approval/useApprovalQuery";
import { useApprovalSessionMutation } from "../features/approval/useApprovalSessionMutation";
import { useApprovalSourceQuery } from "../features/approval/useApprovalSourceQuery";
import { useApprovalViewState } from "../features/approval/useApprovalViewState";
import type { SessionContext } from "../productTypes";

export function TenantSwitcher({
  session,
  busy,
  onSwitch,
}: {
  session: SessionContext;
  busy: boolean;
  onSwitch: (tenantId: string) => void;
}) {
  if (session.auth_mode !== "development")
    return (
      <div className="production-tenant" aria-label="当前租户">
        <small>当前租户</small>
        <strong>{session.active_tenant.name}</strong>
      </div>
    );
  return (
    <label>
      当前租户
      <select
        aria-label="当前租户"
        value={session.active_tenant.id}
        disabled={busy}
        onChange={(event) => onSwitch(event.target.value)}
      >
        {session.accessible_tenants.map((tenant) => (
          <option key={tenant.id} value={tenant.id}>
            {tenant.name}
          </option>
        ))}
      </select>
    </label>
  );
}

export function ApprovalPage({
  selected,
  session,
  csrf,
  onSession,
  onNavigate,
  onSessionFailure,
}: {
  selected?: string;
  session: SessionContext;
  csrf: string;
  onSession: (csrf: string, session: SessionContext) => void;
  onNavigate: (path: string, replace?: boolean) => void;
  onSessionFailure: (message: string) => void;
}) {
  const mutation = useApprovalMutation({
    approvalId: selected,
    tenantId: session.active_tenant.id,
    csrf,
  });
  const query = useApprovalQuery({
    selected,
    tenantId: session.active_tenant.id,
    editingDecision: mutation.editing,
  });
  const source = useApprovalSourceQuery({
    approvalId: selected,
    ticketId: query.detail?.ticket_id,
    tenantId: session.active_tenant.id,
  });
  const view = useApprovalViewState({
    scopeKey: `${session.active_tenant.id}:${selected ?? "list"}`,
    cancelSource: source.cancel,
  });
  const sessionMutation = useApprovalSessionMutation({
    session,
    csrf,
    onSession,
    onNavigate,
    onSessionFailure,
    clearApprovalScope: () => {
      query.clearScope();
      mutation.reset();
    },
    resumeApprovalScope: query.retryList,
  });
  const busy = mutation.busy || sessionMutation.busy;

  return (
    <div className="approval-layout">
      <aside className="approval-sidebar">
        <div className="brand-block">
          <span className="brand-mark">SG</span>
          <strong>SupportGuard</strong>
        </div>
        <TenantSwitcher
          session={session}
          busy={busy}
          onSwitch={(tenantId) => void sessionMutation.switchTenant(tenantId)}
        />
        {session.auth_mode === "development" ? (
          <button
            className="back-to-customer"
            onClick={() => void sessionMutation.switchToCustomer()}
          >
            切换为客户演示
          </button>
        ) : null}
      </aside>
      <main className="approval-main">
        <header>
          <div>
            <small>人工审批工作台</small>
            <h1>审批工作台</h1>
          </div>
          <span className="identity-pill">
            {session.principal.display_name} · 审批者
          </span>
        </header>
        {query.listError && query.items.length > 0 ? (
          <div className="safe-error">{query.listError}</div>
        ) : null}
        {sessionMutation.error ? (
          <div className="safe-error">{sessionMutation.error}</div>
        ) : null}
        <div className="approval-columns">
          <section className="approval-list">
            <ApprovalList
              items={query.items}
              state={query.listState}
              selected={selected}
              onSelect={(id) => onNavigate(`/approvals/${id}`)}
              onRetry={query.retryList}
            />
          </section>
          <section className="approval-detail">
            {query.detailError ? (
              <div className="empty-state">
                <p>{query.detailError}</p>
                <button type="button" onClick={() => onNavigate("/approvals")}>
                  返回审批列表
                </button>
              </div>
            ) : (
              <ApprovalDetailPanel
                detail={query.detail}
                busy={busy}
                decision={mutation.decision}
                reason={mutation.reason}
                refundReason={mutation.refundReason}
                targetConcurrency={mutation.targetConcurrency}
                mutationError={mutation.error}
                mutationFieldError={mutation.fieldError}
                onDecision={(decision) =>
                  mutation.selectDecision(query.detail, decision)
                }
                onReason={mutation.setReason}
                onRefundReason={mutation.setRefundReason}
                onTargetConcurrency={mutation.setTargetConcurrency}
                onDecide={(decision) =>
                  void mutation.decide(query.detail, decision, {
                    list: query.loadItems,
                    detail: query.loadDetail,
                    onListError: query.setListError,
                    onDetailError: query.setDetailError,
                  })
                }
                source={source.source}
                sourceOpen={view.sourceOpen}
                sourceLoading={source.loading}
                sourceLoadingOlder={source.loadingOlder}
                sourceError={source.error}
                onOpenSource={() => {
                  view.openSource();
                  void source.openSource();
                }}
                onLoadOlderSource={() => void source.loadOlder()}
                onCloseSource={view.closeSource}
              />
            )}
          </section>
        </div>
      </main>
    </div>
  );
}
