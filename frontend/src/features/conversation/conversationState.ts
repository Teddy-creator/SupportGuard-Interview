import type {
  ConversationDetail,
  ConversationListItem,
  ConversationTurn,
  ProductAction,
} from "../../productTypes";

export function reconcileConversationSummary(
  items: ConversationListItem[],
  detail: ConversationDetail,
): ConversationListItem[] {
  return items.map((item) =>
    item.id === detail.id
      ? {
          ...item,
          title: detail.title,
          activity_label: detail.activity_label,
        }
      : item,
  );
}

export function omitKey(
  values: Record<string, string>,
  key: string,
): Record<string, string> {
  if (!(key in values)) return values;
  const next = { ...values };
  delete next[key];
  return next;
}

const actionStatusRank: Record<string, number> = {
  pending: 1,
  approved: 2,
  executing: 3,
  verification_pending: 4,
  executed: 5,
  rejected: 5,
  stale: 5,
  withdrawn: 5,
  failed: 5,
};

export function mergeActions(
  current: ProductAction[],
  incoming: ProductAction[],
): ProductAction[] {
  const prior = new Map(current.map((item) => [item.id, item]));
  const merged = incoming.map((item) => {
    const previous = prior.get(item.id);
    if (!previous) return item;
    const previousVersion = previous.status_version ?? 0;
    const nextVersion = item.status_version ?? 0;
    if (nextVersion < previousVersion) return previous;
    if (
      nextVersion === previousVersion &&
      (actionStatusRank[item.status] ?? 0) <
        (actionStatusRank[previous.status] ?? 0)
    )
      return previous;
    return item;
  });
  const incomingIds = new Set(incoming.map((item) => item.id));
  return [
    ...merged,
    ...current.filter((item) => !incomingIds.has(item.id)),
  ];
}

export function latestInspectableTurn(
  conversation: ConversationDetail | null,
): { turn: ConversationTurn; messageId: string } | null {
  if (!conversation) return null;
  for (const turn of [...conversation.turns].reverse()) {
    if (!turn.run_id || !turn.run) continue;
    const message = [...turn.messages]
      .reverse()
      .find((item) => item.kind === "assistant");
    if (message) return { turn, messageId: message.id };
  }
  return null;
}
