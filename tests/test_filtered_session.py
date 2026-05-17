"""FilteredOpenAIConversationsSession の単体テスト.

OCI Conversations API 経由で戻ってくる reasoning item を Responses API の
input に積まないこと (gpt-oss-120b との互換性確保) を検証する.
"""

from __future__ import annotations

import pytest

from ta.agent._filtered_session import FilteredOpenAIConversationsSession


@pytest.mark.asyncio
async def test_get_items_drops_reasoning(monkeypatch) -> None:
    """親 get_items() が返す reasoning item は除外される."""
    raw_items = [
        {"id": "1", "type": "message", "role": "user", "content": "hello"},
        {"id": "2", "type": "reasoning", "content": [{"type": "reasoning_text", "text": "..."}]},
        {"id": "3", "type": "function_call", "call_id": "c1", "name": "tool", "arguments": "{}"},
        {"id": "4", "type": "function_call_output", "call_id": "c1", "output": "ok"},
    ]
    session = FilteredOpenAIConversationsSession.__new__(FilteredOpenAIConversationsSession)

    async def fake_super_get(self, limit=None):  # noqa: ARG001 — descriptor 経由なので self が渡る
        assert limit == 10
        return list(raw_items)

    # 親 (OpenAIConversationsSession) の get_items を差し替える
    monkeypatch.setattr(
        "ta.agent._filtered_session.OpenAIConversationsSession.get_items",
        fake_super_get,
    )

    items = await session.get_items(limit=10)

    assert [i["id"] for i in items] == ["1", "3", "4"]
    assert all(i.get("type") != "reasoning" for i in items)


@pytest.mark.asyncio
async def test_add_items_filters_reasoning(monkeypatch) -> None:
    """親 add_items() に reasoning item は渡らない."""
    captured: list[list[dict]] = []

    async def fake_super_add(self, items):  # noqa: ARG001
        captured.append(list(items))

    monkeypatch.setattr(
        "ta.agent._filtered_session.OpenAIConversationsSession.add_items",
        fake_super_add,
    )

    session = FilteredOpenAIConversationsSession.__new__(FilteredOpenAIConversationsSession)

    await session.add_items(
        [
            {"type": "message", "role": "assistant", "content": "ok"},
            {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "thought"}]},
            {"type": "function_call", "call_id": "c2", "name": "x", "arguments": "{}"},
        ]
    )

    assert len(captured) == 1
    assert [i.get("type") for i in captured[0]] == ["message", "function_call"]


@pytest.mark.asyncio
async def test_add_items_skips_super_call_when_all_filtered(monkeypatch) -> None:
    """reasoning だけの入力では親 add_items は呼ばれない (空 POST 抑止)."""
    called = False

    async def fake_super_add(self, items):  # noqa: ARG001
        nonlocal called
        called = True

    monkeypatch.setattr(
        "ta.agent._filtered_session.OpenAIConversationsSession.add_items",
        fake_super_add,
    )

    session = FilteredOpenAIConversationsSession.__new__(FilteredOpenAIConversationsSession)

    await session.add_items(
        [{"type": "reasoning", "content": [{"type": "reasoning_text", "text": "x"}]}]
    )

    assert called is False


@pytest.mark.asyncio
async def test_get_items_passes_through_non_dict(monkeypatch) -> None:
    """dict でない item (理論上来ないが念のため) はそのまま通す."""
    raw_items = ["raw_string_item", {"type": "message", "id": "a"}]

    async def fake_super_get(self, limit=None):  # noqa: ARG001 — descriptor 経由なので self が渡る
        return list(raw_items)

    monkeypatch.setattr(
        "ta.agent._filtered_session.OpenAIConversationsSession.get_items",
        fake_super_get,
    )

    session = FilteredOpenAIConversationsSession.__new__(FilteredOpenAIConversationsSession)
    items = await session.get_items()
    assert items == raw_items
