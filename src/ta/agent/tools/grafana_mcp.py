"""Grafana MCP サーバを Agents SDK の HostedMCPTool として登録する.

OCI Enterprise AI の Responses API ネイティブ MCP (`tools:[{type:"mcp"}]`) に
そのままマッピングされる. 自前で MCP クライアントは書かない.
"""

from __future__ import annotations

from agents import HostedMCPTool

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


def make_grafana_mcp_tool() -> HostedMCPTool:
    """Agents SDK の Agent.tools にそのまま渡せる HostedMCPTool を返す."""
    s = get_settings()
    return HostedMCPTool(
        tool_config={
            "type": "mcp",
            "server_label": "grafana",
            "server_url": s.mcp_grafana_url,
            "allowed_tools": GRAFANA_ALLOWED_TOOLS,
            "require_approval": "never",
        }
    )
