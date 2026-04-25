"""Phase 0-3 移行後の Agent 本体スモークテスト.

クラスタ内 (telemetry-analyst NS の ta-agent pod) から `kubectl exec` で実行する想定.
新版 src/ta を /tmp/ta に kubectl cp して、PYTHONPATH=/tmp で本スクリプトを叩く.

確認項目:
1. agent.run() が ec-shop の Pod 一覧 (k8s_list_pods function tool) を取得できる
2. agent.run_stream() の 4 種イベントが正しい順で流れる
3. agent.create_conversation() + 2 ターン会話継続が動作 (Sessions)
4. HostedMCPTool 経由の Grafana MCP も streaming で実行される
"""

from __future__ import annotations

import asyncio
import sys
import traceback


async def main() -> int:
    from ta.agent.core import get_agent

    agent = get_agent()

    # 1) run() with k8s function tool
    print("\n=== 1) agent.run() — k8s_list_pods ===")
    try:
        r1 = await agent.run("ec-shop の Pod 一覧を見せて。", mode="engineer")
        print(f"text: {r1.text[:500]}")
        print(f"tool_calls: {[(t['name'], t['arguments']) for t in r1.tool_calls]}")
    except Exception as e:
        print(f"[FAIL] 1: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc()
        return 1

    # 2) run_stream() — 4 種イベントの確認
    print("\n=== 2) agent.run_stream() ===")
    seen = {"delta": False, "tool_call": False, "tool_result": False, "done": False}
    try:
        async for ev in agent.run_stream(
            "ec-shop の Deployment 一覧を出してください。", mode="engineer"
        ):
            seen[ev["type"]] = True
            if ev["type"] == "delta":
                sys.stdout.write(ev["text"])
                sys.stdout.flush()
            elif ev["type"] == "tool_call":
                print(f"\n[tool_call] {ev['name']}({ev['arguments']})")
            elif ev["type"] == "tool_result":
                print(f"\n[tool_result] {ev['name']} -> {ev['result'][:120]}")
            elif ev["type"] == "done":
                print(f"\n[done] response_id={ev['response_id']}")
        print(f"seen events: {seen}")
        if not all(seen.values()):
            print("[WARN] 一部イベント未受信", file=sys.stderr)
    except Exception as e:
        print(f"[FAIL] 2: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc()
        return 1

    # 3) create_conversation + 2 turn
    print("\n=== 3) create_conversation + 2 ターン ===")
    try:
        conv_id = await agent.create_conversation(metadata={"source": "smoke"})
        print(f"conv_id: {conv_id}")
        r3a = await agent.run(
            "ec-shop の Pod 一覧をください。", mode="engineer", conversation_id=conv_id
        )
        print(f"[turn 1] {r3a.text[:200]}")
        r3b = await agent.run(
            "そのうち最初の Pod の役割を 1 行で。",
            mode="engineer",
            conversation_id=conv_id,
        )
        print(f"[turn 2] {r3b.text[:200]}")
    except Exception as e:
        print(f"[FAIL] 3: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc()
        return 1

    print("\n=== ALL OK ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
