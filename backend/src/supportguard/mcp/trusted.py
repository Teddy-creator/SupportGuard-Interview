from datetime import UTC, datetime, timedelta

from supportguard.contracts.context import WorkerExecutionContext
from supportguard.contracts.tools import ToolCallContext


def mcp_worker_context(
    context: ToolCallContext, *, executor_service_principal: str
) -> WorkerExecutionContext:
    if context.mcp_context is None:
        from supportguard.config import get_settings

        if get_settings().app_env != "test":
            raise RuntimeError("typed MCP call context is required")
        deadline = datetime.now(UTC) + timedelta(seconds=10)
    else:
        deadline = context.mcp_context.call_deadline
        if deadline <= datetime.now(UTC):
            raise RuntimeError("MCP call deadline has expired")
    return WorkerExecutionContext(
        tenant_id=context.tenant_id,
        actor_principal_id=context.customer_id,
        executor_service_principal=executor_service_principal,
        customer_id=context.customer_id,
        ticket_id=context.ticket_id,
        run_id=context.run_id,
        job_id=context.job_id,
        segment_id=context.segment_id,
        delivery_generation=context.delivery_generation,
        fencing_token=context.fencing_token,
        trace_id=context.trace_id,
        deadline=deadline,
    )
