from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import exc, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

pytestmark = pytest.mark.postgres


def _database_url() -> str:
    raw = os.getenv("TEST_DATABASE_URL") or os.getenv("TEST_FINALIZER_DATABASE_URL")
    if not raw:
        pytest.skip("TEST_DATABASE_URL or TEST_FINALIZER_DATABASE_URL is required")
    return raw


def _role_url(database_url: str, role: str) -> str:
    return (
        make_url(database_url)
        .set(username=role, password=role)
        .render_as_string(hide_password=False)
    )


async def _seed_runtime_job(
    connection: AsyncConnection,
    *,
    suffix: str,
) -> dict[str, str]:
    tenant_id = f"tenant_v1512_deadline_{suffix}"
    customer_id = f"customer_v1512_deadline_{suffix}"
    ticket_id = f"ticket_v1512_deadline_{suffix}"
    message_id = f"message_v1512_deadline_{suffix}"
    run_id = f"run_v1512_deadline_{suffix}"
    job_id = f"job_v1512_deadline_{suffix}"
    outbox_id = f"outbox_v1512_deadline_{suffix}"
    delivery_id = f"delivery_v1512_deadline_{suffix}"
    await connection.execute(
        text("SELECT set_config('app.tenant_id',CAST(:tenant_id AS text),true)"),
        {"tenant_id": tenant_id},
    )
    await connection.execute(
        text(
            """
            INSERT INTO public.tenants(id,name,status,created_at,updated_at)
            VALUES (
              :tenant_id,'v1.5.12 reconciler deadline tenant','active',
              clock_timestamp(),clock_timestamp()
            )
            """
        ),
        {"tenant_id": tenant_id},
    )
    await connection.execute(
        text(
            """
            INSERT INTO public.customers(
              id,tenant_id,display_name,email,status,security_status,region,
              version,created_at,updated_at
            ) VALUES (
              :customer_id,:tenant_id,'v1.5.12 reconciler deadline customer',
              :email,'active','normal','test',1,
              clock_timestamp(),clock_timestamp()
            )
            """
        ),
        {
            "customer_id": customer_id,
            "tenant_id": tenant_id,
            "email": f"{customer_id}@example.invalid",
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO public.support_tickets(
              id,tenant_id,customer_id,status,issue_type,risk,version,
              next_event_sequence,created_at,updated_at
            ) VALUES (
              :ticket_id,:tenant_id,:customer_id,'queued','unknown','low',1,0,
              clock_timestamp(),clock_timestamp()
            )
            """
        ),
        {
            "ticket_id": ticket_id,
            "tenant_id": tenant_id,
            "customer_id": customer_id,
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO public.ticket_messages(
              id,tenant_id,ticket_id,role,content,source_refs,created_at,updated_at
            ) VALUES (
              :message_id,:tenant_id,:ticket_id,'user',
              'v1.5.12 reconciler deadline authorization fixture','[]'::jsonb,
              clock_timestamp(),clock_timestamp()
            )
            """
        ),
        {
            "message_id": message_id,
            "tenant_id": tenant_id,
            "ticket_id": ticket_id,
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO public.agent_runs(
              id,tenant_id,ticket_id,customer_id,message_id,status,model,
              provider_mode,tool_call_mode,prompt_version,schema_version,
              context_version,checkpoint_stage,status_version,next_run_sequence,
              canonical_checkpoint_version,step_index,tool_rounds,tool_attempts,
              llm_calls,created_at,updated_at
            ) VALUES (
              :run_id,:tenant_id,:ticket_id,:customer_id,:message_id,'queued',
              'fake','fake','native_fixture','v1512','agent.v1','context.v1.5.12',
              'request_created',1,0,0,0,0,0,0,
              clock_timestamp(),clock_timestamp()
            )
            """
        ),
        {
            "run_id": run_id,
            "tenant_id": tenant_id,
            "ticket_id": ticket_id,
            "customer_id": customer_id,
            "message_id": message_id,
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO public.runtime_jobs(
              id,tenant_id,run_id,kind,status,attempt,available_at,fencing_token,
              timing_version,status_version,created_at,updated_at
            ) VALUES (
              :job_id,:tenant_id,:run_id,'agent_start','queued',0,
              clock_timestamp(),0,1,1,clock_timestamp(),clock_timestamp()
            )
            """
        ),
        {"job_id": job_id, "tenant_id": tenant_id, "run_id": run_id},
    )
    await connection.execute(
        text(
            """
            INSERT INTO public.outbox_events(
              id,delivery_id,tenant_id,job_id,run_id,delivery_generation,
              event_type,schema_version,payload,available_at,publish_attempts,
              delivery_state_version,created_at,updated_at
            ) VALUES (
              :outbox_id,:delivery_id,:tenant_id,:job_id,:run_id,1,
              'runtime_job_available','runtime-job.v1','{}'::jsonb,
              clock_timestamp(),0,1,clock_timestamp(),clock_timestamp()
            )
            """
        ),
        {
            "outbox_id": outbox_id,
            "delivery_id": delivery_id,
            "tenant_id": tenant_id,
            "job_id": job_id,
            "run_id": run_id,
        },
    )
    return {
        "tenant_id": tenant_id,
        "customer_id": customer_id,
        "ticket_id": ticket_id,
        "run_id": run_id,
        "job_id": job_id,
        "outbox_id": outbox_id,
        "delivery_id": delivery_id,
    }


def _unknown_observation(
    *,
    fixture: dict[str, str],
    prepared: dict[str, object],
    runner_nonce: str,
) -> str:
    return json.dumps(
        {
            "schema_version": "redis-delivery-observation.v1",
            "intent_id": prepared["intent_id"],
            "observation_nonce": prepared["observation_nonce"],
            "job_id": fixture["job_id"],
            "outbox_id": fixture["outbox_id"],
            "delivery_generation": 1,
            "runner_nonce": runner_nonce,
            "observed_at": datetime.now(UTC).isoformat(),
            "status": "unknown",
            "error_code": "redis_timeout",
        },
        sort_keys=True,
        separators=(",", ":"),
    )


@pytest.mark.asyncio
async def test_reconciler_deadline_repair_is_authorized_once_and_receipt_is_transaction_local() -> (
    None
):
    database_url = _database_url()
    suffix = uuid4().hex[:10]
    admin = create_async_engine(database_url)
    reconciler = create_async_engine(_role_url(database_url, "supportguard_reconciler"))
    try:
        async with admin.begin() as connection:
            direct_fixture = await _seed_runtime_job(
                connection,
                suffix=f"{suffix}_direct",
            )
            held_fixture = await _seed_runtime_job(
                connection,
                suffix=f"{suffix}_held",
            )
            stale_fixture = await _seed_runtime_job(
                connection,
                suffix=f"{suffix}_stale",
            )
            deadline_fixture = await _seed_runtime_job(
                connection,
                suffix=f"{suffix}_deadline",
            )

        # The runtime role still cannot mint a dead transition by writing the
        # table directly.
        with pytest.raises(exc.DBAPIError) as direct_denied:
            async with reconciler.begin() as connection:
                await connection.execute(
                    text("SELECT set_config('app.tenant_id',CAST(:tenant_id AS text),true)"),
                    direct_fixture,
                )
                await connection.execute(
                    text(
                        "UPDATE public.runtime_jobs "
                        "SET status='dead',status_version=status_version+1 "
                        "WHERE id=:job_id"
                    ),
                    direct_fixture,
                )
        assert getattr(direct_denied.value.orig, "sqlstate", None) == "42501"

        # A valid observation before its deadline is held, not authorized to
        # cross the dead transition.
        async with reconciler.begin() as connection:
            held_prepared = await connection.scalar(
                text("SELECT supportguard_reconciler_prepare(:job_id,1,'delivery_recovery')"),
                held_fixture,
            )
        assert isinstance(held_prepared, dict)
        assert held_prepared["result"] == "prepared"
        async with reconciler.begin() as connection:
            held = await connection.scalar(
                text(
                    "SELECT supportguard_reconciler_repair("
                    ":job_id,1,:intent_id,CAST(:observation AS jsonb))"
                ),
                {
                    **held_fixture,
                    "intent_id": held_prepared["intent_id"],
                    "observation": _unknown_observation(
                        fixture=held_fixture,
                        prepared=held_prepared,
                        runner_nonce="1" * 32,
                    ),
                },
            )
            held_receipt = await connection.scalar(
                text("SELECT current_setting('app.dead_convergence_authorized',true)")
            )
        assert held == "held_unknown"
        assert held_receipt != "v1512"

        # A stale expected version returns through the existing validation path
        # and likewise cannot mint the receipt.
        async with reconciler.begin() as connection:
            stale_prepared = await connection.scalar(
                text("SELECT supportguard_reconciler_prepare(:job_id,1,'delivery_recovery')"),
                stale_fixture,
            )
        assert isinstance(stale_prepared, dict)
        assert stale_prepared["result"] == "prepared"
        async with reconciler.begin() as connection:
            stale = await connection.scalar(
                text(
                    "SELECT supportguard_reconciler_repair("
                    ":job_id,2,:intent_id,CAST(:observation AS jsonb))"
                ),
                {
                    **stale_fixture,
                    "intent_id": stale_prepared["intent_id"],
                    "observation": _unknown_observation(
                        fixture=stale_fixture,
                        prepared=stale_prepared,
                        runner_nonce="2" * 32,
                    ),
                },
            )
            stale_receipt = await connection.scalar(
                text("SELECT current_setting('app.dead_convergence_authorized',true)")
            )
        assert stale == "state_drift"
        assert stale_receipt != "v1512"

        # Once the validated intent deadline expires, the same narrow capability
        # may set the receipt immediately before its protected dead transition.
        async with reconciler.begin() as connection:
            deadline_prepared = await connection.scalar(
                text("SELECT supportguard_reconciler_prepare(:job_id,1,'delivery_recovery')"),
                deadline_fixture,
            )
        assert isinstance(deadline_prepared, dict)
        assert deadline_prepared["result"] == "prepared"
        async with admin.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE public.reconcile_intents "
                    "SET expires_at=:expired_at,updated_at=clock_timestamp() "
                    "WHERE id=:intent_id"
                ),
                {
                    "expired_at": datetime.now(UTC) - timedelta(seconds=1),
                    "intent_id": deadline_prepared["intent_id"],
                },
            )
        async with reconciler.begin() as connection:
            repaired = await connection.scalar(
                text(
                    "SELECT supportguard_reconciler_repair("
                    ":job_id,1,:intent_id,CAST(:observation AS jsonb))"
                ),
                {
                    **deadline_fixture,
                    "intent_id": deadline_prepared["intent_id"],
                    "observation": _unknown_observation(
                        fixture=deadline_fixture,
                        prepared=deadline_prepared,
                        runner_nonce="3" * 32,
                    ),
                },
            )
            live_receipt = await connection.scalar(
                text("SELECT current_setting('app.dead_convergence_authorized',true)")
            )
        assert repaired == "dead"
        assert live_receipt == "v1512"

        async with admin.connect() as connection:
            terminal = (
                await connection.execute(
                    text(
                        "SELECT j.status,j.outcome,j.last_error,"
                        "r.status,t.status,i.status,i.terminal_reason "
                        "FROM public.runtime_jobs j "
                        "JOIN public.agent_runs r "
                        "  ON r.tenant_id=j.tenant_id AND r.id=j.run_id "
                        "JOIN public.support_tickets t "
                        "  ON t.tenant_id=j.tenant_id AND t.id=j.ticket_id "
                        "JOIN public.reconcile_intents i "
                        "  ON i.tenant_id=j.tenant_id AND i.id=:intent_id "
                        "WHERE j.tenant_id=:tenant_id AND j.id=:job_id"
                    ),
                    {
                        **deadline_fixture,
                        "intent_id": deadline_prepared["intent_id"],
                    },
                )
            ).one()
        assert tuple(terminal) == (
            "dead",
            "infrastructure_exhausted",
            "delivery_state_unknown_deadline",
            "failed",
            "failed",
            "consumed",
            "delivery_state_unknown_deadline",
        )

        # SET LOCAL must disappear at transaction end. Reusing the same engine
        # cannot authorize a later direct transition.
        async with reconciler.begin() as connection:
            leaked_receipt = await connection.scalar(
                text("SELECT current_setting('app.dead_convergence_authorized',true)")
            )
            core_execute = await connection.scalar(
                text(
                    "SELECT has_function_privilege("
                    "'supportguard_reconciler',"
                    "'public.supportguard_reconciler_consume_v126_core(text,text,text)',"
                    "'EXECUTE')"
                )
            )
        assert leaked_receipt != "v1512"
        assert core_execute is False
        with pytest.raises(exc.DBAPIError) as direct_denied_after:
            async with reconciler.begin() as connection:
                await connection.execute(
                    text("SELECT set_config('app.tenant_id',CAST(:tenant_id AS text),true)"),
                    direct_fixture,
                )
                await connection.execute(
                    text(
                        "UPDATE public.runtime_jobs "
                        "SET status='dead',status_version=status_version+1 "
                        "WHERE id=:job_id"
                    ),
                    direct_fixture,
                )
        assert getattr(direct_denied_after.value.orig, "sqlstate", None) == "42501"
    finally:
        await reconciler.dispose()
        await admin.dispose()
