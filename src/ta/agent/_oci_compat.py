"""OCI Enterprise AI Responses API 互換性レイヤ.

OCI の Responses API は OpenAI 互換とされるが、いくつかのフィールド検証が
公式 API より厳格で、Agents SDK が出す request body をそのまま投げると 400
になる場合がある.

本モジュールは httpx の AsyncHTTPTransport を継承して、`/responses` POST
直前に request body の input 配列を sanitize する.

既知の差分:
- 失敗した `mcp_call` item に `output` フィールドが無いと OCI は
  `Missing required parameter: 'input[N].output'` で reject する
  (OpenAI 公式は省略可). Agents SDK は MCP 呼出失敗時に `error` のみ
  入れて output を省くため、ここで補完する.
- 同様に `function_call_output.output` が空文字 ("") のケースで OCI が
  reject する報告がある (公式は許容). 念のためフォールバックを入れる.
- OCI Responses API は `input[N]` の各 message item に `"type":"message"`
  を要求する (省略すると `Invalid 'input': expected a valid Responses API
  input payload.` 400). OpenAI 公式は `{role,content}` だけでも受け付ける.
  Agents SDK / openai SDK は `type` 省略形を送るため、role 持ち & type 欠落
  の item に type=message を補完する.
"""

from __future__ import annotations

import json
import logging

import httpx

logger = logging.getLogger(__name__)


def _mcp_error_text(item: dict) -> str:
    err = item.get("error")
    if isinstance(err, dict):
        content = err.get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict):
            return str(content[0].get("text") or "unknown error")
        return str(err.get("type") or "unknown error")
    return "unknown error"


def _sanitize_input_items(items: list) -> int:
    """input 配列を OCI 互換に sanitize. 変更件数を返す."""
    changed = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        # role 持ちで type 欠落 → message item を意図しているので type=message を補完
        # (OCI が `Invalid 'input': expected a valid Responses API input payload.` で reject する原因)
        if t is None and "role" in item:
            item["type"] = "message"
            changed += 1
            continue
        if t == "mcp_call" and "output" not in item:
            err_text = _mcp_error_text(item)
            item["output"] = f"(MCP tool error: {err_text})"
            changed += 1
        elif t == "function_call_output" and not item.get("output"):
            item["output"] = "(empty output)"
            changed += 1
    return changed


def _sanitize_request_body(body_bytes: bytes) -> bytes | None:
    """request body (bytes) を sanitize. 変更が無ければ None を返す."""
    try:
        body = json.loads(body_bytes)
    except Exception:
        return None
    items = body.get("input")
    if not isinstance(items, list):
        return None
    changed = _sanitize_input_items(items)
    if changed == 0:
        return None
    logger.info("OCI 互換: input 配列の %d 件を sanitize しました", changed)
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


class OCISanitizingTransport(httpx.AsyncHTTPTransport):
    """`/responses` POST の request body を OCI 互換に sanitize する transport."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/responses"):
            try:
                content = request.content  # bytes
            except Exception:
                content = None
            if content:
                new_content = _sanitize_request_body(content)
                if new_content is not None:
                    # httpx.Request の content は内部 _content に格納される.
                    # 書き換え後 Content-Length と stream を再構築する.
                    request._content = new_content
                    request.headers["content-length"] = str(len(new_content))
                    request.stream = httpx._content.ByteStream(new_content)
        return await super().handle_async_request(request)


def make_oci_http_client() -> httpx.AsyncClient:
    """OCI 互換 sanitize 付きの httpx.AsyncClient を返す.

    AsyncOpenAI(http_client=...) に渡す想定.
    """
    return httpx.AsyncClient(transport=OCISanitizingTransport())
