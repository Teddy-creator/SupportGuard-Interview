from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine

from supportguard.db import upgrade_v126


async def _pause_after_committed_phase(phase: str) -> None:
    expected = os.getenv("UPGRADE_PAUSE_AFTER_PHASE")
    ready = os.getenv("UPGRADE_PHASE_READY_PATH")
    if phase != expected or not ready:
        return
    await asyncio.to_thread(Path(ready).write_text, phase, encoding="utf-8")
    await asyncio.Event().wait()


async def main() -> None:
    engine = create_async_engine(os.environ["UPGRADE_DATABASE_URL"])
    redis = Redis.from_url(os.environ["UPGRADE_REDIS_URL"], decode_responses=False)
    original_quiesce = upgrade_v126.quiesce_upgrade
    original_advance = upgrade_v126._advance

    async def pausing_quiesce(*args: object, **kwargs: object) -> object:
        result = await original_quiesce(*args, **kwargs)  # type: ignore[arg-type]
        await _pause_after_committed_phase("quiesced")
        return result

    async def pausing_advance(*args: object, **kwargs: object) -> None:
        await original_advance(*args, **kwargs)  # type: ignore[arg-type]
        await _pause_after_committed_phase(str(kwargs.get("next_phase", "")))

    upgrade_v126.quiesce_upgrade = pausing_quiesce  # type: ignore[assignment]
    upgrade_v126._advance = pausing_advance  # type: ignore[assignment]
    try:
        result = await upgrade_v126.run_upgrade_bridge(
            engine,
            redis,
            artifact_directory=Path(os.environ["UPGRADE_ARTIFACT_DIRECTORY"]),
            stream=os.environ["UPGRADE_STREAM"],
            source_revision=os.environ["UPGRADE_SOURCE_REVISION"],
            target_revision=os.environ["UPGRADE_TARGET_REVISION"],
            run_id=os.environ["UPGRADE_RUN_ID"],
            actor_instance_id=os.environ["UPGRADE_ACTOR_INSTANCE_ID"],
            sample_interval_seconds=0.01,
        )
        payload = json.dumps(
            {
                "run_id": result.run_id,
                "phase": result.phase,
                "artifact_hash": result.artifact_hash,
                "attestation_hash": result.attestation_hash,
            },
            sort_keys=True,
        )
        await asyncio.to_thread(
            Path(os.environ["UPGRADE_RESULT_PATH"]).write_text,
            payload,
            encoding="utf-8",
        )
    finally:
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
