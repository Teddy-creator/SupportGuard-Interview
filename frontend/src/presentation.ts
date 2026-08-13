export const LEGACY_TAKEOVER_NOTICE =
  "自动处理已停止；当前版本没有人工坐席收件或回复闭环。消息仅记录，不会创建 Agent Run。";

const statusNames: Record<string, string> = {
  accepted: "已加入队列",
  queued: "排队中",
  running: "处理中",
  waiting_external: "等待审批",
  completed: "已回复",
  failed: "未完成",
  answered: "已回答",
  answered_limited: "已给出有限结论",
  needs_clarification: "需要补充信息",
  refused: "请求未执行",
  rejected: "审批者已拒绝",
  withdrawn: "申请已撤回",
  proposal_created: "等待审批",
  human_queue: LEGACY_TAKEOVER_NOTICE,
  pending: "等待审批",
  approved: "已批准，准备执行",
  executing: "正在安全执行",
  verification_pending: "正在核验执行结果",
  executed: "已执行",
  manual_takeover: LEGACY_TAKEOVER_NOTICE,
  manual_takeover_legacy: LEGACY_TAKEOVER_NOTICE,
  stale: "业务事实已变化",
  bound: "已绑定审批快照",
  finalized: "快照已确认",
};

export function statusLabel(value: string): string {
  return statusNames[value] ?? value.replaceAll("_", " ");
}

export function activityLabel(value?: string | null): string {
  if (!value) return "";
  return /人工队列|转入人工处理|已转入人工|human_queue|manual_takeover(?:_legacy)?/i.test(
    value,
  )
    ? LEGACY_TAKEOVER_NOTICE
    : value;
}

export function formatTime(value?: string): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}
