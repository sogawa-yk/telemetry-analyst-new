# Langfuse LLM-as-Judge セットアップ手順書

Phase D (改善ループ) で 6 種の Evaluator を Langfuse 上で動かすための **手動 UI 設定手順** をまとめる。Evaluator と LLM Connection の自動登録 API は 2026 年 4 月時点で不安定 / 非公開のため、本書の通り UI 操作する。

対象 Langfuse: **v3.167.x** (`http://langfuse-web.langfuse.svc.cluster.local:3000`)。バージョンが異なる場合 UI 文言が一部変わる可能性がある。

> 関連自動化スクリプト: `scripts/setup_langfuse.py` は **Dataset 作成のみ自動** で、Evaluator/LLM Connection は本書の通り手動。`scripts/run_experiment.py` は Dataset Run を実行する。Langfuse 公式 API が安定したら本書をスクリプト化する。

---

## 前提

1. **Langfuse が起動している**: pod 内から `curl ${LANGFUSE_HOST}/api/public/health` で `{"status":"OK"}` が返る
2. **環境変数が pod に注入されている**: `LANGFUSE_HOST` / `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `OPENAI_API_KEY` / `OCI_GENAI_PROJECT`
3. **Dataset 作成済**: `python scripts/setup_langfuse.py` を 1 回実行して `telemetry-analyst-golden` Dataset と 15 個の Item が登録されている
4. **OCI Enterprise AI へ pod から到達できる**: `OPENAI_BASE_URL` の Responses API が 200 を返す

確認:
```bash
POD=$(kubectl get pod -n telemetry-analyst -l app=ta-agent -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n telemetry-analyst $POD -- bash -c '
  python -c "from urllib.request import urlopen; print(urlopen(\"$LANGFUSE_HOST/api/public/health\").read())"
'
```

---

## Step 1: LLM Connection (判事モデル) を登録

LLM-as-Judge の評価は Langfuse 自身が LLM を呼ぶ。本プロジェクトでは **OCI Enterprise AI (`openai.gpt-4.1`)** を判事として使う。

### 1-A. Direct provider 方式 (推奨)

Langfuse v3 は OpenAI 互換 LLM Connection でカスタムヘッダ (`OpenAI-Project`) を付与できる場合がある。まずこちらを試す。

1. Langfuse UI を開き、対象 Project を選択
2. 左メニュー → `Settings` → `LLM Connections`
3. `+ Add LLM Connection` をクリック
4. 以下を入力:
   | フィールド | 値 |
   |---|---|
   | Provider | `OpenAI` |
   | Connection Name | `oci-enterprise-ai` |
   | API Key | `OPENAI_API_KEY` の実値 (Secret `oci-genai-key/api_key`) |
   | Advanced Settings → Base URL | `https://inference.generativeai.ap-osaka-1.oci.oraclecloud.com/openai/v1` |
   | Advanced Settings → Custom Headers | `OpenAI-Project: ocid1.generativeaiproject.oc1.ap-osaka-1.amaaaaaassl65iqak67q6dr5zu6jqoimgf54sylota5devqglzkkoenxznxa` |
   | Default Model | `openai.gpt-4.1` |
5. `Save` → `Test Connection` を押し、200 が返ることを確認

カスタムヘッダ欄が無い / 効かない場合は **1-B (LiteLLM proxy)** へフォールバック。

### 1-B. LiteLLM proxy 経由 (フォールバック)

Langfuse がカスタムヘッダをサポートしない場合、`OpenAI-Project` ヘッダを付与する LiteLLM プロキシを挟む。

1. proxy を deploy:
   ```bash
   kubectl apply -f deploy/k8s/deployment-litellm-proxy.yaml
   kubectl rollout status -n telemetry-analyst deployment/litellm-proxy
   ```
2. proxy のヘルスを確認:
   ```bash
   POD=$(kubectl get pod -n telemetry-analyst -l app=ta-agent -o jsonpath='{.items[0].metadata.name}')
   kubectl exec -n telemetry-analyst $POD -- python -c "
     from urllib.request import urlopen
     print(urlopen('http://litellm-proxy.telemetry-analyst.svc:4000/health').read())
   "
   ```
3. Langfuse UI で LLM Connection を改めて登録:
   | フィールド | 値 |
   |---|---|
   | Provider | `OpenAI` |
   | Connection Name | `oci-enterprise-ai-via-litellm` |
   | API Key | `LITELLM_MASTER_KEY` (Secret `litellm-master/master_key`、未設定なら任意の dummy) |
   | Base URL | `http://litellm-proxy.telemetry-analyst.svc:4000` |
   | Default Model | `oci-gpt` (LiteLLM の `model_list` で定義した名前) |

---

## Step 2: Evaluator 6 種を登録

`eval/evaluators/*.yaml` の 6 種を Langfuse の **Custom Evaluator** として登録する。

### Variable mapping の方針

Langfuse v3 の Evaluator UI では **Object Field の選択肢が `Input` / `Output` / `Metadata` の 3 種のみ** で、ネストされたフィールドや Dataset Item の `expected_output` を直接参照できない。

そのため `scripts/run_experiment.py` 側で **Evaluator が必要とする全変数を `metadata` 直下にフラットに書き込んで**ある。Langfuse UI では:

- `{{input}}` / `{{output}}` → Object Field=`Input` / `Output` をそのまま選択
- それ以外 (`expected_tools`, `actual_tools`, `tool_arguments`, `selected_skills`, `mode`) → Object Field=`Metadata` を選択し、JSONPath/Field name に **その変数名そのまま** を入れる (例: `expected_tools`)

`run_experiment.py` がトレースに書き込む metadata は次の通り:

```json
{
  "kind": "agent-main-response",
  "mode": "engineer",
  "expected_tools": ["query_prometheus", "find_slow_requests"],
  "actual_tools": ["k8s_list_deployments", "query_prometheus"],
  "tool_arguments": ["{}", "{\"datasourceUid\":\"prometheus\"...}"],
  "selected_skills": ["latency-regression", "cost-aware-query", "explain-engineer"],
  "tool_calls": [{"name": "...", "arguments": "..."}]
}
```

### 共通設定 (全 6 種)

| フィールド | 値 |
|---|---|
| Type | `Custom Evaluator` |
| LLM Connection | `oci-enterprise-ai` (Step 1 で作ったもの) |
| Sampling | `100%` (15 ケース × N 周なので全件採点) |
| Trigger | `Live evaluator` (新しい trace に自動適用) |
| Trigger Target | `Observations` (root の generation ではなく individual observation) |
| Trigger Filter | `metadata.kind = "agent-main-response"` |

下表で「Object Field=Metadata」と書いた行は、JSONPath / Field name 欄に **変数名そのまま** (例: `expected_tools`) を入力する。

### 2-1. Hypothesis Grounding

- **Name**: `Hypothesis Grounding`
- **Score Type**: `Numeric` (0.0–1.0)
- **Prompt**: `eval/evaluators/hypothesis-grounding.yaml` の `prompt` 全文をコピペ
- **Variable mapping**:

| 変数 | Object | Object Field | JSONPath / Field name |
|---|---|---|---|
| `input` | Observation | Input | (空欄) |
| `output` | Observation | Output | (空欄) |

### 2-2. Tool Selection Optimality

- **Name**: `Tool Selection Optimality`
- **Score Type**: `Numeric`
- **Prompt**: `eval/evaluators/tool-selection.yaml` の `prompt` 全文
- **Variable mapping**:

| 変数 | Object | Object Field | JSONPath / Field name |
|---|---|---|---|
| `input` | Observation | Input | (空欄) |
| `output` | Observation | Output | (空欄) |
| `expected_tools` | Observation | Metadata | `expected_tools` |
| `actual_tools` | Observation | Metadata | `actual_tools` |

### 2-3. Query Correctness (PromQL/LogQL)

- **Name**: `Query Correctness (PromQL/LogQL)`
- **Score Type**: `Numeric`
- **Prompt**: `eval/evaluators/query-correctness.yaml` の `prompt` 全文
- **Variable mapping**:

| 変数 | Object | Object Field | JSONPath / Field name |
|---|---|---|---|
| `input` | Observation | Input | (空欄) |
| `actual_tools` | Observation | Metadata | `actual_tools` |
| `tool_arguments` | Observation | Metadata | `tool_arguments` |

### 2-4. Mode Adherence (beginner/engineer)

- **Name**: `Mode Adherence (beginner/engineer)`
- **Score Type**: `Numeric`
- **Prompt**: `eval/evaluators/mode-adherence.yaml` の `prompt` 全文
- **Variable mapping**:

| 変数 | Object | Object Field | JSONPath / Field name |
|---|---|---|---|
| `input` | Observation | Input | (空欄) |
| `output` | Observation | Output | (空欄) |
| `mode` | Observation | Metadata | `mode` |

### 2-5. Skill Pick Accuracy

- **Name**: `Skill Pick Accuracy`
- **Score Type**: `Numeric`
- **Prompt**: `eval/evaluators/skill-pick-accuracy.yaml` の `prompt` 全文
- **Variable mapping**:

| 変数 | Object | Object Field | JSONPath / Field name |
|---|---|---|---|
| `input` | Observation | Input | (空欄) |
| `selected_skills` | Observation | Metadata | `selected_skills` |
| `mode` | Observation | Metadata | `mode` |

### 2-6. Safety RBAC Boundary

- **Name**: `Safety: RBAC Scope Boundary`
- **Score Type**: `Categorical (pass / fail)`
- **Prompt**: `eval/evaluators/safety-rbac-boundary.yaml` の `prompt` 全文
- **Output schema**: JSON `{"value": "pass"|"fail", "rationale": string}`
- **Variable mapping**:

| 変数 | Object | Object Field | JSONPath / Field name |
|---|---|---|---|
| `input` | Observation | Input | (空欄) |
| `output` | Observation | Output | (空欄) |
| `actual_tools` | Observation | Metadata | `actual_tools` |

> **注**: Categorical Evaluator は **Step 2-pre** (下記) で先に Score Config を作って参照する必要がある. Score Config 未登録だと judge が `pass` を返しても numeric value=0 で記録され、平均スコアが常に 0 になる (Iter-03 で観測).

---

## Step 2-pre: Categorical 用 Score Config を登録 (Safety RBAC Boundary 用)

Langfuse では Categorical 型のスコアは **Score Config** で値マッピングを事前定義する必要がある。Safety RBAC Boundary 用の `pass-fail` 設定を作成する。

1. UI 左メニュー → `Settings` → `Score Configs`
2. `+ Add Score Config` をクリック
3. 以下を入力:
   | フィールド | 値 |
   |---|---|
   | Name | `pass-fail` |
   | Data Type | `Categorical` |
   | Categories | `pass` (Value: `1`), `fail` (Value: `0`) |
   | Description (任意) | `Safety boundary 等の二値判定. pass=1 / fail=0 で平均 1.0 = 全件通過` |
4. `Save`

その後、Step 2-6 で登録した `Safety: RBAC Scope Boundary` Evaluator を編集し、**Score Config** 欄で先ほど作った `pass-fail` を選択する。

> 既に Iter-NN を回してしまっている場合、その Run のスコアは value=0 のまま固定. Score Config を後から関連付けても **過去スコアは再計算されない**ため、修復後に新しい label (例: `iter-04`) で再走させる必要がある.

---

## Step 3: Dataset Run (Iter-NN) を実行

```bash
POD=$(kubectl get pod -n telemetry-analyst -l app=ta-agent -o jsonpath='{.items[0].metadata.name}')
kubectl cp src/ta "telemetry-analyst/$POD:/tmp/ta"
kubectl cp scripts "telemetry-analyst/$POD:/tmp/scripts"
kubectl cp eval "telemetry-analyst/$POD:/tmp/eval"
kubectl cp memory "telemetry-analyst/$POD:/tmp/memory"
kubectl cp skills "telemetry-analyst/$POD:/tmp/skills"

kubectl exec -n telemetry-analyst $POD -- bash -c '
  export PYTHONPATH=/tmp:/home/ta/.local/lib/python3.12/site-packages
  export TA_SKILLS_DIR=/tmp/skills TA_MEMORY_DIR=/tmp/memory
  cd /tmp && python scripts/run_experiment.py --label iter-NN \
    --description "<このイテレーションで触った箇所と仮説>" --concurrency 2
'
```

実行が終わると 15 ケース全件の trace が Langfuse に届き、Live evaluator が自動採点を開始する。採点完了まで 1〜3 分待つ。

---

## Step 4: スコア確認 / 周回比較

1. UI 左メニュー → `Datasets` → `telemetry-analyst-golden`
2. `Runs` タブで `iter-00`, `iter-01`, ..., `iter-NN` が並ぶ
3. **Run 比較ビュー**: 複数 Run にチェックを入れて `Compare` を押すと、各 item の 6 軸スコアと出力テキストを並列に見られる
4. 個別 trace を開くと、各 Evaluator の `score` と `rationale` が表示される
5. 低スコアトップ 3 件を抽出し、`eval/iterations.md` に「Iter-NN 所見」として記録 (CLAUDE.md の改善ループ手順)

### 終了条件 (CLAUDE.md より)

- 10 周以上実施
- 6 軸平均 0.8 以上
- 連続 2 周で人間レビューから新規不適切判断ゼロ

---

## トラブルシュート

| 症状 | 原因と対処 |
|---|---|
| `Test Connection` で 401 (Incorrect API key) | OCI 鍵を OpenAI 公式エンドポイントに送ろうとしている。Base URL が `OPENAI_BASE_URL` の値になっているか確認 |
| `Test Connection` で `OpenAI-Project header missing` 系のエラー | カスタムヘッダ欄が効いていない → 1-B (LiteLLM) にフォールバック |
| Run しても evaluator が走らない | Trigger の `Filter` が外れている / `metadata.kind` が一致しない。`run_experiment.py` の `metadata={"kind": "agent-main-response", ...}` と評価器側の filter を見比べる |
| `expected_tools` / `actual_tools` 等が空 | 古い `run_experiment.py` で生成した trace は metadata がフラット化されていない。`scripts/run_experiment.py` を最新版にして再実行 (`iter-NN` を新しい label で叩き直す) |
| `Missing required parameter: 'input[N].output'` (judge 側) | OCI 互換性の差分。**判事モデル**は短い prompt で済むので 1-A 直接接続でほぼ問題ないはず。Run-time agent 側の同問題は `src/ta/agent/_oci_compat.py` で解決済 |
| Categorical evaluator (RBAC) のスコアが Numeric になる | `Score Type` を `Categorical` に設定し、prompt が `{"value": ...}` を返すよう指示しているか確認 |
| Categorical evaluator のスコアが常に 0 (pass判定でも) | **Step 2-pre** の Score Config が未登録. `pass-fail` を作成し Evaluator から参照させる. 過去 Run のスコアは再計算されないため新 label で再走が必要 |
| Hypothesis Grounding が常に低スコア (judge の comment に「Prometheus 未接続」等の文言) | Grafana バックエンド (`prometheus-grafana.observability.svc.cluster.local:3000` 等) に MCP-grafana から到達できていない. ConfigMap の `GRAFANA_URL` を再確認し、`mcp-grafana` deployment を rollout restart |

---

## 将来の自動化 (TODO)

Langfuse Public API で以下が安定したらスクリプト化する:

- `POST /api/public/llm-connections` — LLM Connection 作成
- `POST /api/public/evaluators` — Evaluator 作成 (custom prompt + variable mapping)
- `POST /api/public/evaluator-runs` — 過去 trace への遡及採点

参考: `scripts/setup_langfuse.py` の末尾に該当 API へのフックを足し、本書を deprecate する。

---

## 参考

- `eval/evaluators/*.yaml` — Evaluator の正準定義 (本書の prompt と一致)
- `eval/golden_set.yaml` — 15 ケースの Dataset 定義 (`expected_tools` / `expected_outcome`)
- `eval/iterations.md` — 周回ごとの所見と修正履歴
- `scripts/setup_langfuse.py` — Dataset 作成の冪等スクリプト + 本手順を簡易出力
- `scripts/run_experiment.py` — Dataset Run を 1 周実行 (tenacity リトライ、concurrency 制御)
- `deploy/k8s/deployment-litellm-proxy.yaml` — フォールバック用 LiteLLM プロキシ
