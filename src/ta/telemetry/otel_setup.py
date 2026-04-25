"""OpenTelemetry 計装 (メトリクス / ログ / トレース).

Collector 宛先: otel-gateway-opentelemetry-collector.observability:4317 (gRPC)
経由で既存の Prometheus / Loki / Tempo へ転送.

提供メトリクス:
  - ta_agent_response_latency_seconds (Histogram)  モード別・最終回答までの総時間
  - ta_agent_tool_invocations_total   (Counter)    ツール別・成否別呼出数
  - ta_agent_llm_tokens_total         (Counter)    kind=input|output
  - ta_agent_active_sessions          (UpDownCounter) アクティブな会話スレッド数
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from ta.config import get_settings

logger = logging.getLogger(__name__)

_initialized = False
_meter: metrics.Meter | None = None

# メトリクスインストゥルメントは init 時に作って使い回す
_m_response_latency: metrics.Histogram | None = None
_m_tool_invocations: metrics.Counter | None = None
_m_llm_tokens: metrics.Counter | None = None
_m_active_sessions: metrics.UpDownCounter | None = None


def init_otel(app: FastAPI | None = None) -> None:
    """OTel を初期化する. 冪等."""
    global _initialized, _meter
    global _m_response_latency, _m_tool_invocations, _m_llm_tokens, _m_active_sessions

    if _initialized:
        return

    s = get_settings()
    resource = Resource.create(
        {
            "service.name": s.otel_service_name,
            "service.version": "0.2.0",
            "deployment.environment": "production",
        }
    )

    # Traces
    trace_exporter = OTLPSpanExporter(endpoint=s.otel_exporter_otlp_endpoint, insecure=True)
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(trace_exporter))
    trace.set_tracer_provider(tracer_provider)

    # Metrics
    metric_exporter = OTLPMetricExporter(endpoint=s.otel_exporter_otlp_endpoint, insecure=True)
    reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=15000)
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(meter_provider)
    _meter = metrics.get_meter("ta.agent")

    _m_response_latency = _meter.create_histogram(
        name="ta_agent_response_latency_seconds",
        description="Agent end-to-end response latency (seconds)",
        unit="s",
    )
    _m_tool_invocations = _meter.create_counter(
        name="ta_agent_tool_invocations_total",
        description="Tool invocations by name and outcome",
    )
    _m_llm_tokens = _meter.create_counter(
        name="ta_agent_llm_tokens_total",
        description="LLM tokens used by kind (input/output)",
    )
    _m_active_sessions = _meter.create_up_down_counter(
        name="ta_agent_active_sessions",
        description="Active Chainlit sessions",
    )

    # 自動インストゥルメント
    if app is not None:
        FastAPIInstrumentor.instrument_app(app, tracer_provider=tracer_provider)
    HTTPXClientInstrumentor().instrument(tracer_provider=tracer_provider)

    _initialized = True
    logger.info(
        "OpenTelemetry initialized: service=%s endpoint=%s",
        s.otel_service_name,
        s.otel_exporter_otlp_endpoint,
    )


# ------------------------------------------------------------------
# メトリクス記録ヘルパ (agent.core から呼ぶ)
# ------------------------------------------------------------------


def record_response_latency(seconds: float, mode: str) -> None:
    if _m_response_latency is not None:
        _m_response_latency.record(seconds, attributes={"mode": mode})


def record_tool_invocation(tool_name: str, outcome: str) -> None:
    if _m_tool_invocations is not None:
        _m_tool_invocations.add(1, attributes={"tool": tool_name, "outcome": outcome})


def record_llm_tokens(kind: str, count: int, model: str) -> None:
    if _m_llm_tokens is not None and count > 0:
        _m_llm_tokens.add(count, attributes={"kind": kind, "model": model})


def incr_active_sessions(delta: int = 1) -> None:
    if _m_active_sessions is not None:
        _m_active_sessions.add(delta)


def get_tracer(name: str = "ta.agent") -> trace.Tracer:
    return trace.get_tracer(name)


# ------------------------------------------------------------------
# Python logging → OTLP (簡易)
# ------------------------------------------------------------------


def configure_logging() -> None:
    """structured logging. OTel Collector の filelog receiver / stdout で拾う前提."""
    import structlog

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )


def get_logger(name: str = "ta") -> Any:
    import structlog

    return structlog.get_logger(name)
