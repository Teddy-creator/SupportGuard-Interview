import { cleanup, render, screen, within } from "@testing-library/react";
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
    expect(within(proof).getByText("只读工具调用")).toBeInTheDocument();
    expect(within(proof).getAllByText("0")).toHaveLength(3);
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
    expect(within(proof).getByText("按当前会话范围运行")).toBeInTheDocument();
    expect(within(proof).queryByText(/工具调用前被拒绝/)).toBeNull();
    expect(within(proof).getByText("1")).toBeInTheDocument();
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
});
