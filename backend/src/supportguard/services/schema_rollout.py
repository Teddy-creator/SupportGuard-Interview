from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.exc import (
    DBAPIError,
    DisconnectionError,
    InterfaceError,
    OperationalError,
)
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from supportguard.db.reference_contract import CURRENT_PRODUCT_DATABASE_HEAD
from supportguard.db.security_contract import LEGACY_FINAL_DATABASE_HEAD

LEGACY_READER_HEAD = "b179c0a1d001"
EXPAND_HEAD = "b180c0a1d001"
BACKFILL_HEAD = "b181c0a1d001"
CONTRACT_HEAD = "b182c0a1d001"
POST_CONTRACT_HEADS = (
    "b183c0a1d001",
    "b184c0a1d001",
    "b185c0a1d001",
    "b186c0a1d001",
    "b187c0a1d001",
    "b188c0a1d001",
    "b189c0a1d001",
    "b190c0a1d001",
    "b191c0a1d001",
    "b192c0a1d001",
    "b193c0a1d001",
    "b194c0a1d001",
    "b195c0a1d001",
    "b196c0a1d001",
    "b197c0a1d001",
    "b198c0a1d001",
    "b199c0a1d001",
    "b200c0a1d001",
    "b201c0a1d001",
    "b202c0a1d001",
    "b203c0a1d001",
    "b204c0a1d001",
    "b205c0a1d001",
    "b206c0a1d001",
    "b207c0a1d001",
)

READER_CONTRACT = "interview-baseline-reader.v1"

WriterContract = Literal[
    "legacy",
    "expand-dual-write",
    "backfill-read-only",
    "contract",
    "unsupported",
]
WriterService = Literal["worker", "dispatcher", "reconciler", "action_mcp"]
RuntimeSchemaService = Literal["api", "read_mcp"]
SchemaCapabilityService = Literal[
    "worker",
    "dispatcher",
    "reconciler",
    "read_mcp",
    "action_mcp",
]
SchemaProbeFailure = Literal["transient", "configuration"]
DatabaseIdentity = Literal["interview_baseline", "legacy_final", "legacy_history", "unknown"]

_TRANSIENT_SQLSTATES = {
    "40001",
    "40P01",
    "53300",
    "53400",
    "57014",
    "57P01",
    "57P02",
    "57P03",
}
_CONFIGURATION_SQLSTATES = {
    "28P01",
    "3D000",
    "3F000",
    "42501",
    "42703",
    "42883",
    "42P01",
    "55000",
}


class WriterContractUnavailable(RuntimeError):
    """The current writer binary must not run against this schema generation."""

    def __init__(self, *, service: WriterService, snapshot: SchemaRolloutSnapshot) -> None:
        self.service = service
        self.snapshot = snapshot
        super().__init__(
            "writer_contract_unavailable:"
            f"service={service}:database_head={snapshot.database_head or 'unknown'}:"
            f"required={CURRENT_PRODUCT_DATABASE_HEAD}"
        )


class RuntimeSchemaUnavailable(RuntimeError):
    """A serving process must not initialize against a non-current schema."""

    def __init__(
        self,
        *,
        service: RuntimeSchemaService,
        snapshot: SchemaRolloutSnapshot,
    ) -> None:
        self.service = service
        self.snapshot = snapshot
        super().__init__(
            "runtime_schema_unavailable:"
            f"service={service}:database_head={snapshot.database_head or 'unknown'}:"
            f"required={CURRENT_PRODUCT_DATABASE_HEAD}"
        )


@dataclass(frozen=True, slots=True)
class SchemaRolloutSnapshot:
    database_head: str
    database_identity: DatabaseIdentity
    reader_contract: str
    reader_compatible: bool
    writer_contract: WriterContract
    writer_contract_generation: int
    writer_enabled: bool

    @property
    def serving_mode(self) -> Literal["full", "read_only", "unavailable"]:
        if self.current_writer_compatible:
            return "full"
        if self.reader_compatible:
            return "read_only"
        return "unavailable"

    @property
    def current_writer_compatible(self) -> bool:
        """Bind the current Binary to its exact database head."""

        return (
            self.writer_enabled
            and self.database_identity == "interview_baseline"
            and self.database_head == CURRENT_PRODUCT_DATABASE_HEAD
        )


_LEGACY_HEADS = frozenset(
    (LEGACY_READER_HEAD, EXPAND_HEAD, BACKFILL_HEAD, CONTRACT_HEAD, *POST_CONTRACT_HEADS)
)


def schema_rollout_for_head(database_head: str) -> SchemaRolloutSnapshot:
    if database_head == CURRENT_PRODUCT_DATABASE_HEAD:
        database_identity: DatabaseIdentity = "interview_baseline"
        writer_contract: WriterContract = "contract"
        generation = 3
        reader_compatible = True
        writer_enabled = True
    elif database_head == LEGACY_FINAL_DATABASE_HEAD:
        database_identity = "legacy_final"
        writer_contract = "unsupported"
        generation = -1
        reader_compatible = False
        writer_enabled = False
    elif database_head in _LEGACY_HEADS:
        database_identity = "legacy_history"
        writer_contract = "unsupported"
        generation = -1
        reader_compatible = False
        writer_enabled = False
    else:
        database_identity = "unknown"
        writer_contract = "unsupported"
        generation = -1
        reader_compatible = False
        writer_enabled = False
    return SchemaRolloutSnapshot(
        database_head=database_head,
        database_identity=database_identity,
        reader_contract=READER_CONTRACT,
        reader_compatible=reader_compatible,
        writer_contract=writer_contract,
        writer_contract_generation=generation,
        writer_enabled=writer_enabled,
    )


def schema_rollout_for_revisions(revisions: object) -> SchemaRolloutSnapshot:
    """Classify a direct version-table result with exact-one-row semantics."""

    if not isinstance(revisions, (list, tuple)) or len(revisions) != 1:
        return schema_rollout_for_head("")
    revision = revisions[0]
    if not isinstance(revision, str) or not revision:
        return schema_rollout_for_head("")
    return schema_rollout_for_head(revision)


def schema_rollout_from_capability(value: object) -> SchemaRolloutSnapshot:
    """Validate one bounded database capability payload.

    The existing database functions expose the exact single revision through
    ``migration_head``.  Runtime derives schema identity from that revision so
    the independent baseline does not introduce catalog drift in those
    functions.  Missing, malformed, legacy, and unknown values fail closed.
    """

    if not isinstance(value, dict):
        return schema_rollout_for_head("")
    database_head = value.get("migration_head")
    if not isinstance(database_head, str) or not database_head:
        return schema_rollout_for_head("")
    return schema_rollout_for_head(database_head)


def classify_schema_probe_failure(exc: BaseException) -> SchemaProbeFailure | None:
    """Classify only failures that the HTTP boundary may safely fail closed.

    Unknown DBAPI states and application exceptions remain programmer/internal
    errors; callers must re-raise them instead of relabeling them as rollout.
    """

    sqlstate = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    if isinstance(exc, DBAPIError):
        sqlstate = (
            sqlstate
            or getattr(exc.orig, "sqlstate", None)
            or getattr(
                exc.orig,
                "pgcode",
                None,
            )
        )
    if isinstance(sqlstate, str):
        if sqlstate.startswith("08") or sqlstate in _TRANSIENT_SQLSTATES:
            return "transient"
        if sqlstate in _CONFIGURATION_SQLSTATES:
            return "configuration"
        return None
    if isinstance(exc, DBAPIError):
        if exc.connection_invalidated or isinstance(
            exc,
            (InterfaceError, OperationalError),
        ):
            return "transient"
        return None
    if isinstance(exc, (DisconnectionError, SQLAlchemyTimeoutError, OSError)):
        return "transient"
    return None


async def inspect_schema_rollout(session: AsyncSession) -> SchemaRolloutSnapshot:
    """Read the actual database head without selecting migration tables directly.

    The API role's bounded runtime snapshot is available on b179, so the
    compatible Reader can probe the rollout generation before any v1.5.12
    column exists. Writer roles use their heartbeat capability instead.
    """

    if session.get_bind().dialect.name != "postgresql":
        # SQLite is a deterministic fixture created from current ORM metadata, so
        # its honest schema identity is the current product head.  This identity
        # does not prove that PostgreSQL-only functions, grants, triggers, or RLS
        # exist; health exposes that distinction explicitly.
        return schema_rollout_for_head(CURRENT_PRODUCT_DATABASE_HEAD)
    session_user = await session.scalar(text("SELECT session_user"))
    if session_user != "supportguard_api":
        raise RuntimeError("schema rollout probe requires the API snapshot capability")
    value = await session.scalar(text("SELECT supportguard_api_runtime_snapshot()"))
    return schema_rollout_from_capability(value)


async def inspect_writer_schema_rollout(
    session: AsyncSession,
    *,
    service: SchemaCapabilityService,
) -> SchemaRolloutSnapshot:
    """Read the actual head through each writer role's bounded heartbeat API.

    ``__healthcheck__`` is a read-only branch of the capability: it reports the
    migration head without creating or updating a heartbeat row.
    """

    if session.get_bind().dialect.name != "postgresql":
        # Current-metadata fixture identity only; never treat this branch as
        # PostgreSQL catalog/capability evidence.
        return schema_rollout_for_head(CURRENT_PRODUCT_DATABASE_HEAD)
    value = await session.scalar(
        text(
            "SELECT supportguard_record_service_heartbeat(:instance_id,:service,'__healthcheck__')"
        ),
        {
            "instance_id": f"schema-rollout-preflight:{service}",
            "service": service,
        },
    )
    return schema_rollout_from_capability(value)


async def require_current_runtime_schema(
    factory: async_sessionmaker[AsyncSession],
    *,
    service: RuntimeSchemaService,
    current_metadata_fixture: bool = False,
) -> SchemaRolloutSnapshot:
    """Fence API and Read MCP before they expose any serving capability.

    Production identities prove the schema through their least-privilege
    SECURITY DEFINER capability.  SQLite has no PostgreSQL capability surface;
    callers must explicitly identify a current-metadata test fixture instead of
    receiving an implicit compatibility exemption.

    This Runtime fence validates the resulting revision identity.  Physical
    ``alembic_version`` exact-one cardinality remains owned by the mandatory
    administrator/migrator baseline preflight that precedes current services;
    the bounded Runtime capability intentionally does not expose catalog rows.
    """

    async with factory() as session:
        if session.get_bind().dialect.name != "postgresql" and not current_metadata_fixture:
            snapshot = schema_rollout_for_head("")
        elif service == "api":
            snapshot = await inspect_schema_rollout(session)
        else:
            snapshot = await inspect_writer_schema_rollout(session, service="read_mcp")
    if not snapshot.current_writer_compatible:
        raise RuntimeSchemaUnavailable(service=service, snapshot=snapshot)
    return snapshot


async def require_current_writer_contract(
    factory: async_sessionmaker[AsyncSession],
    *,
    service: WriterService,
) -> SchemaRolloutSnapshot:
    """Fail closed before a current writer starts any provider or work loop."""

    async with factory() as session:
        snapshot = await inspect_writer_schema_rollout(session, service=service)
    if not snapshot.current_writer_compatible:
        raise WriterContractUnavailable(service=service, snapshot=snapshot)
    return snapshot


def reference_head_is_contract() -> bool:
    """Make a stale application/reference pairing observable in tests and health."""

    current = schema_rollout_for_head(CURRENT_PRODUCT_DATABASE_HEAD)
    return current.writer_contract == "contract" and current.writer_enabled


def upgrade_unavailable_payload() -> dict[str, object]:
    return {
        "schema_version": "upgrade-unavailable.v1",
        "error_code": "upgrade_in_progress",
        "status": "unavailable",
        "retryable": True,
    }


def schema_probe_unavailable_payload(
    *,
    failure: SchemaProbeFailure = "transient",
) -> dict[str, object]:
    return {
        "schema_version": "schema-probe-unavailable.v1",
        "error_code": (
            "dependency_unavailable" if failure == "transient" else "schema_probe_misconfigured"
        ),
        "status": "unavailable",
        "retryable": failure == "transient",
    }
