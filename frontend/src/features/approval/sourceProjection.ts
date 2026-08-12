import type {
  ApprovalSource,
  ApprovalSourceMessage,
} from "../../productTypes";

function compareSourceMessages(
  left: Pick<ApprovalSourceMessage, "sequence" | "id">,
  right: Pick<ApprovalSourceMessage, "sequence" | "id">,
) {
  return left.sequence - right.sequence || left.id.localeCompare(right.id);
}

function sameSourceMessage(
  left: ApprovalSourceMessage,
  right: ApprovalSourceMessage,
) {
  return (
    left.id === right.id &&
    left.turn_id === right.turn_id &&
    left.kind === right.kind &&
    left.role === right.role &&
    left.content === right.content &&
    left.sequence === right.sequence &&
    left.is_origin_turn === right.is_origin_turn &&
    left.created_at === right.created_at
  );
}

function normalizeSourceMessages(
  messages: ApprovalSourceMessage[],
): ApprovalSourceMessage[] | null {
  const byId = new Map<string, ApprovalSourceMessage>();
  for (const message of messages) {
    if (
      !message.id ||
      !message.turn_id ||
      !Number.isInteger(message.sequence) ||
      message.sequence < 1
    )
      return null;
    const existing = byId.get(message.id);
    if (existing && !sameSourceMessage(existing, message)) return null;
    byId.set(message.id, message);
  }
  return [...byId.values()].sort(compareSourceMessages);
}

function sourceCursorIsValid(
  source: ApprovalSource,
  messages: ApprovalSourceMessage[],
) {
  const hasSequence = source.next_before_sequence !== null;
  const hasMessageId = source.next_before_message_id !== null;
  if (
    source.returned !== source.messages.length ||
    hasSequence !== hasMessageId ||
    source.has_more !== (hasSequence && hasMessageId)
  )
    return false;
  if (!source.has_more) return true;
  const first = messages[0];
  return Boolean(
    first &&
      first.sequence === source.next_before_sequence &&
      first.id === source.next_before_message_id,
  );
}

export function validateInitialSource(
  source: ApprovalSource,
  approvalId: string,
  ticketId: string,
): ApprovalSource | null {
  const messages = normalizeSourceMessages(source.messages);
  if (
    source.approval_id !== approvalId ||
    source.ticket_id !== ticketId ||
    !source.origin_turn_id ||
    !messages ||
    !sourceCursorIsValid(source, messages)
  )
    return null;
  const originMessages = messages.filter(
    (message) => message.turn_id === source.origin_turn_id,
  );
  if (
    originMessages.length === 0 ||
    originMessages.some((message) => !message.is_origin_turn) ||
    messages.some(
      (message) =>
        message.is_origin_turn && message.turn_id !== source.origin_turn_id,
    )
  )
    return null;
  const lastOrigin = originMessages[originMessages.length - 1];
  if (
    messages.some(
      (message) =>
        message.turn_id !== source.origin_turn_id &&
        compareSourceMessages(message, lastOrigin) > 0,
    )
  )
    return null;
  return { ...source, messages };
}

export function validateOlderSource(
  source: ApprovalSource,
  approvalId: string,
  ticketId: string,
  originTurnId: string,
  beforeSequence: number,
  beforeMessageId: string,
): ApprovalSource | null {
  const messages = normalizeSourceMessages(source.messages);
  const boundary = { sequence: beforeSequence, id: beforeMessageId };
  if (
    source.approval_id !== approvalId ||
    source.ticket_id !== ticketId ||
    source.origin_turn_id !== originTurnId ||
    !messages ||
    !sourceCursorIsValid(source, messages) ||
    messages.some(
      (message) =>
        message.is_origin_turn ||
        message.turn_id === originTurnId ||
        compareSourceMessages(message, boundary) >= 0,
    )
  )
    return null;
  return { ...source, messages };
}

export function mergeSourcePages(
  current: ApprovalSource,
  older: ApprovalSource,
): ApprovalSource | null {
  const messages = normalizeSourceMessages([
    ...older.messages,
    ...current.messages,
  ]);
  if (!messages) return null;
  return {
    ...current,
    messages,
    returned: messages.length,
    has_more: older.has_more,
    next_before_sequence: older.next_before_sequence,
    next_before_message_id: older.next_before_message_id,
  };
}
