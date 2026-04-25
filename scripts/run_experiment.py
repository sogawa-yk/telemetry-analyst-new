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
"""

from __future__ import annotations

import argparse
import os
import sys

from langfuse import Langfuse

from ta.agent.core import get_agent
from ta.telemetry.langfuse_setup import init_langfuse

DATASET_NAME = "telemetry-analyst-golden"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True, help="周回ラベル (例: iter-01)")
    parser.add_argument(
        "--description", default="", help="Run description (iterations.md への記録補助)"
    )
    parser.add_argument("--limit", type=int, default=0, help="処理する item 数の上限 (0=全件)")
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
    print(f"Running {len(items)} items as run '{args.label}'...")

    agent = get_agent()
    for i, item in enumerate(items, 1):
        question = item.input.get("question") if isinstance(item.input, dict) else str(item.input)
        mode = (item.input.get("mode") if isinstance(item.input, dict) else None) or "engineer"
        print(f"[{i}/{len(items)}] {item.id} ({mode}) — {question[:60]}")
        # Langfuse Dataset Item に紐付けた run を作成
        with item.run(
            run_name=args.label,
            run_description=args.description or None,
            run_metadata={"project": "telemetry-analyst"},
        ) as root_span:
            try:
                result = agent.run(
                    question,
                    mode=mode,  # type: ignore[arg-type]
                    metadata={
                        "kind": "agent-main-response",
                        "dataset_item_id": item.id,
                        "run_label": args.label,
                    },
                )
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
                print(f"  ERROR: {e}", file=sys.stderr)
                root_span.update(output=f"ERROR: {e}", metadata={"kind": "agent-error"})

    lf.flush()
    print(f"\nDone. View in Langfuse: {os.environ['LANGFUSE_HOST']}/datasets/{DATASET_NAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
