import { describe, expect, it } from "vitest";

import approvalPage from "./pages/ApprovalPage.tsx?raw";
import conversationPage from "./pages/ConversationPage.tsx?raw";
import approvalMutation from "./features/approval/useApprovalMutation.ts?raw";
import approvalQuery from "./features/approval/useApprovalQuery.ts?raw";
import approvalSourceQuery from "./features/approval/useApprovalSourceQuery.ts?raw";
import approvalView from "./features/approval/useApprovalViewState.ts?raw";
import sourceProjection from "./features/approval/sourceProjection.ts?raw";
import conversationController from "./features/conversation/useConversationController.ts?raw";
import conversationMutation from "./features/conversation/useConversationMutation.ts?raw";
import conversationQuery from "./features/conversation/useConversationQuery.ts?raw";
import conversationView from "./features/conversation/useConversationViewState.ts?raw";
import ticketStream from "./useTicketStream.ts?raw";

describe("Phase 5 frontend state ownership", () => {
  it("keeps pages declarative and composes explicit state owners", () => {
    for (const page of [conversationPage, approvalPage]) {
      expect(page).not.toMatch(/\buse(?:State|Effect)\b/);
      expect(page).not.toContain("createDemoSession");
      expect(page).not.toMatch(/\bapi(?:<|\()/);
    }
    expect(conversationPage).toContain("useConversationController");
    expect(approvalPage).toContain("useApprovalQuery");
    expect(approvalPage).toContain("useApprovalMutation");
    expect(approvalPage).toContain("useApprovalSourceQuery");
    expect(approvalPage).toContain("useApprovalViewState");
  });

  it("separates conversation Query, Stream, Mutation, and View State", () => {
    expect(conversationController).toContain("useConversationQuery");
    expect(conversationController).toContain("useTicketStream");
    expect(conversationController).toContain("useConversationMutation");
    expect(conversationController).toContain("useConversationViewState");
    expect(conversationController).not.toMatch(/\buseState\b/);
    expect(conversationController).not.toContain("mutationIdentity");
    expect(conversationQuery).toMatch(/api<Conversation(?:Page|Detail)>/);
    expect(conversationQuery).not.toContain('method: "POST"');
    expect(conversationQuery).not.toContain("useTicketStream");
    expect(conversationMutation).toContain("mutationIdentity");
    expect(conversationMutation).toContain('method: "POST"');
    expect(conversationMutation).not.toContain("useTicketStream");
    expect(conversationView).not.toMatch(/\bapi(?:<|\()/);
    expect(conversationView).not.toContain("mutationIdentity");
    expect(conversationView).not.toContain("useTicketStream");
    expect(ticketStream).not.toContain("mutationIdentity");
  });

  it("keeps approval source validation pure and drawer visibility view-owned", () => {
    expect(sourceProjection).not.toContain("react");
    expect(sourceProjection).not.toMatch(/\bapi(?:<|\()/);
    expect(approvalQuery).toContain('api<Approval[]>("/approvals"');
    expect(approvalQuery).not.toContain('method: "POST"');
    expect(approvalSourceQuery).toContain("validateInitialSource");
    expect(approvalSourceQuery).not.toContain("sourceOpen");
    expect(approvalMutation).toContain("mutationIdentity");
    expect(approvalMutation).toContain('method: "POST"');
    expect(approvalView).toContain("sourceOpen");
    expect(approvalView).not.toMatch(/\bapi(?:<|\()/);
  });
});
