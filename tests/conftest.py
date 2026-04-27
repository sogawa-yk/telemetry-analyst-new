"""pytest 共通フィクスチャ.

`ta.config` は pydantic-settings で OPENAI_API_KEY 等を必須にしているため、
テストは実 OCI 接続せずダミー値で settings をインスタンス化できるようにする.
"""

from __future__ import annotations

import os

# pydantic-settings は import 時に env を解決するため、test モジュール側の
# `from ta.config import ...` より前にここで env を埋めておく必要がある.
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault(
    "OPENAI_BASE_URL", "https://inference.generativeai.ap-osaka-1.oci.oraclecloud.com/openai/v1"
)
os.environ.setdefault("OCI_GENAI_PROJECT", "ocid1.test")
os.environ.setdefault("OCI_GENAI_MODEL", "gpt-test")
os.environ.setdefault("TA_TARGET_NAMESPACE", "ec-shop")
os.environ.setdefault("TA_SKILLS_DIR", "skills")
os.environ.setdefault("TA_MEMORY_DIR", "memory")
# Langfuse は無効化 (キー空) にして、テスト中に外部接続させない
os.environ.setdefault("LANGFUSE_PUBLIC_KEY", "")
os.environ.setdefault("LANGFUSE_SECRET_KEY", "")
os.environ.setdefault("LANGFUSE_HOST", "http://test-langfuse.invalid:3000")
# OTel も外部接続を起こさないようダミー endpoint
os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "http://test-otel.invalid:4317")
# A2A サーバ用. 未設定だと 503 になり認証/エンドポイントテストができないため必須
os.environ.setdefault("A2A_AUTH_TOKEN", "test-a2a-token")
os.environ.setdefault("A2A_PUBLIC_URL", "http://test-a2a.invalid/a2a")
