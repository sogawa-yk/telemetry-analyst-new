"""単一 ReAct エージェント本体 (OpenAI Agents SDK 版).

OCI Enterprise AI Responses API + Conversations API + Hosted MCP を、
公式の OpenAI Agents SDK (`openai-agents`) 経由で利用する.

自前 ReAct ループ・関数呼出ループ・previous_response_id 排他処理は SDK が
丸ごと吸収するため不要 (Phase 0-3 で削除).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import warnings
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents import (
    Agent as SDKAgent,
)
from agents import (
    ItemHelpers,
    OpenAIConversationsSession,
    RunConfig,
    Runner,
    set_default_openai_api,
    set_default_openai_client,
    set_tracing_disabled,
)
from openai import APIStatusError, AsyncOpenAI
from openai.types.responses import (
    ResponseIncompleteEvent,
    ResponseTextDeltaEvent,
)

from ta.agent._oci_compat import make_oci_http_client
from ta.agent.prompts import __path__ as _PROMPTS_PKG
from ta.agent.skills.loader import SkillRetriever
from ta.agent.tools import k8s as k8s_tools
from ta.agent.tools.grafana_mcp import make_grafana_mcp_tool
from ta.config import Mode, get_settings
from ta.telemetry import otel_setup

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(_PROMPTS_PKG[0])
_SKILLS_DIR = Path(get_settings().skills_dir)
_MEMORY_DIR = Path(get_settings().memory_dir)

# OCI ap-osaka-1 Responses API streaming は stream_final_event_missing を散発的に
# 返す (検証で 30-50%). 部分出力が無い場合に限り内部 retry する.
# retry を使い切っても failure 続きなら non-streaming にフォールバック (これは 100%).
_OCI_STREAM_RETRY_MAX = 5


@dataclass
class AgentResult:
    text: str
    conversation_id: str | None
    tool_calls: list[dict[str, Any]]
    response_id: str | None


class Agent:
    def __init__(self) -> None:
        s = get_settings()
        # Agents SDK 用の AsyncOpenAI を OCI Enterprise AI に向ける.
        # OCI 固有の Responses API 検証差分 (mcp_call の output 欠落で 400) を吸収する
        # ため、httpx カスタム transport を挟んで request body を sanitize する.
        self._async_client = AsyncOpenAI(
            api_key=s.openai_api_key,
            base_url=s.openai_base_url,
            default_headers={"OpenAI-Project": s.oci_genai_project},
            http_client=make_oci_http_client(),
        )
        set_default_openai_client(self._async_client)
        set_default_openai_api("responses")
        # Agents SDK の OpenAI hosted tracing (api.openai.com/v1/traces/ingest) を無効化.
        # OCI 用 API key では 401 になりノイズになる. アプリ側の trace は
        # ta.telemetry.langfuse_setup.init_langfuse() (OTel + openinference) に集約.
        set_tracing_disabled(True)

        # Pydantic の Conversations API シリアライズ警告 (str → content list 変換時に出る non-fatal)
        warnings.filterwarnings(
            "ignore",
            message=r"Pydantic serializer warnings.*",
            category=UserWarning,
        )

        self._model = s.oci_genai_model
        self._max_react_turns = s.max_tool_calls
        self._target_ns = s.target_namespace
        self._retriever = SkillRetriever(skills_dir=_SKILLS_DIR)

    # ------------------------------------------------------------------
    # instructions ビルダ (context engineering の中核)
    # ------------------------------------------------------------------

    def _read_prompt(self, filename: str) -> str:
        return (_PROMPTS_DIR / filename).read_text(encoding="utf-8").strip()

    def _read_memory(self, filename: str) -> str:
        path = _MEMORY_DIR / filename
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8").strip()

    def build_instructions(self, user_msg: str, mode: Mode) -> str:
        parts = [
            self._read_prompt("system.md"),
            self._read_prompt(f"mode_{mode}.md"),
            "## 現在の監視対象\n\nNamespace: `" + self._target_ns + "`",
        ]
        env = self._read_memory("environment.md")
        if env:
            parts.append(env)
        skill_section = self._retriever.render(user_msg, mode, k=3)
        if skill_section:
            parts.append(skill_section)
        return "\n\n---\n\n".join(parts)

    def picked_skills(self, user_msg: str, mode: Mode) -> list[str]:
        """Langfuse / OTel に流すために選ばれた skill 名を取り出す."""
        return [s.name for s in self._retriever.pick(user_msg, mode, k=3)]

    # ------------------------------------------------------------------
    # SDK Agent の構築
    # ------------------------------------------------------------------

    def _build_sdk_agent(self, instructions: str) -> SDKAgent:
        tools: list[Any] = list(k8s_tools.ALL_TOOLS)
        if get_settings().mcp_grafana_enabled:
            tools.append(make_grafana_mcp_tool())
        return SDKAgent(
            name="telemetry-analyst",
            instructions=instructions,
            model=self._model,
            tools=tools,
        )

    def _run_config(self, mode: Mode, metadata: dict[str, Any] | None) -> RunConfig:
        return RunConfig(
            trace_metadata={"mode": mode, **(metadata or {})},
        )

    # ------------------------------------------------------------------
    # Conversations API (メモリ委任)
    # ------------------------------------------------------------------

    async def create_conversation(self, metadata: dict[str, Any] | None = None) -> str:
        conv = await self._async_client.conversations.create(metadata=metadata or {})
        return conv.id

    def _session_for(self, conversation_id: str | None) -> OpenAIConversationsSession | None:
        if not conversation_id:
            return None
        return OpenAIConversationsSession(
            conversation_id=conversation_id,
            openai_client=self._async_client,
        )

    # ------------------------------------------------------------------
    # ReAct 実行 (SDK の Runner に丸投げ)
    # ------------------------------------------------------------------

    async def run(
        self,
        user_msg: str,
        *,
        mode: Mode = "engineer",
        conversation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentResult:
        start = time.monotonic()
        instructions = self.build_instructions(user_msg, mode)
        sdk_agent = self._build_sdk_agent(instructions)
        session = self._session_for(conversation_id)

        # Phase C-2: skill 選択結果と prompt 長を OTel メトリクスへ
        otel_setup.record_prompt_chars(len(instructions), mode)
        for skill_name in self.picked_skills(user_msg, mode):
            otel_setup.record_skill_hit(skill_name, mode)

        # OCI Responses API streaming は ap-osaka-1 で stream_final_event_missing が
        # 散発的に発生し、final_output が空文字で返る (~30% 程度). 空応答は再試行する.
        result = None
        for attempt in range(_OCI_STREAM_RETRY_MAX):
            result = await Runner.run(
                sdk_agent,
                input=user_msg,
                session=session,
                max_turns=self._max_react_turns,
                run_config=self._run_config(mode, metadata),
            )
            text = str(result.final_output) if result.final_output is not None else ""
            if text or attempt == _OCI_STREAM_RETRY_MAX - 1:
                break
            logger.warning(
                "OCI streaming returned empty output (attempt %d/%d), retrying",
                attempt + 1,
                _OCI_STREAM_RETRY_MAX,
            )

        assert result is not None
        tool_calls_record = _extract_tool_calls(result.new_items)
        otel_setup.record_response_latency(time.monotonic() - start, mode)
        otel_setup.record_react_turns(len(tool_calls_record), mode)
        return AgentResult(
            text=str(result.final_output) if result.final_output is not None else "",
            conversation_id=conversation_id,
            tool_calls=tool_calls_record,
            response_id=getattr(result, "last_response_id", None),
        )

    # ------------------------------------------------------------------
    # ストリーミング (Chainlit / CLI 用)
    # ------------------------------------------------------------------

    async def run_stream(
        self,
        user_msg: str,
        *,
        mode: Mode = "engineer",
        conversation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """SDK の stream_events を Chainlit 互換 4 種に正規化して逐次 yield する.

        event の種類:
          - {"type": "delta", "text": "..."}
          - {"type": "tool_call", "name", "arguments"}
          - {"type": "tool_result", "name", "result"}
          - {"type": "done", "response_id", "text"}
        """
        start = time.monotonic()
        instructions = self.build_instructions(user_msg, mode)
        sdk_agent = self._build_sdk_agent(instructions)
        session = self._session_for(conversation_id)

        # Phase C-2: skill / prompt 計装
        otel_setup.record_prompt_chars(len(instructions), mode)
        for skill_name in self.picked_skills(user_msg, mode):
            otel_setup.record_skill_hit(skill_name, mode)

        text_parts: list[str] = []
        # call_id -> tool 名 を覚えておき、tool_call_output_item で名前を引く
        tool_name_by_call_id: dict[str, str] = {}
        turn_count = 0
        # 各 OCI 呼出 (= turn) の状態を追跡. 最後の turn が "completed" でないと
        # ユーザに途中で切断された応答が届く. multi-turn ReAct の N>=2 turn で
        # incomplete が起きるケースを後段の fallback で救うために使用.
        last_turn_state = "no_turn"  # "no_turn" | "in_progress" | "completed" | "incomplete"
        last_turn_incomplete_reason: str | None = None
        midflight_api_error: APIStatusError | None = None

        # OCI ap-osaka-1 の streaming flakiness 対策: stream_final_event_missing で
        # 部分出力が無い場合は内部的に再試行する. 外部から見ると単純な遅延に見える.
        # 424 Failed Dependency (MCP tool list 取得失敗) も同様に retry.
        result = None
        for attempt in range(_OCI_STREAM_RETRY_MAX):
            stream_failed_no_output = False
            result = Runner.run_streamed(
                sdk_agent,
                input=user_msg,
                session=session,
                max_turns=self._max_react_turns,
                run_config=self._run_config(mode, metadata),
            )
            try:
                async for event in result.stream_events():
                    if event.type == "raw_response_event":
                        etype = getattr(event.data, "type", "")
                        if etype == "response.created":
                            last_turn_state = "in_progress"
                        elif etype == "response.completed":
                            last_turn_state = "completed"
                        if isinstance(event.data, ResponseTextDeltaEvent):
                            delta = event.data.delta or ""
                            if delta:
                                text_parts.append(delta)
                                yield {"type": "delta", "text": delta}
                        elif isinstance(event.data, ResponseIncompleteEvent):
                            last_turn_state = "incomplete"
                            details = getattr(
                                event.data.response, "incomplete_details", None
                            )
                            reason = getattr(details, "reason", None) if details else None
                            last_turn_incomplete_reason = reason
                            # 出力済の text/tool が無い場合のみ retry (重複 yield を防ぐ)
                            if (
                                reason == "stream_final_event_missing"
                                and not text_parts
                                and turn_count == 0
                                and attempt < _OCI_STREAM_RETRY_MAX - 1
                            ):
                                logger.warning(
                                    "OCI stream_final_event_missing (attempt %d/%d), retrying",
                                    attempt + 1,
                                    _OCI_STREAM_RETRY_MAX,
                                )
                                stream_failed_no_output = True
                                break
                    elif event.type == "run_item_stream_event":
                        item = event.item
                        itype = getattr(item, "type", None)
                        if itype == "tool_call_item":
                            raw = getattr(item, "raw_item", None)
                            name = getattr(raw, "name", None) or getattr(raw, "type", "?")
                            args = getattr(raw, "arguments", "") or ""
                            call_id = (
                                getattr(raw, "call_id", None)
                                or getattr(raw, "id", None)
                                or ""
                            )
                            if call_id:
                                tool_name_by_call_id[call_id] = str(name)
                            otel_setup.record_tool_invocation(str(name), "called")
                            turn_count += 1
                            yield {
                                "type": "tool_call",
                                "name": str(name),
                                "arguments": args,
                                "call_id": str(call_id),
                            }
                        elif itype == "tool_call_output_item":
                            raw = getattr(item, "raw_item", None)
                            out = getattr(item, "output", "") or ""
                            # function_call_output は raw が dict / SDK 型のいずれもありうる
                            call_id = ""
                            if isinstance(raw, dict):
                                call_id = raw.get("call_id") or ""
                            else:
                                call_id = getattr(raw, "call_id", "") or ""
                            name = tool_name_by_call_id.get(call_id) or "(tool)"
                            yield {
                                "type": "tool_result",
                                "name": name,
                                "result": str(out),
                                "call_id": str(call_id),
                            }
                        # message_output_item は delta で逐次受信済みのため再 yield は不要
            except APIStatusError as e:
                # OCI が MCP tool list 取得で 424 等を返すケース. 部分出力が無ければ retry.
                if (
                    not text_parts
                    and turn_count == 0
                    and attempt < _OCI_STREAM_RETRY_MAX - 1
                ):
                    logger.warning(
                        "OCI APIStatusError %d (attempt %d/%d), retrying: %s",
                        e.status_code,
                        attempt + 1,
                        _OCI_STREAM_RETRY_MAX,
                        str(e)[:200],
                    )
                    stream_failed_no_output = True
                else:
                    # 部分出力済 (= multi-turn の途中) で 400/424 が来た場合は
                    # raise せずに記録し、後段の fallback で再実行する.
                    # ASGI exception で SSE が中途半端に切れて
                    # "peer closed connection" 系の UI エラーになるのを防ぐ.
                    logger.warning(
                        "OCI APIStatusError %d mid-stream (text=%d chars, turns=%d): %s",
                        e.status_code,
                        sum(len(t) for t in text_parts),
                        turn_count,
                        str(e)[:200],
                    )
                    midflight_api_error = e
                    last_turn_state = "incomplete"

            # retry が必要なら次の attempt へ. 必要なければループ終了.
            if not stream_failed_no_output:
                break

        # 後段 fallback の起動条件:
        #   (1) text が一切出ていない (旧条件)
        #   (2) 最後の turn が response.completed で終わっていない (multi-turn 途中切断)
        #   (3) ストリーム途中で APIStatusError が発生した (MCP fetch 失敗等)
        #   (4) tool 呼出があるのに最終 text が異常短 (C truncated_completion パターン)
        #   (5) 最終 text が open delimiter / 未閉じ code block で終わっている
        # multi-turn ReAct の synthesis turn で incomplete が起きるケースを救う.
        # 既送信済の text と新たに生成される full 応答の重複は separator で明示する.
        full_text = "".join(text_parts)
        looks_truncated = _looks_truncated(full_text, turn_count)
        needs_fallback = (
            not text_parts
            or last_turn_state != "completed"
            or midflight_api_error is not None
            or looks_truncated
        )
        if needs_fallback:
            logger.warning(
                "fallback to non-streaming: text=%d chars, turns=%d, last_turn=%s, "
                "incomplete_reason=%s, mid_api_err=%s, looks_truncated=%s",
                len(full_text),
                turn_count,
                last_turn_state,
                last_turn_incomplete_reason,
                bool(midflight_api_error),
                looks_truncated,
            )
            try:
                fallback_result = await Runner.run(
                    sdk_agent,
                    input=user_msg,
                    session=session,
                    max_turns=self._max_react_turns,
                    run_config=self._run_config(mode, metadata),
                )
                fallback_text = (
                    str(fallback_result.final_output)
                    if fallback_result.final_output is not None
                    else ""
                )
                if fallback_text:
                    if text_parts:
                        # 既送信済の途切れた text と区別するための separator
                        sep = "\n\n---\n_[応答が途中で切れたため再生成しました]_\n\n"
                        text_parts.append(sep)
                        yield {"type": "delta", "text": sep}
                    text_parts.append(fallback_text)
                    yield {"type": "delta", "text": fallback_text}
                result = fallback_result
            except Exception as e:
                logger.error("non-streaming fallback failed: %s", e)
                # それでも何も text が無い場合は最低限のエラーメッセージを返す
                if not text_parts:
                    msg = (
                        "(OCI Responses API の応答が安定せず取得できませんでした. "
                        "もう一度お試しください.)"
                    )
                    text_parts.append(msg)
                    yield {"type": "delta", "text": msg}

        otel_setup.record_response_latency(time.monotonic() - start, mode)
        otel_setup.record_react_turns(turn_count, mode)
        yield {
            "type": "done",
            "response_id": getattr(result, "last_response_id", None),
            "text": "".join(text_parts),
        }


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _looks_truncated(text: str, turn_count: int) -> bool:
    """text が「OCI/モデル側で途中切断された」 ように見えるかの heuristic.

    OCI Responses API は時々 response.completed を送るのに output_tokens が
    異常少ない (C パターン). 真の SDK 失敗では検出できないため、出力末尾の
    形と長さで判定する.
    """
    import re

    s = text.rstrip()
    if not s:
        return False  # 完全空は別系統で fallback
    # ReAct で tool 使ったのに極端に短い
    # 構造化レポート (症状/根拠/仮説/推奨アクション/次に掘るべき点) は通常 800+ chars.
    # 500 chars を下回るのは途中切断の可能性高.
    if turn_count >= 1 and len(s) < 500:
        return True
    # 末尾が open paren / open bracket / 未閉じ inline code
    # 半角に加え全角の括弧 (FULLWIDTH 括弧群) も truncation 候補
    if s[-1] in "([「『（｛`":  # noqa: RUF001
        return True
    # ``` の数が奇数 = 未閉じ code fence
    if s.count("```") % 2 == 1:
        return True
    # 末尾が空のリスト項目 / セクションヘッダ:
    #   "## XX" だけで本文無し
    #   "1." / "1)" / "(1)" / "- " / "* " で内容なし
    last_line = s.rsplit("\n", 1)[-1].strip()
    if re.match(r"^#{1,6}\s+\S+\s*$", last_line) and len(last_line) < 40:
        return True
    return bool(re.match(r"^(\d+[\.\)]|[-*])\s*$", last_line))


def _extract_tool_calls(new_items: list[Any]) -> list[dict[str, Any]]:
    """Runner.run の result.new_items から tool 呼出を取り出して 1 行サマリにする."""
    pending: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    for item in new_items:
        itype = getattr(item, "type", None)
        if itype == "tool_call_item":
            raw = getattr(item, "raw_item", None)
            name = getattr(raw, "name", None) or getattr(raw, "type", "?")
            args = getattr(raw, "arguments", "") or ""
            call_id = getattr(raw, "call_id", None) or getattr(raw, "id", None) or ""
            pending[call_id] = {"name": str(name), "arguments": args}
        elif itype == "tool_call_output_item":
            raw = getattr(item, "raw_item", None)
            output = getattr(item, "output", "") or ""
            if isinstance(raw, dict):
                call_id = raw.get("call_id") or ""
            else:
                call_id = getattr(raw, "call_id", "") or ""
            base = pending.pop(call_id, {"name": "(tool)", "arguments": ""})
            out.append({**base, "result": str(output)[:500]})
    # output が来ずに残った tool_call も保存
    for base in pending.values():
        out.append({**base, "result": ""})
    return out


__all__ = ["Agent", "AgentResult", "ItemHelpers", "get_agent", "get_agent_sync_run"]


# ----------------------------------------------------------------------
# ランタイムシングルトン
# ----------------------------------------------------------------------


_agent: Agent | None = None


def get_agent() -> Agent:
    global _agent
    if _agent is None:
        log_level = os.environ.get("TA_LOG_LEVEL", "INFO")
        logging.basicConfig(level=log_level)
        _agent = Agent()
    return _agent


def get_agent_sync_run(
    user_msg: str,
    *,
    mode: Mode = "engineer",
    conversation_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AgentResult:
    """同期文脈 (CLI など) から Agent.run を呼ぶためのヘルパ."""
    agent = get_agent()
    return asyncio.run(
        agent.run(user_msg, mode=mode, conversation_id=conversation_id, metadata=metadata)
    )
