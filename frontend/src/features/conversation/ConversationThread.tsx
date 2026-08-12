import { useState } from "react";

import type {
  ConversationDetail,
  ConversationTurn,
  ProductAction,
} from "../../productTypes";
import { formatTime, LEGACY_TAKEOVER_NOTICE } from "../../presentation";
import { InlineActionCard } from "./ActionCard";
import {
  CitationChip,
  citationSourceKey,
  groupedCitationsFor,
  SafeMessage,
} from "./Sources";

function activitySummary(turn: ConversationTurn): string {
  const attempts = turn.run?.budgets?.tool_attempts ?? 0;
  if (turn.activity_state === "running") return "正在检查业务事实与产品资料";
  if (turn.activity_state === "queued" || turn.activity_state === "accepted")
    return "已加入处理队列";
  if (turn.activity_state === "waiting_external")
    return `已完成核验并创建操作申请${attempts ? ` · ${attempts} 项检查` : ""}`;
  if (turn.result_state === "failed" || turn.activity_state === "failed")
    return "本轮未完成 · 未执行任何操作";
  if (
    turn.result_state === "answered_limited" ||
    turn.run?.finish_reason === "evidence_freshness_insufficient"
  )
    return "已给出有限结论 · 实时数据待刷新";
  if (turn.result_state === "refused") return "请求已安全拒绝 · 未执行任何操作";
  if (turn.result_state === "needs_clarification") return "需要补充信息后继续";
  if (turn.result_state === "human_queue") return LEGACY_TAKEOVER_NOTICE;
  return `已完成检查${attempts ? ` · ${attempts} 项` : ""}`;
}

function failedTurnMessage(turn: ConversationTurn): string {
  const category = turn.run?.failure_category ?? "runtime";
  const unknown: Record<string, string> = {
    api_request: "当前还无法把问题与具体 API 请求记录关联",
    provider: "当前还无法确认模型服务是否完成了本轮推理",
    tool: "当前还无法确认所需业务数据是否完整返回",
    runtime: "当前还无法确认自动流程完成到了哪一个可恢复步骤",
  };
  const next: Record<string, string> = {
    api_request: "请重试；若仍失败，请补充 Request ID 和发生时间",
    provider: "请稍后重试；若仍失败，请保留发生时间和相关资源编号",
    tool: "请核对账单、API Key 或订阅编号后重试",
    runtime: "请稍后重试，或发送一条新消息重新开始本轮处理",
  };
  return [
    "系统已检查本轮请求并尝试完成自动诊断。",
    "已确认本轮没有形成可验证的完整结果。",
    `${unknown[category] ?? unknown.runtime}。`,
    "本轮没有执行新的高风险操作，相关申请仍以当前持久化状态为准。",
    `${next[category] ?? next.runtime}。`,
  ].join("");
}

export function MessageStream({
  conversation,
  withdrawing,
  actionErrors,
  onWithdraw,
  onDismissActionError,
  onInspectTurn,
  hasOlder,
  loadingOlder,
  onLoadOlder,
}: {
  conversation: ConversationDetail;
  withdrawing: boolean;
  actionErrors: Record<string, string>;
  onWithdraw: (action: ProductAction) => void;
  onDismissActionError: (action: ProductAction) => void;
  onInspectTurn: (
    turn: ConversationTurn,
    messageId: string,
    trigger: HTMLButtonElement,
  ) => void;
  hasOlder: boolean;
  loadingOlder: boolean;
  onLoadOlder: () => void;
}) {
  const [copyState, setCopyState] = useState<string | null>(null);
  const actions = new Map<string | null | undefined, ProductAction[]>();
  for (const action of conversation.pending_actions) {
    actions.set(action.turn_id, [
      ...(actions.get(action.turn_id) ?? []),
      action,
    ]);
  }
  return (
    <div className="message-stream">
      {hasOlder ? (
        <button
          className="load-older"
          type="button"
          disabled={loadingOlder}
          onClick={onLoadOlder}
        >
          {loadingOlder ? "正在加载…" : "加载更早消息"}
        </button>
      ) : null}
      <div className="message-announcer" role="status" aria-live="polite">
        {copyState === "copy_failed"
          ? "复制失败，请手动选择消息内容"
          : copyState
            ? "消息已复制"
            : ""}
      </div>
      {conversation.turns.map((turn) => {
        const customer = turn.messages.find(
          (message) => message.kind === "customer",
        );
        const assistant = turn.messages.filter(
          (message) => message.kind === "assistant",
        );
        const updates = turn.messages.filter((message) =>
          ["action_update", "human_queue_update"].includes(message.kind),
        );
        const turnActions = actions.get(turn.id) ?? [];
        return (
          <article
            className="turn"
            key={turn.id}
            data-turn-state={turn.activity_state}
            data-turn-id={turn.id}
            data-turn-run-id={turn.run_id ?? ""}
            data-turn-activity={turn.activity_state}
            data-turn-result={turn.result_state ?? ""}
            data-turn-failure-category={turn.run?.failure_category ?? ""}
          >
            {customer ? (
              <div className="customer-message">
                <time>{formatTime(customer.created_at)}</time>
                <SafeMessage content={customer.content} />
              </div>
            ) : null}
            {turn.run_id ? (
              <details className="agent-activity">
                <summary>
                  <span className="activity-spinner" />
                  {activitySummary(turn)}
                  <span aria-hidden="true">⌄</span>
                </summary>
                <p>
                  本轮的 Decision、MCP、Observation
                  与失败阶段可在技术视图中查看。
                </p>
              </details>
            ) : null}
            {assistant.map((message) => {
              const citationGroups = groupedCitationsFor(
                turn.citations,
                message.id,
              );
              const primaryCitationGroup =
                citationGroups.find(
                  (group) => group[0]?.source_type === "business_fact",
                ) ?? citationGroups[0];
              return (
              <div className="assistant-row" key={message.id}>
                <span className="assistant-mark">SG</span>
                <div className="assistant-content">
                  <div className="assistant-meta">
                    <strong>SupportGuard</strong>
                    <time>{formatTime(message.created_at)}</time>
                  </div>
                  <div className="assistant-bubble">
                    <SafeMessage content={message.content} />
                    <div className="message-actions">
                      {turn.run_id ? (
                        <button
                          className="inspect-turn"
                          type="button"
                          onClick={(event) =>
                            onInspectTurn(turn, message.id, event.currentTarget)
                          }
                        >
                          在技术视图中查看本轮
                        </button>
                      ) : null}
                      <button
                        className="copy-message"
                        type="button"
                        onClick={async () => {
                          try {
                            await navigator.clipboard.writeText(message.content);
                            setCopyState(message.id);
                            window.setTimeout(() => setCopyState(null), 1400);
                          } catch {
                            setCopyState("copy_failed");
                            window.setTimeout(() => setCopyState(null), 2200);
                          }
                        }}
                      >
                        {copyState === message.id
                          ? "已复制"
                          : copyState === "copy_failed"
                            ? "复制失败"
                            : "复制"}
                      </button>
                    </div>
                  </div>
                  {(["knowledge", "business_fact"] as const).map(
                    (sourceType) => {
                      const groups = citationGroups.filter(
                        (group) =>
                          (group[0].source_type ?? "knowledge") === sourceType,
                      );
                      return groups.length ? (
                        <div
                          className="citation-group"
                          key={`${message.id}-${sourceType}`}
                        >
                          <small>
                            {sourceType === "knowledge"
                              ? "知识文档"
                              : "实时业务事实"}
                          </small>
                          <div className="citation-row">
                            {groups.map((citations, index) => (
                              <CitationChip
                                key={citationSourceKey(citations[0])}
                                citations={citations}
                                index={index}
                                primary={citations === primaryCitationGroup}
                              />
                            ))}
                          </div>
                        </div>
                      ) : null;
                    },
                  )}
                </div>
              </div>
              );
            })}
            {turnActions.map((action) => (
              <InlineActionCard
                key={action.id}
                action={action}
                withdrawing={withdrawing}
                error={actionErrors[action.id]}
                onWithdraw={onWithdraw}
                onDismissError={onDismissActionError}
              />
            ))}
            {updates.map((message) => (
              <div className="action-update" key={message.id}>
                {message.kind === "human_queue_update"
                  ? LEGACY_TAKEOVER_NOTICE
                  : message.content}
              </div>
            ))}
            {!assistant.length &&
            (turn.result_state === "failed" ||
              turn.activity_state === "failed") ? (
              <div className="safe-error-message">
                {failedTurnMessage(turn)}
              </div>
            ) : null}
          </article>
        );
      })}
    </div>
  );
}
