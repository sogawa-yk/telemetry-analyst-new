"""Telemetry Analyst の A2A Agent Card 構築.

Agent Card は他エージェントが本エージェントの能力を発見するための自己記述
ドキュメントで、`/a2a/.well-known/agent-card.json` で配信される.

skill は単一 (`diagnose-ec-shop`). 内部で単一 ReAct ループに振り分けるため、
表現上も 1 skill で十分. examples は `eval/golden_set.yaml` から代表的な
問合せを 3 件抜粋.
"""

from __future__ import annotations

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
)
from a2a.utils import TransportProtocol

from ta.config import get_settings

# ec-shop 診断エージェントの代表的な質問 (eval/golden_set.yaml から抜粋)
SKILL_EXAMPLES: list[str] = [
    "ec-shop の checkout-service のレスポンスが最近遅い気がする。原因を教えて。",
    "catalog の 5xx が急増してないか確認して。",
    "cart の Pod が再起動を繰り返している。原因は?",
]


def build_agent_card() -> AgentCard:
    """`telemetry-analyst` の Agent Card を組み立てて返す."""
    s = get_settings()

    skill = AgentSkill(
        id="diagnose-ec-shop",
        name="ec-shop 障害診断",
        description=(
            "Kubernetes 上の ec-shop ネームスペース配下のアプリを、"
            "Prometheus / Loki / Tempo (Grafana MCP) と K8s 読取 API を"
            "横断調査して、症状 → 仮説 → 根拠 → 推奨アクションを返す. "
            "監視対象は ec-shop NS のみ (kube-system 等は権限外). "
            "書込み系 API は実装していない (read-only)."
        ),
        tags=["kubernetes", "observability", "incident-response", "ec-shop"],
        examples=SKILL_EXAMPLES,
        input_modes=["text"],
        output_modes=["text"],
    )

    capabilities = AgentCapabilities(
        streaming=False,  # 初期版は同期 (Task → Artifact 完了通知). 将来 streaming 対応
        push_notifications=False,
    )

    interface = AgentInterface(
        url=s.a2a_public_url.rstrip("/"),
        protocol_binding=TransportProtocol.JSONRPC.value,
        protocol_version="1.0",
    )

    return AgentCard(
        name="telemetry-analyst",
        description=(
            "ec-shop NS の障害を Prometheus / Loki / Tempo / K8s 読取で診断する"
            "単一 ReAct エージェント."
        ),
        version="0.2.9",
        capabilities=capabilities,
        default_input_modes=["text"],
        default_output_modes=["text"],
        skills=[skill],
        supported_interfaces=[interface],
    )
