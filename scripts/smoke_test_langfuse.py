"""Langfuse + OpenInference 計装スモーク (Phase 0-3 (e)).

OpenAI Agents SDK の操作が OpenInference instrumentor 経由で OTel スパンとして
emit され、Langfuse の OTLP endpoint へ届くことを確認する.

実行 (pod 内):
    PYTHONPATH=/tmp:/home/ta/.local/lib/python3.12/site-packages \\
    TA_SKILLS_DIR=/tmp/skills TA_MEMORY_DIR=/tmp/memory \\
    python /tmp/smoke_lf.py

確認:
- 終了後、Langfuse UI (http://langfuse-web.langfuse.svc.cluster.local:3000)
  の Traces タブに本実行のスパンが現れること.
"""

from __future__ import annotations

import asyncio
import os
import sys


async def main() -> int:
    # 計装は init_langfuse() 内で OpenAIAgentsInstrumentor().instrument() を呼ぶ
    from ta.agent.core import get_agent
    from ta.telemetry.langfuse_setup import flush, init_langfuse

    lf = init_langfuse()
    if lf is None:
        print("[skip] Langfuse 設定なし — 計装は走らない", file=sys.stderr)
        return 1

    agent = get_agent()
    print("\n--- 1) run() ---")
    r = await agent.run("ec-shop の Pod 一覧を見せて。", mode="engineer")
    print(f"text: {r.text[:200]}")

    print("\n--- 2) run_stream() ---")
    async for ev in agent.run_stream("ec-shop の Deployment 一覧をください。", mode="engineer"):
        if ev["type"] == "tool_call":
            print(f"[tool_call] {ev['name']}")

    flush()
    print(f"\n[done] check Langfuse UI: {os.environ.get('LANGFUSE_HOST')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
