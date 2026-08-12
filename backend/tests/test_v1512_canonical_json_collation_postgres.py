from __future__ import annotations

import hashlib
import json
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from supportguard.contracts.canonical_json import canonical_json_bytes

pytestmark = pytest.mark.postgres


@pytest.mark.asyncio
async def test_canonical_json_hash_is_byte_exact_for_locale_sensitive_nested_keys() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")

    value = {
        "freshness_status": "fresh",
        "fresh_until": "2026-07-29T00:00:00Z",
        "nested": {
            "z_key": 1,
            "zebra": 2,
            "éclair": "composed",
            "e\u0301clair": "decomposed",
            "中文_key": "值",
        },
        "array": [
            {
                "resource_version": 3,
                "resource_id": "sub_demo",
            }
        ],
    }
    expected = canonical_json_bytes(value)
    assert expected.index(b'"fresh_until"') < expected.index(b'"freshness_status"')
    expected_hash = hashlib.sha256(expected).hexdigest()

    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("SET LOCAL ROLE supportguard_owner"))
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
    finally:
        await engine.dispose()

    actual_bytes = str(actual).encode("utf-8")
    assert actual_bytes == expected
    assert hashlib.sha256(actual_bytes).hexdigest() == expected_hash
