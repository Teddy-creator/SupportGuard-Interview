import { useEffect, useLayoutEffect, useRef } from "react";

import type {
  ApprovalDecision,
  ApprovalDetail,
  ApprovalSource,
} from "../../productTypes";
import {
  approvalDecisionAllowed,
  approvalEditableField,
  validTargetConcurrency,
} from "../../approvalEditing";
import { actionLabel } from "../../productCopy";
import { formatTime, statusLabel } from "../../presentation";

function displayFact(value: unknown, fallback = "—"): string {
  return typeof value === "string" || typeof value === "number"
    ? String(value)
    : fallback;
}

function safeBusinessFacts(review: ApprovalDetail["review_context"]) {
  const fieldLabels: Record<string, string> = {
    billing_record_id: "账单",
    duplicate_of: "重复记录",
    amount: "金额",
    currency: "币种",
    status: "当前状态",
    balance: "账户余额",
    plan: "套餐",
    api_key_id: "API Key",
    subscription_id: "订阅",
    security_status: "安全状态",
    region: "区域",
  };
  const facts = new Map<string, string>();
  for (const observation of review.tool_observations) {
    const data = observation.data;
    for (const [field, value] of Object.entries(data)) {
      const label = fieldLabels[field];
      if (
        label &&
        (typeof value === "string" || typeof value === "number") &&
        String(value).trim()
      )
        facts.set(label, String(value));
    }
  }
  return [...facts.entries()].map(([label, value]) => ({ label, value }));
}

function evidenceSummary(review: ApprovalDetail["review_context"]) {
  return review.evidence.map((item) => ({
    title: item.title,
    section: item.section_path,
    version: item.version,
    freshness: item.freshness,
  }));
}

function freshnessLabel(value: string): string {
  const labels: Record<string, string> = {
    current: "当前有效",
    changed_since_proposal: "提案后已变化",
    unavailable: "未提供独立时效标记",
  };
  return labels[value] ?? "时效状态未知";
}

function ApprovalSummary({
  detail,
  onOpenSource,
}: {
  detail: ApprovalDetail;
  onOpenSource?: () => void;
}) {
  const payload = detail.action_payload;
  const review = detail.review_context;
  const resource = detail.resource_id;
  const originalTurn = review.original_request;
  const evidence = review.evidence;
  const facts = safeBusinessFacts(review);
  const sources = evidenceSummary(review);
  const policyRoute = review.policy_route;
  const refundPayload = "billing_record_id" in payload ? payload : null;
  return (
    <>
      <div
        className="source-conversation"
        id="approval-source-conversation"
      >
        <span>
          来源会话：{detail.ticket?.title ?? "客户支持会话"}
        </span>
        <small>当前审批身份下的只读来源投影</small>
        {onOpenSource ? (
          <button type="button" onClick={onOpenSource}>
            查看来源会话
          </button>
        ) : null}
      </div>
      {typeof originalTurn === "string" && originalTurn.trim() ? (
        <blockquote
          className="approval-origin"
          id="approval-original-request"
        >
          <small>客户原始诉求</small>
          {originalTurn}
        </blockquote>
      ) : null}
      <h3>安全动作摘要</h3>
      <dl className="approval-facts">
        <div>
          <dt>动作</dt>
          <dd>{actionLabel(detail.action_type)}</dd>
        </div>
        {refundPayload?.amount !== undefined ? (
          <div>
            <dt>金额</dt>
            <dd>
              {displayFact(refundPayload.amount)}{" "}
              {displayFact(refundPayload.currency, "")}
            </dd>
          </div>
        ) : null}
        <div>
          <dt>目标资源</dt>
          <dd>{displayFact(resource, "受保护资源")}</dd>
        </div>
        {refundPayload?.refund_reason ? (
          <div>
            <dt>操作理由</dt>
            <dd>{displayFact(refundPayload.refund_reason)}</dd>
          </div>
        ) : null}
      </dl>
      {facts.length ? (
        <>
          <h3>已核验业务事实</h3>
          <dl className="approval-facts">
            {facts.map((fact) => (
              <div key={fact.label}>
                <dt>{fact.label}</dt>
                <dd>{fact.value}</dd>
              </div>
            ))}
          </dl>
        </>
      ) : null}
      <h3>策略与审批快照</h3>
      <dl>
        <div>
          <dt>资源版本</dt>
          <dd>{detail.business_version}</dd>
        </div>
        <div>
          <dt>当前状态</dt>
          <dd>{statusLabel(detail.status)}</dd>
        </div>
        <div>
          <dt>策略校验</dt>
          <dd>{policyRoute}</dd>
        </div>
        <div>
          <dt>证据摘要</dt>
          <dd>{evidence.length} 项</dd>
        </div>
      </dl>
      {sources.length ? (
        <ul className="approval-evidence-list" aria-label="审批证据">
          {sources.map((source, index) => (
            <li key={`${source.title}-${source.section}-${index}`}>
              <strong>{source.title}</strong>
              <span>
                {[source.section, source.version ? `v${source.version}` : ""]
                  .filter(Boolean)
                  .join(" · ")}
              </span>
              <small>{freshnessLabel(source.freshness)}</small>
            </li>
          ))}
        </ul>
      ) : (
        <p className="approval-evidence-warning">
          当前安全投影没有可展开的证据详情；请勿仅依据证据数量作出决定。
        </p>
      )}
      {detail.proposed_diff?.length ? (
        <>
          <h3>执行差异</h3>
          <dl className="approval-facts">
            {detail.proposed_diff.map((item) => (
              <div key={item.field}>
                <dt>{item.field}</dt>
                <dd>
                  {item.current} → {item.proposed}
                </dd>
              </div>
            ))}
          </dl>
        </>
      ) : null}
      {detail.execution_preconditions?.length ? (
        <>
          <h3>执行前置条件</h3>
          <ul className="approval-preconditions">
            {detail.execution_preconditions.map((item) => (
              <li key={item.label} className={item.satisfied ? "ok" : "blocked"}>
                {item.satisfied ? "已满足" : "未满足"} · {item.label}
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </>
  );
}

function ApprovalSourceDrawer({
  source,
  loading,
  loadingOlder,
  error,
  onLoadOlder,
  onClose,
}: {
  source: ApprovalSource | null;
  loading: boolean;
  loadingOlder: boolean;
  error: string;
  onLoadOlder: () => void;
  onClose: () => void;
}) {
  const originMessage = useRef<HTMLElement | null>(null);
  const drawer = useRef<HTMLElement | null>(null);
  const closeButton = useRef<HTMLButtonElement | null>(null);
  const returnFocus = useRef<HTMLElement | null>(null);
  const positionedOrigin = useRef(false);
  const prependScroll = useRef<{
    scrollHeight: number;
    scrollTop: number;
  } | null>(null);

  useEffect(() => {
    if (!source || positionedOrigin.current) return;
    const frame = window.requestAnimationFrame(() => {
      originMessage.current?.scrollIntoView?.({
        block: "center",
        behavior: "instant",
      });
      positionedOrigin.current = true;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [source]);

  useLayoutEffect(() => {
    const previous = prependScroll.current;
    if (!previous || loadingOlder) return;
    const currentDrawer = drawer.current;
    if (currentDrawer)
      currentDrawer.scrollTop =
        previous.scrollTop + (currentDrawer.scrollHeight - previous.scrollHeight);
    prependScroll.current = null;
  }, [loadingOlder, source]);

  useEffect(() => {
    const active = document.activeElement;
    returnFocus.current = active instanceof HTMLElement ? active : null;
    closeButton.current?.focus();
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      onClose();
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("keydown", closeOnEscape);
      returnFocus.current?.focus();
    };
  }, [onClose]);

  const loadOlder = () => {
    const currentDrawer = drawer.current;
    prependScroll.current = currentDrawer
      ? {
          scrollHeight: currentDrawer.scrollHeight,
          scrollTop: currentDrawer.scrollTop,
        }
      : null;
    onLoadOlder();
  };

  return (
    <div className="approval-source-overlay" role="presentation">
      <aside
        ref={drawer}
        className="approval-source-drawer"
        role="dialog"
        aria-modal="true"
        aria-label="来源会话"
      >
        <header>
          <div>
            <small>审批绑定的只读会话</small>
            <h2>{source?.title ?? "正在载入来源会话…"}</h2>
          </div>
          <button
            ref={closeButton}
            type="button"
            onClick={onClose}
            aria-label="关闭来源会话"
          >
            ×
          </button>
        </header>
        {loading ? <div className="skeleton">正在核验来源绑定…</div> : null}
        {error ? (
          <div className="safe-error" role="alert">
            {error}
          </div>
        ) : null}
        {source ? (
          <>
            <p className="approval-source-binding">
              来源工单 {source.ticket_id} · 原始轮次 {source.origin_turn_id}
            </p>
            <p className="approval-source-window">
              当前显示审批来源及其之前的 {source.messages.length} 条消息，不包含来源之后的对话。
            </p>
            {source.has_more ? (
              <button
                className="load-more"
                type="button"
                disabled={loadingOlder}
                onClick={loadOlder}
              >
                {loadingOlder ? "正在加载更早消息…" : "加载更早来源消息"}
              </button>
            ) : null}
            <div className="approval-source-messages">
              {source.messages.map((message) => (
                <article
                  key={message.id}
                  ref={message.is_origin_turn ? originMessage : undefined}
                  className={[
                    message.kind === "customer"
                      ? "source-customer-message"
                      : "source-support-message",
                    message.is_origin_turn ? "source-origin-message" : "",
                  ]
                    .filter(Boolean)
                    .join(" ")}
                >
                  <header>
                    <strong>
                      {message.kind === "customer" ? "客户" : "SupportGuard"}
                    </strong>
                    <span>
                      {message.is_origin_turn ? "审批来源 · " : ""}
                      <time>{formatTime(message.created_at)}</time>
                    </span>
                  </header>
                  <p>{message.content}</p>
                </article>
              ))}
            </div>
          </>
        ) : null}
      </aside>
    </div>
  );
}

export function ApprovalDetailPanel({
  detail,
  busy,
  decision,
  reason,
  refundReason,
  targetConcurrency,
  mutationError,
  mutationFieldError,
  onDecision,
  onReason,
  onRefundReason,
  onTargetConcurrency,
  onDecide,
  source = null,
  sourceOpen = false,
  sourceLoading = false,
  sourceLoadingOlder = false,
  sourceError = "",
  onOpenSource,
  onLoadOlderSource,
  onCloseSource,
}: {
  detail: ApprovalDetail | null;
  busy: boolean;
  decision: ApprovalDecision;
  reason: string;
  refundReason: string;
  targetConcurrency: string;
  mutationError: string;
  mutationFieldError: string;
  onDecision: (value: ApprovalDecision) => void;
  onReason: (value: string) => void;
  onRefundReason: (value: string) => void;
  onTargetConcurrency: (value: string) => void;
  onDecide: (action: ApprovalDecision) => void;
  source?: ApprovalSource | null;
  sourceOpen?: boolean;
  sourceLoading?: boolean;
  sourceLoadingOlder?: boolean;
  sourceError?: string;
  onOpenSource?: () => void;
  onLoadOlderSource?: () => void;
  onCloseSource?: () => void;
}) {
  if (!detail)
    return (
      <div className="empty-state">
        选择一项申请查看证据、策略与执行前置条件。
      </div>
    );
  const editableField = approvalEditableField(detail.action_type);
  const allowedDecisions = (
    ["approve", "edit-and-approve", "reject"] as const
  ).filter((item) => approvalDecisionAllowed(detail, item));
  const selectedDecisionAllowed = approvalDecisionAllowed(detail, decision);
  const optionalEditReasonValid =
    decision !== "edit-and-approve" ||
    reason.trim().length === 0 ||
    reason.trim().length >= 3;
  const editValueValid =
    decision !== "edit-and-approve" ||
    (editableField === "refund_reason"
      ? refundReason.trim().length >= 5
      : editableField === "target_concurrency"
        ? validTargetConcurrency(targetConcurrency)
        : false);
  return (
    <>
      <div className="approval-title">
        <h2>
          {actionLabel(detail.action_type)}
        </h2>
        <span>{statusLabel(detail.status)}</span>
      </div>
      <ApprovalSummary detail={detail} onOpenSource={onOpenSource} />
      {sourceOpen && onCloseSource && onLoadOlderSource ? (
        <ApprovalSourceDrawer
          source={source}
          loading={sourceLoading}
          loadingOlder={sourceLoadingOlder}
          error={sourceError}
          onLoadOlder={onLoadOlderSource}
          onClose={onCloseSource}
        />
      ) : null}
      {allowedDecisions.length ? (
        <section className="approval-decision-panel" aria-label="审批动作">
          <p className="approval-role-note">
            你只是在决定本次高风险动作。拒绝不会接管或结束客户会话，客户仍可继续由
            SupportGuard 响应。
          </p>
          <div className="decision-selector" aria-label="审批决定">
            {approvalDecisionAllowed(detail, "approve") ? (
              <button
                type="button"
                className={decision === "approve" ? "selected" : ""}
                onClick={() => onDecision("approve")}
              >
                批准
              </button>
            ) : null}
            {approvalDecisionAllowed(detail, "edit-and-approve") ? (
              <button
                type="button"
                className={decision === "edit-and-approve" ? "selected" : ""}
                onClick={() => onDecision("edit-and-approve")}
              >
                修改并批准
              </button>
            ) : null}
            {approvalDecisionAllowed(detail, "reject") ? (
              <button
                type="button"
                className={decision === "reject" ? "selected" : ""}
                onClick={() => onDecision("reject")}
              >
                拒绝
              </button>
            ) : null}
          </div>
          {selectedDecisionAllowed ? (
            <>
              <label className="reason-field">
                {decision === "reject"
                  ? "拒绝理由（必填）"
                  : decision === "edit-and-approve"
                    ? "审批备注（可选）"
                    : "审批理由（可选）"}
                <textarea
                  value={reason}
                  maxLength={2000}
                  onChange={(event) => onReason(event.target.value)}
                  placeholder={
                    decision === "reject"
                      ? "请说明拒绝依据（至少 3 个字符）"
                      : decision === "edit-and-approve"
                        ? "可补充修改依据（填写时至少 3 个字符）"
                        : "可补充批准依据（填写时至少 3 个字符）"
                  }
                />
              </label>
              {decision === "edit-and-approve" &&
              editableField === "refund_reason" ? (
                <label className="reason-field">
                  修改后的退款理由
                  <textarea
                    value={refundReason}
                    minLength={5}
                    maxLength={2000}
                    onChange={(event) => onRefundReason(event.target.value)}
                    placeholder="至少 5 个字符"
                  />
                  {mutationFieldError ? (
                    <span className="field-error" role="alert">
                      {mutationFieldError}
                    </span>
                  ) : null}
                </label>
              ) : null}
              {decision === "edit-and-approve" &&
              editableField === "target_concurrency" ? (
                <label className="reason-field">
                  目标并发（整数）
                  <input
                    type="number"
                    inputMode="numeric"
                    min={1}
                    max={1_000_000}
                    step={1}
                    value={targetConcurrency}
                    onChange={(event) =>
                      onTargetConcurrency(event.target.value)
                    }
                    placeholder="1–1000000"
                  />
                  {mutationFieldError ? (
                    <span className="field-error" role="alert">
                      {mutationFieldError}
                    </span>
                  ) : null}
                </label>
              ) : null}
              <div className="approval-actions">
                <button
                  type="button"
                  disabled={
                    busy ||
                    !optionalEditReasonValid ||
                    !editValueValid ||
                    (decision === "reject" && reason.trim().length < 3)
                  }
                  onClick={() => onDecide(decision)}
                >
                  {decision === "approve"
                    ? "批准并提交执行"
                    : decision === "edit-and-approve"
                      ? "确认修改并提交执行"
                      : "确认拒绝"}
                </button>
              </div>
            </>
          ) : (
            <p className="completed-decision">
              请选择当前申请仍允许的审批决定。
            </p>
          )}
          {mutationError ? (
            <div className="action-inline-error" role="alert">
              <strong>这项审批没有更新。</strong>
              <span>{mutationError}</span>
            </div>
          ) : null}
        </section>
      ) : (
        <p className="completed-decision">
          {detail.status === "executed"
            ? "该动作已执行，结果会同步回来源会话。"
            : detail.status === "pending"
              ? "该申请暂不可操作：提案、检查点或业务事实绑定需要恢复。系统没有执行任何业务动作。"
            : detail.status === "rejected"
              ? "该申请已被拒绝，没有执行任何业务动作；客户会话仍可继续。"
              : detail.status === "stale"
                ? "申请所依据的业务事实或资源版本已经过期，不能继续执行；请从来源会话重新核验。"
                : detail.status === "withdrawn"
                  ? "客户已撤回这项申请，系统没有执行任何业务动作。"
              : detail.status === "failed"
                ? "执行没有完成。请返回来源会话查看安全说明，必要时由运维排查。"
                : ["approved", "executing", "verification_pending"].includes(
                      detail.status,
                    )
                  ? "审批决定已提交，系统正在执行并核验最终结果。"
                  : "该申请已进入终态，不能重复处理。"}
        </p>
      )}
    </>
  );
}
