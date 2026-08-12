import type { Approval } from "../../productTypes";
import { actionLabel, actionSummary } from "../../productCopy";
import { formatTime, statusLabel } from "../../presentation";

export function ApprovalList({
  items,
  state,
  selected,
  onSelect,
  onRetry,
}: {
  items: Approval[];
  state: "loading" | "ready" | "error";
  selected?: string;
  onSelect: (id: string) => void;
  onRetry: () => void;
}) {
  if (!items.length && state === "loading")
    return (
      <div className="empty-state" role="status">
        正在加载审批申请…
      </div>
    );
  if (!items.length && state === "error")
    return (
      <div className="empty-state" role="alert">
        <p>暂时无法加载审批申请。</p>
        <button type="button" onClick={onRetry}>
          重新加载审批申请
        </button>
      </div>
    );
  if (!items.length)
    return <div className="empty-state">当前租户没有审批申请。</div>;
  const sections = [
    {
      label: "待我处理",
      statuses: new Set(["pending"]),
    },
    {
      label: "执行中",
      statuses: new Set(["approved", "executing", "verification_pending"]),
    },
    {
      label: "最近完成",
      statuses: new Set(["executed", "rejected", "stale", "withdrawn", "failed"]),
    },
  ];
  const knownStatuses = new Set(
    sections.flatMap((section) => [...section.statuses]),
  );
  const unknownStatuses = new Set(
    items
      .map((item) => item.status)
      .filter((status) => !knownStatuses.has(status)),
  );
  if (unknownStatuses.size)
    sections.push({
      label: "其他状态",
      statuses: unknownStatuses,
    });
  return sections.map((section) => {
    const scoped = items.filter((item) => section.statuses.has(item.status));
    if (!scoped.length) return null;
    return (
      <div className="approval-list-group" key={section.label}>
        <h2>
          {section.label} <span>{scoped.length}</span>
        </h2>
        {scoped.map((item) => (
          <button
            key={item.id}
            className={selected === item.id ? "selected" : ""}
            onClick={() => onSelect(item.id)}
          >
            <strong>{actionLabel(item.action_type)}</strong>
            <span>
              {item.resource_summary || actionSummary(item.action_type, {})}
            </span>
            <small>
              {statusLabel(item.status)} · {item.risk === "high" ? "高风险" : item.risk}
            </small>
            <small>
              来源会话 {item.source_label ?? item.ticket_id} ·{" "}
              {formatTime(item.created_at)}
            </small>
          </button>
        ))}
      </div>
    );
  });
}
