"""A2A エンドポイントの Bearer Token 認証.

Starlette middleware として実装し、`/a2a/*` 配下の全リクエストで
`Authorization: Bearer <token>` を検証する.

- token 未設定 (`A2A_AUTH_TOKEN` 環境変数なし) → A2A 機能を無効化 (503)
- ヘッダ無し / 不一致 → 401
- 一致 → 通過
"""

from __future__ import annotations

import hmac
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ta.config import get_settings

logger = logging.getLogger(__name__)


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """A2A サーバに Authorization: Bearer <token> 検証を被せる."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        token = get_settings().a2a_auth_token
        if not token:
            return JSONResponse(
                {"error": "A2A is not enabled (A2A_AUTH_TOKEN is not configured)"},
                status_code=503,
            )

        header = request.headers.get("authorization") or ""
        if not header.lower().startswith("bearer "):
            return _unauthorized("missing or malformed Authorization header")

        provided = header.split(" ", 1)[1].strip()
        # 定数時間比較で timing attack を回避
        if not hmac.compare_digest(provided, token):
            logger.warning("A2A auth failed (token mismatch) from %s", request.client)
            return _unauthorized("invalid bearer token")

        return await call_next(request)


def _unauthorized(reason: str) -> Response:
    return JSONResponse(
        {"error": "unauthorized", "reason": reason},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer realm="telemetry-analyst-a2a"'},
    )
