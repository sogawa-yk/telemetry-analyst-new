"""Chainlit UI (port 8081).

バックエンド (FastAPI, port 8080) の SSE ストリームをラップして対話型で表示する.
モード切替 (初心者 / エンジニア) は Chat Profile で提供.
OCI Conversations API のスレッド id を Chainlit session に保持することで会話継続を実現する.
"""

from __future__ import annotations

import json
import os
from typing import Any

import chainlit as cl
import httpx

# ---------------------------------------------------------------------------
# バックエンド接続
# ---------------------------------------------------------------------------

BACKEND_URL = os.environ.get("TA_AGENT_URL", "http://ta-agent:8080").rstrip("/")


# ---------------------------------------------------------------------------
# Chat Profile (モード切替)
# ---------------------------------------------------------------------------


@cl.set_chat_profiles
async def chat_profiles() -> list[cl.ChatProfile]:
    return [
        cl.ChatProfile(
            name="engineer",
            markdown_description=(
                "**エンジニアモード** — PromQL / LogQL / kubectl コマンドをそのまま提示. "
                "用語解説は省略し、SRE / 開発者向けに簡潔に回答."
            ),
            icon="🛠",
        ),
        cl.ChatProfile(
            name="beginner",
            markdown_description=(
                "**初心者モード** — 用語解説つきでやさしく説明. 運用担当者や開発以外のメンバー向け."
            ),
            icon="🔰",
        ),
    ]


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


@cl.on_chat_start
async def on_chat_start() -> None:
    mode = cl.user_session.get("chat_profile") or "engineer"
    cl.user_session.set("mode", mode)

    # OCI Conversations API のスレッドを作成して id を保持
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.post(
                f"{BACKEND_URL}/conversations",
                json={"session_id": cl.user_session.get("id"), "mode": mode},
            )
            r.raise_for_status()
            conv_id = r.json()["conversation_id"]
        except Exception as e:
            await cl.Message(
                content=f"⚠️ 会話スレッドの作成に失敗しました: {e}\nBackend URL: `{BACKEND_URL}`"
            ).send()
            conv_id = None
    cl.user_session.set("conversation_id", conv_id)

    await cl.Message(
        content=(
            f"こんにちは。Telemetry Analyst です。監視対象は `ec-shop` 名前空間です。\n"
            f"現在のモード: **{mode}**\n\n"
            f"調査したいことを自由に日本語でお書きください。"
        )
    ).send()


# ---------------------------------------------------------------------------
# Message handler (SSE をラップ)
# ---------------------------------------------------------------------------


@cl.on_message
async def on_message(message: cl.Message) -> None:
    mode = cl.user_session.get("mode") or "engineer"
    conv_id = cl.user_session.get("conversation_id")

    # 最終応答メッセージ (逐次 token を流し込む)
    answer_msg = cl.Message(content="")
    await answer_msg.send()

    # 同じ tool 呼出を 2 回表示しないため、call_id ごとに 1 つの Step を保持し、
    # tool_call で input を立て、tool_result で同じ step を update する.
    steps: dict[str, cl.Step] = {}
    fallback_idx = [0]  # call_id が無い event 用の連番

    # SSE ストリームを最後まで受け取るため timeout は無効化する (read 側を None)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
    ) as client:
        try:
            async with client.stream(
                "POST",
                f"{BACKEND_URL}/chat/stream",
                json={
                    "message": message.content,
                    "conversation_id": conv_id,
                    "mode": mode,
                    "session_id": cl.user_session.get("id"),
                },
            ) as resp:
                if resp.status_code >= 400:
                    err_body = await resp.aread()
                    await cl.Message(
                        content=f"⚠️ API エラー {resp.status_code}: {err_body[:500]!r}"
                    ).send()
                    return

                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[len("data:") :].strip()
                    if not payload:
                        continue
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    await _handle_event(event, answer_msg, steps, fallback_idx)
        except Exception as e:
            await cl.Message(content=f"⚠️ 通信エラー: {e}").send()
            return

    await answer_msg.update()


async def _handle_event(
    event: dict[str, Any],
    answer_msg: cl.Message,
    steps: dict[str, cl.Step],
    fallback_idx: list[int],
) -> None:
    t = event.get("type")
    if t == "delta":
        token = event.get("text", "")
        if token:
            await answer_msg.stream_token(token)
    elif t == "tool_call":
        name = event.get("name", "?")
        args = event.get("arguments", "")
        call_id = event.get("call_id") or f"_fb_{fallback_idx[0]}"
        if not event.get("call_id"):
            fallback_idx[0] += 1
        # cl.Step を手動 send して call_id 単位で保持. tool_result で同 step を更新する.
        step = cl.Step(name=f"tool: {name}", type="tool")
        step.input = args
        await step.send()
        steps[call_id] = step
    elif t == "tool_result":
        name = event.get("name", "?")
        result = event.get("result", "")
        call_id = event.get("call_id") or ""
        step = steps.get(call_id)
        if step is None:
            # call_id が一致する step が無ければ新規作成 (フォールバック)
            step = cl.Step(name=f"tool: {name}", type="tool")
            await step.send()
            if call_id:
                steps[call_id] = step
        step.output = result[:2000]
        await step.update()
    elif t == "done":
        # delta で逐次ストリームしてきたので answer_msg はすでに完成
        pass
