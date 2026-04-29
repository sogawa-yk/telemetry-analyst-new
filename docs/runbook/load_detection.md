# 負荷シナリオ × telemetry-analyst 検出能力の検証

`chaos/k6/*.js` と `chaos/scripts/k6-in-cluster.sh` を使って ec-shop に意図的な負荷を掛け、UI 経由で telemetry-analyst が異常を正しく検知できるかを **手動レビュー** で確かめる runbook.

## 1. 前提

- 新クラスタ KIDS2 (`devday26.sogawa-yk.com`) 上で:
  - `ec-shop/ec-web` Deployment が稼働. ServiceMonitor 経由で `ec_http_requests_total` / `ec_http_request_duration_seconds` / `ec_active_requests` / `ec_orders_total` / `ec_db_pool_used` 等が Prometheus に流れている
  - `telemetry-analyst/ta-agent` が稼働. UI は `https://ta.devday26.sogawa-yk.com`
  - `kubectl` コンテキストが新クラスタを向いている
- k6 は `grafana/k6:0.52.0` イメージを Pod として ec-shop NS で実行する (ホストへの k6 インストール不要)
- 既存の `ec-shop/load-generator` Deployment (curl ループ, ~20 req/s baseline) は **動かしたまま**. k6 はその上に上乗せするチャオス用途

> **判定対象は ec-shop 側の `ec_*` メトリクスと Loki ログ**. k6 自身のメトリクスは Prometheus に送らない (stdout のみ).

### 1.1 実行前のプリフライトチェック (重要)

**`spike-load` / `ramp-load` を起動する前に必ず確認**. クエリ流量が増えると Grafana / Prometheus / mcp-grafana のリソース不足が顕在化し、telemetry-analyst が tool 呼出 hang で「フリーズ」する事故に繋がる. 下記の最小要件を満たしていないと、k6 自体は完走しても **Grafana が OOMKill して関連サービス (ta-agent / mcp-grafana 経由のクエリ) が連鎖で落ちる**.

| コンポーネント | 最小要件 (limits) | 確認コマンド |
|---|---|---|
| `prometheus-grafana` (grafana コンテナ) | CPU ≥ 1000m / Mem ≥ 1Gi | `kubectl get deploy prometheus-grafana -n observability -o jsonpath='{.spec.template.spec.containers[?(@.name=="grafana")].resources}'` |
| `prometheus-kube-prometheus-prometheus` | CPU ≥ 500m / Mem ≥ 2Gi | (kube-prometheus-stack のデフォルトで足りる場合が多い) |
| `mcp-grafana` (telemetry-analyst NS) | CPU ≥ 200m / Mem ≥ 256Mi | `kubectl get deploy mcp-grafana -n telemetry-analyst -o jsonpath='{.spec.template.spec.containers[0].resources}'` |

不足があれば事前に `kubectl set resources` または Helm values で上げる. 永続化は `devday-k8s-template/k8s/observability-template/generated/kube-prometheus-stack/values.yaml` の `grafana.resources` を編集して `helm upgrade prometheus prometheus-community/kube-prometheus-stack -n observability -f <values>`.

### 1.2 想定外のフリーズが起きた時の応急対応

```bash
# 1) k6 Job を全停止
chaos/scripts/k6-in-cluster.sh --cleanup

# 2) Grafana が unhealthy なら restart (Helm values 修正 + helm upgrade で恒久対応)
kubectl rollout restart -n observability deployment/prometheus-grafana

# 3) ta-agent UI のフリーズは Grafana 復旧後に新セッションで再接続. ta-agent Pod 自体の restart は基本不要
```

## 2. シナリオ実行手順

### 起動

```bash
# クラスタ内 Service 経由 (デフォルト)
chaos/scripts/k6-in-cluster.sh constant-load
chaos/scripts/k6-in-cluster.sh spike-load
chaos/scripts/k6-in-cluster.sh ramp-load
chaos/scripts/k6-in-cluster.sh error-storm

# 外部 Ingress 経由 (TLS 含めて測りたい場合)
TARGET=https://ec-shop.devday26.sogawa-yk.com chaos/scripts/k6-in-cluster.sh spike-load
```

### 進捗確認

```bash
kubectl logs -n ec-shop -l scenario=<scenario-name> -f --tail=50
```

### 後片付け

```bash
chaos/scripts/k6-in-cluster.sh --cleanup   # 全 k6 Job/ConfigMap を削除
```

> Job には `ttlSecondsAfterFinished: 900` (15min) が付くので放置でも自動消滅する.

## 3. シナリオ × 検出マッピング

| k6 シナリオ | 想定症状 (ec-shop 側) | UI 投入質問例 | 期待される観測経路 / Skill |
|---|---|---|---|
| `constant-load` | 10 VU × 2min, p95 ≤ 800ms 継続. 異常なし | 「ec-shop の現在の状態を教えて」 | `query_prometheus` + `k8s_list_pods`. 異常なしと正直に答える. (overall-health-beginner-09 系) |
| `spike-load` | 0→200 VU spike, p99 急上昇. HPA 90% threshold で間に合わず 5xx 一時発生の可能性 | 「ec-shop の products API が遅い、原因を調べて」/「直近 10min で p99 が悪化したエンドポイントは?」 | `query_prometheus` (`histogram_quantile(0.99, ...)`) + `find_slow_requests`. **latency-regression** skill が triggers にヒット. (latency-checkout-01 / tempo-top-slow-07 系) |
| `ramp-load` | 1→100 VU, 5min ramp. 100 VU 持続中に DB pool maxconn=5 枯渇 + メモリ limit 192Mi で OOMKill 候補. HPA は遅れて反応 | 「ec-web の Pod が再起動している」/「ec-shop の checkout が遅いが、原因は下流?」 | `k8s_list_pods` + `k8s_describe_pod` + `k8s_list_events` (OOMKilled イベント) + `query_prometheus` (`ec_db_pool_used`, replicas). **pod-crashloop / oom-kill / downstream-dependency** skill が triggers にヒット. (pod-crashloop-05 / oom-pod-06 / downstream-dep-08 / slo-checkout-engineer-10 系) |
| `error-storm` | 30 VU × 3min, 4xx と `does-not-exist` 404, 不正 JSON, 異常 args の混合. Loki に多量の error ログが流れる | 「ec-shop の 4xx/5xx が急増している」/「直近 24h で増えた異常ログのパターンは?」 | `query_prometheus` (`ec_http_requests_total{status=~"4..\|5.."}`) + `find_error_pattern_logs` (Sift 抽出) + `query_loki_logs`. **error-rate-spike** skill が triggers にヒット. (error-catalog-5xx-03 / error-recent-24h-04 系) |

## 4. 検証プロトコル

各シナリオで以下を実施し、結果を §5 の検証ログ表に追記する.

1. **T-0**: シナリオを起動 (`chaos/scripts/k6-in-cluster.sh <name>`)
2. **T+30s〜1min**: UI で対応する質問を投入 (シナリオ毎の例は §3 参照). **新規スレッドで** 投げる (会話継続の影響を排除)
3. **応答を目視レビュー** し、以下 4 観点でチェック:
   - **Tool Selection**: マッピング表の「期待される観測経路」のツールが呼ばれているか
   - **Skill Pick**: 期待 skill (latency-regression / error-rate-spike / pod-crashloop 等) が発火しているか (応答内のフレーズや構造から推察可能)
   - **Hypothesis Grounding**: 仮説に具体根拠 (PromQL 式 / メトリクス値 / Pod 名 / 異常ログ抜粋) が紐付いているか
   - **時間範囲**: 直近 5–10min を見ているか. 24h を雑に見ていないか
4. **判定**: PASS / FAIL / 不確実 のいずれかを記録. FAIL の場合は不足観点を 1 行で添える
5. **T+post**: シナリオ完了後 cleanup

> `langfuse` トレースで実際に呼ばれた tool / model 出力を見るとより詳細に追える (`https://langfuse.devday26.sogawa-yk.com`).

## 5. 検証ログ (実施時に追記)

| 実施日 | シナリオ | 質問 | Tool | Skill | Grounding | 時間範囲 | 判定 | メモ |
|---|---|---|---|---|---|---|---|---|
|  | constant-load |  |  |  |  |  |  |  |
|  | spike-load |  |  |  |  |  |  |  |
|  | ramp-load |  |  |  |  |  |  |  |
|  | error-storm |  |  |  |  |  |  |  |

## 6. 判定基準 (cheat sheet)

| 観点 | PASS | FAIL |
|---|---|---|
| Tool Selection | 期待ツールが呼ばれた (順番不問) | 期待ツールが 1 つも呼ばれず推測のみ |
| Skill Pick | 応答に skill 由来の構造化された手順 / 観点が見える | 一般論で skill 発火の痕跡なし |
| Grounding | PromQL 式やメトリクス値、Pod 名、ログ抜粋が応答に含まれる | 「だと思います」「考えられます」だけで根拠ゼロ |
| 時間範囲 | 直近 5–15min を中心に | 24h などの広範囲に拡散 |

「3/4 観点 PASS」を最低ラインとし、ALL PASS を狙う.

## 7. 既知の限界

- `ramp-load` での OOMKill は ec-web の実メモリ使用量に依存. Memory limit 192Mi に対し 100 VU で実際にどこまで使うかは実測しないと分からない. OOMKill が起きない場合でも DB pool 枯渇 (`ec_db_pool_used` ≥ 5) は確実に観測できるため、検出対象としては成立する
- `/api/checkout` は `@require_login` で OIDC ログインが必須. 新クラスタでは Keycloak 未配置のため、checkout の 10% 故障注入を k6 から駆動することは現状不可. 必要になったら Keycloak をデプロイするか、login bypass の env flag を ec-shop に追加するか別計画
- HPA はメトリクスの推移で動くため、`spike-load` の 30s スパイクではスケールが間に合わずに 5xx が出てから収束する場合がある. これは検出対象として「むしろ健全」

## 8. 関連

- 既存ゴールデンセット: `eval/golden_set.yaml`
- 既存改善ループ記録: `eval/iterations.md`
- k6 シナリオ本体: `chaos/k6/*.js`
- 起動スクリプト: `chaos/scripts/k6-in-cluster.sh`
