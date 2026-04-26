---
name: cost-aware-query
triggers: []
mode: any
---

## クエリ作法 (常時注入)

Prometheus / Loki / Tempo を重くしないための原則:

- **時間範囲は絞る**: デフォルトで `[5m]`〜`[1h]`。24h を超える範囲は必要性を明示してから使う。
- **Loki は `|~` や `|= ""` ノイズを避ける**: ラベルで先に絞り込んでから正規表現
- **`limit` を付ける**: Loki `query_loki_logs` は必ず `limit=200` 等を設定
- **集計は `sum by (...)` / `topk(...)` で**: ラベル爆発を避ける
- **histogram 系は `_bucket` + `rate` + `histogram_quantile` の順で**
- **全期間スキャンは禁止**: `{}` のような空ラベルクエリは打たない

## 探索系ツールの呼出回数制限 (重要)

メトリクス命名や label 探索でツールを冗長に呼ぶ事故が多い. 以下を遵守:

- **`list_prometheus_metric_names`**: **1 質問につき最大 1 回**. 結果を覚えて再利用する.
- **`list_prometheus_label_names` / `list_prometheus_label_values`**: **1 質問につき合計 2 回まで**. 既知の label (`namespace`, `service`, `pod`, `status` 等) には呼ばない.
- **`list_loki_label_names` / `list_loki_label_values`**: 同上、合計 2 回まで.
- **同じツールを同じ引数で 2 回呼んではならない**: 1 回目の結果を必ず参照する.

## 直行ツールの優先

以下の質問パターンには **専用ツールを最優先**で使う (Grafana 検索や label 探索を経由しない):

- 「いま発火しているアラートは?」「アラート確認」 → **`list_alert_rules`** を 1 回呼ぶだけで十分
- 「ダッシュボードはどれ?」「Grafana のどこ見れば?」 → **`search_dashboards`** を 1 回
- 「Pod 一覧 / 状態は?」 → **`k8s_list_pods`** を 1 回
- 「Deployment のイメージは?」 → **`k8s_list_deployments`** を 1 回
