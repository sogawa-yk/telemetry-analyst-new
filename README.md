# Telemetry Analyst (ec-shop 診断エージェント)

自然言語で Kubernetes 上の `ec-shop` の状態を問合せると、Prometheus / Loki / Tempo の
テレメトリと K8s 状態を横断調査し、根拠つきで原因仮説と対処案を提示する AI エージェント。

## 設計概要

- **LLM**: OCI Enterprise AI Responses API / Conversations API (OpenAI SDK 互換)
- **エージェント**: 単一 ReAct (Responses API のネイティブ tool calling ループ)
- **ツール**: Grafana MCP (Responses API ネイティブ MCP) + 自作 K8s 読取 function tools
- **メモリ**: Responses API / Conversations API に委任 (自前ストアなし)
- **Chat UI**: Chainlit
- **可観測性**: Langfuse (LLM 挙動) + OTel → 既存 Prometheus/Loki/Tempo → Grafana (ランタイム)
- **公開**: Kubernetes Ingress (IngressClass `ic`, `ta.sogawa-yk.com` / `ta-api.sogawa-yk.com`)

詳細は `/home/opc/.claude/plans/kubernetes-ec-shop-prometheus-loki-temp-fancy-hartmanis.md` 参照。

## ディレクトリ

```
src/ta/            エージェント本体
skills/            markdown で書く診断 playbook (動的注入)
memory/            長期知識 (ec-shop 構成 / SLO)
eval/              ゴールデンセット + LLM-as-Judge 評価テンプレ
grafana/dashboards エージェント自己観測用ダッシュボード
deploy/k8s/        Kubernetes マニフェスト
scripts/           ビルド / Langfuse セットアップ等
```

## ローカル起動 (P1)

```bash
pip install -e '.[dev,ui]'
cp .env.example .env   # 実値を入れる
python -m ta.cli "ec-shop の checkout のエラー率を教えて"
```

## Chainlit UI (P2)

```bash
python -m uvicorn ta.api.main:app --port 8080 &
chainlit run src/ta/ui/chainlit_app.py --port 8081 --headless
```

## Kubernetes デプロイ (P3)

```bash
./scripts/build_and_push.sh <TAG>
kubectl apply -f deploy/k8s/
```

## 改善ループ (P5b)

```bash
python scripts/setup_langfuse.py       # LLM Connection + Evaluator + Dataset
python -m ta.eval.run_golden_set       # 1 周実行 (Langfuse Experiment)
# Langfuse UI でスコア確認 → 該当箇所修正 → 再実行 を 10 周以上
```

### 負荷シナリオ × 検出検証

k6 で ec-shop に意図的な負荷を掛け、UI 経由で telemetry-analyst が検知できるかを手動レビューする。
詳細は [`docs/runbook/load_detection.md`](docs/runbook/load_detection.md)。

```bash
chaos/scripts/k6-in-cluster.sh spike-load    # 0→200 VU spike
chaos/scripts/k6-in-cluster.sh error-storm   # 4xx/404 ログ大量生成
chaos/scripts/k6-in-cluster.sh --cleanup     # 全 k6 Job/CM 削除
```
