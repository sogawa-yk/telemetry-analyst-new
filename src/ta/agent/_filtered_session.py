"""OCI Conversations Session から reasoning item を取り除くラッパ.

gpt-oss-120b は Responses API で reasoning item (`type=="reasoning"`,
中身に `Content(type="reasoning_text", ...)`) を **output** するが、同じ
item を input に積み直すと OCI Enterprise AI が
`reasoning_text is not supported by model openai.gpt-oss-120b` を返す
(BadRequestError 400).

`OpenAIConversationsSession` の `get_items()` は OCI Conversations API から
履歴 (前 turn の output items が永続化されている) を取得して
そのまま次の Responses API 呼出に積むため、ReAct 2 turn 目で必ず失敗する.
ここで `type=="reasoning"` を捨てて Conversations API の動作を保ったまま
互換性を確保する.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from agents import OpenAIConversationsSession


def _is_reasoning_item(item: Any) -> bool:
    return isinstance(item, dict) and item.get("type") == "reasoning"


class FilteredOpenAIConversationsSession(OpenAIConversationsSession):
    """`type=="reasoning"` の item を入出力両方向で捨てる Session ラッパ."""

    async def get_items(self, limit: int | None = None) -> list[Any]:
        items = await super().get_items(limit=limit)
        return [item for item in items if not _is_reasoning_item(item)]

    async def add_items(self, items: Iterable[Any]) -> None:
        filtered = [item for item in items if not _is_reasoning_item(item)]
        if not filtered:
            return
        await super().add_items(filtered)
