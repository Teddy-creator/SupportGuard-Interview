"""Bind the refund demo to one truthful two-charge policy contract.

Revision ID: i203_demo_truthful_refund
Revises: i202_refund_fence_authority

The Interview baseline remains immutable.  This forward-only revision adds the
missing billing facts and makes the restricted Read MCP, Action MCP, approval
edit, and Worker effect paths consume the same canonical pair snapshot.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from supportguard.db.interview_baseline import execute_interview_migration_sql

revision: str = "i203_demo_truthful_refund"
down_revision: str | None = "i202_refund_fence_authority"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_BILLING_FACTS_SQL = r"""
-- Source tables use FORCE RLS.  Temporarily suspend only that owner-facing
-- flag inside Alembic's surrounding transaction so this deterministic,
-- all-tenant backfill can run; policies and RLS remain installed throughout.
ALTER TABLE public.billing_records NO FORCE ROW LEVEL SECURITY;
ALTER TABLE public.proposal_records NO FORCE ROW LEVEL SECURITY;

ALTER TABLE public.billing_records
  ADD COLUMN charged_at timestamptz,
  ADD COLUMN service_period_start date,
  ADD COLUMN service_period_end date;

UPDATE public.billing_records
SET charged_at=created_at,
    service_period_start=date_trunc('month',created_at AT TIME ZONE 'UTC')::date,
    service_period_end=(date_trunc('month',created_at AT TIME ZONE 'UTC')
      + interval '1 month')::date;

ALTER TABLE public.billing_records
  ALTER COLUMN charged_at SET NOT NULL,
  ALTER COLUMN service_period_start SET NOT NULL,
  ALTER COLUMN service_period_end SET NOT NULL,
  ADD CONSTRAINT ck_billing_records_service_period_valid
    CHECK (service_period_end>service_period_start),
  ADD CONSTRAINT ck_billing_records_duplicate_not_self
    CHECK (duplicate_of IS NULL OR duplicate_of<>id);

ALTER TABLE public.proposal_records
  ADD COLUMN refund_original_resource_id text,
  ADD COLUMN refund_original_version integer,
  ADD COLUMN refund_pair_hash text,
  ADD CONSTRAINT ck_proposal_records_refund_pair_binding_complete CHECK (
    (action_type='refund' AND (
      (refund_original_resource_id IS NULL AND refund_original_version IS NULL
        AND refund_pair_hash IS NULL)
      OR
      (refund_original_resource_id IS NOT NULL AND refund_original_version IS NOT NULL
        AND refund_pair_hash~'^[0-9a-f]{64}$')
    ))
    OR
    (action_type<>'refund' AND refund_original_resource_id IS NULL
      AND refund_original_version IS NULL AND refund_pair_hash IS NULL)
  );

CREATE FUNCTION public.supportguard_refund_pair_snapshot(
  p_tenant_id text,
  p_customer_id text,
  p_billing_record_id text,
  p_as_of timestamptz
) RETURNS jsonb
LANGUAGE plpgsql STABLE
SET search_path TO 'pg_catalog', 'public'
AS $function$
DECLARE
  v_target public.billing_records%ROWTYPE;
  v_original public.billing_records%ROWTYPE;
  v_checks jsonb;
  v_pair_payload jsonb;
  v_pair_hash text;
  v_eligible boolean;
BEGIN
  IF current_user<>'supportguard_owner' OR p_as_of IS NULL THEN
    RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='refund_pair_snapshot_forbidden';
  END IF;
  SELECT * INTO v_target FROM public.billing_records
  WHERE tenant_id=p_tenant_id AND customer_id=p_customer_id
    AND id=p_billing_record_id;
  IF v_target.id IS NULL THEN RETURN NULL; END IF;
  IF v_target.duplicate_of IS NOT NULL THEN
    SELECT * INTO v_original FROM public.billing_records
    WHERE tenant_id=p_tenant_id AND customer_id=p_customer_id
      AND id=v_target.duplicate_of;
  END IF;
  v_checks:=pg_catalog.jsonb_build_object(
    'same_scope',v_original.id IS NOT NULL
      AND v_target.tenant_id=v_original.tenant_id
      AND v_target.customer_id=v_original.customer_id,
    'explicit_relation',v_original.id IS NOT NULL
      AND v_target.id<>v_original.id
      AND v_target.duplicate_of=v_original.id,
    'both_charged',v_original.id IS NOT NULL
      AND v_target.status='charged' AND v_original.status='charged',
    'same_amount',v_original.id IS NOT NULL
      AND v_target.amount=v_original.amount,
    'same_currency',v_original.id IS NOT NULL
      AND v_target.currency=v_original.currency,
    'same_service_period',v_original.id IS NOT NULL
      AND v_target.service_period_start=v_original.service_period_start
      AND v_target.service_period_end=v_original.service_period_end
      AND v_target.service_period_start<v_target.service_period_end,
    'within_application_window',v_original.id IS NOT NULL
      AND v_original.charged_at<=p_as_of AND v_target.charged_at<=p_as_of
      AND p_as_of-v_target.charged_at<=interval '30 days'
  );
  v_eligible:=NOT EXISTS(
    SELECT 1 FROM pg_catalog.jsonb_each(v_checks) item
    WHERE item.value<>'true'::jsonb
  );
  IF v_original.id IS NOT NULL THEN
    v_pair_payload:=pg_catalog.jsonb_build_object(
      'original',pg_catalog.jsonb_build_object(
        'amount',v_original.amount::text,
        'billing_record_id',v_original.id,
        'charged_at',public.supportguard_internal_format_utc_timestamp(
          v_original.charged_at),
        'currency',v_original.currency,
        'customer_id',v_original.customer_id,
        'service_period_end',v_original.service_period_end::text,
        'service_period_start',v_original.service_period_start::text,
        'status',v_original.status,
        'tenant_id',v_original.tenant_id,
        'version',v_original.version
      ),
      'policy_version','refund-pair.v1',
      'target',pg_catalog.jsonb_build_object(
        'amount',v_target.amount::text,
        'billing_record_id',v_target.id,
        'charged_at',public.supportguard_internal_format_utc_timestamp(
          v_target.charged_at),
        'currency',v_target.currency,
        'customer_id',v_target.customer_id,
        'duplicate_of',v_target.duplicate_of,
        'service_period_end',v_target.service_period_end::text,
        'service_period_start',v_target.service_period_start::text,
        'status',v_target.status,
        'tenant_id',v_target.tenant_id,
        'version',v_target.version
      ),
      'window_days',30
    );
    v_pair_hash:=pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
      public.supportguard_canonical_jsonb(v_pair_payload),'UTF8')),'hex');
  END IF;
  RETURN pg_catalog.jsonb_strip_nulls(pg_catalog.jsonb_build_object(
    'billing_record_id',v_target.id,
    'amount',v_target.amount::text,
    'currency',v_target.currency,
    'status',v_target.status,
    'charged_at',v_target.charged_at,
    'service_period_start',v_target.service_period_start,
    'service_period_end',v_target.service_period_end,
    'duplicate_of',v_target.duplicate_of,
    'version',v_target.version,
    'original_billing_record_id',v_original.id,
    'original_amount',CASE WHEN v_original.id IS NOT NULL
      THEN v_original.amount::text END,
    'original_currency',v_original.currency,
    'original_status',v_original.status,
    'original_charged_at',v_original.charged_at,
    'original_service_period_start',v_original.service_period_start,
    'original_service_period_end',v_original.service_period_end,
    'original_version',v_original.version,
    'duplicate_pair_eligible',v_eligible,
    'refund_pair_hash',v_pair_hash,
    'refund_pair_checks',v_checks
  ));
END
$function$;

CREATE FUNCTION public.supportguard_billing_refund_identity_guard()
RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path TO 'pg_catalog', 'public'
AS $function$
BEGIN
  IF current_user<>'supportguard_owner' THEN
    RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='billing_identity_guard_forbidden';
  END IF;
  IF (NEW.tenant_id,NEW.customer_id,NEW.amount,NEW.currency,NEW.charged_at,
      NEW.service_period_start,NEW.service_period_end,NEW.duplicate_of)
     IS DISTINCT FROM
     (OLD.tenant_id,OLD.customer_id,OLD.amount,OLD.currency,OLD.charged_at,
      OLD.service_period_start,OLD.service_period_end,OLD.duplicate_of) THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='billing_refund_identity_immutable';
  END IF;
  RETURN NEW;
END
$function$;

REVOKE ALL ON FUNCTION public.supportguard_refund_pair_snapshot(
  text,text,text,timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.supportguard_billing_refund_identity_guard()
FROM PUBLIC;

CREATE TRIGGER trg_billing_refund_identity_guard
BEFORE UPDATE ON public.billing_records
FOR EACH ROW EXECUTE FUNCTION public.supportguard_billing_refund_identity_guard();

WITH eligible AS (
  SELECT p.tenant_id,p.id,payload.value
  FROM public.proposal_records p
  JOIN public.agent_runs r ON r.tenant_id=p.tenant_id AND r.id=p.run_id
  JOIN public.support_tickets t
    ON t.tenant_id=r.tenant_id AND t.id=r.ticket_id
   AND t.customer_id=r.customer_id
  CROSS JOIN LATERAL (
    SELECT public.supportguard_refund_pair_snapshot(
      p.tenant_id,t.customer_id,p.resource_id,clock_timestamp()
    ) AS value
  ) payload
  WHERE p.action_type='refund'
    AND p.refund_original_resource_id IS NULL
    AND p.refund_original_version IS NULL
    AND p.refund_pair_hash IS NULL
    AND payload.value->>'duplicate_pair_eligible'='true'
)
UPDATE public.proposal_records p
SET refund_original_resource_id=eligible.value->>'original_billing_record_id',
    refund_original_version=(eligible.value->>'original_version')::integer,
    refund_pair_hash=eligible.value->>'refund_pair_hash'
FROM eligible
WHERE p.tenant_id=eligible.tenant_id AND p.id=eligible.id;

ALTER TABLE public.billing_records FORCE ROW LEVEL SECURITY;
ALTER TABLE public.proposal_records FORCE ROW LEVEL SECURITY;
"""


_READ_REFUND_PAIR_SQL = r"""
CREATE FUNCTION public.supportguard_read_mcp_query_billing_record_v203(
  p_model_arguments jsonb,
  p_trusted_context jsonb
) RETURNS jsonb
LANGUAGE plpgsql
SET search_path TO 'pg_catalog', 'public', 'supportguard_control'
AS $function$
DECLARE
  v_result jsonb;
  v_pair jsonb;
  v_customer_id text;
  v_now timestamptz:=clock_timestamp();
BEGIN
  IF current_user<>'supportguard_owner' THEN
    RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='read_billing_v203_forbidden';
  END IF;
  v_result:=public.supportguard_read_mcp_execute(
    'query_billing_record',p_model_arguments,p_trusted_context);
  IF v_result->>'authorized'<>'true' OR v_result->>'phase'<>'execute' THEN
    RETURN v_result;
  END IF;
  SELECT t.customer_id INTO v_customer_id
  FROM public.support_tickets t
  WHERE t.tenant_id=p_trusted_context->>'tenant_id'
    AND t.id=v_result#>>'{result,ticket_id}';
  IF v_customer_id IS NULL THEN
    RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='read_billing_scope_unavailable';
  END IF;
  v_pair:=public.supportguard_refund_pair_snapshot(
    p_trusted_context->>'tenant_id',v_customer_id,
    v_result#>>'{result,billing_record_id}',v_now);
  IF v_pair IS NULL THEN
    RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='read_billing_pair_unavailable';
  END IF;
  v_result:=pg_catalog.jsonb_set(
    v_result,'{result}',
    (v_result->'result') || v_pair || pg_catalog.jsonb_build_object(
      'source_refs',coalesce(v_result#>'{result,source_refs}','[]'::jsonb)
        || CASE WHEN v_pair->>'original_billing_record_id' IS NULL
          THEN '[]'::jsonb ELSE pg_catalog.jsonb_build_array(
            pg_catalog.jsonb_build_object(
              'source_type','business_record','source_id',
              'billing_record:'||(v_pair->>'original_billing_record_id'),
              'observed_at',v_now
            )
          ) END
    ),false
  );
  RETURN v_result;
END
$function$;

CREATE OR REPLACE FUNCTION public.supportguard_read_mcp_query_billing_record(
  p_model_arguments jsonb,
  p_trusted_context jsonb
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path TO 'pg_catalog', 'public'
AS $function$
DECLARE v_allowed boolean;
BEGIN
  IF session_user<>'supportguard_read_mcp' THEN
    RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='security_definer_session_user_forbidden';
  END IF;
  IF jsonb_typeof(p_model_arguments)<>'object'
     OR jsonb_typeof(p_trusted_context)<>'object'
     OR p_trusted_context->>'schema_version'<>'read-mcp-wrapper.v1'
     OR p_trusted_context->>'tool_name'<>'query_billing_record'
     OR p_trusted_context->>'phase' NOT IN ('reserve','recheck','execute')
     OR (SELECT array_agg(key ORDER BY key)
         FROM jsonb_object_keys(p_trusted_context) key)
        <> ARRAY['call_deadline','delivery_generation','fencing_token','job_id',
                 'logical_invocation_id','phase','provider_tool_call_id','run_id',
                 'schema_version','segment_id','tenant_id','tool_attempt_id',
                 'tool_name','trace_origin','transport_attempt_id','transport_ordinal']
  THEN
    RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='invalid read MCP wrapper envelope';
  END IF;
  IF p_trusted_context->>'phase'='reserve'
     AND p_trusted_context->>'trace_origin'='agent_read_tool' THEN
    v_allowed:=public.supportguard_mcp_consume_read_reservation(
      p_trusted_context->>'tenant_id',p_trusted_context->>'run_id',
      p_trusted_context->>'job_id',p_trusted_context->>'segment_id',
      (p_trusted_context->>'fencing_token')::bigint,
      (p_trusted_context->>'delivery_generation')::integer,
      p_trusted_context->>'logical_invocation_id',
      p_trusted_context->>'tool_attempt_id',
      p_trusted_context->>'transport_attempt_id',
      (p_trusted_context->>'transport_ordinal')::integer,
      'query_billing_record',p_trusted_context->>'provider_tool_call_id',
      (p_trusted_context->>'call_deadline')::timestamptz);
  ELSE
    v_allowed:=public.supportguard_mcp_verify_fence(
      p_trusted_context->>'tenant_id',p_trusted_context->>'run_id',
      p_trusted_context->>'job_id',p_trusted_context->>'segment_id',
      (p_trusted_context->>'fencing_token')::bigint,
      (p_trusted_context->>'delivery_generation')::integer);
  END IF;
  IF p_trusted_context->>'phase'='execute' THEN
    IF v_allowed IS DISTINCT FROM true THEN
      RETURN pg_catalog.jsonb_build_object(
        'schema_version','mcp-wrapper-result.v1','authorized',false,
        'tool_name','query_billing_record','phase','execute');
    END IF;
    RETURN public.supportguard_read_mcp_query_billing_record_v203(
      p_model_arguments,p_trusted_context);
  END IF;
  RETURN pg_catalog.jsonb_build_object(
    'schema_version','mcp-wrapper-result.v1','authorized',v_allowed,
    'tool_name','query_billing_record','phase',p_trusted_context->>'phase');
END
$function$;

REVOKE ALL ON FUNCTION public.supportguard_read_mcp_query_billing_record_v203(
  jsonb,jsonb
) FROM PUBLIC;
"""


_REFUND_GUARDS_SQL = r"""
CREATE FUNCTION public.supportguard_refund_proposal_pair_current_v203(
  p_tenant_id text,
  p_customer_id text,
  p_proposal_id text,
  p_resource_id text,
  p_resource_version integer,
  p_as_of timestamptz
) RETURNS boolean
LANGUAGE plpgsql STABLE
SET search_path TO 'pg_catalog', 'public'
AS $function$
DECLARE v_proposal public.proposal_records%ROWTYPE; v_pair jsonb;
BEGIN
  IF current_user<>'supportguard_owner' THEN RETURN false; END IF;
  SELECT * INTO v_proposal FROM public.proposal_records
  WHERE tenant_id=p_tenant_id AND id=p_proposal_id;
  IF v_proposal.id IS NULL OR v_proposal.action_type<>'refund'
     OR v_proposal.resource_id IS DISTINCT FROM p_resource_id
     OR v_proposal.resource_version IS DISTINCT FROM p_resource_version
     OR v_proposal.refund_original_resource_id IS NULL
     OR v_proposal.refund_original_version IS NULL
     OR v_proposal.refund_pair_hash IS NULL THEN
    RETURN false;
  END IF;
  v_pair:=public.supportguard_refund_pair_snapshot(
    p_tenant_id,p_customer_id,p_resource_id,p_as_of);
  RETURN v_pair IS NOT NULL
    AND v_pair->>'duplicate_pair_eligible'='true'
    AND v_pair->>'original_billing_record_id'
      =v_proposal.refund_original_resource_id
    AND (v_pair->>'original_version')::integer
      =v_proposal.refund_original_version
    AND v_pair->>'refund_pair_hash'=v_proposal.refund_pair_hash;
END
$function$;

CREATE FUNCTION public.supportguard_refund_human_decision_guard_v203()
RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path TO 'pg_catalog', 'public'
AS $function$
DECLARE v_approval public.approval_requests%ROWTYPE;
BEGIN
  IF NEW.decision NOT IN ('approve','edit_and_approve') THEN RETURN NEW; END IF;
  SELECT * INTO v_approval FROM public.approval_requests
  WHERE tenant_id=NEW.tenant_id AND id=NEW.approval_id;
  IF v_approval.action_type='refund' AND NOT
     public.supportguard_refund_proposal_pair_current_v203(
       v_approval.tenant_id,v_approval.customer_id,v_approval.proposal_id,
       v_approval.resource_id,v_approval.business_version,clock_timestamp()
     ) THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='refund_pair_approval_stale';
  END IF;
  RETURN NEW;
END
$function$;

CREATE TRIGGER trg_refund_human_decision_guard_v203
BEFORE INSERT ON public.human_decisions
FOR EACH ROW EXECUTE FUNCTION public.supportguard_refund_human_decision_guard_v203();

CREATE FUNCTION public.supportguard_refund_business_action_guard_v203()
RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path TO 'pg_catalog', 'public'
AS $function$
DECLARE v_approval public.approval_requests%ROWTYPE;
BEGIN
  IF NEW.action_type<>'refund' THEN RETURN NEW; END IF;
  SELECT * INTO v_approval FROM public.approval_requests
  WHERE tenant_id=NEW.tenant_id AND id=NEW.approval_id;
  IF v_approval.id IS NULL
     OR NEW.customer_id IS DISTINCT FROM v_approval.customer_id
     OR NEW.resource_id IS DISTINCT FROM v_approval.resource_id
     OR NEW.resource_version IS DISTINCT FROM v_approval.business_version
     OR NOT public.supportguard_refund_proposal_pair_current_v203(
       v_approval.tenant_id,v_approval.customer_id,v_approval.proposal_id,
       v_approval.resource_id,v_approval.business_version,clock_timestamp()
     ) THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='refund_pair_execution_stale';
  END IF;
  RETURN NEW;
END
$function$;

CREATE TRIGGER trg_refund_business_action_guard_v203
BEFORE INSERT ON public.business_actions
FOR EACH ROW EXECUTE FUNCTION public.supportguard_refund_business_action_guard_v203();

REVOKE ALL ON FUNCTION public.supportguard_refund_proposal_pair_current_v203(
  text,text,text,text,integer,timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.supportguard_refund_human_decision_guard_v203()
FROM PUBLIC;
REVOKE ALL ON FUNCTION public.supportguard_refund_business_action_guard_v203()
FROM PUBLIC;
"""


_WORKER_REFUND_STALE_SQL = r"""
ALTER FUNCTION public.supportguard_worker_execute_approved_action(
  text,text,text,bigint
) RENAME TO supportguard_worker_execute_approved_action_i202;

REVOKE ALL ON FUNCTION public.supportguard_worker_execute_approved_action_i202(
  text,text,text,bigint
) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.supportguard_worker_execute_approved_action_i202(
  text,text,text,bigint
) FROM supportguard_worker;

CREATE FUNCTION public.supportguard_worker_execute_approved_action(
  p_approval_id text,
  p_human_decision_id text,
  p_job_id text,
  p_fencing_token bigint
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path TO 'pg_catalog', 'public', 'supportguard_control'
AS $function$
DECLARE
  v_tenant_id text:=current_setting('app.tenant_id',true);
  v_message text;
  v_job_probe public.runtime_jobs%ROWTYPE;
  v_ticket public.support_tickets%ROWTYPE;
  v_approval public.approval_requests%ROWTYPE;
  v_proposal public.proposal_records%ROWTYPE;
  v_run public.agent_runs%ROWTYPE;
  v_job public.runtime_jobs%ROWTYPE;
  v_decision public.human_decisions%ROWTYPE;
  v_rows bigint;
BEGIN
  IF session_user<>'supportguard_worker' THEN
    RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='worker_role_required';
  END IF;
  BEGIN
    RETURN public.supportguard_worker_execute_approved_action_i202(
      p_approval_id,p_human_decision_id,p_job_id,p_fencing_token);
  EXCEPTION WHEN check_violation THEN
    GET STACKED DIAGNOSTICS v_message=MESSAGE_TEXT;
    IF v_message<>'refund_pair_execution_stale' THEN RAISE; END IF;
  END;

  -- The delegated capability reached the final effect INSERT, where the
  -- refund-pair trigger rejected it.  That subtransaction was rolled back.
  -- Reacquire the frozen aggregate locks and prove the same worker authority
  -- before publishing the normal stale result; no business effect occurred.
  SELECT * INTO v_job_probe FROM public.runtime_jobs job
  WHERE job.tenant_id=v_tenant_id AND job.id=p_job_id;
  IF v_job_probe.id IS NULL OR v_job_probe.kind<>'approval_resume'
     OR v_job_probe.approval_id IS DISTINCT FROM p_approval_id
     OR v_job_probe.ticket_id IS NULL OR v_job_probe.run_id IS NULL THEN
    RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='action_binding_invalid';
  END IF;
  SELECT * INTO v_ticket FROM public.support_tickets ticket
  WHERE ticket.tenant_id=v_tenant_id AND ticket.id=v_job_probe.ticket_id
  FOR UPDATE;
  SELECT * INTO v_approval FROM public.approval_requests approval
  WHERE approval.tenant_id=v_tenant_id AND approval.id=p_approval_id
    AND approval.ticket_id=v_ticket.id AND approval.run_id=v_job_probe.run_id
    AND approval.customer_id=v_ticket.customer_id
  FOR UPDATE;
  PERFORM proposal.id FROM public.proposal_records proposal
  WHERE proposal.tenant_id=v_tenant_id AND (
    proposal.id=v_approval.proposal_id OR (
      proposal.action_type=v_approval.action_type
      AND proposal.resource_id=v_approval.resource_id
      AND proposal.status IN ('draft','bound')
    )
  ) ORDER BY proposal.id FOR UPDATE;
  SELECT * INTO v_proposal FROM public.proposal_records proposal
  WHERE proposal.tenant_id=v_tenant_id AND proposal.id=v_approval.proposal_id
    AND proposal.run_id=v_approval.run_id;
  SELECT * INTO v_run FROM public.agent_runs run
  WHERE run.tenant_id=v_tenant_id AND run.id=v_approval.run_id
    AND run.ticket_id=v_approval.ticket_id AND run.customer_id=v_approval.customer_id
  FOR UPDATE;
  SELECT * INTO v_job FROM public.runtime_jobs job
  WHERE job.tenant_id=v_tenant_id AND job.id=v_job_probe.id
    AND job.kind='approval_resume' AND job.approval_id=v_approval.id
    AND job.run_id=v_approval.run_id AND job.ticket_id=v_approval.ticket_id
    AND job.dispatch_sequence=v_job_probe.dispatch_sequence
  FOR UPDATE;
  SELECT * INTO v_decision FROM public.human_decisions decision_row
  WHERE decision_row.tenant_id=v_tenant_id AND decision_row.id=p_human_decision_id
    AND decision_row.approval_id=v_approval.id;
  IF v_ticket.id IS NULL OR v_approval.id IS NULL OR v_proposal.id IS NULL
     OR v_run.id IS NULL OR v_job.id IS NULL OR v_decision.id IS NULL
     OR v_approval.action_type<>'refund' OR v_approval.status<>'approved'
     OR v_approval.consumed_at IS NOT NULL
     OR v_decision.decision NOT IN ('approve','edit_and_approve')
     OR v_job.status<>'leased' OR v_job.fencing_token<>p_fencing_token
     OR v_run.active_job_id IS DISTINCT FROM v_job.id
     OR v_run.active_fencing_token IS DISTINCT FROM v_job.fencing_token
     OR public.supportguard_refund_proposal_pair_current_v203(
       v_approval.tenant_id,v_approval.customer_id,v_approval.proposal_id,
       v_approval.resource_id,v_approval.business_version,clock_timestamp()
     ) THEN
    RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='action_binding_invalid';
  END IF;
  UPDATE public.approval_requests
  SET status='stale',status_version=status_version+1,
      updated_at=clock_timestamp()
  WHERE tenant_id=v_tenant_id AND id=v_approval.id AND status='approved'
    AND status_version=v_approval.status_version AND consumed_at IS NULL;
  GET DIAGNOSTICS v_rows=ROW_COUNT;
  IF v_rows<>1 THEN
    RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='approval_state_conflict';
  END IF;
  UPDATE public.proposal_records
  SET status='stale',status_version=status_version+1,
      updated_at=clock_timestamp()
  WHERE tenant_id=v_tenant_id AND action_type='refund'
    AND resource_id=v_approval.resource_id AND status IN ('draft','bound');
  RETURN pg_catalog.jsonb_build_object(
    'schema_version','runtime-action-result.v1',
    'approval_id',v_approval.id,
    'human_decision_id',v_decision.id,
    'business_action_id',NULL,
    'action_type','refund',
    'resource_id',v_approval.resource_id,
    'status','stale','reused',false,
    'reason','refund_pair_execution_stale');
END
$function$;

REVOKE ALL ON FUNCTION public.supportguard_worker_execute_approved_action(
  text,text,text,bigint
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.supportguard_worker_execute_approved_action(
  text,text,text,bigint
) TO supportguard_worker;
"""


_CUSTOMER_REFUND_DISPLAY_SQL = r"""
CREATE FUNCTION public.supportguard_api_get_refund_display(
  p_customer_id text,
  p_approval_ids text[]
) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path TO 'pg_catalog', 'public'
AS $function$
DECLARE v_tenant_id text:=current_setting('app.tenant_id',true);
BEGIN
  IF session_user<>'supportguard_api'
     OR current_setting('app.principal_role',true)
       NOT IN ('customer','customer_member','customer_admin')
     OR current_setting('app.subject_customer_id',true) IS DISTINCT FROM p_customer_id
     OR p_approval_ids IS NULL OR cardinality(p_approval_ids)>100
     OR EXISTS(SELECT 1 FROM unnest(p_approval_ids) value WHERE value IS NULL OR value='')
  THEN
    RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='refund_display_forbidden';
  END IF;
  RETURN coalesce((
    SELECT pg_catalog.jsonb_object_agg(a.id,pg_catalog.jsonb_strip_nulls(
      pg_catalog.jsonb_build_object(
        'billing_record_id',a.resource_id,
        'amount',b.amount::text,
        'currency',b.currency,
        'original_billing_record_id',p.refund_original_resource_id,
        'duplicate_pair_verified',(
          p.refund_original_resource_id IS NOT NULL
          AND p.refund_original_version IS NOT NULL
          AND p.refund_pair_hash IS NOT NULL
          AND pair.value->>'duplicate_pair_eligible'='true'
          AND pair.value->>'refund_pair_hash'=p.refund_pair_hash
        ),
        'service_period_start',b.service_period_start,
        'service_period_end',b.service_period_end
      )
    ))
    FROM public.approval_requests a
    JOIN public.proposal_records p
      ON p.tenant_id=a.tenant_id AND p.id=a.proposal_id
    JOIN public.billing_records b
      ON b.tenant_id=a.tenant_id AND b.customer_id=a.customer_id
     AND b.id=a.resource_id
    CROSS JOIN LATERAL (SELECT public.supportguard_refund_pair_snapshot(
      a.tenant_id,a.customer_id,a.resource_id,clock_timestamp()) AS value) pair
    WHERE a.tenant_id=v_tenant_id AND a.customer_id=p_customer_id
      AND a.action_type='refund' AND a.id=ANY(p_approval_ids)
  ),'{}'::jsonb);
END
$function$;

REVOKE ALL ON FUNCTION public.supportguard_api_get_refund_display(text,text[])
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.supportguard_api_get_refund_display(text,text[])
TO supportguard_api;
"""


_TERMINAL_STATE_SQL = r"""
ALTER TABLE public.conversation_turns NO FORCE ROW LEVEL SECURITY;
ALTER TABLE public.approval_requests NO FORCE ROW LEVEL SECURITY;
ALTER TABLE public.proposal_withdrawals NO FORCE ROW LEVEL SECURITY;

ALTER TABLE public.conversation_turns
DROP CONSTRAINT ck_conversation_turns_ck_conversation_turn_result_state;
ALTER TABLE public.conversation_turns
ADD CONSTRAINT ck_conversation_turns_ck_conversation_turn_result_state CHECK (
  result_state IS NULL OR result_state IN (
    'answered','answered_limited','needs_clarification','refused',
    'proposal_created','human_queue','failed','stale','rejected','withdrawn'
  )
);

UPDATE public.conversation_turns turn_row
SET result_state='rejected',updated_at=clock_timestamp()
FROM public.approval_requests approval
JOIN public.human_decisions decision
  ON decision.tenant_id=approval.tenant_id
 AND decision.approval_id=approval.id AND decision.decision='reject'
WHERE turn_row.tenant_id=approval.tenant_id
  AND turn_row.id=approval.origin_turn_id
  AND turn_row.result_state='refused';

UPDATE public.conversation_turns turn_row
SET result_state='withdrawn',updated_at=clock_timestamp()
FROM public.approval_requests approval
JOIN public.proposal_withdrawals withdrawal
  ON withdrawal.tenant_id=approval.tenant_id
 AND withdrawal.approval_id=approval.id
WHERE turn_row.tenant_id=approval.tenant_id
  AND turn_row.id=approval.origin_turn_id
  AND turn_row.result_state='refused';

ALTER TABLE public.conversation_turns FORCE ROW LEVEL SECURITY;
ALTER TABLE public.approval_requests FORCE ROW LEVEL SECURITY;
ALTER TABLE public.proposal_withdrawals FORCE ROW LEVEL SECURITY;

CREATE FUNCTION public.supportguard_conversation_action_terminal_state_v203()
RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path TO 'pg_catalog', 'public'
AS $function$
DECLARE v_run_id text; v_turn_id text; v_result text;
BEGIN
  IF TG_TABLE_NAME='human_decisions' THEN
    IF NEW.decision<>'reject' THEN RETURN NEW; END IF;
    SELECT run_id,origin_turn_id INTO v_run_id,v_turn_id
    FROM public.approval_requests
    WHERE tenant_id=NEW.tenant_id AND id=NEW.approval_id;
    v_result:='rejected';
  ELSE
    SELECT run_id,origin_turn_id INTO v_run_id,v_turn_id
    FROM public.approval_requests
    WHERE tenant_id=NEW.tenant_id AND id=NEW.approval_id;
    v_result:='withdrawn';
  END IF;
  UPDATE public.conversation_turns
  SET activity_state='completed',result_state=v_result,
      completed_at=coalesce(completed_at,clock_timestamp()),updated_at=clock_timestamp()
  WHERE tenant_id=NEW.tenant_id AND id=v_turn_id AND run_id=v_run_id;
  RETURN NEW;
END
$function$;

CREATE TRIGGER trg_conversation_rejected_state_v203
AFTER INSERT ON public.human_decisions
FOR EACH ROW EXECUTE FUNCTION public.supportguard_conversation_action_terminal_state_v203();
CREATE TRIGGER trg_conversation_withdrawn_state_v203
AFTER INSERT ON public.proposal_withdrawals
FOR EACH ROW EXECUTE FUNCTION public.supportguard_conversation_action_terminal_state_v203();

REVOKE ALL ON FUNCTION public.supportguard_conversation_action_terminal_state_v203()
FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.supportguard_api_list_conversations(
  p_customer_id text,
  p_query text,
  p_cursor text,
  p_limit integer
) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path TO 'pg_catalog', 'public'
AS $function$
BEGIN
  IF session_user<>'supportguard_api' OR p_limit NOT BETWEEN 1 AND 50 THEN
    RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='conversation_query_invalid';
  END IF;
  IF NOT EXISTS(
    SELECT 1 FROM public.customers c
    WHERE c.tenant_id=current_setting('app.tenant_id',true)
      AND c.id=p_customer_id
  ) THEN
    RETURN pg_catalog.jsonb_build_object('items','[]'::jsonb,'next_cursor',NULL);
  END IF;
  RETURN (WITH selected AS (
    SELECT t.* FROM public.support_tickets t
    WHERE t.tenant_id=current_setting('app.tenant_id',true)
      AND t.customer_id=p_customer_id
      AND (p_query IS NULL OR p_query=''
        OR pg_catalog.to_tsvector('simple',coalesce(t.title,''))
          @@ pg_catalog.plainto_tsquery('simple',p_query)
        OR t.title ILIKE '%'||p_query||'%'
        OR EXISTS(
          SELECT 1 FROM public.ticket_messages m
          WHERE m.tenant_id=t.tenant_id AND m.ticket_id=t.id
            AND (pg_catalog.to_tsvector('simple',m.content)
              @@ pg_catalog.plainto_tsquery('simple',p_query)
              OR m.content ILIKE '%'||p_query||'%')
        ))
      AND (p_cursor IS NULL OR (t.last_message_at,t.id)<(
        SELECT c.last_message_at,c.id FROM public.support_tickets c
        WHERE c.tenant_id=t.tenant_id AND c.id=p_cursor
      ))
    ORDER BY t.last_message_at DESC,t.id DESC LIMIT p_limit+1
  ), page AS (
    SELECT * FROM selected ORDER BY last_message_at DESC,id DESC LIMIT p_limit
  )
  SELECT pg_catalog.jsonb_build_object(
    'items',coalesce((SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
      'id',t.id,'title',coalesce(t.title,'未命名对话'),'lifecycle',t.lifecycle,
      'automation_mode',t.automation_mode,'activity_label',CASE
        WHEN EXISTS(SELECT 1 FROM public.approval_requests a
          WHERE a.tenant_id=t.tenant_id AND a.ticket_id=t.id
            AND a.status IN ('approved','executing'))
          THEN '正在执行已批准操作'
        WHEN EXISTS(SELECT 1 FROM public.approval_requests a
          WHERE a.tenant_id=t.tenant_id AND a.ticket_id=t.id
            AND a.status='pending') THEN '等待审批'
        WHEN EXISTS(SELECT 1 FROM public.conversation_turns x
          WHERE x.tenant_id=t.tenant_id AND x.ticket_id=t.id
            AND x.activity_state='running') THEN '正在处理'
        WHEN EXISTS(SELECT 1 FROM public.conversation_turns x
          WHERE x.tenant_id=t.tenant_id AND x.ticket_id=t.id
            AND x.activity_state='queued') THEN '排队中'
        WHEN t.automation_mode='human_queue' THEN '自动处理已停止'
        WHEN (SELECT x.result_state FROM public.conversation_turns x
          WHERE x.tenant_id=t.tenant_id AND x.ticket_id=t.id
          ORDER BY x.ordinal DESC,x.id DESC LIMIT 1)='needs_clarification'
          THEN '需要补充信息'
        WHEN (SELECT x.result_state FROM public.conversation_turns x
          WHERE x.tenant_id=t.tenant_id AND x.ticket_id=t.id
          ORDER BY x.ordinal DESC,x.id DESC LIMIT 1)='rejected'
          THEN '审批者已拒绝'
        WHEN (SELECT x.result_state FROM public.conversation_turns x
          WHERE x.tenant_id=t.tenant_id AND x.ticket_id=t.id
          ORDER BY x.ordinal DESC,x.id DESC LIMIT 1)='withdrawn'
          THEN '申请已撤回'
        WHEN (SELECT x.result_state FROM public.conversation_turns x
          WHERE x.tenant_id=t.tenant_id AND x.ticket_id=t.id
          ORDER BY x.ordinal DESC,x.id DESC LIMIT 1)='stale'
          THEN '业务事实已变化'
        WHEN (SELECT x.result_state FROM public.conversation_turns x
          WHERE x.tenant_id=t.tenant_id AND x.ticket_id=t.id
          ORDER BY x.ordinal DESC,x.id DESC LIMIT 1)='refused'
          THEN '请求未执行'
        WHEN (SELECT x.result_state FROM public.conversation_turns x
          WHERE x.tenant_id=t.tenant_id AND x.ticket_id=t.id
          ORDER BY x.ordinal DESC,x.id DESC LIMIT 1)='failed'
          THEN '本轮未完成'
        WHEN (SELECT x.result_state FROM public.conversation_turns x
          WHERE x.tenant_id=t.tenant_id AND x.ticket_id=t.id
          ORDER BY x.ordinal DESC,x.id DESC LIMIT 1)='proposal_created'
          THEN '等待审批'
        WHEN (SELECT x.result_state FROM public.conversation_turns x
          WHERE x.tenant_id=t.tenant_id AND x.ticket_id=t.id
          ORDER BY x.ordinal DESC,x.id DESC LIMIT 1)='answered_limited'
          THEN '已给出有限结论'
        WHEN (SELECT x.result_state FROM public.conversation_turns x
          WHERE x.tenant_id=t.tenant_id AND x.ticket_id=t.id
          ORDER BY x.ordinal DESC,x.id DESC LIMIT 1)='answered'
          THEN '已回答'
        WHEN t.lifecycle='archived' THEN '已归档'
        ELSE '等待处理' END,
      'latest_summary',(SELECT left(regexp_replace(m.content,'[[:space:]]+',' ','g'),120)
        FROM public.ticket_messages m
        WHERE m.tenant_id=t.tenant_id AND m.ticket_id=t.id
        ORDER BY m.conversation_sequence DESC,m.id DESC LIMIT 1),
      'pending_action_count',(SELECT count(*) FROM public.approval_requests a
        WHERE a.tenant_id=t.tenant_id AND a.ticket_id=t.id
          AND a.status IN ('pending','approved','executing')),
      'updated_at',t.last_message_at
    ) ORDER BY t.last_message_at DESC,t.id DESC) FROM page t),'[]'::jsonb),
    'next_cursor',CASE WHEN (SELECT count(*) FROM selected)>p_limit
      THEN (SELECT id FROM page ORDER BY last_message_at,id LIMIT 1)
      ELSE NULL END));
END
$function$;
"""


_PROPOSE_REFUND_PAIR_SQL = r"""
CREATE FUNCTION public.supportguard_action_refund_observation_bound_v203(
  p_trusted_context jsonb,
  p_bindings jsonb,
  p_pair jsonb
) RETURNS boolean
LANGUAGE plpgsql
SET search_path TO 'pg_catalog', 'public'
AS $function$
DECLARE v_target jsonb; v_count integer;
BEGIN
  IF current_user<>'supportguard_owner'
     OR jsonb_typeof(p_bindings)<>'array'
     OR p_pair->>'duplicate_pair_eligible'<>'true' THEN
    RETURN false;
  END IF;
  SELECT count(*),min(value::text)::jsonb INTO v_count,v_target
  FROM pg_catalog.jsonb_array_elements(p_bindings)
  WHERE value->>'tool_name'='query_billing_record'
    AND value->>'status'='ok'
    AND value->>'resource_field'='billing_record_id'
    AND value->>'resource_id'=p_pair->>'billing_record_id'
    AND (value->>'resource_version')::integer=(p_pair->>'version')::integer;
  IF v_count<>1 THEN RETURN false; END IF;
  RETURN EXISTS(
    SELECT 1 FROM public.tool_observations o
    JOIN public.tool_invocations i ON i.id=o.invocation_id
    JOIN public.turn_groups g ON g.id=i.turn_group_id
    WHERE o.id=v_target->>'observation_id'
      AND o.invocation_id=v_target->>'invocation_id'
      AND o.content_hash=v_target->>'observation_content_hash'
      AND o.tenant_id=p_trusted_context->>'tenant_id'
      AND o.run_id=p_trusted_context->>'run_id'
      AND o.job_id=p_trusted_context->>'job_id'
      AND o.segment_id=p_trusted_context->>'segment_id'
      AND o.fencing_token=(p_trusted_context->>'fencing_token')::bigint
      AND o.status='ok' AND i.tool_name='query_billing_record'
      AND i.lifecycle='terminal' AND i.outcome='succeeded'
      AND g.id=v_target->>'turn_group_id' AND g.status='closed'
      AND o.payload::jsonb#>>'{data,billing_record_id}'
        =p_pair->>'billing_record_id'
      AND (o.payload::jsonb#>>'{data,version}')::integer
        =(p_pair->>'version')::integer
      AND o.payload::jsonb#>>'{data,duplicate_pair_eligible}'='true'
      AND o.payload::jsonb#>>'{data,refund_pair_hash}'
        =p_pair->>'refund_pair_hash'
      AND o.payload::jsonb#>>'{data,original_billing_record_id}'
        =p_pair->>'original_billing_record_id'
      AND (o.payload::jsonb#>>'{data,original_version}')::integer
        =(p_pair->>'original_version')::integer
  );
END
$function$;

CREATE FUNCTION public.supportguard_refund_proposal_binding_guard_v203()
RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path TO 'pg_catalog', 'public'
AS $function$
DECLARE
  v_customer_id text;
  v_pair jsonb;
BEGIN
  IF current_user<>'supportguard_owner' THEN
    RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='refund_proposal_guard_forbidden';
  END IF;
  IF TG_OP='UPDATE' THEN
    IF (NEW.refund_original_resource_id,NEW.refund_original_version,NEW.refund_pair_hash)
       IS DISTINCT FROM
       (OLD.refund_original_resource_id,OLD.refund_original_version,OLD.refund_pair_hash) THEN
      RAISE EXCEPTION USING
        ERRCODE='55000',MESSAGE='refund_proposal_pair_identity_immutable';
    END IF;
    RETURN NEW;
  END IF;
  IF NEW.action_type<>'refund' THEN RETURN NEW; END IF;
  SELECT t.customer_id INTO v_customer_id
  FROM public.agent_runs r
  JOIN public.support_tickets t
    ON t.tenant_id=r.tenant_id AND t.id=r.ticket_id
   AND t.customer_id=r.customer_id
  WHERE r.tenant_id=NEW.tenant_id AND r.id=NEW.run_id;
  v_pair:=public.supportguard_refund_pair_snapshot(
    NEW.tenant_id,v_customer_id,NEW.resource_id,clock_timestamp());
  IF v_customer_id IS NULL OR v_pair IS NULL
     OR v_pair->>'duplicate_pair_eligible'<>'true'
     OR (v_pair->>'version')::integer<>NEW.resource_version
     OR NEW.action_payload::jsonb->>'billing_record_id'<>NEW.resource_id
     OR NEW.action_payload::jsonb->>'customer_id'<>v_customer_id
     OR (NEW.action_payload::jsonb->>'business_version')::integer<>NEW.resource_version THEN
    RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='refund_proposal_pair_binding_invalid';
  END IF;
  IF NEW.refund_original_resource_id IS NULL
     AND NEW.refund_original_version IS NULL
     AND NEW.refund_pair_hash IS NULL THEN
    NEW.refund_original_resource_id:=v_pair->>'original_billing_record_id';
    NEW.refund_original_version:=(v_pair->>'original_version')::integer;
    NEW.refund_pair_hash:=v_pair->>'refund_pair_hash';
  ELSIF NEW.refund_original_resource_id IS DISTINCT FROM
          v_pair->>'original_billing_record_id'
     OR NEW.refund_original_version IS DISTINCT FROM
          (v_pair->>'original_version')::integer
     OR NEW.refund_pair_hash IS DISTINCT FROM v_pair->>'refund_pair_hash' THEN
    RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='refund_proposal_pair_binding_conflict';
  END IF;
  RETURN NEW;
END
$function$;

CREATE TRIGGER trg_refund_proposal_binding_guard_v203
BEFORE INSERT OR UPDATE ON public.proposal_records
FOR EACH ROW EXECUTE FUNCTION public.supportguard_refund_proposal_binding_guard_v203();

CREATE FUNCTION public.supportguard_action_mcp_execute_refund_v203(
  p_model_arguments jsonb,
  p_trusted_context jsonb
) RETURNS jsonb
LANGUAGE plpgsql
SET search_path TO 'pg_catalog', 'public', 'supportguard_control'
AS $function$
DECLARE
  v_weak jsonb;
  v_pair jsonb;
  v_weak_id text;
  v_proposal record;
  v_tenant_id text:=p_trusted_context->>'tenant_id';
  v_customer_id text;
BEGIN
  IF current_user<>'supportguard_owner' THEN
    RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='action_refund_v203_forbidden';
  END IF;
  v_weak:=public.supportguard_action_mcp_execute(
    'propose_refund',p_model_arguments,p_trusted_context);
  IF v_weak->>'authorized'<>'true' OR v_weak->>'phase'<>'execute' THEN
    RETURN v_weak;
  END IF;
  v_weak_id:=v_weak#>>'{result,proposal_id}';
  SELECT p.*,t.customer_id INTO v_proposal
  FROM public.proposal_records p
  JOIN public.agent_runs r ON r.tenant_id=p.tenant_id AND r.id=p.run_id
  JOIN public.support_tickets t ON t.tenant_id=r.tenant_id AND t.id=r.ticket_id
    AND t.customer_id=r.customer_id
  WHERE p.tenant_id=v_tenant_id AND p.id=v_weak_id
  FOR UPDATE OF p;
  v_customer_id:=v_proposal.customer_id;
  IF v_proposal.id IS NULL OR v_proposal.status<>'draft' THEN
    RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='refund_proposal_binding_invalid';
  END IF;
  v_pair:=public.supportguard_refund_pair_snapshot(
    v_tenant_id,v_customer_id,p_model_arguments->>'billing_record_id',clock_timestamp());
  IF v_pair IS NULL OR v_pair->>'duplicate_pair_eligible'<>'true'
     OR v_pair->>'currency'<>'USD'
     OR (v_pair->>'amount')::numeric>500.00
     OR NOT public.supportguard_action_refund_observation_bound_v203(
       p_trusted_context,
       p_trusted_context#>'{execution_payload,observation_binding}',v_pair
     ) THEN
    RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='refund_pair_policy_denied';
  END IF;
  IF v_proposal.refund_original_resource_id IS DISTINCT FROM
       v_pair->>'original_billing_record_id'
     OR v_proposal.refund_original_version IS DISTINCT FROM
       (v_pair->>'original_version')::integer
     OR v_proposal.refund_pair_hash IS DISTINCT FROM v_pair->>'refund_pair_hash' THEN
    RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='refund_proposal_pair_conflict';
  END IF;
  RETURN v_weak;
END
$function$;

CREATE OR REPLACE FUNCTION public.supportguard_action_mcp_propose_refund(
  p_model_arguments jsonb,
  p_trusted_context jsonb
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path TO 'pg_catalog', 'public'
AS $function$
DECLARE v_allowed boolean; v_phase text;
BEGIN
  IF session_user<>'supportguard_action_mcp' THEN
    RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='security_definer_session_user_forbidden';
  END IF;
  v_phase:=p_trusted_context->>'phase';
  IF jsonb_typeof(p_model_arguments)<>'object'
     OR jsonb_typeof(p_trusted_context)<>'object'
     OR p_trusted_context->>'schema_version'<>'action-mcp-wrapper.v1'
     OR p_trusted_context->>'capability_name'<>'propose_refund'
     OR v_phase NOT IN ('reserve','recheck','execute','record_result')
     OR (v_phase='record_result' AND
       (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(p_trusted_context) key)
       <> ARRAY['attempt_id','binding_hash','call_deadline','capability_name',
                'decision_hash','delivery_generation','effect_identity','fencing_token',
                'invocation_id','job_id','payload_hash','phase','run_id','schema_version',
                'segment_id','sequence','tenant_id'])
     OR (v_phase IN ('reserve','recheck') AND
       (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(p_trusted_context) key)
       <> ARRAY['attempt_id','binding_hash','call_deadline','capability_name',
                'decision_hash','delivery_generation','effect_identity','fencing_token',
                'invocation_id','job_id','phase','run_id','schema_version','segment_id',
                'sequence','tenant_id'])
     OR (v_phase='execute' AND
       (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(p_trusted_context) key)
       <> ARRAY['attempt_id','binding_hash','call_deadline','capability_name',
                'decision_hash','delivery_generation','effect_identity','execution_payload',
                'fencing_token','invocation_id','job_id','phase','run_id','schema_version',
                'segment_id','sequence','tenant_id'])
  THEN
    RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='invalid action MCP wrapper envelope';
  END IF;
  IF v_phase='reserve' THEN
    v_allowed:=public.supportguard_mcp_consume_capability_reservation(
      p_trusted_context->>'tenant_id',p_trusted_context->>'run_id',
      p_trusted_context->>'job_id',p_trusted_context->>'segment_id',
      (p_trusted_context->>'fencing_token')::bigint,
      (p_trusted_context->>'delivery_generation')::integer,
      p_trusted_context->>'invocation_id',p_trusted_context->>'attempt_id',
      (p_trusted_context->>'sequence')::integer,'propose_refund',
      p_trusted_context->>'effect_identity',p_trusted_context->>'decision_hash',
      p_trusted_context->>'binding_hash',
      (p_trusted_context->>'call_deadline')::timestamptz);
  ELSIF v_phase='record_result' THEN
    v_allowed:=public.supportguard_mcp_record_capability_result(
      p_trusted_context->>'tenant_id',p_trusted_context->>'run_id',
      p_trusted_context->>'job_id',p_trusted_context->>'invocation_id',
      p_trusted_context->>'attempt_id',p_trusted_context->>'effect_identity',
      p_trusted_context->>'payload_hash',p_model_arguments);
  ELSE
    v_allowed:=public.supportguard_mcp_verify_fence(
      p_trusted_context->>'tenant_id',p_trusted_context->>'run_id',
      p_trusted_context->>'job_id',p_trusted_context->>'segment_id',
      (p_trusted_context->>'fencing_token')::bigint,
      (p_trusted_context->>'delivery_generation')::integer);
  END IF;
  IF v_phase='execute' THEN
    IF v_allowed IS DISTINCT FROM true THEN
      RETURN pg_catalog.jsonb_build_object(
        'schema_version','mcp-wrapper-result.v1','authorized',false,
        'capability_name','propose_refund','phase','execute');
    END IF;
    RETURN public.supportguard_action_mcp_execute_refund_v203(
      p_model_arguments,p_trusted_context);
  END IF;
  RETURN pg_catalog.jsonb_build_object(
    'schema_version','mcp-wrapper-result.v1','authorized',v_allowed,
    'capability_name','propose_refund','phase',v_phase);
END
$function$;

REVOKE ALL ON FUNCTION public.supportguard_action_refund_observation_bound_v203(
  jsonb,jsonb,jsonb
) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.supportguard_refund_proposal_binding_guard_v203()
FROM PUBLIC;
REVOKE ALL ON FUNCTION public.supportguard_action_mcp_execute_refund_v203(
  jsonb,jsonb
) FROM PUBLIC;
"""


def upgrade() -> None:
    op.execute("SET LOCAL ROLE supportguard_owner")
    connection = op.get_bind()
    execute_interview_migration_sql(connection, _BILLING_FACTS_SQL)
    execute_interview_migration_sql(connection, _READ_REFUND_PAIR_SQL)
    execute_interview_migration_sql(connection, _PROPOSE_REFUND_PAIR_SQL)
    execute_interview_migration_sql(connection, _REFUND_GUARDS_SQL)
    execute_interview_migration_sql(connection, _WORKER_REFUND_STALE_SQL)
    execute_interview_migration_sql(connection, _CUSTOMER_REFUND_DISPLAY_SQL)
    execute_interview_migration_sql(connection, _TERMINAL_STATE_SQL)


def downgrade() -> None:
    raise RuntimeError("interview_demo_truthful_refund_downgrade_forbidden")
