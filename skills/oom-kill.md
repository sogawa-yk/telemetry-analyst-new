---
name: oom-kill
triggers:
  - OOM
  - OOMKilled
  - メモリ
  - memory
  - out of memory
  - メモリ不足
  - Evicted
  - メモリリーク
  - memory leak
mode: any
---

## 適用タイミング

「Pod が OOM で落ちる」「メモリが枯渇」系の質問。

## 進め方

1. **OOMKilled Pod の特定**
   - `k8s_list_events(namespace="ec-shop", since="30m")` で `OOMKilled` を検索
   - `k8s_describe_pod` で Last State の Reason を確認
2. **メモリ使用量の推移**
   - PromQL: `container_memory_working_set_bytes{namespace="ec-shop",pod=~"ec-web.*"}`
   - コンテナ設定の limit と比較 (`kube_pod_container_resource_limits{resource="memory"}`)
3. **リーク vs 一時的なピーク**
   - 連続増加していればリーク疑い
   - スパイクが特定時刻のみならトラフィック起因
4. **QPS との相関**
   - `sum(rate(ec_http_requests_total{namespace="ec-shop"}[5m]))` とメモリ使用率を重ねる

## 回答に含めるべき項目

- いつ OOM が発生したか
- limit はいくらで、実使用がいくらだったか
- リーク型 / ピーク型 の判定
- 対処案 (limit 引上げ / ヒープ解析 / キャッシュ設定見直し等)
