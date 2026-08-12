"""Fail-closed owner for the Interview Edition empty-database baseline."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, cast

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text

from supportguard.db.security_contract import (
    BASELINE_IDENTITY,
    CURRENT_INTERVIEW_DATABASE_REVISION,
    DATABASE_PREFLIGHT,
    INTERVIEW_BASELINE_ROOT_REVISION,
    LEGACY_FINAL_DATABASE_HEAD,
)

BASELINE_PROVENANCE_SCHEMA: Final = "supportguard-interview-baseline-provenance.v1"
BASELINE_LOCK_NAME: Final = "supportguard:interview-baseline:i200"
EMPTY_BOOTSTRAP_MANIFEST_SHA256: Final = (
    "fbf069ab8bcffef7e16a63839412b33a37922461fcced52a9be12c9073f37e4f"
)
RAW_EMPTY_DATABASE_MANIFEST_SHA256: Final = (
    "4849a25909fcfcc95474b19065fed7a0bd00eba7cb045b0ddbffa795423c18b1"
)
I200_BASELINE_MANIFEST_SHA256: Final = (
    "9007f1da6c8e85dcfb03d2ebc7d2e6aa2397882160f1d7df8d148dd7648bfe80"
)
I200_BASELINE_NON_DATABASE_MANIFEST_SHA256: Final = (
    "2fd99b453a689840fea40d580c21d71119ef3b5f451ec443bc28f9ae6de43185"
)
CURRENT_BASELINE_MANIFEST_SHA256: Final = (
    "9d4faabe4e2705803aea84316aabca8467f357a164560fa7123de23afae82e49"
)
CURRENT_BASELINE_NON_DATABASE_MANIFEST_SHA256: Final = (
    "ab4c1eefc5d7d8adda68eabd6403839eca84028187a721642f79c2a59c497cfc"
)
RUNTIME_TIMING_V1_CONFIG_HASH: Final = (
    "2c68017cce05fa144eb8aaaaccd9acc45bec3e6ca28d40f7f0269dc5bdee1672"
)
_REVISION_PATTERN: Final = re.compile(r"^[a-z0-9_]{1,64}$")
_BOOTSTRAP_FUNCTION: Final = "public.supportguard_bootstrap_transfer_ownership()"
_COMPLETION_MARKER: Final = "supportguard_control.runtime_timing_snapshots"


class InterviewBaselinePreflightError(RuntimeError):
    """The database is not a permitted empty-baseline input."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class InterviewBaselinePreflight:
    classification: str
    observed_revision: str | None
    observed_objects: tuple[str, ...]


class _AsyncDriverConnection(Protocol):
    def execute(self, statement: str) -> Awaitable[object]: ...


class _AsyncAdaptedConnection(Protocol):
    def run_async(
        self, operation: Callable[[_AsyncDriverConnection], Awaitable[object]]
    ) -> object: ...


def acquire_interview_baseline_lock(connection: Connection) -> None:
    connection.execute(
        text("SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(:name,0))"),
        {"name": BASELINE_LOCK_NAME},
    )


def _version_rows(connection: Connection) -> tuple[str, ...] | None:
    relation = connection.execute(
        text("SELECT pg_catalog.to_regclass(:relation)::text"),
        {"relation": DATABASE_PREFLIGHT.version_relation},
    ).scalar_one()
    if relation is None:
        return None
    rows = tuple(
        str(row[0])
        for row in connection.execute(
            text("SELECT version_num FROM public.alembic_version ORDER BY version_num")
        ).all()
    )
    return rows


def _shell_objects(connection: Connection) -> tuple[str, ...]:
    rows = connection.execute(
        text(
            "WITH extension_objects AS ("
            " SELECT d.classid,d.objid FROM pg_catalog.pg_depend d WHERE d.deptype='e'"
            "), objects AS ("
            " SELECT 'schema:'||n.nspname AS identity FROM pg_catalog.pg_namespace n"
            " WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'"
            " UNION ALL SELECT 'extension:'||e.extname FROM pg_catalog.pg_extension e"
            " UNION ALL SELECT CASE WHEN c.relkind='S' THEN 'sequence:' ELSE 'relation:' END"
            "   ||n.nspname||'.'||c.relname"
            " FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace"
            " WHERE n.nspname IN ('public','supportguard_control')"
            " AND NOT EXISTS (SELECT 1 FROM extension_objects x"
            "   WHERE x.classid='pg_catalog.pg_class'::pg_catalog.regclass AND x.objid=c.oid)"
            " UNION ALL SELECT 'function:'||n.nspname||'.'||p.proname||'('||"
            "   pg_catalog.pg_get_function_identity_arguments(p.oid)||')'"
            " FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace"
            " WHERE n.nspname IN ('public','supportguard_control')"
            " AND NOT EXISTS (SELECT 1 FROM extension_objects x"
            "   WHERE x.classid='pg_catalog.pg_proc'::pg_catalog.regclass AND x.objid=p.oid)"
            " UNION ALL SELECT 'type:'||n.nspname||'.'||t.typname"
            " FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid=t.typnamespace"
            " WHERE n.nspname IN ('public','supportguard_control') AND t.typrelid=0"
            " AND t.typtype IN ('c','d','e','m','r')"
            " AND NOT EXISTS (SELECT 1 FROM extension_objects x"
            "   WHERE x.classid='pg_catalog.pg_type'::pg_catalog.regclass AND x.objid=t.oid)"
            ") SELECT identity FROM objects ORDER BY identity"
        )
    ).all()
    return tuple(str(row[0]) for row in rows)


_CATALOG_MANIFEST_SQL: Final = r"""
WITH extension_objects AS (
  SELECT d.classid,d.objid FROM pg_catalog.pg_depend d WHERE d.deptype='e'
), records AS (
  SELECT 'database'::text category,'$DATABASE'::text identity,
    pg_catalog.jsonb_build_object(
      'owner',pg_catalog.pg_get_userbyid(d.datdba),
      'acl',coalesce(d.datacl,pg_catalog.acldefault('d'::"char",d.datdba))
    )::text payload
  FROM pg_catalog.pg_database d WHERE d.datname=pg_catalog.current_database()
  UNION ALL
  SELECT 'schema',n.nspname,pg_catalog.jsonb_build_object(
    'owner',pg_catalog.pg_get_userbyid(n.nspowner),
    'acl',coalesce(n.nspacl,pg_catalog.acldefault('n'::"char",n.nspowner))
  )::text
  FROM pg_catalog.pg_namespace n
  WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema'
  UNION ALL
  SELECT 'extension',e.extname,pg_catalog.jsonb_build_object(
    'version',e.extversion,'schema',n.nspname,
    'owner',pg_catalog.pg_get_userbyid(e.extowner),'relocatable',e.extrelocatable
  )::text
  FROM pg_catalog.pg_extension e
  JOIN pg_catalog.pg_namespace n ON n.oid=e.extnamespace
  UNION ALL
  SELECT 'relation',n.nspname||'.'||c.relname,pg_catalog.jsonb_build_object(
    'kind',c.relkind::text,'persistence',c.relpersistence::text,
    'owner',pg_catalog.pg_get_userbyid(c.relowner),
    'acl',coalesce(c.relacl,pg_catalog.acldefault(
      CASE WHEN c.relkind='S' THEN 's'::"char" ELSE 'r'::"char" END,c.relowner)),
    'rls',c.relrowsecurity,'force_rls',c.relforcerowsecurity,
    'partition_key',coalesce(pg_catalog.pg_get_partkeydef(c.oid),'')
  )::text
  FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname IN ('public','supportguard_control')
    AND NOT EXISTS (SELECT 1 FROM extension_objects x
      WHERE x.classid='pg_catalog.pg_class'::pg_catalog.regclass AND x.objid=c.oid)
  UNION ALL
  SELECT 'column',n.nspname||'.'||c.relname||'.'||a.attname,
    pg_catalog.jsonb_build_object(
      'type',pg_catalog.format_type(a.atttypid,a.atttypmod),
      'not_null',a.attnotnull,
      'default',coalesce(pg_catalog.pg_get_expr(d.adbin,d.adrelid),''),
      'identity',a.attidentity,'generated',a.attgenerated,
      'collation',coalesce(coll.collname,'')
    )::text
  FROM pg_catalog.pg_attribute a
  JOIN pg_catalog.pg_class c ON c.oid=a.attrelid
  JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
  LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
  LEFT JOIN pg_catalog.pg_collation coll ON coll.oid=a.attcollation
  WHERE a.attnum>0 AND NOT a.attisdropped
    AND n.nspname IN ('public','supportguard_control')
    AND c.relkind IN ('r','p','v','m','f')
    AND NOT EXISTS (SELECT 1 FROM extension_objects x
      WHERE x.classid='pg_catalog.pg_class'::pg_catalog.regclass AND x.objid=c.oid)
  UNION ALL
  SELECT 'constraint',n.nspname||'.'||c.relname||'.'||k.conname,
    pg_catalog.jsonb_build_object(
      'type',k.contype::text,'definition',pg_catalog.pg_get_constraintdef(k.oid,true),
      'validated',k.convalidated,'deferrable',k.condeferrable,'deferred',k.condeferred
    )::text
  FROM pg_catalog.pg_constraint k
  JOIN pg_catalog.pg_class c ON c.oid=k.conrelid
  JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname IN ('public','supportguard_control')
  UNION ALL
  SELECT 'index',n.nspname||'.'||c.relname||'.'||i.relname,
    pg_catalog.jsonb_build_object(
      'definition',pg_catalog.pg_get_indexdef(i.oid),'valid',x.indisvalid,
      'ready',x.indisready,'live',x.indislive,'replica_identity',x.indisreplident
    )::text
  FROM pg_catalog.pg_index x
  JOIN pg_catalog.pg_class c ON c.oid=x.indrelid
  JOIN pg_catalog.pg_class i ON i.oid=x.indexrelid
  JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname IN ('public','supportguard_control')
  UNION ALL
  SELECT 'trigger',n.nspname||'.'||c.relname||'.'||t.tgname,
    pg_catalog.jsonb_build_object(
      'definition',pg_catalog.pg_get_triggerdef(t.oid,true),'enabled',t.tgenabled::text
    )::text
  FROM pg_catalog.pg_trigger t
  JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
  JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
  WHERE NOT t.tgisinternal AND n.nspname IN ('public','supportguard_control')
  UNION ALL
  SELECT 'function',n.nspname||'.'||p.proname||'('||
    pg_catalog.pg_get_function_identity_arguments(p.oid)||')',
    pg_catalog.jsonb_build_object(
      'owner',pg_catalog.pg_get_userbyid(p.proowner),'language',l.lanname,
      'kind',p.prokind::text,'volatility',p.provolatile::text,
      'parallel',p.proparallel::text,'strict',p.proisstrict,
      'security_definer',p.prosecdef,'leakproof',p.proleakproof,
      'config',coalesce(p.proconfig,ARRAY[]::text[]),
      'acl',coalesce(p.proacl,pg_catalog.acldefault('f'::"char",p.proowner)),
      'result',pg_catalog.pg_get_function_result(p.oid),
      'definition',pg_catalog.pg_get_functiondef(p.oid)
    )::text
  FROM pg_catalog.pg_proc p
  JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
  JOIN pg_catalog.pg_language l ON l.oid=p.prolang
  WHERE n.nspname IN ('public','supportguard_control')
    AND NOT EXISTS (SELECT 1 FROM extension_objects x
      WHERE x.classid='pg_catalog.pg_proc'::pg_catalog.regclass AND x.objid=p.oid)
  UNION ALL
  SELECT 'policy',schemaname||'.'||tablename||'.'||policyname,
    pg_catalog.jsonb_build_object(
      'permissive',permissive,'roles',roles,'command',cmd,
      'using',coalesce(qual,''),'check',coalesce(with_check,'')
    )::text
  FROM pg_catalog.pg_policies WHERE schemaname IN ('public','supportguard_control')
  UNION ALL
  SELECT 'default_acl',pg_catalog.pg_get_userbyid(d.defaclrole)||'.'||
    coalesce(n.nspname,'$GLOBAL')||'.'||d.defaclobjtype::text,
    pg_catalog.jsonb_build_object('acl',d.defaclacl)::text
  FROM pg_catalog.pg_default_acl d
  LEFT JOIN pg_catalog.pg_namespace n ON n.oid=d.defaclnamespace
  WHERE n.nspname IS NULL OR n.nspname IN ('public','supportguard_control')
  UNION ALL
  SELECT 'role',r.rolname,pg_catalog.jsonb_build_object(
    'superuser',r.rolsuper,'inherit',r.rolinherit,'create_role',r.rolcreaterole,
    'create_database',r.rolcreatedb,'login',r.rolcanlogin,
    'replication',r.rolreplication,'bypass_rls',r.rolbypassrls,
    'connection_limit',r.rolconnlimit,'valid_until',r.rolvaliduntil::text,
    'config',coalesce(r.rolconfig,ARRAY[]::text[])
  )::text
  FROM pg_catalog.pg_roles r WHERE r.rolname LIKE 'supportguard_%'
  UNION ALL
  SELECT 'role_membership',parent.rolname||'.'||member.rolname,
    pg_catalog.jsonb_build_object(
      'grantor',grantor.rolname,'admin',m.admin_option,
      'inherit',m.inherit_option,'set',m.set_option
    )::text
  FROM pg_catalog.pg_auth_members m
  JOIN pg_catalog.pg_roles parent ON parent.oid=m.roleid
  JOIN pg_catalog.pg_roles member ON member.oid=m.member
  JOIN pg_catalog.pg_roles grantor ON grantor.oid=m.grantor
  WHERE parent.rolname LIKE 'supportguard_%' OR member.rolname LIKE 'supportguard_%'
  UNION ALL
  SELECT 'type',n.nspname||'.'||t.typname,pg_catalog.jsonb_build_object(
    'kind',t.typtype::text,'category',t.typcategory::text,
    'owner',pg_catalog.pg_get_userbyid(t.typowner),
    'relation',t.typrelid::pg_catalog.regclass::text
  )::text
  FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid=t.typnamespace
  WHERE n.nspname IN ('public','supportguard_control')
    AND NOT EXISTS (SELECT 1 FROM extension_objects x
      WHERE x.classid='pg_catalog.pg_type'::pg_catalog.regclass AND x.objid=t.oid)
  UNION ALL
  SELECT 'operator',n.nspname||'.'||o.oprname||'('||o.oprleft::pg_catalog.regtype::text||','||
    o.oprright::pg_catalog.regtype::text||')',pg_catalog.jsonb_build_object(
      'owner',pg_catalog.pg_get_userbyid(o.oprowner),'function',o.oprcode::text
    )::text
  FROM pg_catalog.pg_operator o JOIN pg_catalog.pg_namespace n ON n.oid=o.oprnamespace
  WHERE n.nspname IN ('public','supportguard_control')
    AND NOT EXISTS (SELECT 1 FROM extension_objects x
      WHERE x.classid='pg_catalog.pg_operator'::pg_catalog.regclass AND x.objid=o.oid)
  UNION ALL
  SELECT 'collation',n.nspname||'.'||c.collname,pg_catalog.jsonb_build_object(
    'owner',pg_catalog.pg_get_userbyid(c.collowner),'provider',c.collprovider::text,
    'deterministic',c.collisdeterministic,'locale',coalesce(c.colliculocale,c.collcollate)
  )::text
  FROM pg_catalog.pg_collation c JOIN pg_catalog.pg_namespace n ON n.oid=c.collnamespace
  WHERE n.nspname IN ('public','supportguard_control')
    AND NOT EXISTS (SELECT 1 FROM extension_objects x
      WHERE x.classid='pg_catalog.pg_collation'::pg_catalog.regclass AND x.objid=c.oid)
  UNION ALL
  SELECT 'conversion',n.nspname||'.'||c.conname,pg_catalog.jsonb_build_object(
    'owner',pg_catalog.pg_get_userbyid(c.conowner),'source',c.conforencoding,
    'target',c.contoencoding,'default',c.condefault,'function',c.conproc::text
  )::text
  FROM pg_catalog.pg_conversion c JOIN pg_catalog.pg_namespace n ON n.oid=c.connamespace
  WHERE n.nspname IN ('public','supportguard_control')
  UNION ALL
  SELECT 'event_trigger',e.evtname,pg_catalog.jsonb_build_object(
    'event',e.evtevent,'owner',pg_catalog.pg_get_userbyid(e.evtowner),
    'function',e.evtfoid::pg_catalog.regprocedure::text,'enabled',e.evtenabled::text,
    'tags',coalesce(e.evttags,ARRAY[]::text[])
  )::text FROM pg_catalog.pg_event_trigger e
)
SELECT category,identity,payload FROM records ORDER BY category,identity,payload
"""


def catalog_security_manifest_records(
    connection: Connection,
    *,
    include_cluster_roles: bool = True,
    include_database_access: bool = True,
) -> tuple[tuple[str, str, str], ...]:
    # pg_dump intentionally clears search_path while installing its payload.
    # PostgreSQL deparsers such as pg_get_constraintdef() and format_type() are
    # search-path-sensitive, so pin the canonical catalog rendering before
    # hashing.  SET LOCAL is transaction-scoped and performs no schema DDL.
    connection.execute(
        text("SELECT pg_catalog.set_config('search_path', '\"$user\", public', true)")
    )
    return tuple(
        (str(row[0]), str(row[1]), str(row[2]))
        for row in connection.execute(text(_CATALOG_MANIFEST_SQL)).all()
        if (include_cluster_roles or str(row[0]) not in {"role", "role_membership"})
        and (include_database_access or str(row[0]) != "database")
    )


def catalog_security_manifest_sha256(
    connection: Connection,
    *,
    include_cluster_roles: bool = True,
    include_database_access: bool = True,
) -> str:
    rows = catalog_security_manifest_records(
        connection,
        include_cluster_roles=include_cluster_roles,
        include_database_access=include_database_access,
    )
    serialized = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def _expected_manifest_for_revision(revision: str) -> str:
    if revision == INTERVIEW_BASELINE_ROOT_REVISION:
        return I200_BASELINE_MANIFEST_SHA256
    if revision == CURRENT_INTERVIEW_DATABASE_REVISION:
        return CURRENT_BASELINE_MANIFEST_SHA256
    raise InterviewBaselinePreflightError("interview_baseline_unknown_revision_rejected")


def _current_database(connection: Connection) -> str:
    return str(connection.execute(text("SELECT pg_catalog.current_database()")).scalar_one())


def _runtime_timing_v1_contract_is_exact(connection: Connection) -> bool:
    rows = tuple(
        tuple(row)
        for row in connection.execute(
            text(
                "SELECT timing_version,max_job_age_seconds,max_attempts,"
                "max_delivery_generation,redelivery_grace_seconds,lease_seconds,"
                "backlog_count_limit,oldest_backlog_seconds,config_hash,is_active,"
                "activated_at IS NOT NULL "
                "FROM supportguard_control.runtime_timing_snapshots ORDER BY timing_version"
            )
        ).all()
    )
    return rows == ((1, 600, 5, 5, 15, 30, 500, 600, RUNTIME_TIMING_V1_CONFIG_HASH, True, True),)


def _active_runtime_timing_contract_is_valid(connection: Connection) -> bool:
    rows = tuple(
        tuple(row)
        for row in connection.execute(
            text(
                "SELECT timing_version,config_hash,activated_at IS NOT NULL "
                "FROM supportguard_control.runtime_timing_snapshots "
                "WHERE is_active ORDER BY timing_version"
            )
        ).all()
    )
    return (
        len(rows) == 1 and int(rows[0][0]) > 0 and len(str(rows[0][1])) == 64 and bool(rows[0][2])
    )


def _database_identity_contract_is_exact(connection: Connection, *, expected_database: str) -> bool:
    rows = tuple(
        (str(row[0]), str(row[1]), bool(row[2]))
        for row in connection.execute(
            text(
                "SELECT database_uuid::text,database_name,created_at IS NOT NULL "
                "FROM supportguard_control.database_identity ORDER BY database_name"
            )
        ).all()
    )
    return (
        len(rows) == 1
        and bool(rows[0][0])
        and rows[0][1:]
        == (
            expected_database,
            True,
        )
    )


def _require_fresh_operational_bootstrap(
    connection: Connection, *, expected_database: str, error_prefix: str
) -> None:
    if not _runtime_timing_v1_contract_is_exact(connection):
        raise InterviewBaselinePreflightError(f"{error_prefix}_runtime_timing_invalid")
    if not _database_identity_contract_is_exact(connection, expected_database=expected_database):
        raise InterviewBaselinePreflightError(f"{error_prefix}_database_identity_invalid")


def _require_current_operational_state(
    connection: Connection, *, expected_database: str, error_prefix: str
) -> None:
    if not _active_runtime_timing_contract_is_valid(connection):
        raise InterviewBaselinePreflightError(f"{error_prefix}_runtime_timing_invalid")
    if not _database_identity_contract_is_exact(connection, expected_database=expected_database):
        raise InterviewBaselinePreflightError(f"{error_prefix}_database_identity_invalid")


def _require_current_operational_state_with_owner_access(
    connection: Connection, *, expected_database: str, error_prefix: str
) -> None:
    current_user = str(connection.execute(text("SELECT current_user::text")).scalar_one())
    switched_role = current_user == "supportguard_migrator"
    if switched_role:
        connection.execute(text("SET LOCAL ROLE supportguard_owner"))
    try:
        _require_current_operational_state(
            connection,
            expected_database=expected_database,
            error_prefix=error_prefix,
        )
    finally:
        if switched_role:
            connection.execute(text("RESET ROLE"))


def inspect_interview_template_clone_admin(
    connection: Connection, *, expected_source_database: str
) -> InterviewBaselinePreflight:
    """Accept only the exact ACL-less shape produced by CREATE DATABASE TEMPLATE."""

    if connection.dialect.name != "postgresql":
        raise InterviewBaselinePreflightError("interview_baseline_requires_postgresql")
    rows = _version_rows(connection)
    if rows != (CURRENT_INTERVIEW_DATABASE_REVISION,):
        raise InterviewBaselinePreflightError("interview_template_clone_revision_invalid")
    marker = connection.execute(
        text("SELECT pg_catalog.to_regclass(:marker)::text"), {"marker": _COMPLETION_MARKER}
    ).scalar_one()
    bootstrap = connection.execute(
        text("SELECT pg_catalog.to_regprocedure(:function)::text"),
        {"function": _BOOTSTRAP_FUNCTION},
    ).scalar_one()
    owner, acl_is_null = connection.execute(
        text(
            "SELECT pg_catalog.pg_get_userbyid(datdba),datacl IS NULL "
            "FROM pg_catalog.pg_database WHERE datname=pg_catalog.current_database()"
        )
    ).one()
    if marker is None or bootstrap is not None:
        raise InterviewBaselinePreflightError("interview_template_clone_state_incomplete")
    if str(owner) != "supportguard" or not bool(acl_is_null):
        raise InterviewBaselinePreflightError("interview_template_clone_database_acl_invalid")
    if (
        catalog_security_manifest_sha256(connection, include_database_access=False)
        != CURRENT_BASELINE_NON_DATABASE_MANIFEST_SHA256
    ):
        raise InterviewBaselinePreflightError("interview_template_clone_catalog_mismatch")
    _require_fresh_operational_bootstrap(
        connection,
        expected_database=expected_source_database,
        error_prefix="interview_template_clone",
    )
    return InterviewBaselinePreflight("template_clone", CURRENT_INTERVIEW_DATABASE_REVISION, ())


def inspect_interview_baseline(connection: Connection) -> InterviewBaselinePreflight:
    if connection.dialect.name != "postgresql":
        raise InterviewBaselinePreflightError("interview_baseline_requires_postgresql")
    actor = connection.execute(text("SELECT session_user::text,current_user::text")).one()
    if tuple(str(value) for value in actor) != (
        "supportguard_migrator",
        "supportguard_migrator",
    ):
        raise InterviewBaselinePreflightError("interview_baseline_requires_migrator")

    rows = _version_rows(connection)
    if rows is not None:
        if len(rows) != 1:
            raise InterviewBaselinePreflightError("interview_baseline_version_cardinality_invalid")
        revision = rows[0]
        if not _REVISION_PATTERN.fullmatch(revision):
            raise InterviewBaselinePreflightError("interview_baseline_revision_invalid")
        if revision == LEGACY_FINAL_DATABASE_HEAD:
            raise InterviewBaselinePreflightError("interview_baseline_legacy_database_rejected")
        if revision not in DATABASE_PREFLIGHT.accepted_existing_revisions:
            raise InterviewBaselinePreflightError("interview_baseline_unknown_revision_rejected")
        marker = connection.execute(
            text("SELECT pg_catalog.to_regclass(:marker)::text"), {"marker": _COMPLETION_MARKER}
        ).scalar_one()
        bootstrap = connection.execute(
            text("SELECT pg_catalog.to_regprocedure(:function)::text"),
            {"function": _BOOTSTRAP_FUNCTION},
        ).scalar_one()
        if marker is None or bootstrap is not None:
            raise InterviewBaselinePreflightError("interview_baseline_current_state_incomplete")
        if catalog_security_manifest_sha256(connection) != _expected_manifest_for_revision(
            revision
        ):
            raise InterviewBaselinePreflightError("interview_baseline_current_catalog_mismatch")
        _require_current_operational_state_with_owner_access(
            connection,
            expected_database=_current_database(connection),
            error_prefix="interview_baseline_current",
        )
        classification = (
            "current" if revision == CURRENT_INTERVIEW_DATABASE_REVISION else "migration_source"
        )
        return InterviewBaselinePreflight(classification, revision, ())

    observed = _shell_objects(connection)
    allowed = {
        "schema:public",
        "schema:supportguard_control",
        "extension:plpgsql",
        "extension:vector",
        f"function:{_BOOTSTRAP_FUNCTION}",
    }
    if set(observed) != allowed or len(observed) != len(allowed):
        raise InterviewBaselinePreflightError("interview_baseline_nonempty_database_rejected")
    if catalog_security_manifest_sha256(connection) != EMPTY_BOOTSTRAP_MANIFEST_SHA256:
        raise InterviewBaselinePreflightError("interview_baseline_bootstrap_manifest_mismatch")
    return InterviewBaselinePreflight("empty_bootstrap_shell", None, observed)


def inspect_interview_baseline_admin(connection: Connection) -> InterviewBaselinePreflight:
    """Read-only target-database check that must run before role bootstrap DDL."""

    if connection.dialect.name != "postgresql":
        raise InterviewBaselinePreflightError("interview_baseline_requires_postgresql")
    rows = _version_rows(connection)
    if rows is not None:
        if len(rows) != 1:
            raise InterviewBaselinePreflightError("interview_baseline_version_cardinality_invalid")
        revision = rows[0]
        if revision == LEGACY_FINAL_DATABASE_HEAD:
            raise InterviewBaselinePreflightError("interview_baseline_legacy_database_rejected")
        if revision not in DATABASE_PREFLIGHT.accepted_existing_revisions:
            raise InterviewBaselinePreflightError("interview_baseline_unknown_revision_rejected")
        marker = connection.execute(
            text("SELECT pg_catalog.to_regclass(:marker)::text"), {"marker": _COMPLETION_MARKER}
        ).scalar_one()
        bootstrap = connection.execute(
            text("SELECT pg_catalog.to_regprocedure(:function)::text"),
            {"function": _BOOTSTRAP_FUNCTION},
        ).scalar_one()
        if marker is None or bootstrap is not None:
            raise InterviewBaselinePreflightError("interview_baseline_current_state_incomplete")
        if catalog_security_manifest_sha256(connection) != _expected_manifest_for_revision(
            revision
        ):
            raise InterviewBaselinePreflightError("interview_baseline_current_catalog_mismatch")
        _require_current_operational_state(
            connection,
            expected_database=_current_database(connection),
            error_prefix="interview_baseline_current",
        )
        classification = (
            "current" if revision == CURRENT_INTERVIEW_DATABASE_REVISION else "migration_source"
        )
        return InterviewBaselinePreflight(classification, revision, ())

    observed = _shell_objects(connection)
    raw_empty = ("extension:plpgsql", "schema:public")
    if observed == raw_empty:
        if (
            catalog_security_manifest_sha256(connection, include_cluster_roles=False)
            != RAW_EMPTY_DATABASE_MANIFEST_SHA256
        ):
            raise InterviewBaselinePreflightError("interview_baseline_raw_empty_manifest_mismatch")
        return InterviewBaselinePreflight("raw_empty_database", None, observed)
    bootstrap_shell = {
        "schema:public",
        "schema:supportguard_control",
        "extension:plpgsql",
        "extension:vector",
        f"function:{_BOOTSTRAP_FUNCTION}",
    }
    if set(observed) != bootstrap_shell or len(observed) != len(bootstrap_shell):
        raise InterviewBaselinePreflightError("interview_baseline_nonempty_database_rejected")
    if catalog_security_manifest_sha256(connection) != EMPTY_BOOTSTRAP_MANIFEST_SHA256:
        raise InterviewBaselinePreflightError("interview_baseline_bootstrap_manifest_mismatch")
    return InterviewBaselinePreflight("empty_bootstrap_shell", None, observed)


def upgrade_interview_baseline() -> None:
    """Apply the independent baseline through its sole public runtime owner."""

    command.upgrade(Config("alembic-interview.ini"), "head")


def _artifact_paths(artifact_directory: Path) -> tuple[Path, Path]:
    return artifact_directory / "baseline.sql", artifact_directory / "provenance.json"


def load_verified_baseline_sql(artifact_directory: Path) -> str:
    sql_path, provenance_path = _artifact_paths(artifact_directory)
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    if not isinstance(provenance, dict):
        raise RuntimeError("interview_baseline_provenance_invalid")
    expected = {
        "schema_version": BASELINE_PROVENANCE_SCHEMA,
        "source_revision": BASELINE_IDENTITY.source_legacy_head,
        "baseline_revision": BASELINE_IDENTITY.revision,
    }
    if any(provenance.get(key) != value for key, value in expected.items()):
        raise RuntimeError("interview_baseline_provenance_identity_mismatch")
    sql = sql_path.read_text(encoding="utf-8")
    digest = hashlib.sha256(sql.encode()).hexdigest()
    if provenance.get("baseline_sql_sha256") != digest:
        raise RuntimeError("interview_baseline_sql_hash_mismatch")
    if "backend/alembic/versions" in sql or "supportguard_baseline_version" in sql:
        raise RuntimeError("interview_baseline_sql_not_independent")
    return sql


def install_interview_baseline(connection: Connection, *, artifact_directory: Path) -> None:
    sql = load_verified_baseline_sql(artifact_directory)
    available = connection.execute(
        text("SELECT pg_catalog.to_regprocedure(:function)::text"),
        {"function": _BOOTSTRAP_FUNCTION},
    ).scalar_one()
    if available is None:
        raise InterviewBaselinePreflightError("interview_baseline_bootstrap_capability_missing")
    connection.execute(text("SELECT public.supportguard_bootstrap_transfer_ownership()"))
    connection.execute(text("DROP FUNCTION public.supportguard_bootstrap_transfer_ownership()"))
    connection.execute(text("SET LOCAL ROLE supportguard_owner"))
    adapted = connection.connection
    run_async = getattr(adapted, "run_async", None)
    if not callable(run_async):
        raise RuntimeError("interview_baseline_requires_async_driver")
    async_adapted = cast(
        _AsyncAdaptedConnection, adapted
    )  # SQLAlchemy's async DBAPI adapter owns this bridge.
    async_adapted.run_async(lambda driver: driver.execute(sql))
    connection.execute(
        text(
            "INSERT INTO supportguard_control.runtime_timing_snapshots("
            "timing_version,max_job_age_seconds,max_attempts,max_delivery_generation,"
            "redelivery_grace_seconds,lease_seconds,backlog_count_limit,"
            "oldest_backlog_seconds,config_hash,is_active) VALUES ("
            "1,600,5,5,15,30,500,600,:config_hash,true)"
        ),
        {"config_hash": RUNTIME_TIMING_V1_CONFIG_HASH},
    )
    connection.execute(
        text(
            "INSERT INTO supportguard_control.database_identity(database_name) "
            "VALUES (pg_catalog.current_database())"
        )
    )
    if not _runtime_timing_v1_contract_is_exact(connection):
        raise RuntimeError("interview_baseline_runtime_timing_installation_failed")
    if not _database_identity_contract_is_exact(
        connection, expected_database=_current_database(connection)
    ):
        raise RuntimeError("interview_baseline_database_identity_installation_failed")
    connection.execute(
        text("GRANT SELECT ON TABLE public.alembic_version TO supportguard_migrator")
    )
    connection.execute(text("REVOKE CREATE ON SCHEMA public FROM supportguard_migrator"))


def _verify_interview_revision_postcondition(
    connection: Connection, *, revision: str, manifest_sha256: str
) -> None:
    rows = _version_rows(connection)
    if rows != (revision,):
        raise RuntimeError("interview_baseline_revision_postcondition_failed")
    marker = connection.execute(
        text("SELECT pg_catalog.to_regclass(:marker)::text"), {"marker": _COMPLETION_MARKER}
    ).scalar_one()
    bootstrap = connection.execute(
        text("SELECT pg_catalog.to_regprocedure(:function)::text"),
        {"function": _BOOTSTRAP_FUNCTION},
    ).scalar_one()
    owner = connection.execute(
        text(
            "SELECT pg_catalog.pg_get_userbyid(c.relowner) FROM pg_catalog.pg_class c"
            " WHERE c.oid='public.alembic_version'::pg_catalog.regclass"
        )
    ).scalar_one()
    if marker is None or bootstrap is not None or owner != "supportguard_owner":
        raise RuntimeError("interview_baseline_catalog_postcondition_failed")
    if catalog_security_manifest_sha256(connection) != manifest_sha256:
        raise RuntimeError("interview_baseline_manifest_postcondition_failed")
    try:
        _require_current_operational_state_with_owner_access(
            connection,
            expected_database=_current_database(connection),
            error_prefix="interview_baseline_postcondition",
        )
    except InterviewBaselinePreflightError as exc:
        raise RuntimeError(exc.code) from exc


def verify_interview_migration_postcondition(connection: Connection) -> None:
    """Verify an exact supported Alembic target without weakening current Runtime identity."""

    rows = _version_rows(connection)
    if rows == (INTERVIEW_BASELINE_ROOT_REVISION,):
        _verify_interview_revision_postcondition(
            connection,
            revision=INTERVIEW_BASELINE_ROOT_REVISION,
            manifest_sha256=I200_BASELINE_MANIFEST_SHA256,
        )
        return
    if rows == (CURRENT_INTERVIEW_DATABASE_REVISION,):
        _verify_interview_revision_postcondition(
            connection,
            revision=CURRENT_INTERVIEW_DATABASE_REVISION,
            manifest_sha256=CURRENT_BASELINE_MANIFEST_SHA256,
        )
        return
    raise RuntimeError("interview_baseline_revision_postcondition_failed")


def verify_interview_baseline_postcondition(connection: Connection) -> None:
    """Verify the only schema identity accepted by current Runtime services."""

    _verify_interview_revision_postcondition(
        connection,
        revision=CURRENT_INTERVIEW_DATABASE_REVISION,
        manifest_sha256=CURRENT_BASELINE_MANIFEST_SHA256,
    )
