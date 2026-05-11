"""OCI 互換 sanitizer (mcp_call の output 欠落補完) の単体テスト."""

from __future__ import annotations

import json

from ta.agent._oci_compat import _sanitize_input_items, _sanitize_request_body


def test_sanitize_mcp_call_failed_without_output() -> None:
    items = [
        {"role": "user", "content": "hello", "type": "message"},
        {
            "id": "mcp_xxx",
            "type": "mcp_call",
            "name": "search_dashboards",
            "arguments": "{}",
            "status": "failed",
            "error": {
                "type": "mcp_tool_execution_error",
                "content": [{"text": "DNS resolution failed"}],
            },
        },
    ]
    changed = _sanitize_input_items(items)
    assert changed == 1
    assert items[1]["output"].startswith("(MCP tool error:")
    assert "DNS resolution failed" in items[1]["output"]


def test_sanitize_mcp_call_already_has_output_unchanged() -> None:
    items = [
        {
            "type": "mcp_call",
            "status": "completed",
            "output": "[{...}]",
        },
    ]
    before = json.dumps(items)
    changed = _sanitize_input_items(items)
    assert changed == 0
    assert json.dumps(items) == before


def test_sanitize_function_call_output_empty_filled() -> None:
    items = [
        {"type": "function_call_output", "call_id": "c1", "output": ""},
    ]
    changed = _sanitize_input_items(items)
    assert changed == 1
    assert items[0]["output"] == "(empty output)"


def test_sanitize_request_body_returns_none_when_no_change() -> None:
    # role 持ちでも既に type=message なら変更なし
    body = json.dumps(
        {"input": [{"type": "message", "role": "user", "content": "hi"}]}
    ).encode()
    assert _sanitize_request_body(body) is None


def test_sanitize_adds_type_message_to_user_item() -> None:
    """role を持つが type を持たない item に type='message' を補完する.

    OCI Responses API は `input[N].type` の指定が無いと `Invalid 'input':
    expected a valid Responses API input payload.` で 400 を返す.
    OpenAI SDK / Agents SDK は `{role,content}` だけを送るため、ここで補完する.
    """
    items = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    changed = _sanitize_input_items(items)
    assert changed == 2
    assert items[0]["type"] == "message"
    assert items[1]["type"] == "message"
    # content 自体は触らない
    assert items[0]["content"] == "hello"


def test_sanitize_keeps_existing_type_unchanged() -> None:
    items = [{"type": "message", "role": "user", "content": "hi"}]
    assert _sanitize_input_items(items) == 0


def test_sanitize_skips_items_without_role() -> None:
    """role の無い item (function_call 等) には type を付けない."""
    items = [
        {"type": "function_call", "call_id": "c1", "arguments": "{}"},
        {"id": "x", "content": "no role"},  # role 無し → 触らない
    ]
    assert _sanitize_input_items(items) == 0
    assert "type" not in items[1]


def test_sanitize_request_body_adds_type_to_messages() -> None:
    body = json.dumps(
        {
            "input": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "ok"},
            ]
        }
    ).encode()
    out = _sanitize_request_body(body)
    assert out is not None
    parsed = json.loads(out)
    assert parsed["input"][0]["type"] == "message"
    assert parsed["input"][1]["type"] == "message"


def test_sanitize_request_body_returns_new_bytes_on_change() -> None:
    body = json.dumps(
        {
            "input": [
                {
                    "type": "mcp_call",
                    "status": "failed",
                    "error": {"content": [{"text": "boom"}]},
                }
            ]
        }
    ).encode()
    out = _sanitize_request_body(body)
    assert out is not None
    parsed = json.loads(out)
    assert parsed["input"][0]["output"].startswith("(MCP tool error:")


def test_sanitize_request_body_invalid_json_returns_none() -> None:
    assert _sanitize_request_body(b"not json") is None
