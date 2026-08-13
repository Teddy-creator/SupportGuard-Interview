import argparse
import asyncio
import importlib
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import cast

import uvicorn
from redis.asyncio import Redis
from sqlalchemy import text

from supportguard.config import get_settings
from supportguard.db.permissions import (
    bootstrap_interview_database_roles,
    configure_local_mcp_roles,
)
from supportguard.db.seed import seed_demo_data
from supportguard.db.session import create_engine, create_session_factory
from supportguard.observability.logging import configure_json_logging
from supportguard.observability.tracing import configure_tracing
from supportguard.rag.embeddings import build_embedding_provider
from supportguard.rag.ingest import ingest_corpus
from supportguard.runtime.delivery import (
    CONTROL_LOOP_TIMEOUT_SECONDS,
    OutboxDispatcher,
    ServiceLoopProgress,
    bounded_service_loop,
)
from supportguard.runtime.worker import worker_runtime
from supportguard.services.demo_temporal import (
    demo_resource_preflight,
    demo_temporal_preflight,
    refresh_demo_temporal_fixtures,
)
from supportguard.services.heartbeats import ServiceHeartbeatSnapshot, service_heartbeat
from supportguard.services.retention import RetentionService
from supportguard.services.runtime_maintenance import RuntimeReconciler, trim_terminal_deliveries
from supportguard.services.runtime_timing import RuntimeTiming
from supportguard.services.schema_rollout import WriterService, require_current_writer_contract


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="supportguard")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("serve", help="Run the FastAPI development server")
    db_parser = subcommands.add_parser("db", help="Manage the database")
    db_commands = db_parser.add_subparsers(dest="db_command", required=True)
    db_commands.add_parser(
        "baseline-upgrade",
        help="Create the Interview baseline on an accepted empty database",
    )
    db_commands.add_parser("seed", help="Insert idempotent demo fixtures")
    db_commands.add_parser(
        "bootstrap-interview-roles",
        help="Create service roles only for an accepted Interview baseline database",
    )
    db_commands.add_parser(
        "configure-mcp-roles", help="Configure Docker-demo least-privilege MCP roles"
    )
    knowledge_parser = subcommands.add_parser("knowledge", help="Manage the RAG corpus")
    knowledge_commands = knowledge_parser.add_subparsers(dest="knowledge_command", required=True)
    for command_name, help_text in (
        ("ingest", "Build and activate an index"),
        ("rebuild", "Idempotently rebuild and activate the corpus index"),
    ):
        index_parser = knowledge_commands.add_parser(command_name, help=help_text)
        index_parser.add_argument(
            "--fixture", action="store_true", help="Use deterministic offline embeddings"
        )
    runtime_parser = subcommands.add_parser("runtime", help="Run async runtime services")
    runtime_commands = runtime_parser.add_subparsers(dest="runtime_command", required=True)
    runtime_commands.add_parser("dispatcher", help="Publish PostgreSQL Outbox to Redis Streams")
    runtime_commands.add_parser("reconciler", help="Recover queued, retry, and expired jobs")
    runtime_commands.add_parser("worker", help="Consume and execute fenced Agent jobs")
    runtime_health = runtime_commands.add_parser("health", help="Check a runtime heartbeat")
    runtime_health.add_argument("--instance", required=True)
    runtime_health.add_argument("--service", required=True)
    maintenance = subcommands.add_parser("maintenance", help="Run audited maintenance jobs")
    maintenance_commands = maintenance.add_subparsers(dest="maintenance_command", required=True)
    retention = maintenance_commands.add_parser("retention", help="Apply retention policy")
    retention_mode = retention.add_mutually_exclusive_group(required=True)
    retention_mode.add_argument("--dry-run", action="store_true")
    retention_mode.add_argument("--apply", action="store_true")
    redis_trim = maintenance_commands.add_parser(
        "redis-trim", help="Audit and optionally delete eligible terminal stream deliveries"
    )
    redis_trim_mode = redis_trim.add_mutually_exclusive_group(required=True)
    redis_trim_mode.add_argument("--dry-run", action="store_true")
    redis_trim_mode.add_argument("--apply", action="store_true")
    demo = subcommands.add_parser("demo", help="Operate protected local demo fixtures")
    demo_commands = demo.add_subparsers(dest="demo_command", required=True)
    for name, help_text in (
        ("temporal-refresh", "Refresh only demo usage timestamps and buckets"),
        ("temporal-maintain", "Continuously maintain local demo usage freshness"),
        ("temporal-preflight", "Report demo temporal freshness without mutation"),
        ("resource-preflight", "Verify the clean interview-demo business resources"),
    ):
        command = demo_commands.add_parser(name, help=help_text)
        command.add_argument("--tenant", required=True)
    return parser


def upgrade_interview_baseline_database() -> None:
    """Thin Runtime CLI boundary around the independent baseline owner."""

    owner = importlib.import_module("supportguard.db.interview_baseline")
    upgrade_interview_baseline = getattr(owner, "upgrade_interview_baseline", None)
    if not callable(upgrade_interview_baseline):
        raise RuntimeError("interview_baseline_owner_unavailable")
    upgrade_interview_baseline()


async def seed_database() -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    async with factory() as session:
        await seed_demo_data(session)
        await session.commit()
    await engine.dispose()


async def ingest_knowledge(*, fixture: bool) -> None:
    engine = create_engine()
    factory = create_session_factory(engine)
    settings = get_settings()
    # Keep index construction and query-time retrieval on one configuration
    # path.  ``--fixture`` is an explicit test override; otherwise
    # EMBEDDING_MODE remains the authoritative runtime setting.
    embedding = build_embedding_provider(settings, testing=fixture)
    async with factory() as session:
        result = await ingest_corpus(
            session,
            root=Path.cwd(),
            manifest_path=Path("knowledge/manifests/documents.json"),
            embedding=embedding,
        )
        await session.commit()
    await engine.dispose()
    print(
        f"index={result.index_version} documents={result.document_count} "
        f"chunks={result.chunk_count} reused={result.reused}"
    )


async def run_runtime_service(kind: str) -> None:
    settings = get_settings()
    if kind == "worker":
        async with (
            worker_runtime(settings) as worker,
            service_heartbeat(
                worker.factory,
                instance_id=settings.service_instance_id,
                service="worker",
                capabilities=["agent", "read-mcp", "action-mcp", "native-tools"],
                interval_seconds=worker.timing.heartbeat_interval.total_seconds()
                if worker.timing is not None
                else settings.runtime_heartbeat_interval_seconds,
                snapshot_provider=worker.heartbeat_snapshot_provider,
            ),
        ):
            await bounded_service_loop(worker.consume_once, interval_seconds=0)
        return
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        await require_current_writer_contract(
            factory,
            service="dispatcher" if kind == "dispatcher" else "reconciler",
        )
    except BaseException:
        await engine.dispose()
        raise
    redis = Redis.from_url(
        settings.redis_url,
        decode_responses=False,
        socket_connect_timeout=5,
        socket_timeout=5,
        health_check_interval=10,
    )
    try:
        timing = await RuntimeTiming.from_database(factory, settings)
        loop_progress = ServiceLoopProgress(service=kind)

        def control_loop_snapshot() -> ServiceHeartbeatSnapshot:
            progress_age = loop_progress.progress_age_seconds()
            ready = progress_age <= CONTROL_LOOP_TIMEOUT_SECONDS
            return ServiceHeartbeatSnapshot(
                status="ready" if ready else "degraded",
                capabilities=(
                    "outbox" if kind == "dispatcher" else "reconcile",
                    f"control_loop:{'recent' if ready else 'stale'}",
                    f"control_loop_progress_age_ms:{int(progress_age * 1000)}",
                    f"control_loop_iterations:{loop_progress.completed_iterations}",
                ),
            )

        async with service_heartbeat(
            factory,
            instance_id=f"{kind}:{settings.service_instance_id}",
            service=cast(WriterService, kind),
            capabilities=["outbox"] if kind == "dispatcher" else ["reconcile"],
            interval_seconds=timing.heartbeat_interval.total_seconds(),
            snapshot_provider=control_loop_snapshot,
        ):
            if kind == "dispatcher":
                dispatcher = OutboxDispatcher(
                    factory,
                    redis,
                    stream=settings.redis_stream,
                )
                await bounded_service_loop(
                    dispatcher.dispatch_once,
                    interval_seconds=0.25,
                    operation_timeout_seconds=CONTROL_LOOP_TIMEOUT_SECONDS,
                    progress=loop_progress,
                )
            else:
                reconciler = RuntimeReconciler(
                    factory, redis, stream=settings.redis_stream, timing=timing
                )
                await bounded_service_loop(
                    reconciler.reconcile_once,
                    interval_seconds=timing.reconciler_interval.total_seconds(),
                    operation_timeout_seconds=CONTROL_LOOP_TIMEOUT_SECONDS,
                    progress=loop_progress,
                )
    finally:
        await redis.aclose()
        await engine.dispose()


async def runtime_health(*, instance_id: str, service: str) -> bool:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            heartbeat = await session.scalar(
                text(
                    "SELECT supportguard_record_service_heartbeat("
                    ":instance_id,:service,'__healthcheck__')"
                ),
                {"instance_id": instance_id, "service": service},
            )
            return isinstance(heartbeat, dict) and heartbeat.get("healthy") is True
    finally:
        await engine.dispose()


async def run_retention(*, apply: bool) -> None:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            report = await RetentionService(session, settings).run(apply=apply)
        print(f"mode={report.mode} eligible={report.eligible} deleted={report.deleted}")
    finally:
        await engine.dispose()


async def run_redis_trim(*, apply: bool) -> None:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    redis = Redis.from_url(settings.redis_url, decode_responses=False)
    try:
        async with factory() as session, session.begin():
            report = await trim_terminal_deliveries(
                session,
                redis,
                stream=settings.redis_stream,
                timing=RuntimeTiming.from_settings(settings),
                apply=apply,
                audit_factory=factory,
            )
        print(json.dumps(asdict(report), sort_keys=True))
    finally:
        await redis.aclose()
        await engine.dispose()


async def run_demo_temporal(*, tenant_id: str, refresh: bool) -> None:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            if refresh:
                async with session.begin():
                    report = await refresh_demo_temporal_fixtures(
                        session, settings=settings, tenant_id=tenant_id
                    )
            else:
                report = await demo_temporal_preflight(
                    session, settings=settings, tenant_id=tenant_id
                )
        print(json.dumps(report.payload(), sort_keys=True))
    finally:
        await engine.dispose()


async def maintain_demo_temporal(*, tenant_id: str) -> None:
    """Own the bounded, development-only demo freshness lifecycle."""

    settings = get_settings()
    if settings.app_env == "production" or settings.auth_mode == "production":
        raise RuntimeError("demo_temporal_refresh_forbidden_in_production")
    engine = create_engine(settings)
    factory = create_session_factory(engine, settings=settings)

    async def refresh_once() -> None:
        async with factory() as session, session.begin():
            await refresh_demo_temporal_fixtures(
                session,
                settings=settings,
                tenant_id=tenant_id,
            )

    try:
        await bounded_service_loop(
            refresh_once,
            interval_seconds=30,
            operation_timeout_seconds=CONTROL_LOOP_TIMEOUT_SECONDS,
        )
    finally:
        await engine.dispose()


async def run_demo_resource_preflight(*, tenant_id: str) -> None:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            report = await demo_resource_preflight(session, settings=settings, tenant_id=tenant_id)
        print(json.dumps(report.payload(), sort_keys=True))
        if not report.ready:
            raise RuntimeError("demo_resource_preflight_failed")
    finally:
        await engine.dispose()


def main() -> None:
    args = build_parser().parse_args()
    service = args.runtime_command if args.command == "runtime" else str(args.command)
    if service == "serve":
        service = "api"
    configure_json_logging(service=service)
    configure_tracing(service=service, settings=get_settings())
    if args.command == "serve":
        settings = get_settings()
        uvicorn.run(
            "supportguard.main:app",
            host=settings.app_host,
            port=settings.app_port,
            reload=settings.app_reload,
            log_config=None,
        )
    elif args.command == "db" and args.db_command == "baseline-upgrade":
        upgrade_interview_baseline_database()
    elif args.command == "db" and args.db_command == "seed":
        asyncio.run(seed_database())
    elif args.command == "db" and args.db_command == "bootstrap-interview-roles":
        asyncio.run(bootstrap_interview_database_roles())
    elif args.command == "db" and args.db_command == "configure-mcp-roles":
        asyncio.run(configure_local_mcp_roles())
    elif args.command == "knowledge" and args.knowledge_command in {"ingest", "rebuild"}:
        asyncio.run(ingest_knowledge(fixture=args.fixture))
    elif args.command == "runtime":
        if args.runtime_command == "health":
            healthy = asyncio.run(runtime_health(instance_id=args.instance, service=args.service))
            if not healthy:
                sys.exit(1)
        else:
            asyncio.run(run_runtime_service(args.runtime_command))
    elif args.command == "maintenance" and args.maintenance_command == "retention":
        asyncio.run(run_retention(apply=bool(args.apply)))
    elif args.command == "maintenance" and args.maintenance_command == "redis-trim":
        asyncio.run(run_redis_trim(apply=bool(args.apply)))
    elif args.command == "demo" and args.demo_command == "resource-preflight":
        asyncio.run(run_demo_resource_preflight(tenant_id=str(args.tenant)))
    elif args.command == "demo" and args.demo_command == "temporal-maintain":
        asyncio.run(maintain_demo_temporal(tenant_id=str(args.tenant)))
    elif args.command == "demo":
        asyncio.run(
            run_demo_temporal(
                tenant_id=str(args.tenant),
                refresh=args.demo_command == "temporal-refresh",
            )
        )
