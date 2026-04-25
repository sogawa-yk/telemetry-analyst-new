"""FastAPI バックエンドの単体テスト.

実 OCI / Langfuse / OTel に接続せずに、エンドポイント形状と Agent 連携を確認する.
SSE (`/chat/stream`) は streaming 部分だけ動作することを確認 (内容は agent mock).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def mock_agent() -> MagicMock:
    """Agent クラスを丸ごと差し替えるための mock."""
    a = MagicMock()
    a.create_conversation = AsyncMock(return_value="conv_test_123")

    async def fake_run_stream(*args, **kwargs):  # type: ignore[no-untyped-def]
        yield {"type": "delta", "text": "こんにちは"}
        yield {"type": "tool_call", "name": "k8s_list_pods", "arguments": "{}"}
        yield {"type": "tool_result", "name": "k8s_list_pods", "result": "Pods OK"}
        yield {"type": "done", "response_id": "resp_xxx", "text": "こんにちは"}

    a.run_stream = fake_run_stream
    return a


@pytest.fixture
def client(mock_agent: MagicMock):
    """TestClient + agent mock + lifespan の外部接続を抑止."""
    # 実 OTel / Langfuse 初期化を避ける. 失敗ではなく no-op に.
    with (
        patch("ta.api.main.otel_setup.init_otel"),
        patch("ta.api.main.otel_setup.configure_logging"),
        patch("ta.api.main.langfuse_setup.init_langfuse", return_value=None),
        patch("ta.api.main.langfuse_setup.flush"),
        patch("ta.api.main.get_agent", return_value=mock_agent),
    ):
        # lifespan を回したいので with 内で TestClient をネストさせる
        from ta.api.main import app

        with TestClient(app) as c:
            yield c


def test_root_returns_service_info(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "ta-agent"
    assert "version" in body


def test_healthz_returns_ok(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_create_conversation_returns_id(client: TestClient, mock_agent: MagicMock) -> None:
    r = client.post(
        "/conversations",
        json={"session_id": "sess-1", "mode": "engineer"},
    )
    assert r.status_code == 200
    assert r.json() == {"conversation_id": "conv_test_123"}
    mock_agent.create_conversation.assert_awaited_once()
    # metadata に session_id と mode が乗っていること
    kwargs = mock_agent.create_conversation.await_args.kwargs
    assert kwargs["metadata"]["session_id"] == "sess-1"
    assert kwargs["metadata"]["mode"] == "engineer"


def test_chat_stream_emits_4_event_types(client: TestClient) -> None:
    """SSE のイベント順 (delta / tool_call / tool_result / done) が崩れないこと."""
    with client.stream(
        "POST",
        "/chat/stream",
        json={"message": "test", "conversation_id": None, "mode": "engineer"},
    ) as r:
        assert r.status_code == 200
        events: list[dict] = []
        current_event: str | None = None
        for line in r.iter_lines():
            if not line:
                continue
            if line.startswith("event:"):
                current_event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                payload = json.loads(line[len("data:") :].strip())
                events.append({"event": current_event, "data": payload})

    types = [e["event"] for e in events]
    assert types == ["delta", "tool_call", "tool_result", "done"]
    assert events[0]["data"]["text"] == "こんにちは"
    assert events[1]["data"]["name"] == "k8s_list_pods"
    assert events[3]["data"]["response_id"] == "resp_xxx"
