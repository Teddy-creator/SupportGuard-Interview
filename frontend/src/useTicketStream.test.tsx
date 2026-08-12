import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useTicketStream } from "./useTicketStream";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  window.sessionStorage.clear();
});

describe("useTicketStream", () => {
  it("falls back to durable projection polling after bounded SSE failures", async () => {
    vi.useFakeTimers();
    const refreshProjection = vi.fn();
    const fetchStream = vi.fn(async () => new Response(null, { status: 503 }));
    vi.stubGlobal("fetch", fetchStream);

    const { result } = renderHook(() =>
      useTicketStream(
        {
          principalId: "customer_a",
          tenantId: "tenant_a",
          ticketId: "ticket_stream",
        },
        refreshProjection,
      ),
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(24_000);
    });

    expect(fetchStream).toHaveBeenCalledTimes(7);
    expect(result.current.connection).toBe("polling");
    expect(refreshProjection).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_500);
    });
    expect(refreshProjection).toHaveBeenCalledTimes(2);

    act(() => result.current.reconnect());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(fetchStream).toHaveBeenCalledTimes(8);
    expect(result.current.connection).toBe("retrying");
  });

  it("retains a cursor, ignores duplicate events, and bounds the in-memory buffer", async () => {
    const encoder = new TextEncoder();
    const streamResponse = (sequences: number[]) =>
      new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(
              encoder.encode(
                sequences
                  .map((sequence) =>
                    [
                      `id: ${sequence}`,
                      "event: agent_event",
                      `data: ${JSON.stringify({
                        ticket_sequence: sequence,
                        run_id: "run-stream",
                        run_sequence: sequence,
                        event_type: "tool_observation",
                        status: "completed",
                        payload: {},
                        created_at: "2026-07-28T01:00:00Z",
                      })}`,
                      "",
                    ].join("\n"),
                  )
                  .join("\n") + "\n",
              ),
            );
          },
        }),
        { status: 200, headers: { "Content-Type": "text/event-stream" } },
      );
    const refreshProjection = vi.fn();
    const fetchStream = vi
      .fn()
      .mockResolvedValueOnce(
        streamResponse([
          ...Array.from({ length: 70 }, (_, index) => index + 1),
          70,
        ]),
      )
      .mockResolvedValueOnce(streamResponse([70, 71]));
    vi.stubGlobal("fetch", fetchStream);

    const { result } = renderHook(() =>
      useTicketStream(
        {
          principalId: "customer_a",
          tenantId: "tenant_a",
          ticketId: "ticket_cursor",
        },
        refreshProjection,
      ),
    );

    await waitFor(() => expect(result.current.cursor).toBe(70));
    expect(refreshProjection).toHaveBeenCalledTimes(70);
    expect(result.current.events).toHaveLength(64);
    expect(result.current.bufferedEventCount).toBe(64);
    expect(result.current.bufferedEventUniqueCount).toBe(64);
    expect(result.current.bufferLimit).toBe(64);
    expect(result.current.events[0].ticket_sequence).toBe(7);

    act(() => result.current.reconnect());
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(2));
    expect(
      (fetchStream.mock.calls[1][1] as RequestInit).headers,
    ).toMatchObject({ "Last-Event-ID": "70" });
    await waitFor(() => expect(result.current.cursor).toBe(71));
    expect(refreshProjection).toHaveBeenCalledTimes(71);
  });

  it("does not reuse a cursor for the same ticket id across tenant or principal scopes", async () => {
    window.sessionStorage.setItem(
      "supportguard:sse-cursor:customer_a:tenant_a:ticket_shared",
      "41",
    );
    const fetchStream = vi.fn(
      async (...args: [RequestInfo | URL, RequestInit?]) => {
        void args;
        return new Response(null, { status: 404 });
      },
    );
    vi.stubGlobal("fetch", fetchStream);

    const { rerender } = renderHook(
      ({
        principalId,
        tenantId,
      }: {
        principalId: string;
        tenantId: string;
      }) =>
        useTicketStream(
          { principalId, tenantId, ticketId: "ticket_shared" },
          vi.fn(),
        ),
      {
        initialProps: {
          principalId: "customer_a",
          tenantId: "tenant_a",
        },
      },
    );
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));
    expect(
      (fetchStream.mock.calls[0][1] as RequestInit).headers,
    ).toMatchObject({ "Last-Event-ID": "41" });

    rerender({ principalId: "customer_a", tenantId: "tenant_b" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(2));
    expect(
      (fetchStream.mock.calls[1][1] as RequestInit).headers,
    ).toMatchObject({ "Last-Event-ID": "0" });

    rerender({ principalId: "customer_b", tenantId: "tenant_a" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(3));
    expect(
      (fetchStream.mock.calls[2][1] as RequestInit).headers,
    ).toMatchObject({ "Last-Event-ID": "0" });
  });
});
