# 判断品質改善ループ 記録 (P5b)

このファイルはエージェントの判断品質を磨くための周回記録です。
LLM-as-Judge (Langfuse) の自動採点 + 人間 (Claude) の目視レビューの二系統で点検し、
**最低 10 周**、以下の終了条件を満たすまで回します。

## 終了条件

- 10 周以上実施
- かつ LLM-as-Judge 全 6 観点の平均スコアが **0.8 以上**
- かつ 連続 2 周で人間レビューから新規不適切判断が出ない

## 運用コマンド (毎周)

```bash
# 1. 初回のみ: Dataset と Evaluator のセットアップ
python scripts/setup_langfuse.py

# 2. 1 周の実行 (全 15 item に対し Experiment Run)
python scripts/run_experiment.py --label iter-NN --description "..."

# 3. Langfuse UI で Experiment Run を開き、自動スコアを確認
#    → 低スコア / 無作為 3〜5 件を目視

# 4. 修正対象を分類して修正
#    a) tool description    (src/ta/agent/tools/*.py の TOOL_SPECS)
#    b) skill               (skills/*.md の内容 / triggers)
#    c) system prompt       (src/ta/agent/prompts/*.md)
#    d) ツール粒度          (引数設計 / 戻り値フォーマット)
#    e) 戻り値フォーマット  (k8s_*.py の整形処理)

# 5. 下記の 「周回記録」 に追記
```

## 周回記録

### Iter-00 (初回、基準値取得)

- **日時**: (未実行)
- **Experiment Run 名**: `iter-00`
- **LLM-as-Judge 平均スコア**:
  - tool-selection: (pending)
  - query-correctness: (pending)
  - hypothesis-grounding: (pending)
  - mode-adherence: (pending)
  - skill-pick-accuracy: (pending)
  - safety-rbac-boundary (pass 率): (pending)
- **人間レビュー所見**: (pending)
- **修正**: なし (基準値取得のみ)

---

### Iter-01

- **日時**: (pending)
- **Experiment Run 名**: `iter-01`
- **LLM-as-Judge 平均スコア**: (pending)
- **人間レビュー所見**: (pending)
- **修正**: (pending)
- **期待効果**: (pending)

---

### Iter-02 〜 Iter-10

(各周、上記テンプレートで追記)

---

## 気づきログ (周回共通)

(10 周全体で見えた傾向をここに書く。次のバージョンへの申し送り)
