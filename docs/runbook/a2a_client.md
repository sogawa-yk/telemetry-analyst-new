# A2A サーバ運用と他エージェントから呼ぶ手順

`telemetry-analyst` v0.2.9 で A2A (Agent-to-Agent) プロトコル v1.0 のサーバ機能を有効化した. 本書は **クラスタ内の他エージェント / Pod から本サーバを A2A で呼ぶ** 際の手順をまとめる.

## 概要

- **エンドポイント**:
  - クラスタ内 (推奨): `http://ta-agent.telemetry-analyst.svc:8080/a2a/`
  - 外部公開 (Ingress 経由): `https://ta.devday26.sogawa-yk.com/a2a/` (UI と同一ホスト. パスで分離)
  - AgentCard: `GET /a2a/.well-known/agent-card.json`
  - JSON-RPC: `POST /a2a/`
- **公開範囲**: ClusterIP + Ingress 同居. AgentCard の `supportedInterfaces[].url` は `A2A_PUBLIC_URL` 環境変数 (ConfigMap `ta-agent-config`) で制御し、現状は外部 URL を採用
- **認証**: Bearer Token (Secret `ta-agent-a2a-token` の `token` フィールド)
- **Skill**: 単一 `diagnose-ec-shop` ("ec-shop NS の障害診断")
- **入出力**: text のみ. streaming は v0.2.9 では未対応 (Task → Artifact 完了通知)

## 1. Token 発行 (運用初回のみ)

```bash
# 32 バイト random をベースに Bearer Token を生成して Secret 化
TOKEN=$(openssl rand -hex 32)
kubectl create secret generic ta-agent-a2a-token \
  --from-literal=token="$TOKEN" \
  -n telemetry-analyst

# Token を呼び出し側に共有 (kubectl get secret -o jsonpath で再取得可能)
echo "A2A token: $TOKEN"
```

> Secret 未作成のままだと `A2A_AUTH_TOKEN` が undefined になり、`/a2a/*` は **503 (A2A is not enabled)** を返す.

ta-agent Pod を rollout して env 反映:

```bash
kubectl rollout restart -n telemetry-analyst deployment/ta-agent
kubectl rollout status -n telemetry-analyst deployment/ta-agent
```

## 2. AgentCard 取得 (能力発見)

```bash
TOKEN=$(kubectl get secret -n telemetry-analyst ta-agent-a2a-token -o jsonpath='{.data.token}' | base64 -d)

# クラスタ内 Pod から
kubectl run a2a-probe --rm -it --restart=Never \
  --image=curlimages/curl:8.11.1 -n telemetry-analyst -- \
  -sS -H "Authorization: Bearer $TOKEN" \
  http://ta-agent.telemetry-analyst.svc:8080/a2a/.well-known/agent-card.json | jq
```

期待されるレスポンス:

```json
{
  "name": "telemetry-analyst",
  "description": "ec-shop NS の障害を Prometheus / Loki / Tempo / K8s 読取で診断する単一 ReAct エージェント.",
  "version": "0.2.9",
  "capabilities": {"streaming": false, "pushNotifications": false},
  "skills": [{
    "id": "diagnose-ec-shop",
    "name": "ec-shop 障害診断",
    "tags": ["kubernetes", "observability", "incident-response", "ec-shop"],
    "examples": [...]
  }],
  "supportedInterfaces": [{
    "url": "https://ta.devday26.sogawa-yk.com/a2a",
    "protocolBinding": "JSONRPC",
    "protocolVersion": "1.0"
  }]
}
```

## 3. Python (a2a-sdk) クライアントから呼ぶ例

`a2a-sdk` 1.x の `A2AClient` を使うと v1.0 ヘッダ / メソッド名は SDK が自動付与する.
httpx 共通ヘッダで Bearer Token を入れるだけで良い.

```python
import asyncio, os, uuid
import httpx
from a2a.client import A2AClient
from a2a.types import Message, Part, Role

async def main():
    base = "http://ta-agent.telemetry-analyst.svc:8080/a2a"
    token = os.environ["TA_A2A_TOKEN"]
    headers = {
        "Authorization": f"Bearer {token}",
        # a2a-sdk が自動付与する想定だが、互換のため明示しても良い:
        # "A2A-Version": "1.0",
    }

    async with httpx.AsyncClient(headers=headers) as http:
        client = A2AClient(
            httpx_client=http,
            agent_card_path=f"{base}/.well-known/agent-card.json",
        )
        await client.resolve_agent_card()

        msg = Message(
            role=Role.ROLE_USER,  # v1.0 の proto enum
            parts=[Part(text="ec-shop の checkout の応答が遅い気がする。原因を調べて。")],
            message_id=str(uuid.uuid4()),
        )
        result = await client.send_message(message=msg)
        # result は Task. artifacts に最終応答テキストが入る
        for artifact in result.artifacts:
            for part in artifact.parts:
                if part.text:
                    print(part.text)

asyncio.run(main())
```

## 4. curl での JSON-RPC 直接呼出 (debug 用)

> **A2A v1.0 の重要ポイント**:
> - method 名は **`SendMessage`** (キャメルケース、gRPC service method 名と一致). v0.3 の `message/send` ではない
> - **`A2A-Version: 1.0` ヘッダ必須**. 未指定だと v0.3 default として扱われ `version 0.3 is not supported` エラー
> - `role` は protobuf enum 文字列 **`ROLE_USER`** / `ROLE_AGENT` (proto JSON 表現). 小文字 `user` でも受理されるケースがあるが、ここでは v1.0 標準の大文字を使う
> - フィールド名は protobuf 由来で **snake_case** (`message_id` / `context_id`). `messageId` も proto JSON 規則上は受理される

```bash
TOKEN=$(kubectl get secret -n telemetry-analyst ta-agent-a2a-token -o jsonpath='{.data.token}' | base64 -d)
MSG_ID=$(uuidgen)

kubectl run a2a-probe --restart=Never --image=curlimages/curl:8.11.1 -n telemetry-analyst -- \
  -sS -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  http://ta-agent.telemetry-analyst.svc:8080/a2a/ \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"id\": 1,
    \"method\": \"SendMessage\",
    \"params\": {
      \"message\": {
        \"role\": \"ROLE_USER\",
        \"parts\": [{\"text\": \"ec-shop の Pod 一覧を見せて\"}],
        \"message_id\": \"$MSG_ID\"
      }
    }
  }"
kubectl logs -n telemetry-analyst a2a-probe
kubectl delete pod -n telemetry-analyst a2a-probe
```

レスポンス例:
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "task": {
      "id": "...",
      "contextId": "...",
      "status": {"state": "TASK_STATE_COMPLETED", "timestamp": "..."},
      "artifacts": [{
        "artifactId": "...",
        "name": "diagnosis",
        "parts": [{"text": "ec-shop namespace の Pod 一覧は..."}],
        "metadata": {"tool_calls": [...], "response_id": "resp_kix_..."}
      }]
    }
  }
}
```

最終応答は `result.task.artifacts[0].parts[0].text` に入る.

### v0.3 互換 (任意)

過去 SDK 互換が必要なクライアントには、`build_a2a_starlette_app()` 内の
`create_jsonrpc_routes(..., enable_v0_3_compat=True)` を有効化すると
`message/send` (lowercase) も同 endpoint で受け付けられる. 現状は
v1.0 のみ有効.

## 5. 会話継続 (Multi-turn)

A2A v1.0 の `SendMessage` で同一 `context_id` を再利用すれば、複数の呼出間で OCI Conversations API のスレッドが共有される. クライアント側で前回 Task の `contextId` を保持して、次のリクエストの `params.message.context_id` に同じ値を入れる.

```json
{"jsonrpc":"2.0","id":2,"method":"SendMessage","params":{"message":{
  "role":"ROLE_USER",
  "context_id":"前回 Task の contextId",
  "parts":[{"text":"続けて、最初の Pod の役割を説明して"}],
  "message_id":"<new uuid>"
}}}
```

サーバ側は `TelemetryAnalystExecutor._conv_map` で `a2a_context_id → oci_conversation_id` をプロセスローカルに保持する. レプリカ間で共有されないので、ステートフルな会話は単一レプリカ前提.

## 6. Token ローテーション

```bash
NEW_TOKEN=$(openssl rand -hex 32)
kubectl create secret generic ta-agent-a2a-token \
  --from-literal=token="$NEW_TOKEN" \
  -n telemetry-analyst \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl rollout restart -n telemetry-analyst deployment/ta-agent
```

旧 Token は反映後すぐ無効になる. 呼び出し側 (peer エージェント) にも `NEW_TOKEN` を共有する.

## 7. トラブルシュート

| 症状 | 原因 / 対処 |
|---|---|
| `503 A2A is not enabled` | `A2A_AUTH_TOKEN` env 未設定. Secret `ta-agent-a2a-token` が存在するか / Deployment が rollout 済みか確認 |
| `401 unauthorized (missing or malformed Authorization header)` | `Authorization: Bearer <token>` ヘッダ不在 |
| `401 unauthorized (invalid bearer token)` | Token 不一致. `kubectl get secret ... -o jsonpath` で正しい値を取り直す |
| `agent-card.json` の `supportedInterfaces[].url` が想定と違う | `A2A_PUBLIC_URL` 環境変数で上書き可. ConfigMap `ta-agent-config` か Deployment env で設定 |
| `Method not found` (-32601) | v0.3 形式 (`message/send`) で送っている. v1.0 では **`SendMessage`** (CamelCase, gRPC service method 由来). 受け付けるメソッド: `SendMessage` / `SendStreamingMessage` / `GetTask` / `ListTasks` / `CancelTask` / `SubscribeToTask` / `GetExtendedAgentCard` / `*PushNotificationConfig` |
| `version 0.3 is not supported` (-32009) | `A2A-Version: 1.0` ヘッダが無い. v0.3 がデフォルトなので明示必須. クライアントは httpx の default_headers に入れるか、各リクエストで指定 |

## 8. 観測

- Langfuse: A2A 経由の `Agent.run` 呼出も同 trace pipeline に流れる. trace metadata の `source: a2a` で絞り込める
- OTel: `ta_agent_react_turns` / `ta_skill_hit_total` 等のメトリクスは A2A 経由でも記録される
- ta-agent Pod ログ: `A2A: created OCI conversation ...` が初回会話で出力される
