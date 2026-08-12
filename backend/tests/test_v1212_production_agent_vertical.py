from __future__ import annotations

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from supportguard.config import Settings, get_settings
from supportguard.contracts.event_channels import ticket_event_channel
from supportguard.db.models import (
    AgentCallAttempt,
    AgentEvent,
    AgentRun,
    CheckpointCommitMarker,
    CitationBinding,
    ContextMembership,
    InboxDelivery,
    OutboxEvent,
    RetrievalTrace,
    RuntimeJob,
    SupportTicket,
    ToolInvocation,
    ToolObservation,
)
from supportguard.main import create_app
from supportguard.runtime.worker import worker_runtime
from supportguard.services.demo_temporal import refresh_demo_temporal_fixtures
from supportguard.services.runtime_queue import OutboxDispatcher

pytestmark = [pytest.mark.postgres, pytest.mark.redis, pytest.mark.mcp]


def _database_url(base: str, username: str) -> str:
    return (
        make_url(base)
        .set(username=username, password=username)
        .render_as_string(hide_password=False)
    )


def _redis_url(base: str, username: str, password: str) -> str:
    parsed = make_url(base)
    return parsed.set(username=username, password=password).render_as_string(hide_password=False)


@pytest.mark.asyncio
async def test_public_http_to_restricted_mcp_agent_finalizer_vertical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    redis_url = os.getenv("TEST_REDIS_URL")
    if not database_url or not redis_url:
        pytest.skip("TEST_DATABASE_URL and TEST_REDIS_URL are required")

    suffix = uuid4().hex[:12]
    stream = f"supportguard:test:v1212:vertical:{suffix}"
    group = f"supportguard-v1212-{suffix}"
    api_database_url = _database_url(database_url, "supportguard_api")
    dispatcher_database_url = _database_url(database_url, "supportguard_dispatcher")
    worker_database_url = _database_url(database_url, "supportguard_worker")
    read_database_url = _database_url(database_url, "supportguard_read_mcp")
    action_database_url = _database_url(database_url, "supportguard_action_mcp")
    api_redis_url = _redis_url(redis_url, "api", "api_dev")
    dispatcher_redis_url = _redis_url(redis_url, "dispatcher", "dispatcher_dev")
    worker_redis_url = _redis_url(redis_url, "worker", "worker_dev")

    # Child MCP uses the deterministic embedding fixture, while its restricted
    # database login still forces the production reservation/fence path.
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("AUTH_MODE", "development")
    monkeypatch.setenv("ASYNC_RUNTIME_ENABLED", "true")
    monkeypatch.setenv("DEMO_FAKE_PROVIDER", "true")
    monkeypatch.setenv("DATABASE_URL", api_database_url)
    monkeypatch.setenv("REDIS_URL", api_redis_url)
    monkeypatch.setenv("REDIS_STREAM", stream)
    monkeypatch.setenv("MCP_READ_DATABASE_URL", read_database_url)
    monkeypatch.setenv("MCP_ACTION_DATABASE_URL", action_database_url)
    monkeypatch.setenv("APP_SECRET_KEY", f"v1212-test-secret-{suffix}")
    monkeypatch.setenv("INTERNAL_API_TOKEN", f"v1212-internal-{suffix}")
    get_settings.cache_clear()

    admin_engine = create_async_engine(database_url)
    admin_factory = async_sessionmaker(admin_engine, expire_on_commit=False)
    dispatcher_engine = create_async_engine(dispatcher_database_url)
    dispatcher_factory = async_sessionmaker(dispatcher_engine, expire_on_commit=False)
    dispatcher_redis = Redis.from_url(dispatcher_redis_url, decode_responses=False)
    cleanup_redis = Redis.from_url(
        _redis_url(redis_url, "integration", "integration_dev"),
        decode_responses=False,
    )
    wakeup_redis = Redis.from_url(api_redis_url, decode_responses=False)
    wakeup_pubsub = wakeup_redis.pubsub()
    worker_settings = Settings(
        _env_file=None,
        app_env="development",
        auth_mode="development",
        async_runtime_enabled=True,
        demo_fake_provider=True,
        database_url=worker_database_url,
        redis_url=worker_redis_url,
        redis_stream=stream,
        redis_consumer_group=group,
        service_instance_id=f"v1212-worker-{suffix}",
        mcp_read_database_url=read_database_url,
        mcp_action_database_url=action_database_url,
        code_version="v1.2.12-production-vertical",
    )
    ticket_id = run_id = job_id = follow_up_run_id = comparison_run_id = ""
    historical_run_id = ""
    worker_context = None
    try:
        async with admin_factory() as session, session.begin():
            await refresh_demo_temporal_fixtures(
                session,
                settings=worker_settings,
                tenant_id="tenant_demo",
            )
        with TestClient(create_app()) as client:
            login = client.post(
                "/api/demo-sessions",
                json={"role": "customer", "customer_id": "cust_demo"},
            )
            assert login.status_code == 200
            csrf = str(login.json()["csrf_token"])
            accepted = client.post(
                "/api/tickets",
                headers={
                    "X-CSRF-Token": csrf,
                    "Idempotency-Key": f"v1212-vertical-{suffix}",
                },
                json={
                    "message": (
                        "余额充足，但 atlas-chat 返回 "
                        "429 concurrency_limit_exceeded，请结合账户、用量和产品文档定位原因。"
                    )
                },
            )
            assert accepted.status_code == 202, accepted.text
            command = accepted.json()
            ticket_id = str(command["ticket_id"])
            run_id = str(command["run_id"])
            job_id = str(command["job_id"])
            wakeup_channel = ticket_event_channel("tenant_demo", ticket_id)
            await wakeup_pubsub.subscribe(wakeup_channel)
            await wakeup_pubsub.get_message(timeout=1.0)

            dispatcher = OutboxDispatcher(
                dispatcher_factory,
                dispatcher_redis,
                stream=stream,
            )
            assert await dispatcher.dispatch_once(batch_size=50) >= 1
            worker_context = worker_runtime(worker_settings)
            worker = await worker_context.__aenter__()
            processed = 0
            for _ in range(20):
                processed += await worker.consume_once(block_ms=100)
                async with admin_factory() as session:
                    job = await session.get(RuntimeJob, job_id)
                    if job is not None and job.status in {
                        "succeeded",
                        "failed",
                        "dead_letter",
                    }:
                        break
            assert processed >= 1

            wakeup = await wakeup_pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=2.0,
            )
            assert wakeup is not None
            assert wakeup["channel"] == wakeup_channel.encode()
            assert wakeup["data"] == run_id.encode()

            detail = client.get(f"/api/tickets/{ticket_id}")
            events_response = client.get(f"/api/tickets/{ticket_id}/events")
            assert detail.status_code == 200
            assert events_response.status_code == 200
            product_detail = detail.json()
            assert product_detail["latest_run"]["id"] == run_id
            assert product_detail["latest_run"]["actual_runtime"]["provider_mode"] == (
                "fake"
            )
            assert product_detail["latest_run"]["actual_runtime"]["source"] == (
                "agent_call_attempt"
            )
            assert product_detail["knowledge_sources"]
            assert all(
                item["citation_binding_id"].startswith("citation_")
                and item["binding_purpose"] == "answer_claim"
                for item in product_detail["knowledge_sources"]
            )
            assert {item["run_id"] for item in product_detail["timeline"]} == {run_id}
            detail_event_ids = [item["id"] for item in product_detail["timeline"]]
            assert detail_event_ids
            assert len(detail_event_ids) == len(set(detail_event_ids))
            aggregation = product_detail["aggregation"]
            assert aggregation["messages"]["returned"] == len(product_detail["messages"])
            assert aggregation["timeline"]["returned"] == len(product_detail["timeline"])
            assert aggregation["knowledge_sources"]["returned"] == len(
                product_detail["knowledge_sources"]
            )
            assert aggregation["business_facts"]["returned"] == len(
                product_detail["business_facts"]
            )
            assert all(
                window["total_is_exact"] is True and window["has_more"] is False
                for window in aggregation.values()
            )

            follow_up = client.post(
                f"/api/tickets/{ticket_id}/messages",
                headers={
                    "X-CSRF-Token": csrf,
                    "Idempotency-Key": f"v1212-vertical-follow-up-{suffix}",
                },
                json={
                    "message": (
                        "请再次检查：余额充足，但 atlas-chat 返回 "
                        "429 concurrency_limit_exceeded，请结合账户、用量和产品文档定位原因。"
                    )
                },
            )
            assert follow_up.status_code == 202, follow_up.text
            follow_up_run_id = str(follow_up.json()["run_id"])
            assert follow_up_run_id != run_id
            assert await dispatcher.dispatch_once(batch_size=50) >= 1
            for _ in range(20):
                await worker.consume_once(block_ms=100)
                async with admin_factory() as session:
                    follow_up_run = await session.get(AgentRun, follow_up_run_id)
                    if follow_up_run is not None and follow_up_run.status in {
                        "completed",
                        "failed",
                    }:
                        break
            follow_up_detail = client.get(f"/api/tickets/{ticket_id}")
            assert follow_up_detail.status_code == 200
            assert follow_up_detail.json()["latest_run"]["id"] == follow_up_run_id

            comparison = client.post(
                f"/api/tickets/{ticket_id}/messages",
                headers={
                    "X-CSRF-Token": csrf,
                    "Idempotency-Key": f"v1212-vertical-comparison-{suffix}",
                },
                json={
                    "message": (
                        "请对比 atlas-chat 当前版本和旧版本的上下文上限及 JSON 输出限制。"
                    ),
                },
            )
            assert comparison.status_code == 202, comparison.text
            comparison_run_id = str(comparison.json()["run_id"])
            assert comparison_run_id not in {run_id, follow_up_run_id}
            assert await dispatcher.dispatch_once(batch_size=50) >= 1
            for _ in range(20):
                await worker.consume_once(block_ms=100)
                async with admin_factory() as session:
                    comparison_run = await session.get(AgentRun, comparison_run_id)
                    if comparison_run is not None and comparison_run.status in {
                        "completed",
                        "failed",
                    }:
                        break

            historical = client.post(
                f"/api/tickets/{ticket_id}/messages",
                headers={
                    "X-CSRF-Token": csrf,
                    "Idempotency-Key": f"v1212-vertical-historical-{suffix}",
                },
                json={"message": "旧版本呢？"},
            )
            assert historical.status_code == 202, historical.text
            historical_run_id = str(historical.json()["run_id"])
            assert historical_run_id not in {
                run_id,
                follow_up_run_id,
                comparison_run_id,
            }
            assert await dispatcher.dispatch_once(batch_size=50) >= 1
            for _ in range(20):
                await worker.consume_once(block_ms=100)
                async with admin_factory() as session:
                    historical_run = await session.get(AgentRun, historical_run_id)
                    if historical_run is not None and historical_run.status in {
                        "completed",
                        "failed",
                    }:
                        break
            historical_detail = client.get(f"/api/tickets/{ticket_id}")
            assert historical_detail.status_code == 200
            assert historical_detail.json()["latest_run"]["id"] == historical_run_id

        async with admin_factory() as session:
            ticket = await session.get(SupportTicket, ticket_id)
            run = await session.get(AgentRun, run_id)
            job = await session.get(RuntimeJob, job_id)
            assert ticket is not None
            assert run is not None and run.status == "completed"
            assert run.agent_finish_reason == "evidence_freshness_insufficient"
            assert job is not None and job.status == "succeeded"
            assert run.tool_rounds == 1
            assert run.tool_attempts == 3
            assert run.llm_calls == 4

            llm_attempts = list(
                (
                    await session.scalars(
                        select(AgentCallAttempt)
                        .where(
                            AgentCallAttempt.run_id == run_id,
                            AgentCallAttempt.call_kind == "llm",
                        )
                        .order_by(AgentCallAttempt.ordinal)
                    )
                ).all()
            )
            assert [item.ordinal for item in llm_attempts] == [1, 2, 3, 4]
            assert all(item.status == "succeeded" for item in llm_attempts)

            invocations = list(
                (
                    await session.scalars(
                        select(ToolInvocation)
                        .where(ToolInvocation.run_id == run_id)
                        .order_by(ToolInvocation.created_at, ToolInvocation.ordinal)
                    )
                ).all()
            )
            observations = list(
                (
                    await session.scalars(
                        select(ToolObservation).where(ToolObservation.run_id == run_id)
                    )
                ).all()
            )
            assert {item.tool_name for item in invocations} == {
                "search_knowledge",
                "query_account",
                "query_api_usage",
            }
            assert len(invocations) == len(observations) == 3
            assert {item.invocation_id for item in observations} == {
                item.id for item in invocations
            }
            assert all(item.lifecycle == "terminal" for item in invocations)
            assert all(item.outcome == "succeeded" for item in invocations)
            assert all(item.status == "ok" for item in observations)
            assert all(item.payload.get("error_code") is None for item in observations)
            assert all(item.payload.get("source_refs") for item in observations)
            search_observation = next(
                item for item in observations if item.payload.get("tool_name") == "search_knowledge"
            )
            evidence_instances = search_observation.payload.get("data", {}).get("evidence", [])
            supporting_evidence = [
                item for item in evidence_instances if item.get("supporting_span_eligible") is True
            ]
            assert supporting_evidence
            assert all(item.get("supporting_span") for item in supporting_evidence)
            assert all(
                item.get("source_locator", {}).get("locator_hash") for item in supporting_evidence
            )

            retrievals = list(
                (
                    await session.scalars(
                        select(RetrievalTrace).where(RetrievalTrace.run_id == run_id)
                    )
                ).all()
            )
            assert len(retrievals) == 1
            assert retrievals[0].trace_status == "terminal_ok"
            assert retrievals[0].trace_schema_version == "retrieval-trace.v3"
            assert retrievals[0].selected_candidates
            assert retrievals[0].result_digest
            assert await session.scalar(
                select(func.count(ContextMembership.id)).where(ContextMembership.run_id == run_id)
            )
            assert await session.scalar(
                select(func.count(CitationBinding.id)).where(CitationBinding.run_id == run_id)
            )
            reservation_count = await session.scalar(
                text(
                    "SELECT count(*) FROM tool_transport_attempts "
                    "WHERE run_id=:run_id AND status='succeeded'"
                ),
                {"run_id": run_id},
            )
            assert int(reservation_count or 0) == 3

            events = list(
                (
                    await session.scalars(
                        select(AgentEvent)
                        .where(AgentEvent.run_id == run_id)
                        .order_by(AgentEvent.created_at, AgentEvent.id)
                    )
                ).all()
            )
            event_types = {item.event_type for item in events}
            assert {
                "agent_decision",
                "tool_observation",
                "policy_decision",
                "final_outcome",
            } <= event_types
            assert sum(item.event_type == "agent_decision" for item in events) == 3
            evidence_replans = [
                item for item in events if item.event_type == "evidence_group_incomplete"
            ]
            assert len(evidence_replans) == 1
            assert evidence_replans[0].payload["result"] == "replan"
            assert (
                next(item for item in events if item.event_type == "policy_decision").payload[
                    "route"
                ]
                == "answer"
            )
            assert (
                next(item for item in events if item.event_type == "final_outcome").payload[
                    "terminal_state"
                ]
                == "resolved"
            )

            follow_up_run = await session.get(AgentRun, follow_up_run_id)
            assert follow_up_run is not None
            assert follow_up_run.status == "completed"
            assert follow_up_run.agent_finish_reason == "evidence_freshness_insufficient"
            follow_up_invocations = list(
                (
                    await session.scalars(
                        select(ToolInvocation).where(
                            ToolInvocation.run_id == follow_up_run_id,
                            ToolInvocation.tool_name == "search_knowledge",
                        )
                    )
                ).all()
            )
            follow_up_observations = list(
                (
                    await session.scalars(
                        select(ToolObservation).where(
                            ToolObservation.run_id == follow_up_run_id,
                        )
                    )
                ).all()
            )
            assert len(follow_up_invocations) == 1
            assert len(follow_up_observations) == 3
            assert follow_up_invocations[0].outcome == "succeeded"
            assert all(item.status == "ok" for item in follow_up_observations)

            comparison_run = await session.get(AgentRun, comparison_run_id)
            assert comparison_run is not None
            assert comparison_run.status == "completed"
            assert comparison_run.agent_finish_reason in {
                "answered",
                "comparison_citation_incomplete",
                "comparison_transition_incomplete",
            }

            historical_run = await session.get(AgentRun, historical_run_id)
            assert historical_run is not None
            assert historical_run.status == "completed"
            assert historical_run.agent_finish_reason in {
                "answered",
                "comparison_citation_incomplete",
                "comparison_transition_incomplete",
            }
            assert historical_run.agent_finish_reason != "no_progress"
            historical_invocations = list(
                (
                    await session.scalars(
                        select(ToolInvocation).where(
                            ToolInvocation.run_id == historical_run_id,
                            ToolInvocation.tool_name == "search_knowledge",
                        )
                    )
                ).all()
            )
            historical_observations = list(
                (
                    await session.scalars(
                        select(ToolObservation).where(
                            ToolObservation.run_id == historical_run_id,
                        )
                    )
                ).all()
            )
            assert len(historical_invocations) == len(historical_observations) == 1
            assert historical_invocations[0].outcome == "succeeded"
            assert historical_observations[0].status == "ok"
            assert historical_observations[0].payload["trusted_retrieval_intent"][
                "intent"
            ] == "compare"
            assert historical_observations[0].payload["trusted_retrieval_intent"][
                "reason_code"
            ] == "contextual_historical_comparison_semantics"
            markers = list(
                (
                    await session.scalars(
                        select(CheckpointCommitMarker).where(
                            CheckpointCommitMarker.run_id == run_id
                        )
                    )
                ).all()
            )
            assert len(markers) == 1 and markers[0].status == "finalized"
            outbox = await session.scalar(select(OutboxEvent).where(OutboxEvent.job_id == job_id))
            inbox = await session.scalar(
                select(InboxDelivery).where(InboxDelivery.job_id == job_id)
            )
            assert outbox is not None and outbox.published_at is not None
            assert inbox is not None and inbox.status == "acked"

        api_events = events_response.json()
        event_ids = [item["id"] for item in api_events]
        assert event_ids
        assert len(event_ids) == len(set(event_ids))
        assert detail_event_ids == event_ids
        assert {item["event_type"] for item in api_events} >= {
            "agent_decision",
            "tool_observation",
            "policy_decision",
            "final_outcome",
        }
        serialized_events = events_response.text.lower()
        # Prompt/schema versions are safe provenance. Prompt bodies and
        # private reasoning remain forbidden on the product API.
        assert '"prompt":' not in serialized_events
        assert '"system_prompt":' not in serialized_events
        assert '"developer_prompt":' not in serialized_events
        assert '"chain_of_thought":' not in serialized_events
        assert '"reasoning_content":' not in serialized_events
    finally:
        if worker_context is not None:
            await worker_context.__aexit__(None, None, None)
        await cleanup_redis.delete(stream)
        await wakeup_pubsub.aclose()
        await wakeup_redis.aclose()
        await dispatcher_redis.aclose()
        await cleanup_redis.aclose()
        await dispatcher_engine.dispose()
        await admin_engine.dispose()
        get_settings.cache_clear()
