"""Agent クラスの instructions ビルダ / tool_calls 抽出 / イベント正規化テスト.

Runner.run の実呼出は外部 API に出るため、mock を使ってロジックのみ検証する.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ta.agent.core import Agent, _extract_tool_calls


@pytest.fixture
def agent() -> Agent:
    """AsyncOpenAI コンストラクタを mock して Agent を作る."""
    with patch("ta.agent.core.AsyncOpenAI"):
        return Agent()


# ---------------------------------------------------------------------------
# build_instructions: skill 注入と環境メモリ統合
# ---------------------------------------------------------------------------


def test_build_instructions_includes_mode_section(agent: Agent) -> None:
    out = agent.build_instructions("checkout が遅い", mode="engineer")
    assert "エンジニアモード" in out or "出力モード: エンジニア" in out
    assert "ec-shop" in out


def test_build_instructions_picks_latency_skill_for_latency_query(agent: Agent) -> None:
    out = agent.build_instructions("checkout の p99 latency が悪化している", mode="engineer")
    assert "latency-regression" in out


def test_build_instructions_picks_oom_skill_for_oom_query(agent: Agent) -> None:
    out = agent.build_instructions("payment が OOMKilled で再起動している", mode="engineer")
    assert "oom-kill" in out


def test_build_instructions_includes_environment_memory(agent: Agent) -> None:
    out = agent.build_instructions("健康状態を教えて", mode="engineer")
    # environment.md に記載されているキーフレーズで存在を確認
    assert "監視対象" in out
    assert "ec-shop" in out


def test_build_instructions_mode_filter_excludes_other_mode_skills(agent: Agent) -> None:
    """beginner モードで explain-engineer が混入しないこと (逆も)."""
    out_beg = agent.build_instructions("どうすればいい?", mode="beginner")
    out_eng = agent.build_instructions("どうすればいい?", mode="engineer")
    assert "explain-beginner" in out_beg
    assert "explain-beginner" not in out_eng
    assert "explain-engineer" in out_eng
    assert "explain-engineer" not in out_beg


def test_picked_skills_returns_names(agent: Agent) -> None:
    skills = agent.picked_skills("OOMKilled で payment が落ちている", mode="engineer")
    assert "oom-kill" in skills
    assert "explain-engineer" in skills  # 常時注入


# ---------------------------------------------------------------------------
# _extract_tool_calls: tool_call_item と tool_call_output_item の対応
# ---------------------------------------------------------------------------


def _mk_tool_call_item(call_id: str, name: str, arguments: str = "{}"):
    raw = SimpleNamespace(call_id=call_id, name=name, arguments=arguments)
    return SimpleNamespace(type="tool_call_item", raw_item=raw)


def _mk_tool_output_item(call_id: str, output: str):
    raw = {"type": "function_call_output", "call_id": call_id, "output": output}
    return SimpleNamespace(type="tool_call_output_item", raw_item=raw, output=output)


def test_extract_tool_calls_pairs_call_id() -> None:
    items = [
        _mk_tool_call_item("c1", "k8s_list_pods"),
        _mk_tool_output_item("c1", "Pod 一覧 ..."),
        _mk_tool_call_item("c2", "k8s_describe_pod", '{"name":"x"}'),
        _mk_tool_output_item("c2", "Pod 詳細..."),
    ]
    out = _extract_tool_calls(items)
    assert len(out) == 2
    assert out[0]["name"] == "k8s_list_pods"
    assert out[0]["result"].startswith("Pod 一覧")
    assert out[1]["name"] == "k8s_describe_pod"
    assert out[1]["arguments"] == '{"name":"x"}'


def test_extract_tool_calls_handles_unmatched_call() -> None:
    """tool_call が来たが output が来なかった場合も result='' で記録される."""
    items = [
        _mk_tool_call_item("c1", "k8s_list_pods"),
    ]
    out = _extract_tool_calls(items)
    assert len(out) == 1
    assert out[0]["name"] == "k8s_list_pods"
    assert out[0]["result"] == ""


def test_extract_tool_calls_truncates_long_output() -> None:
    items = [
        _mk_tool_call_item("c1", "k8s_pod_logs"),
        _mk_tool_output_item("c1", "x" * 1000),
    ]
    out = _extract_tool_calls(items)
    assert len(out[0]["result"]) == 500


# ---------------------------------------------------------------------------
# _build_sdk_agent: tools の構成 (MCP フラグで切替)
# ---------------------------------------------------------------------------


def test_build_sdk_agent_without_mcp(agent: Agent) -> None:
    with patch("ta.agent.core.get_settings") as mock_gs:
        mock_gs.return_value = MagicMock(mcp_grafana_enabled=False)
        sdk_agent = agent._build_sdk_agent("test-instructions")
    assert sdk_agent.name == "telemetry-analyst"
    assert len(sdk_agent.tools) == 7  # k8s ツール 7 種のみ


def test_build_sdk_agent_with_mcp(agent: Agent) -> None:
    with patch("ta.agent.core.get_settings") as mock_gs:
        # MCP を有効化. URL 等は make_grafana_mcp_tool 内でも settings を引くので mock を整える
        mock_gs.return_value = MagicMock(
            mcp_grafana_enabled=True,
            mcp_grafana_url="https://test.example/mcp",
        )
        sdk_agent = agent._build_sdk_agent("test-instructions")
    assert len(sdk_agent.tools) == 8  # K8s 7 + MCP 1
