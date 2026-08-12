// HISTORICAL v1.2.x identity-bound Gate carrier.
// This file is intentionally excluded from current Playwright discovery. Its
// assertions and fixture paths remain only for immutable historical receipts;
// current product acceptance lives in conversation-v15.spec.ts.

import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import path from "node:path";

import { expect, test, type Locator, type Page, type TestInfo } from "@playwright/test";

type ActionType = "refund" | "api_key_revocation" | "entitlement_change";
type Fixture = {
  action_type: ActionType;
  tenant_id: string;
  customer_id: string;
  customer_subject: string;
  approver_subject: string;
  resource_id: string;
  resource_version: number;
  expected_effect: string | number;
};
type Accepted = {
  schema_version: "command-accepted.v1";
  ticket_id: string;
  run_id: string;
  job_id: string;
  reused: boolean;
};
type DecisionAction = "approve" | "reject" | "manual-takeover" | "edit-and-approve";
type Approval = {
  id: string;
  ticket_id: string;
  run_id: string;
  action_type: ActionType;
  action_payload: Record<string, unknown>;
  business_version: number;
  status: string;
  actionable: boolean;
};
type EffectInspection = {
  tenant_id: string;
  ticket_id: string;
  run_id: string;
  approval_id: string;
  approval_status: string;
  action_type: ActionType;
  resource_id: string;
  resource_version: number;
  human_decision_count: number;
  business_action_count: number;
  business_action_id: string | null;
  effect_identity: string | null;
  resource_effect: string | number;
};
type RunInspection = {
  tenant_id: string;
  run_id: string;
  run_status: string;
  decision_count: number;
  tool_invocation_count: number;
  terminal_invocation_count: number;
  observation_count: number;
  ok_observation_count: number;
  retrieval_trace_count: number;
  context_membership_count: number;
  citation_binding_count: number;
  observation_feedback_tokens: number;
  policy_count: number;
  final_outcome_count: number;
  final_event_type: string | null;
  finalized_marker_count: number;
};
type FailureDatabaseSnapshot = {
  schema_version: "supportguard-failure-snapshot.v1";
  snapshot_sha256: string;
  identity: { tenant_id: string; ticket_id: string; run_id: string };
  runtime_jobs: Array<{ kind: string; status: string }>;
  events: Array<{ event_type: string; ticket_sequence: number }>;
  proposals: Array<{ id: string; status: string }>;
  approvals: Array<{ id: string; status: string }>;
  business_actions: Array<{ id: string; status: string }>;
  durable_event_head: {
    max_ticket_sequence: number;
    last_event_type: string | null;
  };
};
type BrowserFailureSnapshot = {
  pathname: string;
  sse_ticket_id: string | null;
  sse_state: string | null;
  sse_cursor: number;
  visible_ticket_id: string | null;
  visible_events: Array<{
    event_type: string;
    ticket_sequence: number;
    run_sequence: number;
    status: string;
  }>;
};

const repoRoot = path.resolve(import.meta.dirname, "../..");
const fixtureScript = path.join(repoRoot, "scripts/identity_bound_e2e_fixture.py");

function fixtureCommand(args: string[]): string {
  const fixtureRunner = process.env.E2E_FIXTURE_RUNNER;
  if (fixtureRunner) {
    return execFileSync(
      "docker",
      ["exec", fixtureRunner, "uv", "run", "--frozen", "--no-sync", "python", fixtureScript, ...args],
      { cwd: repoRoot, encoding: "utf8", env: process.env },
    ).trim();
  }
  return execFileSync("uv", ["run", "--frozen", "--no-sync", "python", fixtureScript, ...args], {
    cwd: repoRoot,
    encoding: "utf8",
    env: process.env,
  }).trim();
}

function seedFixture(actionType: ActionType): Fixture {
  return JSON.parse(fixtureCommand(["seed", "--action-type", actionType])) as Fixture;
}

function inspectEffect(fixture: Fixture, approvalId: string): EffectInspection {
  return JSON.parse(fixtureCommand([
    "inspect",
    "--tenant-id",
    fixture.tenant_id,
    "--approval-id",
    approvalId,
  ])) as EffectInspection;
}

function inspectRun(tenantId: string, runId: string): RunInspection {
  return JSON.parse(fixtureCommand([
    "inspect-run",
    "--tenant-id",
    tenantId,
    "--run-id",
    runId,
  ])) as RunInspection;
}

function inspectFailureSnapshot(tenantId: string, runId: string): FailureDatabaseSnapshot {
  return JSON.parse(fixtureCommand([
    "snapshot-run",
    "--tenant-id",
    tenantId,
    "--run-id",
    runId,
  ])) as FailureDatabaseSnapshot;
}

function classifyFailure(
  database: FailureDatabaseSnapshot | null,
  browser: BrowserFailureSnapshot | null,
): string {
  if (!database) return "unclassified_with_required_facts";
  const initialJob = database.runtime_jobs.find((item) => item.kind === "agent_start");
  if (!initialJob || initialJob.status !== "succeeded") {
    return "queue_or_worker_not_terminal";
  }
  if (database.proposals.length === 0) return "agent_or_policy_no_proposal";
  const proposalPersisted = database.events.some(
    (item) => item.event_type === "proposal_drafted",
  );
  const proposalVisible = browser?.visible_events.some(
    (item) => item.event_type === "proposal_drafted",
  ) ?? false;
  if (proposalPersisted && !proposalVisible) {
    return "proposal_persisted_ui_or_sse_missing";
  }
  return "unclassified_with_required_facts";
}

async function captureAcceptedFailure(
  page: Page,
  testInfo: TestInfo,
  tenantId: string,
  accepted: Accepted,
  cause: unknown,
): Promise<void> {
  let database: FailureDatabaseSnapshot | null = null;
  let databaseSnapshotError: string | null = null;
  try {
    database = inspectFailureSnapshot(tenantId, accepted.run_id);
  } catch (error) {
    databaseSnapshotError = error instanceof Error ? error.name : "unknown_error";
  }

  let browser: BrowserFailureSnapshot | null = null;
  let browserSnapshotError: string | null = null;
  try {
    browser = await page.evaluate(() => {
      const stream = document.querySelector<HTMLElement>("[data-sse-ticket-id]");
      const ticket = document.querySelector<HTMLElement>(".ticket-detail[data-ticket-id]");
      const visibleEvents = [...document.querySelectorAll<HTMLElement>(".timeline [data-event-type]")]
        .map((item) => ({
          event_type: item.dataset.eventType ?? "unknown",
          ticket_sequence: Number(item.dataset.ticketSequence ?? 0),
          run_sequence: Number(item.dataset.runSequence ?? 0),
          status: item.dataset.eventStatus ?? "unknown",
        }));
      return {
        pathname: window.location.pathname,
        sse_ticket_id: stream?.dataset.sseTicketId ?? null,
        sse_state: stream?.dataset.sseState ?? null,
        sse_cursor: Number(stream?.dataset.sseCursor ?? 0),
        visible_ticket_id: ticket?.dataset.ticketId ?? null,
        visible_events: visibleEvents,
      };
    });
  } catch (error) {
    browserSnapshotError = error instanceof Error ? error.name : "unknown_error";
  }

  const body = {
    schema_version: "supportguard-e2e-failure.v1",
    captured_at: new Date().toISOString(),
    test_title: testInfo.title,
    accepted: {
      ticket_id: accepted.ticket_id,
      run_id: accepted.run_id,
      job_id: accepted.job_id,
    },
    failure_type: cause instanceof Error ? cause.name : typeof cause,
    classification: classifyFailure(database, browser),
    database_snapshot_error: databaseSnapshotError,
    browser_snapshot_error: browserSnapshotError,
    database,
    browser,
  };
  const encoded = JSON.stringify(body);
  const sealed = JSON.stringify({
    ...body,
    snapshot_sha256: createHash("sha256").update(encoded).digest("hex"),
  }, null, 2);
  try {
    await testInfo.attach("supportguard-failure-snapshot", {
      body: sealed,
      contentType: "application/json",
    });
  } catch {
    // Diagnostics must never replace the original E2E failure.
  }
}

function fixtureUrl(fixture: Fixture): string {
  const params = new URLSearchParams({
    demo_tenant_id: fixture.tenant_id,
    demo_customer_id: fixture.customer_id,
    demo_customer_subject: fixture.customer_subject,
    demo_approver_subject: fixture.approver_subject,
  });
  return `/?${params.toString()}`;
}

function actionRequest(fixture: Fixture): { label: string; message: string } {
  if (fixture.action_type === "refund") {
    return {
      label: "重复扣费",
      message: `${fixture.resource_id} 是重复扣费，请按政策退款`,
    };
  }
  if (fixture.action_type === "api_key_revocation") {
    return {
      label: "Key 泄露",
      message: `${fixture.resource_id} 疑似泄露，请立即撤销这个 API Key`,
    };
  }
  return {
    label: "配额调整",
    message: `请把订阅 ${fixture.resource_id} 的并发配额从当前值明确提升到 60`,
  };
}

async function submitNewTicket(page: Page, message: string): Promise<Accepted> {
  await page.getByRole("textbox", { name: "问题描述" }).fill(message);
  const responsePromise = page.waitForResponse((response) =>
    response.request().method() === "POST"
    && new URL(response.url()).pathname === "/api/tickets"
  );
  await page.getByRole("button", { name: "提交新工单" }).click();
  const response = await responsePromise;
  expect(response.status()).toBe(202);
  return response.json() as Promise<Accepted>;
}

async function openExactApproval(
  page: Page,
  accepted: Accepted,
  fixture: Fixture,
): Promise<{ approval: Approval; proposal: Locator }> {
  await page.getByRole("button", { name: "审批工作台" }).click();
  await expect.poll(
    async () => {
      try {
        return (await exactApproval(page, accepted, fixture)).id;
      } catch {
        return null;
      }
    },
    { timeout: 30_000 },
  ).not.toBeNull();
  const approval = await exactApproval(page, accepted, fixture);
  const row = page.locator("button.approval-row").filter({ hasText: fixture.resource_id });
  await expect(row).toHaveCount(1);
  await row.click();
  const proposal = page.locator(`article[data-approval-id="${approval.id}"]`);
  await expect(proposal).toBeVisible();
  return { approval, proposal };
}

async function submitVisibleDecision(
  page: Page,
  proposal: Locator,
  approvalId: string,
  action: DecisionAction,
): Promise<void> {
  const buttons: Record<DecisionAction, { begin: string; confirm: string }> = {
    approve: { begin: "审核后批准", confirm: "确认批准" },
    reject: { begin: "拒绝提案", confirm: "确认拒绝" },
    "manual-takeover": {
      begin: "停止自动处理并转入人工队列",
      confirm: "转入人工队列",
    },
    "edit-and-approve": {
      begin: "修改允许字段并批准",
      confirm: "修改并批准",
    },
  };
  await proposal.getByRole("button", { name: buttons[action].begin }).click();
  if (action === "edit-and-approve") {
    await proposal.getByLabel("允许修改的退款原因").fill(
      "人工复核确认同一服务周期发生等额重复扣费。",
    );
  }
  await proposal.getByLabel("决策理由", { exact: true }).fill(
    `浏览器验收：已核验当前证据、资源版本与审批快照，并选择 ${action}。`,
  );
  const responsePromise = page.waitForResponse((response) =>
    response.request().method() === "POST"
    && new URL(response.url()).pathname === `/api/approvals/${approvalId}/${action}`
  );
  await proposal.getByRole("button", { name: buttons[action].confirm }).click();
  expect((await responsePromise).status()).toBe(202);
}

async function exactApproval(
  page: Page,
  accepted: Accepted,
  fixture: Fixture,
): Promise<Approval> {
  const matches = await page.evaluate(async ({ ticketId, runId, actionType, resourceId }) => {
    const response = await fetch("/api/approvals", { credentials: "same-origin" });
    if (!response.ok) throw new Error(`approval list failed: ${response.status}`);
    const approvals = await response.json() as Approval[];
    return approvals.filter((item) => {
      const payloadResource = item.action_payload.billing_record_id
        ?? item.action_payload.api_key_id
        ?? item.action_payload.subscription_id;
      return item.ticket_id === ticketId
        && item.run_id === runId
        && item.action_type === actionType
        && payloadResource === resourceId;
    });
  }, {
    ticketId: accepted.ticket_id,
    runId: accepted.run_id,
    actionType: fixture.action_type,
    resourceId: fixture.resource_id,
  });
  expect(matches).toHaveLength(1);
  return matches[0];
}

test("one workspace exposes all six product families and the isolation scenario", async ({ page }) => {
  await page.goto("/");
  for (const label of [
    "产品能力",
    "429 诊断",
    "重复扣费",
    "Key 泄露",
    "事故影响",
    "配额调整",
    "越权拦截",
  ]) {
    await expect(page.getByRole("button", { name: label })).toBeVisible();
  }
});

test("an underspecified request becomes an appendable same-ticket second Agent run", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "客户工作台" }).click();
  const first = await submitNewTicket(page, "我遇到了问题，但还没有产品名称、错误码或请求 ID。");
  await expect(page.getByRole("heading", { name: "需要补充信息" })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("heading", { name: "继续当前工单" })).toBeVisible();
  await page.getByRole("textbox", { name: "续问内容" }).fill(
    "补充：atlas-chat 当前是否支持 JSON Object，限制是什么？",
  );
  const followupResponse = page.waitForResponse((response) =>
    response.request().method() === "POST"
    && new URL(response.url()).pathname === `/api/tickets/${first.ticket_id}/messages`
  );
  await page.getByRole("button", { name: "发送续问" }).click();
  const second = await (await followupResponse).json() as Accepted;
  expect(second.ticket_id).toBe(first.ticket_id);
  expect(second.run_id).not.toBe(first.run_id);
  await expect(page.getByRole("heading", { name: "基于当前证据的处理结果" })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("heading", { name: "可追溯证据与实时事实" })).toBeVisible();
  await expect(page.locator(".conversation article.message.customer")).toHaveCount(2);
  await expect(page.locator(".ticket-detail")).toHaveAttribute("data-ticket-id", first.ticket_id);
});

test("cross-tenant prompt injection is visibly refused without a proposal or action", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "客户工作台" }).click();
  await page.getByRole("button", { name: "越权拦截" }).click();
  const accepted = await submitNewTicket(
    page,
    await page.getByRole("textbox", { name: "问题描述" }).inputValue(),
  );
  await expect(page.locator(".answer-card.pending h3", { hasText: "已转入人工队列" })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("高风险动作没有执行", { exact: false })).toBeVisible();
  const snapshot = inspectFailureSnapshot("tenant_demo", accepted.run_id);
  expect(snapshot.proposals).toHaveLength(0);
  expect(snapshot.approvals).toHaveLength(0);
  expect(snapshot.business_actions).toHaveLength(0);
});

test("390px customer flow keeps account, new ticket, history drawer and refresh recovery usable", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(page.getByRole("region", { name: "当前账号" })).toBeVisible();
  await page.getByRole("button", { name: "＋ 新建工单" }).last().click();
  const accepted = await submitNewTicket(page, "atlas-chat 当前是否支持 JSON Object，限制是什么？");
  await expect(page.getByRole("heading", { name: "基于当前证据的处理结果" })).toBeVisible({ timeout: 30_000 });
  await page.reload();
  await expect(page.locator(".ticket-detail")).toHaveAttribute("data-ticket-id", accepted.ticket_id);
  await expect(page.getByRole("heading", { name: "基于当前证据的处理结果" })).toBeVisible();
  await page.getByRole("button", { name: "工单历史" }).click();
  await expect(page.getByRole("complementary", { name: "工单历史" })).toHaveClass(/open/);
  const restoredTicket = page.locator("button.ticket-row.selected");
  await expect(restoredTicket).toHaveCount(1);
  await expect(restoredTicket).toContainText("atlas-chat");
  await page.getByRole("button", { name: "关闭" }).click();
});

test("SSE outage visibly retries and replays the durable result", async ({ page }) => {
  let streamUnavailable = true;
  await page.route("**/api/tickets/*/events/stream", async (route) => {
    if (streamUnavailable) {
      await route.fulfill({ status: 502, contentType: "text/html", body: "<h1>Bad Gateway</h1>" });
      return;
    }
    await route.fallback();
  });
  await page.goto("/");
  await page.getByRole("button", { name: "客户工作台" }).click();
  await submitNewTicket(page, "余额充足，但 atlas-chat 返回 429 concurrency_limit_exceeded，为什么？");
  await expect(page.getByText("事件通道正在重连", { exact: true })).toBeVisible();
  streamUnavailable = false;
  await expect(page.getByRole("heading", { name: "基于当前证据的处理结果" })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("这不是余额不足：", { exact: false })).toBeVisible();
});

test("API rebuild surfaces a readable retry and never exposes proxy HTML", async ({ page }) => {
  let apiUnavailable = true;
  await page.route("**/api/health", async (route) => {
    if (apiUnavailable) {
      await route.fulfill({ status: 502, contentType: "text/html", body: "<h1>Bad Gateway</h1>" });
      return;
    }
    await route.fallback();
  });
  await page.goto("/");
  await expect(page.getByRole("alert")).toContainText("服务暂时不可用", { timeout: 15_000 });
  await expect(page.getByRole("alert")).not.toContainText("<h1>Bad Gateway</h1>");
  apiUnavailable = false;
  await page.getByRole("button", { name: "重试加载" }).click();
  await expect(page.getByRole("heading", { name: "今天遇到了什么问题？" })).toBeVisible();
});

test("async diagnostic binds one run to Decision, Observation, feedback, Policy and Finalizer", async ({ page }, testInfo) => {
  await page.goto("/");
  await page.getByRole("button", { name: "客户工作台" }).click();
  await page.getByRole("button", { name: "429 诊断" }).click();
  const acceptedResponse = page.waitForResponse((response) =>
    response.request().method() === "POST" && new URL(response.url()).pathname === "/api/tickets"
  );
  await page.getByRole("button", { name: "提交新工单" }).click();
  const accepted = await (await acceptedResponse).json() as Accepted;
  try {
    expect(accepted.schema_version).toBe("command-accepted.v1");
    expect(accepted.reused).toBe(false);
    await expect(page.getByRole("heading", { name: "基于当前证据的处理结果" })).toBeVisible({ timeout: 30_000 });
    await expect(page.getByText("这不是余额不足：", { exact: false })).toBeVisible();
    await expect(page.getByRole("heading", { name: "可追溯证据与实时事实" })).toBeVisible();

    const events = await page.evaluate(async (ticketId) => {
      const response = await fetch(`/api/tickets/${ticketId}/events`, {
        credentials: "same-origin",
      });
      if (!response.ok) throw new Error(`event list failed: ${response.status}`);
      return response.json() as Promise<Array<{
        event_type: string;
        run_id: string;
        ticket_sequence: number;
        payload: Record<string, unknown>;
      }>>;
    }, accepted.ticket_id);
    expect(events.every((item) => item.run_id === accepted.run_id)).toBe(true);
    expect(events.map((item) => item.ticket_sequence)).toEqual(
      [...events].map((item) => item.ticket_sequence).sort((left, right) => left - right),
    );
    const eventTypes = events.map((item) => item.event_type);
    expect(eventTypes.filter((item) => item === "agent_decision")).toHaveLength(2);
    expect(eventTypes).toContain("tool_observation");
    expect(eventTypes).toContain("policy_decision");
    expect(eventTypes.at(-1)).toBe("final_outcome");
    const inspection = inspectRun("tenant_demo", accepted.run_id);
    expect(inspection).toMatchObject({
      tenant_id: "tenant_demo",
      run_id: accepted.run_id,
      run_status: "completed",
      decision_count: 2,
      policy_count: 1,
      final_outcome_count: 1,
      final_event_type: "final_outcome",
      finalized_marker_count: 1,
    });
    expect(inspection.tool_invocation_count).toBeGreaterThanOrEqual(2);
    expect(inspection.terminal_invocation_count).toBe(inspection.tool_invocation_count);
    expect(inspection.observation_count).toBe(inspection.tool_invocation_count);
    expect(inspection.ok_observation_count).toBe(inspection.observation_count);
    expect(inspection.retrieval_trace_count).toBeGreaterThanOrEqual(1);
    expect(inspection.context_membership_count).toBeGreaterThanOrEqual(1);
    expect(inspection.citation_binding_count).toBeGreaterThanOrEqual(1);
    expect(inspection.observation_feedback_tokens).toBeGreaterThan(0);
  } catch (cause) {
    await captureAcceptedFailure(page, testInfo, "tenant_demo", accepted, cause);
    throw cause;
  }
});

for (const scenario of [
  { actionType: "api_key_revocation" as const, label: "Key 泄露" },
  { actionType: "refund" as const, label: "重复扣费" },
  { actionType: "entitlement_change" as const, label: "配额调整" },
]) {
  test(`${scenario.actionType} binds exact identities and reaches exactly one durable effect`, async ({ page }, testInfo) => {
    const fixture = seedFixture(scenario.actionType);
    await page.goto(fixtureUrl(fixture));
    await page.getByRole("button", { name: "客户工作台" }).click();
    await page.getByRole("button", { name: scenario.label }).click();
    const resourceMessage = {
      refund: `${fixture.resource_id} 是重复扣费，请按政策退款`,
      api_key_revocation: `${fixture.resource_id} 疑似泄露，请立即撤销这个 API Key`,
      entitlement_change: `请把订阅 ${fixture.resource_id} 的并发配额从当前值明确提升到 60`,
    }[scenario.actionType];
    await page.getByRole("textbox").fill(resourceMessage);
    const acceptedResponse = page.waitForResponse((response) =>
      response.request().method() === "POST" && new URL(response.url()).pathname === "/api/tickets"
    );
    await page.getByRole("button", { name: "提交新工单" }).click();
    const accepted = await (await acceptedResponse).json() as Accepted;
    try {
      expect(accepted.reused).toBe(false);
      expect(accepted.ticket_id).toMatch(/^ticket_/);
      expect(accepted.run_id).toMatch(/^run_/);
      expect(accepted.job_id).toMatch(/^job_/);
      await expect(page.getByRole("heading", { name: "高风险动作尚未执行" })).toBeVisible({ timeout: 30_000 });
      await expect(page.getByRole("heading", { name: "可追溯证据与实时事实" })).toBeVisible();

    const approvalListResponse = page.waitForResponse((response) =>
      response.request().method() === "GET"
      && new URL(response.url()).pathname === "/api/approvals"
      && response.status() === 200
    );
    await page.getByRole("button", { name: "审批工作台" }).click();
    await approvalListResponse;
    const approval = await exactApproval(page, accepted, fixture);
    expect(approval.status).toBe("pending");
    expect(approval.actionable).toBe(true);
    expect(approval.business_version).toBe(fixture.resource_version);
    const approvalRow = page.locator("button.approval-row").filter({ hasText: fixture.resource_id });
    await expect(approvalRow).toHaveCount(1);
    await approvalRow.click();
    const proposal = page.locator(`article[data-approval-id="${approval.id}"]`);
    await expect(proposal).toHaveCount(1);
    await expect(proposal).toHaveAttribute("data-ticket-id", accepted.ticket_id);
    await expect(proposal).toHaveAttribute("data-run-id", accepted.run_id);
    await expect(proposal).toContainText(fixture.resource_id);

    const decisionResponsePromise = page.waitForResponse((response) =>
      response.request().method() === "POST"
      && new URL(response.url()).pathname === `/api/approvals/${approval.id}/approve`
    );
    await proposal.getByRole("button", { name: "审核后批准" }).click();
    await proposal.getByLabel("决策理由", { exact: true }).fill(
      "已核验用户请求、实时业务事实、知识证据、资源版本与审批快照。",
    );
    await proposal.getByRole("button", { name: "确认批准" }).click();
    const decisionResponse = await decisionResponsePromise;
    expect(decisionResponse.status()).toBe(202);
    const decision = await decisionResponse.json() as {
      approval_id: string;
      ticket_id: string;
      run_id: string;
      job_id: string;
      reused: boolean;
    };
    expect(decision).toMatchObject({
      approval_id: approval.id,
      ticket_id: accepted.ticket_id,
      run_id: accepted.run_id,
      reused: false,
    });
    expect(decision.job_id).not.toBe(accepted.job_id);
    await expect(proposal.getByText("Runtime Action Receipt", { exact: true })).toBeVisible({ timeout: 30_000 });

    const originalRequest = decisionResponse.request();
    const replay = await page.evaluate(async ({ url, body, headers }) => {
      const response = await fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers,
        body,
      });
      return { status: response.status, payload: await response.json() };
    }, {
      url: originalRequest.url(),
      body: originalRequest.postData(),
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": originalRequest.headers()["x-csrf-token"],
        "Idempotency-Key": originalRequest.headers()["idempotency-key"],
      },
    });
    expect(replay.status).toBe(202);
    expect(replay.payload).toMatchObject({
      approval_id: approval.id,
      ticket_id: accepted.ticket_id,
      run_id: accepted.run_id,
      job_id: decision.job_id,
      reused: true,
    });

    await expect.poll(async () => page.evaluate(async (approvalId) => {
      const response = await fetch("/api/approvals", { credentials: "same-origin" });
      const approvals = await response.json() as Array<{ id: string; status: string }>;
      return approvals.find((item) => item.id === approvalId)?.status ?? "missing";
    }, approval.id), { timeout: 30_000 }).toBe("executed");

    const effect = inspectEffect(fixture, approval.id);
    expect(effect).toMatchObject({
      tenant_id: fixture.tenant_id,
      ticket_id: accepted.ticket_id,
      run_id: accepted.run_id,
      approval_id: approval.id,
      approval_status: "executed",
      action_type: scenario.actionType,
      resource_id: fixture.resource_id,
      resource_version: fixture.resource_version + 1,
      human_decision_count: 1,
      business_action_count: 1,
      resource_effect: fixture.expected_effect,
    });
    expect(effect.business_action_id).toMatch(/^action_/);
    expect(effect.effect_identity).toMatch(/^[0-9a-f]{64}$/);

    const durableSnapshot = inspectFailureSnapshot(fixture.tenant_id, accepted.run_id);
    expect(durableSnapshot).toMatchObject({
      schema_version: "supportguard-failure-snapshot.v1",
      identity: {
        tenant_id: fixture.tenant_id,
        ticket_id: accepted.ticket_id,
        run_id: accepted.run_id,
      },
    });
    expect(durableSnapshot.snapshot_sha256).toMatch(/^[0-9a-f]{64}$/);
    expect(durableSnapshot.runtime_jobs.map((item) => item.status)).toEqual([
      "succeeded",
      "succeeded",
    ]);
    expect(durableSnapshot.proposals).toHaveLength(1);
    expect(durableSnapshot.approvals).toHaveLength(1);
    expect(durableSnapshot.business_actions).toHaveLength(1);
    expect(durableSnapshot.events.map((item) => item.event_type)).toContain(
      "proposal_drafted",
    );

    await page.getByRole("button", { name: "客户工作台" }).click();
    const ticket = page.locator("button.ticket-row").filter({ hasText: resourceMessage });
    await expect(ticket).toHaveCount(1);
    await ticket.click();
    await expect(
      page.getByRole("heading", { name: "人工审批后的动作已完成" }),
    ).toBeVisible({ timeout: 10_000 });
    const completedAction = page.locator(".answer-card.completed-action");
    await expect(completedAction).toBeVisible();
    await expect(completedAction).toContainText(fixture.resource_id);
    } catch (cause) {
      await captureAcceptedFailure(page, testInfo, fixture.tenant_id, accepted, cause);
      throw cause;
    }
  });
}

for (const decisionCase of [
  {
    action: "reject" as const,
    actionType: "refund" as const,
    approverOutcome: "处理结果：已拒绝",
    customerOutcome: "提案已被拒绝",
  },
  {
    action: "manual-takeover" as const,
    actionType: "api_key_revocation" as const,
    approverOutcome: "处理结果：已转入人工队列",
    customerOutcome: "已转入人工队列",
  },
]) {
  test(`${decisionCase.action} converges visibly with no Runtime Action`, async ({ page }, testInfo) => {
    const fixture = seedFixture(decisionCase.actionType);
    const request = actionRequest(fixture);
    await page.goto(fixtureUrl(fixture));
    await page.getByRole("button", { name: "客户工作台" }).click();
    const accepted = await submitNewTicket(page, request.message);
    try {
      await expect(page.getByRole("heading", { name: "高风险动作尚未执行" })).toBeVisible({ timeout: 30_000 });
      const { approval, proposal } = await openExactApproval(page, accepted, fixture);
      await submitVisibleDecision(page, proposal, approval.id, decisionCase.action);
      await expect(proposal.getByRole("heading", { name: decisionCase.approverOutcome })).toBeVisible({ timeout: 30_000 });

      const snapshot = inspectFailureSnapshot(fixture.tenant_id, accepted.run_id);
      expect(snapshot.approvals).toHaveLength(1);
      expect(snapshot.approvals[0].status).toBe(
        decisionCase.action === "reject" ? "rejected" : "manual_takeover",
      );
      expect(snapshot.business_actions).toHaveLength(0);

      await page.getByRole("button", { name: "客户工作台" }).click();
      const ticket = page.locator("button.ticket-row").filter({ hasText: request.message });
      await expect(ticket).toHaveCount(1);
      await ticket.click();
      await expect(
        page.locator(".answer-card.pending h3", { hasText: decisionCase.customerOutcome }),
      ).toBeVisible();
    } catch (cause) {
      await captureAcceptedFailure(page, testInfo, fixture.tenant_id, accepted, cause);
      throw cause;
    }
  });
}

test("edit-and-approve visibly executes the edited refund exactly once", async ({ page }, testInfo) => {
  const fixture = seedFixture("refund");
  const request = actionRequest(fixture);
  await page.goto(fixtureUrl(fixture));
  await page.getByRole("button", { name: "客户工作台" }).click();
  const accepted = await submitNewTicket(page, request.message);
  try {
    await expect(page.getByRole("heading", { name: "高风险动作尚未执行" })).toBeVisible({ timeout: 30_000 });
    const { approval, proposal } = await openExactApproval(page, accepted, fixture);
    await submitVisibleDecision(page, proposal, approval.id, "edit-and-approve");
    await expect(proposal.getByText("Runtime Action Receipt", { exact: true })).toBeVisible({ timeout: 30_000 });
    const effect = inspectEffect(fixture, approval.id);
    expect(effect.human_decision_count).toBe(1);
    expect(effect.business_action_count).toBe(1);
    expect(effect.approval_status).toBe("executed");
    await page.getByRole("button", { name: "客户工作台" }).click();
    const ticket = page.locator("button.ticket-row").filter({ hasText: request.message });
    await ticket.click();
    await expect(page.getByRole("heading", { name: "人工审批后的动作已完成" })).toBeVisible();
  } catch (cause) {
    await captureAcceptedFailure(page, testInfo, fixture.tenant_id, accepted, cause);
    throw cause;
  }
});

test("a separate approver session automatically converges the open customer page", async ({ browser }, testInfo) => {
  const fixture = seedFixture("api_key_revocation");
  const request = actionRequest(fixture);
  const baseURL = String(testInfo.project.use.baseURL ?? "http://127.0.0.1:5173");
  const customerContext = await browser.newContext({ baseURL });
  const approverContext = await browser.newContext({ baseURL });
  const customer = await customerContext.newPage();
  const approver = await approverContext.newPage();
  let accepted: Accepted | null = null;
  try {
    await customer.goto(fixtureUrl(fixture));
    await customer.getByRole("button", { name: "客户工作台" }).click();
    accepted = await submitNewTicket(customer, request.message);
    await expect(customer.getByRole("heading", { name: "高风险动作尚未执行" })).toBeVisible({ timeout: 30_000 });
    await expect(customer.locator("[data-sse-state=live]")).toBeVisible();

    await approver.goto(fixtureUrl(fixture));
    const { approval, proposal } = await openExactApproval(approver, accepted, fixture);
    await submitVisibleDecision(approver, proposal, approval.id, "approve");
    await expect(proposal.getByText("Runtime Action Receipt", { exact: true })).toBeVisible({ timeout: 30_000 });

    await expect(customer.getByRole("heading", { name: "人工审批后的动作已完成" })).toBeVisible({ timeout: 30_000 });
    await expect(customer.locator(".ticket-detail")).toHaveAttribute("data-ticket-id", accepted.ticket_id);
  } catch (cause) {
    if (accepted) {
      await captureAcceptedFailure(customer, testInfo, fixture.tenant_id, accepted, cause);
    }
    throw cause;
  } finally {
    await approverContext.close();
    await customerContext.close();
  }
});
