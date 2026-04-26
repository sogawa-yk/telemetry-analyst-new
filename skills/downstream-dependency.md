---
name: downstream-dependency
triggers: [下流, downstream, 依存, dependency, DB, database, 外部API, timeout, connection]
mode: any
---

## 適用タイミング

「他サービスが原因っぽい」「DB が遅い?」「外部 API で詰まっている?」系の質問。

## 進め方

1. **Tempo で span ツリーを見る**
   - `find_slow_requests` で遅いトレースを取得し、子 span のうち時間を食っているものを特定
   - 子 span の `service.name` と `peer.service` で下流を特定
2. **下流サービスの RED 指標** (ec-shop は `ec_` 接頭辞 + `service=` ラベル)
   - `sum by (service) (rate(ec_http_requests_total{namespace="ec-shop"}[5m]))`
   - `sum by (service) (rate(ec_http_requests_total{namespace="ec-shop",status=~"5.."}[5m]))`
   - p99 latency: `histogram_quantile(0.99, sum by (service, le) (rate(ec_http_request_duration_seconds_bucket{namespace="ec-shop"}[5m])))`
3. **接続数 / キュー深さ**
   - DB プール使用率: `ec_db_pool_used{namespace="ec-shop"}`
   - gRPC keepalive、HTTP keep-alive の状況
4. **Loki で下流側のログ抜粋**
   - `query_loki_logs` で該当下流のエラーログを時系列で確認

## 回答に含めるべき項目

- どの下流サービス / DB / 外部 API がボトルネックか
- その裏付け (trace 抜粋 + 下流の RED メトリクス)
- 下流が問題なら、下流の担当チームにエスカレーションすべき旨
