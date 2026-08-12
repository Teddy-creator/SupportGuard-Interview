import { useEffect, useRef, useState } from "react";

import { api, errorMessage, isAbortError } from "../../productApi";
import type {
  ConversationDetail,
  TurnInspector,
} from "../../productTypes";
import type { InspectorSelection } from "./useConversationViewState";

export function useConversationInspectorQuery({
  open,
  selectedId,
  selection,
  conversation,
  streamCursor,
  onError,
}: {
  open: boolean;
  selectedId: string | null;
  selection: InspectorSelection | null;
  conversation: ConversationDetail | null;
  streamCursor: number;
  onError: (message: string) => void;
}) {
  const [inspector, setInspector] = useState<TurnInspector | null>(null);
  const [loading, setLoading] = useState(false);
  const requestEpoch = useRef(0);

  useEffect(
    () => () => {
      requestEpoch.current += 1;
    },
    [],
  );

  useEffect(() => {
    if (!open || !selectedId || !selection) {
      requestEpoch.current += 1;
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setInspector(null);
      setLoading(false);
      return;
    }
    const selectedTurn = conversation?.turns.find(
      (turn) =>
        turn.id === selection.turnId && turn.run_id === selection.runId,
    );
    const selectedMessage = selectedTurn?.messages.find(
      (message) =>
        message.id === selection.messageId && message.kind === "assistant",
    );
    if (!selectedTurn?.run || !selectedMessage) {
      setInspector(null);
      setLoading(false);
      return;
    }
    const epoch = ++requestEpoch.current;
    const requestConversationId = selectedId;
    const controller = new AbortController();
    setLoading(true);
    const params = new URLSearchParams({
      conversation_id: requestConversationId,
      turn_id: selection.turnId,
      message_id: selection.messageId,
    });
    void api<TurnInspector>(
      `/runs/${encodeURIComponent(selection.runId)}/inspector?${params}`,
      { signal: controller.signal },
    )
      .then((result) => {
        if (requestEpoch.current !== epoch) return;
        if (
          result.message_id !== selection.messageId ||
          result.turn_id !== selection.turnId ||
          result.run_id !== selection.runId ||
          result.run.id !== selection.runId ||
          result.timeline.some((event) => event.run_id !== selection.runId) ||
          [...result.knowledge_sources, ...result.business_facts].some(
            (citation) => citation.message_id !== selection.messageId,
          )
        ) {
          setInspector(null);
          onError("技术记录与所选消息不一致，已阻止显示。请刷新后重试。");
          return;
        }
        setInspector(result);
        onError("");
      })
      .catch((value) => {
        if (!isAbortError(value)) onError(errorMessage(value));
      })
      .finally(() => {
        if (requestEpoch.current === epoch) setLoading(false);
      });
    return () => {
      requestEpoch.current += 1;
      controller.abort();
    };
  }, [conversation, onError, open, selectedId, selection, streamCursor]);

  return { inspector, loading };
}
