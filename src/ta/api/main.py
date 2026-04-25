"""FastAPI バックエンド (port 8080).

Chainlit UI から呼ばれる最低限のエンドポイントを提供:

- GET  /healthz                 — ヘルスチェック
- POST /conversations           — OCI Conversations API のスレッドを新規作成
- POST /chat/stream             — SSE で ReAct の進行をストリーム
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from ta.agent.core import get_agent
from ta.config import Mode, get_settings
from ta.telemetry import langfuse_setup, otel_setup


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    otel_setup.configure_logging()
    otel_setup.init_otel(app)
    langfuse_setup.init_langfuse()
    yield
    langfuse_setup.flush()


app = FastAPI(title="Telemetry Analyst API", version="0.2.0", lifespan=lifespan)


# -----------------------------------------------------------------------------
# モデル
# -----------------------------------------------------------------------------


class ConversationCreateRequest(BaseModel):
    session_id: str | None = None
    mode: Mode = "engineer"


class ConversationCreateResponse(BaseModel):
    conversation_id: str


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None
    mode: Mode = Field(default_factory=lambda: get_settings().default_mode)
    session_id: str | None = None


# -----------------------------------------------------------------------------
# エンドポイント
# -----------------------------------------------------------------------------


@app.get("/")
def root() -> dict[str, str]:
    # OCI Native Ingress Controller の LB ヘルスチェック用 (path="/")
    return {"service": "ta-agent", "version": "0.2.0"}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": "0.2.0"}


@app.post("/conversations", response_model=ConversationCreateResponse)
def create_conversation(req: ConversationCreateRequest) -> ConversationCreateResponse:
    agent = get_agent()
    metadata = {"source": "chainlit", "mode": req.mode}
    if req.session_id:
        metadata["session_id"] = req.session_id
    conv_id = agent.create_conversation(metadata=metadata)
    return ConversationCreateResponse(conversation_id=conv_id)


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest) -> EventSourceResponse:
    """SSE で ReAct のイベントを逐次配信する.

    イベントタイプ:
      - delta        : 最終応答テキストの差分
      - tool_call    : ツール呼出開始
      - tool_result  : ツール結果
      - done         : 最終 (response_id と text 含む)
    """

    async def gen() -> AsyncIterator[dict]:
        agent = get_agent()
        metadata = {}
        if req.session_id:
            metadata["session_id"] = req.session_id
        # agent.run_stream は同期ジェネレータなので、ブロッキングを逃すために
        # asyncio.to_thread は使わず直接イテレート (1 リクエスト = 1 タスク、軽負荷想定)
        for event in agent.run_stream(
            req.message,
            mode=req.mode,
            conversation_id=req.conversation_id,
            metadata=metadata,
        ):
            yield {"event": event["type"], "data": json.dumps(event, ensure_ascii=False)}

    return EventSourceResponse(gen())


# -----------------------------------------------------------------------------
# エントリポイント (pyproject.toml の script)
# -----------------------------------------------------------------------------


def run() -> None:
    import uvicorn

    s = get_settings()
    uvicorn.run(
        "ta.api.main:app",
        host="0.0.0.0",  # noqa: S104 — コンテナ内で明示的に全 IF で listen
        port=8080,
        log_level=s.log_level.lower(),
    )
