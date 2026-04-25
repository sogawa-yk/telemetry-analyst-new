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
