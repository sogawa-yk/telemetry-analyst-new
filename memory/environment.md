# ec-shop 環境情報 (システムプロンプトに常時注入)

## 監視対象

- Namespace: `ec-shop`
- 主なサービス: 実際のサービス名は `k8s_list_deployments` で都度確認すること.
  デフォルト Deployment は `ec-web` (Web フロント) と `load-generator` の 2 つ.

## メトリクス命名規則 (重要)

ec-shop アプリは **`ec_` 接頭辞**付きで Prometheus メトリクスを公開している. Loki / Tempo の検索ラベルも実環境のラベル名に従うこと.

- HTTP リクエスト系: **`ec_http_requests_total`** / **`ec_http_request_duration_seconds_bucket`** など
- 他のドメインメトリクス: `ec_active_requests`, `ec_cart_items_total`, `ec_db_pool_used`, `ec_admin_actions_total` など (`ec_` で始まる)
- サービス絞り込みラベル: **`service="ec-web"`** (一般的な `app=` ではない. `job` と `service` どちらも使えるが `service` を優先)
- HTTP ステータス: **`status="500"`** / `status=~"5.."` (`code=` ではない)
- HTTP メソッド: `method="GET"`
- HTTP パス: **`exported_endpoint="/path"`** (Prometheus client_python の慣例で `endpoint` ではなく `exported_endpoint`)

> 不明な場合は `list_prometheus_metric_names` で `regex="^ec_"` を打つ、または `list_prometheus_label_names` でラベルを確認してから query を組むこと.

## 観測基盤

- Prometheus: クラスタ内共通 (`namespace="ec-shop"` + 上記命名規則)
- Loki: ラベルは `{namespace="ec-shop", service="ec-web"}` から始める. 不明なら `list_loki_label_names` で確認.
- Tempo: 分散トレース. `find_slow_requests` でスロースパン抽出可.
- Grafana: `search_dashboards` で該当ダッシュボード候補を検索.

## SLO (初期値、未確定なら未記入)

- checkout: p99 latency < 500ms / エラー率 < 0.1%
- catalog: p99 latency < 200ms / エラー率 < 0.5%
- その他サービス: 運用チームに確認

## 既知の注意点

- フロントエンドからのリクエストは `frontend → checkout → payment` のフローが多い
- カナリアデプロイを使っている時間帯 (平日 10:00-12:00 JST) は一時的に latency が揺れる可能性
- 深夜バッチ (JST 02:00-04:00) 中は一部メトリクスが通常と異なる挙動を示すことがある

### 障害パターン早見表 (旧版 playbook 由来)

| 症状 | 第一に疑う原因 | 確認手段 |
| --- | --- | --- |
| 5xx 急増 (HTTP storm) | 直近デプロイ / 下流 (DB) 接続枯渇 / メモリ逼迫 | `k8s_list_deployments` のイメージタグ時刻 / `find_error_pattern_logs` |
| latency 悪化 | CPU throttling / GC スパイク / 下流レイテンシ | CPU throttling PromQL → `find_slow_requests` で span ツリー |
| Pod 再起動ループ | OOMKilled / liveness probe 失敗 / 設定不備 | `k8s_list_events` の reason / `k8s_pod_logs(previous=true)` |
| DB プール枯渇 | 同時接続数の急増 / コネクションリーク | アプリログの `pool exhausted` / 接続数メトリクス |
| 容量逼迫 (Evicted) | ノードの memory pressure / disk pressure | Event の `Evicted` reason / Node 状態 |

## クエリテンプレート (ec-shop 想定)

LLM はここの式をベースに `app` ラベルや時間範囲を差替えて使うこと。`<svc>` は `checkout` `catalog` 等。

### Prometheus

- **エラー率** (5xx, 全体):
  ```
  sum(rate(ec_http_requests_total{namespace="ec-shop", status=~"5.."}[5m]))
    / sum(rate(ec_http_requests_total{namespace="ec-shop"}[5m]))
  ```

- **エラー率 (サービス・エンドポイント別)**:
  ```
  sum by (service, exported_endpoint) (rate(ec_http_requests_total{namespace="ec-shop", status=~"5.."}[5m]))
    / sum by (service, exported_endpoint) (rate(ec_http_requests_total{namespace="ec-shop"}[5m]))
  ```

- **p99 latency**:
  ```
  histogram_quantile(0.99, sum by (le) (rate(ec_http_request_duration_seconds_bucket{namespace="ec-shop"}[5m])))
  ```

- **p99 latency (サービス別)**:
  ```
  histogram_quantile(0.99, sum by (service, le) (rate(ec_http_request_duration_seconds_bucket{namespace="ec-shop"}[5m])))
  ```

- **DB 接続プール使用率**:
  ```
  ec_db_pool_used{namespace="ec-shop"}
  ```

- **CPU throttling 比率** (>0.05 で要警戒):
  ```
  rate(container_cpu_cfs_throttled_periods_total{namespace="ec-shop", pod=~"ec-web.*"}[5m])
    / rate(container_cpu_cfs_periods_total{namespace="ec-shop", pod=~"ec-web.*"}[5m])
  ```

- **メモリ使用率** (limit 比 / 0.9 超で OOM 警戒):
  ```
  container_memory_working_set_bytes{namespace="ec-shop", pod=~"ec-web.*"}
    / container_spec_memory_limit_bytes{namespace="ec-shop", pod=~"ec-web.*"}
  ```

- **HPA 飽和** (current=max なら頭打ち):
  ```
  kube_horizontalpodautoscaler_status_current_replicas{namespace="ec-shop"}
    / kube_horizontalpodautoscaler_spec_max_replicas{namespace="ec-shop"}
  ```

- **直近 OOMKilled の有無**:
  ```
  kube_pod_container_status_last_terminated_reason{namespace="ec-shop", reason="OOMKilled"}
  ```

### LogQL

- **エラー抽出 (起点)**:
  ```
  {namespace="ec-shop", service="ec-web"} |~ "(?i)(error|exception|timeout|panic)" | json | limit 200
  ```

- **特定パターン頻度** (Loki stats):
  ```
  count_over_time({namespace="ec-shop", service="ec-web"} |= "<keyword>" [15m])
  ```

### Tempo

- スローエンドポイント抽出: `find_slow_requests(service="ec-web")`
- 個別 span ツリー: `get_trace_by_id(trace_id)`

## 過去インシデント (参考、随時追記)

- (ユーザーから過去事例を共有してもらい、ここに追記する)

## 権限境界

- 読み取り可能: `ec-shop` NS の pods, pods/log, services, deployments, replicasets, events, hpa, configmaps
- **読み取り不可**: kube-system / observability / その他の NS、Secrets の中身、書込み全般
