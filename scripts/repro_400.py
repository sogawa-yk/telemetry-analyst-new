"""OCI Responses API の 400 (input[N].output 欠落) を再現するための最小スクリプト.

Iter-00 で `latency-checkout-01` と `slo-checkout-engineer-10` が
`Missing required parameter: 'input[N].output'` で失敗した. 仮説:
Hosted MCP の tool 呼出で output が空 / null になったケースで OCI が拒否する.

このスクリプトは httpx リクエストボディをダンプして、実際に何の output が
空だったかを切り分ける. クラスタ内の ta-agent pod から実行する想定.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

# httpx と openai のリクエスト本文をダンプ
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.DEBUG)
logging.getLogger("openai").setLevel(logging.DEBUG)
os.environ["OPENAI_LOG"] = "debug"


async def main() -> int:
    from ta.agent.core import get_agent
    from ta.telemetry.langfuse_setup import init_langfuse

    init_langfuse()
    agent = get_agent()

    question = "ec-shop の checkout-service のレスポンスが最近遅い気がする。原因を教えて。"
    print(f"\n=== Reproducing failed case: {question[:60]}", file=sys.stderr)
    try:
        result = await agent.run(question, mode="engineer")
        print(f"\nfinal_output: {result.text[:300]}")
        for tc in result.tool_calls:
            print(
                f"  tool_call: name={tc['name']} args={tc['arguments'][:80]} result_len={len(tc['result'])}"
            )
    except Exception as e:
        print(f"\nFAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
