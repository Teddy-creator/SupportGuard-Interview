from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx

from supportguard.config import Settings
from supportguard.providers.deepseek import DeepSeekProvider


def canonical_fixture_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def validate_native_tool_cassette(path: Path) -> dict[str, Any]:
    cassette: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if cassette.get("cassette_schema") not in {
        "deepseek-native-tools.v1",
        "deepseek-native-tools.v2",
        "deepseek-native-tools.v3",
    }:
        raise ValueError("unsupported_cassette_schema")
    for side in ("response", "parser_expectation"):
        expected = cassette.get(f"{side}_hash")
        if expected != canonical_fixture_hash(cassette.get(side)):
            raise ValueError(f"{side}_hash_mismatch")
    request_hash = cassette.get("request_hash")
    if not isinstance(request_hash, str) or len(request_hash) != 64:
        raise ValueError("request_hash_invalid")
    return cassette


async def replay_native_tool_cassette(path: Path) -> dict[str, object]:
    """Exercise the production OpenAI serializer and DeepSeek response parser offline."""
    cassette = validate_native_tool_cassette(path)
    request_input = cassette.get("request_input")
    if not isinstance(request_input, dict):
        raise ValueError("request_input_invalid")
    response_payload = cassette["response"]
    observed_requests: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        observed = json.loads(request.content)
        if not isinstance(observed, dict):
            raise ValueError("serialized_request_not_object")
        observed_requests.append(observed)
        return httpx.Response(200, json=response_payload, request=request)

    settings = Settings(
        _env_file=None,
        deepseek_api_key="offline-cassette-placeholder",
        llm_model=str(cassette["model"]),
        llm_temperature=0,
        llm_thinking_enabled=False,
    )
    provider = DeepSeekProvider(settings, http_transport=httpx.MockTransport(handler))
    try:
        result = await provider.decide(
            system=str(request_input["system"]),
            context=str(request_input["context"]),
            tools=list(request_input["tools"]),
            prior_turns=list(request_input["prior_turns"]),
            trace_metadata={"fixture": "offline-native-tools"},
        )
    finally:
        await provider.aclose()
    if len(observed_requests) != 1:
        raise ValueError("unexpected_transport_request_count")
    request_hash = canonical_fixture_hash(observed_requests[0])
    if request_hash != cassette["request_hash"]:
        raise ValueError("production_request_hash_mismatch")
    parsed = {
        "finish_reason": result.output.finish_reason,
        "tool_calls": [
            {
                "id": item.provider_tool_call_id,
                "name": item.name,
                "arguments": json.loads(item.arguments_json),
            }
            for item in result.output.tool_calls
        ],
    }
    if parsed != cassette["parser_expectation"]:
        raise ValueError("production_parser_expectation_mismatch")
    if result.transport is None or result.transport.request_bytes == b"":
        raise ValueError("production_transport_capture_missing")
    return {
        "request_hash": request_hash,
        "response_hash": canonical_fixture_hash(response_payload),
        "transport_request_hash": result.transport.request_hash,
        "tool_call_count": len(result.output.tool_calls),
        "network_access": False,
    }
