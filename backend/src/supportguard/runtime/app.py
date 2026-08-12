from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from supportguard.agent.graph import AgentState, SupportGraph
from supportguard.approvals.coordinator import ApprovalCoordinator
from supportguard.config import Settings
from supportguard.contracts.context import WorkerExecutionContext, worker_execution_context
from supportguard.contracts.testing import TestRuntimeCapability
from supportguard.db.models import ApprovalRequest
from supportguard.db.session import ScopedSessionFactory, create_scoped_session_factory
from supportguard.mcp.runtime import ToolTransport
from supportguard.memory.service import MemoryHistoryLoader, MemoryWriter
from supportguard.observability.metrics import GRAPH_RUNS
from supportguard.providers.base import StructuredProvider
from supportguard.rag.embeddings import EmbeddingProvider
from supportguard.rag.repository import KnowledgeRepository
from supportguard.rag.service import RetrievalService
from supportguard.tools.gateway import ToolGateway


class AppRuntime:
    def __init__(
        self,
        *,
        engine: AsyncEngine,
        factory: async_sessionmaker[AsyncSession],
        checkpointer: Any,
        provider: StructuredProvider,
        embedding: EmbeddingProvider,
        settings: Settings | None = None,
        mcp_manager: ToolTransport | None = None,
        test_capability: TestRuntimeCapability | None = None,
    ) -> None:
        self.engine = engine
        self.factory = factory
        self.settings = settings or Settings(
            _env_file=None,
            app_env="test",
            code_version="development",
        )
        self.scoped_factory: ScopedSessionFactory = create_scoped_session_factory(
            engine,
            settings=self.settings,
        )
        self.checkpointer = checkpointer
        self.provider = provider
        self.embedding = embedding
        self.mcp_manager = mcp_manager
        self.test_capability = test_capability

    def _graph(self, session: AsyncSession) -> SupportGraph:
        if self.mcp_manager is None:
            raise RuntimeError("Agent Graph requires an explicitly injected MCP transport")
        return SupportGraph(
            provider=self.provider,
            retrieval=RetrievalService(KnowledgeRepository(session), self.embedding),
            gateway=ToolGateway(self.mcp_manager, test_capability=self.test_capability),
            approval_handler=ApprovalCoordinator(
                self.scoped_factory,
                test_capability=self.test_capability,
            ),
            checkpointer=self.checkpointer,
            memory_writer=MemoryWriter(self.scoped_factory),
            history_loader=MemoryHistoryLoader(self.scoped_factory),
            session=session,
            test_capability=self.test_capability,
            settings=self.settings,
        )

    async def run_ticket(
        self,
        state: AgentState,
        *,
        execution_context: WorkerExecutionContext,
        checkpoint_ns: str = "",
    ) -> AgentState:
        thread_id = checkpoint_ns or state["run_id"]
        config: RunnableConfig = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": "",
            }
        }
        self._validate_execution_state(state, execution_context)
        with worker_execution_context.bind(execution_context):
            async with self.scoped_factory.worker(execution_context) as session:
                graph = self._graph(session)
                output = await graph.run(state, config)
                output["segment_events"] = graph.segment_events
        GRAPH_RUNS.labels(result=str(output.get("policy_route", "completed"))).inc()
        return output

    async def resume_durable_tool_turn(
        self,
        *,
        checkpoint_ns: str,
        execution_context: WorkerExecutionContext,
    ) -> AgentState:
        """Continue the checkpointed tool node without issuing another Provider decision."""

        config: RunnableConfig = {"configurable": {"thread_id": checkpoint_ns, "checkpoint_ns": ""}}
        if await self.checkpointer.aget_tuple(config) is None:
            raise RuntimeError("durable tool turn checkpoint is unavailable")
        with worker_execution_context.bind(execution_context):
            async with self.scoped_factory.worker(execution_context) as session:
                graph = self._graph(session)
                output = cast(
                    AgentState,
                    await graph.compiled.ainvoke(
                        Command[Any](
                            update={
                                "job_id": execution_context.job_id,
                                "segment_id": execution_context.segment_id,
                                "delivery_generation": execution_context.delivery_generation,
                                "fencing_token": execution_context.fencing_token,
                                "trace_id": execution_context.trace_id,
                            }
                        ),
                        config,
                    ),
                )
                output["segment_events"] = graph.segment_events
        GRAPH_RUNS.labels(result="tool_turn_resumed").inc()
        return output

    async def fork_checkpoint(
        self,
        *,
        source_namespace: str,
        source_checkpoint_id: str,
        target_namespace: str,
    ) -> str:
        source_config: RunnableConfig = {
            "configurable": {
                "thread_id": source_namespace,
                "checkpoint_ns": "",
                "checkpoint_id": source_checkpoint_id,
            }
        }
        source = await self.checkpointer.aget_tuple(source_config)
        if source is None:
            raise RuntimeError("canonical checkpoint is unavailable for resume")
        checkpoint = dict(source.checkpoint)
        checkpoint["id"] = str(uuid4())
        metadata = dict(source.metadata)
        metadata.update(
            {
                "source": "fork",
                "canonical_parent": source_checkpoint_id,
                "private_namespace": target_namespace,
            }
        )
        target_config: RunnableConfig = {
            "configurable": {"thread_id": target_namespace, "checkpoint_ns": ""}
        }
        written = await self.checkpointer.aput(
            target_config,
            cast(Any, checkpoint),
            cast(Any, metadata),
            cast(Any, checkpoint.get("channel_versions", {})),
        )
        for task_id, channel, value in source.pending_writes or []:
            await self.checkpointer.aput_writes(
                written,
                [(channel, value)],
                task_id,
            )
        return str(written["configurable"]["checkpoint_id"])

    async def resume_ticket(
        self,
        *,
        run_id: str,
        approval_id: str,
        decision: dict[str, Any],
        execution_context: WorkerExecutionContext | None = None,
        checkpoint_ns: str = "",
    ) -> AgentState:
        config: RunnableConfig = {
            "configurable": {
                "thread_id": checkpoint_ns or run_id,
                "checkpoint_ns": "",
            }
        }
        decision = {**decision, "approval_id": approval_id, "run_id": run_id}
        if execution_context is None:
            if self.test_capability is None:
                raise RuntimeError(
                    "fixture resume context is unavailable outside an injected test runtime"
                )
            async with self.factory() as lookup:
                approval = await lookup.get(ApprovalRequest, approval_id)
                if approval is None or approval.run_id != run_id:
                    raise RuntimeError("fixture resume approval binding is unavailable")
                execution_context = WorkerExecutionContext(
                    tenant_id=approval.tenant_id,
                    actor_principal_id=approval.customer_id,
                    executor_service_principal="test-runtime",
                    customer_id=approval.customer_id,
                    ticket_id=approval.ticket_id,
                    run_id=run_id,
                    job_id="fixture_job",
                    segment_id="test-fixture-segment",
                    delivery_generation=1,
                    fencing_token=1,
                    trace_id=f"fixture:{approval_id}",
                    deadline=datetime.now(UTC) + timedelta(minutes=1),
                )
        if execution_context.run_id != run_id:
            raise RuntimeError("trusted resume run mismatch")
        with worker_execution_context.bind(execution_context):
            async with self.scoped_factory.worker(execution_context) as session:
                graph = self._graph(session)
                output = cast(
                    AgentState,
                    await graph.compiled.ainvoke(Command[Any](resume=decision), config),
                )
                output["segment_events"] = graph.segment_events
        GRAPH_RUNS.labels(result="resumed").inc()
        return output

    def _validate_execution_state(self, state: AgentState, context: WorkerExecutionContext) -> None:
        expected: dict[str, str | int] = {
            "tenant_id": context.tenant_id,
            "customer_id": context.customer_id,
            "ticket_id": context.ticket_id,
            "run_id": context.run_id,
        }
        if self.test_capability is None:
            expected.update(
                {
                    "job_id": context.job_id,
                    "fencing_token": context.fencing_token,
                }
            )
        for field, value in expected.items():
            if state.get(field) != value:
                raise RuntimeError(f"trusted worker context mismatch: {field}")
