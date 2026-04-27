"""A2A (Agent-to-Agent) サーバ実装.

`a2a-sdk` 1.x で `telemetry-analyst` を A2A プロトコルで他エージェントから
呼べるよう expose する. 既存 FastAPI (`src/ta/api/main.py`) に
`/a2a/*` を mount する形で統合.
"""

from ta.a2a.agent_card import build_agent_card
from ta.a2a.server import build_a2a_starlette_app

__all__ = ["build_a2a_starlette_app", "build_agent_card"]
