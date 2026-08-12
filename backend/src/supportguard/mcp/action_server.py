from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, TypeAdapter
from sqlalchemy.engine import make_url

from supportguard.config import get_settings
from supportguard.contracts.context import McpCallContext, PolicyCapabilityMcpCallContext
from supportguard.contracts.testing import issue_test_runtime_capability
from supportguard.contracts.tools import (
    ApiKeyRevocationProposalInput,
    DraftProposalResult,
    EntitlementChangeProposalInput,
    RefundProposalInput,
    ToolCallContext,
)
from supportguard.db.session import (
    ScopedSessionFactory,
    create_engine,
    create_scoped_session_factory,
    create_session_factory,
)
from supportguard.mcp.process import register_managed_process
from supportguard.mcp.trusted import mcp_worker_context
from supportguard.services.business import BusinessService
from supportguard.services.errors import DomainError, observation_status_for_error
from supportguard.services.schema_rollout import require_current_writer_contract

_factory: ScopedSessionFactory | None = None
_MCP_CONTEXT_ADAPTER: TypeAdapter[McpCallContext] = TypeAdapter(McpCallContext)


@asynccontextmanager
async def server_lifespan(_: FastMCP) -> AsyncIterator[None]:
    global _factory
    settings = get_settings()
    engine = create_engine(
        settings.model_copy(
            update={"database_url": settings.mcp_action_database_url or settings.database_url}
        )
    )
    try:
        await require_current_writer_contract(
            create_session_factory(engine),
            service="action_mcp",
        )
        _factory = create_scoped_session_factory(engine)
        yield
    finally:
        _factory = None
        await engine.dispose()


mcp = FastMCP(
    "support-action-mcp",
    instructions=(
        "Scoped refund, API Key revocation, and entitlement proposals only. "
        "No runtime execution capability."
    ),
    log_level="ERROR",
    lifespan=server_lifespan,
)


def _context(
    customer_id: str,
    ticket_id: str,
    run_id: str,
    checkpoint_id: str | None,
    tool_call_id: str,
    trace_id: str,
    tenant_id: str,
    job_id: str,
    fencing_token: int,
    segment_id: str,
    delivery_generation: int,
    observation_binding: list[dict[str, object]] | None = None,
    mcp_context: dict[str, object] | None = None,
) -> ToolCallContext:
    parsed = _MCP_CONTEXT_ADAPTER.validate_python(mcp_context) if mcp_context else None
    if parsed is not None and not isinstance(parsed, PolicyCapabilityMcpCallContext):
        raise ValueError("action MCP requires a policy capability context")
    if parsed is None and get_settings().app_env != "test":
        raise ValueError("action MCP requires a typed call context")
    return ToolCallContext(
        customer_id=customer_id,
        tenant_id=tenant_id,
        ticket_id=ticket_id,
        run_id=run_id,
        job_id=job_id,
        segment_id=segment_id,
        delivery_generation=delivery_generation,
        fencing_token=fencing_token,
        checkpoint_id=checkpoint_id,
        observation_binding=observation_binding or [],
        tool_call_id=tool_call_id,
        trace_id=trace_id,
        mcp_context=parsed,
    )


async def _invoke(
    method: str,
    context: ToolCallContext,
    arguments: (
        RefundProposalInput | ApiKeyRevocationProposalInput | EntitlementChangeProposalInput
    ),
) -> dict[str, object]:
    if _factory is None:
        raise RuntimeError("action MCP lifespan is not initialized")
    if get_settings().app_env != "test" and (
        context.job_id is None or context.fencing_token is None
    ):
        raise RuntimeError("action MCP requires a fenced RuntimeJob context")
    execution = mcp_worker_context(context, executor_service_principal="support-action-mcp")
    async with _factory.worker(execution) as session:
        try:
            settings = get_settings()
            database_url = settings.mcp_action_database_url or settings.database_url
            restricted_login = make_url(database_url).username == "supportguard_action_mcp"
            test_capability = (
                issue_test_runtime_capability(testing=True)
                if settings.app_env == "test" and not restricted_login
                else None
            )
            service = BusinessService(session, test_capability=test_capability)
            await service.consume_mcp_reservation(
                context,
                method=method,
                model_arguments=arguments.model_dump(mode="json"),
            )
            result: BaseModel
            if restricted_login:
                payload = await service.execute_mcp_tool(
                    context,
                    method=method,
                    model_arguments=arguments.model_dump(mode="json"),
                    execution_payload={
                        "customer_id": context.customer_id,
                        "ticket_id": context.ticket_id,
                        "tool_call_id": context.tool_call_id,
                        "observation_binding": context.observation_binding,
                        "causal_decision_schema_version": (
                            context.mcp_context.causal_decision_schema_version
                            if isinstance(context.mcp_context, PolicyCapabilityMcpCallContext)
                            else None
                        ),
                        "causal_decision": (
                            context.mcp_context.causal_decision.model_dump(mode="json")
                            if isinstance(context.mcp_context, PolicyCapabilityMcpCallContext)
                            else None
                        ),
                    },
                )
                result = DraftProposalResult.model_validate(payload)
            elif method == "propose_refund" and isinstance(arguments, RefundProposalInput):
                if test_capability is not None:
                    result = await service.propose_refund(context, arguments)
                elif context.job_id is None or context.fencing_token is None:
                    if get_settings().app_env != "test":
                        raise RuntimeError("action MCP requires a fenced RuntimeJob context")
                    result = await service.propose_refund(context, arguments)
                else:
                    result = await service.propose_refund_draft(context, arguments)
            elif method == "propose_api_key_revocation" and isinstance(
                arguments, ApiKeyRevocationProposalInput
            ):
                result = await service.propose_api_key_revocation_draft(context, arguments)
            elif method == "propose_entitlement_change" and isinstance(
                arguments, EntitlementChangeProposalInput
            ):
                result = await service.propose_entitlement_change_draft(context, arguments)
            else:
                raise ValueError("unsupported action method")
            if test_capability is None:
                await service.record_capability_effect(
                    context,
                    payload=result.model_dump(mode="json"),
                )
            await session.commit()
        except DomainError as exc:
            await session.rollback()
            error_payload: dict[str, object] = {
                "domain_error": True,
                "status": observation_status_for_error(exc.code),
                "error_code": exc.code.value,
                "safe_error_summary": exc.message,
            }
            boundary_reason = exc.details.get("boundary_reason")
            if boundary_reason and exc.details.get("sqlstate") in {
                "22023",
                "42501",
                "55000",
            }:
                error_payload["internal_boundary_reason"] = boundary_reason
            return error_payload
    return result.model_dump(mode="json")


@mcp.tool()
async def propose_refund(
    billing_record_id: str,
    refund_reason: str,
    idempotency_key: str,
    customer_id: str,
    ticket_id: str,
    run_id: str,
    tool_call_id: str,
    trace_id: str,
    tenant_id: str,
    job_id: str,
    fencing_token: int,
    segment_id: str,
    delivery_generation: int,
    checkpoint_id: str | None = None,
    observation_binding: list[dict[str, object]] | None = None,
    mcp_context: dict[str, object] | None = None,
) -> dict[str, object]:
    """Create a pending refund proposal; this never executes a refund."""
    return await _invoke(
        "propose_refund",
        _context(
            customer_id,
            ticket_id,
            run_id,
            checkpoint_id,
            tool_call_id,
            trace_id,
            tenant_id,
            job_id,
            fencing_token,
            segment_id,
            delivery_generation,
            observation_binding,
            mcp_context,
        ),
        RefundProposalInput(
            billing_record_id=billing_record_id,
            refund_reason=refund_reason,
            idempotency_key=idempotency_key,
        ),
    )


@mcp.tool()
async def propose_api_key_revocation(
    api_key_id: str,
    reason: str,
    idempotency_key: str,
    customer_id: str,
    ticket_id: str,
    run_id: str,
    tool_call_id: str,
    trace_id: str,
    tenant_id: str,
    job_id: str,
    fencing_token: int,
    segment_id: str,
    delivery_generation: int,
    checkpoint_id: str | None = None,
    observation_binding: list[dict[str, object]] | None = None,
    mcp_context: dict[str, object] | None = None,
) -> dict[str, object]:
    """Create an inert API Key revocation draft; never revoke a Key."""
    return await _invoke(
        "propose_api_key_revocation",
        _context(
            customer_id,
            ticket_id,
            run_id,
            checkpoint_id,
            tool_call_id,
            trace_id,
            tenant_id,
            job_id,
            fencing_token,
            segment_id,
            delivery_generation,
            observation_binding,
            mcp_context,
        ),
        ApiKeyRevocationProposalInput(
            api_key_id=api_key_id, reason=reason, idempotency_key=idempotency_key
        ),
    )


@mcp.tool()
async def propose_entitlement_change(
    subscription_id: str,
    change_type: str,
    target: dict[str, object],
    reason: str,
    idempotency_key: str,
    customer_id: str,
    ticket_id: str,
    run_id: str,
    tool_call_id: str,
    trace_id: str,
    tenant_id: str,
    job_id: str,
    fencing_token: int,
    segment_id: str,
    delivery_generation: int,
    checkpoint_id: str | None = None,
    observation_binding: list[dict[str, object]] | None = None,
    mcp_context: dict[str, object] | None = None,
) -> dict[str, object]:
    """Create an inert entitlement change draft; never change a subscription."""
    return await _invoke(
        "propose_entitlement_change",
        _context(
            customer_id,
            ticket_id,
            run_id,
            checkpoint_id,
            tool_call_id,
            trace_id,
            tenant_id,
            job_id,
            fencing_token,
            segment_id,
            delivery_generation,
            observation_binding,
            mcp_context,
        ),
        EntitlementChangeProposalInput(
            subscription_id=subscription_id,
            change_type=change_type,
            target=target,
            reason=reason,
            idempotency_key=idempotency_key,
        ),
    )


def main() -> None:
    register_managed_process()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
