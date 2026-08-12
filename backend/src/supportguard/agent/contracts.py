"""Versioned identities shared by prompts, persistence, and provider requests."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from supportguard.agent.schemas import (
    AgentDecision,
    Classification,
    FinalResponse,
    ProviderBoundEvidenceSynthesis,
)
from supportguard.prompts.registry import load_prompt
from supportguard.rag.embeddings import configured_embedding_fingerprint
from supportguard.rag.types import SourceLocatorV2
from supportguard.tools.gateway import READ_TOOL_ARGUMENTS, native_read_tool_schemas

if TYPE_CHECKING:
    from supportguard.config import Settings

PROMPT_NAME = "agent_decide+bound_evidence_synthesis"
PROMPT_ASSET_VERSION = "v5+v1"
PROMPT_VERSION = "agent_decide.v5+bound_evidence_synthesis.v1"
AGENT_SCHEMA_VERSION = "agent-contract.v5.1"
CONTEXT_VERSION = "context-v1.2"
EXPECTED_PROMPT_HASH = "dc9bcfe05cab140881e8dd441a1e4357dbe4177d5aeec6dc2e8832203550db2d"
EXPECTED_SCHEMA_HASH = "51c30150c8940975a08593e0121432a5c6a7c04c2786dc14186348506b2e90a1"
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class AgentContractDrift(RuntimeError):
    pass


def prompt_text() -> str:
    return "\n\n--- bound synthesis ---\n\n".join(
        (
            load_prompt("agent_decide", version="v5").content,
            load_prompt("bound_evidence_synthesis", version="v1").content,
        )
    )


def contract_manifest() -> dict[str, object]:
    schemas = {
        "classification": Classification.model_json_schema(),
        "agent_decision": AgentDecision.model_json_schema(),
        "bound_evidence_synthesis": ProviderBoundEvidenceSynthesis.model_json_schema(),
        "final_response": FinalResponse.model_json_schema(),
        "source_locator": SourceLocatorV2.model_json_schema(),
        "read_tools": native_read_tool_schemas(set(READ_TOOL_ARGUMENTS)),
    }
    encoded = json.dumps(schemas, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {
        "prompt_name": PROMPT_NAME,
        "prompt_version": PROMPT_VERSION,
        "prompt_hash": hashlib.sha256(prompt_text().encode()).hexdigest(),
        "schema_version": AGENT_SCHEMA_VERSION,
        "schema_hash": hashlib.sha256(encoded.encode()).hexdigest(),
    }


@dataclass(frozen=True, slots=True)
class CanonicalRuntimeManifest:
    manifest_version: str
    prompt_name: str
    prompt_version: str
    prompt_hash: str
    schema_version: str
    schema_hash: str
    context_assembly_version: str
    provider: str
    provider_mode: str
    model: str
    tool_call_mode: str
    knowledge_index_contract: str
    embedding_fingerprint: str
    code_commit: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    def identity_dict(self) -> dict[str, object]:
        """Return semantic runtime identity without repository provenance."""

        return {
            key: value
            for key, value in self.as_dict().items()
            if key != "code_commit"
        }

    @property
    def content_hash(self) -> str:
        encoded = json.dumps(
            self.identity_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


def canonical_runtime_manifest(
    *,
    settings: Settings,
    model: str,
    provider_mode: str,
    tool_call_mode: str,
    context_version: str = CONTEXT_VERSION,
) -> CanonicalRuntimeManifest:
    contract = contract_manifest()
    provider = "deepseek" if provider_mode == "production" else "deterministic-fake"
    return CanonicalRuntimeManifest(
        manifest_version="canonical-runtime-manifest.v2",
        prompt_name=str(contract["prompt_name"]),
        prompt_version=str(contract["prompt_version"]),
        prompt_hash=str(contract["prompt_hash"]),
        schema_version=str(contract["schema_version"]),
        schema_hash=str(contract["schema_hash"]),
        context_assembly_version=context_version,
        provider=provider,
        provider_mode=provider_mode,
        model=model,
        tool_call_mode=tool_call_mode,
        knowledge_index_contract="runtime-pinned-corpus-snapshot.v1",
        embedding_fingerprint=configured_embedding_fingerprint(
            settings,
            testing=settings.app_env == "test",
        ),
        code_commit=settings.code_version,
    )


def runtime_provenance(
    *,
    model: str,
    provider_mode: str,
    tool_call_mode: str,
    context_version: str,
    code_version: str,
    settings: Settings | None = None,
) -> dict[str, object]:
    from supportguard.config import get_settings

    resolved_settings = (settings or get_settings()).model_copy(
        update={"code_version": code_version}
    )
    manifest = canonical_runtime_manifest(
        settings=resolved_settings,
        model=model,
        provider_mode=provider_mode,
        tool_call_mode=tool_call_mode,
        context_version=context_version,
    )
    return {
        **manifest.as_dict(),
        "runtime_manifest_hash": manifest.content_hash,
        # Compatibility aliases retained for existing immutable trace readers.
        "context_version": manifest.context_assembly_version,
        "code_version": manifest.code_commit,
    }


def validate_contract_bundle(
    *,
    prompt: str | None = None,
    read_tools: list[dict[str, object]] | None = None,
) -> None:
    """Fail closed when a deployed prompt/tool bundle diverges from typed contracts."""
    deployed_prompt = prompt if prompt is not None else prompt_text()
    actual_prompt_hash = hashlib.sha256(deployed_prompt.encode()).hexdigest()
    if prompt is None and actual_prompt_hash != EXPECTED_PROMPT_HASH:
        raise AgentContractDrift("canonical_prompt_hash_drift")
    if "containing only the evidence's `citation_binding_id`" not in deployed_prompt:
        raise AgentContractDrift("prompt_citation_binding_contract_drift")
    canonical_tools = native_read_tool_schemas(set(READ_TOOL_ARGUMENTS))
    deployed_tools = read_tools if read_tools is not None else canonical_tools
    canonical = json.dumps(
        canonical_tools, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    candidate = json.dumps(
        deployed_tools, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    if candidate != canonical:
        raise AgentContractDrift("provider_tool_schema_drift")
    actual_schema_hash = str(contract_manifest()["schema_hash"])
    if actual_schema_hash != EXPECTED_SCHEMA_HASH:
        raise AgentContractDrift("canonical_agent_schema_hash_drift")


def validate_candidate_code_version(settings: Settings) -> None:
    if settings.app_env == "production" and not _COMMIT_SHA.fullmatch(settings.code_version):
        raise AgentContractDrift("production_code_commit_unbound")
