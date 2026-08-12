import {
  type MutableRefObject,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import {
  appendUniqueById,
  mergeUniqueById,
  prependUniqueById,
} from "../../pagination";
import {
  api,
  errorMessage,
  isAbortError,
  ProductApiError,
} from "../../productApi";
import type {
  ConversationDetail,
  ConversationPage,
} from "../../productTypes";
import { mergeActions, reconcileConversationSummary } from "./conversationState";

export type ConversationResourceState =
  | "idle"
  | "loading"
  | "ready"
  | "not_found"
  | "forbidden"
  | "failed";

const REMOTE_ACTION_RECONCILE_INTERVAL_MS = 1_500;
const REMOTE_ACTION_STATUSES = new Set([
  "pending",
  "approved",
  "executing",
  "verification_pending",
]);

export function useConversationQuery({
  selectedId,
  tenantId,
  scopeTransitioning,
}: {
  selectedId: string | null;
  tenantId: string;
  scopeTransitioning: MutableRefObject<boolean>;
}) {
  const [list, setList] = useState<ConversationPage>({ items: [] });
  const [listState, setListState] = useState<"loading" | "ready" | "error">(
    "loading",
  );
  const [conversation, setConversation] = useState<ConversationDetail | null>(
    null,
  );
  const [resourceState, setResourceState] =
    useState<ConversationResourceState>(selectedId ? "loading" : "idle");
  const [resourceEpoch, setResourceEpoch] = useState(0);
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");
  const [listLoadingMore, setListLoadingMore] = useState(false);
  const [olderLoading, setOlderLoading] = useState(false);
  const listRequestEpoch = useRef(0);
  const conversationRequestEpoch = useRef(0);
  const conversationScroll = useRef<HTMLDivElement | null>(null);
  const activeScope = useRef({ tenantId, conversationId: selectedId, query });

  useEffect(() => {
    activeScope.current = { tenantId, conversationId: selectedId, query };
  }, [query, selectedId, tenantId]);

  useEffect(
    () => () => {
      conversationRequestEpoch.current += 1;
      listRequestEpoch.current += 1;
    },
    [],
  );

  const loadList = useCallback(
    async (signal?: AbortSignal, cursor?: string | null, append = false) => {
      if (scopeTransitioning.current) return;
      const requestTenantId = tenantId;
      const requestQuery = query;
      const requestEpoch = ++listRequestEpoch.current;
      if (!append) setListState("loading");
      const params = new URLSearchParams();
      if (query.trim()) params.set("query", query.trim());
      if (cursor) params.set("cursor", cursor);
      let page: ConversationPage;
      try {
        page = await api<ConversationPage>(
          `/conversations${params.size ? `?${params}` : ""}`,
          { signal },
        );
      } catch (value) {
        if (
          !append &&
          !isAbortError(value) &&
          listRequestEpoch.current === requestEpoch &&
          activeScope.current.tenantId === requestTenantId &&
          activeScope.current.query === requestQuery
        )
          setListState("error");
        throw value;
      }
      if (
        listRequestEpoch.current !== requestEpoch ||
        activeScope.current.tenantId !== requestTenantId ||
        activeScope.current.query !== requestQuery
      )
        return;
      setList((current) =>
        append
          ? {
              items: appendUniqueById(current.items, page.items),
              next_cursor: page.next_cursor,
            }
          : page,
      );
      if (!append) setListState("ready");
      setError("");
    },
    [query, scopeTransitioning, tenantId],
  );

  const loadConversation = useCallback(
    async (
      id = selectedId,
      signal?: AbortSignal,
      before?: number | null,
      prepend = false,
    ) => {
      if (!id || scopeTransitioning.current) return;
      const requestTenantId = tenantId;
      const requestEpoch =
        before === undefined || before === null
          ? ++conversationRequestEpoch.current
          : null;
      const suffix = before ? `?before_turn=${before}` : "";
      const page = await api<ConversationDetail>(
        `/conversations/${encodeURIComponent(id)}${suffix}`,
        { signal },
      );
      if (
        (requestEpoch !== null &&
          conversationRequestEpoch.current !== requestEpoch) ||
        activeScope.current.tenantId !== requestTenantId ||
        activeScope.current.conversationId !== id
      )
        return;
      setConversation((current) =>
        prepend && current
          ? {
              ...current,
              turns: prependUniqueById(current.turns, page.turns),
              turn_pagination: page.turn_pagination,
            }
          : current?.id === page.id
            ? {
                ...page,
                turns: mergeUniqueById(current.turns, page.turns).sort(
                  (left, right) => left.ordinal - right.ordinal,
                ),
                pending_actions: mergeActions(
                  current.pending_actions,
                  page.pending_actions,
                ),
                turn_pagination: current.turn_pagination,
              }
            : page,
      );
      setResourceState("ready");
      setError("");
    },
    [scopeTransitioning, selectedId, tenantId],
  );

  const loadMoreConversations = useCallback(async () => {
    if (!list.next_cursor || listLoadingMore) return;
    setListLoadingMore(true);
    try {
      await loadList(undefined, list.next_cursor, true);
    } catch (value) {
      setError(errorMessage(value));
    } finally {
      setListLoadingMore(false);
    }
  }, [list.next_cursor, listLoadingMore, loadList]);

  const retryList = useCallback(() => {
    setError("");
    void loadList().catch((value) => {
      if (!isAbortError(value)) setError(errorMessage(value));
    });
  }, [loadList]);

  const loadOlderTurns = useCallback(async () => {
    const before = conversation?.turn_pagination?.next_before_ordinal;
    if (!selectedId || !before || olderLoading) return;
    const node = conversationScroll.current;
    const previousScrollHeight = node?.scrollHeight ?? 0;
    const previousScrollTop = node?.scrollTop ?? 0;
    setOlderLoading(true);
    try {
      await loadConversation(selectedId, undefined, before, true);
      window.requestAnimationFrame(() => {
        if (!node) return;
        node.scrollTop =
          previousScrollTop +
          Math.max(0, node.scrollHeight - previousScrollHeight);
      });
    } catch (value) {
      setError(errorMessage(value));
    } finally {
      setOlderLoading(false);
    }
  }, [conversation, loadConversation, olderLoading, selectedId]);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(
      () =>
        void loadList(controller.signal).catch((value) => {
          if (!isAbortError(value)) setError(errorMessage(value));
        }),
      query ? 220 : 0,
    );
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [loadList, query, tenantId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setConversation(null);
    setError("");
    setResourceState(selectedId ? "loading" : "idle");
    if (!selectedId) return;
    const controller = new AbortController();
    void loadConversation(selectedId, controller.signal).catch((value) => {
      if (isAbortError(value)) return;
      if (value instanceof ProductApiError && value.status === 404)
        setResourceState("not_found");
      else if (value instanceof ProductApiError && value.status === 403)
        setResourceState("forbidden");
      else {
        setResourceState("failed");
        setError(errorMessage(value));
      }
    });
    return () => controller.abort();
  }, [loadConversation, resourceEpoch, selectedId, tenantId]);

  const remoteActionMayChange =
    conversation?.pending_actions.some((action) =>
      REMOTE_ACTION_STATUSES.has(action.status),
    ) ?? false;
  useEffect(() => {
    if (!selectedId || !remoteActionMayChange || scopeTransitioning.current)
      return;
    const controller = new AbortController();
    let requestRunning = false;
    const reconcile = () => {
      if (requestRunning || controller.signal.aborted) return;
      requestRunning = true;
      void loadConversation(selectedId, controller.signal)
        // Keep the last authoritative projection visible. The SSE owner and
        // the next bounded reconciliation attempt remain available.
        .catch(() => undefined)
        .finally(() => {
          requestRunning = false;
        });
    };
    const timer = window.setInterval(
      reconcile,
      REMOTE_ACTION_RECONCILE_INTERVAL_MS,
    );
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [loadConversation, remoteActionMayChange, scopeTransitioning, selectedId]);

  const clearScope = useCallback(() => {
    conversationRequestEpoch.current += 1;
    listRequestEpoch.current += 1;
    setConversation(null);
    setList({ items: [] });
    setListState("loading");
  }, []);

  const presentedList = conversation
    ? {
        ...list,
        items: reconcileConversationSummary(list.items, conversation),
      }
    : list;

  return {
    list: presentedList,
    listState,
    listLoadingMore,
    conversation,
    conversationScroll,
    resourceState,
    olderLoading,
    query,
    error,
    activeScope,
    setQuery,
    setError,
    retryList,
    loadList,
    loadConversation,
    loadMoreConversations,
    loadOlderTurns,
    retryResource: () => setResourceEpoch((value) => value + 1),
    clearScope,
  };
}
