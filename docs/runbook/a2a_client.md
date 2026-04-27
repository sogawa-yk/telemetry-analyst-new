# A2A サーバ運用と他エージェントから呼ぶ手順

`telemetry-analyst` v0.2.9 で A2A (Agent-to-Agent) プロトコル v1.0 のサーバ機能を有効化した. 本書は **クラスタ内の他エージェント / Pod から本サーバを A2A で呼ぶ** 際の手順をまとめる.

## 概要

- **エンドポイント**: `http://ta-agent.telemetry-analyst.svc:8080/a2a/`
  - AgentCard: `GET /a2a/.well-known/agent-card.json`
  - JSON-RPC: `POST /a2a/`
- **公開範囲**: ClusterIP のみ. Ingress なし (外部公開しない)
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
    "url": "http://ta-agent.telemetry-analyst.svc:8080/a2a",
    "protocolBinding": "JSONRPC",
    "protocolVersion": "1.0"
  }]
}
```

## 3. Python (a2a-sdk) クライアントから呼ぶ例

```python
import asyncio, os
import httpx
from a2a.client import A2AClient
from a2a.types import Message, Part, Role

async def main():
    base = "http://ta-agent.telemetry-analyst.svc:8080/a2a"
    token = os.environ["TA_A2A_TOKEN"]
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(headers=headers) as http:
        client = A2AClient(httpx_client=http, agent_card_path=f"{base}/.well-known/agent-card.json")
        await client.resolve_agent_card()

        msg = Message(
            role=Role.USER,
            parts=[Part(text="ec-shop の checkout の応答が遅い気がする。原因を調べて。")],
            message_id="msg-1",
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

```bash
TOKEN=$(kubectl get secret -n telemetry-analyst ta-agent-a2a-token -o jsonpath='{.data.token}' | base64 -d)

kubectl run a2a-probe --rm -it --restart=Never \
  --image=curlimages/curl:8.11.1 -n telemetry-analyst -- \
  -sS -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  http://ta-agent.telemetry-analyst.svc:8080/a2a/ \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "message/send",
    "params": {
      "message": {
        "role": "user",
        "parts": [{"text": "ec-shop の Pod 一覧を見せて"}],
        "messageId": "test-msg-1"
      }
    }
  }'
```

レスポンスの `result.artifacts[0].parts[0].text` に最終応答が入る.

## 5. 会話継続 (Multi-turn)

A2A の `context_id` を再利用すれば、複数の `message/send` 間で OCI Conversations API のスレッドが共有される. クライアント側で前回 Task の `context_id` を保持して、次のリクエストの `params.message.contextId` に同じ値を入れる.

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
| `Method not found` | A2A v1.0 のメソッド名 (`message/send`, `tasks/get`, `tasks/cancel` 等) を確認. v0.3 互換は無効化済 |

## 8. 観測

- Langfuse: A2A 経由の `Agent.run` 呼出も同 trace pipeline に流れる. trace metadata の `source: a2a` で絞り込める
- OTel: `ta_agent_react_turns` / `ta_skill_hit_total` 等のメトリクスは A2A 経由でも記録される
- ta-agent Pod ログ: `A2A: created OCI conversation ...` が初回会話で出力される
