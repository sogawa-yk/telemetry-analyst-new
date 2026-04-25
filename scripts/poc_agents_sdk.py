"""OpenAI Agents SDK と OCI Enterprise AI の互換性 PoC.

Phase 0-2 の Go/No-Go 判定用. クラスタ内 (telemetry-analyst NS の ta-agent pod)
から `kubectl exec` 経由で実行する想定.

確認項目:
1. `set_default_openai_client` で OCI に向けた AsyncOpenAI を差し替え、Responses API が 200 OK
2. `@function_tool` でラップした Python 関数が Runner.run で自動呼出される
3. `HostedMCPTool(tool_config={...})` で Grafana MCP に到達し、allowed_tools が機能
4. `Runner.run_streamed` の stream_events() から delta / tool_call_item / tool_call_output_item / message_output_item が取れる
5. `OpenAIConversationsSession` で複数ターン会話が継続される

実行手順:
    kubectl cp scripts/poc_agents_sdk.py telemetry-analyst/<ta-agent-pod>:/tmp/poc.py
    kubectl exec -n telemetry-analyst <ta-agent-pod> -- pip install --user --quiet 'openai-agents>=0.14,<1.0'
    kubectl exec -n telemetry-analyst <ta-agent-pod> -- python /tmp/poc.py

Pod には OPENAI_API_KEY / OPENAI_BASE_URL / OCI_GENAI_PROJECT / OCI_GENAI_MODEL /
MCP_GRAFANA_URL が既に注入されている前提.
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback

from agents import (
    Agent,
    HostedMCPTool,
    ItemHelpers,
    OpenAIConversationsSession,
    Runner,
    function_tool,
    set_default_openai_api,
    set_default_openai_client,
    set_tracing_disabled,
)
from openai import AsyncOpenAI
from openai.types.responses import ResponseTextDeltaEvent

# ---------------------------------------------------------------------------
# Step 1: OCI Enterprise AI を Agents SDK のデフォルトクライアントに差し替える
# ---------------------------------------------------------------------------


def configure_oci_client() -> None:
    api_key = os.environ["OPENAI_API_KEY"]
    base_url = os.environ.get(
        "OPENAI_BASE_URL",
        "https://inference.generativeai.ap-osaka-1.oci.oraclecloud.com/openai/v1",
    )
    project = os.environ["OCI_GENAI_PROJECT"]
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        default_headers={"OpenAI-Project": project},
    )
    set_default_openai_client(client)
    set_default_openai_api("responses")
    # OCI 鍵で OpenAI 公式 trace endpoint を叩いて 401 を出すのを防ぐ
    set_tracing_disabled(True)
    print(f"[setup] OCI client base_url={base_url} project={project[:40]}...")


# ---------------------------------------------------------------------------
# Step 2: function_tool 1 個 + HostedMCPTool で Agent を組む
# ---------------------------------------------------------------------------


@function_tool
def k8s_list_pods_demo() -> str:
    """ec-shop NS の Pod 一覧を返す (PoC 用ダミー実装).

    本番版は ta.agent.tools.k8s.k8s_list_pods に置き換える.
    """
    return (
        "Namespace `ec-shop` の Pod 一覧 (4 件):\n"
        "- ec-web-557766b744-5xvl4   Running  restarts=0  age=17h\n"
        "- ec-web-557766b744-pr28h   Running  restarts=0  age=17h\n"
        "- load-generator-59779ccf6d-fwqrg  Running  restarts=0  age=11h\n"
        "- postgres-0                Running  restarts=0  age=11h"
    )


def build_agent(*, with_mcp: bool = True, with_function: bool = True) -> Agent:
    """Agent を構築. with_* フラグで切り分けテスト可能."""
    model = os.environ.get("OCI_GENAI_MODEL", "openai.gpt-4.1")
    tools: list = []
    if with_function:
        tools.append(k8s_list_pods_demo)
    if with_mcp:
        mcp_url = os.environ["MCP_GRAFANA_URL"]
        tools.append(
            HostedMCPTool(
                tool_config={
                    "type": "mcp",
                    "server_label": "grafana",
                    "server_url": mcp_url,
                    "require_approval": "never",
                    "allowed_tools": [
                        "list_prometheus_metric_names",
                        "search_dashboards",
                    ],
                }
            )
        )
    return Agent(
        name="ta-poc",
        instructions=(
            "あなたは ec-shop NS の診断エージェントです。"
            "ユーザの質問に対し、提供されたツールを 1〜2 個呼んで根拠を集め、"
            "短く日本語で回答してください。"
        ),
        model=model,
        tools=tools,
    )


# ---------------------------------------------------------------------------
# Step 3: 各検証ターン
# ---------------------------------------------------------------------------


async def turn_oneshot(agent: Agent) -> bool:
    print("\n=== Turn 1: function_tool 1 回呼出 (Sessions なし) ===")
    try:
        result = await Runner.run(agent, input="ec-shop の Pod 一覧を見せて。")
        print(f"[ok] final_output:\n{result.final_output[:400]}")
        return True
    except Exception as e:
        print(f"[FAIL] turn_oneshot: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc()
        return False


async def turn_streaming_no_mcp(agent: Agent) -> bool:
    """function_tool のみで Runner.run_streamed を回し、stream_events の構造を確認."""
    print("\n=== Turn 2a: streaming (function_tool のみ、MCP なし) ===")
    seen_delta = False
    seen_tool_call = False
    seen_tool_output = False
    try:
        result = Runner.run_streamed(
            agent,
            input="ec-shop の Pod 一覧を出して、状態を 1 行で要約して。",
        )
        async for event in result.stream_events():
            if event.type == "raw_response_event":
                if isinstance(event.data, ResponseTextDeltaEvent):
                    if not seen_delta:
                        print("[delta] (受信開始)", flush=True)
                        seen_delta = True
                    sys.stdout.write(event.data.delta)
                    sys.stdout.flush()
            elif event.type == "run_item_stream_event":
                itype = getattr(event.item, "type", "?")
                if itype == "tool_call_item":
                    seen_tool_call = True
                    raw = getattr(event.item, "raw_item", None)
                    name = getattr(raw, "name", None) or getattr(raw, "type", "?")
                    print(f"\n[tool_call_item] {name}", flush=True)
                elif itype == "tool_call_output_item":
                    seen_tool_output = True
                    out = getattr(event.item, "output", "")
                    print(f"\n[tool_call_output_item] {str(out)[:200]}", flush=True)
                elif itype == "message_output_item":
                    text = ItemHelpers.text_message_output(event.item)
                    print(f"\n[message_output_item] {text[:200]}", flush=True)
        print()
        return seen_delta and seen_tool_call and seen_tool_output
    except Exception as e:
        print(f"\n[FAIL] turn_streaming_no_mcp: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc()
        return False


async def turn_streaming_mcp(agent: Agent) -> bool:
    print("\n=== Turn 2b: streaming + HostedMCPTool (Grafana) ===")
    try:
        result = Runner.run_streamed(
            agent,
            input="Grafana のダッシュボードから ec-shop に関するものを 3 件挙げて。",
        )
        async for event in result.stream_events():
            if event.type == "raw_response_event":
                if isinstance(event.data, ResponseTextDeltaEvent):
                    sys.stdout.write(event.data.delta)
                    sys.stdout.flush()
            elif event.type == "run_item_stream_event":
                item = event.item
                itype = getattr(item, "type", "?")
                if itype == "tool_call_item":
                    raw = getattr(item, "raw_item", None)
                    name = getattr(raw, "name", None) or getattr(raw, "type", "?")
                    print(f"\n[tool_call_item] {name}", flush=True)
                elif itype == "tool_call_output_item":
                    out = getattr(item, "output", "")
                    print(f"\n[tool_call_output_item] {str(out)[:200]}", flush=True)
                elif itype == "message_output_item":
                    text = ItemHelpers.text_message_output(item)
                    print(f"\n[message_output_item] {text[:200]}", flush=True)
        print()
        return True
    except Exception as e:
        print(f"\n[FAIL] turn_streaming_mcp: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc()
        return False


async def turn_session_2steps(agent: Agent) -> bool:
    print("\n=== Turn 3-4: OpenAIConversationsSession で 2 ターン継続 ===")
    try:
        session = OpenAIConversationsSession()
        r1 = await Runner.run(
            agent,
            input="ec-shop の Pod 一覧をください。",
            session=session,
        )
        print(f"[turn 3] {str(r1.final_output)[:300]}")
        r2 = await Runner.run(
            agent,
            input="さっきの 1 つ目の Pod の役割を初心者向けに 1 行で説明して。",
            session=session,
        )
        print(f"[turn 4] {str(r2.final_output)[:300]}")
        return True
    except Exception as e:
        print(f"\n[FAIL] turn_session_2steps: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc()
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def turn_no_tools(agent: Agent) -> bool:
    """ツール無しで OCI Responses API への基本疎通だけを確認."""
    print("\n=== Turn 0: tools 無しで OCI Responses 疎通確認 ===")
    try:
        result = await Runner.run(agent, input="自己紹介を 1 行で。")
        print(f"[ok] {str(result.final_output)[:300]}")
        return True
    except Exception as e:
        print(f"[FAIL] turn_no_tools: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc()
        return False


async def main() -> int:
    configure_oci_client()
    results: dict[str, bool] = {}

    # 段階 0: ツール無し (OCI Responses API + Agents SDK の最低疎通)
    agent_bare = build_agent(with_mcp=False, with_function=False)
    results["0_no_tools"] = await turn_no_tools(agent_bare)

    # 段階 1: function_tool のみ (Agents SDK の自動 ReAct ループが動くか)
    agent_fn = build_agent(with_mcp=False, with_function=True)
    results["1_function_only"] = await turn_oneshot(agent_fn)

    # 段階 2a: streaming (MCP なし) — Runner.run_streamed のイベント構造確認
    results["2a_streaming_no_mcp"] = await turn_streaming_no_mcp(agent_fn)

    # 段階 2b: MCP + function (HostedMCPTool が OCI の hosted MCP として登録できるか)
    agent_full = build_agent(with_mcp=True, with_function=True)
    results["2b_streaming_mcp"] = await turn_streaming_mcp(agent_full)

    # 段階 3: OpenAIConversationsSession で 2 ターン継続
    results["3_session"] = await turn_session_2steps(agent_fn)  # MCP 抜き

    print("\n=== Summary ===")
    for name, ok in results.items():
        print(f"  {name}: {'OK' if ok else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
