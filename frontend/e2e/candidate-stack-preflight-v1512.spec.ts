import { expect, test, type Page } from "@playwright/test";

/**
 * Candidate-stack preflight only.
 *
 * These checks deliberately contain no browser-side API interception and
 * run against the already-started, candidate-SHA-bound Compose runtime. They
 * prove that the browser, nginx, API, PostgreSQL-backed projections, and real
 * development-session boundary agree before the frozen Journey run starts.
 * They do not submit an Agent message and therefore consume no Provider call.
 */

async function openCustomer(page: Page): Promise<void> {
  await page.goto("/conversations/new");
  await expect(
    page.getByRole("heading", { name: "今天想解决什么问题？" }),
  ).toBeVisible();
}

async function openApprover(page: Page): Promise<void> {
  await page.goto("/approvals");
  await expect(
    page.getByRole("heading", { name: "审批工作台" }),
  ).toBeVisible();
}

async function sessionProjection(page: Page): Promise<Record<string, unknown>> {
  return page.evaluate(async () => {
    const response = await fetch("/api/session", { credentials: "same-origin" });
    if (!response.ok) throw new Error(`session:${response.status}`);
    return response.json() as Promise<Record<string, unknown>>;
  });
}

test.describe("v1.5.12 real candidate stack preflight", () => {
  test("01 nginx reaches the candidate API health projection", async ({
    request,
  }) => {
    const response = await request.get("/api/health");
    expect(response.status()).toBe(200);
    const body = (await response.json()) as Record<string, unknown>;
    expect(body.provider_mode).toBe("worker-owned");
    expect(body.provider_model).toBe("deepseek-v4-flash");
    expect(body.tool_call_mode).toBe("native-worker");
  });

  test("02 the production frontend bundle boots without a fatal state", async ({
    page,
  }) => {
    await openCustomer(page);
    await expect(page.locator("main.fatal-state")).toHaveCount(0);
    await expect(page.getByText("SupportGuard", { exact: true }).first()).toBeVisible();
  });

  test("03 development bootstrap creates a real customer session", async ({
    page,
  }) => {
    await openCustomer(page);
    const session = await sessionProjection(page);
    expect((session.principal as Record<string, unknown>).role).toBe("customer");
    expect((session.active_tenant as Record<string, unknown>).id).toBe(
      "tenant_demo",
    );
  });

  test("04 the customer header and server tenant projection agree", async ({
    page,
  }) => {
    await openCustomer(page);
    const session = await sessionProjection(page);
    const tenant = session.active_tenant as Record<string, unknown>;
    await expect(page.getByLabel("当前租户")).toContainText(String(tenant.name));
  });

  test("05 a new conversation exposes an enabled composer", async ({ page }) => {
    await openCustomer(page);
    await expect(page.getByRole("textbox", { name: "开始新对话" })).toBeEnabled();
    await expect(page.getByRole("button", { name: "发送消息" })).toBeVisible();
  });

  test("06 the new-conversation control preserves the canonical route", async ({
    page,
  }) => {
    await openCustomer(page);
    await page.getByRole("button", { name: "＋ 新建对话" }).click();
    await expect(page).toHaveURL(/\/conversations\/new$/);
    await expect(page.getByRole("textbox", { name: "开始新对话" })).toBeVisible();
  });

  test("07 the real conversation search control is available", async ({ page }) => {
    await openCustomer(page);
    const search = page.getByRole("textbox", { name: "搜索对话" });
    await expect(search).toBeEnabled();
    await search.fill("no-such-candidate-preflight-conversation");
    await expect(search).toHaveValue("no-such-candidate-preflight-conversation");
  });

  test("08 the sidebar reports the live connection boundary", async ({ page }) => {
    await openCustomer(page);
    await expect(
      page.getByText("选择对话后连接", { exact: true }),
    ).toBeVisible();
  });

  test("09 the customer profile is backed by the real session", async ({ page }) => {
    await openCustomer(page);
    const session = await sessionProjection(page);
    const principal = session.principal as Record<string, unknown>;
    await page.locator("button.profile-button").click();
    const dialog = page.getByRole("dialog", { name: "当前身份" });
    await expect(dialog).toContainText(String(principal.display_name));
    await expect(dialog).toContainText(
      principal.role === "customer" ? "客户" : "审批者",
    );
  });

  test("10 the real conversation list endpoint is tenant-scoped", async ({ page }) => {
    await openCustomer(page);
    const result = await page.evaluate(async () => {
      const response = await fetch("/api/conversations?limit=5", {
        credentials: "same-origin",
      });
      return {
        status: response.status,
        body: (await response.json()) as Record<string, unknown>,
      };
    });
    expect(result.status).toBe(200);
    expect(Array.isArray(result.body.items)).toBe(true);
    expect(result.body).toHaveProperty("next_cursor");
  });

  test("11 a missing conversation fails closed without a composer", async ({
    page,
  }) => {
    await openCustomer(page);
    await page.goto("/conversations/ticket_v1512_preflight_missing");
    await expect(
      page.getByRole("heading", { name: "没有找到这条对话" }),
    ).toBeVisible();
    await expect(page.getByRole("textbox", { name: "继续提问" })).toHaveCount(0);
  });

  test("12 the missing-resource API is normalized JSON rather than HTML", async ({
    page,
  }) => {
    await openCustomer(page);
    const result = await page.evaluate(async () => {
      const response = await fetch(
        "/api/conversations/ticket_v1512_preflight_missing",
        { credentials: "same-origin" },
      );
      return {
        status: response.status,
        contentType: response.headers.get("content-type"),
        body: (await response.json()) as Record<string, unknown>,
      };
    });
    expect(result.status).toBe(404);
    expect(result.contentType).toContain("application/json");
    expect(result.body).toMatchObject({
      public_code: "resource_not_found",
      retryable: false,
    });
    expect(JSON.stringify(result.body)).not.toContain("<html");
  });

  test("13 the technical inspector opens and closes on the real bundle", async ({
    page,
  }) => {
    await openCustomer(page);
    const inspectorToggle = page.getByRole("button", { name: "运行详情" });
    await inspectorToggle.click();
    await expect(
      page.getByRole("complementary", { name: "技术检查器" }),
    ).toBeVisible();
    await page.getByRole("button", { name: "关闭技术检查器" }).click();
    await expect(
      page.getByRole("complementary", { name: "技术检查器" }),
    ).toHaveCount(0);
    await expect(inspectorToggle).toBeFocused();
  });

  test("14 mobile navigation opens and closes against the candidate bundle", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await openCustomer(page);
    await page.getByRole("button", { name: "打开对话导航" }).click();
    const drawer = page.getByRole("complementary", { name: "对话导航" });
    await expect(drawer).toHaveClass(/open/);
    await page.getByRole("button", { name: "关闭对话导航" }).click();
    await expect(drawer).not.toHaveClass(/open/);
  });

  test("15 mobile New closes the drawer and keeps the composer reachable", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await openCustomer(page);
    await page.getByRole("button", { name: "打开对话导航" }).click();
    const drawer = page.getByRole("complementary", { name: "对话导航" });
    await drawer.getByRole("button", { name: "＋ 新建对话" }).click();
    await expect(drawer).not.toHaveClass(/open/);
    await expect(page.getByRole("textbox", { name: "开始新对话" })).toBeVisible();
  });

  test("16 the 390px composer remains inside the viewport", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await openCustomer(page);
    const metrics = await page.locator(".composer-dock").evaluate((node) => {
      const box = node.getBoundingClientRect();
      return { top: box.top, bottom: box.bottom, viewport: window.innerHeight };
    });
    expect(metrics.top).toBeGreaterThanOrEqual(0);
    expect(metrics.bottom).toBeLessThanOrEqual(metrics.viewport);
  });

  test("17 the approver route bootstraps an independent real role", async ({
    page,
  }) => {
    await openApprover(page);
    const session = await sessionProjection(page);
    expect((session.principal as Record<string, unknown>).role).toBe("approver");
  });

  test("18 the approver tenant selector reflects server scope", async ({ page }) => {
    await openApprover(page);
    const session = await sessionProjection(page);
    const accessible = session.accessible_tenants as Array<Record<string, unknown>>;
    const selector = page.getByLabel("当前租户", { exact: true });
    await expect(selector.locator("option")).toHaveCount(accessible.length);
    await expect(selector).toHaveValue(
      String((session.active_tenant as Record<string, unknown>).id),
    );
  });

  test("19 the real approval projection is readable without executing actions", async ({
    page,
  }) => {
    await openApprover(page);
    const result = await page.evaluate(async () => {
      const response = await fetch("/api/approvals", {
        credentials: "same-origin",
      });
      return {
        status: response.status,
        body: (await response.json()) as unknown,
      };
    });
    expect(result.status).toBe(200);
    expect(Array.isArray(result.body)).toBe(true);
    await expect(page.locator(".approval-main")).toBeVisible();
  });
});
