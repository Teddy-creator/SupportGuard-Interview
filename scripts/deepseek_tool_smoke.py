#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json

from supportguard.config import Settings
from supportguard.providers.deepseek import DeepSeekProvider


async def main() -> None:
    settings = Settings(_env_file=None)
    provider = DeepSeekProvider(settings)
    try:
        result = await provider.decide(
            system=(
                "This is a native tool contract smoke, not an answer-quality task. "
                "Call search_knowledge exactly once."
            ),
            context="Use search_knowledge for the query: refund policy.",
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "search_knowledge",
                        "description": "Search the support knowledge base.",
                        "parameters": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                            "additionalProperties": False,
                        },
                    },
                }
            ],
            prior_turns=[],
            trace_metadata={"purpose": "v125-native-tool-contract-smoke"},
        )
    finally:
        await provider.aclose()
    calls = result.output.tool_calls
    if len(calls) != 1 or calls[0].name != "search_knowledge":
        raise RuntimeError("deepseek_native_tool_contract_failed")
    print(
        json.dumps(
            {
                "schema_version": "deepseek-native-tool-smoke.v1",
                "model": settings.llm_model,
                "thinking_enabled": settings.llm_thinking_enabled,
                "temperature": settings.llm_temperature,
                "tool_call_mode": provider.tool_call_mode,
                "tool_call_count": len(calls),
                "tool_name": calls[0].name,
                "finish_reason": result.output.finish_reason,
                "request_hash": result.transport.request_hash if result.transport else None,
                "prompt_tokens": result.usage.prompt_tokens,
                "completion_tokens": result.usage.completion_tokens,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
