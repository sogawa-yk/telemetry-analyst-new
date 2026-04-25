"""Grafana MCP サーバを Responses API のネイティブ MCP ツールとして登録する.

Responses API の tools パラメータに以下の形で渡す:

    {"type": "mcp", "server_url": ..., "allowed_tools": [...]}

これにより MCP サーバ側のツールを LLM が自動呼出できる. 自前で MCP クライアントは書かない.
"""

from __future__ import annotations

from typing import Any

from ta.config import get_settings

# ec-shop 診断で有用な MCP ツールに絞って露出する.
# 全部露出するとトークンを食いすぎるため.
GRAFANA_ALLOWED_TOOLS: list[str] = [
    # Prometheus
    "query_prometheus",
    "list_prometheus_metric_names",
    "list_prometheus_label_names",
    "list_prometheus_label_values",
    # Loki
    "query_loki_logs",
    "query_loki_stats",
    "list_loki_label_names",
    "list_loki_label_values",
    "find_error_pattern_logs",
    # Tempo
    "find_slow_requests",
    # Alert / Dashboard
    "list_alert_rules",
    "get_alert_rule_by_uid",
    "search_dashboards",
    "get_dashboard_by_uid",
]


def grafana_mcp_tool_spec() -> dict[str, Any]:
    """Responses API の tools パラメータに渡せる 1 エントリを返す."""
    s = get_settings()
    return {
        "type": "mcp",
        "server_label": "grafana",
        "server_url": s.mcp_grafana_url,
        "allowed_tools": GRAFANA_ALLOWED_TOOLS,
        "require_approval": "never",
    }
