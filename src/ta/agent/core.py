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
)
from openai import AsyncOpenAI
from openai.types.responses import ResponseTextDeltaEvent

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


@dataclass
class AgentResult:
    text: str
    conversation_id: str | None
    tool_calls: list[dict[str, Any]]
    response_id: str | None


class Agent:
    def __init__(self) -> None:
        s = get_settings()
        # Agents SDK 用の AsyncOpenAI を OCI Enterprise AI に向ける
        self._async_client = AsyncOpenAI(
            api_key=s.openai_api_key,
            base_url=s.openai_base_url,
            default_headers={"OpenAI-Project": s.oci_genai_project},
        )
        set_default_openai_client(self._async_client)
        set_default_openai_api("responses")
        # トレースの設定は ta.telemetry.langfuse_setup.init_langfuse() に集約.
        # (Agent インスタンス生成前に init_langfuse を呼ぶ運用)

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

        result = await Runner.run(
            sdk_agent,
            input=user_msg,
            session=session,
            max_turns=self._max_react_turns,
            run_config=self._run_config(mode, metadata),
        )

        tool_calls_record = _extract_tool_calls(result.new_items)
        otel_setup.record_response_latency(time.monotonic() - start, mode)
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

        text_parts: list[str] = []
        # call_id -> tool 名 を覚えておき、tool_call_output_item で名前を引く
        tool_name_by_call_id: dict[str, str] = {}

        result = Runner.run_streamed(
            sdk_agent,
            input=user_msg,
            session=session,
            max_turns=self._max_react_turns,
            run_config=self._run_config(mode, metadata),
        )

        async for event in result.stream_events():
            if event.type == "raw_response_event":
                if isinstance(event.data, ResponseTextDeltaEvent):
                    delta = event.data.delta or ""
                    if delta:
                        text_parts.append(delta)
                        yield {"type": "delta", "text": delta}
            elif event.type == "run_item_stream_event":
                item = event.item
                itype = getattr(item, "type", None)
                if itype == "tool_call_item":
                    raw = getattr(item, "raw_item", None)
                    name = getattr(raw, "name", None) or getattr(raw, "type", "?")
                    args = getattr(raw, "arguments", "") or ""
                    call_id = getattr(raw, "call_id", None) or getattr(raw, "id", None)
                    if call_id:
                        tool_name_by_call_id[call_id] = str(name)
                    otel_setup.record_tool_invocation(str(name), "called")
                    yield {"type": "tool_call", "name": str(name), "arguments": args}
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
                    yield {"type": "tool_result", "name": name, "result": str(out)}
                # message_output_item は delta で逐次受信済みのため再 yield は不要

        otel_setup.record_response_latency(time.monotonic() - start, mode)
        yield {
            "type": "done",
            "response_id": getattr(result, "last_response_id", None),
            "text": "".join(text_parts),
        }


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


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
