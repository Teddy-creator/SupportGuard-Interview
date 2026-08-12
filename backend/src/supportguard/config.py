import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["development", "test", "production"] = "development"
    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65535)
    app_reload: bool = False
    app_secret_key: SecretStr = SecretStr("local-development-only-change-me")
    internal_api_token: SecretStr = SecretStr("local-internal-health-token")
    auth_mode: Literal["development", "production"] = "development"
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_json: SecretStr | None = None

    database_url: str = (
        "postgresql+asyncpg://supportguard_api:supportguard_api@localhost:5432/supportguard"
    )
    migrator_database_url: str | None = None
    mcp_read_database_url: str | None = None
    mcp_action_database_url: str | None = None
    migrator_database_password: SecretStr = SecretStr("supportguard_migrator")
    api_database_password: SecretStr = SecretStr("supportguard_api")
    dispatcher_database_password: SecretStr = SecretStr("supportguard_dispatcher")
    reconciler_database_password: SecretStr = SecretStr("supportguard_reconciler")
    worker_database_password: SecretStr = SecretStr("supportguard_worker")
    read_mcp_database_password: SecretStr = SecretStr("supportguard_read_mcp")
    action_mcp_database_password: SecretStr = SecretStr("supportguard_action_mcp")
    bootstrap_database_password: SecretStr = SecretStr("supportguard_bootstrap")
    maintenance_database_password: SecretStr = SecretStr("supportguard_maintenance")
    redis_url: str = "redis://localhost:6379/0"
    redis_stream: str = "supportguard:runtime-jobs:v1"
    redis_consumer_group: str = "supportguard-workers-v1"
    service_instance_id: str = "local-instance"
    code_version: str = "development"
    worker_concurrency: int = Field(default=1, ge=1, le=1)
    async_runtime_enabled: bool = True
    tenant_commands_per_minute: int = Field(default=60, ge=1, le=10000)
    principal_commands_per_minute: int = Field(default=20, ge=1, le=10000)
    fallback_commands_per_minute: int = Field(default=10, ge=1, le=1000)
    max_durable_backlog: int = Field(default=500, ge=1, le=100000)
    runtime_threshold_schema_version: Literal["runtime-thresholds.v1"] = "runtime-thresholds.v1"
    runtime_operational_horizon_seconds: int = Field(default=600, ge=60, le=86400)
    runtime_job_lease_seconds: int = Field(default=30, ge=10, le=300)
    runtime_heartbeat_interval_seconds: int = Field(default=10, ge=1, le=60)
    runtime_reconciler_interval_seconds: int = Field(default=5, ge=1, le=60)
    redis_pel_min_idle_ms: int = Field(default=35_000, ge=1_000, le=3_600_000)

    deepseek_api_key: SecretStr | None = None
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-v4-flash"
    llm_thinking_enabled: bool = False
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    demo_fake_provider: bool = False
    demo_fake_provider_delay_seconds: float = Field(default=0, ge=0, le=60)
    provider_max_inflight: int = Field(default=2, ge=1, le=32)
    provider_rpm_capacity: int = Field(default=60, ge=1, le=10000)
    # Transport preflight covers the complete OpenAI-compatible request, not
    # just ContextAssembler's 6k packet.  The AgentDecision schema and system
    # policy and the expanded typed AgentDecision schema add roughly 5.6k
    # conservative tokens. 16k keeps the complete bounded 8k context packet
    # representable without weakening ContextAssembler's per-section limits.
    provider_max_input_tokens: int = Field(default=16_000, ge=1000, le=128000)
    provider_max_output_tokens: int = Field(default=2_000, ge=256, le=16_000)
    embedding_mode: Literal["e5", "deterministic-fixture"] = "e5"
    embedding_model: str = "intfloat/multilingual-e5-small"
    embedding_revision: str = "614241f622f53c4eeff9890bdc4f31cfecc418b3"

    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr | None = None
    otel_exporter_otlp_endpoint: str | None = None
    max_external_eval_cost_cny: float = Field(default=30.0, gt=0)
    retention_event_days: int = Field(default=30, ge=7, le=3650)
    retention_delivery_days: int = Field(default=14, ge=7, le=3650)
    retention_trace_days: int = Field(default=30, ge=7, le=3650)


@lru_cache
def get_settings() -> Settings:
    if os.getenv("SUPPORTGUARD_DISABLE_DOTENV") == "1":
        return Settings(_env_file=None)
    return Settings()
