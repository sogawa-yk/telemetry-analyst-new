"""Skills (playbooks) の動的ロード + retrieve.

`skills/*.md` を frontmatter 付き markdown として読み込み、ユーザー発話とモードから
適合する skill を 1〜3 本選んでシステムプロンプトに挿入する。

retrieve 戦略は初期版ではキーワード一致 + mode フィルタ。後に埋め込み検索へ昇格可能。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import frontmatter

Mode = Literal["beginner", "engineer", "any"]


@dataclass
class Skill:
    name: str
    triggers: list[str] = field(default_factory=list)
    mode: Mode = "any"
    content: str = ""

    def matches(self, query: str, mode: Mode) -> int:
        """質問に対する適合スコア. 0 以下は不適合."""
        # モードフィルタ: skill の mode が "any" 以外で、かつ現モードと一致しなければ不適合
        if self.mode != "any" and self.mode != mode:
            return -1

        # trigger がひとつも無い skill は常時注入扱い (explain-* 等)
        if not self.triggers:
            return 1

        # 半角/全角・大文字小文字の揺れを吸収するため NFKC + casefold で正規化
        q_norm = unicodedata.normalize("NFKC", query).casefold()
        score = 0
        for t in self.triggers:
            t_norm = unicodedata.normalize("NFKC", t).casefold()
            if t_norm and t_norm in q_norm:
                score += 2
        return score


class SkillRetriever:
    """skills/ ディレクトリから全 skill を読み込み、retrieve する."""

    def __init__(self, skills_dir: Path | str = "skills"):
        self.skills_dir = Path(skills_dir)
        self._skills: list[Skill] = []
        self._load()

    def _load(self) -> None:
        self._skills = []
        if not self.skills_dir.exists():
            return
        for md in sorted(self.skills_dir.glob("*.md")):
            post = frontmatter.load(md)
            meta = post.metadata
            self._skills.append(
                Skill(
                    name=str(meta.get("name", md.stem)),
                    triggers=[str(t) for t in (meta.get("triggers") or [])],
                    mode=str(meta.get("mode", "any")),  # type: ignore[arg-type]
                    content=post.content.strip(),
                )
            )

    def pick(self, query: str, mode: Mode, k: int = 3, threshold: int | None = None) -> list[Skill]:
        """常時注入 (triggers=[]) は mode に合えば必ず選び、残りはスコア >= threshold で k 本."""
        # threshold 未指定なら settings から取得 (テストで上書き可能なよう引数化)
        if threshold is None:
            try:
                from ta.config import get_settings  # 遅延 import (循環回避)

                threshold = get_settings().skill_retriever_threshold
            except Exception:
                threshold = 1
        always: list[Skill] = []
        scored: list[tuple[int, Skill]] = []
        for s in self._skills:
            score = s.matches(query, mode)
            if score < 0:
                continue
            if not s.triggers:
                always.append(s)
            elif score >= threshold:
                scored.append((score, s))

        scored.sort(key=lambda x: x[0], reverse=True)
        picked = [s for _, s in scored[:k]]
        return always + picked

    def render(self, query: str, mode: Mode, k: int = 3) -> str:
        """選んだ skill の本文を連結して返す (システムプロンプトへの差込み用)."""
        skills = self.pick(query, mode, k=k)
        if not skills:
            return ""
        sections = []
        for s in skills:
            sections.append(f"### Skill: {s.name}\n\n{s.content}")
        return "## 参考プレイブック\n\n" + "\n\n".join(sections)
