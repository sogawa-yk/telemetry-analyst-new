"""Langfuse SDK セットアップ.

OpenAI クライアントを langfuse.openai ラッパでラップすることで Responses API の
呼出を自動でトレース化する. また追加タグを付けるヘルパも提供.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from langfuse import Langfuse

from ta.config import get_settings

logger = logging.getLogger(__name__)

_client: Langfuse | None = None


def init_langfuse() -> Langfuse | None:
    """Langfuse クライアントを初期化して返す. 設定が無ければ None."""
    global _client
    if _client is not None:
        return _client

    s = get_settings()
    if not (s.langfuse_public_key and s.langfuse_secret_key):
        logger.info("Langfuse は無効 (キー未設定)")
        return None

    # Langfuse SDK は環境変数も読むので明示的にセット (OpenAI ラッパが参照する)
    os.environ.setdefault("LANGFUSE_HOST", s.langfuse_host)
    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", s.langfuse_public_key)
    os.environ.setdefault("LANGFUSE_SECRET_KEY", s.langfuse_secret_key)

    _client = Langfuse(
        public_key=s.langfuse_public_key,
        secret_key=s.langfuse_secret_key,
        host=s.langfuse_host,
    )
    logger.info("Langfuse initialized: host=%s", s.langfuse_host)
    return _client


def get_langfuse() -> Langfuse | None:
    return _client


def flush() -> None:
    if _client is not None:
        try:
            _client.flush()
        except Exception:
            logger.exception("Langfuse flush failed")


def update_trace_metadata(**attrs: Any) -> None:
    """現在のトレースにメタデータを追記する (Langfuse v3)."""
    if _client is None:
        return
    try:
        _client.update_current_trace(metadata=attrs)  # type: ignore[attr-defined]
    except Exception:
        logger.debug("Langfuse metadata update failed (trace context may be missing)")
