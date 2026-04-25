"""P1 用 CLI: ローカルから単発の質問を実行する."""

from __future__ import annotations

import argparse
import asyncio
import sys

from ta.agent.core import get_agent
from ta.config import get_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Telemetry Analyst CLI")
    parser.add_argument("question", type=str, help="自然言語の質問")
    parser.add_argument(
        "--mode",
        choices=["beginner", "engineer"],
        default=None,
        help="説明モード (既定は .env の TA_DEFAULT_MODE)",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="ストリームせず一括表示",
    )
    parser.add_argument(
        "--new-conversation",
        action="store_true",
        help="OCI Conversations API に新しいスレッドを作って紐付ける",
    )
    args = parser.parse_args(argv)

    mode = args.mode or get_settings().default_mode
    agent = get_agent()

    return asyncio.run(_main_async(agent, args, mode))


async def _main_async(agent, args, mode) -> int:  # type: ignore[no-untyped-def]
    conv_id: str | None = None
    if args.new_conversation:
        conv_id = await agent.create_conversation(metadata={"source": "cli"})
        print(f"(new conversation: {conv_id})", file=sys.stderr)

    if args.no_stream:
        result = await agent.run(args.question, mode=mode, conversation_id=conv_id)
        print(result.text)
        if result.tool_calls:
            print("\n--- tool calls ---", file=sys.stderr)
            for tc in result.tool_calls:
                print(f"  [{tc['name']}] {tc['arguments']}", file=sys.stderr)
        return 0

    async for event in agent.run_stream(args.question, mode=mode, conversation_id=conv_id):
        t = event["type"]
        if t == "delta":
            sys.stdout.write(event["text"])
            sys.stdout.flush()
        elif t == "tool_call":
            sys.stderr.write(f"\n[tool_call] {event['name']}({event['arguments']})\n")
            sys.stderr.flush()
        elif t == "tool_result":
            preview = event["result"][:200].replace("\n", " ")
            sys.stderr.write(f"[tool_result] {event['name']} -> {preview}...\n")
            sys.stderr.flush()
        elif t == "done":
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
