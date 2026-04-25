"""環境変数をまとめた設定オブジェクト."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Mode = Literal["beginner", "engineer"]


class Settings(BaseSettings):
    """アプリ全体の設定. 環境変数または .env から読み込む."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: str = Field(..., alias="OPENAI_API_KEY")
    openai_base_url: str = Field(
        "https://inference.generativeai.ap-osaka-1.oci.oraclecloud.com/openai/v1",
        alias="OPENAI_BASE_URL",
    )
    oci_genai_project: str = Field(..., alias="OCI_GENAI_PROJECT")
    oci_genai_model: str = Field("openai.gpt-4.1", alias="OCI_GENAI_MODEL")

    langfuse_host: str = Field(
        "http://langfuse-web.langfuse.svc.cluster.local:3000", alias="LANGFUSE_HOST"
    )
    langfuse_public_key: str | None = Field(None, alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str | None = Field(None, alias="LANGFUSE_SECRET_KEY")

    otel_service_name: str = Field("ta-agent", alias="OTEL_SERVICE_NAME")
    otel_exporter_otlp_endpoint: str = Field(
        "http://otel-gateway-opentelemetry-collector.observability:4317",
        alias="OTEL_EXPORTER_OTLP_ENDPOINT",
    )

    grafana_url: str | None = Field(None, alias="GRAFANA_URL")
    grafana_token: str | None = Field(None, alias="GRAFANA_TOKEN")
    grafana_external_url: str | None = Field(None, alias="GRAFANA_EXTERNAL_URL")
    mcp_grafana_url: str = Field(
        "http://mcp-grafana.telemetry-analyst.svc:8000/sse", alias="MCP_GRAFANA_URL"
    )
    mcp_grafana_enabled: bool = Field(False, alias="MCP_GRAFANA_ENABLED")

    target_namespace: str = Field("ec-shop", alias="TA_TARGET_NAMESPACE")

    agent_url: str = Field("http://ta-agent:8080", alias="TA_AGENT_URL")

    max_tool_calls: int = Field(20, alias="TA_MAX_TOOL_CALLS")
    request_timeout_sec: int = Field(120, alias="TA_REQUEST_TIMEOUT_SEC")
    default_mode: Mode = Field("engineer", alias="TA_DEFAULT_MODE")

    log_level: str = Field("INFO", alias="TA_LOG_LEVEL")

    skills_dir: str = Field("skills", alias="TA_SKILLS_DIR")
    memory_dir: str = Field("memory", alias="TA_MEMORY_DIR")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
