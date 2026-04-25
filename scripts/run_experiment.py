"""Dataset Experiment を SDK 経由で 1 周実行する.

P5b の 10 周ループで毎周呼ぶ想定. 実行すると:
  - Dataset の全 item についてエージェントを実行
  - 各実行を Langfuse のトレースとして送信
  - Experiment Run として Dataset にリンク
  - UI 側で Evaluator が自動スコアを付ける (Live 評価が設定されていれば)

前提:
  - OPENAI_API_KEY, OCI_GENAI_PROJECT, OCI_GENAI_MODEL
  - LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY
  - (MCP Grafana と K8s tool が到達可能な環境)

使い方:
  python scripts/run_experiment.py --label iter-01

Phase C-4: tenacity 指数バックオフ再試行 + asyncio.Semaphore で同時実行数制限 +
1 ケース失敗で続行しつつ最後に失敗一覧を stderr 出力.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from langfuse import Langfuse
from tenacity import retry, stop_after_attempt, wait_exponential

from ta.agent.core import get_agent
from ta.telemetry.langfuse_setup import init_langfuse

DATASET_NAME = "telemetry-analyst-golden"


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=4, max=30),
)
async def _run_with_retry(agent, question: str, mode: str, item_id: str, label: str):  # type: ignore[no-untyped-def]
    return await agent.run(
        question,
        mode=mode,
        metadata={
            "kind": "agent-main-response",
            "dataset_item_id": item_id,
            "run_label": label,
        },
    )


async def _run_one(  # type: ignore[no-untyped-def]
    sem: asyncio.Semaphore,
    agent,
    item,
    label: str,
    description: str,
    failed: list[tuple[str, str]],
) -> None:
    async with sem:
        question = item.input.get("question") if isinstance(item.input, dict) else str(item.input)
        mode = (item.input.get("mode") if isinstance(item.input, dict) else None) or "engineer"
        print(f"[{item.id}] ({mode}) {question[:60]}")
        with item.run(
            run_name=label,
            run_description=description or None,
            run_metadata={"project": "telemetry-analyst"},
        ) as root_span:
            try:
                result = await _run_with_retry(agent, question, mode, item.id, label)
                root_span.update(
                    input=question,
                    output=result.text,
                    metadata={
                        "kind": "agent-main-response",
                        "tool_calls": [
                            {"name": tc["name"], "arguments": tc["arguments"]}
                            for tc in result.tool_calls
                        ],
                        "mode": mode,
                    },
                )
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                print(f"  ERROR ({item.id}): {msg}", file=sys.stderr)
                failed.append((item.id, msg))
                root_span.update(output=f"ERROR: {msg}", metadata={"kind": "agent-error"})


async def amain() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True, help="周回ラベル (例: iter-01)")
    parser.add_argument(
        "--description", default="", help="Run description (iterations.md への記録補助)"
    )
    parser.add_argument("--limit", type=int, default=0, help="処理する item 数の上限 (0=全件)")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="同時実行数 (default 1. OCI レート制限を踏まないため小さめ推奨)",
    )
    args = parser.parse_args()

    init_langfuse()
    lf = Langfuse(
        host=os.environ["LANGFUSE_HOST"],
        public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
        secret_key=os.environ["LANGFUSE_SECRET_KEY"],
    )
    dataset = lf.get_dataset(name=DATASET_NAME)
    items = list(dataset.items)
    if args.limit:
        items = items[: args.limit]
    print(f"Running {len(items)} items as run '{args.label}' (concurrency={args.concurrency})...")

    agent = get_agent()
    sem = asyncio.Semaphore(args.concurrency)
    failed: list[tuple[str, str]] = []
    await asyncio.gather(
        *(_run_one(sem, agent, item, args.label, args.description, failed) for item in items)
    )

    lf.flush()
    print(f"\nDone. View in Langfuse: {os.environ['LANGFUSE_HOST']}/datasets/{DATASET_NAME}")
    if failed:
        print(f"\n{len(failed)} 件失敗:", file=sys.stderr)
        for item_id, msg in failed:
            print(f"  - {item_id}: {msg}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())
