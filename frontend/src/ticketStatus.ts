export function isTicketSettled(ticket: { status: string } | null): boolean {
  if (!ticket) return false;
  return ["resolved", "rejected", "manual_takeover", "failed"].includes(
    ticket.status,
  );
}
