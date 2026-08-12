import { afterEach, describe, expect, it, vi } from "vitest";

import { api, ProductApiError } from "./productApi";

afterEach(() => vi.unstubAllGlobals());

describe("public API problem boundary", () => {
  it("uses a complete bounded ProductProblem and its request id", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            public_code: "service_unavailable",
            message: "服务暂时不可用，请稍后重试。",
            retryable: true,
            request_id: "request-safe-1",
          }),
          { status: 503, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    await expect(api("/example")).rejects.toMatchObject({
      message: "服务暂时不可用，请稍后重试。（请求编号：request-safe-1）",
      status: 503,
      code: "service_unavailable",
      retryable: true,
    } satisfies Partial<ProductApiError>);
  });

  it("does not render an arbitrary JSON or HTML gateway error", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            message: "Traceback: Bearer raw-upstream-secret",
            detail: "<script>gateway-poison</script>",
          }),
          { status: 502, headers: { "Content-Type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response("<html>nginx raw error</html>", {
          status: 502,
          headers: { "Content-Type": "text/html" },
        }),
      );
    vi.stubGlobal("fetch", fetch);

    for (const path of ["/json-gateway", "/html-gateway"]) {
      await expect(api(path)).rejects.toMatchObject({
        message: "服务暂时不可用，请稍后重试。",
        status: 502,
        code: "http_502",
      } satisfies Partial<ProductApiError>);
    }
  });
});
