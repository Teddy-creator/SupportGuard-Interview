"""Frozen temporal selection and content-addressed document-family mapping."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("temporal timestamps must include a timezone")
    return value.astimezone(UTC)


def applicability_scope_hash(plan: str | None, region: str | None) -> str:
    payload = {
        "schema_version": "applicability-scope.v1",
        "plan": plan,
        "region": region,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class CurrentSelector(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["current"] = "current"
    claim_effective_time: datetime


class AsOfSelector(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["as_of"] = "as_of"
    explicit_as_of: datetime
    claim_effective_time: datetime

    @model_validator(mode="after")
    def exact_claim_time(self) -> AsOfSelector:
        if _utc(self.explicit_as_of) != _utc(self.claim_effective_time):
            raise ValueError("as_of claim time must equal explicit_as_of")
        return self


class VersionSelector(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["version"] = "version"
    historical_version: str = Field(min_length=1)
    claim_effective_time: None = None


class VersionAsOfSelector(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["version_as_of"] = "version_as_of"
    historical_version: str = Field(min_length=1)
    explicit_as_of: datetime
    claim_effective_time: datetime

    @model_validator(mode="after")
    def exact_claim_time(self) -> VersionAsOfSelector:
        if _utc(self.explicit_as_of) != _utc(self.claim_effective_time):
            raise ValueError("version_as_of claim time must equal explicit_as_of")
        return self


TemporalSelector = Annotated[
    CurrentSelector | AsOfSelector | VersionSelector | VersionAsOfSelector,
    Field(discriminator="mode"),
]
TEMPORAL_SELECTOR_ADAPTER: TypeAdapter[TemporalSelector] = TypeAdapter(TemporalSelector)


def build_temporal_selector(
    *,
    trace_logical_time: datetime,
    historical_version: str | None,
    explicit_as_of: datetime | None,
) -> TemporalSelector:
    logical_time = _utc(trace_logical_time)
    version = historical_version.removeprefix("v") if historical_version else None
    as_of = _utc(explicit_as_of) if explicit_as_of else None
    if version and as_of:
        return VersionAsOfSelector(
            historical_version=version,
            explicit_as_of=as_of,
            claim_effective_time=as_of,
        )
    if version:
        return VersionSelector(historical_version=version)
    if as_of:
        return AsOfSelector(explicit_as_of=as_of, claim_effective_time=as_of)
    return CurrentSelector(claim_effective_time=logical_time)


class TemporalBackfillEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str
    document_family_key: str
    effective_from: datetime
    effective_until: datetime | None = None
    applicable_plan: str | None = None
    applicable_region: str | None = None

    @model_validator(mode="after")
    def valid_interval(self) -> TemporalBackfillEntry:
        start = _utc(self.effective_from)
        if self.effective_until is not None and _utc(self.effective_until) <= start:
            raise ValueError("effective interval must be non-empty and half-open")
        return self

    @property
    def scope_hash(self) -> str:
        return applicability_scope_hash(self.applicable_plan, self.applicable_region)


class TemporalBackfillManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["knowledge-temporal-backfill.v1"]
    entries: tuple[TemporalBackfillEntry, ...]

    @model_validator(mode="after")
    def unique_documents(self) -> TemporalBackfillManifest:
        keys = [entry.document_id for entry in self.entries]
        if len(keys) != len(set(keys)):
            raise ValueError("temporal backfill contains duplicate document_id")
        return self

    @property
    def content_hash(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(canonical).hexdigest()

    def by_document_id(self) -> dict[str, TemporalBackfillEntry]:
        return {entry.document_id: entry for entry in self.entries}


def load_temporal_backfill(path: Path) -> TemporalBackfillManifest:
    return TemporalBackfillManifest.model_validate_json(path.read_bytes())
