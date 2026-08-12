import { useState } from "react";

import { citationEvidenceKey } from "./citationKeys";
import { actionLabel } from "./productCopy";
import { formatTime, statusLabel } from "./presentation";
import type { TurnInspector } from "./productTypes";

const eventNames: Record<string, string> = {
  run_started: "开始分析请求",
  pii_redacted: "已保护敏感信息",
  classification: "识别问题类型",
  agent_decision: "规划下一步",
  tool_invocation: "调用只读工具",
  tool_observation: "取得业务事实",
  policy_decision: "完成策略校验",
  proposal_drafted: "生成动作申请",
  approval_interrupted: "等待独立审批",
  human_decision_accepted: "收到审批决定",
  final_outcome: "完成本轮处理",
  evidence_group_incomplete: "发现待补充证据",
  agent_stopped: "本轮安全停止",
  no_progress: "停止重复调用",
  proposal_eligibility: "检查动作申请条件",
  action_admitted: "校验高风险操作请求",
  action_obligations_evaluated: "评估操作证据义务",
  terminal_business_outcome_derived: "识别不可执行业务终态",
  terminal_business_outcome_projected: "生成零副作用答复",
  tool_surface_reduced: "收窄可用工具",
  evidence_synthesized: "绑定回答证据",
  action_candidate_assembled: "组装待审批申请",
  semantic_no_progress: "停止无进展调用",
  legacy_action_admission_recovery: "恢复旧版操作状态",
};

function eventDetail(payload?: Record<string, unknown>): string | null {
  if (!payload) return null;
  const assessment =
    payload.evidence_assessment &&
    typeof payload.evidence_assessment === "object"
      ? (payload.evidence_assessment as Record<string, unknown>)
      : undefined;
  const remaining =
    payload.remaining_budget && typeof payload.remaining_budget === "object"
      ? (payload.remaining_budget as Record<string, unknown>)
      : undefined;
  const values: Array<string | null> = [
    typeof payload.tool_name === "string" ? `工具 ${payload.tool_name}` : null,
    typeof payload.freshness_status === "string"
      ? `时效 ${payload.freshness_status}`
      : null,
    typeof payload.route === "string" ? `策略 ${payload.route}` : null,
    payload.failure_recorded === true ? "失败类别 已安全归类" : null,
    payload.stop_condition_recorded === true
      ? "停止条件 已记录"
      : null,
    Array.isArray(payload.injected_tool_allowlist)
      ? `可用工具 ${payload.injected_tool_allowlist.length} 个`
      : null,
    remaining
      ? `剩余预算 LLM ${String(remaining.llm_calls ?? "—")} / 工具轮 ${String(remaining.tool_rounds ?? "—")} / 尝试 ${String(remaining.tool_attempts ?? "—")}`
      : null,
  ];
  const missingGroups = Array.isArray(payload.missing_groups)
    ? payload.missing_groups
    : assessment && Array.isArray(assessment.missing_groups)
      ? assessment.missing_groups
      : [];
  const missing = missingGroups
    .filter((item): item is string => typeof item === "string")
    .slice(0, 4);
  if (missing.length) values.push(`缺少 ${missing.join("、")}`);
  const obligations = Array.isArray(payload.obligations)
    ? payload.obligations
        .filter(
          (item): item is Record<string, unknown> =>
            Boolean(item) && typeof item === "object",
        )
        .slice(0, 5)
        .map(
          (item) =>
            `${String(item.obligation_id ?? "unknown")}=${String(item.status ?? "unknown")}`,
        )
    : [];
  if (obligations.length) values.push(`证据义务 ${obligations.join("、")}`);
  if (Array.isArray(payload.injected_tools)) {
    values.push(
      `本轮工具 ${payload.injected_tools.length ? payload.injected_tools.join("、") : "无（仅证据合成）"}`,
    );
  }
  if (typeof payload.action_type === "string") {
    values.push(`操作 ${actionLabel(payload.action_type)}`);
  }
  if (typeof payload.source_count === "number") {
    values.push(`业务来源 ${payload.source_count} 项`);
  }
  return values.filter(Boolean).join(" · ") || null;
}

export function TechnicalInspector({
  open,
  loading,
  data,
  onClose,
}: {
  open: boolean;
  loading: boolean;
  data: TurnInspector | null;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<"run" | "evidence">("run");
  if (!open) return null;
  const run = data?.run;
  const configured = run?.configured_runtime ?? {
    model: run?.model,
    provider_mode: run?.provider_mode,
    tool_call_mode: run?.tool_call_mode,
  };
  const actual = run?.actual_runtime;
  return (
    <aside
      id="technical-inspector"
      className="technical-inspector"
      aria-label="技术检查器"
    >
      <div className="inspector-head">
        <h2>技术检查器</h2>
        <button
          className="icon-button"
          onClick={onClose}
          aria-label="关闭技术检查器"
        >
          »
        </button>
      </div>
      {loading ? (
        <p className="inspector-empty">正在读取持久化运行事实…</p>
      ) : !run ? (
        <p className="inspector-empty">
          请选择一条 Assistant 回答查看对应的 Agent Run。
        </p>
      ) : (
        <>
          <div className="inspector-binding" aria-label="所选回答绑定">
            <strong>所选 Assistant 回答</strong>
            <small>Message {data?.message_id}</small>
            <small>Turn {data?.turn_id}</small>
            <small>Run {data?.run_id}</small>
          </div>
          <div className="runtime-comparison">
            <section>
              <small>Configured Runtime</small>
              <strong>{configured.model ?? "未记录"}</strong>
              <span>{configured.provider_mode ?? "未记录"}</span>
              <span>{configured.tool_call_mode ?? "未记录"}</span>
            </section>
            <section>
              <small>实际 AgentCallAttempt</small>
              <strong>{actual?.model ?? "本轮未持久化 LLM Attempt"}</strong>
              <span>{actual?.provider_mode ?? "—"}</span>
              <span>{actual?.tool_call_mode ?? "—"}</span>
              <span>{actual?.attempt_status ?? "—"}</span>
            </section>
          </div>
          <div className="inspector-tabs">
            <button
              className={tab === "run" ? "active" : ""}
              onClick={() => setTab("run")}
            >
              运行
            </button>
            <button
              className={tab === "evidence" ? "active" : ""}
              onClick={() => setTab("evidence")}
            >
              证据
            </button>
          </div>
          {tab === "run" ? (
            <>
              <ol className="inspector-timeline">
                {(data?.timeline ?? [])
                  .filter((event) => event.run_id === data.run_id)
                  .map((event) => {
                    const detail = eventDetail(event.payload);
                    return (
                      <li
                        key={`${event.ticket_sequence}-${event.event_type}`}
                        data-event-type={event.event_type}
                        data-event-status={event.status}
                        data-ticket-sequence={event.ticket_sequence}
                      >
                        <span className="timeline-dot" />
                        <time>{formatTime(event.created_at)}</time>
                        <strong>
                          {eventNames[event.event_type] ?? "运行事件"}
                        </strong>
                        <small>{statusLabel(event.status)}</small>
                        {detail ? <p>{detail}</p> : null}
                      </li>
                    );
                  })}
              </ol>
              <div className="inspector-summary">
                <span>Tool rounds {run.budgets?.tool_rounds ?? 0}</span>
                <span>Attempts {run.budgets?.tool_attempts ?? 0}</span>
                <span>LLM calls {run.budgets?.llm_calls ?? 0}</span>
                <span>结果 {statusLabel(run.finish_reason ?? run.status)}</span>
              </div>
              {run.failure_category ? (
                <p className="inspector-failure">
                  本轮失败已归入公开类别 {run.failure_category}
                  ；内部错误码与原始载荷不会进入客户界面。
                </p>
              ) : null}
            </>
          ) : (
            <div className="inspector-evidence">
              {(data?.knowledge_sources ?? []).map((source, index) => (
                <article key={citationEvidenceKey(source, index)}>
                  <strong>{source.title ?? `来源 ${index + 1}`}</strong>
                  <small>
                    {source.section_path} · v{source.version}
                  </small>
                  <p>{source.supporting_span}</p>
                  <details>
                    <summary>技术绑定</summary>
                    <code>{source.citation_binding_id}</code>
                    <code>{source.chunk_id}</code>
                    <code>{source.index_version}</code>
                  </details>
                </article>
              ))}
              {(data?.business_facts ?? []).map((fact, index) => (
                <article
                  key={`${fact.title}-${fact.observed_at}-${index}`}
                >
                  <strong>{fact.title ?? "实时业务事实"}</strong>
                  <small>
                    {fact.freshness === "fresh" ? "当前有效" : "时效待确认"} ·{" "}
                    {formatTime(fact.observed_at)}
                  </small>
                  {fact.claim_summary ? <p>{fact.claim_summary}</p> : null}
                </article>
              ))}
              {!(data?.knowledge_sources ?? []).length &&
              !(data?.business_facts ?? []).length ? (
                <p className="inspector-empty">
                  当前 Run 没有已验证的引用或业务事实。
                </p>
              ) : null}
            </div>
          )}
        </>
      )}
    </aside>
  );
}
