from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Literal

from supportguard.contracts.capabilities import (
    POLICY_TOOL_INPUTS,
    READ_TOOL_INPUTS,
    RUNTIME_COMMAND_INPUTS,
)

READ_CAPABILITIES = frozenset(READ_TOOL_INPUTS)
ACTION_PROPOSAL_CAPABILITIES = frozenset(POLICY_TOOL_INPUTS)
RUNTIME_EFFECT_CAPABILITIES = frozenset(RUNTIME_COMMAND_INPUTS)


@dataclass(frozen=True, slots=True)
class ToolCapability:
    name: str
    effect_class: Literal["read", "proposal", "mutation"]
    model_visible: bool
    required_scope: str
    allowed_callers: tuple[str, ...]
    requires_fence: bool
    requires_human_approval: bool
    idempotency_required: bool
    concurrency_group: str
    timeout_ms: int
    interrupt_policy: str
    schema_hash: str


def _hash_schema(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _read(name: str) -> ToolCapability:
    return ToolCapability(
        name=name,
        effect_class="read",
        model_visible=True,
        required_scope="worker_execution",
        allowed_callers=("agent_runtime",),
        requires_fence=True,
        requires_human_approval=False,
        idempotency_required=False,
        concurrency_group="read_mcp",
        timeout_ms=8_000,
        interrupt_policy="bounded_retry_once",
        schema_hash=_hash_schema(READ_TOOL_INPUTS[name].model_json_schema()),
    )


CAPABILITIES: dict[str, ToolCapability] = {
    **{name: _read(name) for name in READ_CAPABILITIES},
    **{
        name: ToolCapability(
            name=name,
            effect_class="proposal",
            model_visible=False,
            required_scope="worker_execution",
            allowed_callers=("deterministic_policy",),
            requires_fence=True,
            requires_human_approval=True,
            idempotency_required=True,
            concurrency_group="action_mcp",
            timeout_ms=8_000,
            interrupt_policy="fail_closed",
            schema_hash=_hash_schema({"name": name, "surface": "policy_only"}),
        )
        for name in ACTION_PROPOSAL_CAPABILITIES
    },
    **{
        name: ToolCapability(
            name=name,
            effect_class="mutation",
            model_visible=False,
            required_scope="worker_execution",
            allowed_callers=("runtime_finalizer",),
            requires_fence=True,
            requires_human_approval=True,
            idempotency_required=True,
            concurrency_group="runtime_action",
            timeout_ms=8_000,
            interrupt_policy="reconcile_unknown",
            schema_hash=_hash_schema({"name": name, "surface": "runtime_only"}),
        )
        for name in RUNTIME_EFFECT_CAPABILITIES
    },
}


MODEL_VISIBLE_READ_TOOLS = frozenset(
    name for name, capability in CAPABILITIES.items() if capability.model_visible
)


def registry_hash() -> str:
    return _hash_schema({name: asdict(CAPABILITIES[name]) for name in sorted(CAPABILITIES)})


def authorize_model_tool(name: str, allowlist: set[str]) -> ToolCapability:
    capability = CAPABILITIES.get(name)
    if capability is None or not capability.model_visible or capability.effect_class != "read":
        raise PermissionError("forbidden_tool_surface")
    if name not in allowlist:
        raise PermissionError("tool_not_allowlisted")
    return capability
