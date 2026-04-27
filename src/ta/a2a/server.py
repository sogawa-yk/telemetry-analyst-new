"""A2A サーバを Starlette ルートとして組み立て、FastAPI に mount する.

`a2a-sdk` 1.x が提供する `create_agent_card_routes` / `create_jsonrpc_routes`
を使って Starlette Routes を生成し、それを Starlette アプリケーションに包んで
FastAPI 側で `app.mount("/a2a", ...)` する.
"""

from __future__ import annotations

import logging

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.utils import AGENT_CARD_WELL_KNOWN_PATH, DEFAULT_RPC_URL
from starlette.applications import Starlette

from ta.a2a.agent_card import build_agent_card
from ta.a2a.executor import TelemetryAnalystExecutor

logger = logging.getLogger(__name__)


def build_a2a_starlette_app() -> Starlette:
    """A2A 用 Starlette アプリを構築して返す.

    FastAPI 側で `app.mount("/a2a", build_a2a_starlette_app())` する.
    Bearer Token 認証は別途 `ta.a2a.auth.BearerTokenMiddleware` で wrap する.
    """
    agent_card = build_agent_card()
    executor = TelemetryAnalystExecutor()
    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
    )

    routes = []
    routes.extend(create_agent_card_routes(agent_card, card_url=AGENT_CARD_WELL_KNOWN_PATH))
    routes.extend(create_jsonrpc_routes(request_handler, rpc_url=DEFAULT_RPC_URL))

    app = Starlette(routes=routes)
    logger.info(
        "A2A app built: card_url=%s rpc_url=%s skill=%s",
        AGENT_CARD_WELL_KNOWN_PATH,
        DEFAULT_RPC_URL,
        agent_card.skills[0].id if agent_card.skills else "(none)",
    )
    return app
