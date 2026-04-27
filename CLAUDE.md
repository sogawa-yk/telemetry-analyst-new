# CLAUDE.md — Telemetry Analyst (ec-shop 診断エージェント) 開発ガイド

## プロジェクト概要

Kubernetes 上の `ec-shop` ネームスペース配下のアプリケーションを、Prometheus / Loki / Tempo で
収集されたテレメトリと Kubernetes の状態を用いて自然言語で診断する AI エージェント。

- **配置 NS**: `telemetry-analyst` (既存 Secret / Ingress を再利用)
- **監視対象 NS**: `ec-shop`
- **旧版**: `/home/opc/Github/telemetry-analyst/` の後継。OCID・OCIR イメージ名・Ingress ホスト名など
  具体値は旧版から**踏襲**する。

## 運用原則

### 1. 日本語ファースト

仕様・コメント・コミットメッセージ・PR は日本語。コードの識別子は英語。

### 2. 単一 ReAct + Context Engineering

LangGraph の state machine は**組まない**。Responses API のネイティブ tool calling ループに任せる。
質を決めるのは:

- **Tools**: 少数精鋭、description と戻り値フォーマットを丁寧に
- **Skills (playbooks)**: `skills/*.md` を動的注入
- **Instructions**: system + mode + environment を重ねる
- **Memory**: Responses API / Conversations API に委任 (自前ストアは作らない)

### 3. 可観測性二系統

- **LLM 挙動**: Langfuse (自動トレース + LLM-as-Judge)
- **ランタイム**: OpenTelemetry → 既存 Prometheus / Loki / Tempo → Grafana

いずれも**計装なしのマージは禁止**。

### 4. 改善ループ (LLM-as-Judge + 人間レビューの二系統)

完成条件は「動く」ではなく「判断が適切」。

1. ゴールデンセットを Langfuse Dataset Experiment で実行
2. LLM-as-Judge が 6 観点で自動採点
3. Claude が低スコア + 抽出ケースを目視確認
4. 問題を (a) ツール description (b) skill (c) prompt (d) ツール粒度 (e) 戻り値 に分類
5. 該当箇所のみ修正 (コード大改修は避ける)
6. `eval/iterations.md` に記録
7. **最低 10 周**

### 5. 読み取り専用原則

K8s / Grafana / Prometheus / Loki / Tempo いずれも**書込 API は呼ばない**。
RBAC (Role スコープ) と実装レイヤの双方で遮断する二重防御。

## 具体値 (旧版から踏襲、原文)

- OCI Project OCID: `ocid1.generativeaiproject.oc1.ap-osaka-1.amaaaaaassl65iqak67q6dr5zu6jqoimgf54sylota5devqglzkkoenxznxa`
- OCI Endpoint: `https://inference.generativeai.ap-osaka-1.oci.oraclecloud.com/openai/v1`
- OCIR: `syd.ocir.io/orasejapan/telemetry-analyst[-ui]:<TAG>`
- Ingress `ingressClassName`: `ic`
- ホスト: `ta.sogawa-yk.com` (UI) / `ta-api.sogawa-yk.com` (API)
- TLS Secret: `wildcard-sogawa-yk-com-tls`
- 主要 Secret: `oci-genai-key` / `langfuse-keys` / `ocir`

## 旧版との主な差分

| 項目 | 旧版 | 新版 |
| --- | --- | --- |
| エージェント実装 | LangGraph ReAct + 自作 A2A | 単一 ReAct (OpenAI Agents SDK) + 公式 a2a-sdk |
| LLM API | Responses + Chat Completions 併用 | Responses API に一本化 |
| メモリ | PostgreSQL + pgvector フォールバック | Conversations API 委任 |
| ツール MCP 連携 | langchain-mcp-adapters | Responses API の `tools: {type: "mcp"}` |
| LLM-as-Judge | 未設定 | 初期セットアップから実施 |
| A2A | 自作 JSON-RPC 薄実装 | 公式 `a2a-sdk` v1.0 (v0.2.9〜). サーバ側 expose のみ、ClusterIP 限定、Bearer Token 認証 |

## 開発コマンド

```bash
# セットアップ
pip install -e '.[dev,ui]'

# lint / format
ruff check src/ tests/
ruff format src/ tests/

# 型チェック
mypy src/

# テスト
pytest

# ローカル実行
python -m ta.cli "ec-shop の catalog の 5xx を調べて"

# Chainlit UI
chainlit run src/ta/ui/chainlit_app.py --port 8081

# ゴールデンセット実行
python -m ta.eval.run_golden_set
```

## 参考

- 設計プラン: `/home/opc/.claude/plans/kubernetes-ec-shop-prometheus-loki-temp-fancy-hartmanis.md`
- 旧版実装: `/home/opc/Github/telemetry-analyst/`
