"""A2A AgentExecutor 実装.

`a2a-sdk` の AgentExecutor 抽象を継承し、`Agent.run` を呼ぶ薄ブリッジ.
A2A の `context_id` (会話スレッド) を OCI Conversations API の
`conversation_id` にマッピングしてセッション継続を維持する.
"""

from __future__ import annotations

import logging

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Part, Task, TaskState, TaskStatus

from ta.agent.core import get_agent

logger = logging.getLogger(__name__)


class TelemetryAnalystExecutor(AgentExecutor):
    """A2A 経由の問合せを既存 `Agent.run` に委譲する Executor."""

    def __init__(self) -> None:
        # A2A context_id (UUID) → OCI conversation_id (conv_kix_...) のマッピング.
        # プロセスローカル辞書. レプリカ間で共有しないので将来 Redis 等への外出しを想定.
        self._conv_map: dict[str, str] = {}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # context.task_id / context_id は a2a-sdk が必ず採番する想定
        task_id = context.task_id or ""
        ctx_id = context.context_id or ""
        updater = TaskUpdater(event_queue, task_id, ctx_id)

        # 新規 Task の場合、先に Task オブジェクト自体を event_queue に enqueue する.
        # a2a-sdk 1.x の active_task は TaskStatusUpdateEvent より前に Task の
        # 生成イベントが来ることを期待するため (`Agent should enqueue Task before
        # TaskStatusUpdateEvent event` エラーの回避).
        if not context.current_task:
            initial_task = Task(
                id=task_id,
                context_id=ctx_id,
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
            )
            await event_queue.enqueue_event(initial_task)
        await updater.start_work()

        user_text = context.get_user_input()
        if not user_text:
            await updater.failed(
                message=updater.new_agent_message(parts=[Part(text="入力テキストが空です。")])
            )
            return

        agent = get_agent()

        # OCI Conversations にマッピングして継続会話を支援
        oci_conv_id = self._conv_map.get(ctx_id) if ctx_id else None
        if not oci_conv_id and ctx_id:
            try:
                oci_conv_id = await agent.create_conversation(
                    metadata={"source": "a2a", "a2a_context_id": ctx_id}
                )
                self._conv_map[ctx_id] = oci_conv_id
                logger.info("A2A: created OCI conversation %s for ctx %s", oci_conv_id, ctx_id)
            except Exception:
                logger.exception("A2A: OCI Conversations 作成失敗、conversation_id 無しで続行")
                oci_conv_id = None

        try:
            result = await agent.run(
                user_text,
                mode="engineer",
                conversation_id=oci_conv_id,
                metadata={"source": "a2a", "a2a_task_id": task_id, "a2a_context_id": ctx_id},
            )
        except Exception as e:
            logger.exception("A2A: Agent.run 失敗")
            await updater.failed(
                message=updater.new_agent_message(
                    parts=[Part(text=f"エージェント実行に失敗しました: {type(e).__name__}: {e}")]
                )
            )
            return

        # 最終応答テキストを Artifact として返す
        await updater.add_artifact(
            parts=[Part(text=result.text or "")],
            name="diagnosis",
            metadata={
                "tool_calls": [
                    {"name": tc["name"], "arguments": tc["arguments"]} for tc in result.tool_calls
                ],
                "response_id": result.response_id,
            },
        )
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # 現状 cancel 未対応. a2a-sdk 標準のエラーを上げるよう Task を failed 化
        task_id = context.task_id or ""
        ctx_id = context.context_id or ""
        updater = TaskUpdater(event_queue, task_id, ctx_id)
        await updater.failed(
            message=updater.new_agent_message(
                parts=[Part(text="cancel は未対応です (Telemetry Analyst v0.2.9).")]
            )
        )
