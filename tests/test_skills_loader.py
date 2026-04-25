"""Skills ローダと retriever の回帰テスト."""

from __future__ import annotations

from pathlib import Path

import pytest

from ta.agent.skills.loader import SkillRetriever

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def retriever() -> SkillRetriever:
    return SkillRetriever(skills_dir=REPO / "skills")


def test_loads_all_skills(retriever: SkillRetriever) -> None:
    names = {s.name for s in retriever._skills}
    assert "latency-regression" in names
    assert "error-rate-spike" in names
    assert "pod-crashloop" in names
    assert "oom-kill" in names
    assert "explain-beginner" in names
    assert "explain-engineer" in names


def test_triggers_are_stringified(retriever: SkillRetriever) -> None:
    """YAML が数値 (500/503 等) を含んでも str に揃っていること."""
    for s in retriever._skills:
        for t in s.triggers:
            assert isinstance(t, str)


def test_latency_keyword_picks_latency_skill(retriever: SkillRetriever) -> None:
    picks = retriever.pick("checkout の p99 が悪化している", mode="engineer")
    names = [s.name for s in picks]
    assert "latency-regression" in names
    assert "explain-engineer" in names  # 常時注入 (engineer モード)
    assert "explain-beginner" not in names  # 他モードは除外


def test_beginner_mode_injects_beginner_explain(retriever: SkillRetriever) -> None:
    picks = retriever.pick("エラーが増えた", mode="beginner")
    names = [s.name for s in picks]
    assert "error-rate-spike" in names
    assert "explain-beginner" in names
    assert "explain-engineer" not in names


def test_render_returns_markdown_section(retriever: SkillRetriever) -> None:
    rendered = retriever.render("Pod が CrashLoop", mode="engineer")
    assert "## 参考プレイブック" in rendered
    assert "pod-crashloop" in rendered


def test_cost_aware_query_always_injected(retriever: SkillRetriever) -> None:
    """trigger が空の skill は (モード適合なら) 常時注入される."""
    picks = retriever.pick("どうでもいい質問", mode="engineer")
    names = {s.name for s in picks}
    assert "cost-aware-query" in names


# ---------------------------------------------------------------------------
# Phase B-4 で改善するための起点テスト (現状失敗 → 同義語拡張で通す)
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason="Phase B-4 で通す予定. trigger に Evicted が無いため現状ヒットしない.",
    strict=True,
)
def test_evicted_query_picks_oom_kill(retriever: SkillRetriever) -> None:
    """Pod が Evicted されたケースも oom-kill / capacity 系へ向ける."""
    picks = retriever.pick("Pod が Evicted されている", mode="engineer")
    names = {s.name for s in picks}
    assert "oom-kill" in names


@pytest.mark.xfail(
    reason="Phase B-4 で通す予定. error-rate-spike の trigger に「応答時間」「劣化」等が無い.",
    strict=True,
)
def test_japanese_synonym_picks_latency_regression(retriever: SkillRetriever) -> None:
    """「応答時間が劣化している」でも latency-regression にヒットすること."""
    picks = retriever.pick("checkout の応答時間が劣化している", mode="engineer")
    names = {s.name for s in picks}
    assert "latency-regression" in names
