"""Small terminal interface for durable conversations."""

import argparse
import asyncio
from uuid import uuid4

from jarvis.application.conversations import ConversationService


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="jarvis")
    parser.add_argument(
        "--data-dir", required=True, help="Explicit private data directory"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="Create a conversation")
    create.add_argument("title", nargs="?", default="New conversation")

    commands.add_parser("list", help="List stored conversations")

    resume = commands.add_parser("resume", help="Display a stored conversation")
    resume.add_argument("conversation_id")

    send = commands.add_parser("send", help="Send one message to a conversation")
    send.add_argument("conversation_id")
    send.add_argument("text")
    send.add_argument(
        "--submission-id", help="Reuse this ID when retrying the same submission"
    )
    return parser.parse_args(argv)


def dispatch(args: argparse.Namespace, service: ConversationService) -> int:
    if args.command == "create":
        print(service.create(args.title).id)
    elif args.command == "list":
        for conversation in service.list_conversations():
            print(f"{conversation.id}\t{conversation.title}")
    elif args.command == "resume":
        conversation, turns, messages = service.resume(args.conversation_id)
        print(f"{conversation.title} [{conversation.id}]")
        by_turn: dict[str, list[str]] = {}
        for message in messages:
            by_turn.setdefault(message.turn_id, []).append(
                f"  {message.role}: {message.text}"
            )
        for turn in turns:
            print(
                f"turn {turn.position} [{turn.status.value}] submission_id={turn.submission_id}"
            )
            for line in by_turn.get(turn.id, []):
                print(line)
            draft = service.get_draft(turn.id)
            if draft is not None:
                print(f"  draft [{draft.status}]: {draft.text}")
    elif args.command == "send":
        submission_id = args.submission_id or str(uuid4())
        print(f"submission_id={submission_id}", flush=True)
        emitted = False

        async def output_delta(delta: str) -> None:
            nonlocal emitted
            emitted = True
            print(delta, end="", flush=True)

        try:
            result = asyncio.run(
                service.send_async(
                    args.conversation_id,
                    args.text,
                    submission_id,
                    on_delta=output_delta,
                )
            )
        finally:
            if emitted:
                print()
        if result.duplicate:
            print("Existing submission; no new provider call.")
        print(f"turn_status={result.turn.status.value}")
        if result.duplicate and result.answer is not None:
            print(result.answer.text)
    return 0
