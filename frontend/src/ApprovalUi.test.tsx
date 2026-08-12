import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { approvalEditableField } from "./approvalEditing";
import { ApprovalDetailPanel, ApprovalList } from "./ApprovalUi";
import type { Approval, ApprovalDetail } from "./productTypes";

afterEach(cleanup);

const baseApproval: Approval = {
  id: "approval_1",
  ticket_id: "ticket_1",
  status: "pending",
  action_type: "refund",
  actionable: true,
  allowed_actions: ["approve", "edit_and_approve", "reject"],
  resource_summary: "bill_1",
  risk: "high",
  created_at: "2026-07-28T01:00:00Z",
};

const baseDetail: ApprovalDetail = {
  ...baseApproval,
  status: "pending",
  resource_type: "billing_record_id",
  resource_id: "bill_1",
  origin_turn_id: "turn_1",
  resource_identity: {
    resource_type: "billing_record_id",
    resource_id: "bill_1",
    origin_turn_id: "turn_1",
    identity_source: "persisted",
    identity_complete: true,
  },
  action_payload: {
    billing_record_id: "bill_1",
    amount: "49.00",
    currency: "USD",
  },
  review_context: {
    original_request: "请检查重复扣费并按政策处理。",
    risk: "high",
    policy_route: "确定性策略与证据已绑定",
    freshness: {
      status: "current",
      proposed_version: 2,
      current_version: 2,
    },
    tool_observations: [
      {
        data: {
          kind: "billing_record",
          billing_record_id: "bill_1",
          status: "charged",
          amount: "49.00",
          currency: "USD",
          duplicate_of: "bill_original",
          version: 2,
        },
      },
    ],
    evidence: [
      {
        title: "billing-refunds",
        section_path: "重复扣费",
        version: "3.1",
        freshness: "current",
      },
    ],
  },
  business_version: 2,
  status_version: 1,
  execution_preconditions: [],
  proposed_diff: [
    {
      field: "账单退款状态",
      current: "charged",
      proposed: "退款 49.00 USD",
    },
  ],
  ticket: {
    id: "ticket_1",
    title: "重复扣费",
    status: "awaiting_approval",
    issue_type: "refund",
    risk: "high",
  },
  proposal: {
    resource_id: "bill_1",
    resource_version: 2,
    status: "bound",
  },
  updated_at: "2026-07-28T01:00:00Z",
};

describe("approval product projection", () => {
  it("allows the frozen blank approve reason while keeping reject reason required", () => {
    const shared = {
      detail: baseDetail,
      busy: false,
      reason: "",
      refundReason: "",
      targetConcurrency: "",
      mutationError: "",
      mutationFieldError: "",
      onReason: vi.fn(),
      onRefundReason: vi.fn(),
      onTargetConcurrency: vi.fn(),
      onDecide: vi.fn(),
    };
    const { rerender } = render(
      <ApprovalDetailPanel
        {...shared}
        decision="approve"
        onDecision={vi.fn()}
      />,
    );

    expect(screen.getByLabelText("审批理由（可选）")).toHaveValue("");
    expect(
      screen.getByRole("button", { name: "批准并提交执行" }),
    ).toBeEnabled();

    rerender(
      <ApprovalDetailPanel
        {...shared}
        decision="reject"
        onDecision={vi.fn()}
      />,
    );
    expect(screen.getByLabelText("拒绝理由（必填）")).toHaveValue("");
    expect(screen.getByRole("button", { name: "确认拒绝" })).toBeDisabled();
  });

  it("does not silently drop a server status outside the known groups", () => {
    render(
      <ApprovalList
        items={[{ ...baseApproval, status: "reconciling" }]}
        state="ready"
        onSelect={vi.fn()}
        onRetry={vi.fn()}
      />,
    );

    expect(screen.getByRole("heading", { name: /其他状态/ })).toBeInTheDocument();
    expect(screen.getByText("bill_1")).toBeInTheDocument();
  });

  it("distinguishes approval-list loading, error, empty, and preserved-data states", () => {
    const retry = vi.fn();
    const { rerender } = render(
      <ApprovalList
        items={[]}
        state="loading"
        onSelect={vi.fn()}
        onRetry={retry}
      />,
    );
    expect(screen.getByText("正在加载审批申请…")).toBeInTheDocument();
    expect(screen.queryByText("当前租户没有审批申请。")).not.toBeInTheDocument();

    rerender(
      <ApprovalList
        items={[]}
        state="error"
        onSelect={vi.fn()}
        onRetry={retry}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "重新加载审批申请" }));
    expect(retry).toHaveBeenCalledOnce();
    expect(screen.queryByText("当前租户没有审批申请。")).not.toBeInTheDocument();

    rerender(
      <ApprovalList
        items={[]}
        state="ready"
        onSelect={vi.fn()}
        onRetry={retry}
      />,
    );
    expect(screen.getByText("当前租户没有审批申请。")).toBeInTheDocument();

    rerender(
      <ApprovalList
        items={[baseApproval]}
        state="error"
        onSelect={vi.fn()}
        onRetry={retry}
      />,
    );
    expect(screen.getByText("bill_1")).toBeInTheDocument();
  });

  it("explains a non-actionable pending request without calling it terminal", () => {
    render(
      <ApprovalDetailPanel
        detail={{
          ...baseDetail,
          actionable: false,
          allowed_actions: [],
        }}
        busy={false}
        decision="approve"
        reason=""
        refundReason=""
        targetConcurrency=""
        mutationError=""
        mutationFieldError=""
        onDecision={vi.fn()}
        onReason={vi.fn()}
        onRefundReason={vi.fn()}
        onTargetConcurrency={vi.fn()}
        onDecide={vi.fn()}
      />,
    );

    expect(
      screen.getByText(/提案、检查点或业务事实绑定需要恢复/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/已进入终态/)).not.toBeInTheDocument();
  });

  it("projects stable evidence identity and freshness details for review", () => {
    render(
      <ApprovalDetailPanel
        detail={baseDetail}
        busy={false}
        decision="approve"
        reason=""
        refundReason=""
        targetConcurrency=""
        mutationError=""
        mutationFieldError=""
        onDecision={vi.fn()}
        onReason={vi.fn()}
        onRefundReason={vi.fn()}
        onTargetConcurrency={vi.fn()}
        onDecide={vi.fn()}
      />,
    );

    expect(screen.getByText("billing-refunds")).toBeInTheDocument();
    expect(screen.getByText("重复扣费 · v3.1")).toBeInTheDocument();
    expect(screen.getByText("当前有效")).toBeInTheDocument();
    expect(screen.queryByText("action-hash")).not.toBeInTheDocument();
    expect(screen.queryByText("技术绑定")).not.toBeInTheDocument();
  });

  it("renders unavailable evidence freshness without inventing a current status", () => {
    render(
      <ApprovalDetailPanel
        detail={{
          ...baseDetail,
          review_context: {
            ...baseDetail.review_context,
            evidence: [
              {
                ...baseDetail.review_context.evidence[0],
                freshness: "unavailable",
              },
            ],
          },
        }}
        busy={false}
        decision="approve"
        reason=""
        refundReason=""
        targetConcurrency=""
        mutationError=""
        mutationFieldError=""
        onDecision={vi.fn()}
        onReason={vi.fn()}
        onRefundReason={vi.fn()}
        onTargetConcurrency={vi.fn()}
        onDecide={vi.fn()}
      />,
    );

    expect(screen.getByText("未提供独立时效标记")).toBeInTheDocument();
    expect(screen.queryByText("当前有效")).not.toBeInTheDocument();
  });

  it("keeps edit conflicts at the decision object instead of the edited field", () => {
    render(
      <ApprovalDetailPanel
        detail={baseDetail}
        busy={false}
        decision="edit-and-approve"
        reason=""
        refundReason="重复扣费事实已确认"
        targetConcurrency=""
        mutationError="状态已经发生变化，请刷新后重试。"
        mutationFieldError=""
        onDecision={vi.fn()}
        onReason={vi.fn()}
        onRefundReason={vi.fn()}
        onTargetConcurrency={vi.fn()}
        onDecide={vi.fn()}
      />,
    );

    const field = screen.getByLabelText(/修改后的退款理由/);
    const error = screen.getByRole("alert");
    expect(error).toHaveTextContent("状态已经发生变化");
    expect(field.closest("label")).not.toContainElement(error);
  });

  it("renders only the edit field authorized by the action type", () => {
    const sharedProps = {
      busy: false,
      decision: "edit-and-approve" as const,
      reason: "",
      mutationError: "",
      mutationFieldError: "",
      onDecision: vi.fn(),
      onReason: vi.fn(),
      onRefundReason: vi.fn(),
      onTargetConcurrency: vi.fn(),
      onDecide: vi.fn(),
    };
    const { rerender } = render(
      <ApprovalDetailPanel
        {...sharedProps}
        detail={baseDetail}
        refundReason="重复扣费事实已确认"
        targetConcurrency=""
      />,
    );

    expect(screen.getByLabelText("修改后的退款理由")).toBeInTheDocument();
    expect(screen.queryByLabelText(/目标并发/)).not.toBeInTheDocument();

    rerender(
      <ApprovalDetailPanel
        {...sharedProps}
        detail={{
          ...baseDetail,
          action_type: "entitlement_change",
          action_payload: {
            subscription_id: "subscription_1",
            target: { concurrency_limit: 4 },
          },
        }}
        refundReason=""
        targetConcurrency="12.5"
      />,
    );

    expect(screen.queryByLabelText(/修改后的退款理由/)).not.toBeInTheDocument();
    expect(screen.getByLabelText(/目标并发/)).toHaveValue(12.5);
    expect(
      screen.getByRole("button", { name: "确认修改并提交执行" }),
    ).toBeDisabled();

    rerender(
      <ApprovalDetailPanel
        {...sharedProps}
        detail={{
          ...baseDetail,
          action_type: "entitlement_change",
          action_payload: {
            subscription_id: "subscription_1",
            target: { concurrency_limit: 4 },
          },
        }}
        refundReason=""
        targetConcurrency="12"
      />,
    );
    expect(
      screen.getByRole("button", { name: "确认修改并提交执行" }),
    ).toBeEnabled();
  });

  it("fails closed when an immutable or unknown action advertises edit_and_approve", () => {
    render(
      <ApprovalDetailPanel
        detail={{
          ...baseDetail,
          action_type: "api_key_revocation",
          action_payload: { api_key_id: "key_immutable" },
        }}
        busy={false}
        decision="edit-and-approve"
        reason=""
        refundReason=""
        targetConcurrency=""
        mutationError=""
        mutationFieldError=""
        onDecision={vi.fn()}
        onReason={vi.fn()}
        onRefundReason={vi.fn()}
        onTargetConcurrency={vi.fn()}
        onDecide={vi.fn()}
      />,
    );

    expect(
      screen.queryByRole("button", { name: "修改并批准" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "确认修改并提交执行" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/修改后的退款理由/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/目标并发/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "批准" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "拒绝" })).toBeEnabled();
    expect(approvalEditableField("unknown_action")).toBeNull();
  });
});
