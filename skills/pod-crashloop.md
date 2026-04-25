---
name: pod-crashloop
triggers: [crashloop, restart, 再起動, ImagePullBackOff, CreateContainerConfigError, Error]
mode: any
---

## 適用タイミング

「Pod が落ちている」「restart が多い」「起動しない」系の質問。

## 進め方

1. **該当 Pod の一覧と状態**
   - `k8s_list_pods(namespace="ec-shop")` で Running 以外 / restart 数 > 0 を抽出
2. **describe で直近イベントを見る**
   - `k8s_describe_pod(name)` で直近のイベント、終了コード、理由を確認
3. **ログで実際の失敗原因**
   - `k8s_pod_logs(name, container=..., tail=200)` で前回コンテナのログ
   - 必要なら `previous=true` 相当のフラグで直前の終了ログ
4. **Namespace イベント**
   - `k8s_list_events(namespace="ec-shop", since="30m")` でスケジューリング / ImagePull 系の問題
5. **リソース逼迫 / OOM の可能性**
   - OOMKilled の場合は `oom-kill` skill を参照
6. **設定変更の有無**
   - `k8s_list_deployments` で直前の変更を確認、ConfigMap / Secret の参照ミスが多い

## よくある原因

- ConfigMap / Secret の参照ミス (`CreateContainerConfigError`)
- imagePullSecrets 未設定 / タグ typo (`ImagePullBackOff`)
- 起動 Probe の設定が厳しすぎる
- 依存サービス未起動でアプリが exit
- OOMKilled (メモリ上限超過)

## 回答に含めるべき項目

- 該当 Pod と restart 回数
- 直近イベント / ログの代表行 (終了コード含む)
- 疑わしい原因
- 確認するべき Describe / ログコマンド
