from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from supportguard.agent.context import ContextAssembler, ContextBudget
from supportguard.agent.contracts import CanonicalRuntimeManifest
from supportguard.agent.conversation_semantics import contains_exact_resource_reference
from supportguard.agent.nodes.runtime_support import GraphRuntimeSupport
from supportguard.agent.patterns import SAFE_STRUCTURED_ERROR_PATH
from supportguard.agent.proposal_assembler import (
    evaluate_grounded_repair_eligibility,
)
from supportguard.agent.schemas import (
    AgentDecision,
    CandidateResponse,
    Classification,
    GroundedRepairEligibility,
    ProviderBoundEvidenceSynthesis,
)
from supportguard.agent.state import AgentState
from supportguard.config import Settings
from supportguard.contracts.action_preconditions import explicit_current_turn_action
from supportguard.contracts.canonical_json import canonical_json_hash
from supportguard.contracts.testing import TestRuntimeCapability
from supportguard.db.models import (
    RetrievalTrace,
    ToolInvocation,
    ToolObservation,
    new_id,
)
from supportguard.providers.base import StructuredProvider
from supportguard.providers.deepseek import (
    ProviderStructuredOutputError,
)
from supportguard.rag.context_projection import EVIDENCE_PROJECTION_V2, project_context_evidence
from supportguard.services.conversation_action_state import ConversationActionStateV1
from supportguard.services.runtime_jobs import RuntimeConflict
from supportguard.tools.gateway import ToolGateway


class AgentRuntimeServices(GraphRuntimeSupport):
    """Explicit runtime services injected into typed Agent node collaborators."""

    def __init__(
        self,
        *,
        provider: StructuredProvider,
        gateway: ToolGateway,
        budget: ContextBudget,
        context_assembler: ContextAssembler,
        approval_handler: Any | None,
        history_loader: Any | None,
        session: AsyncSession | None,
        test_capability: TestRuntimeCapability | None,
        settings: Settings,
        runtime_manifest: CanonicalRuntimeManifest,
    ) -> None:
        self.provider = provider
        self.gateway = gateway
        self.budget = budget
        self.context_assembler = context_assembler
        self.approval_handler = approval_handler
        self.history_loader = history_loader
        self.session = session
        self.test_capability = test_capability
        self.settings = settings
        self.runtime_manifest = runtime_manifest
        self.segment_events: list[dict[str, Any]] = []

    @staticmethod
    def _action_issue_type(action_type: str) -> str:
        return {
            "refund": "billing_refund",
            "api_key_revocation": "credential_security",
            "entitlement_change": "entitlement_change",
        }.get(action_type, "unknown")

    @classmethod
    def _canonical_action_query_classification(
        cls,
        provider_classification: Classification,
        *,
        action_state_query: dict[str, Any] | None,
        current_actions: list[dict[str, Any]],
    ) -> Classification:
        """Keep Provider semantics advisory while canonical action truth stays deterministic.

        A status/reason follow-up must produce a real Provider attempt in
        production, but Provider output cannot select a different resource,
        reopen an action, or grant proposal authority.  The bounded recognizer
        selects an existing customer-scoped projection; this adapter then
        narrows the Provider classification to the authority-free read.
        """

        if action_state_query is None:
            return provider_classification
        if action_state_query.get("resolution") != "selected":
            return Classification(
                issue_type="unknown",
                risk="low",
                policy_boundary="allowed",
                requested_action="none",
                requested_concurrency_limit=None,
                needs_realtime_facts=False,
                support_subject="customer_problem",
                rationale=(
                    "Provider semantic intake completed; deterministic current-action-state "
                    "resolution requires the customer to choose one existing resource."
                ),
            )
        selected = cls._action_state_for_query(
            current_actions,
            action_state_query,
        )
        if selected is None:
            raise RuntimeConflict("selected conversation action state is unavailable")
        return Classification(
            issue_type=cls._action_issue_type(selected.action_type),
            risk="low",
            policy_boundary="allowed",
            requested_action="none",
            requested_concurrency_limit=None,
            needs_realtime_facts=False,
            support_subject="customer_problem",
            rationale=(
                "Provider semantic intake completed; deterministic current-action-state "
                "projection owns status, reason, and authority."
            ),
        )

    @classmethod
    def _resolve_existing_action_replay(
        cls,
        message: str,
        classification: Classification,
        current_actions: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Converge a repeated action onto current trusted state.

        The Provider still performs semantic intake, but cannot turn an already
        active or non-repeatable action into a misleading generic security
        refusal. Exact customer-scoped resource identity and the persisted
        action projection remain mandatory; this path never grants authority.
        """

        action_type = classification.requested_action
        if action_type == "none":
            # A safe Provider may classify an already executed, explicitly
            # repeated effect as ``none`` because no *new* action should run.
            # Recover only the two intrinsically non-repeatable action kinds
            # from the same deterministic parser used by ActionAdmission.
            # Entitlement changes are excluded because a new target on the
            # same subscription is not necessarily a replay.
            inferred_action = explicit_current_turn_action(message)
            action_type = (
                inferred_action if inferred_action in {"refund", "api_key_revocation"} else "none"
            )
        if action_type == "none" or not current_actions:
            return None
        projections = [ConversationActionStateV1.model_validate(item) for item in current_actions]
        matches = [
            item
            for item in projections
            if item.action_type == action_type
            and contains_exact_resource_reference(message, item.resource_id)
        ]
        if not matches:
            return None
        selected = max(matches, key=lambda item: item.updated_at)
        active_statuses = {
            "pending",
            "approved",
            "executing",
            "verification_pending",
        }
        reason_code: str | None = None
        if selected.projection_status in active_statuses:
            reason_code = "existing_action_in_progress"
        elif selected.projection_status == "executed" and action_type in {
            "refund",
            "api_key_revocation",
        }:
            reason_code = "executed_action_not_repeatable"
        elif (
            selected.projection_status == "executed"
            and action_type == "entitlement_change"
            and classification.requested_concurrency_limit is None
        ):
            reason_code = "executed_action_replay_without_new_target"
        if reason_code is None:
            return None
        return {
            "schema_version": "conversation-action-state-query.v1",
            "resolution": "selected",
            "approval_id": selected.approval_id,
            "query_kind": "repeat_request",
            "reason_code": reason_code,
            "grants_action_authority": False,
        }

    @staticmethod
    def _action_state_for_query(
        current_actions: list[dict[str, Any]],
        query: dict[str, Any],
    ) -> ConversationActionStateV1 | None:
        approval_id = str(query.get("approval_id", ""))
        for payload in current_actions:
            projection = ConversationActionStateV1.model_validate(payload)
            if projection.approval_id == approval_id:
                return projection
        return None

    @classmethod
    def _selected_action_state(
        cls,
        state: AgentState,
    ) -> ConversationActionStateV1 | None:
        query = state.get("action_state_query")
        if not query:
            return None
        return cls._action_state_for_query(
            state.get("current_actions", []),
            query,
        )

    @staticmethod
    def _public_action_option_label(option: dict[str, Any]) -> str:
        action_label = {
            "refund": "退款",
            "api_key_revocation": "API Key 撤销",
            "entitlement_change": "配额或套餐变更",
        }.get(str(option["action_type"]), "操作")
        resource_label = (
            "API Key（引用已隐藏）"
            if option.get("resource_reference_hidden") is True
            else str(option["resource_id"])
        )
        return f"{action_label} {resource_label}（{option['projection_status']}）"

    @classmethod
    def _trusted_action_state_answer(cls, state: AgentState) -> str | None:
        query = state.get("action_state_query")
        if query and query.get("resolution") == "unresolved":
            references = query.get("requested_resource_references")
            safe_references = (
                [item for item in references if isinstance(item, str) and 0 < len(item) <= 128]
                if isinstance(references, list)
                else []
            )
            subject = (
                f"资源引用 {'、'.join(safe_references)}" if safe_references else "你提到的资源"
            )
            if query.get("reason_code") == "resource_action_type_mismatch":
                return (
                    f"我在当前对话的申请记录中找到了{subject}，"
                    "但它的操作类型与你这次询问的操作不一致，因此不能把两者当成同一项申请。"
                    "本次没有创建审批，也没有执行任何变更。请确认正确的资源引用和操作类型后重试。"
                )
            if query.get("reason_code") == "action_referent_action_type_mismatch":
                return (
                    "你这次询问的操作类型与上一条申请状态更新不一致，因此我不能把它们当成同一项申请。"
                    "本次没有创建审批，也没有执行任何变更。"
                    "请补充完整资源引用，并确认要查询的操作类型。"
                )
            if query.get("reason_code") in {
                "action_referent_missing",
                "action_referent_not_in_current_state",
            }:
                return (
                    "我还不能确定你问的是哪一项申请，因为这条消息前没有可直接对应的申请状态更新。"
                    "本次没有创建审批，也没有执行任何变更。"
                    "请补充完整资源引用，并说明你要查询的是退款、API Key 撤销还是配额变更。"
                )
            return (
                f"我无法在当前对话的申请记录中找到与{subject}完全匹配的申请，"
                "因此不会用其他历史申请代替回答。本次没有创建审批，也没有执行任何变更。"
                "请核对完整资源引用，或说明你要查询的是退款、API Key 撤销还是配额变更。"
            )
        if query and query.get("resolution") == "ambiguous":
            options = query.get("candidate_options")
            safe_options = (
                [
                    item
                    for item in options
                    if isinstance(item, dict)
                    and isinstance(item.get("action_type"), str)
                    and isinstance(item.get("projection_status"), str)
                    and (
                        isinstance(item.get("resource_id"), str)
                        or (
                            item.get("action_type") == "api_key_revocation"
                            and item.get("resource_reference_hidden") is True
                        )
                    )
                ]
                if isinstance(options, list)
                else []
            )
            if not safe_options:
                return (
                    "我找到多项可能相关的历史申请，但当前问题没有唯一指向其中一项。"
                    "请补充对应的资源引用；在确认前不会创建审批或执行任何变更。"
                )
            option_text = "；".join(cls._public_action_option_label(item) for item in safe_options)
            return (
                f"我找到多项可能相关的申请：{option_text}。"
                "仅凭“这个/那项”无法安全确定你问的是哪一项，"
                "请回复其中一个完整资源引用。在确认前不会创建审批或执行任何变更。"
            )
        action = cls._selected_action_state(state)
        if action is None:
            return None
        label = {
            "refund": "退款申请",
            "api_key_revocation": "API Key 撤销申请",
            "entitlement_change": "配额或套餐变更申请",
        }[action.action_type]
        # Credential references are deliberately not echoed in customer-visible
        # prose. They may be sensitive even after revocation and the selected
        # projection already binds this answer to one canonical Approval.
        subject = (
            "当前对话中的 API Key 撤销申请"
            if action.action_type == "api_key_revocation"
            else f"资源 {action.resource_id} 的{label}"
        )
        if query and query.get("query_kind") == "reason":
            return cls._trusted_action_state_reason_answer(action, subject=subject)
        if query and query.get("query_kind") == "continuity":
            status_label = {
                "pending": "等待审批",
                "approved": "已批准、等待执行",
                "executing": "执行中",
                "verification_pending": "结果核验中",
                "executed": "已执行",
                "rejected": "已拒绝",
                "stale": "已失效",
                "withdrawn": "已撤回",
                "failed": "执行失败",
                "manual_takeover_legacy": "历史流程已停止",
            }[action.projection_status]
            return (
                "可以。你仍可继续查询账户状态，或咨询其他支持范围内的问题。"
                f"{subject}当前保持“{status_label}”；这条消息不会重新提交、"
                "重试或执行该操作。请告诉我你想查询的具体账户信息。"
            )
        return {
            "pending": (
                f"{subject}仍在等待独立审批，尚未执行任何业务变更。"
                "你可以继续咨询；如果不再需要处理，也可以撤回这项申请。"
            ),
            "approved": (
                f"{subject}已通过审批，正在等待系统执行；"
                "“已批准”不等于“已完成”。目前还不能确认业务变更已生效，"
                "请不要重复提交。"
            ),
            "executing": (
                f"{subject}已通过审批并正在安全执行，尚未到终态。"
                "请不要重复提交；稍后刷新当前对话查看最终结果。"
            ),
            "verification_pending": (
                f"{subject}的执行结果暂时还无法确认。"
                "系统仍锁定原申请并继续核验；当前不能断言已成功或失败，"
                "也不应重复提交。请稍后查看本对话的最终状态。"
            ),
            "executed": (
                f"{subject}已确认执行完成，业务变更已经生效。"
                "当前状态来自已落库的审批和业务动作记录，不需要再提交同一申请。"
            ),
            "rejected": (
                f"{subject}已被独立审批者拒绝，未执行任何业务变更。"
                "当前记录只确认“审批未通过”，不会展示审批者的内部备注；"
                "如仍需处理，请先确认该资源的当前事实，再提交新的明确请求。"
            ),
            "stale": (
                f"{subject}因资源事实或版本已经变化而失效，"
                "未执行本次业务变更。请先重新核验该资源的当前状态，"
                "再发起新的明确请求。"
            ),
            "withdrawn": (
                f"{subject}已由客户撤回，未执行任何业务变更。"
                "如果仍需要处理，请重新提交一条明确请求。"
            ),
            "failed": (
                f"{subject}已确认执行失败且未产生业务效果。"
                "你可以稍后重新提交；如果持续失败，请在新消息中带上该资源引用"
                "并说明当前期望。"
            ),
            "manual_takeover_legacy": (
                f"{subject}属于历史“转人工”记录。自动处理已经停止，"
                "但当前版本没有人工坐席收件、回复或完成闭环，"
                "因此不会声称有人正在处理。你仍可继续咨询产品问题，"
                "或基于当前资源事实发起新的受支持请求。"
            ),
        }[action.projection_status]

    @staticmethod
    def _trusted_action_state_reason_answer(
        action: ConversationActionStateV1,
        *,
        subject: str,
    ) -> str:
        """Explain only customer-safe structured reason classes.

        Human notes and internal failures are deliberately absent from
        ``ConversationActionStateV1``. When the projection has no more specific
        public reason class, say so instead of inventing a cause.
        """

        reason_code = action.customer_safe_reason_code
        decision_class = action.decision_class
        if reason_code == "approval_rejected_no_effect":
            decision = (
                "已被独立审批者拒绝" if decision_class == "reject" else "当前记录确认审批未通过"
            )
            effect = (
                "因此本次未撤销该 API Key，也未执行任何业务变更"
                if action.action_type == "api_key_revocation"
                else "因此未执行任何业务变更"
            )
            return (
                f"{subject}已结束：{decision}，{effect}。"
                "当前记录没有提供可以向你说明的更具体拒绝原因，"
                "也不会展示审批者的内部备注。若仍需处理，请先核验该资源的当前状态，"
                "再提交一条新的明确请求。"
            )
        if reason_code == "action_requires_fresh_verification":
            transition = (
                "系统在执行前重新校验时发现资源事实或版本已经变化"
                if decision_class == "system_transition"
                else "当前记录确认资源事实或版本已经变化"
            )
            return (
                f"{subject}已失效，因为{transition}，继续使用旧快照会有风险。"
                "本次没有执行任何业务变更。请先查询该资源的最新状态，"
                "再基于新事实发起明确请求。"
            )
        if reason_code == "action_failed_confirmed_no_effect":
            return (
                f"{subject}已确认失败，并且当前业务记录确认未产生业务效果。"
                "当前记录没有提供可以向你说明的更具体失败原因，"
                "因此我不会猜测内部异常或展示原始错误。请稍后使用同一资源引用重试；"
                "若资源状态已变化，请先重新核验。"
            )
        if reason_code == "approval_withdrawn_no_effect":
            return (
                f"{subject}是由客户撤回而结束的，不是执行失败；"
                "撤回后未执行任何业务变更。如仍需处理，请重新提交明确请求。"
            )
        if reason_code == "action_execution_verification_pending":
            return (
                f"{subject}目前无法给出成功或失败原因，因为执行结果仍在核验。"
                "系统会继续锁定原申请并阻止重复操作；请等待本对话更新最终状态。"
            )
        if reason_code == "legacy_manual_takeover_no_operator":
            return (
                f"{subject}来自历史“转人工”记录，但该记录没有当前可用的人工坐席闭环。"
                "自动处理已停止，也不会声称有人正在处理；你仍可以继续咨询支持范围内的问题。"
            )
        return (
            f"{subject}当前状态为 {action.projection_status}。"
            "当前记录没有可以向你说明的更具体原因，也不会展示内部备注或错误详情；"
            "你可以继续询问当前状态或补充该资源的最新事实。"
        )

    @classmethod
    def _action_state_contract_valid(
        cls,
        state: AgentState,
        candidate: CandidateResponse,
    ) -> bool:
        action = cls._selected_action_state(state)
        expected = cls._trusted_action_state_answer(state)
        query = state.get("action_state_query")
        non_selected = bool(query and query.get("resolution") in {"ambiguous", "unresolved"})
        return bool(
            (non_selected or (action is not None and action.grants_action_authority is False))
            and query is not None
            and query.get("grants_action_authority") is False
            and expected is not None
            and candidate.action == "answer"
            and candidate.answer == expected
            and not candidate.knowledge_chunk_ids
            and not candidate.knowledge_citations
            and not candidate.business_source_ids
            and not candidate.material_claims
            and not candidate.proposed_arguments
        )

    @staticmethod
    def _provider_component_manifest(
        assembled: Any,
        *,
        tools: list[dict[str, Any]],
        node: str,
    ) -> dict[str, Any]:
        manifest = assembled.manifest.model_dump(mode="json")
        return {
            **manifest,
            "node": node,
            "injected_tool_names": [
                str(item.get("function", {}).get("name", "")) for item in tools
            ],
            "injected_tool_schema_hash": canonical_json_hash(tools),
        }

    @staticmethod
    def _grounded_terminal_repair_eligibility(
        state: AgentState,
        *,
        evidence: list[dict[str, Any]],
        observations: list[dict[str, Any]],
    ) -> GroundedRepairEligibility:
        """Return a content-free, auditable decision for the answer-only repair."""

        return evaluate_grounded_repair_eligibility(
            obligation_synthesis_mode=bool(state.get("obligation_synthesis_mode")),
            admission_payload=state.get("action_admission"),
            evidence=evidence,
            observations=observations,
            knowledge_comparison_complete=bool(state.get("knowledge_comparison_complete", False)),
        )

    @staticmethod
    def _decision_error_paths(exc: Exception) -> list[str]:
        if isinstance(exc, ProviderStructuredOutputError):
            safe_paths: list[str] = []
            for path in exc.error_paths[:12]:
                safe_path = (
                    path
                    if isinstance(path, str)
                    and len(path) <= 200
                    and SAFE_STRUCTURED_ERROR_PATH.fullmatch(path) is not None
                    else "$:schema_error"
                )
                if safe_path not in safe_paths:
                    safe_paths.append(safe_path)
            return safe_paths or ["$:schema_error"]
        if isinstance(exc, ValidationError):
            return [
                f"{'.'.join(str(part) for part in item.get('loc', ())) or '$'}:"
                f"{item.get('type', 'schema_error')}"
                for item in exc.errors(
                    include_url=False, include_context=False, include_input=False
                )[:12]
            ]
        if isinstance(exc, json.JSONDecodeError):
            return ["$:json_decode"]
        return ["$:terminal_decision_invalid"]

    @staticmethod
    def _canonicalize_repair_extra_fields(
        exc: ProviderStructuredOutputError,
    ) -> AgentDecision | None:
        """Salvage a repair response only by deleting forbidden extra fields.

        The Provider has already spent the run's one structure-repair call.
        This method never fills, renames, coerces, or infers data: every
        reported error must be ``extra_forbidden``, every path must resolve to
        a dictionary member, and the remainder must pass the full
        AgentDecision schema. The raw payload remains ephemeral.
        """

        candidate = AgentRuntimeServices._prune_extra_forbidden_payload(exc)
        if candidate is None:
            return None
        try:
            return AgentDecision.model_validate(candidate)
        except ValidationError:
            return None

    @staticmethod
    def _canonicalize_bound_synthesis_extra_fields(
        exc: ProviderStructuredOutputError,
    ) -> ProviderBoundEvidenceSynthesis | None:
        """Delete only schema-proven extra fields from a bound synthesis response."""

        candidate = AgentRuntimeServices._prune_extra_forbidden_payload(exc)
        if candidate is None:
            return None
        try:
            return ProviderBoundEvidenceSynthesis.model_validate(candidate)
        except ValidationError:
            return None

    @staticmethod
    def _prune_extra_forbidden_payload(
        exc: ProviderStructuredOutputError,
    ) -> dict[str, Any] | None:
        """Return a copy with exact ``extra_forbidden`` paths removed, or fail closed."""

        if not isinstance(exc.parsed_payload, dict) or not exc.error_paths:
            return None
        if any(not path.endswith(":extra_forbidden") for path in exc.error_paths):
            return None
        candidate = deepcopy(exc.parsed_payload)
        for error_path in exc.error_paths:
            dotted_path, _, _error_type = error_path.rpartition(":")
            parts = dotted_path.split(".") if dotted_path not in {"", "$"} else []
            if not parts:
                return None
            parent: Any = candidate
            for part in parts[:-1]:
                if isinstance(parent, dict) and part in parent:
                    parent = parent[part]
                elif isinstance(parent, list) and part.isdigit():
                    index = int(part)
                    if index >= len(parent):
                        return None
                    parent = parent[index]
                else:
                    return None
            key = parts[-1]
            if not isinstance(parent, dict) or key not in parent:
                return None
            del parent[key]
        return candidate

    @staticmethod
    def _terminal_reference_error_paths(
        decision: AgentDecision,
        *,
        evidence: list[dict[str, Any]],
        observations: list[dict[str, Any]],
    ) -> list[str]:
        """Validate Provider-selected claim identities before Policy.

        Citation Binding IDs are attempt-local wire identities. A terminal
        candidate that omits support or copies an identity outside the current
        Provider context is structurally complete but cannot be published.
        Surface only stable field-level reason codes so the run's one bounded
        repair can select from the freshly assembled context without receiving
        raw Provider output or privileged lineage.
        """

        candidate = decision.candidate
        if candidate is None or candidate.action in {"reject", "manual_takeover", "escalate"}:
            return []
        allowed_bindings = {
            str(item["citation_binding_id"]) for item in evidence if item.get("citation_binding_id")
        }
        allowed_sources = {
            str(source["source_id"])
            for observation in observations
            if observation.get("tool_name") != "search_knowledge"
            and observation.get("status") == "ok"
            for source in observation.get("source_refs", [])
            if source.get("source_id")
        }
        errors: list[str] = []
        for index, claim in enumerate(candidate.material_claims):
            prefix = f"candidate.material_claims.{index}"
            if not claim.citation_binding_ids and not claim.observation_source_ids:
                errors.append(f"{prefix}:support_reference_required")
            if not set(claim.citation_binding_ids) <= allowed_bindings:
                errors.append(f"{prefix}.citation_binding_ids:unknown_context_binding")
            if not set(claim.observation_source_ids) <= allowed_sources:
                errors.append(f"{prefix}.observation_source_ids:unknown_business_source")
        return errors[:12]

    @staticmethod
    def _project_context_observation(observation: dict[str, Any]) -> dict[str, Any]:
        """Keep decision facts while leaving audit-only lineage in durable storage."""
        projected = {
            key: observation.get(key)
            for key in (
                "tool_name",
                "tool_call_id",
                "attempt_index",
                "status",
                "retryable",
                "error_code",
                "safe_error_summary",
                "observed_at",
                "freshness_class",
                "freshness_status",
                "fresh_until",
                "source_refs",
                "resource_version",
                "data",
                "trusted_retrieval_intent",
            )
        }
        if observation.get("tool_name") != "search_knowledge":
            return projected
        data = dict(observation.get("data", {}))
        evidence = list(data.pop("evidence", []))
        data["evidence_ids"] = [item.get("evidence_id") for item in evidence]
        projected["data"] = data
        # Knowledge is cited through the retrieved-evidence binding IDs.  Its
        # chunk refs are not current business-observation identities.
        projected["source_refs"] = []
        return projected

    @staticmethod
    def _project_context_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
        return project_context_evidence(
            evidence,
            projection_version=EVIDENCE_PROJECTION_V2,
        )

    async def _prepare_context_evidence_bindings(
        self,
        state: AgentState,
        evidence_lineage: list[dict[str, Any]],
        *,
        provider_attempt_id: str,
        context_ledger_id: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
        """Create wire IDs before the Provider call and verify their durable parents."""

        projected: list[dict[str, Any]] = []
        plans: list[dict[str, Any]] = []
        root_inputs: list[dict[str, Any]] = []
        for payload_ordinal, evidence in enumerate(evidence_lineage):
            binding_id = new_id("citation")
            membership_id = new_id("cmem")
            fragment = project_context_evidence(
                evidence,
                citation_binding_id=binding_id,
                projection_version=EVIDENCE_PROJECTION_V2,
            )
            fragment_hash = hashlib.sha256(
                json.dumps(
                    fragment,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            projected.append(fragment)
            root_inputs.append(
                {
                    "payload_ordinal": payload_ordinal,
                    "citation_binding_id": binding_id,
                    "fragment_hash": fragment_hash,
                }
            )
            if self.session is None or self.test_capability is not None:
                continue
            invocation_id = str(evidence.get("invocation_id") or "")
            observation_id = str(evidence.get("observation_id") or "")
            if not invocation_id or not observation_id:
                raise RuntimeConflict("citation_binding_incomplete")
            trace = await self.session.scalar(
                select(RetrievalTrace).where(
                    RetrievalTrace.tenant_id == state["tenant_id"],
                    RetrievalTrace.run_id == state["run_id"],
                    RetrievalTrace.logical_invocation_id == invocation_id,
                    RetrievalTrace.trace_status == "terminal_ok",
                )
            )
            observation = await self.session.get(ToolObservation, observation_id)
            if (
                trace is None
                or observation is None
                or observation.invocation_id != invocation_id
                or observation.status != "ok"
                or trace.origin_job_id is None
                or trace.origin_marker_id is None
                or trace.origin_fencing_token is None
                or trace.origin_segment_ref is None
            ):
                raise RuntimeConflict("citation_binding_incomplete")
            selected_ordinal = next(
                (
                    index
                    for index, candidate in enumerate(trace.selected_candidates)
                    if candidate.get("chunk_id") == evidence.get("chunk_id")
                    and candidate.get("evidence_group") == evidence.get("evidence_group")
                ),
                None,
            )
            locator_hash = str(evidence.get("source_locator", {}).get("locator_hash") or "")
            if selected_ordinal is None or len(locator_hash) != 64:
                raise RuntimeConflict("citation_binding_incomplete")
            matching_groups = [
                group
                for group in trace.evidence_groups
                if group.get("group") == (evidence.get("evidence_group") or "current")
                and any(
                    candidate.get("chunk_id") == evidence.get("chunk_id")
                    and candidate.get("locator_hash")
                    == trace.selected_candidates[selected_ordinal].get("locator_hash")
                    for candidate in group.get("selected_candidates", [])
                )
            ]
            if len(matching_groups) != 1 or not isinstance(matching_groups[0].get("filter"), dict):
                raise RuntimeConflict("citation_binding_incomplete")
            group_filter = dict(matching_groups[0]["filter"])
            temporal_selector = group_filter.get("temporal_selector")
            if not isinstance(temporal_selector, dict):
                raise RuntimeConflict("citation_binding_incomplete")
            plans.append(
                {
                    "membership_id": membership_id,
                    "citation_binding_id": binding_id,
                    "provider_attempt_id": provider_attempt_id,
                    "context_ledger_id": context_ledger_id,
                    "payload_ordinal": payload_ordinal,
                    "payload_json_pointer": f"/retrieved_evidence/{payload_ordinal}",
                    "fragment_hash": fragment_hash,
                    "origin_job_id": trace.origin_job_id,
                    "origin_marker_id": trace.origin_marker_id,
                    "origin_fencing_token": trace.origin_fencing_token,
                    "origin_segment_ref": trace.origin_segment_ref,
                    "logical_invocation_id": invocation_id,
                    "observation_id": observation_id,
                    "retrieval_trace_id": trace.id,
                    "selected_candidate_ordinal": selected_ordinal,
                    "locator_hash": locator_hash,
                    "temporal_selector": temporal_selector,
                }
            )
        root_hash = hashlib.sha256(
            json.dumps(root_inputs, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        for plan in plans:
            plan["ordered_membership_root_hash"] = root_hash
        return projected, plans, root_hash

    async def _prepare_context_observation_memberships(
        self,
        state: AgentState,
        observation_lineage: list[dict[str, Any]],
        context_observations: list[dict[str, Any]],
        *,
        provider_attempt_id: str,
        context_ledger_id: str,
        payload_ordinal_offset: int,
    ) -> tuple[list[dict[str, Any]], str]:
        """Bind every Provider-visible Observation fragment to its durable origin.

        Knowledge evidence has a separate CitationBinding contract.  Business
        and read-status Observations still need an immutable proof that the
        exact projected fragment was included in this Provider attempt.  The
        existing ContextMembership relation is deliberately generic enough to
        carry that proof without manufacturing a customer-visible citation.
        """

        if len(observation_lineage) != len(context_observations):
            raise RuntimeConflict("context_observation_binding_incomplete")
        plans: list[dict[str, Any]] = []
        root_inputs: list[dict[str, Any]] = []
        for index, (lineage, fragment) in enumerate(
            zip(observation_lineage, context_observations, strict=True)
        ):
            if fragment != self._project_context_observation(lineage):
                raise RuntimeConflict("context_observation_projection_changed")
            payload_ordinal = payload_ordinal_offset + index
            payload_json_pointer = f"/latest_observations/{index}"
            fragment_hash = canonical_json_hash(fragment)
            root_inputs.append(
                {
                    "payload_ordinal": payload_ordinal,
                    "payload_json_pointer": payload_json_pointer,
                    "fragment_hash": fragment_hash,
                }
            )
            if self.session is None or self.test_capability is not None:
                continue
            invocation_id = str(lineage.get("invocation_id") or "")
            observation_id = str(lineage.get("observation_id") or "")
            if not invocation_id or not observation_id:
                raise RuntimeConflict("context_observation_binding_incomplete")
            observation = await self.session.get(ToolObservation, observation_id)
            invocation = await self.session.get(ToolInvocation, invocation_id)
            persisted_payload = observation.payload if observation is not None else {}
            if (
                observation is None
                or invocation is None
                or observation.tenant_id != state["tenant_id"]
                or observation.run_id != state["run_id"]
                or observation.invocation_id != invocation_id
                or invocation.tenant_id != state["tenant_id"]
                or invocation.run_id != state["run_id"]
                or invocation.id != observation.invocation_id
                or invocation.job_id != observation.job_id
                or invocation.segment_id != observation.segment_id
                or invocation.fencing_token != observation.fencing_token
                or observation.status != lineage.get("status")
                or len(observation.content_hash) != 64
                or lineage.get("observation_content_hash") != observation.content_hash
                or not isinstance(persisted_payload, dict)
                or any(lineage.get(key) != value for key, value in persisted_payload.items())
            ):
                raise RuntimeConflict("context_observation_binding_incomplete")
            plans.append(
                {
                    "membership_kind": "observation",
                    "membership_id": new_id("cmem"),
                    "provider_attempt_id": provider_attempt_id,
                    "context_ledger_id": context_ledger_id,
                    "payload_ordinal": payload_ordinal,
                    "payload_json_pointer": payload_json_pointer,
                    "fragment_hash": fragment_hash,
                    "origin_job_id": observation.job_id,
                    "origin_marker_id": observation.segment_id,
                    "origin_fencing_token": observation.fencing_token,
                    "origin_segment_ref": observation.segment_id,
                    "logical_invocation_id": invocation_id,
                    "schema_version": "context-membership.v2",
                }
            )
        root_hash = canonical_json_hash(root_inputs)
        for plan in plans:
            plan["ordered_membership_root_hash"] = root_hash
        return plans, root_hash
