import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { TechnicalInspector } from "./TechnicalInspector";
import type { SessionContext, TurnInspector } from "./productTypes";

const session: SessionContext = {
  auth_mode: "development",
  csrf_token: "csrf",
  principal: {
    id: "cust_demo",
    display_name: "Aster Customer",
    role: "customer",
    membership_role: "customer_admin",
  },
  active_tenant: { id: "tenant_demo", name: "Aster Labs" },
  customer: {
    id: "cust_demo",
    display_name: "Aster",
    region: "eu",
    security_status: "normal",
  },
  accessible_tenants: [{ id: "tenant_demo", name: "Aster Labs" }],
  configured_runtime: {
    mode: "fake",
    model: "deterministic-fake",
    actual_run_source: "ticket.latest_run",
  },
};

function inspector(
  timeline: TurnInspector["timeline"],
  finishReason = "refused",
): TurnInspector {
  return {
    message_id: "message_1",
    turn_id: "turn_1",
    run_id: "run_1",
    run: {
      id: "run_1",
      status: "completed",
      model: "deterministic-fake",
      provider_mode: "fake",
      tool_call_mode: "native_fixture",
      finish_reason: finishReason,
      configured_runtime: {
        model: "deterministic-fake",
        provider_mode: "fake",
        tool_call_mode: "native_fixture",
      },
      actual_runtime: null,
      budgets: { tool_rounds: 0, tool_attempts: 0, llm_calls: 0 },
    },
    timeline,
    knowledge_sources: [],
    business_facts: [],
  };
}

function event(
  eventType: string,
  sequence: number,
  payload?: Record<string, unknown>,
): TurnInspector["timeline"][number] {
  return {
    event_type: eventType,
    status: "completed",
    run_id: "run_1",
    ticket_sequence: sequence,
    created_at: "2026-08-14T01:00:00Z",
    payload,
  };
}

afterEach(cleanup);

describe("TechnicalInspector security summary", () => {
  it("calls a denial pre-tool only when no tool invocation occurred", () => {
    render(
      <TechnicalInspector
        open
        loading={false}
        data={inspector([
          event("policy_decision", 1, { route: "rejected" }),
        ])}
        session={session}
        onClose={() => undefined}
      />,
    );

    const proof = screen.getByRole("region", { name: "本轮安全边界" });
    expect(
      within(proof).getByText("请求在工具调用前被拒绝"),
    ).toBeInTheDocument();
    expect(within(proof).getByText("只读工具结果")).toBeInTheDocument();
    expect(within(proof).getAllByText("0")).toHaveLength(4);
  });

  it("does not disguise a failed or empty tool path as a pre-tool denial", () => {
    render(
      <TechnicalInspector
        open
        loading={false}
        data={inspector([
          event("tool_invocation", 1, { tool_name: "get_billing_record" }),
          event("policy_decision", 2, { route: "rejected" }),
        ])}
        session={session}
        onClose={() => undefined}
      />,
    );

    const proof = screen.getByRole("region", { name: "本轮安全边界" });
    expect(within(proof).getByText("只读工具调用已记录")).toBeInTheDocument();
    expect(within(proof).queryByText(/工具调用前被拒绝/)).toBeNull();
    expect(within(proof).getByText(/没有持久化终态结果/)).toBeInTheDocument();
  });

  it("counts persisted tool results even when a separate invocation event is absent", () => {
    render(
      <TechnicalInspector
        open
        loading={false}
        data={inspector([
          event("tool_observation", 1, { tool_name: "search_knowledge" }),
          event("tool_observation", 2, { tool_name: "query_subscription" }),
          event("tool_observation", 3, { tool_name: "query_api_usage" }),
        ], "evidence freshness insufficient")}
        session={session}
        onClose={() => undefined}
      />,
    );

    const proof = screen.getByRole("region", { name: "本轮安全边界" });
    expect(
      within(proof).getByText("已返回 3 个只读工具结果"),
    ).toBeInTheDocument();
    expect(within(proof).getByText(/业务查询 2 个，知识检索 1 个/)).toBeInTheDocument();
    const resultRow = within(proof).getByText("只读工具结果").closest("div");
    const businessRow = within(proof).getByText("业务查询结果").closest("div");
    const knowledgeRow = within(proof).getByText("知识检索结果").closest("div");
    expect(resultRow).not.toBeNull();
    expect(businessRow).not.toBeNull();
    expect(knowledgeRow).not.toBeNull();
    expect(within(resultRow as HTMLElement).getByText("3")).toBeInTheDocument();
    expect(within(businessRow as HTMLElement).getByText("2")).toBeInTheDocument();
    expect(within(knowledgeRow as HTMLElement).getByText("1")).toBeInTheDocument();
  });

  it("states when no tool was used without claiming an RLS hit", () => {
    render(
      <TechnicalInspector
        open
        loading={false}
        data={inspector([event("agent_stopped", 1)], "completed")}
        session={session}
        onClose={() => undefined}
      />,
    );

    const proof = screen.getByRole("region", { name: "本轮安全边界" });
    expect(within(proof).getByText("本轮未调用业务工具")).toBeInTheDocument();
    expect(within(proof).getByText(/不能据此声称数据库 RLS/)).toBeInTheDocument();
  });

  it("recognizes a structured tenant-scope reason as a pre-tool denial", () => {
    render(
      <TechnicalInspector
        open
        loading={false}
        data={inspector([
          event("agent_stopped", 1, { reason_code: "tenant_scope_mismatch" }),
        ])}
        session={session}
        onClose={() => undefined}
      />,
    );

    const proof = screen.getByRole("region", { name: "本轮安全边界" });
    expect(
      within(proof).getByText("请求在工具调用前被拒绝"),
    ).toBeInTheDocument();
    expect(within(proof).getByText(/没有访问业务工具/)).toBeInTheDocument();
  });

  it("groups one claim across business sources without hiding the bindings", () => {
    const data = inspector([], "answered");
    data.business_facts = [
      {
        source_type: "business_fact",
        claim_id: "claim-diagnostic",
        title: "请求追踪结果",
        observation_source_id: "api_request_trace:trace_demo_429",
        claim_summary: "请求失败时并发槽位已满。",
        observed_at: "2026-08-14T01:00:01Z",
        freshness: "fresh",
      },
      {
        source_type: "business_fact",
        claim_id: "claim-diagnostic",
        title: "API 使用情况",
        observation_source_id: "api_usage_snapshot:usage_demo",
        claim_summary: "请求失败时并发槽位已满。",
        observed_at: "2026-08-14T01:00:00Z",
        freshness: "fresh",
      },
      {
        source_type: "business_fact",
        claim_id: "claim-diagnostic",
        title: "API 使用情况",
        observation_source_id: "subscription:sub_demo",
        claim_summary: "请求失败时并发槽位已满。",
        observed_at: "2026-08-14T01:00:00Z",
        freshness: "fresh",
      },
    ];
    render(
      <TechnicalInspector
        open
        loading={false}
        data={data}
        session={session}
        onClose={() => undefined}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "证据" }));

    expect(
      screen.getByText("请求追踪结果 · API 使用情况"),
    ).toBeInTheDocument();
    expect(screen.getByText(/3 项来源/)).toBeInTheDocument();
    expect(screen.getAllByText("请求失败时并发槽位已满。")).toHaveLength(1);
    fireEvent.click(screen.getByText("查看来源绑定"));
    expect(
      screen.getByText("api_request_trace:trace_demo_429"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("api_usage_snapshot:usage_demo"),
    ).toBeInTheDocument();
    expect(screen.getByText("subscription:sub_demo")).toBeInTheDocument();
  });
});
