"""A2A サーバの単体テスト.

- AgentCard 配信 (`GET /a2a/.well-known/agent-card.json`)
- Bearer Token 認証 (token 無し/不一致 → 401, 一致 → 200)
- AgentExecutor → Agent.run のブリッジ動作
- 503 (A2A 無効化) のケース
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

A2A_TOKEN = "test-a2a-token"  # tests/conftest.py の setdefault と一致させる


@pytest.fixture
def mock_agent() -> MagicMock:
    """A2A から呼ばれる Agent を mock 化."""
    a = MagicMock()
    a.create_conversation = AsyncMock(return_value="conv_test_a2a")

    fake_result = MagicMock()
    fake_result.text = "ec-shop の Pod 一覧:\n- ec-web-1 Running\n"
    fake_result.tool_calls = [{"name": "k8s_list_pods", "arguments": "{}", "result": "..."}]
    fake_result.response_id = "resp_a2a_test"
    a.run = AsyncMock(return_value=fake_result)
    return a


@pytest.fixture
def client(mock_agent: MagicMock):
    """TestClient + agent / lifespan の外部接続を mock."""
    with (
        patch("ta.api.main.otel_setup.init_otel"),
        patch("ta.api.main.otel_setup.configure_logging"),
        patch("ta.api.main.langfuse_setup.init_langfuse", return_value=None),
        patch("ta.api.main.langfuse_setup.flush"),
        patch("ta.a2a.executor.get_agent", return_value=mock_agent),
    ):
        from ta.api.main import app

        with TestClient(app) as c:
            yield c


# ---------------------------------------------------------------------------
# AgentCard 配信
# ---------------------------------------------------------------------------


def test_agent_card_requires_auth(client: TestClient) -> None:
    r = client.get("/a2a/.well-known/agent-card.json")
    assert r.status_code == 401
    body = r.json()
    assert body["error"] == "unauthorized"


def test_agent_card_with_valid_token(client: TestClient) -> None:
    r = client.get(
        "/a2a/.well-known/agent-card.json",
        headers={"Authorization": f"Bearer {A2A_TOKEN}"},
    )
    assert r.status_code == 200
    card = r.json()
    assert card["name"] == "telemetry-analyst"
    assert "ec-shop" in card["description"]
    assert card["version"] == "0.2.9"
    skills = card["skills"]
    assert len(skills) == 1
    assert skills[0]["id"] == "diagnose-ec-shop"
    assert "kubernetes" in skills[0]["tags"]
    assert len(skills[0]["examples"]) >= 2


def test_agent_card_with_invalid_token(client: TestClient) -> None:
    r = client.get(
        "/a2a/.well-known/agent-card.json",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert r.status_code == 401
    assert r.json()["reason"] == "invalid bearer token"


def test_agent_card_with_malformed_header(client: TestClient) -> None:
    r = client.get(
        "/a2a/.well-known/agent-card.json",
        headers={"Authorization": "Token foo"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# JSON-RPC エンドポイント (POST /a2a/) も同様に認証保護されている
# ---------------------------------------------------------------------------


def test_jsonrpc_requires_auth(client: TestClient) -> None:
    r = client.post(
        "/a2a/",
        json={"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {}},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Executor の単体テスト (Runner mock 経由ではなく Agent.run mock で軽量に)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_calls_agent_run(mock_agent: MagicMock) -> None:
    from a2a.server.events import EventQueue

    from ta.a2a.executor import TelemetryAnalystExecutor

    executor = TelemetryAnalystExecutor()

    # context を mock 化. RequestContext を直接インスタンス化するのは依存が多いので、
    # MagicMock で必要なメソッドだけ模擬する.
    context = MagicMock()
    context.task_id = "task-abc"
    context.context_id = "ctx-xyz"
    context.current_task = None
    context.get_user_input = MagicMock(return_value="ec-shop の Pod 一覧を見せて")

    event_queue = EventQueue()

    with patch("ta.a2a.executor.get_agent", return_value=mock_agent):
        await executor.execute(context, event_queue)

    # Agent.run が正しい引数で呼ばれたこと
    mock_agent.run.assert_awaited_once()
    kwargs = mock_agent.run.await_args.kwargs
    args = mock_agent.run.await_args.args
    user_text = args[0] if args else kwargs.get("user_msg")
    assert "Pod 一覧" in user_text
    assert kwargs["mode"] == "engineer"
    assert kwargs["metadata"]["a2a_context_id"] == "ctx-xyz"

    # OCI Conversations が初回呼出で作成されてマッピングされたこと
    mock_agent.create_conversation.assert_awaited_once()
    assert executor._conv_map["ctx-xyz"] == "conv_test_a2a"


# ---------------------------------------------------------------------------
# A2A_AUTH_TOKEN 未設定なら 503 (誤運用防止)
# ---------------------------------------------------------------------------


def test_a2a_disabled_when_token_unset(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A2A_AUTH_TOKEN が空/None なら middleware は 503 を返す (誤運用防止)."""
    from ta.a2a import auth as a2a_auth

    fake_settings = MagicMock()
    fake_settings.a2a_auth_token = None  # 未設定状態
    monkeypatch.setattr(a2a_auth, "get_settings", lambda: fake_settings)

    mw = a2a_auth.BearerTokenMiddleware(app=None)  # type: ignore[arg-type]

    import asyncio

    from starlette.requests import Request

    scope = {"type": "http", "method": "GET", "path": "/", "headers": []}

    async def call_next(request):  # type: ignore[no-untyped-def]
        from starlette.responses import PlainTextResponse

        return PlainTextResponse("should not reach")

    request = Request(scope)
    resp = asyncio.get_event_loop().run_until_complete(mw.dispatch(request, call_next))
    assert resp.status_code == 503
    body = resp.body.decode()
    assert "not enabled" in body
