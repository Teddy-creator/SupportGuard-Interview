import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ConversationComposer,
  ConversationHeader,
  ConversationSidebar,
  groupedCitationsFor,
  deduplicatedCitationEvidence,
  MessageStream,
  SafeMessage,
} from "./ConversationUi";
import type {
  Citation,
  ConversationDetail,
  SessionContext,
} from "./productTypes";

const noop = () => undefined;

afterEach(cleanup);

const customerSession: SessionContext = {
  auth_mode: "development",
  principal: {
    id: "customer-ui",
    display_name: "UI Customer",
    role: "customer",
    membership_role: "customer_admin",
  },
  active_tenant: { id: "tenant-ui", name: "UI Tenant" },
  customer: {
    id: "customer-ui",
    display_name: "UI Customer",
    region: "cn",
    security_status: "normal",
  },
  accessible_tenants: [{ id: "tenant-ui", name: "UI Tenant" }],
  configured_runtime: {
    mode: "production",
    model: "deepseek-v4-flash",
    actual_run_source: "run",
  },
};

function failedConversation(
  failureCategory: "api_request" | "provider" | "tool" | "runtime",
): ConversationDetail {
  return {
    id: "ticket-failed",
    title: "失败会话",
    lifecycle: "active",
    automation_mode: "agent",
    activity_label: "本轮未完成",
    allowed_actions: ["append_message", "archive"],
    turns: [
      {
        id: "turn-failed",
        ordinal: 1,
        activity_state: "failed",
        result_state: "failed",
        run_id: "run-failed",
        messages: [
          {
            id: "message-customer",
            kind: "customer",
            role: "customer",
            content: "请核验当前状态。",
            sequence: 1,
            created_at: "2026-07-28T01:00:00Z",
          },
        ],
        citations: [],
        run: {
          id: "run-failed",
          status: "failed",
          model: "deepseek-v4-flash",
          provider_mode: "production",
          tool_call_mode: "native",
          finish_reason: "failed",
          failure_category: failureCategory,
        },
      },
    ],
    pending_actions: [],
    turn_pagination: {
      limit: 50,
      returned: 1,
      has_more: false,
    },
    created_at: "2026-07-28T01:00:00Z",
    updated_at: "2026-07-28T01:01:00Z",
  };
}

function messageStream(conversation: ConversationDetail) {
  return (
    <MessageStream
      conversation={conversation}
      withdrawing={false}
      actionErrors={{}}
      onWithdraw={noop}
      onDismissActionError={noop}
      onInspectTurn={noop}
      hasOlder={false}
      loadingOlder={false}
      onLoadOlder={noop}
    />
  );
}

describe("customer-safe answer projection", () => {
  it("exposes a typed terminal identity for browser acceptance without changing copy", () => {
    const { container, unmount } = render(
      messageStream(failedConversation("tool")),
    );

    const turn = container.querySelector(".turn");
    expect(turn).toHaveAttribute("data-turn-id", "turn-failed");
    expect(turn).toHaveAttribute("data-turn-run-id", "run-failed");
    expect(turn).toHaveAttribute("data-turn-activity", "failed");
    expect(turn).toHaveAttribute("data-turn-result", "failed");
    expect(turn).toHaveAttribute("data-turn-failure-category", "tool");
    unmount();
  });

  it("keeps global and turn-scoped technical controls uniquely addressable", () => {
    const conversation = failedConversation("provider");
    conversation.turns[0].messages.push({
      id: "message-assistant",
      kind: "assistant",
      role: "assistant",
      content: "本轮已安全结束。",
      sequence: 2,
      created_at: "2026-07-28T01:01:00Z",
    });
    const onToggleInspector = vi.fn();
    const onInspectTurn = vi.fn();

    render(
      <>
        <ConversationHeader
          title={conversation.title}
          activity={conversation.activity_label}
          inspectorOpen={false}
          session={customerSession}
          onToggleInspector={onToggleInspector}
          onOpenSidebar={noop}
          onOpenProfile={noop}
        />
        <MessageStream
          conversation={conversation}
          withdrawing={false}
          actionErrors={{}}
          onWithdraw={noop}
          onDismissActionError={noop}
          onInspectTurn={onInspectTurn}
          hasOlder={false}
          loadingOlder={false}
          onLoadOlder={noop}
        />
      </>,
    );

    const globalControl = screen.getByRole("button", {
      name: "运行详情",
    });
    const turnControl = screen.getByRole("button", {
      name: "在技术视图中查看本轮",
    });
    expect(screen.getAllByRole("button", { name: /运行详情/ })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: /技术视图/ })).toHaveLength(1);

    fireEvent.click(globalControl);
    expect(onToggleInspector).toHaveBeenCalledOnce();
    fireEvent.click(turnControl);
    expect(onInspectTurn).toHaveBeenCalledWith(
      conversation.turns[0],
      "message-assistant",
      turnControl,
    );
  });

  it("renders useful Markdown while removing raw HTML and unsafe links", () => {
    const { container } = render(
      <SafeMessage
        content={[
          "# 诊断结论",
          "",
          "**并发限制**与余额独立。",
          "",
          "- 降低并发",
          "- 按 `Retry-After` 重试",
          "",
          "```json",
          '{"ok":true}',
          "```",
          "",
          "[安全链接](https://example.com)",
          "[危险链接](javascript:alert(1))",
          "<script>window.secret = 'leak'</script>",
        ].join("\n")}
      />,
    );

    expect(
      screen.getByRole("heading", { name: "诊断结论" }),
    ).toBeInTheDocument();
    expect(container.querySelectorAll("ul")).toHaveLength(1);
    expect(container.querySelectorAll("li")).toHaveLength(2);
    expect(screen.getByText("并发限制").tagName).toBe("STRONG");
    expect(screen.getByText("Retry-After").tagName).toBe("CODE");
    expect(screen.getByRole("link", { name: "安全链接" })).toHaveAttribute(
      "href",
      "https://example.com",
    );
    expect(screen.getByText("危险链接").closest("a")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).not.toContain("window.secret");
  });

  it("binds citations to the exact message and aggregates stable source identity", () => {
    const citations: Citation[] = [
      {
        source_type: "knowledge",
        document_id: "api-errors",
        version: "2.2",
        section_path: "429",
        claim_id: "claim-a",
        message_id: "message-a",
      },
      {
        source_type: "knowledge",
        document_id: "api-errors",
        version: "2.2",
        section_path: "429",
        claim_id: "claim-b",
        message_id: "message-a",
      },
      {
        source_type: "knowledge",
        document_id: "api-errors",
        version: "2.2",
        section_path: "Retry",
        message_id: "message-a",
      },
      {
        source_type: "business_fact",
        observation_source_id: "observation:query_account:account-current:7",
        section_path: "status",
        message_id: "message-a",
      },
      {
        source_type: "knowledge",
        document_id: "must-not-cross-turn",
        version: "1",
        section_path: "other",
        message_id: "message-b",
      },
      {
        source_type: "knowledge",
        document_id: "unbound-legacy",
        version: "1",
        section_path: "other",
      },
    ];

    const groups = groupedCitationsFor(citations, "message-a");
    expect(groups).toHaveLength(2);
    expect(groups[0]).toHaveLength(3);
    expect(
      groups.some((group) => group[0].source_type === "business_fact"),
    ).toBe(true);
    expect(groups.flat()).not.toContain(citations[4]);
    expect(groups.flat()).not.toContain(citations[5]);
  });

  it("keeps distinct business observations separate even when their labels match", () => {
    const citations: Citation[] = [
      {
        source_type: "business_fact",
        observation_source_id: "observation:billing:bill_a:v2",
        title: "账单状态",
        section_path: "当前业务事实",
        message_id: "message-a",
      },
      {
        source_type: "business_fact",
        observation_source_id: "observation:billing:bill_b:v4",
        title: "账单状态",
        section_path: "当前业务事实",
        message_id: "message-a",
      },
    ];

    expect(groupedCitationsFor(citations, "message-a")).toHaveLength(2);
  });

  it("labels same-title business sources with their safe resource identities", () => {
    const conversation = failedConversation("tool");
    conversation.activity_label = "已回答";
    conversation.turns[0].activity_state = "completed";
    conversation.turns[0].result_state = "answered";
    conversation.turns[0].messages.push({
      id: "message-assistant",
      kind: "assistant",
      role: "assistant",
      content: "两笔账单已经核验。",
      sequence: 2,
      created_at: "2026-07-28T01:01:00Z",
    });
    conversation.turns[0].citations = [
      {
        source_type: "business_fact",
        observation_source_id: "billing_record:bill_demo_duplicate",
        title: "账单状态",
        version: "2",
        section_path: "当前业务事实",
        supporting_span: "重复账单已核验。",
        message_id: "message-assistant",
      },
    ];
    render(messageStream(conversation));

    expect(
      screen.getByRole("button", {
        name: "▤ 账单状态 v2 · bill_demo_duplicate",
      }),
    ).toBeInTheDocument();
  });

  it("deduplicates identical evidence spans while preserving claim summaries", () => {
    const citations: Citation[] = [
      {
        source_type: "knowledge",
        document_id: "billing-policy",
        title: "退款政策",
        version: "3.1",
        section_path: "重复扣费资格",
        supporting_span: "两笔扣款必须满足同一服务周期。",
        claim_summary: "金额必须一致。",
      },
      {
        source_type: "knowledge",
        document_id: "billing-policy",
        title: "退款政策",
        version: "3.1",
        section_path: "重复扣费资格",
        supporting_span: "两笔扣款必须满足同一服务周期。",
        claim_summary: "币种与服务周期必须一致。",
      },
    ];

    expect(deduplicatedCitationEvidence(citations)).toEqual([
      expect.objectContaining({
        claim_summary: "金额必须一致。；币种与服务周期必须一致。",
      }),
    ]);
  });

  it("opens one multi-claim citation without duplicate React keys", () => {
    const consoleError = vi
      .spyOn(console, "error")
      .mockImplementation(() => undefined);
    const citations: Citation[] = [
      {
        source_type: "knowledge",
        document_id: "atlas-json-guide",
        title: "Atlas JSON 指南",
        version: "3.1",
        section_path: "JSON Object",
        citation_binding_id: "shared-binding",
        claim_id: "claim-json-capability",
        message_id: "message-assistant",
        claim_summary: "支持 JSON Object 输出。",
      },
      {
        source_type: "knowledge",
        document_id: "atlas-json-guide",
        title: "Atlas JSON 指南",
        version: "3.1",
        section_path: "输入格式限制",
        citation_binding_id: "limit-binding",
        claim_id: "claim-json-limit",
        message_id: "message-assistant",
        claim_summary: "需要满足输入格式限制。",
      },
      {
        source_type: "knowledge",
        document_id: "atlas-json-guide",
        title: "Atlas JSON 指南",
        version: "3.1",
        section_path: "区域要求",
        citation_binding_id: "region-binding",
        message_id: "message-assistant",
        claim_summary: "区域要求随版本发布。",
      },
      {
        source_type: "business_fact",
        observation_source_id: "customer:current",
        title: "客户账户状态",
        section_path: "当前业务事实",
        message_id: "message-assistant",
        claim_summary: "当前账户状态正常。",
      },
    ];
    const conversation: ConversationDetail = {
      id: "ticket-citations",
      title: "JSON 输出",
      lifecycle: "active",
      automation_mode: "agent",
      activity_label: "已回答",
      allowed_actions: ["append_message", "archive"],
      turns: [
        {
          id: "turn-citations",
          ordinal: 1,
          activity_state: "completed",
          result_state: "answered",
          messages: [
            {
              id: "message-customer",
              kind: "customer",
              role: "customer",
              content: "请说明 JSON 输出要求。",
              sequence: 1,
              created_at: "2026-07-28T01:00:00Z",
            },
            {
              id: "message-assistant",
              kind: "assistant",
              role: "assistant",
              content: "这里是三个受同一来源支持的结论。",
              sequence: 2,
              created_at: "2026-07-28T01:01:00Z",
            },
          ],
          citations,
        },
      ],
      pending_actions: [],
      turn_pagination: {
        limit: 50,
        returned: 1,
        has_more: false,
      },
      created_at: "2026-07-28T01:00:00Z",
      updated_at: "2026-07-28T01:01:00Z",
    };

    try {
      const { container, rerender } = render(messageStream(conversation));
      expect(container.querySelectorAll(".evidence-chip")).toHaveLength(2);
      expect(container.querySelectorAll(".citation-chip")).toHaveLength(1);
      expect(container.querySelector(".citation-chip")).toHaveTextContent(
        "客户账户状态",
      );
      const chips = screen.getAllByRole("button", {
        name: /Atlas JSON 指南/,
      });
      expect(chips).toHaveLength(1);
      fireEvent.click(chips[0]);
      expect(screen.getAllByText(/支持结论：/)).toHaveLength(3);
      expect(screen.getByText("JSON Object")).toBeInTheDocument();
      expect(screen.getByText("输入格式限制")).toBeInTheDocument();
      expect(screen.getByText("区域要求")).toBeInTheDocument();
      expect(screen.getByText("知识文档")).toBeInTheDocument();
      expect(screen.getByText("实时业务事实")).toBeInTheDocument();
      fireEvent.click(chips[0]);
      fireEvent.click(
        screen.getByRole("button", { name: /客户账户状态/ }),
      );
      expect(screen.getByText("当前业务事实")).toBeInTheDocument();
      expect(
        consoleError.mock.calls.some((call) =>
          /same key|unique.*key/i.test(call.join(" ")),
        ),
      ).toBe(false);

      rerender(
        messageStream({
          ...conversation,
          turns: [
            {
              ...conversation.turns[0],
              citations: citations.filter(
                (citation) => citation.source_type === "knowledge",
              ),
            },
          ],
        }),
      );
      expect(container.querySelectorAll(".evidence-chip")).toHaveLength(1);
      expect(container.querySelectorAll(".citation-chip")).toHaveLength(1);
      expect(container.querySelector(".citation-chip")).toHaveTextContent(
        "Atlas JSON 指南",
      );
    } finally {
      consoleError.mockRestore();
    }
  });

  it("shows the selected entitlement target without rendering unsafe values", () => {
    const entitlementConversation: ConversationDetail = {
      id: "ticket-entitlement",
      title: "调整并发配额",
      lifecycle: "active",
      automation_mode: "agent",
      activity_label: "等待审批",
      allowed_actions: ["append_message", "archive"],
      turns: [
        {
          id: "turn-entitlement",
          ordinal: 1,
          activity_state: "completed",
          result_state: "proposal_created",
          messages: [],
          citations: [],
        },
      ],
      pending_actions: [
        {
          id: "approval-entitlement",
          turn_id: "turn-entitlement",
          status: "pending",
          action_type: "entitlement_change",
          action_payload: {
            subscription_id: "sub-demo",
            change_type: "quota_change",
            target: { concurrency_limit: 48 },
          },
          allowed_actions: ["withdraw"],
          created_at: "2026-07-28T01:00:00Z",
        },
      ],
      turn_pagination: {
        limit: 50,
        returned: 0,
        has_more: false,
      },
      created_at: "2026-07-28T01:00:00Z",
      updated_at: "2026-07-28T01:00:00Z",
    };

    const { rerender } = render(messageStream(entitlementConversation));

    expect(screen.getByText("目标")).toBeInTheDocument();
    expect(screen.getByText("并发上限 48")).toBeInTheDocument();

    rerender(
      messageStream({
        ...entitlementConversation,
        pending_actions: [
          {
            ...entitlementConversation.pending_actions[0],
            action_payload: {
              subscription_id: "sub-demo",
              change_type: "plan_change",
              target: { plan: "<script>alert(1)</script>" },
            },
          },
        ],
      }),
    );

    expect(screen.queryByText("目标")).not.toBeInTheDocument();
    expect(screen.queryByText(/script|alert/)).not.toBeInTheDocument();
  });

  it("exposes a failed action as a semantic terminal region", () => {
    const conversation = failedConversation("runtime");
    conversation.pending_actions = [
      {
        id: "approval-failed",
        turn_id: "turn-failed",
        status: "failed",
        action_type: "refund",
        action_payload: {
          billing_record_id: "bill-failed",
          amount: "49.00",
          currency: "USD",
        },
        allowed_actions: [],
        created_at: "2026-07-28T01:00:00Z",
      },
    ];

    const { unmount } = render(messageStream(conversation));

    expect(
      screen.getByRole("region", { name: "退款申请 未完成" }),
    ).toHaveTextContent("动作没有完成");
    unmount();
  });

  it("distinguishes a current refund pair from an executed approval snapshot", () => {
    const conversation = failedConversation("runtime");
    const action = {
      id: "approval-refund-pair",
      turn_id: "turn-failed",
      status: "pending",
      action_type: "refund",
      action_payload: {
        billing_record_id: "bill_demo_duplicate",
        original_billing_record_id: "bill_demo_original",
        amount: "49.00",
        currency: "USD",
        service_period_start: "2026-08-01",
        service_period_end: "2026-09-01",
        duplicate_pair_verified: true,
      },
      allowed_actions: ["withdraw"],
      created_at: "2026-07-28T01:00:00Z",
    };
    conversation.pending_actions = [action];

    const { rerender } = render(messageStream(conversation));

    expect(screen.getByText(/当前已核验：与原账单 bill_demo_original/)).toBeInTheDocument();

    rerender(
      messageStream({
        ...conversation,
        pending_actions: [
          {
            ...action,
            status: "executed",
            action_payload: {
              ...action.action_payload,
              duplicate_pair_verified: false,
            },
            allowed_actions: [],
          },
        ],
      }),
    );

    expect(screen.getByText(/审批快照：执行依据关联原账单 bill_demo_original/)).toBeInTheDocument();
    expect(screen.queryByText(/需要重新核验/)).not.toBeInTheDocument();
  });

  it("describes legacy takeover as record-only without inventing an operator queue", () => {
    const session: SessionContext = {
      auth_mode: "development",
      principal: {
        id: "customer-legacy",
        display_name: "Legacy Customer",
        role: "customer",
        membership_role: "customer_admin",
      },
      active_tenant: { id: "tenant-legacy", name: "Legacy Tenant" },
      customer: {
        id: "customer-legacy",
        display_name: "Legacy Customer",
        region: "cn",
        security_status: "normal",
      },
      accessible_tenants: [
        { id: "tenant-legacy", name: "Legacy Tenant" },
      ],
      configured_runtime: {
        mode: "production",
        model: "deepseek-v4-flash",
        actual_run_source: "run",
      },
    };
    const conversation: ConversationDetail = {
      id: "ticket-legacy",
      title: "历史自动处理记录",
      lifecycle: "active",
      automation_mode: "human_queue",
      activity_label: "人工队列",
      allowed_actions: ["append_message", "archive"],
      turns: [
        {
          id: "turn-legacy",
          ordinal: 1,
          activity_state: "completed",
          result_state: "human_queue",
          run_id: "run-legacy",
          messages: [
            {
              id: "message-customer",
              kind: "customer",
              role: "customer",
              content: "这条历史记录现在是什么状态？",
              sequence: 1,
              created_at: "2026-07-28T01:00:00Z",
            },
            {
              id: "message-update",
              kind: "human_queue_update",
              role: "action",
              content: "消息已进入人工队列。",
              sequence: 2,
              created_at: "2026-07-28T01:01:00Z",
            },
          ],
          citations: [],
        },
      ],
      pending_actions: [
        {
          id: "approval-legacy",
          turn_id: "turn-legacy",
          status: "manual_takeover_legacy",
          action_type: "refund",
          action_payload: { billing_record_id: "bill-legacy" },
          allowed_actions: [],
          created_at: "2026-07-28T01:01:00Z",
        },
      ],
      turn_pagination: {
        limit: 50,
        returned: 1,
        has_more: false,
      },
      created_at: "2026-07-28T01:00:00Z",
      updated_at: "2026-07-28T01:01:00Z",
    };

    const { container } = render(
      <>
        <ConversationSidebar
          session={session}
          items={[
            {
              id: conversation.id,
              title: conversation.title,
              lifecycle: "active",
              automation_mode: "human_queue",
              activity_label: "人工队列",
              latest_summary: "已转入人工队列",
              pending_action_count: 0,
              updated_at: conversation.updated_at,
            },
          ]}
          selectedId={conversation.id}
          query=""
          mobileOpen
          onQuery={noop}
          onNew={noop}
          onOpen={noop}
          onClose={noop}
          connection="closed"
          listState="ready"
          hasMore={false}
          loadingMore={false}
          onLoadMore={noop}
          onRetryList={noop}
        />
        <ConversationHeader
          title={conversation.title}
          activity="人工队列"
          inspectorOpen={false}
          session={session}
          onToggleInspector={noop}
          onOpenSidebar={noop}
          onOpenProfile={noop}
        />
        {messageStream(conversation)}
        <ConversationComposer
          value=""
          busy={false}
          mode="human_queue"
          isNew={false}
          onChange={noop}
          onSubmit={(event) => event.preventDefault()}
        />
      </>,
    );

    expect(container).not.toHaveTextContent(/人工队列|转入人工处理|有人正在处理/);
    expect(container).toHaveTextContent(
      "当前版本没有人工坐席收件或回复闭环",
    );
    expect(container).toHaveTextContent("消息仅记录，不会创建 Agent Run");
    expect(container).toHaveTextContent("实时连接已关闭");
    expect(container).not.toHaveTextContent("正在连接");
    expect(screen.getByRole("textbox", { name: "继续提问" })).toHaveAttribute(
      "placeholder",
      "补充信息（消息仅记录，不会发送给人工坐席）…",
    );
  });

  it("distinguishes conversation-list loading, error, empty, and stale-data states", () => {
    const retry = vi.fn();
    const props = {
      session: customerSession,
      selectedId: null,
      query: "",
      mobileOpen: true,
      onQuery: noop,
      onNew: noop,
      onOpen: noop,
      onClose: noop,
      connection: "idle" as const,
      hasMore: false,
      loadingMore: false,
      onLoadMore: noop,
      onRetryList: retry,
    };
    const { rerender } = render(
      <ConversationSidebar {...props} items={[]} listState="loading" />,
    );
    expect(screen.getByText("正在加载对话…")).toBeInTheDocument();
    expect(screen.queryByText("还没有对话")).not.toBeInTheDocument();

    rerender(<ConversationSidebar {...props} items={[]} listState="error" />);
    fireEvent.click(screen.getByRole("button", { name: "重新加载对话" }));
    expect(retry).toHaveBeenCalledOnce();
    expect(screen.queryByText("还没有对话")).not.toBeInTheDocument();

    rerender(<ConversationSidebar {...props} items={[]} listState="ready" />);
    expect(screen.getByText("还没有对话")).toBeInTheDocument();

    rerender(
      <ConversationSidebar
        {...props}
        items={[
          {
            id: "ticket-preserved",
            title: "保留的对话",
            lifecycle: "active",
            automation_mode: "agent",
            activity_label: "已回答",
            pending_action_count: 0,
            updated_at: "2026-08-11T01:00:00Z",
          },
        ]}
        listState="error"
      />,
    );
    expect(screen.getByText("保留的对话")).toBeInTheDocument();
    expect(
      screen.getByText("对话列表刷新失败，仍显示上次结果。"),
    ).toBeInTheDocument();
  });

  it("keeps the selected sidebar item identifiable without repeating its visible transcript preview", () => {
    const session: SessionContext = {
      auth_mode: "development",
      principal: {
        id: "customer-sidebar",
        display_name: "Sidebar Customer",
        role: "customer",
        membership_role: "customer_admin",
      },
      active_tenant: { id: "tenant-sidebar", name: "Sidebar Tenant" },
      customer: {
        id: "customer-sidebar",
        display_name: "Sidebar Customer",
        region: "cn",
        security_status: "normal",
      },
      accessible_tenants: [
        { id: "tenant-sidebar", name: "Sidebar Tenant" },
      ],
      configured_runtime: {
        mode: "production",
        model: "deepseek-v4-flash",
        actual_run_source: "run",
      },
    };

    render(
      <ConversationSidebar
        session={session}
        items={[
          {
            id: "ticket-selected",
            title: "当前会话",
            lifecycle: "active",
            automation_mode: "agent",
            activity_label: "已回复",
            latest_summary: "当前正文中已经显示的唯一回答",
            pending_action_count: 0,
            updated_at: "2026-07-28T01:01:00Z",
          },
          {
            id: "ticket-other",
            title: "其他会话",
            lifecycle: "active",
            automation_mode: "agent",
            activity_label: "已回复",
            latest_summary: "其他会话仍保留摘要预览",
            pending_action_count: 0,
            updated_at: "2026-07-28T01:02:00Z",
          },
        ]}
        selectedId="ticket-selected"
        query=""
        mobileOpen
        onQuery={noop}
        onNew={noop}
        onOpen={noop}
        onClose={noop}
        connection="live"
        listState="ready"
        hasMore={false}
        loadingMore={false}
        onLoadMore={noop}
        onRetryList={noop}
      />,
    );

    const selected = screen.getByRole("button", { name: /当前会话/ });
    expect(selected).toHaveAttribute("aria-current", "page");
    expect(selected).toHaveTextContent("已回复");
    expect(selected).not.toHaveTextContent("当前正文中已经显示的唯一回答");

    const other = screen.getByRole("button", { name: /其他会话/ });
    expect(other).not.toHaveAttribute("aria-current");
    expect(other).toHaveTextContent("其他会话仍保留摘要预览");
  });

  it("asks for Request ID only for an explicit API failure category", () => {
    const { rerender } = render(messageStream(failedConversation("provider")));

    expect(screen.getByText(/系统已检查本轮请求/)).toHaveTextContent(
      "当前还无法确认模型服务是否完成了本轮推理",
    );
    expect(screen.queryByText(/Request ID/)).not.toBeInTheDocument();

    rerender(messageStream(failedConversation("tool")));
    expect(screen.getByText(/系统已检查本轮请求/)).toHaveTextContent(
      "请核对账单、API Key 或订阅编号后重试",
    );
    expect(screen.getByText(/系统已检查本轮请求/)).not.toHaveTextContent(
      "模型服务",
    );

    rerender(messageStream(failedConversation("api_request")));
    expect(screen.getByText(/系统已检查本轮请求/)).toHaveTextContent(
      "请重试；若仍失败，请补充 Request ID 和发生时间",
    );
  });
});
