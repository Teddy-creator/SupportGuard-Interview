from __future__ import annotations

import asyncio
import json

import pytest

from supportguard.agent.schemas import Classification
from supportguard.config import Settings
from supportguard.main import build_provider
from supportguard.providers.fake import DeterministicFakeProvider


def test_fake_provider_delay_is_explicit_and_bounded() -> None:
    settings = Settings(
        _env_file=None,
        demo_fake_provider=True,
        demo_fake_provider_delay_seconds=0.01,
    )
    provider = build_provider(settings, testing=False)
    assert isinstance(provider, DeterministicFakeProvider)
    assert provider.delay_seconds == 0.01


@pytest.mark.asyncio
async def test_concurrent_provider_results_own_their_transport_capture() -> None:
    provider = DeterministicFakeProvider(delay_seconds=0.01)

    async def invoke(ticket: str):
        return await provider.generate(
            system="classify",
            user=json.dumps({"ticket": ticket}),
            output_schema=Classification,
            trace_metadata={"ticket": ticket},
        )

    first, second = await asyncio.gather(invoke("first 429"), invoke("second 退款"))
    assert first.transport is not None
    assert second.transport is not None
    assert first.transport.request_hash != second.transport.request_hash
    assert json.loads(first.transport.request_bytes)["user"] == json.dumps({"ticket": "first 429"})
    assert json.loads(second.transport.request_bytes)["user"] == json.dumps(
        {"ticket": "second 退款"}
    )
