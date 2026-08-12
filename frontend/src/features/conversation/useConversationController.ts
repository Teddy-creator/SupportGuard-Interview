import { useCallback, useEffect, useRef } from "react";

import { errorMessage } from "../../productApi";
import type { SessionContext } from "../../productTypes";
import { useRoute } from "../../routing";
import { useTicketStream } from "../../useTicketStream";
import { useConversationInspectorQuery } from "./useConversationInspectorQuery";
import { useConversationMutation } from "./useConversationMutation";
import { useConversationQuery } from "./useConversationQuery";
import { useConversationViewState } from "./useConversationViewState";

export function useConversationController({
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
  const route = useRoute();
  const selectedId = route.page === "conversation" ? route.id : null;
  const scopeTransitioning = useRef(false);
  const queryState = useConversationQuery({
    selectedId,
    tenantId: session.active_tenant.id,
    scopeTransitioning,
  });
  const {
    conversation,
    conversationScroll,
    error: queryError,
    list,
    listLoadingMore,
    listState,
    loadConversation,
    loadList,
    loadMoreConversations,
    loadOlderTurns,
    olderLoading,
    query,
    resourceState,
    retryList,
    setError: setQueryError,
    setQuery,
  } = queryState;
  const refreshTimer = useRef<number | null>(null);
  useEffect(
    () => () => {
      if (refreshTimer.current !== null) {
        window.clearTimeout(refreshTimer.current);
        refreshTimer.current = null;
      }
    },
    [],
  );

  const onStreamEvent = useCallback(() => {
    if (refreshTimer.current !== null)
      window.clearTimeout(refreshTimer.current);
    refreshTimer.current = window.setTimeout(() => {
      void Promise.all([loadConversation(), loadList()])
        .then(() => setQueryError(""))
        .catch((value) => setQueryError(errorMessage(value)));
    }, 100);
  }, [loadConversation, loadList, setQueryError]);
  const stream = useTicketStream(
    selectedId
      ? {
          principalId: session.principal.id,
          tenantId: session.active_tenant.id,
          ticketId: selectedId,
        }
      : undefined,
    onStreamEvent,
    Boolean(
      selectedId &&
        resourceState === "ready" &&
        conversation?.lifecycle === "active",
    ),
  );
  const view = useConversationViewState({
    selectedId,
    conversation,
    conversationScroll,
    streamCursor: stream.cursor,
  });
  const inspectorQuery = useConversationInspectorQuery({
    open: view.inspectorOpen,
    selectedId,
    selection: view.inspectorSelection,
    conversation,
    streamCursor: stream.cursor,
    onError: setQueryError,
  });
  const mutation = useConversationMutation({
    selectedId,
    session,
    csrf,
    onSession,
    onSessionFailure,
    scopeTransitioning,
    loadConversation,
    loadList,
    clearQueryScope: queryState.clearScope,
    retryResource: queryState.retryResource,
    clearViewScope: view.clearScope,
  });
  const {
    actionErrors,
    busy,
    draft,
    error,
    messageMutation,
    setActionErrors,
    setDraft,
    setError,
    submit,
    switchRole,
    transitionLifecycle,
    withdraw,
    withdrawing,
  } = mutation;

  useEffect(() => {
    if (refreshTimer.current !== null) {
      window.clearTimeout(refreshTimer.current);
      refreshTimer.current = null;
    }
  }, [selectedId, session.active_tenant.id]);

  const title = selectedId
    ? resourceState === "not_found"
      ? "对话不存在"
      : resourceState === "forbidden"
        ? "无权查看此对话"
        : resourceState === "failed"
          ? "暂时无法加载对话"
        : (conversation?.title ?? "正在载入对话…")
    : "新对话";
  const activity = conversation?.activity_label;
  const invalidResource =
    resourceState === "not_found" || resourceState === "forbidden";
  const unavailableResource = invalidResource || resourceState === "failed";
  const canAppend =
    !selectedId ||
    (resourceState === "ready" &&
      conversation?.allowed_actions.includes("append_message"));
  return {
    actionErrors,
    activity,
    busy,
    canAppend,
    closeInspector: view.closeInspector,
    conversation,
    conversationScroll,
    draft,
    error,
    inspectTurn: view.inspectTurn,
    inspector: inspectorQuery.inspector,
    inspectorLoading: inspectorQuery.loading,
    inspectorOpen: view.inspectorOpen,
    inspectorToggle: view.inspectorToggle,
    list,
    listState,
    listLoadingMore,
    loadMoreConversations,
    loadOlderTurns,
    messageMutation,
    olderLoading,
    profileOpen: view.profileOpen,
    profileToggle: view.profileToggle,
    query,
    queryError,
    retryList,
    resourceState,
    selectedId,
    setActionErrors,
    setDraft,
    setError,
    closeProfile: view.closeProfile,
    setQuery,
    setQueryError,
    retryResource: queryState.retryResource,
    closeSidebar: view.closeSidebar,
    openSidebar: view.openSidebar,
    sidebarOpen: view.sidebarOpen,
    sidebarToggle: view.sidebarToggle,
    stream,
    submit,
    switchRole,
    title,
    toggleInspector: view.toggleInspector,
    toggleProfile: view.toggleProfile,
    transitionLifecycle,
    unavailableResource,
    updateFollowLatestMessage: view.updateFollowLatestMessage,
    withdraw,
    withdrawing,
  };
}
