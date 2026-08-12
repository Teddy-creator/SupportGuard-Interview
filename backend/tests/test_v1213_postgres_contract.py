from __future__ import annotations

import hashlib
import json
import os

import pytest
from sqlalchemy import exc, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from supportguard.contracts.canonical_json import canonical_json_bytes
from supportguard.db.reference_contract import CURRENT_PRODUCT_DATABASE_HEAD
from supportguard.db.role_contract import (
    MCP_HELPER_CALL_GRAPH,
    MCP_OWNER_ONLY_HELPERS,
    RUNTIME_ROLES,
)

pytestmark = pytest.mark.postgres


@pytest.mark.asyncio
async def test_canonical_json_v1_python_and_postgres_vectors_are_byte_exact() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    vectors: list[object] = [
        {"b": 1, "a": "中文"},
        {"composed": "é", "decomposed": "e\u0301", "emoji": "😀"},
        {"z": None, "a": [True, False, {"k": "v"}]},
        {"escaped": 'line\n"quote"\\slash'},
        {"array": [3, 2, 1]},
        {"money": "49.00", "zero": 0},
    ]
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("SET LOCAL ROLE supportguard_owner"))
            for value in vectors:
                expected = canonical_json_bytes(value)
                actual = await connection.scalar(
                    text("SELECT supportguard_canonical_jsonb(CAST(:value AS jsonb))"),
                    {
                        "value": json.dumps(
                            value,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        )
                    },
                )
                assert str(actual).encode("utf-8") == expected
                assert hashlib.sha256(str(actual).encode("utf-8")).digest() == (
                    hashlib.sha256(expected).digest()
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_v1213_forward_repairs_are_current_head_without_new_schema_objects() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                CURRENT_PRODUCT_DATABASE_HEAD
            )
            extra_tables = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname IN ('public','supportguard_control') "
                    "AND c.relname LIKE 'v1213_%'"
                )
            )
            assert extra_tables == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_v1213_mcp_helpers_and_call_graph_match_frozen_manifest() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_async_engine(database_url)
    helper_names = [item.signature.split("(", 1)[0] for item in MCP_OWNER_ONLY_HELPERS]
    expected = {
        item.signature: {
            "identity_arguments": item.identity_arguments,
            "owner": item.owner,
            "volatility": item.volatility,
            "strict": item.strict,
            "parallel": item.parallel,
            "security_definer": item.security_definer,
            "search_path": list(item.search_path),
            "definition_sha256": item.definition_sha256,
        }
        for item in MCP_OWNER_ONLY_HELPERS
    }
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT p.oid::regprocedure::text,"
                        "pg_get_function_identity_arguments(p.oid),"
                        "pg_get_userbyid(p.proowner),p.provolatile,p.proisstrict,p.proparallel,"
                        "p.prosecdef,COALESCE(p.proconfig,ARRAY[]::text[]),"
                        "encode(pg_catalog.sha256(convert_to(pg_get_functiondef(p.oid),"
                        "'UTF8')),'hex') FROM pg_proc p JOIN pg_namespace n "
                        "ON n.oid=p.pronamespace WHERE n.nspname='public' "
                        "AND p.proname=ANY(:names) ORDER BY p.oid::regprocedure::text"
                    ),
                    {"names": helper_names},
                )
            ).all()
            actual = {
                str(row[0]): {
                    "identity_arguments": row[1],
                    "owner": row[2],
                    "volatility": row[3].decode() if isinstance(row[3], bytes) else row[3],
                    "strict": row[4],
                    "parallel": row[5].decode() if isinstance(row[5], bytes) else row[5],
                    "security_definer": row[6],
                    "search_path": list(row[7]),
                    "definition_sha256": row[8],
                }
                for row in rows
            }
            assert actual == expected

            for signature in expected:
                assert not await connection.scalar(
                    text("SELECT has_function_privilege('public',:signature,'EXECUTE')"),
                    {"signature": signature},
                )
                for role in RUNTIME_ROLES:
                    assert not await connection.scalar(
                        text("SELECT has_function_privilege(:role,:signature,'EXECUTE')"),
                        {"role": role, "signature": signature},
                    )

            graph_rows = (
                await connection.execute(
                    text(
                        "WITH functions AS (SELECT p.oid::regprocedure::text AS signature,"
                        "p.proname,pg_get_functiondef(p.oid) AS body FROM pg_proc p "
                        "JOIN pg_namespace n ON n.oid=p.pronamespace "
                        "WHERE n.nspname='public' AND p.proname LIKE 'supportguard_%') "
                        "SELECT signature,helper FROM functions CROSS JOIN "
                        "unnest(CAST(:helpers AS text[])) AS helper "
                        "WHERE strpos(body,helper)>0 AND proname<>helper"
                    ),
                    {"helpers": helper_names},
                )
            ).all()
            assert {(str(row[0]), str(row[1])) for row in graph_rows} == set(MCP_HELPER_CALL_GRAPH)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_v1213_every_runtime_login_is_denied_all_six_helpers() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    statements = [
        "SELECT supportguard_action_mcp_execute(NULL,NULL,NULL)",
        "SELECT supportguard_action_observation_bound(NULL,NULL,NULL,NULL,NULL,NULL)",
        "SELECT supportguard_canonical_jsonb(NULL)",
        (
            "SELECT supportguard_read_mcp_chunk_payload(NULL::knowledge_chunks,"
            "NULL::knowledge_documents,NULL,NULL,NULL,NULL)"
        ),
        "SELECT supportguard_read_mcp_execute(NULL,NULL,NULL)",
        "SELECT supportguard_read_mcp_search_execute(NULL,NULL)",
    ]
    for role in sorted(RUNTIME_ROLES):
        role_url = (
            make_url(database_url)
            .set(username=role, password=role)
            .render_as_string(hide_password=False)
        )
        engine = create_async_engine(role_url)
        try:
            for statement in statements:
                async with engine.connect() as connection:
                    with pytest.raises(exc.DBAPIError) as denied:
                        await connection.execute(text(statement))
                    assert str(getattr(denied.value.orig, "sqlstate", "")) == "42501"
        finally:
            await engine.dispose()
