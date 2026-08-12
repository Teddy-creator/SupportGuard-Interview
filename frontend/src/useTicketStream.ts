import { useEffect, useRef, useState } from "react";

import { bearerHeaders } from "./auth";

export type StreamEvent = {
  ticket_sequence: number;
  run_id: string;
  run_sequence: number;
  event_type: string;
  status: string;
  payload: Record<string, unknown>;
  created_at: string;
  tool_round?: number;
  tool_call_id?: string;
};

type Connection =
  | "idle"
  | "connecting"
  | "live"
  | "retrying"
  | "polling"
  | "closed"
  | "error";

const MAX_BUFFERED_EVENTS = 64;

export type TicketStreamScope = {
  principalId: string;
  tenantId: string;
  ticketId: string;
};

function streamKey(scope: TicketStreamScope): string {
  return `${scope.principalId}:${scope.tenantId}:${scope.ticketId}`;
}

function cursorKey(scope: TicketStreamScope): string {
  return `supportguard:sse-cursor:${streamKey(scope)}`;
}

function readCursor(scope: TicketStreamScope): number {
  try {
    const value = Number.parseInt(
      window.sessionStorage.getItem(cursorKey(scope)) ?? "0",
      10,
    );
    return Number.isFinite(value) && value > 0 ? value : 0;
  } catch {
    return 0;
  }
}

function rememberCursor(scope: TicketStreamScope, cursor: number) {
  try {
    window.sessionStorage.setItem(cursorKey(scope), String(cursor));
  } catch {
    // A blocked storage API must not disable the durable SSE reconnect path.
  }
}

export function useTicketStream(
  scope: TicketStreamScope | undefined,
  onEvent: () => void,
  enabled = true,
) {
  const [stream, setStream] = useState<{
    scopeKey: string;
    events: StreamEvent[];
    cursor: number;
  }>({ scopeKey: "", events: [], cursor: 0 });
  const [connection, setConnection] = useState<{
    scopeKey: string;
    value: Connection;
  }>({ scopeKey: "", value: "idle" });
  const [restartEpoch, setRestartEpoch] = useState(0);
  const principalId = scope?.principalId;
  const tenantId = scope?.tenantId;
  const ticketId = scope?.ticketId;
  const activeScopeKey =
    principalId && tenantId && ticketId
      ? `${principalId}:${tenantId}:${ticketId}`
      : "";
  const callback = useRef(onEvent);
  useEffect(() => {
    callback.current = onEvent;
  }, [onEvent]);

  useEffect(() => {
    if (!principalId || !tenantId || !ticketId || !enabled) {
      return;
    }
    const activeScope = { principalId, tenantId, ticketId };
    const activeTicketId = ticketId;
    const activeKey = streamKey(activeScope);
    const lifetime = new AbortController();
    let request: AbortController | null = null;
    let cursor = readCursor(activeScope);
    let failures = 0;
    let stableTimer: number | null = null;
    let pollingTimer: number | null = null;

    const waitUntilOnline = async () => {
      if (navigator.onLine) return;
      await new Promise<void>((resolve) => {
        const finish = () => {
          window.removeEventListener("online", finish);
          lifetime.signal.removeEventListener("abort", finish);
          resolve();
        };
        window.addEventListener("online", finish, { once: true });
        lifetime.signal.addEventListener("abort", finish, { once: true });
      });
    };
    const onOffline = () => {
      setConnection({ scopeKey: activeKey, value: "retrying" });
      request?.abort();
    };
    window.addEventListener("offline", onOffline);

    const waitForBackoff = async (milliseconds: number) =>
      new Promise<void>((resolve) => {
        const timer = window.setTimeout(finish, milliseconds);
        function finish() {
          lifetime.signal.removeEventListener("abort", finish);
          window.clearTimeout(timer);
          resolve();
        }
        lifetime.signal.addEventListener("abort", finish, { once: true });
      });

    const startPolling = () => {
      setConnection({ scopeKey: activeKey, value: "polling" });
      callback.current();
      pollingTimer = window.setInterval(() => callback.current(), 2500);
    };

    async function follow() {
      while (!lifetime.signal.aborted && failures <= 6) {
        await waitUntilOnline();
        if (lifetime.signal.aborted) return;
        setConnection({
          scopeKey: activeKey,
          value: failures ? "retrying" : "connecting",
        });
        try {
          request = new AbortController();
          const response = await fetch(
            `/api/tickets/${activeTicketId}/events/stream`,
            {
              credentials: "same-origin",
              headers: { ...bearerHeaders(), "Last-Event-ID": String(cursor) },
              signal: request.signal,
            },
          );
          if ([401, 403, 404, 422].includes(response.status)) {
            setConnection({ scopeKey: activeKey, value: "error" });
            return;
          }
          if (!response.ok || !response.body)
            throw new Error(`SSE ${response.status}`);
          stableTimer = window.setTimeout(() => {
            failures = 0;
            setConnection({ scopeKey: activeKey, value: "live" });
          }, 3000);
          const reader = response.body.getReader();
          const decoder = new TextDecoder();
          let buffer = "";
          while (!lifetime.signal.aborted && !request.signal.aborted) {
            const chunk = await reader.read();
            if (chunk.done) break;
            buffer += decoder.decode(chunk.value, { stream: true });
            const frames = buffer.split("\n\n");
            buffer = frames.pop() ?? "";
            for (const frame of frames) {
              const data = frame
                .split("\n")
                .find((line) => line.startsWith("data: "));
              if (!data) continue;
              const event = JSON.parse(data.slice(6)) as StreamEvent;
              if (
                !Number.isInteger(event.ticket_sequence) ||
                event.ticket_sequence <= cursor
              )
                continue;
              failures = 0;
              if (stableTimer !== null) {
                window.clearTimeout(stableTimer);
                stableTimer = null;
              }
              setConnection({ scopeKey: activeKey, value: "live" });
              cursor = event.ticket_sequence;
              rememberCursor(activeScope, cursor);
              setStream((current) => {
                const events =
                  current.scopeKey === activeKey ? current.events : [];
                return {
                  scopeKey: activeKey,
                  cursor,
                  events: [...events, event].slice(-MAX_BUFFERED_EVENTS),
                };
              });
              callback.current();
            }
          }
          if (!lifetime.signal.aborted && !request.signal.aborted)
            throw new Error("SSE stream closed");
        } catch {
          if (lifetime.signal.aborted) return;
          if (stableTimer !== null) {
            window.clearTimeout(stableTimer);
            stableTimer = null;
          }
          failures += 1;
          if (failures > 6) break;
          setConnection({ scopeKey: activeKey, value: "retrying" });
          if (navigator.onLine) {
            await waitForBackoff(
              Math.min(8000, 500 * 2 ** (failures - 1)),
            );
          }
        }
      }
      if (!lifetime.signal.aborted) startPolling();
    }
    void follow();
    return () => {
      window.removeEventListener("offline", onOffline);
      lifetime.abort();
      request?.abort();
      if (stableTimer !== null) window.clearTimeout(stableTimer);
      if (pollingTimer !== null) window.clearInterval(pollingTimer);
    };
  }, [enabled, principalId, restartEpoch, tenantId, ticketId]);

  return {
    events: stream.scopeKey === activeScopeKey ? stream.events : [],
    bufferedEventCount:
      stream.scopeKey === activeScopeKey ? stream.events.length : 0,
    bufferedEventUniqueCount:
      stream.scopeKey === activeScopeKey
        ? new Set(stream.events.map((event) => event.ticket_sequence)).size
        : 0,
    bufferLimit: MAX_BUFFERED_EVENTS,
    cursor: stream.scopeKey === activeScopeKey ? stream.cursor : 0,
    connection: ticketId
      ? !enabled
        ? "closed"
        : connection.scopeKey === activeScopeKey
          ? connection.value
          : "connecting"
      : "idle",
    reconnect: () => setRestartEpoch((value) => value + 1),
  };
}
