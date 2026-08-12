export type Role = "customer" | "approver";
export type JsonObject = Record<string, unknown>;

export type SessionContext = {
  auth_mode: "development" | "production";
  csrf_token?: string | null;
  principal: {
    id: string;
    display_name: string;
    role: Role;
    membership_role: string;
  };
  active_tenant: { id: string; name: string; status?: string };
  customer?: {
    id: string;
    display_name: string;
    region: string;
    security_status: string;
  };
  accessible_tenants: Array<{ id: string; name: string; status?: string }>;
  configured_runtime: {
    mode: string;
    model: string;
    actual_run_source: string;
  };
};

export type ConversationListItem = {
  id: string;
  title: string;
  lifecycle: "active" | "archived";
  automation_mode: "agent" | "human_queue";
  activity_label: string;
  pending_action_count: number;
  latest_summary?: string | null;
  updated_at: string;
};

export type Citation = {
  source_type?: "knowledge" | "business_fact";
  observation_source_id?: string;
  document_id?: string;
  title?: string;
  section_path?: string;
  version?: string;
  supporting_span?: string;
  effective_at?: string;
  citation_binding_id?: string;
  chunk_id?: string;
  index_version?: string;
  source_locator?: {
    locator_hash?: string;
    document_internal_id?: string;
    document_version?: string;
  };
  claim_id?: string;
  message_id?: string;
  claim_summary?: string;
  observed_at?: string;
  freshness?: string;
  fact_summary?: JsonObject;
};

export type RunProjection = {
  id: string;
  status: string;
  model: string;
  provider_mode: string;
  tool_call_mode: string;
  finish_reason?: string;
  failure_category?: "api_request" | "provider" | "tool" | "runtime" | null;
  budgets?: { tool_rounds: number; tool_attempts: number; llm_calls: number };
  configured_runtime?: {
    model?: string;
    provider_mode?: string;
    tool_call_mode?: string;
    source?: string;
  };
  actual_runtime?: {
    model?: string;
    provider?: string;
    provider_mode?: string;
    tool_call_mode?: string;
    prompt_version?: string;
    schema_version?: string;
    context_assembly_version?: string;
    knowledge_index_contract?: string;
    attempt_status?: string;
    source?: string;
    provider_transport_attempts?: number;
    provider_retry_count?: number;
  } | null;
};

export type ConversationMessage = {
  id: string;
  kind:
    | "customer"
    | "assistant"
    | "action_proposal"
    | "action_update"
    | "human_queue_update";
  role: string;
  content: string;
  sequence: number;
  approval_id?: string | null;
  created_at: string;
};

export type ConversationTurn = {
  id: string;
  ordinal: number;
  activity_state: string;
  result_state?:
    | "answered"
    | "answered_limited"
    | "needs_clarification"
    | "refused"
    | "proposal_created"
    | "human_queue"
    | "failed"
    | null;
  run_id?: string | null;
  messages: ConversationMessage[];
  citations: Citation[];
  run?: RunProjection | null;
};

export type ProductAction = {
  id: string;
  turn_id?: string | null;
  status: string;
  action_type: string;
  action_payload: JsonObject;
  allowed_actions: string[];
  status_version?: number;
  customer_safe_reason_code?: string | null;
  created_at: string;
  updated_at?: string;
};

export type ConversationDetail = {
  id: string;
  title: string;
  lifecycle: "active" | "archived";
  automation_mode: "agent" | "human_queue";
  activity_label: string;
  allowed_actions: string[];
  turns: ConversationTurn[];
  pending_actions: ProductAction[];
  turn_pagination: {
    limit: number;
    returned: number;
    has_more: boolean;
    next_before_ordinal?: number | null;
  };
  created_at: string;
  updated_at: string;
};

export type ConversationPage = {
  items: ConversationListItem[];
  next_cursor?: string | null;
};

export type CommandAccepted = {
  schema_version: "command-accepted.v1";
  ticket_id: string;
  run_id?: string | null;
  job_id?: string | null;
  status: "queued" | "accepted";
  reused: boolean;
};

export type InspectorEvent = {
  event_type: string;
  status: string;
  run_id: string;
  ticket_sequence: number;
  created_at: string;
  payload?: JsonObject;
};

export type TurnInspector = {
  message_id: string;
  turn_id: string;
  run_id: string;
  run: RunProjection;
  timeline: InspectorEvent[];
  knowledge_sources: Citation[];
  business_facts: Citation[];
};

export type ApprovalDecision = "approve" | "reject" | "edit-and-approve";

export type ApprovalActionType =
  | "refund"
  | "api_key_revocation"
  | "entitlement_change";

export type ApprovalProjectionStatus =
  | "pending"
  | "approved"
  | "executing"
  | "verification_pending"
  | "executed"
  | "rejected"
  | "stale"
  | "withdrawn"
  | "failed"
  | "manual_takeover_legacy"
  | "projection_unavailable";

export type ApprovalRisk = "low" | "medium" | "high";

export type ApprovalAllowedAction =
  | "approve"
  | "edit_and_approve"
  | "reject";

export type ApprovalEditableField =
  | "refund_reason"
  | "target_concurrency";

export type ApprovalEditChanges =
  | { refund_reason: string }
  | { target_concurrency: number };

export type Approval = {
  id: string;
  ticket_id: string;
  status: string;
  action_type: ApprovalActionType;
  actionable: boolean;
  allowed_actions: ApprovalAllowedAction[];
  resource_summary: string;
  risk: ApprovalRisk;
  created_at: string;
  source_label?: string;
};

export type ApprovalResourceType =
  | "billing_record_id"
  | "api_key_id"
  | "subscription_id";

export type ApprovalResourceIdentity = {
  resource_type: ApprovalResourceType;
  resource_id: string;
  origin_turn_id: string;
  identity_source: "persisted";
  identity_complete: true;
};

export type ApprovalActionPayload =
  | {
      billing_record_id: string;
      amount?: string | null;
      currency?: string | null;
      refund_reason?: string | null;
    }
  | {
      api_key_id: string;
    }
  | {
      subscription_id: string;
      change_type?: "quota_change" | "plan_change" | null;
      target?: {
        plan?: string | null;
        rpm_limit?: number | null;
        concurrency_limit?: number | null;
      } | null;
    };

export type ApprovalResourceFacts =
  | {
      kind: "billing_record";
      billing_record_id: string;
      status: "charged" | "refunded" | "pending" | "failed" | "void" | "unknown";
      amount?: string | null;
      currency?: string | null;
      duplicate_of?: string | null;
      version?: number | null;
    }
  | {
      kind: "api_key";
      api_key_id: string;
      status: "active" | "revoked" | "disabled" | "expired" | "unknown";
      version?: number | null;
    }
  | {
      kind: "subscription";
      subscription_id: string;
      status:
        | "active"
        | "past_due"
        | "suspended"
        | "cancelled"
        | "canceled"
        | "unknown";
      plan?: string | null;
      rpm_limit?: number | null;
      concurrency_limit?: number | null;
      version?: number | null;
    };

export type ApprovalReviewContext = {
  original_request?: string | null;
  risk: ApprovalRisk;
  policy_route: "确定性策略与证据已绑定" | "策略或证据绑定不可用";
  freshness: {
    status: "current" | "changed_since_proposal" | "unavailable";
    proposed_version: number;
    current_version?: number | null;
  };
  tool_observations: Array<{ data: ApprovalResourceFacts }>;
  evidence: Array<{
    title: string;
    section_path: string;
    version: string;
    freshness: "current" | "changed_since_proposal" | "unavailable";
  }>;
};

export type ApprovalDetail = Approval & {
  status: ApprovalProjectionStatus;
  resource_type: ApprovalResourceType;
  resource_id: string;
  origin_turn_id: string;
  resource_identity: ApprovalResourceIdentity;
  action_payload: ApprovalActionPayload;
  review_context: ApprovalReviewContext;
  business_version: number;
  status_version: number;
  execution_preconditions: Array<{
    label: string;
    satisfied: boolean;
  }>;
  proposed_diff: Array<{
    field: string;
    current: string;
    proposed: string;
  }>;
  ticket?: {
    id: string;
    title: string;
    status:
      | "open"
      | "queued"
      | "running"
      | "awaiting_approval"
      | "verification_pending"
      | "resolved"
      | "rejected"
      | "failed"
      | "human_queue"
      | "archived"
      | "unknown";
    issue_type: ApprovalActionType;
    risk: ApprovalRisk;
  } | null;
  proposal?: {
    resource_id: string;
    resource_version: number;
    status: "draft" | "bound" | "stale" | "unknown";
  } | null;
  human_decision?: {
    decision: "approve" | "edit_and_approve" | "reject" | "manual_takeover";
    created_at: string;
  } | null;
  resume_job?: {
    status: "queued" | "leased" | "succeeded" | "retry_wait" | "dead" | "unknown";
    outcome: "pending" | "completed" | "verification_pending" | "failed" | "unknown";
  } | null;
  business_action?: {
    status:
      | "pending"
      | "running"
      | "executing"
      | "succeeded"
      | "failed"
      | "stale"
      | "unknown"
      | "verification_pending";
    action_type: ApprovalActionType;
    resource_id: string;
    resource_version?: number | null;
    created_at: string;
  } | null;
  updated_at: string;
  decided_at?: string | null;
  consumed_at?: string | null;
};

export type ApprovalSourceMessage = {
  id: string;
  turn_id: string;
  kind: string;
  role: string;
  content: string;
  sequence: number;
  is_origin_turn: boolean;
  created_at: string;
};

export type ApprovalSource = {
  approval_id: string;
  ticket_id: string;
  title: string;
  origin_turn_id: string;
  messages: ApprovalSourceMessage[];
  returned: number;
  has_more: boolean;
  next_before_sequence: number | null;
  next_before_message_id: string | null;
};
