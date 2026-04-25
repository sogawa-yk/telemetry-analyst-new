# ec-shop 環境情報 (システムプロンプトに常時注入)

## 監視対象

- Namespace: `ec-shop`
- 主なサービス (想定): `checkout`, `catalog`, `cart`, `payment`, `order`, `frontend` など
  - 実際のサービス名は `k8s_list_deployments` で確認すること
- コンテナラベル慣例: `app=<service>` (Prometheus / Loki のラベルで利用)

## 観測基盤

- Prometheus: クラスタ内共通 (ラベル `namespace="ec-shop"` で絞る)
- Loki: 同上。`{namespace="ec-shop", app="<svc>"}` から始める
- Tempo: 分散トレース。`find_slow_requests` でスロースパン抽出可
- Grafana: `search_dashboards` で該当ダッシュボード候補を検索

## SLO (初期値、未確定なら未記入)

- checkout: p99 latency < 500ms / エラー率 < 0.1%
- catalog: p99 latency < 200ms / エラー率 < 0.5%
- その他サービス: 運用チームに確認

## 既知の注意点

- フロントエンドからのリクエストは `frontend → checkout → payment` のフローが多い
- カナリアデプロイを使っている時間帯 (平日 10:00-12:00 JST) は一時的に latency が揺れる可能性
- 深夜バッチ (JST 02:00-04:00) 中は一部メトリクスが通常と異なる挙動を示すことがある

## 過去インシデント (参考、随時追記)

- (ユーザーから過去事例を共有してもらい、ここに追記する)

## 権限境界

- 読み取り可能: `ec-shop` NS の pods, pods/log, services, deployments, replicasets, events, hpa, configmaps
- **読み取り不可**: kube-system / observability / その他の NS、Secrets の中身、書込み全般
