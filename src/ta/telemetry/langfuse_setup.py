"""Langfuse セットアップ (Phase 0-3 (e) 版).

OpenAI Agents SDK の `langfuse.openai` ラッパは効かないため、

  1. Langfuse の OTLP/HTTP endpoint (`/api/public/otel/v1/traces`) を
     現行の OTel TracerProvider に **追加** の BatchSpanProcessor として登録
  2. `OpenAIAgentsInstrumentor().instrument()` で Agents SDK 操作を OTel スパン化
  3. Langfuse() クライアントは update_current_trace 等のメタデータ補強用に保持

の 3 段で計装する. これにより既存の OTel 経路 (otel-gateway → Tempo) を保ったまま、
LLM 関連スパンのみ Langfuse にも複製される.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

from agents import set_trace_processors
from langfuse import Langfuse
from openinference.instrumentation.openai_agents import OpenAIAgentsInstrumentor
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from ta.config import get_settings

logger = logging.getLogger(__name__)

_client: Langfuse | None = None
_instrumented = False
_otlp_added = False


def _otlp_endpoint(host: str) -> str:
    return f"{host.rstrip('/')}/api/public/otel/v1/traces"


def _basic_auth(public_key: str, secret_key: str) -> str:
    raw = f"{public_key}:{secret_key}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _ensure_tracer_provider() -> TracerProvider:
    """グローバルな TracerProvider を取得. 未設定なら最低限のものを作って返す.

    api/main.py の lifespan では otel_setup.init_otel が先に走り SDK の
    TracerProvider が設定されている想定だが、CLI / smoke 単体実行では未設定の
    可能性があるためフォールバックを用意する.
    """
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        return provider
    fallback = TracerProvider(resource=Resource.create({"service.name": "ta-agent"}))
    trace.set_tracer_provider(fallback)
    logger.info("OTel TracerProvider が未設定のためフォールバックを生成")
    return fallback


def init_langfuse() -> Langfuse | None:
    """Langfuse OTLP exporter + OpenInference instrumentation を初期化する. 冪等."""
    global _client, _instrumented, _otlp_added

    s = get_settings()
    if not (s.langfuse_public_key and s.langfuse_secret_key):
        logger.info("Langfuse は無効 (キー未設定)")
        # Langfuse 無効でも SDK 標準の OpenAI trace exporter は OCI 鍵で 401 を出すため排除
        try:
            set_trace_processors([])
        except Exception:
            logger.debug("set_trace_processors([]) failed", exc_info=True)
        return None

    # 1) Langfuse 行きの OTLP HTTP exporter を BatchSpanProcessor として追加
    if not _otlp_added:
        provider = _ensure_tracer_provider()
        try:
            exporter = OTLPSpanExporter(
                endpoint=_otlp_endpoint(s.langfuse_host),
                headers={
                    "Authorization": _basic_auth(s.langfuse_public_key, s.langfuse_secret_key)
                },
            )
            provider.add_span_processor(BatchSpanProcessor(exporter))
            _otlp_added = True
            logger.info("Langfuse OTLP exporter added: %s", _otlp_endpoint(s.langfuse_host))
        except Exception:
            logger.exception("Langfuse OTLP exporter の登録に失敗")

    # 2) OpenInference: Agents SDK 操作を OTel スパンとして emit.
    #    exclusive_processor=True で OpenAI 公式 trace exporter (OCI 鍵で 401 になる) を排除し、
    #    OpenInference の processor だけを SDK の tracing 系統に登録する.
    if not _instrumented:
        try:
            OpenAIAgentsInstrumentor().instrument(exclusive_processor=True)
            _instrumented = True
            logger.info("OpenAIAgentsInstrumentor instrumented (exclusive)")
        except Exception:
            logger.exception("OpenAIAgentsInstrumentor instrument failed")

    # 3) Langfuse クライアント本体 (update_current_trace 用. 上記 OTLP とは独立に動く)
    if _client is None:
        os.environ.setdefault("LANGFUSE_HOST", s.langfuse_host)
        os.environ.setdefault("LANGFUSE_PUBLIC_KEY", s.langfuse_public_key)
        os.environ.setdefault("LANGFUSE_SECRET_KEY", s.langfuse_secret_key)
        _client = Langfuse(
            public_key=s.langfuse_public_key,
            secret_key=s.langfuse_secret_key,
            host=s.langfuse_host,
        )
        logger.info("Langfuse client initialized: host=%s", s.langfuse_host)

    return _client


def get_langfuse() -> Langfuse | None:
    return _client


def flush() -> None:
    """Langfuse client と OTel SDK 双方をフラッシュ (アプリ終了時に呼ぶ)."""
    if _client is not None:
        try:
            _client.flush()
        except Exception:
            logger.exception("Langfuse flush failed")
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        try:
            provider.force_flush(timeout_millis=5000)
        except Exception:
            logger.exception("OTel force_flush failed")


def update_trace_metadata(**attrs: Any) -> None:
    """現在のトレースにメタデータを追記する (Langfuse v3 のラッパ)."""
    if _client is None:
        return
    try:
        _client.update_current_trace(metadata=attrs)  # type: ignore[attr-defined]
    except Exception:
        logger.debug("Langfuse metadata update failed (trace context may be missing)")
