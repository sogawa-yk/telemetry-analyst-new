# OCI ap-osaka-1 Responses API streaming flakiness ナレッジ

ta-agent (および orchestrator のような OpenAI Agents SDK + OCI Enterprise AI を使う系) で **「応答が空文字で返る」「UI がフリーズしているように見える」** という症状が再発した場合の調査・対応知識をまとめる. 2026-04-29 に v0.2.14 で恒久対策を入れた経緯のレポート.

---

## 1. 症状チェックリスト

下記のいずれかに該当すれば本ナレッジの対象:

- [ ] `/chat/stream` を叩くと SSE 自体は正常に終了 (`event: done`) するが `text` が空 (`""`)
- [ ] 同じプロンプトを連投すると 30-50% 程度で空応答が返る
- [ ] tool 呼出は走るが、その後の最終 text が出ない (途中で `done` してしまう)
- [ ] Chainlit UI で「考え中…」のまま長時間止まって見える
- [ ] ta-agent ログに `WARNING:ta.agent.core:OCI stream_final_event_missing` が頻出
- [ ] ta-agent ログに `APIStatusError 424 ... Error retrieving tool list from MCP server: 'grafana'` が出る

> 追加メトリクス目線では `ta_agent_response_latency_seconds` の `_count` が増えているのに `text` が空、という不整合が起きる. Grafana ダッシュボード `Telemetry Analyst — Agent Health` の **Upstream HTTP** row で 4xx (424) 急増を見れば早く気付ける.

---

## 2. 真因 (2 種類)

### 2.1 `response.incomplete` with `reason="stream_final_event_missing"`

OCI ap-osaka-1 (`inference.generativeai.ap-osaka-1.oci.oraclecloud.com`) の **Responses API streaming** が SSE の `response.completed` イベントを送る前にコネクションを切る. openai-python SDK 側でこれを検知し、合成イベントとして `ResponseIncompleteEvent(incomplete_details=IncompleteDetails(reason='stream_final_event_missing'))` を yield する.

特性:
- **散発的** (同一プロンプト連投で 30-50% 発生)
- **非ストリーミング (`Runner.run` / `client.responses.create(stream=False)`) では 100% 成功**
- ツール数や request body サイズに対して綺麗に相関しない (1 tool でも 7 tools でも発生)

→ **OCI 側の streaming パス特有の不安定さ**. 発生頻度は時期によって揺れるが、根本治癒はクライアント側ではできない.

### 2.2 `APIStatusError 424` "Failed Dependency"

OCI Hosted MCP (`tools: [{type: "mcp", server_url: "https://ta-mcp..."}]`) で OCI が tool list 取得時に外部 MCP サーバ (`mcp-grafana`) を呼ぶ. これが散発的に失敗する.

OCI が返すエラー:
```
{
  "error": {
    "code": "http_error",
    "message": "Error retrieving tool list from MCP server: 'grafana'. Http status code: 424 (Failed Dependency)",
    "type": "external_connector_error"
  }
}
```

ローカルから同じ MCP endpoint を叩いた直接プローブでは **200 / 44ms** で正常応答するため、`mcp-grafana` 自体は正常. OCI から外部 (Ingress LB 経由) への経路で何らかの timeout/制限に引っかかっている可能性が高い.

---

## 3. 調査手順 (再現性確保)

### 3.1 まず ta-agent のログを見る

```bash
kubectl logs -n telemetry-analyst -l app=ta-agent --since=10m \
  | grep -iE "stream_final|APIStatusError|424|retry|fallback"
```

`stream_final_event_missing` が連発していれば真因 2.1、`424 Failed Dependency` なら真因 2.2.

### 3.2 OCI を直接叩いて切り分け

ta-agent Pod の中から OCI を直接叩く. Agents SDK / OCISanitizingTransport / MCP の影響を排除する切り分け.

```bash
POD=$(kubectl get pod -n telemetry-analyst -l app=ta-agent -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n telemetry-analyst $POD -- python3 -c "
import os, time
from openai import OpenAI
c = OpenAI(api_key=os.environ['OPENAI_API_KEY'], base_url=os.environ['OPENAI_BASE_URL'],
           project=os.environ['OCI_GENAI_PROJECT'])

# 非ストリーミング
t = time.time()
r = c.responses.create(model=os.environ['OCI_GENAI_MODEL'], input='say hello')
print(f'non-stream {time.time()-t:.2f}s: {r.output_text!r}')

# ストリーミング
t = time.time()
deltas = []
for ev in c.responses.create(model=os.environ['OCI_GENAI_MODEL'], input='say hello', stream=True):
    if hasattr(ev, 'delta'):
        deltas.append(ev.delta or '')
print(f'stream {time.time()-t:.2f}s: deltas={len(deltas)}')
"
```

判定:
- 両方成功 → OCI は健全. Agents SDK 側に問題.
- streaming だけ 0 deltas → 真因 2.1 (OCI streaming 不安定)
- 両方失敗 → OCI 全体障害 (status.oci.com 確認)

### 3.3 Agents SDK 経由で連投して failure rate 計測

```python
# /tmp/diag.py を Pod に cp して実行
import asyncio
from ta.agent.core import get_agent
from agents import Runner, Agent as SDKAgent
from ta.agent.tools import k8s as k8s_tools

async def main():
    agent = get_agent()
    sdk = SDKAgent(name='t', instructions='Test', model=agent._model,
                   tools=list(k8s_tools.ALL_TOOLS))
    fails = 0
    for i in range(8):
        result = Runner.run_streamed(sdk, input='hi', max_turns=1)
        incomplete = False
        async for ev in result.stream_events():
            if ev.type == 'raw_response_event':
                etype = str(getattr(ev.data, 'type', '?'))
                if etype == 'response.incomplete':
                    incomplete = True
        fails += 1 if incomplete else 0
    print(f'failure rate: {fails}/8')

asyncio.run(main())
```

連投 8 回中 **2 回以上失敗していれば再発**. 0-1 回なら一時的な揺らぎで様子見.

### 3.4 mcp-grafana 経路の単体確認 (424 が出ている時)

```bash
SID=$(curl -sS -k -i -X POST "https://ta-mcp.devday26.sogawa-yk.com/mcp" \
  -H 'Content-Type: application/json' -H 'Accept: application/json,text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}' \
  2>&1 | grep -i "mcp-session-id" | awk '{print $2}' | tr -d '\r')

time curl -sS -k -X POST "https://ta-mcp.devday26.sogawa-yk.com/mcp" \
  -H 'Content-Type: application/json' -H 'Accept: application/json,text/event-stream' \
  -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
```

100ms 以内に tool list が JSON で返れば、mcp-grafana 自体は健全. それでも OCI が 424 を返すなら OCI → MCP の経路問題 (3.5 に進む).

### 3.5 Grafana / Prometheus / mcp-grafana の体力確認

過去事例 (2026-04-28): k6 で ec-shop に負荷をかけた際、**Grafana が CPU 200m / Mem 256Mi の貧弱設定で OOMKilled** し、それ経由で動く mcp-grafana が遅延. OCI は MCP fetch を timeout 扱いで 424 を返した.

```bash
# Grafana resources 確認 (CPU≥1000m / Mem≥1Gi が最小要件)
kubectl get deploy prometheus-grafana -n observability \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="grafana")].resources}'

# Pod 状態確認
kubectl get pod -n observability -l app.kubernetes.io/name=grafana
kubectl describe pod -n observability -l app.kubernetes.io/name=grafana | grep -E "Last State|Reason|Restart Count"

# /api/health がすぐ返るか
curl -sS -k --max-time 5 https://grafana.devday26.sogawa-yk.com/api/health
```

足りなければ `kubectl set resources` で即時 bump、永続化は `devday-k8s-template/k8s/observability-template/kube-prometheus-stack/values.yaml.tpl` の `grafana.resources` を編集して `helm upgrade prometheus -n observability ...`.

---

## 4. 実装した恒久対策 (v0.2.14)

すべて `src/ta/agent/core.py` に集約.

### 4.1 retry / fallback フロー (`Agent.run_stream`)

```
[Runner.run_streamed]
   ├─ 正常完走 → そのまま yield
   ├─ stream_final_event_missing
   │    └─ text/tool 出力済 0 件 + attempt < 5  → 内部再試行
   │       (それ以外は次工程へ — 部分出力済の重複 yield を避ける)
   ├─ APIStatusError (424 等)
   │    └─ 出力済 0 件 + attempt < 5            → 内部再試行
   └─ 最終 text が無い場合 (post-tool 切断含む)
        └─ Runner.run (non-streaming) でフォールバック取得 → delta + done で yield
```

**判定キー**:
- `text_parts`: streaming で yield 済の delta 群. 1 件でもあれば「部分出力済」扱い
- `turn_count`: tool 呼出回数. 1 以上なら「途中まで進行済」扱い
- 部分出力済の場合に retry すると client から見て同じ text を 2 度 yield してしまうため抑制

### 4.2 `Agent.run` (非ストリーミング) の retry

非ストリーミングも稀に `final_output=None` (空) で返るケースを観測したため、最大 5 回 retry.

### 4.3 Agents SDK tracing 無効化

```python
from agents import set_tracing_disabled
set_tracing_disabled(True)
```

Agents SDK は default で `https://api.openai.com/v1/traces/ingest` に向けて trace を送ろうとする. OCI Enterprise AI 用の API key では 401 になりログに大量のノイズが出るため明示的に切る. 本来のアプリ trace は `ta.telemetry.langfuse_setup.init_langfuse()` (OTel + openinference) に集約.

---

## 5. 検証結果

| 段階 | 同条件 12 連投の成功率 |
|---|---|
| 修正前 (v0.2.10) | **2/8 = 25%** (simple + tool 混在で大半が空応答) |
| stream retry のみ (v0.2.11) | 5/8 = 62% |
| stream retry + non-stream fallback (v0.2.14) | **12/12 = 100%** |

連投時の レイテンシ:
- 一発成功: 1-3s
- 1 回 retry: 3-7s
- non-stream fallback: 5-12s

p99 は伸びるが空応答は出なくなる. 「遅くても返る」 を優先する設計.

---

## 6. 既知の限界 / TODO

- **OCI 側の根治待ち**: 真因 2.1 / 2.2 はクライアント側で迂回しているだけ. OCI ap-osaka-1 が安定すれば retry が走らなくなる. Oracle status を定期確認 (https://ocistatus.oraclecloud.com/)
- **MCP tool list サイズ**: 現状 mcp-grafana は 50 tools / 58KB を露出. `allow_tools` (`grafana_mcp.py:GRAFANA_ALLOWED_TOOLS`, 10 件) は OCI 受信後にフィルタされるため、OCI への fetch サイズ自体は減っていない. 必要なら mcp-grafana 起動オプションで露出 tool を絞ると 424 確率は下がる可能性
- **`Agent.run` retry 中の skill_hit 重複計上**: 1 retry あたり `record_skill_hit` が 1 回ずつ追加で記録される (現状は許容). 厳密に重複排除したい場合は ループ外で 1 回だけ呼ぶように リファクタ
- **Grafana resources の永続化済 bump**: kube-prometheus-stack values で CPU 1000m / Mem 1Gi に上げ済. これより小さいクラスタへの再デプロイ時は再設定が必要

---

## 7. 関連 commit / ファイル

| 区分 | パス / commit |
|---|---|
| 本対策コード | `src/ta/agent/core.py` (commit `ab2d6fb`) |
| 関連診断ダッシュボード | `grafana/dashboards/agent-health.json` (Upstream HTTP row で 4xx 監視, commit `ae67ce1`) |
| 同時に行った Grafana resources 永続化 | `devday-k8s-template/k8s/observability-template/kube-prometheus-stack/values.yaml.tpl` (commit `be2fade`) |
| 起点となった負荷シナリオ runbook | `docs/runbook/load_detection.md` (k6 でこの事象を顕在化させた) |

---

## 8. 再発時の最短手順 (TL;DR)

1. **Grafana ダッシュボード `/d/ta-agent-health/` の Upstream HTTP row** を見る → 4xx か遅延が出ていれば真因確定
2. ta-agent ログを `grep -iE "stream_final|424|retry"` で確認
3. **真因 2.1 (`stream_final_event_missing`)**: 既に retry/fallback 実装済 → 一時的揺らぎなら様子見、恒常化なら OCI status 確認
4. **真因 2.2 (`424 Failed Dependency`)**: §3.5 で Grafana resources / Pod 状態確認. リソース不足なら bump
5. それでも直らないなら本ナレッジの §3.2 で OCI 直接叩いて切り分け
