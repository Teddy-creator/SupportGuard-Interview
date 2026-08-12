import type { FormEvent } from "react";

import {
  ConversationComposer,
  ConversationHeader,
  ConversationSidebar,
  MessageStream,
} from "../ConversationUi";
import { omitKey } from "../features/conversation/conversationState";
import { useConversationController } from "../features/conversation/useConversationController";
import { ProfileMenu } from "../features/session/ProfileMenu";
import type { SessionContext } from "../productTypes";
import { navigate } from "../routing";
import { TechnicalInspector } from "../TechnicalInspector";
import { NewConversationPage } from "./NewConversationPage";
export function ConversationPage({
  session,
  csrf,
  onSession,
  onSessionFailure,
}: {
  session: SessionContext;
  csrf: string;
  onSession: (csrf: string, session: SessionContext) => void;
  onSessionFailure: (message: string) => void;
}) {
  const {
    actionErrors,
    activity,
    busy,
    canAppend,
    closeInspector,
    closeProfile,
    closeSidebar,
    conversation,
    conversationScroll,
    draft,
    error,
    inspectTurn,
    inspector,
    inspectorLoading,
    inspectorOpen,
    inspectorToggle,
    list,
    listState,
    listLoadingMore,
    loadMoreConversations,
    loadOlderTurns,
    messageMutation,
    olderLoading,
    profileOpen,
    profileToggle,
    query,
    queryError,
    retryResource,
    retryList,
    resourceState,
    selectedId,
    setActionErrors,
    setDraft,
    setError,
    setQuery,
    setQueryError,
    openSidebar,
    sidebarOpen,
    sidebarToggle,
    stream,
    submit,
    switchRole,
    title,
    toggleInspector,
    toggleProfile,
    transitionLifecycle,
    unavailableResource,
    updateFollowLatestMessage,
    withdraw,
    withdrawing,
  } = useConversationController({
    session,
    csrf,
    onSession,
    onSessionFailure,
  });
  return (
    <div className={`app-layout ${inspectorOpen ? "with-inspector" : ""}`}>
      <ConversationSidebar
        session={session}
        items={list.items}
        selectedId={selectedId}
        query={query}
        mobileOpen={sidebarOpen}
        onQuery={setQuery}
        onNew={() => {
          messageMutation.reset();
          closeSidebar();
          navigate("/conversations/new");
        }}
        onOpen={(id) => {
          closeSidebar();
          navigate(`/conversations/${encodeURIComponent(id)}`);
        }}
        onClose={closeSidebar}
        connection={stream.connection}
        listState={listState}
        hasMore={Boolean(list.next_cursor)}
        loadingMore={listLoadingMore}
        onLoadMore={() => void loadMoreConversations()}
        onRetryList={retryList}
      />
      <main
        className="conversation-main"
        data-sse-ticket-id={selectedId ?? undefined}
        data-sse-state={stream.connection}
        data-sse-cursor={stream.cursor}
        data-sse-buffered-event-count={stream.bufferedEventCount}
        data-sse-buffered-event-unique-count={stream.bufferedEventUniqueCount}
        data-sse-buffer-limit={stream.bufferLimit}
      >
        <ConversationHeader
          title={title}
          activity={activity}
          inspectorOpen={inspectorOpen}
          inspectorButtonRef={inspectorToggle}
          sidebarButtonRef={sidebarToggle}
          profileButtonRef={profileToggle}
          session={session}
          onToggleInspector={toggleInspector}
          onOpenSidebar={openSidebar}
          onOpenProfile={toggleProfile}
        />
        {conversation ? (
          <div className="conversation-lifecycle">
            <button
              disabled={busy}
              onClick={() =>
                void transitionLifecycle(
                  conversation.lifecycle === "active" ? "archive" : "restore",
                )
              }
            >
              {conversation.lifecycle === "active" ? "归档对话" : "恢复对话"}
            </button>
          </div>
        ) : null}
        {profileOpen ? (
          <ProfileMenu
            session={session}
            onClose={closeProfile}
            onDemoRole={switchRole}
          />
        ) : null}
        {session.configured_runtime.mode === "fake" ? (
          <div className="test-runtime-banner">
            确定性测试模式 · 回答仅用于产品演示
          </div>
        ) : null}
        {stream.connection === "retrying" ? (
          <div className="connection-note" role="status">
            实时连接中断，正在尝试重新连接…
          </div>
        ) : null}
        {stream.connection === "polling" ? (
          <div className="connection-note" role="status">
            实时连接暂不可用，当前通过持久记录同步。
            <button type="button" onClick={stream.reconnect}>
              立即重连
            </button>
          </div>
        ) : null}
        {stream.connection === "error" ? (
          <div className="connection-note" role="status">
            无法建立实时连接。
            <button type="button" onClick={stream.reconnect}>
              重新连接
            </button>
          </div>
        ) : null}
        {error ? (
          <div className="safe-error" role="alert">
            {error}
            <button onClick={() => setError("")}>关闭</button>
          </div>
        ) : null}
        {queryError ? (
          <div className="safe-error" role="alert">
            {queryError}
            <button onClick={() => setQueryError("")}>关闭</button>
          </div>
        ) : null}
        <div
          className="conversation-scroll"
          ref={conversationScroll}
          onScroll={(event) => updateFollowLatestMessage(event.currentTarget)}
        >
          {selectedId ? (
            conversation ? (
              <MessageStream
                conversation={conversation}
                withdrawing={withdrawing}
                actionErrors={actionErrors}
                onWithdraw={withdraw}
                onDismissActionError={(action) =>
                  setActionErrors((current) => omitKey(current, action.id))
                }
                onInspectTurn={inspectTurn}
                hasOlder={Boolean(conversation.turn_pagination?.has_more)}
                loadingOlder={olderLoading}
                onLoadOlder={() => void loadOlderTurns()}
              />
            ) : unavailableResource ? (
              <div className="resource-state" role="alert">
                <span className="brand-mark">SG</span>
                <h2>
                  {resourceState === "forbidden"
                    ? "你没有权限查看这条对话"
                    : resourceState === "not_found"
                      ? "没有找到这条对话"
                      : "暂时无法加载这条对话"}
                </h2>
                <p>
                  {resourceState === "failed"
                    ? "服务暂时不可用。恢复前不会允许向未经确认的资源发送消息。"
                    : "该地址可能已失效，或资源不属于当前租户。这里不会允许继续发送消息。"}
                </p>
                <div>
                  {resourceState === "failed" ? (
                    <button
                      type="button"
                      onClick={retryResource}
                    >
                      重新加载
                    </button>
                  ) : (
                    <button
                      type="button"
                      onClick={() => navigate("/conversations/new")}
                    >
                      新建对话
                    </button>
                  )}
                  <button type="button" onClick={() => navigate("/conversations/new")}>
                    返回对话列表
                  </button>
                </div>
              </div>
            ) : (
              <div className="skeleton">正在恢复完整对话…</div>
            )
          ) : (
            <NewConversationPage session={session} onPick={setDraft} />
          )}
        </div>
        {unavailableResource ? null : conversation?.lifecycle === "archived" ? (
          <div className="archived-dock">
            <span>此对话已归档。</span>
            <button onClick={() => void transitionLifecycle("restore")}>
              恢复后继续
            </button>
            <button onClick={() => navigate("/conversations/new")}>
              新建对话
            </button>
          </div>
        ) : canAppend ? (
          <div className="composer-dock">
            <ConversationComposer
              value={draft}
              busy={busy}
              mode={conversation?.automation_mode ?? "agent"}
              isNew={!selectedId}
              onChange={setDraft}
              onSubmit={submit}
            />
            {messageMutation.retryable && error ? (
              <button
                className="retry-send"
                onClick={(event) => void submit(event as unknown as FormEvent)}
              >
                使用同一请求重试发送
              </button>
            ) : null}
            <p>SupportGuard 可能出错。高风险操作始终需要独立审批。</p>
          </div>
        ) : selectedId && resourceState === "loading" ? (
          <div className="composer-placeholder">正在确认对话状态…</div>
        ) : null}
      </main>
      <TechnicalInspector
        open={inspectorOpen}
        loading={inspectorLoading}
        data={inspector}
        onClose={closeInspector}
      />
    </div>
  );
}
