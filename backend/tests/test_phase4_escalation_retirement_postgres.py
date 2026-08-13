from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import exc, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.postgres


def _url(role: str | None = None) -> str:
    raw = os.getenv("TEST_DATABASE_URL")
    if not raw:
        pytest.skip("TEST_DATABASE_URL is required")
    if role is None:
        return raw
    return make_url(raw).set(username=role, password=role).render_as_string(hide_password=False)


def _load_vertical_fixture_owner() -> ModuleType:
    path = Path(__file__).with_name("test_v124_postgres_mcp_vertical.py")
    spec = importlib.util.spec_from_file_location("phase4_vertical_fixture_owner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _row_counts(database_url: str) -> tuple[int, int, int]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return tuple(
                int(value)
                for value in (
                    await connection.execute(
                        text(
                            "SELECT (SELECT count(*) FROM proposal_records),"
                            "(SELECT count(*) FROM escalation_records),"
                            "(SELECT count(*) FROM business_actions)"
                        )
                    )
                ).one()
            )  # type: ignore[return-value]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_escalation_direct_and_generic_paths_fail_closed_without_writes() -> None:
    admin_url = _url()
    before = await _row_counts(admin_url)

    action_engine = create_async_engine(_url("supportguard_action_mcp"))
    try:
        async with action_engine.connect() as connection:
            with pytest.raises(exc.DBAPIError) as direct_denied:
                await connection.execute(
                    text(
                        "SELECT supportguard_action_mcp_create_support_escalation("
                        "CAST('{}' AS jsonb),CAST('{}' AS jsonb))"
                    )
                )
        assert str(getattr(direct_denied.value.orig, "sqlstate", "")) == "42501"
    finally:
        await action_engine.dispose()

    admin_engine = create_async_engine(admin_url)
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text("SET LOCAL ROLE supportguard_owner"))
            for capability in ("create_support_escalation", "unknown_capability"):
                with pytest.raises(exc.DBAPIError) as generic_denied:
                    async with connection.begin_nested():
                        await connection.execute(
                            text(
                                "SELECT supportguard_action_mcp_execute("
                                ":capability,CAST(:arguments AS jsonb),"
                                "CAST(:context AS jsonb))"
                            ),
                            {
                                "capability": capability,
                                "arguments": "{}",
                                "context": json.dumps(
                                    {"execution_payload": {"observation_binding": []}}
                                ),
                            },
                        )
                assert str(getattr(generic_denied.value.orig, "sqlstate", "")) == "42501"

            for capability in (
                "propose_refund",
                "propose_api_key_revocation",
                "propose_entitlement_change",
            ):
                with pytest.raises(exc.DBAPIError, match="action_scope_unavailable") as routed:
                    async with connection.begin_nested():
                        await connection.execute(
                            text(
                                "SELECT supportguard_action_mcp_execute("
                                ":capability,CAST(:arguments AS jsonb),"
                                "CAST(:context AS jsonb))"
                            ),
                            {
                                "capability": capability,
                                "arguments": "{}",
                                "context": json.dumps(
                                    {"execution_payload": {"observation_binding": []}}
                                ),
                            },
                        )
                assert str(getattr(routed.value.orig, "sqlstate", "")) == "55000"
    finally:
        await admin_engine.dispose()

    assert await _row_counts(admin_url) == before


@pytest.mark.asyncio
@pytest.mark.mcp
async def test_three_current_action_proposals_complete_real_stdio_and_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _load_vertical_fixture_owner()
    admin_url = _url()
    common, bindings, _reads, reservations = await owner._runtime_fixture(admin_url)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("MCP_ACTION_DATABASE_URL", _url("supportguard_action_mcp"))
    actions = owner._action_cases(str(common["run_id"]))
    current = (
        "propose_refund",
        "propose_api_key_revocation",
        "propose_entitlement_change",
    )

    async with owner.action_mcp_session() as session:
        discovered = {tool.name for tool in (await session.list_tools()).tools}
        assert discovered == set(current)
        for ordinal, name in enumerate(current, 1):
            arguments = {
                **common,
                **actions[name],
                "tool_call_id": f"phase4_action_{ordinal}",
                "observation_binding": bindings,
                "mcp_context": owner._capability_context(name, reservations[name]),
            }
            payload = owner.structured_result(await session.call_tool(name, arguments))
            assert payload.get("domain_error") is not True, (name, payload)
            assert payload["status"] == "draft"
            assert payload["action_type"] in {
                "refund",
                "api_key_revocation",
                "entitlement_change",
            }

    engine = create_async_engine(admin_url)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT action_type,status FROM proposal_records "
                        "WHERE run_id=:run_id ORDER BY action_type"
                    ),
                    {"run_id": common["run_id"]},
                )
            ).all()
        assert {(str(row[0]), str(row[1])) for row in rows} == {
            ("refund", "draft"),
            ("api_key_revocation", "draft"),
            ("entitlement_change", "draft"),
        }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.mcp
async def test_refund_proposal_uses_active_fence_when_ticket_projection_has_converged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _load_vertical_fixture_owner()
    admin_url = _url()
    common, bindings, _reads, reservations = await owner._runtime_fixture(admin_url)
    engine = create_async_engine(admin_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE support_tickets SET status='resolved' WHERE id=:ticket_id"),
                {"ticket_id": common["ticket_id"]},
            )
            effect_count_before = int(
                await connection.scalar(text("SELECT count(*) FROM business_actions")) or 0
            )

        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("MCP_ACTION_DATABASE_URL", _url("supportguard_action_mcp"))
        arguments = {
            **common,
            **owner._action_cases(str(common["run_id"]))["propose_refund"],
            "tool_call_id": "phase7_refund_projection_lag",
            "observation_binding": bindings,
            "mcp_context": owner._capability_context(
                "propose_refund", reservations["propose_refund"]
            ),
        }
        async with owner.action_mcp_session() as session:
            payload = owner.structured_result(await session.call_tool("propose_refund", arguments))
        assert payload.get("domain_error") is not True, payload
        assert payload["status"] == "draft"
        assert payload["action_type"] == "refund"
        assert payload["resource_id"] == "bill_demo_duplicate"

        async with engine.connect() as connection:
            proposal_count = await connection.scalar(
                text(
                    "SELECT count(*) FROM proposal_records "
                    "WHERE run_id=:run_id AND action_type='refund'"
                ),
                {"run_id": common["run_id"]},
            )
            effect_count = await connection.scalar(
                text("SELECT count(*) FROM business_actions"),
            )
        assert proposal_count == 1
        assert effect_count == effect_count_before
    finally:
        await engine.dispose()
