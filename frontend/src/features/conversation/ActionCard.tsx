import { useRef } from "react";

import { actionLabel } from "../../productCopy";
import type { ProductAction } from "../../productTypes";
import { LEGACY_TAKEOVER_NOTICE, statusLabel } from "../../presentation";

function actionResource(action: ProductAction): string {
  const payload = action.action_payload;
  const value =
    payload.billing_record_id ?? payload.api_key_id ?? payload.subscription_id;
  return typeof value === "string" ? value : "受保护资源";
}

function actionAmount(action: ProductAction): string | null {
  const amount = action.action_payload.amount;
  const currency = action.action_payload.currency;
  return typeof amount === "string" || typeof amount === "number"
    ? `${amount} ${typeof currency === "string" ? currency : ""}`.trim()
    : null;
}

function actionTarget(action: ProductAction): string | null {
  if (action.action_type !== "entitlement_change") return null;
  const target = action.action_payload.target;
  if (!target || typeof target !== "object" || Array.isArray(target)) {
    return null;
  }

  const plan = Reflect.get(target, "plan");
  if (
    typeof plan === "string" &&
    /^[\p{L}\p{N}][\p{L}\p{N} ._+-]{0,63}$/u.test(plan)
  ) {
    return `套餐 ${plan}`;
  }

  for (const [field, label] of [
    ["rpm_limit", "RPM 上限"],
    ["concurrency_limit", "并发上限"],
  ] as const) {
    const value = Reflect.get(target, field);
    if (
      typeof value === "number" &&
      Number.isSafeInteger(value) &&
      value > 0 &&
      value <= 10_000_000
    ) {
      return `${label} ${value}`;
    }
  }
  return null;
}

export function InlineActionCard({
  action,
  withdrawing,
  error,
  onWithdraw,
  onDismissError,
}: {
  action: ProductAction;
  withdrawing: boolean;
  error?: string;
  onWithdraw: (action: ProductAction) => void;
  onDismissError: (action: ProductAction) => void;
}) {
  const withdrawButton = useRef<HTMLButtonElement | null>(null);
  const target = actionTarget(action);
  const statusHelp: Record<string, string> = {
    pending: "审批结果会自动同步到此对话",
    approved: "审批已通过，正在等待安全执行",
    executing: "动作正在执行；最终结果仍会经过事务核验",
    verification_pending: "执行结果暂不确定，系统正在核验真实业务状态",
    executed: "动作已完成，最终业务状态已核验",
    rejected: "审批者已拒绝；没有执行任何业务动作",
    withdrawn: "申请已撤回；没有执行任何业务动作",
    stale: "原申请已失效；如仍需处理，请重新提出请求",
    failed: "动作没有完成；请查看当前会话中的安全说明",
    human_queue: LEGACY_TAKEOVER_NOTICE,
    manual_takeover: LEGACY_TAKEOVER_NOTICE,
    manual_takeover_legacy: LEGACY_TAKEOVER_NOTICE,
  };
  return (
    <section
      className={`inline-action ${action.status}`}
      aria-label={`${actionLabel(action.action_type)} ${statusLabel(action.status)}`}
    >
      <div className="action-heading">
        <span className="action-icon">＄</span>
        <h3>{actionLabel(action.action_type)}</h3>
      </div>
      <div className="action-facts">
        {actionAmount(action) ? (
          <div>
            <span>金额</span>
            <strong>{actionAmount(action)}</strong>
          </div>
        ) : null}
        <div>
          <span>资源</span>
          <strong>{actionResource(action)}</strong>
        </div>
        {target ? (
          <div>
            <span>目标</span>
            <strong>{target}</strong>
          </div>
        ) : null}
        <div>
          <span>状态</span>
          <strong className="action-status">
            {statusLabel(action.status)}
          </strong>
        </div>
      </div>
      <div className="action-progress" aria-hidden="true">
        <span />
      </div>
      <div className="action-foot">
        <span>{statusHelp[action.status] ?? "该申请的历史状态已保留"}</span>
        {action.allowed_actions.includes("withdraw") ? (
          <button
            ref={withdrawButton}
            type="button"
            disabled={withdrawing}
            onClick={() => onWithdraw(action)}
          >
            撤回申请
          </button>
        ) : null}
      </div>
      {error ? (
        <div className="action-inline-error" role="alert">
          <div>
            <strong>这项申请没有更新。</strong>
            <span>{error}</span>
          </div>
          <button
            type="button"
            aria-label="关闭操作错误"
            onClick={() => {
              withdrawButton.current?.focus();
              onDismissError(action);
            }}
          >
            ×
          </button>
        </div>
      ) : null}
    </section>
  );
}

