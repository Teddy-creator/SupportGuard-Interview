from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


async def main() -> None:
    engine = create_async_engine(os.environ["BARRIER_DATABASE_URL"])
    connection = await engine.connect()
    try:
        result = await connection.scalar(
            text(
                "SELECT supportguard_runtime_acquire_writer_barrier("
                "CAST(:payload AS jsonb))"
            ),
            {
                "payload": json.dumps(
                    {
                        "schema_version": "writer-barrier-acquire.v1",
                        "operation": "dispatcher",
                        "session_nonce": uuid4().hex,
                        "drain": None,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            },
        )
        if not isinstance(result, dict):
            raise RuntimeError("writer barrier fixture did not acquire a lock")
        await connection.commit()
        await asyncio.to_thread(
            Path(os.environ["BARRIER_READY_PATH"]).write_text,
            "ready",
            encoding="utf-8",
        )
        await asyncio.Event().wait()
    finally:
        await connection.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
