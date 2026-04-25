---
name: latency-regression
triggers:
  - latency
  - p99
  - p95
  - slow
  - 遅い
  - レイテンシ
  - 遅延
  - timeout
  - 応答時間
  - 劣化
  - 重い
  - 反応が悪い
mode: any
---

## 適用タイミング

「○○ が遅い」「p99 が悪化」「タイムアウトが増えた」系の質問。

## 進め方

1. **全体の latency 傾向**をまず見る
   - PromQL 例: `histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{namespace="ec-shop",app="<svc>"}[5m])) by (le))`
   - 直近 1h と前日同時刻を比較して差分を定量化
2. **スロークエリの実トレース**を Tempo で取る
   - `find_slow_requests` (Sift) で該当サービスの遅いトレース id を取得
   - スパンのどこで時間を食っているかを確認 (自サービス内 vs 下流依存)
3. **下流依存の状態**を確認
   - 依存先サービスの RED メトリクス (Request, Error, Duration)
   - DB / 外部 API へのレイテンシがトレース上で分かれば原因が絞れる
4. **リソース逼迫の有無**
   - Pod の CPU throttling (`rate(container_cpu_cfs_throttled_periods_total[5m])`)
   - メモリ使用率、GC 頻度
5. **トラフィック急増の有無**
   - QPS (`sum(rate(http_requests_total[5m]))`) が増えているか
   - HPA の動作状況 (`k8s_list_hpa`)

## 回答に含めるべき項目

- 悪化幅 (何倍 / 何 ms 増)
- 原因の候補と確度
- どのスパン / どの下流でボトルネックが出ているか
- 次に見るべきメトリクス or ログ
