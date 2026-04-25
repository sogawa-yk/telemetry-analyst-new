"""Langfuse 初期セットアップ (冪等).

このスクリプトは以下を実施する:

1. **Dataset 作成**: `telemetry-analyst-golden` という名前で Dataset を作り、
   eval/golden_set.yaml の全 item をアップロードする. 既存の場合は上書きせずスキップ.
2. **Evaluator 設定手順の表示**: eval/evaluators/*.yaml の内容を読み取り、
   Langfuse UI で Evaluator を登録するための手順を標準出力に出す.
3. **LLM Connection 設定手順の表示**: OCI Enterprise AI を Langfuse の判事モデルとして
   登録する手順を表示する (UI 操作が必要).

前提環境変数:
  LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY

Evaluator と LLM Connection の自動登録 API は 2026 時点で不安定 / 非公開のため、
手動 UI 手順を案内する. 将来 Public API が安定したら自動化する.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml
from langfuse import Langfuse

REPO = Path(__file__).resolve().parents[1]
DATASET_NAME = "telemetry-analyst-golden"


def main() -> int:
    for key in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        if not os.environ.get(key):
            print(f"ERROR: env {key} is not set", file=sys.stderr)
            return 1

    lf = Langfuse(
        host=os.environ["LANGFUSE_HOST"],
        public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
        secret_key=os.environ["LANGFUSE_SECRET_KEY"],
    )

    # 1) Dataset 作成
    try:
        existing = lf.get_dataset(name=DATASET_NAME)
        print(f"[OK] Dataset '{DATASET_NAME}' already exists (items={len(existing.items)}).")
    except Exception:
        lf.create_dataset(
            name=DATASET_NAME,
            description="Telemetry Analyst の判断品質評価用ゴールデンセット. P5b の 10 周ループで使用.",
            metadata={"project": "telemetry-analyst", "version": "0.2.0"},
        )
        print(f"[NEW] Dataset '{DATASET_NAME}' created.")

    # 2) Dataset item アップロード (同 id は冪等にスキップされる想定)
    golden = yaml.safe_load((REPO / "eval" / "golden_set.yaml").read_text(encoding="utf-8"))
    items = golden.get("items", [])
    print(f"[..] Uploading {len(items)} items...")
    for item in items:
        lf.create_dataset_item(
            dataset_name=DATASET_NAME,
            id=item["id"],
            input={"question": item["input"], "mode": item["mode"]},
            expected_output={
                "expected_tools": item.get("expected_tools", []),
                "expected_outcome": item.get("expected_outcome", ""),
                "notes": item.get("notes", ""),
            },
            metadata={"mode": item["mode"]},
        )
    print(f"[OK] {len(items)} items uploaded.")

    lf.flush()

    # 3) Evaluator / LLM Connection の UI 手順を案内
    print()
    print("=" * 70)
    print("  Manual UI Setup Required (Evaluators + LLM Connection)")
    print("=" * 70)
    print()
    print(f"Open: {os.environ['LANGFUSE_HOST']} → Project Settings")
    print()
    print("--- LLM Connection (OCI Enterprise AI を判事モデルとして登録) ---")
    print("  Settings → LLM Connections → Add new LLM API key")
    print("    Provider:     OpenAI")
    print("    Name:         oci-enterprise-ai")
    print("    API Key:      <Secret oci-genai-key の api_key 値>")
    print("    Advanced Settings → Base URL:")
    print("      https://inference.generativeai.ap-osaka-1.oci.oraclecloud.com/openai/v1")
    print("    カスタムヘッダ (対応していれば):")
    print(
        "      OpenAI-Project: ocid1.generativeaiproject.oc1.ap-osaka-1.amaaaaaassl65iqak67q6dr5zu6jqoimgf54sylota5devqglzkkoenxznxa"
    )
    print("  ※ カスタムヘッダが未対応なら deploy/k8s/deployment-litellm-proxy.yaml を")
    print("     有効化し、LiteLLM 経由でアクセスする (別途手順)")
    print()
    print("--- Evaluators (以下 6 種を登録) ---")
    for y in sorted((REPO / "eval" / "evaluators").glob("*.yaml")):
        spec = yaml.safe_load(y.read_text(encoding="utf-8"))
        print(f"  [{y.name}] name='{spec['name']}' type={spec['score_type']}")
        print(f"     target={spec['trigger']['target']} filter={spec['trigger'].get('filter')}")
    print()
    print("  登録手順: Settings → Evaluators → + Set up Evaluator → Custom Evaluator")
    print("  Prompt 本文・variables・score_type は eval/evaluators/ の各 YAML からコピー.")
    print()
    print("--- Dataset Experiment 実行 ---")
    print("  UI: Datasets → telemetry-analyst-golden → Run Experiment")
    print("  SDK: scripts/run_experiment.py (次ステップで実装予定)")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
