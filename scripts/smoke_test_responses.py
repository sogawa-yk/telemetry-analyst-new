"""OCI Enterprise AI の Responses API まで疎通することを確かめる最小テスト.

環境変数:
  OPENAI_API_KEY, OPENAI_BASE_URL, OCI_GENAI_PROJECT, OCI_GENAI_MODEL
"""

from __future__ import annotations

import os
import sys

from openai import OpenAI


def main() -> int:
    client = OpenAI(
        api_key=os.environ["OPENAI_API_KEY"],
        base_url=os.environ["OPENAI_BASE_URL"],
        project=os.environ["OCI_GENAI_PROJECT"],
    )
    model = os.environ["OCI_GENAI_MODEL"]
    r = client.responses.create(
        model=model,
        input="あなたの役割を 2 行で簡潔に教えてください。",
        instructions="あなたは Kubernetes 監視用の診断エージェント Telemetry Analyst です。",
    )
    text = getattr(r, "output_text", None)
    if not text:
        # フォールバック: output から抽出
        parts = []
        for item in getattr(r, "output", []) or []:
            if getattr(item, "type", None) == "message":
                for c in getattr(item, "content", []) or []:
                    if getattr(c, "type", None) == "output_text":
                        parts.append(getattr(c, "text", ""))
        text = "".join(parts)
    print("---response---")
    print(text)
    print("--------------")
    print("response id:", getattr(r, "id", None))
    print("model:", getattr(r, "model", None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
