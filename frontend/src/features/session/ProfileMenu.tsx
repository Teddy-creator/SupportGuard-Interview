import type { SessionContext } from "../../productTypes";

export function ProfileMenu({
  session,
  onClose,
  onDemoRole,
}: {
  session: SessionContext;
  onClose: () => void;
  onDemoRole: (role: "customer" | "approver") => void;
}) {
  return (
    <div className="profile-menu" role="dialog" aria-label="当前身份">
      <button
        className="profile-close"
        onClick={onClose}
        aria-label="关闭身份信息"
      >
        ×
      </button>
      <strong>{session.principal.display_name}</strong>
      <dl>
        <div>
          <dt>角色</dt>
          <dd>{session.principal.role === "customer" ? "客户" : "审批者"}</dd>
        </div>
        <div>
          <dt>租户</dt>
          <dd>{session.active_tenant.name}</dd>
        </div>
        <div>
          <dt>认证</dt>
          <dd>
            {session.auth_mode === "development" ? "演示会话" : "生产身份"}
          </dd>
        </div>
      </dl>
      {session.auth_mode === "development" ? (
        <div className="demo-switch">
          <span>演示身份</span>
          <button
            onClick={() =>
              onDemoRole(
                session.principal.role === "customer" ? "approver" : "customer",
              )
            }
          >
            切换为{session.principal.role === "customer" ? "审批者" : "客户"}
          </button>
        </div>
      ) : null}
    </div>
  );
}

