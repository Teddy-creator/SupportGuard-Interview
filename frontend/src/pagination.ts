export function appendUniqueById<T extends { id: string }>(
  current: T[],
  incoming: T[],
): T[] {
  const seen = new Set(current.map((item) => item.id));
  return [...current, ...incoming.filter((item) => !seen.has(item.id))];
}

export function prependUniqueById<T extends { id: string }>(
  current: T[],
  incoming: T[],
): T[] {
  const incomingIds = new Set(incoming.map((item) => item.id));
  return [...incoming, ...current.filter((item) => !incomingIds.has(item.id))];
}

export function mergeUniqueById<T extends { id: string }>(
  current: T[],
  incoming: T[],
): T[] {
  const incomingById = new Map(incoming.map((item) => [item.id, item]));
  return [
    ...current.map((item) => incomingById.get(item.id) ?? item),
    ...incoming.filter((item) => !current.some((existing) => existing.id === item.id)),
  ];
}
