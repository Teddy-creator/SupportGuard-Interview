import { useCallback, useEffect, useRef, useState } from "react";

import type {
  ConversationDetail,
  ConversationTurn,
} from "../../productTypes";
import { latestInspectableTurn } from "./conversationState";

export type InspectorSelection = {
  turnId: string;
  messageId: string;
  runId: string;
};

export function useConversationViewState({
  selectedId,
  conversation,
  conversationScroll,
  streamCursor,
}: {
  selectedId: string | null;
  conversation: ConversationDetail | null;
  conversationScroll: React.RefObject<HTMLDivElement | null>;
  streamCursor: number;
}) {
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [inspectorSelection, setInspectorSelection] =
    useState<InspectorSelection | null>(null);
  const [profileOpen, setProfileOpen] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const inspectorToggle = useRef<HTMLButtonElement | null>(null);
  const inspectorReturnFocus = useRef<HTMLButtonElement | null>(null);
  const profileToggle = useRef<HTMLButtonElement | null>(null);
  const sidebarToggle = useRef<HTMLButtonElement | null>(null);
  const followLatestMessage = useRef(true);

  const closeInspector = useCallback(() => {
    setInspectorOpen(false);
    const returnFocus = inspectorReturnFocus.current;
    inspectorReturnFocus.current = null;
    if (returnFocus?.isConnected) returnFocus.focus();
  }, []);

  const closeProfile = useCallback(() => {
    setProfileOpen(false);
    if (profileToggle.current?.isConnected) profileToggle.current.focus();
  }, []);

  const closeSidebar = useCallback(() => {
    setSidebarOpen(false);
    if (sidebarToggle.current?.isConnected) sidebarToggle.current.focus();
  }, []);

  const clearScope = useCallback(() => {
    setInspectorSelection(null);
    setInspectorOpen(false);
    setProfileOpen(false);
    setSidebarOpen(false);
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    clearScope();
  }, [clearScope, selectedId]);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (inspectorOpen) {
        event.preventDefault();
        closeInspector();
      } else if (profileOpen) {
        event.preventDefault();
        closeProfile();
      } else if (sidebarOpen) {
        event.preventDefault();
        closeSidebar();
      }
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [
    closeInspector,
    closeProfile,
    closeSidebar,
    inspectorOpen,
    profileOpen,
    sidebarOpen,
  ]);

  useEffect(() => {
    followLatestMessage.current = true;
    window.requestAnimationFrame(() => {
      const node = conversationScroll.current;
      if (!node || !followLatestMessage.current) return;
      node.scrollTop = selectedId ? node.scrollHeight : 0;
    });
  }, [conversationScroll, selectedId]);

  useEffect(() => {
    if (!selectedId || !followLatestMessage.current) return;
    window.requestAnimationFrame(() => {
      const node = conversationScroll.current;
      if (node && followLatestMessage.current) node.scrollTop = node.scrollHeight;
    });
  }, [
    conversation?.turns.length,
    conversation?.updated_at,
    conversationScroll,
    selectedId,
    streamCursor,
  ]);

  const inspectTurn = useCallback(
    (
      turn: ConversationTurn,
      messageId: string,
      trigger?: HTMLButtonElement,
    ) => {
      if (!turn.run_id || !turn.run) return;
      inspectorReturnFocus.current = trigger ?? inspectorToggle.current;
      setInspectorSelection({
        turnId: turn.id,
        messageId,
        runId: turn.run_id,
      });
      setInspectorOpen(true);
    },
    [],
  );

  const toggleInspector = useCallback(() => {
    if (inspectorOpen) {
      closeInspector();
      return;
    }
    inspectorReturnFocus.current = inspectorToggle.current;
    const latest = latestInspectableTurn(conversation);
    if (latest) inspectTurn(latest.turn, latest.messageId);
    else {
      setInspectorSelection(null);
      setInspectorOpen(true);
    }
  }, [closeInspector, conversation, inspectTurn, inspectorOpen]);

  const toggleProfile = useCallback(() => {
    setProfileOpen((value) => !value);
  }, []);

  const updateFollowLatestMessage = useCallback((node: HTMLDivElement) => {
    followLatestMessage.current =
      node.scrollHeight - node.scrollTop - node.clientHeight <= 80;
  }, []);

  return {
    inspectorOpen,
    inspectorSelection,
    inspectorToggle,
    profileOpen,
    profileToggle,
    sidebarOpen,
    sidebarToggle,
    closeInspector,
    closeProfile,
    closeSidebar,
    clearScope,
    inspectTurn,
    toggleInspector,
    toggleProfile,
    openSidebar: () => setSidebarOpen(true),
    updateFollowLatestMessage,
  };
}
