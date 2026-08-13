import type { Ref } from "react";

import type { SessionContext } from "../../productTypes";
import { activityLabel } from "../../presentation";

export function ConversationHeader({
  title,
  activity,
  inspectorOpen,
  inspectorButtonRef,
  sidebarButtonRef,
  profileButtonRef,
  session,
  onToggleInspector,
  onOpenSidebar,
  onOpenProfile,
  inspectorAvailable = true,
}: {
  title: string;
  activity?: string;
  inspectorOpen: boolean;
  inspectorButtonRef?: Ref<HTMLButtonElement>;
  sidebarButtonRef?: Ref<HTMLButtonElement>;
  profileButtonRef?: Ref<HTMLButtonElement>;
  session: SessionContext;
  onToggleInspector: () => void;
  onOpenSidebar: () => void;
  onOpenProfile: () => void;
  inspectorAvailable?: boolean;
}) {
  return (
    <header className="conversation-header">
      <button
        ref={sidebarButtonRef}
        className="icon-button mobile-only"
        onClick={onOpenSidebar}
        aria-label="打开对话导航"
      >
        ☰
      </button>
      <div className="conversation-title">
        <h1>{title}</h1>
        {activity ? (
          <span className="activity-badge">{activityLabel(activity)}</span>
        ) : null}
      </div>
      <button
        ref={inspectorButtonRef}
        className={
          inspectorOpen ? "inspector-toggle active" : "inspector-toggle"
        }
        onClick={onToggleInspector}
        disabled={!inspectorAvailable}
        aria-controls="technical-inspector"
        aria-expanded={inspectorOpen}
        title={
          inspectorAvailable
            ? "查看所选回答的持久化运行事实"
            : "发送消息并选择一条回答后即可查看运行详情"
        }
      >
        <span aria-hidden="true">&lt;/&gt;</span> 运行详情
      </button>
      <button
        ref={profileButtonRef}
        className="profile-button"
        type="button"
        onClick={onOpenProfile}
      >
        <span className="avatar">
          {session.principal.display_name.slice(0, 1).toUpperCase()}
        </span>
        <span className="profile-copy">
          <strong>{session.principal.display_name}</strong>
          <small>
            {session.principal.role === "customer" ? "客户" : "审批者"}
          </small>
        </span>
        <span aria-hidden="true">⌄</span>
      </button>
    </header>
  );
}
