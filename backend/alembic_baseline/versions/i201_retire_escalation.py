"""Retire Action MCP escalation creation while preserving three proposals.

Revision ID: i201_retire_escalation
Revises: i200_baseline_0001
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "i201_retire_escalation"
down_revision: str | None = "i200_baseline_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_RESTRICT_ACTION_CAPABILITIES_SQL = r"""
CREATE OR REPLACE FUNCTION public.supportguard_action_mcp_execute(
  p_capability_name text,
  p_model_arguments jsonb,
  p_trusted_context jsonb
) RETURNS jsonb
LANGUAGE plpgsql
SET search_path TO 'pg_catalog', 'public', 'supportguard_control'
AS $function$
DECLARE
  ep jsonb:=p_trusted_context->'execution_payload'; v_bindings jsonb;
  v_ticket record; v_resource record; v_catalog record; v_existing record;
  v_payload jsonb; v_action_hash text; v_identity text; v_record_id text;
  v_result jsonb; v_now timestamptz:=clock_timestamp(); v_target jsonb;
  v_decision jsonb; v_binding_hash text;
BEGIN
  IF current_user<>'supportguard_owner'
     OR p_capability_name NOT IN (
       'propose_refund','propose_api_key_revocation','propose_entitlement_change'
     )
     OR jsonb_typeof(p_model_arguments)<>'object' OR jsonb_typeof(ep)<>'object'
     OR jsonb_typeof(ep->'observation_binding')<>'array' THEN
    RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='action_execute_forbidden';
  END IF;
  SELECT t.* INTO v_ticket FROM agent_runs r
  JOIN runtime_jobs j ON j.tenant_id=r.tenant_id AND j.run_id=r.id
    AND j.id=p_trusted_context->>'job_id'
    AND j.fencing_token=(p_trusted_context->>'fencing_token')::bigint
  JOIN support_tickets t ON t.tenant_id=r.tenant_id AND t.id=r.ticket_id
    AND t.customer_id=r.customer_id
  JOIN policy_capability_invocations i ON i.tenant_id=r.tenant_id
    AND i.run_id=r.id AND i.job_id=j.id
    AND i.id=p_trusted_context->>'invocation_id'
    AND i.capability_name=p_capability_name
    AND i.effect_identity=p_trusted_context->>'effect_identity'
    AND i.causal_decision_hash=p_trusted_context->>'decision_hash'
    AND i.observation_binding_hash=p_trusted_context->>'binding_hash'
    AND i.status='executing'
  WHERE r.tenant_id=p_trusted_context->>'tenant_id'
    AND r.id=p_trusted_context->>'run_id' AND r.active_job_id=j.id
    AND r.active_fencing_token=j.fencing_token
    AND t.id=ep->>'ticket_id' AND t.customer_id=ep->>'customer_id';
  IF v_ticket.id IS NULL THEN
    RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='action_scope_unavailable';
  END IF;
  v_bindings:=ep->'observation_binding';
  v_binding_hash:=encode(pg_catalog.sha256(convert_to(
    supportguard_canonical_jsonb(v_bindings),'UTF8')),'hex');
  IF ep->>'causal_decision_schema_version'<>'causal-decision.v2'
     OR jsonb_typeof(ep->'causal_decision')<>'object'
     OR v_binding_hash<>p_trusted_context->>'binding_hash' THEN
    RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='causal_decision_binding_invalid';
  END IF;
  IF p_capability_name='propose_refund' THEN
    IF (SELECT count(*) FROM jsonb_object_keys(p_model_arguments))<>3
       OR NOT p_model_arguments ?& ARRAY[
         'billing_record_id','refund_reason','idempotency_key']
       OR v_ticket.status NOT IN ('open','running','needs_clarification') THEN
      RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='refund_input_invalid';
    END IF;
    SELECT * INTO v_resource FROM billing_records
     WHERE id=p_model_arguments->>'billing_record_id'
      AND tenant_id=p_trusted_context->>'tenant_id'
      AND customer_id=v_ticket.customer_id AND status='charged';
    IF v_resource.id IS NULL OR v_resource.currency<>'USD' OR v_resource.amount>500.00
       OR NOT EXISTS (SELECT 1 FROM billing_records b
         WHERE b.tenant_id=v_resource.tenant_id
           AND b.customer_id=v_resource.customer_id
           AND (b.id=v_resource.duplicate_of OR b.duplicate_of=v_resource.id))
       OR NOT supportguard_action_observation_bound(p_trusted_context,v_bindings,
         'query_billing_record','billing_record_id',v_resource.id,v_resource.version)
    THEN RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='refund_policy_denied'; END IF;
    v_payload:=jsonb_build_object('billing_record_id',v_resource.id,
      'customer_id',v_ticket.customer_id,'amount',v_resource.amount::text,
      'currency',v_resource.currency,'refund_reason',p_model_arguments->>'refund_reason',
      'business_version',v_resource.version);
    v_target:=jsonb_build_object('action_type','refund');
  ELSIF p_capability_name='propose_api_key_revocation' THEN
    IF (SELECT count(*) FROM jsonb_object_keys(p_model_arguments))<>3
       OR NOT p_model_arguments ?& ARRAY['api_key_id','reason','idempotency_key'] THEN
      RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='key_proposal_input_invalid';
    END IF;
    SELECT * INTO v_resource FROM api_key_metadata
     WHERE tenant_id=p_trusted_context->>'tenant_id'
       AND customer_id=v_ticket.customer_id
       AND key_id=p_model_arguments->>'api_key_id' AND status='active';
    IF v_resource.id IS NULL OR NOT supportguard_action_observation_bound(
      p_trusted_context,v_bindings,'query_api_key_metadata','api_key_id',
      v_resource.key_id,v_resource.version) THEN
      RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='key_proposal_policy_denied';
    END IF;
    v_payload:=jsonb_build_object('api_key_id',v_resource.key_id,
      'fingerprint',v_resource.fingerprint,'customer_id',v_ticket.customer_id,
      'reason',p_model_arguments->>'reason','business_version',v_resource.version);
    v_target:=jsonb_build_object('action_type','api_key_revocation');
  ELSE
    IF (SELECT count(*) FROM jsonb_object_keys(p_model_arguments))<>5
       OR NOT p_model_arguments ?& ARRAY[
         'subscription_id','change_type','target','reason','idempotency_key']
       OR jsonb_typeof(p_model_arguments->'target')<>'object' THEN
      RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='entitlement_input_invalid';
    END IF;
    SELECT * INTO v_resource FROM subscriptions
     WHERE tenant_id=p_trusted_context->>'tenant_id'
       AND customer_id=v_ticket.customer_id
       AND id=p_model_arguments->>'subscription_id';
    IF v_resource.id IS NULL OR NOT supportguard_action_observation_bound(
      p_trusted_context,v_bindings,'query_subscription','subscription_id',
      v_resource.id,v_resource.version) THEN
      RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='entitlement_policy_denied';
    END IF;
    v_target:=p_model_arguments->'target';
    IF p_model_arguments->>'change_type'='quota_change' THEN
      SELECT * INTO v_catalog FROM plan_catalog WHERE plan=v_resource.plan
       ORDER BY version DESC LIMIT 1;
      IF v_catalog.id IS NULL OR (SELECT count(*) FROM jsonb_object_keys(v_target))<>1
         OR (v_target ? 'rpm_limit' AND (v_target->>'rpm_limit')::integer
           NOT BETWEEN v_catalog.min_rpm AND v_catalog.max_rpm)
         OR (v_target ? 'concurrency_limit' AND
           (v_target->>'concurrency_limit')::integer
           NOT BETWEEN v_catalog.min_concurrency AND v_catalog.max_concurrency)
         OR NOT (v_target ? 'rpm_limit' OR v_target ? 'concurrency_limit') THEN
        RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='quota_target_denied';
      END IF;
    ELSIF p_model_arguments->>'change_type'<>'plan_change'
       OR (SELECT count(*) FROM jsonb_object_keys(v_target))<>1
       OR COALESCE(v_target->>'plan','')='' THEN
      RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='plan_target_denied';
    END IF;
    v_payload:=jsonb_build_object('subscription_id',v_resource.id,
      'customer_id',v_ticket.customer_id,
      'change_type',p_model_arguments->>'change_type','current',jsonb_build_object(
        'plan',v_resource.plan,'rpm_limit',v_resource.rpm_limit,
        'concurrency_limit',v_resource.concurrency_limit),
      'target',v_target,'reason',p_model_arguments->>'reason',
      'business_version',v_resource.version);
    v_target:=jsonb_build_object('action_type','entitlement_change');
  END IF;
  v_decision:=jsonb_build_object(
    'variant','proposal','capability_name',p_capability_name,
    'action_type',v_target->>'action_type',
    'resource_id',CASE v_target->>'action_type'
      WHEN 'refund' THEN v_payload->>'billing_record_id'
      WHEN 'api_key_revocation' THEN v_payload->>'api_key_id'
      ELSE v_payload->>'subscription_id' END,
    'resource_version',(v_payload->>'business_version')::integer,
    'model_arguments',p_model_arguments,
    'observation_binding_hash',v_binding_hash,
    'policy_version','supportguard-policy-gate.v1');
  IF ep->'causal_decision'<>v_decision OR encode(pg_catalog.sha256(convert_to(
       supportguard_canonical_jsonb(v_decision),'UTF8')),'hex')
       <>p_trusted_context->>'decision_hash' THEN
    RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='causal_decision_mismatch';
  END IF;
  v_action_hash:=encode(pg_catalog.sha256(convert_to(
    supportguard_canonical_jsonb(v_payload),'UTF8')),'hex');
  v_identity:=encode(pg_catalog.sha256(convert_to(supportguard_canonical_jsonb(
    jsonb_build_object('tenant_id',p_trusted_context->>'tenant_id',
      'run_id',p_trusted_context->>'run_id','action_type',v_target->>'action_type',
      'resource_id',CASE v_target->>'action_type'
        WHEN 'refund' THEN v_payload->>'billing_record_id'
        WHEN 'api_key_revocation' THEN v_payload->>'api_key_id'
        ELSE v_payload->>'subscription_id' END,
      'resource_version',(v_payload->>'business_version')::integer,
      'candidate_hash',v_action_hash)),'UTF8')),'hex');
  v_record_id:='proposal_'||substr(v_identity,1,32);
  INSERT INTO proposal_records(id,tenant_id,run_id,proposal_identity,action_type,
    resource_id,resource_version,action_payload,observation_binding,action_hash,status)
  VALUES(v_record_id,p_trusted_context->>'tenant_id',p_trusted_context->>'run_id',
    v_identity,v_target->>'action_type',CASE v_target->>'action_type'
      WHEN 'refund' THEN v_payload->>'billing_record_id'
      WHEN 'api_key_revocation' THEN v_payload->>'api_key_id'
      ELSE v_payload->>'subscription_id' END,
    (v_payload->>'business_version')::integer,v_payload,v_bindings,v_action_hash,'draft')
  ON CONFLICT (tenant_id,proposal_identity) DO NOTHING;
  SELECT id,action_hash,resource_id,resource_version INTO v_existing
    FROM proposal_records WHERE tenant_id=p_trusted_context->>'tenant_id'
      AND proposal_identity=v_identity;
  v_result:=jsonb_build_object('tool_call_id',ep->>'tool_call_id',
    'ticket_id',v_ticket.id,'proposal_id',v_existing.id,'status','draft',
    'action_type',v_target->>'action_type','action_hash',v_existing.action_hash,
    'resource_id',v_existing.resource_id,
    'resource_version',v_existing.resource_version,
    'idempotency_key',p_model_arguments->>'idempotency_key','source_refs',
    jsonb_build_array(jsonb_build_object('source_type','business_record',
      'source_id','proposal_record:'||v_existing.id,'observed_at',v_now)));
  RETURN jsonb_build_object('schema_version','mcp-wrapper-result.v1',
    'authorized',true,'capability_name',p_capability_name,
    'phase','execute','result',v_result);
END
$function$;
"""


def upgrade() -> None:
    # The migrator is an explicit NOINHERIT member of the NOLOGIN owner.  Keep
    # the transaction-local owner role through Alembic's version-row update.
    op.execute("SET LOCAL ROLE supportguard_owner")
    op.execute(_RESTRICT_ACTION_CAPABILITIES_SQL)
    op.execute(
        "REVOKE EXECUTE ON FUNCTION "
        "public.supportguard_action_mcp_create_support_escalation(jsonb,jsonb) "
        "FROM supportguard_action_mcp"
    )


def downgrade() -> None:
    raise RuntimeError("interview_escalation_retirement_downgrade_forbidden")
