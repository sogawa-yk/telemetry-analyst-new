---
name: error-rate-spike
triggers:
  - 5xx
  - error
  - エラー
  - "500"
  - "503"
  - 急増
  - spike
  - 増えた
  - エラー率
  - 失敗
  - 失敗率
mode: any
---

## 適用タイミング

「5xx が急増」「エラーが増えた」系の質問。

## 進め方

1. **エラー率の推移**
   - PromQL 例: `sum(rate(http_requests_total{namespace="ec-shop",app="<svc>",code=~"5.."}[5m])) / sum(rate(http_requests_total{namespace="ec-shop",app="<svc>"}[5m]))`
   - 発生開始時刻を特定 (どの時間帯から上がったか)
2. **エラーパターンを Loki で抽出**
   - `find_error_pattern_logs` (Sift) で異常に増えているログパターンを自動抽出
   - 必要ならユーザー指定条件で `query_loki_logs` を追加
3. **直前の変更 (デプロイ)** を確認
   - `k8s_list_deployments` で該当サービスの更新時刻・イメージタグを確認
   - 発生開始時刻と重なっていれば疑い濃厚
4. **下流依存の可用性**
   - 下流 API / DB が落ちていないか、依存先のエラーメトリクス確認
5. **アラート連携**
   - `list_alert_rules` で関連アラートの発火状況

## 回答に含めるべき項目

- 発生開始時刻とピーク値
- エラーパターンの代表ログ行
- 疑わしい原因 (直近デプロイ / 下流 / リソース枯渇 等)
- ロールバック等の推奨アクション
