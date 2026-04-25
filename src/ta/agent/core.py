"""単一 ReAct エージェント本体.

OCI Enterprise AI の Responses API を直接呼ぶ. ReAct ループは Responses API 側が担当し、
自前で「tool_call → tool_result → 再呼出」の while を回す必要がない…
…と言いたいところだが、2026 時点の Responses API で MCP tools は自動実行される一方、
function tools はクライアント側で実行した結果を `input` に追加して再呼出する形が標準.

そのため、このモジュールは以下の薄いループを回す:

1. `client.responses.create(...)` を呼ぶ
2. `response.output` に function_call があれば、対応する Python 関数を呼んで tool 実行
3. その結果を input に追加して再呼出
4. function_call が無くなったら最終応答テキストを返す

MCP tools は OCI 側で自動実行されるため、ここで扱うのは function tools のみ.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from langfuse.openai import OpenAI  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    from openai import OpenAI

from ta.agent.prompts import __path__ as _PROMPTS_PKG
from ta.agent.skills.loader import SkillRetriever
from ta.agent.tools import k8s as k8s_tools
from ta.agent.tools.grafana_mcp import grafana_mcp_tool_spec
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
        self._client = OpenAI(
            api_key=s.openai_api_key,
            base_url=s.openai_base_url,
            project=s.oci_genai_project,
        )
        self._model = s.oci_genai_model
        self._max_tool_calls = s.max_tool_calls
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

    # ------------------------------------------------------------------
    # ツール実行 (function tools のみ. MCP tools は OCI 側で自動実行)
    # ------------------------------------------------------------------

    def _tools(self) -> list[dict[str, Any]]:
        s = get_settings()
        tools: list[dict[str, Any]] = []
        if s.mcp_grafana_enabled:
            tools.append(grafana_mcp_tool_spec())
        tools.extend(k8s_tools.TOOL_SPECS)
        return tools

    def _run_function_tool(self, name: str, arguments_json: str) -> str:
        try:
            args = json.loads(arguments_json) if arguments_json else {}
        except json.JSONDecodeError as e:
            otel_setup.record_tool_invocation(name, "bad_args")
            return f"ツール引数 JSON パースエラー: {e}"
        tracer = otel_setup.get_tracer()
        with tracer.start_as_current_span(f"tool.{name}") as span:
            span.set_attribute("tool.name", name)
            span.set_attribute("tool.arguments", arguments_json[:1000])
            try:
                result = k8s_tools.dispatch(name, args)
                outcome = (
                    "ok"
                    if not result.startswith(("エラー", "K8s API エラー", "ツール"))
                    else "error"
                )
                otel_setup.record_tool_invocation(name, outcome)
                span.set_attribute("tool.outcome", outcome)
                return result
            except Exception as e:
                otel_setup.record_tool_invocation(name, "exception")
                span.record_exception(e)
                raise

    # ------------------------------------------------------------------
    # Conversations API (メモリ委任)
    # ------------------------------------------------------------------

    def create_conversation(self, metadata: dict[str, Any] | None = None) -> str:
        conv = self._client.conversations.create(metadata=metadata or {})  # type: ignore[attr-defined]
        return conv.id  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # ReAct ループ (function tools の繰返し実行)
    # ------------------------------------------------------------------

    def run(
        self,
        user_msg: str,
        *,
        mode: Mode = "engineer",
        conversation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentResult:
        start = time.monotonic()
        instructions = self.build_instructions(user_msg, mode)
        tools = self._tools()
        tool_calls_record: list[dict[str, Any]] = []

        # 初回: ユーザー入力を渡す
        inputs: list[dict[str, Any]] | str = user_msg
        kwargs: dict[str, Any] = {
            "model": self._model,
            "instructions": instructions,
            "tools": tools,
            "metadata": {"mode": mode, **(metadata or {})},
        }
        if conversation_id:
            kwargs["conversation"] = conversation_id

        response = self._client.responses.create(input=inputs, **kwargs)  # type: ignore[arg-type]

        for _ in range(self._max_tool_calls):
            function_calls = _extract_function_calls(response)
            if not function_calls:
                break

            follow_up: list[dict[str, Any]] = []
            for fc in function_calls:
                name = fc["name"]
                args = fc.get("arguments", "")
                call_id = fc["call_id"]
                result = self._run_function_tool(name, args)
                tool_calls_record.append({"name": name, "arguments": args, "result": result[:500]})
                follow_up.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": result,
                    }
                )

            kwargs_follow: dict[str, Any] = {
                "model": self._model,
                "tools": tools,
            }
            # Responses API は conversation と previous_response_id が排他のため、
            # conversation があれば継続はそちらに任せ、なければ previous_response_id を使う.
            if conversation_id:
                kwargs_follow["conversation"] = conversation_id
            else:
                kwargs_follow["previous_response_id"] = response.id
            response = self._client.responses.create(input=follow_up, **kwargs_follow)  # type: ignore[arg-type]

        text = _extract_text(response)
        otel_setup.record_response_latency(time.monotonic() - start, mode)
        usage = getattr(response, "usage", None)
        if usage is not None:
            otel_setup.record_llm_tokens(
                "input", int(getattr(usage, "input_tokens", 0) or 0), self._model
            )
            otel_setup.record_llm_tokens(
                "output", int(getattr(usage, "output_tokens", 0) or 0), self._model
            )
        return AgentResult(
            text=text,
            conversation_id=conversation_id,
            tool_calls=tool_calls_record,
            response_id=getattr(response, "id", None),
        )

    # ------------------------------------------------------------------
    # ストリーミング (Chainlit 用)
    # ------------------------------------------------------------------

    def run_stream(
        self,
        user_msg: str,
        *,
        mode: Mode = "engineer",
        conversation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """ReAct ループを回しつつ、逐次イベントを yield する.

        event の種類:
          - {"type": "delta", "text": "..."}            最終応答テキストの差分
          - {"type": "tool_call", "name", "arguments"}  function tool 呼出 (Claude 用 UX)
          - {"type": "tool_result", "name", "result"}   function tool 結果
          - {"type": "done", "response_id", "text"}     最終
        """
        start = time.monotonic()
        instructions = self.build_instructions(user_msg, mode)
        tools = self._tools()

        inputs: list[dict[str, Any]] | str = user_msg
        kwargs: dict[str, Any] = {
            "model": self._model,
            "instructions": instructions,
            "tools": tools,
            "metadata": {"mode": mode, **(metadata or {})},
        }
        if conversation_id:
            kwargs["conversation"] = conversation_id

        final_text_accum: list[str] = []
        last_response_id: str | None = None

        for _ in range(self._max_tool_calls + 1):
            # ストリーミング
            with self._client.responses.stream(input=inputs, **kwargs) as stream:  # type: ignore[arg-type]
                for event in stream:
                    etype = getattr(event, "type", "")
                    if etype == "response.output_text.delta":
                        delta = getattr(event, "delta", "")
                        if delta:
                            final_text_accum.append(delta)
                            yield {"type": "delta", "text": delta}
                response = stream.get_final_response()

            last_response_id = getattr(response, "id", None)
            function_calls = _extract_function_calls(response)
            if not function_calls:
                break

            follow_up: list[dict[str, Any]] = []
            for fc in function_calls:
                yield {
                    "type": "tool_call",
                    "name": fc["name"],
                    "arguments": fc.get("arguments", ""),
                }
                result = self._run_function_tool(fc["name"], fc.get("arguments", ""))
                yield {"type": "tool_result", "name": fc["name"], "result": result}
                follow_up.append(
                    {
                        "type": "function_call_output",
                        "call_id": fc["call_id"],
                        "output": result,
                    }
                )

            inputs = follow_up  # 次ループ
            kwargs = {
                "model": self._model,
                "tools": tools,
            }
            # conversation と previous_response_id は排他
            if conversation_id:
                kwargs["conversation"] = conversation_id
            else:
                kwargs["previous_response_id"] = last_response_id

        otel_setup.record_response_latency(time.monotonic() - start, mode)
        yield {
            "type": "done",
            "response_id": last_response_id,
            "text": "".join(final_text_accum),
        }


# ----------------------------------------------------------------------
# Responses API レスポンスからの抽出ヘルパ
# ----------------------------------------------------------------------


def _extract_function_calls(response: Any) -> list[dict[str, Any]]:
    """response.output から function_call アイテムを取り出す."""
    out: list[dict[str, Any]] = []
    for item in getattr(response, "output", []) or []:
        itype = getattr(item, "type", None)
        if itype == "function_call":
            out.append(
                {
                    "call_id": getattr(item, "call_id", None),
                    "name": getattr(item, "name", ""),
                    "arguments": getattr(item, "arguments", "") or "",
                }
            )
    return out


def _extract_text(response: Any) -> str:
    """response.output_text or response.output から最終テキストを抽出."""
    # SDK が output_text を提供していれば優先
    text = getattr(response, "output_text", None)
    if text:
        return str(text)

    # fallback: output からテキストを手繰る
    parts: list[str] = []
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) == "message":
            for content in getattr(item, "content", []) or []:
                if getattr(content, "type", None) == "output_text":
                    parts.append(getattr(content, "text", "") or "")
    return "".join(parts)


# ----------------------------------------------------------------------
# ランタイムシングルトン
# ----------------------------------------------------------------------


_agent: Agent | None = None


def get_agent() -> Agent:
    global _agent
    if _agent is None:
        # LOG LEVEL など軽い副作用はここで
        log_level = os.environ.get("TA_LOG_LEVEL", "INFO")
        import logging

        logging.basicConfig(level=log_level)
        _agent = Agent()
    return _agent
