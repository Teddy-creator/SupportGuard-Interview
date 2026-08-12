import type {
  ConversationListItem,
  SessionContext,
} from "../../productTypes";
import { activityLabel, formatTime } from "../../presentation";

export function ConversationSidebar({
  session,
  items,
  selectedId,
  query,
  mobileOpen,
  onQuery,
  onNew,
  onOpen,
  onClose,
  connection,
  listState,
  hasMore,
  loadingMore,
  onLoadMore,
  onRetryList,
}: {
  session: SessionContext;
  items: ConversationListItem[];
  selectedId: string | null;
  query: string;
  mobileOpen: boolean;
  onQuery: (value: string) => void;
  onNew: () => void;
  onOpen: (id: string) => void;
  onClose: () => void;
  connection:
    | "idle"
    | "connecting"
    | "live"
    | "retrying"
    | "polling"
    | "closed"
    | "error";
  listState: "loading" | "ready" | "error";
  hasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
  onRetryList: () => void;
}) {
  return (
    <aside
      className={`conversation-sidebar ${mobileOpen ? "open" : ""}`}
      aria-label="对话导航"
    >
      <div className="brand-block">
        <span className="brand-mark">SG</span>
        <strong>SupportGuard</strong>
        <button
          className="icon-button mobile-only"
          onClick={onClose}
          aria-label="关闭对话导航"
        >
          ×
        </button>
      </div>
      <div className="tenant-button" aria-label="当前租户">
        <span className="building-icon">▦</span>
        <span>{session.active_tenant.name}</span>
      </div>
      <button className="new-conversation" type="button" onClick={onNew}>
        ＋ 新建对话
      </button>
      <label className="conversation-search">
        <span aria-hidden="true">⌕</span>
        <input
          aria-label="搜索对话"
          value={query}
          onChange={(event) => onQuery(event.target.value)}
          placeholder="搜索对话"
        />
      </label>
      <nav className="primary-nav" aria-label="主要导航">
        <span className="selected">
          ▢ <span>对话</span>
        </span>
      </nav>
      <div className="recent-label">最近对话</div>
      <div className="conversation-list">
        {items.length === 0 && listState === "loading" ? (
          <p className="sidebar-empty" role="status">
            正在加载对话…
          </p>
        ) : items.length === 0 && listState === "error" ? (
          <div className="sidebar-empty" role="alert">
            <p>暂时无法加载对话。</p>
            <button type="button" onClick={onRetryList}>
              重新加载对话
            </button>
          </div>
        ) : items.length === 0 ? (
          <p className="sidebar-empty">
            {query ? "没有匹配的对话" : "还没有对话"}
          </p>
        ) : (
          items.map((item) => {
            const selected = item.id === selectedId;
            const secondaryText = selected
              ? activityLabel(item.activity_label) || "当前对话"
              : activityLabel(item.latest_summary || item.activity_label);

            return (
              <button
                key={item.id}
                className={
                  selected
                    ? "conversation-item selected"
                    : "conversation-item"
                }
                type="button"
                onClick={() => onOpen(item.id)}
                aria-current={selected ? "page" : undefined}
              >
                <strong>{item.title}</strong>
                <small>{secondaryText}</small>
                <time>{formatTime(item.updated_at)}</time>
              </button>
            );
          })
        )}
        {items.length > 0 && listState === "loading" ? (
          <p className="sidebar-list-note" role="status">
            正在刷新对话列表…
          </p>
        ) : null}
        {items.length > 0 && listState === "error" ? (
          <div className="sidebar-list-note" role="status">
            对话列表刷新失败，仍显示上次结果。
            <button type="button" onClick={onRetryList}>
              重试
            </button>
          </div>
        ) : null}
        {hasMore ? (
          <button
            className="load-more"
            type="button"
            disabled={loadingMore}
            onClick={onLoadMore}
          >
            {loadingMore ? "正在加载…" : "加载更多对话"}
          </button>
        ) : null}
      </div>
      <div className={`connection-state ${connection}`} role="status">
        <span className="online-dot" aria-hidden="true" />
        {connection === "live"
          ? "服务已连接"
          : connection === "retrying"
            ? "正在重新连接"
            : connection === "polling"
              ? "持久记录同步中"
            : connection === "error"
              ? "实时连接不可用"
              : connection === "closed"
                ? "实时连接已关闭"
              : selectedId
                ? "正在连接"
                : "选择对话后连接"}
      </div>
    </aside>
  );
}
