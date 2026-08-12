from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from redis.typing import EncodableT


class RuntimeJobMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["runtime-job.v1"] = "runtime-job.v1"
    event_id: str = Field(min_length=1, max_length=64)
    delivery_id: str = Field(min_length=1, max_length=64)
    job_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
    tenant_id: str = Field(min_length=1, max_length=64)
    delivery_generation: int = Field(ge=1, le=5)
    traceparent: str | None = Field(default=None, max_length=128)

    def redis_fields(self) -> dict[EncodableT, EncodableT]:
        return {"payload": self.model_dump_json()}

    @classmethod
    def from_redis(cls, fields: dict[bytes | str, bytes | str]) -> RuntimeJobMessage:
        raw = fields.get(b"payload", fields.get("payload"))
        if raw is None:
            raise ValueError("missing runtime payload")
        if isinstance(raw, bytes):
            raw = raw.decode()
        return cls.model_validate(json.loads(raw))
