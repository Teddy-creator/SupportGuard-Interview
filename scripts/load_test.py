#!/usr/bin/env python3
"""Frozen 20-client / 200-command local acceptance load harness."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import mean
from uuid import uuid4

import httpx
from sqlalchemy import func, select, text

from supportguard.config import Settings
from supportguard.db.models import BillingRecord, InboxDelivery, RuntimeJob, SupportTicket
from supportguard.db.session import create_engine, create_session_factory


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


async def materialize(database_url: str, prefix: str) -> tuple[list[str], list[str]]:
    engine = create_engine(Settings(_env_file=None, database_url=database_url))
    factory = create_session_factory(engine)
    appendable = [f"ticket_{prefix}_append_{index:03d}" for index in range(50)]
    billing_ids = [f"bill_{prefix}_{index:03d}" for index in range(30)]
    async with factory() as session, session.begin():
        original_id = f"bill_{prefix}_original"
        session.add(
            BillingRecord(
                id=original_id,
                tenant_id="tenant_demo",
                customer_id="cust_demo",
                amount=Decimal("9.00"),
                currency="USD",
                status="charged",
                version=1,
            )
        )
        session.add_all(
            [
                BillingRecord(
                    id=billing_id,
                    tenant_id="tenant_demo",
                    customer_id="cust_demo",
                    amount=Decimal("9.00"),
                    currency="USD",
                    status="charged",
                    duplicate_of=original_id,
                    version=1,
                )
                for billing_id in billing_ids
            ]
        )
        session.add_all(
            [
                SupportTicket(
                    id=ticket_id,
                    tenant_id="tenant_demo",
                    customer_id="cust_demo",
                    status="resolved",
                    issue_type="product_knowledge",
                    risk="low",
                    final_response="load fixture",
                )
                for ticket_id in appendable
            ]
        )
    await engine.dispose()
    return appendable, billing_ids


async def session_client(base_url: str, *, role: str) -> tuple[httpx.AsyncClient, str]:
    client = httpx.AsyncClient(base_url=base_url, timeout=30)
    payload: dict[str, str] = {"role": role}
    if role == "customer":
        payload["customer_id"] = "cust_demo"
    response = await client.post("/api/demo-sessions", json=payload)
    response.raise_for_status()
    return client, str(response.json()["csrf_token"])


async def wait_runs(
    client: httpx.AsyncClient, run_ids: list[str], *, timeout_seconds: float
) -> dict[str, str]:
    deadline = time.monotonic() + timeout_seconds
    pending = set(run_ids)
    statuses: dict[str, str] = {}
    while pending and time.monotonic() < deadline:
        for run_id in list(pending):
            response = await client.get(f"/api/runs/{run_id}")
            if response.status_code != 200:
                continue
            current = str(response.json()["status"])
            if current in {"completed", "failed", "interrupted"}:
                statuses[run_id] = current
                pending.remove(run_id)
        if pending:
            await asyncio.sleep(0.5)
    for run_id in pending:
        statuses[run_id] = "timeout"
    return statuses


async def run_load(args: argparse.Namespace) -> dict[str, object]:
    prefix = uuid4().hex[:10]
    appendable, billing_ids = await materialize(args.database_url, prefix)
    customer, customer_csrf = await session_client(args.base_url, role="customer")
    approver, approver_csrf = await session_client(args.base_url, role="approver")
    try:
        # Build 30 real Canonical Interrupts; setup traffic is excluded from measurements.
        prewarm_runs: list[str] = []
        for billing_id in billing_ids:
            response = await customer.post(
                "/api/tickets",
                json={"message": f"{billing_id} 是重复扣费，请按政策退款。"},
                headers={
                    "X-CSRF-Token": customer_csrf,
                    "Idempotency-Key": f"load-setup-{prefix}-{billing_id}",
                },
            )
            response.raise_for_status()
            prewarm_runs.append(str(response.json()["run_id"]))
        setup_statuses = await wait_runs(
            customer, prewarm_runs, timeout_seconds=args.completion_timeout
        )
        if set(setup_statuses.values()) != {"interrupted"}:
            raise RuntimeError(f"approval fixture setup failed: {setup_statuses}")
        approval_rows = (await approver.get("/api/approvals")).json()
        wanted = set(billing_ids)
        approvals = [
            row for row in approval_rows if row["action_payload"].get("billing_record_id") in wanted
        ]
        if len(approvals) != 30:
            raise RuntimeError(f"expected 30 load approvals, got {len(approvals)}")

        commands: list[tuple[str, str, dict[str, str], dict[str, str]]] = []
        commands.extend(
            (
                "new_ticket",
                "/api/tickets",
                {"message": "atlas-chat 当前是否支持 JSON Object，限制是什么？"},
                {"X-CSRF-Token": customer_csrf},
            )
            for _ in range(120)
        )
        commands.extend(
            (
                "append_message",
                f"/api/tickets/{ticket_id}/messages",
                {"message": "请继续说明当前 JSON Object 限制。"},
                {"X-CSRF-Token": customer_csrf},
            )
            for ticket_id in appendable
        )
        commands.extend(
            (
                "approval_decision",
                f"/api/approvals/{row['id']}/approve",
                {"reason": "Frozen local load fixture approval."},
                {"X-CSRF-Token": approver_csrf},
            )
            for row in approvals
        )
        semaphore = asyncio.Semaphore(20)
        stop_sampling = asyncio.Event()
        connection_samples: list[int] = []
        sse_watchers: list[asyncio.Task[float]] = []

        async def sample_connections() -> None:
            engine = create_engine(Settings(_env_file=None, database_url=args.database_url))
            factory = create_session_factory(engine)
            try:
                while not stop_sampling.is_set():
                    async with factory() as session:
                        count = await session.scalar(
                            text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE datname=current_database()"
                            )
                        )
                        connection_samples.append(int(count or 0))
                    await asyncio.sleep(0.25)
            finally:
                await engine.dispose()

        measurement_start = time.monotonic()

        async def watch_final_event(ticket_id: str) -> float:
            async with asyncio.timeout(60):
                async with customer.stream(
                    "GET",
                    f"/api/tickets/{ticket_id}/events/stream",
                    headers={"Last-Event-ID": "0"},
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        event = json.loads(line[6:])
                        if event["event_type"] == "final_outcome":
                            committed = datetime.fromisoformat(event["created_at"])
                            return (datetime.now(UTC) - committed).total_seconds() * 1000
            raise RuntimeError("SSE final outcome was not observed")

        async def submit(index: int, command):  # type: ignore[no-untyped-def]
            kind, url, body, headers = command
            scheduled_at = measurement_start + (index * args.measurement_seconds / len(commands))
            await asyncio.sleep(max(0, scheduled_at - time.monotonic()))
            client = approver if kind == "approval_decision" else customer
            request_headers = {
                **headers,
                "Idempotency-Key": f"load-measured-{prefix}-{index:03d}",
            }
            async with semaphore:
                started = time.perf_counter()
                response = await client.post(url, json=body, headers=request_headers)
                latency_ms = (time.perf_counter() - started) * 1000
            payload = response.json() if response.headers.get("content-type", "").startswith(
                "application/json"
            ) else {}
            if index < 20 and response.status_code == 202 and "ticket_id" in payload:
                sse_watchers.append(
                    asyncio.create_task(watch_final_event(str(payload["ticket_id"])))
                )
            return kind, response.status_code, latency_ms, payload

        started_at = datetime.now(UTC)
        sampler = asyncio.create_task(sample_connections())
        try:
            results = await asyncio.gather(
                *(submit(index, command) for index, command in enumerate(commands))
            )
            accepted = [item for item in results if item[1] == 202]
            job_ids = [str(item[3]["job_id"]) for item in accepted]
            run_ids = [str(item[3]["run_id"]) for item in accepted]
            terminal = await wait_runs(customer, run_ids, timeout_seconds=args.completion_timeout)
            sse_lag_ms = await asyncio.gather(*sse_watchers)
        finally:
            stop_sampling.set()
            await sampler

        engine = create_engine(Settings(_env_file=None, database_url=args.database_url))
        factory = create_session_factory(engine)
        async with factory() as session:
            jobs = list(
                (await session.scalars(select(RuntimeJob).where(RuntimeJob.id.in_(job_ids)))).all()
            )
            delivery_rows = (
                await session.execute(
                    select(InboxDelivery.job_id, func.min(InboxDelivery.created_at))
                    .where(InboxDelivery.job_id.in_(job_ids))
                    .group_by(InboxDelivery.job_id)
                )
            ).all()
            first_deliveries: dict[str, datetime] = {
                str(job_id): created_at for job_id, created_at in delivery_rows
            }
        await engine.dispose()
        latencies = [item[2] for item in results]
        queue_wait_ms = [
            (first_deliveries[job.id] - job.created_at).total_seconds() * 1000
            for job in jobs
            if job.id in first_deliveries
        ]
        completion_ms = [
            (job.updated_at - job.created_at).total_seconds() * 1000 for job in jobs
        ]
        return {
            "schema_version": "load-report.v1",
            "run_at": started_at.isoformat(),
            "platform": platform.platform(),
            "provider_mode": "deterministic-fake-required",
            "workers": 2,
            "clients": 20,
            "warmup_seconds": args.warmup_seconds,
            "measurement_seconds": args.measurement_seconds,
            "commands": 200,
            "mix": {"new_ticket": 120, "append_message": 50, "approval_decision": 30},
            "api_accept_ms": {
                "mean": mean(latencies),
                "p50": percentile(latencies, 0.50),
                "p95": percentile(latencies, 0.95),
                "p99": percentile(latencies, 0.99),
            },
            "queue_wait_ms": {
                "p50": percentile(queue_wait_ms, 0.50),
                "p95": percentile(queue_wait_ms, 0.95),
                "p99": percentile(queue_wait_ms, 0.99),
                "denominator": len(queue_wait_ms),
            },
            "run_completion_ms": {
                "p50": percentile(completion_ms, 0.50),
                "p95": percentile(completion_ms, 0.95),
                "p99": percentile(completion_ms, 0.99),
                "denominator": len(completion_ms),
            },
            "postgres_connections": {
                "max_observed": max(connection_samples, default=0),
                "samples": len(connection_samples),
                "budget": 40,
            },
            "sse_event_visibility_ms": {
                "p50": percentile(sse_lag_ms, 0.50),
                "p95": percentile(sse_lag_ms, 0.95),
                "p99": percentile(sse_lag_ms, 0.99),
                "denominator": len(sse_lag_ms),
                "replay_complete": len(sse_lag_ms) == 20,
            },
            "http_status_counts": {
                str(code): sum(item[1] == code for item in results)
                for code in sorted({item[1] for item in results})
            },
            "accepted": len(accepted),
            "terminal_counts": {
                status: sum(value == status for value in terminal.values())
                for status in sorted(set(terminal.values()))
            },
            "job_status_counts": {
                status: sum(job.status == status for job in jobs)
                for status in sorted({job.status for job in jobs})
            },
            "raw": {"job_ids": job_ids, "run_ids": run_ids},
        }
    finally:
        await customer.aclose()
        await approver.aclose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--database-url",
        default="postgresql+asyncpg://supportguard:supportguard@localhost:5432/supportguard",
    )
    parser.add_argument("--warmup-seconds", type=int, default=30)
    parser.add_argument("--measurement-seconds", type=int, default=30)
    parser.add_argument("--completion-timeout", type=int, default=300)
    parser.add_argument("--output", default="evals/reports/load-v1.2.json")
    args = parser.parse_args()
    if args.warmup_seconds:
        time.sleep(args.warmup_seconds)
    report = asyncio.run(run_load(args))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {key: report[key] for key in ("accepted", "api_accept_ms", "terminal_counts")}
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
