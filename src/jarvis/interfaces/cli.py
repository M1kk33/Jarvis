"""Small terminal interface for durable conversations."""

import argparse
import asyncio
from pathlib import Path
from uuid import uuid4

from jarvis.application.conversations import ConversationService
from jarvis.application.jobs import JobRunner
from jarvis.application.resource_manager import ResourceManager
from jarvis.application.retrieval import RetrievalService
from jarvis.domain.types import ConversationError, SendResult


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="jarvis")
    parser.add_argument(
        "--data-dir", required=True, help="Explicit private data directory"
    )
    parser.add_argument(
        "--profile", help="Pinned local model profile; omit for the offline fake"
    )
    parser.add_argument(
        "--image-profile", help="Pinned local SD 1.5 image profile; requires --profile"
    )
    parser.add_argument("--speech-profile", help="Pinned CPU speech profile (web only)")
    parser.add_argument("--piper-profile", help="Pinned CPU voice profile (web only)")
    parser.add_argument(
        "--enable-network",
        action="store_true",
        help="Offer exact-URL public page retrieval with per-request web approval",
    )
    parser.add_argument(
        "--enable-browser",
        action="store_true",
        help="Offer approved, offline JavaScript page rendering (requires --enable-network)",
    )
    parser.add_argument(
        "--vision-profile", help="Pinned screenshot vision profile (web only)"
    )
    parser.add_argument(
        "--read-root",
        action="append",
        default=[],
        help="Explicit local directory that model read tools may inspect",
    )
    parser.add_argument(
        "--write-root",
        action="append",
        default=[],
        help="Explicit local directory for approved create-only .txt files (web only)",
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
    session = commands.add_parser("session", help="Interactive local model session")
    session.add_argument("conversation_id")
    web = commands.add_parser("web", help="Start the local browser interface")
    web.add_argument("--port", type=int, default=8765)
    commands.add_parser("jobs", help="List durable jobs")
    status = commands.add_parser("status", help="Show a durable job")
    status.add_argument("job_id")
    cancel = commands.add_parser("cancel", help="Request durable job cancellation")
    cancel.add_argument("job_id")
    resume_job = commands.add_parser("resume-job", help="Explicitly resume a job")
    resume_job.add_argument("job_id")
    reconcile = commands.add_parser(
        "reconcile-file", help="Verify an uncertain create-only file result"
    )
    reconcile.add_argument("invocation_id")
    commands.add_parser("backup", help="Create a verified data backup")
    restore = commands.add_parser("restore", help="Restore into --data-dir")
    restore.add_argument("backup_dir")
    commands.add_parser("diagnostics", help="Show local status and recent failures")
    commands.add_parser("calibrate", help="Measure the pinned local model on this PC")
    root_add = commands.add_parser(
        "retrieval-root-add", help="Grant one local document directory"
    )
    root_add.add_argument("path")
    root_add.add_argument("--conversation-id")
    commands.add_parser("retrieval-root-list", help="List document grants")
    root_revoke = commands.add_parser(
        "retrieval-root-revoke", help="Revoke a document grant"
    )
    root_revoke.add_argument("root_id")
    memory_add = commands.add_parser("memory-add", help="Save an explicit memory")
    memory_add.add_argument("text")
    memory_add.add_argument("--conversation-id")
    memory_list = commands.add_parser("memory-list", help="Inspect saved memories")
    memory_list.add_argument("--conversation-id")
    memory_edit = commands.add_parser("memory-edit", help="Edit an explicit memory")
    memory_edit.add_argument("memory_id")
    memory_edit.add_argument("text")
    memory_delete = commands.add_parser("memory-delete", help="Delete a memory")
    memory_delete.add_argument("memory_id")
    document_import = commands.add_parser(
        "document-import", help="Index one granted text document"
    )
    document_import.add_argument("root_id")
    document_import.add_argument("path")
    commands.add_parser("document-list", help="Inspect indexed documents")
    document_reindex = commands.add_parser(
        "document-reindex", help="Refresh one document"
    )
    document_reindex.add_argument("document_id")
    document_delete = commands.add_parser(
        "document-delete", help="Delete one indexed document"
    )
    document_delete.add_argument("document_id")
    retrieve = commands.add_parser(
        "retrieve", help="Inspect cited results for a question"
    )
    retrieve.add_argument("conversation_id")
    retrieve.add_argument("question")
    return parser.parse_args(argv)


def dispatch(
    args: argparse.Namespace,
    service: ConversationService,
    manager: ResourceManager | None = None,
    runner: JobRunner | None = None,
    retrieval: RetrievalService | None = None,
) -> int:
    if args.command in {
        "retrieval-root-add",
        "retrieval-root-list",
        "retrieval-root-revoke",
        "memory-add",
        "memory-list",
        "memory-edit",
        "memory-delete",
        "document-import",
        "document-list",
        "document-reindex",
        "document-delete",
        "retrieve",
    }:
        assert retrieval is not None
        if args.command == "retrieval-root-add":
            print(retrieval.grant_root(Path(args.path), args.conversation_id).id)
        elif args.command == "retrieval-root-list":
            for item in retrieval.roots():
                print(
                    f"{item.id}\t{'active' if item.active else 'revoked'}"
                    f"\t{item.conversation_id or 'global'}\t{item.path}"
                )
        elif args.command == "retrieval-root-revoke":
            retrieval.revoke_root(args.root_id)
            print("revoked")
        elif args.command == "memory-add":
            print(retrieval.add_memory(args.text, args.conversation_id).id)
        elif args.command == "memory-list":
            for memory in retrieval.memories(args.conversation_id):
                print(
                    f"{memory.id}\t{memory.conversation_id or 'global'}\t{memory.text}"
                )
        elif args.command == "memory-edit":
            print(retrieval.edit_memory(args.memory_id, args.text).id)
        elif args.command == "memory-delete":
            retrieval.delete_memory(args.memory_id)
            print("deleted")
        elif args.command == "document-import":
            print(retrieval.import_document(args.root_id, Path(args.path)).id)
        elif args.command == "document-list":
            for document in retrieval.documents():
                print(f"{document.id}\t{document.status}\t{document.path}")
        elif args.command == "document-reindex":
            print(retrieval.reindex_document(args.document_id).id)
        elif args.command == "document-delete":
            retrieval.delete_document(args.document_id)
            print("deleted")
        else:
            for hit in asyncio.run(
                retrieval.search(args.question, args.conversation_id)
            ):
                print(
                    f"{hit.kind}:{hit.source_id}#{hit.location}\t{hit.path or 'user memory'}\n{hit.text}"
                )
    elif args.command == "create":
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
            for invocation in service.get_invocations(turn.id):
                print(
                    f"  tool {invocation.ordinal}: {invocation.name} "
                    f"[{invocation.status.value}]"
                )
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

        async def send_once():
            nonlocal emitted
            try:
                if runner is not None:
                    job, duplicate = await runner.submit(
                        args.conversation_id, args.text, submission_id
                    )
                    print(f"job_id={job.id}", flush=True)
                    if not duplicate and job.status.value == "queued":
                        job = await runner.run_job(job.id, output_delta)
                    if emitted:
                        print()
                        emitted = False
                    print(f"job_status={job.status.value}")
                    turn = await service.store.run(
                        lambda: service.store.get_turn(job.turn_id)
                    )
                    answer = await service.store.run(
                        lambda: service.store.get_answer(job.turn_id)
                    )
                    return SendResult(turn, answer, duplicate)
                return await service.send_async(
                    args.conversation_id,
                    args.text,
                    submission_id,
                    on_delta=output_delta,
                )
            finally:
                if manager is not None:
                    await manager.close()

        try:
            result = asyncio.run(send_once())
        finally:
            if emitted:
                print()
        if result.duplicate:
            print("Existing submission; no new provider call.")
        print(f"turn_status={result.turn.status.value}")
        if result.duplicate and result.answer is not None:
            print(result.answer.text)
    elif args.command == "session":
        if manager is None:
            raise ValueError("Interactive session requires --profile")

        async def interactive() -> None:
            background: dict[str, asyncio.Task] = {}

            async def visible_job(job_id: str):
                assert runner is not None
                emitted = False

                async def output(delta: str) -> None:
                    nonlocal emitted
                    emitted = True
                    print(delta, end="", flush=True)

                try:
                    finished = await runner.run_job(job_id, output)
                    if emitted:
                        print()
                    print(f"job {job_id}: {finished.status.value}")
                    return finished
                except Exception as exc:
                    if emitted:
                        print()
                    print(f"job {job_id}: {exc}")
                    raise

            try:
                while True:
                    line = await asyncio.to_thread(input, "jarvis> ")
                    if line == "/exit":
                        if background:
                            print(
                                "Waiting for active jobs; use /cancel JOB_ID first if needed."
                            )
                            await asyncio.gather(
                                *background.values(), return_exceptions=True
                            )
                        return
                    if line == "/unload":
                        if any(not task.done() for task in background.values()):
                            print("An active job is using the model; cancel it first.")
                            continue
                        await manager.unload()
                        print("Local model unloaded.")
                        continue
                    if not line.strip():
                        continue
                    if runner is not None and line == "/jobs":
                        for job in await runner.list_jobs(args.conversation_id):
                            print(f"{job.id}\t{job.status.value}\t{job.progress}")
                        continue
                    if runner is not None and line.startswith("/status "):
                        job = await runner.status(line.split(maxsplit=1)[1])
                        print(f"{job.id}\t{job.status.value}\t{job.progress}")
                        continue
                    if runner is not None and line.startswith("/cancel "):
                        job = await runner.cancel(line.split(maxsplit=1)[1])
                        print(f"{job.id}\t{job.status.value}")
                        continue
                    if runner is not None and line.startswith("/resume-job "):
                        job_id = line.split(maxsplit=1)[1]
                        background[job_id] = asyncio.create_task(visible_job(job_id))
                        print(f"job_id={job_id}")
                        continue
                    if runner is not None:
                        try:
                            job, duplicate = await runner.submit(
                                args.conversation_id, line
                            )
                        except (ConversationError, ValueError) as exc:
                            print(f"error: {exc}")
                            continue
                        print(f"job_id={job.id}")
                        if not duplicate:
                            background[job.id] = asyncio.create_task(
                                visible_job(job.id)
                            )
                        else:
                            print(
                                f"job_status={job.status.value} (existing submission)"
                            )
                        for completed_id, task in list(background.items()):
                            if task.done():
                                if not task.cancelled():
                                    task.exception()
                                background.pop(completed_id)
                        continue
                    emitted = False

                    async def stream_output(delta: str) -> None:
                        nonlocal emitted
                        emitted = True
                        print(delta, end="", flush=True)

                    try:
                        result = await service.send_async(
                            args.conversation_id, line, on_delta=stream_output
                        )
                        if emitted:
                            print()
                        print(f"turn_status={result.turn.status.value}")
                    except Exception:
                        if emitted:
                            print()
                        raise
            except EOFError:
                return
            finally:
                await manager.close()

        asyncio.run(interactive())
    elif args.command in {"jobs", "status", "cancel", "resume-job"}:
        if runner is None:
            raise ValueError("Job runner is unavailable")

        async def job_command() -> None:
            try:
                if args.command == "jobs":
                    for job in await runner.list_jobs():
                        print(f"{job.id}\t{job.status.value}\t{job.progress}")
                elif args.command == "status":
                    job = await runner.status(args.job_id)
                    print(f"{job.id}\t{job.status.value}\t{job.progress}")
                elif args.command == "cancel":
                    job = await runner.cancel(args.job_id)
                    print(f"{job.id}\t{job.status.value}")
                else:
                    job = await runner.run_job(args.job_id)
                    print(f"{job.id}\t{job.status.value}")
            finally:
                if manager is not None:
                    await manager.close()

        asyncio.run(job_command())
    return 0
