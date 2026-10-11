"""SQLite conversation store and per-data-directory process lock."""

import asyncio
import hashlib
import json
import msvcrt
import sqlite3
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from functools import wraps
from importlib import resources
from pathlib import Path
from typing import Self, TypeVar
from uuid import uuid4

from jarvis.domain.jobs import (
    JOB_EXECUTION_SECONDS,
    TERMINAL_JOBS,
    Job,
    JobStatus,
    JobStep,
    same_profile_identity,
)
from jarvis.domain.preferences import Preferences
from jarvis.domain.retrieval import Document, Memory, RetrievalHit, RetrievalRoot
from jarvis.domain.tools import (
    Approval,
    ContextRecord,
    InvocationStatus,
    ToolContextRecord,
    ToolInvocation,
)
from jarvis.domain.types import (
    ActiveTurnError,
    Conversation,
    ConversationNotFound,
    Draft,
    Message,
    StaleTurnError,
    StorageFailure,
    SubmissionConflict,
    TextPart,
    Turn,
    TurnStatus,
    VisionInput,
)
from jarvis.infrastructure.artifacts import verify_artifact

T = TypeVar("T")


def _on_owner[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    @wraps(fn)
    def dispatched(*args: P.args, **kwargs: P.kwargs) -> R:
        store = args[0]
        if not isinstance(store, SqliteStore):
            raise TypeError("SQLite owner dispatch requires a store")
        if threading.get_ident() == store._owner_ident:
            return fn(*args, **kwargs)
        return store._worker.submit(fn, *args, **kwargs).result()

    return dispatched


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _content(text: str) -> str:
    return json.dumps(
        [{"type": "text", "text": text}], ensure_ascii=False, separators=(",", ":")
    )


class _InstanceLock:
    """Prevents another app instance from misclassifying a live turn as crashed."""

    def __init__(self, data_dir: Path) -> None:
        run_dir = data_dir / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        self._file = (run_dir / "instance.lock").open("a+b")
        self._file.seek(0, 2)
        if self._file.tell() == 0:
            self._file.write(b"\0")
            self._file.flush()
        self._file.seek(0)
        try:
            msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            self._file.close()
            raise RuntimeError(f"Another Jarvis instance is using {data_dir}") from exc

    def close(self) -> None:
        if not self._file.closed:
            try:
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                self._file.close()


class SqliteStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = _InstanceLock(self.data_dir)
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-db")
        try:
            self._worker.submit(self._initialize).result()
        except BaseException:
            self._worker.submit(self._close_connection).result()
            self._worker.shutdown(wait=True)
            self._lock.close()
            raise

    def _initialize(self) -> None:
        self._owner_ident = threading.get_ident()
        self._conn = sqlite3.connect(
            self.data_dir / "jarvis.db", timeout=5, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        journal_mode = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if journal_mode.lower() != "wal":
            raise RuntimeError("SQLite WAL mode is unavailable for this data directory")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._migrate()
        self.session_id = str(uuid4())
        with self._transaction():
            self._conn.execute(
                "UPDATE tool_approvals SET status='invalidated' WHERE status='issued'"
            )
            self._conn.execute(
                "UPDATE tool_invocations SET status='outcome_unknown' WHERE status='intent'"
            )
            self._conn.execute(
                "UPDATE image_steps SET status='outcome_unknown', updated_at=? "
                "WHERE status='running'",
                (_now(),),
            )
            self._conn.execute(
                "UPDATE tool_invocations SET status='cancelled' "
                "WHERE status IN ('proposed', 'waiting_approval')"
            )
            self._conn.execute(
                "UPDATE image_steps SET status='cancelled', updated_at=? "
                "WHERE status='queued'",
                (_now(),),
            )
            self._conn.execute(
                "UPDATE turns SET status='interrupted', error_code='interrupted' WHERE status='pending'"
            )
            self._conn.execute(
                "UPDATE turn_drafts SET status='interrupted' WHERE status='draft'"
            )
            self._conn.execute(
                "UPDATE jobs SET status='interrupted', progress='restart_interrupted', "
                "updated_at=? WHERE status='running'",
                (_now(),),
            )
            self._conn.execute(
                "UPDATE job_steps SET status='interrupted', updated_at=? "
                "WHERE status='running' AND invocation_id IS NULL",
                (_now(),),
            )
            self._conn.execute(
                "UPDATE job_steps SET status='outcome_unknown', updated_at=? "
                "WHERE invocation_id IN (SELECT id FROM tool_invocations "
                "WHERE status='outcome_unknown')",
                (_now(),),
            )
            self._conn.execute(
                "UPDATE job_steps SET status='cancelled', updated_at=? "
                "WHERE invocation_id IN (SELECT id FROM tool_invocations "
                "WHERE status='cancelled') AND status IN ('queued', 'waiting_approval')",
                (_now(),),
            )
            self._conn.execute(
                "UPDATE jobs SET status='cancelled', progress='cancelled_after_restart', "
                "updated_at=? WHERE status='cancel_requested' AND cancel_requested=1 "
                "AND profile_name='fake'",
                (_now(),),
            )
        for artifact in self._conn.execute(
            "SELECT id, relative_path, size_bytes, sha256 FROM artifacts "
            "WHERE status='available'"
        ).fetchall():
            try:
                verify_artifact(
                    self.data_dir,
                    artifact["relative_path"],
                    artifact["size_bytes"],
                    artifact["sha256"],
                )
            except (OSError, ValueError):
                with self._transaction():
                    self._conn.execute(
                        "UPDATE artifacts SET status='unavailable' "
                        "WHERE id=? AND status='available'",
                        (artifact["id"],),
                    )

    def _close_connection(self) -> None:
        if hasattr(self, "_conn"):
            self._conn.close()

    def close(self) -> None:
        try:
            self._worker.submit(self._close_connection).result()
            self._worker.shutdown(wait=True)
        finally:
            self._lock.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    def _migrate(self) -> None:
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        for version, filename in (
            (1, "001_initial.sql"),
            (2, "002_drafts.sql"),
            (3, "003_tools.sql"),
            (4, "004_jobs.sql"),
            (5, "005_images.sql"),
            (6, "006_vision.sql"),
            (7, "007_retrieval.sql"),
            (8, "008_tool_acknowledgements.sql"),
            (9, "009_conversation_pins.sql"),
            (10, "010_preferences.sql"),
        ):
            script = (
                resources.files("jarvis")
                .joinpath("infrastructure", "migrations", filename)
                .read_text(encoding="utf-8")
            )
            checksum = hashlib.sha256(script.encode("utf-8")).hexdigest()
            row = self._conn.execute(
                "SELECT checksum FROM schema_migrations WHERE version=?", (version,)
            ).fetchone()
            if row is not None:
                if row["checksum"] != checksum:
                    raise RuntimeError(
                        f"Migration {version:03d} changed after it was applied"
                    )
                continue
            try:
                self._conn.executescript("BEGIN IMMEDIATE;\n" + script)
                self._conn.execute(
                    "INSERT INTO schema_migrations (version, checksum, applied_at) VALUES (?, ?, ?)",
                    (version, checksum, _now()),
                )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    @_on_owner
    def get_preferences(self) -> Preferences:
        row = self._conn.execute(
            "SELECT payload FROM app_preferences WHERE singleton=1"
        ).fetchone()
        return Preferences(**json.loads(row["payload"])) if row else Preferences()

    @_on_owner
    def set_preferences(self, preferences: Preferences) -> Preferences:
        payload = json.dumps(asdict(preferences), separators=(",", ":"))
        with self._transaction():
            self._conn.execute(
                "INSERT INTO app_preferences(singleton,payload) VALUES(1,?) "
                "ON CONFLICT(singleton) DO UPDATE SET payload=excluded.payload",
                (payload,),
            )
        return self.get_preferences()

    async def run(self, operation: Callable[[], T]) -> T:
        """Serialize database work; cancellation does not cancel its commit."""
        future = self._worker.submit(operation)
        wrapped = asyncio.wrap_future(future)
        # A cancelled await leaves the owner operation running; consume its exception.
        wrapped.add_done_callback(
            lambda settled: settled.exception() if not settled.cancelled() else None
        )
        try:
            return await asyncio.shield(wrapped)
        except sqlite3.Error as exc:
            raise StorageFailure(
                "SQLite operation failed; inspect the saved turn before retrying"
            ) from exc

    def run_sync(self, operation: Callable[[], T]) -> T:
        if threading.get_ident() == self._owner_ident:
            return operation()
        return self._worker.submit(operation).result()

    @_on_owner
    def get_turn_by_submission(
        self, conversation_id: str, submission_id: str
    ) -> Turn | None:
        row = self._conn.execute(
            "SELECT * FROM turns WHERE conversation_id=? AND submission_id=?",
            (conversation_id, submission_id),
        ).fetchone()
        return self._turn(row) if row is not None else None

    @_on_owner
    def get_turn(self, turn_id: str) -> Turn:
        row = self._conn.execute(
            "SELECT * FROM turns WHERE id=?", (turn_id,)
        ).fetchone()
        if row is None:
            raise StaleTurnError("Turn does not exist")
        return self._turn(row)

    @_on_owner
    def checkpoint_draft(
        self, turn_id: str, text: str, expected_job_attempt: int | None = None
    ) -> None:
        with self._transaction():
            row = self._conn.execute(
                "SELECT status FROM turns WHERE id=?", (turn_id,)
            ).fetchone()
            if row is None or row["status"] != "pending":
                raise StaleTurnError("Turn is no longer pending")
            job = self._conn.execute(
                "SELECT attempt, status FROM jobs WHERE turn_id=?", (turn_id,)
            ).fetchone()
            if job is not None and (
                job["attempt"] != expected_job_attempt or job["status"] != "running"
            ):
                raise StaleTurnError("Stale job draft attempt")
            self._conn.execute(
                "INSERT INTO turn_drafts (turn_id, text, status, checkpointed_at) "
                "VALUES (?, ?, 'draft', ?) ON CONFLICT(turn_id) DO UPDATE SET "
                "text=excluded.text, status='draft', checkpointed_at=excluded.checkpointed_at",
                (turn_id, text, _now()),
            )

    @_on_owner
    def get_draft(self, turn_id: str) -> Draft | None:
        row = self._conn.execute(
            "SELECT turn_id, text, status FROM turn_drafts WHERE turn_id=?", (turn_id,)
        ).fetchone()
        return Draft(row["turn_id"], row["text"], row["status"]) if row else None

    @_on_owner
    def interrupt_turn(
        self,
        turn_id: str,
        code: str = "cancelled",
        expected_job_attempt: int | None = None,
    ) -> Turn:
        with self._transaction():
            job = self._conn.execute(
                "SELECT attempt FROM jobs WHERE turn_id=?", (turn_id,)
            ).fetchone()
            if job is not None and job["attempt"] != expected_job_attempt:
                raise StaleTurnError("Stale job interruption attempt")
            self._conn.execute(
                "UPDATE turns SET status='interrupted', error_code=? WHERE id=? AND status='pending'",
                (code, turn_id),
            )
            self._conn.execute(
                "UPDATE turn_drafts SET status='interrupted' WHERE turn_id=?",
                (turn_id,),
            )
            self._conn.execute(
                "UPDATE tool_invocations SET status='cancelled' WHERE turn_id=? "
                "AND status IN ('proposed', 'waiting_approval')",
                (turn_id,),
            )
            self._conn.execute(
                "UPDATE tool_approvals SET status='invalidated' WHERE turn_id=? "
                "AND status='issued'",
                (turn_id,),
            )
            self._conn.execute(
                "UPDATE jobs SET status='interrupted', progress='interrupted', "
                "error_code=?, updated_at=? WHERE turn_id=? AND status='running'",
                (code, _now(), turn_id),
            )
            self._conn.execute(
                "UPDATE job_steps SET status='interrupted', updated_at=? "
                "WHERE job_id=(SELECT id FROM jobs WHERE turn_id=?) "
                "AND kind='chat' AND status='running'",
                (_now(), turn_id),
            )
        return self.get_turn(turn_id)

    @staticmethod
    def _conversation(row: sqlite3.Row) -> Conversation:
        return Conversation(
            row["id"], row["title"], row["created_at"], bool(row["pinned"])
        )

    @staticmethod
    def _turn(row: sqlite3.Row) -> Turn:
        return Turn(
            row["id"],
            row["conversation_id"],
            row["position"],
            row["submission_id"],
            TurnStatus(row["status"]),
            row["created_at"],
            row["error_code"],
        )

    @staticmethod
    def _message(row: sqlite3.Row) -> Message:
        parts = json.loads(row["content_json"])
        if (
            not isinstance(parts, list)
            or len(parts) != 1
            or parts[0].get("type") != "text"
        ):
            raise ValueError("Unsupported M1 message content")
        return Message(
            row["id"],
            row["conversation_id"],
            row["turn_id"],
            row["position"],
            row["role"],
            (TextPart(parts[0]["text"]),),
            row["status"],
            TurnStatus(row["turn_status"]),
            row["created_at"],
        )

    @_on_owner
    def create_conversation(self, title: str) -> Conversation:
        clean = title.strip()
        if not 1 <= len(clean) <= 200:
            raise ValueError("Conversation title must be 1–200 characters")
        conversation = Conversation(str(uuid4()), clean, _now())
        self._conn.execute(
            "INSERT INTO conversations (id, title, created_at) VALUES (?, ?, ?)",
            (conversation.id, conversation.title, conversation.created_at),
        )
        return conversation

    @_on_owner
    def list_conversations(self) -> list[Conversation]:
        rows = self._conn.execute(
            "SELECT id, title, created_at, pinned FROM conversations "
            "ORDER BY pinned DESC, created_at DESC, rowid DESC"
        ).fetchall()
        return [self._conversation(row) for row in rows]

    @_on_owner
    def get_conversation(self, conversation_id: str) -> Conversation:
        row = self._conn.execute(
            "SELECT id, title, created_at, pinned FROM conversations WHERE id=?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            raise ConversationNotFound(f"Conversation {conversation_id} does not exist")
        return self._conversation(row)

    @_on_owner
    def set_conversation_pinned(
        self, conversation_id: str, pinned: bool
    ) -> Conversation:
        if type(pinned) is not bool:
            raise ValueError("Pinned must be a boolean")
        with self._transaction():
            changed = self._conn.execute(
                "UPDATE conversations SET pinned=? WHERE id=?",
                (int(pinned), conversation_id),
            )
            if changed.rowcount != 1:
                raise ConversationNotFound(
                    f"Conversation {conversation_id} does not exist"
                )
            record = self.get_conversation(conversation_id)
        return record

    @_on_owner
    def get_turns(self, conversation_id: str) -> list[Turn]:
        self.get_conversation(conversation_id)
        rows = self._conn.execute(
            "SELECT * FROM turns WHERE conversation_id=? ORDER BY position",
            (conversation_id,),
        ).fetchall()
        return [self._turn(row) for row in rows]

    @_on_owner
    def get_messages(self, conversation_id: str) -> list[Message]:
        self.get_conversation(conversation_id)
        rows = self._conn.execute(
            "SELECT m.*, t.status AS turn_status FROM messages m "
            "JOIN turns t ON t.id=m.turn_id "
            "WHERE m.conversation_id=? ORDER BY m.position",
            (conversation_id,),
        ).fetchall()
        return [self._message(row) for row in rows]

    @_on_owner
    def get_recent_messages(
        self, conversation_id: str, before_position: int | None, limit_turns: int = 8
    ) -> tuple[list[Message], int | None]:
        """Read whole recent turns in a small page for context selection."""
        self.get_conversation(conversation_id)
        if not 1 <= limit_turns <= 32:
            raise ValueError("Context page size must be 1–32 turns")
        rows = self._conn.execute(
            "SELECT id, position FROM turns WHERE conversation_id=? "
            "AND (? IS NULL OR position < ?) ORDER BY position DESC LIMIT ?",
            (conversation_id, before_position, before_position, limit_turns),
        ).fetchall()
        if not rows:
            return [], None
        ids = [row["id"] for row in rows]
        placeholders = ",".join("?" for _ in ids)
        messages = self._conn.execute(
            "SELECT m.*, t.status AS turn_status FROM messages m "
            f"JOIN turns t ON t.id=m.turn_id WHERE m.turn_id IN ({placeholders}) "
            "ORDER BY t.position, m.position",
            ids,
        ).fetchall()
        return [self._message(row) for row in messages], rows[-1]["position"]

    @staticmethod
    def _invocation(row: sqlite3.Row) -> ToolInvocation:
        return ToolInvocation(
            row["id"],
            row["turn_id"],
            row["conversation_id"],
            row["ordinal"],
            row["model_call_id"],
            row["name"],
            row["version"],
            row["arguments_json"],
            row["arguments_sha256"],
            row["policy_version"],
            row["target_identity"],
            InvocationStatus(row["status"]),
            row["decision"],
            row["result_json"],
            row["receipt_json"],
            row["job_id"],
            row["job_attempt"],
            row["acknowledged_at"],
        )

    @_on_owner
    def get_invocations(self, turn_id: str) -> list[ToolInvocation]:
        rows = self._conn.execute(
            "SELECT * FROM tool_invocations WHERE turn_id=? ORDER BY ordinal",
            (turn_id,),
        ).fetchall()
        return [self._invocation(row) for row in rows]

    @_on_owner
    def list_unknown_invocations(self) -> list[ToolInvocation]:
        rows = self._conn.execute(
            "SELECT * FROM tool_invocations WHERE status='outcome_unknown' "
            "AND acknowledged_at IS NULL ORDER BY created_at, id"
        ).fetchall()
        return [self._invocation(row) for row in rows]

    @_on_owner
    def acknowledge_unknown(self, invocation_id: str) -> ToolInvocation:
        """Trusted user acknowledgment frees new turns; it never resolves or replays a tool."""
        with self._transaction():
            invocation = self.get_invocation(invocation_id)
            if invocation.status is not InvocationStatus.OUTCOME_UNKNOWN:
                raise StaleTurnError("Tool outcome is not uncertain")
            if invocation.job_id is not None:
                if self.get_job(invocation.job_id).status not in TERMINAL_JOBS:
                    raise StaleTurnError(
                        "Cancel the unresolved job before acknowledging its outcome"
                    )
            elif self.get_turn(invocation.turn_id).status is TurnStatus.PENDING:
                raise StaleTurnError("Turn is still active")
            self._conn.execute(
                "UPDATE tool_invocations SET acknowledged_at=COALESCE(acknowledged_at, ?) WHERE id=?",
                (_now(), invocation_id),
            )
        return self.get_invocation(invocation_id)

    @_on_owner
    def get_invocation_by_call(
        self, turn_id: str, model_call_id: str
    ) -> ToolInvocation | None:
        row = self._conn.execute(
            "SELECT * FROM tool_invocations WHERE turn_id=? AND model_call_id=?",
            (turn_id, model_call_id),
        ).fetchone()
        return self._invocation(row) if row is not None else None

    @_on_owner
    def get_recent_context(
        self,
        conversation_id: str,
        before_position: int | None,
        limit_turns: int = 8,
    ) -> tuple[list[ContextRecord], int | None]:
        messages, cursor = self.get_recent_messages(
            conversation_id, before_position, limit_turns
        )
        if not messages:
            return [], cursor
        by_turn: dict[str, list[Message]] = {}
        for message in messages:
            by_turn.setdefault(message.turn_id, []).append(message)
        result: list[ContextRecord] = []
        for turn_id, group in by_turn.items():
            result.extend(m for m in group if m.role == "user")
            for invocation in self.get_invocations(turn_id):
                result.append(
                    ToolContextRecord(
                        invocation.id,
                        turn_id,
                        conversation_id,
                        invocation.model_call_id,
                        invocation.name,
                        invocation.arguments_json,
                        "assistant_tool_call",
                    )
                )
                content = invocation.result_json or json.dumps(
                    {
                        "status": invocation.status.value,
                        "acknowledged_at": invocation.acknowledged_at,
                    },
                    separators=(",", ":"),
                )
                result.append(
                    ToolContextRecord(
                        invocation.id,
                        turn_id,
                        conversation_id,
                        invocation.model_call_id,
                        invocation.name,
                        content,
                        "tool",
                    )
                )
            result.extend(m for m in group if m.role == "assistant")
        return result, cursor

    @_on_owner
    def record_tool_proposal(
        self,
        turn_id: str,
        model_call_id: str,
        name: str,
        version: int,
        arguments_json: str,
        arguments_sha256: str,
        policy_version: str,
        target_identity: str,
        decision: str,
        expected_job_attempt: int | None = None,
    ) -> ToolInvocation:
        if decision not in {"allow", "approval", "deny"}:
            raise ValueError("Invalid tool policy decision")
        with self._transaction():
            turn = self._conn.execute(
                "SELECT * FROM turns WHERE id=?", (turn_id,)
            ).fetchone()
            if turn is None or turn["status"] != "pending":
                raise StaleTurnError("Turn is no longer pending")
            job = self._conn.execute(
                "SELECT * FROM jobs WHERE turn_id=?", (turn_id,)
            ).fetchone()
            if job is not None and (
                job["status"] != "running"
                or job["cancel_requested"]
                or job["attempt"] != expected_job_attempt
            ):
                raise StaleTurnError("Job cannot propose a tool")
            previous = self._conn.execute(
                "SELECT * FROM tool_invocations WHERE turn_id=? AND model_call_id=?",
                (turn_id, model_call_id),
            ).fetchone()
            if previous is not None:
                record = self._invocation(previous)
                if (
                    record.name != name
                    or record.arguments_sha256 != arguments_sha256
                    or record.version != version
                ):
                    raise ValueError("Tool call ID was reused with different arguments")
                return record
            ordinal = self._conn.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM tool_invocations WHERE turn_id=?",
                (turn_id,),
            ).fetchone()[0]
            if ordinal > 4:
                raise ValueError("Tool call limit exceeded")
            if (
                name == "generate_image"
                and self._conn.execute(
                    "SELECT 1 FROM tool_invocations WHERE turn_id=? AND name='generate_image' LIMIT 1",
                    (turn_id,),
                ).fetchone()
                is not None
            ):
                raise ValueError("Only one image is permitted per turn")
            if name == "generate_image" and job is None:
                raise ValueError("Image generation requires a durable job")
            invocation_id = str(uuid4())
            status = (
                "waiting_approval"
                if decision == "approval"
                else ("denied" if decision == "deny" else "proposed")
            )
            self._conn.execute(
                "INSERT INTO tool_invocations (id, turn_id, conversation_id, ordinal, "
                "model_call_id, name, version, arguments_json, arguments_sha256, "
                "policy_version, target_identity, status, decision, created_at, job_id, job_attempt) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    invocation_id,
                    turn_id,
                    turn["conversation_id"],
                    ordinal,
                    model_call_id,
                    name,
                    version,
                    arguments_json,
                    arguments_sha256,
                    policy_version,
                    target_identity,
                    status,
                    decision,
                    _now(),
                    job["id"] if job is not None else None,
                    job["attempt"] if job is not None else None,
                ),
            )
            if job is not None:
                self._conn.execute(
                    "INSERT INTO job_steps (id, job_id, ordinal, kind, invocation_id, "
                    "status, attempt, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'tool', ?, ?, 0, ?, ?)",
                    (
                        f"{job['id']}:tool:{ordinal}",
                        job["id"],
                        ordinal,
                        invocation_id,
                        "queued" if status == "proposed" else status,
                        _now(),
                        _now(),
                    ),
                )
                if name == "generate_image":
                    self._conn.execute(
                        "INSERT INTO image_steps (invocation_id, job_id, status, created_at, updated_at) "
                        "VALUES (?, ?, 'queued', ?, ?)",
                        (invocation_id, job["id"], _now(), _now()),
                    )
        return self.get_invocation(invocation_id)

    @_on_owner
    def issue_tool_approval(self, invocation_id: str, expires_at: str) -> Approval:
        with self._transaction():
            existing = self._conn.execute(
                "SELECT * FROM tool_approvals WHERE invocation_id=?", (invocation_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["status"] == "issued"
                    and existing["session_id"] == self.session_id
                ):
                    return self._approval(existing)
                raise StaleTurnError("Tool approval cannot be renewed")
            row = self._conn.execute(
                "SELECT * FROM tool_invocations WHERE id=?", (invocation_id,)
            ).fetchone()
            if row is None or row["status"] != "waiting_approval":
                raise StaleTurnError("Tool invocation is not awaiting approval")
            if row["job_id"] is not None:
                job = self._conn.execute(
                    "SELECT status, cancel_requested, attempt FROM jobs WHERE id=?",
                    (row["job_id"],),
                ).fetchone()
                if (
                    job is None
                    or job["status"] not in ("waiting_approval", "running")
                    or job["cancel_requested"]
                    or job["attempt"] != row["job_attempt"]
                ):
                    raise StaleTurnError("Job approval is no longer eligible")
            approval_id = str(uuid4())
            self._conn.execute(
                "INSERT INTO tool_approvals (id, invocation_id, turn_id, "
                "arguments_sha256, tool_version, target_identity, policy_version, "
                "session_id, expires_at, status, issued_at, job_id, job_attempt) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'issued', ?, ?, ?)",
                (
                    approval_id,
                    invocation_id,
                    row["turn_id"],
                    row["arguments_sha256"],
                    row["version"],
                    row["target_identity"],
                    row["policy_version"],
                    self.session_id,
                    expires_at,
                    _now(),
                    row["job_id"],
                    row["job_attempt"],
                ),
            )
        return Approval(
            approval_id,
            row["turn_id"],
            invocation_id,
            row["arguments_sha256"],
            row["version"],
            row["target_identity"],
            row["policy_version"],
            self.session_id,
            expires_at,
            "issued",
            row["job_id"],
            row["job_attempt"],
        )

    @staticmethod
    def _approval(row: sqlite3.Row) -> Approval:
        return Approval(
            row["id"],
            row["turn_id"],
            row["invocation_id"],
            row["arguments_sha256"],
            row["tool_version"],
            row["target_identity"],
            row["policy_version"],
            row["session_id"],
            row["expires_at"],
            row["status"],
            row["job_id"],
            row["job_attempt"],
        )

    @_on_owner
    def get_tool_approval(self, invocation_id: str) -> Approval | None:
        row = self._conn.execute(
            "SELECT * FROM tool_approvals WHERE invocation_id=?", (invocation_id,)
        ).fetchone()
        return self._approval(row) if row is not None else None

    @_on_owner
    def invalidate_tool_approval(self, invocation_id: str) -> None:
        with self._transaction():
            self._conn.execute(
                "UPDATE tool_approvals SET status='invalidated' "
                "WHERE invocation_id=? AND status='issued'",
                (invocation_id,),
            )

    @_on_owner
    def record_tool_intent(
        self,
        invocation_id: str,
        policy_version: str,
        target_identity: str,
        approval_id: str | None = None,
    ) -> ToolInvocation:
        with self._transaction():
            row = self._conn.execute(
                "SELECT i.*, t.status AS turn_status FROM tool_invocations i "
                "JOIN turns t ON t.id=i.turn_id WHERE i.id=?",
                (invocation_id,),
            ).fetchone()
            if row is None or row["turn_status"] != "pending":
                raise StaleTurnError("Tool turn is not pending")
            if row["job_id"] is not None:
                job = self._conn.execute(
                    "SELECT status, cancel_requested, attempt FROM jobs WHERE id=?",
                    (row["job_id"],),
                ).fetchone()
                if (
                    job is None
                    or job["status"] not in ("running", "waiting_approval")
                    or job["cancel_requested"]
                    or job["attempt"] != row["job_attempt"]
                ):
                    raise StaleTurnError("Tool job cannot execute")
                if (
                    self._conn.execute(
                        "SELECT 1 FROM jobs WHERE (status='running' OR (status='cancel_requested' AND attempt>0)) "
                        "AND id<>? LIMIT 1",
                        (row["job_id"],),
                    ).fetchone()
                    is not None
                ):
                    raise StaleTurnError("Another job is executing")
            if (
                row["policy_version"] != policy_version
                or row["target_identity"] != target_identity
            ):
                raise StaleTurnError("Tool policy or target changed")
            if row["decision"] == "allow":
                if row["status"] != "proposed" or approval_id is not None:
                    raise StaleTurnError("Tool invocation already started")
            elif row["decision"] == "approval":
                approval = self._conn.execute(
                    "SELECT * FROM tool_approvals WHERE id=? AND invocation_id=?",
                    (approval_id, invocation_id),
                ).fetchone()
                if (
                    row["status"] != "waiting_approval"
                    or approval is None
                    or approval["status"] != "issued"
                    or approval["session_id"] != self.session_id
                    or approval["expires_at"] <= _now()
                    or approval["arguments_sha256"] != row["arguments_sha256"]
                    or approval["tool_version"] != row["version"]
                    or approval["target_identity"] != target_identity
                    or approval["policy_version"] != policy_version
                    or approval["turn_id"] != row["turn_id"]
                    or approval["job_id"] != row["job_id"]
                    or approval["job_attempt"] != row["job_attempt"]
                ):
                    raise StaleTurnError("Tool approval is absent, stale, or consumed")
                self._conn.execute(
                    "UPDATE tool_approvals SET status='consumed', consumed_at=? WHERE id=?",
                    (_now(), approval_id),
                )
            else:
                raise StaleTurnError("Denied tool cannot execute")
            updated = self._conn.execute(
                "UPDATE tool_invocations SET status='intent' WHERE id=? AND status=?",
                (invocation_id, row["status"]),
            )
            if updated.rowcount != 1:
                raise StaleTurnError("Tool invocation changed before intent")
            if row["job_id"] is not None:
                self._conn.execute(
                    "UPDATE job_steps SET status='running', attempt=attempt+1, "
                    "updated_at=? WHERE invocation_id=? AND status IN "
                    "('queued', 'waiting_approval')",
                    (_now(), invocation_id),
                )
                if row["name"] == "generate_image":
                    self._conn.execute(
                        "UPDATE image_steps SET status='running', updated_at=? "
                        "WHERE invocation_id=? AND status='queued'",
                        (_now(), invocation_id),
                    )
                self._conn.execute(
                    "UPDATE jobs SET status='running', progress='tool_running', "
                    "updated_at=? WHERE id=? AND status='waiting_approval'",
                    (_now(), row["job_id"]),
                )
        return self.get_invocation(invocation_id)

    @_on_owner
    def get_invocation(self, invocation_id: str) -> ToolInvocation:
        row = self._conn.execute(
            "SELECT * FROM tool_invocations WHERE id=?", (invocation_id,)
        ).fetchone()
        if row is None:
            raise StaleTurnError("Tool invocation does not exist")
        return self._invocation(row)

    @_on_owner
    def list_unknown_file_invocations(self) -> list[ToolInvocation]:
        rows = self._conn.execute(
            "SELECT * FROM tool_invocations WHERE name='create_text_file' "
            "AND status='outcome_unknown' ORDER BY created_at, id"
        ).fetchall()
        return [self._invocation(row) for row in rows]

    @_on_owner
    def assert_tool_intent_active(self, invocation_id: str) -> None:
        row = self._conn.execute(
            "SELECT i.status, i.job_id, i.job_attempt, t.status AS turn_status, "
            "j.status AS job_status, j.attempt AS active_attempt, "
            "j.cancel_requested FROM tool_invocations i "
            "JOIN turns t ON t.id=i.turn_id "
            "LEFT JOIN jobs j ON j.id=i.job_id WHERE i.id=?",
            (invocation_id,),
        ).fetchone()
        if row is None or row["status"] != "intent" or row["turn_status"] != "pending":
            raise StaleTurnError("Tool intent is no longer active")
        if row["job_id"] is not None and (
            row["job_status"] != "running"
            or row["cancel_requested"]
            or row["job_attempt"] != row["active_attempt"]
        ):
            raise StaleTurnError("Tool job was cancelled or superseded")

    @_on_owner
    def commit_tool_result(
        self,
        invocation_id: str,
        receipt_json: str,
        result_json: str,
    ) -> ToolInvocation:
        with self._transaction():
            changed = self._conn.execute(
                "UPDATE tool_invocations SET status='complete', receipt_json=?, "
                "result_json=?, completed_at=? WHERE id=? AND status='intent'",
                (receipt_json, result_json, _now(), invocation_id),
            )
            if changed.rowcount != 1:
                raise StaleTurnError("Tool invocation is not executing")
            row = self._conn.execute(
                "SELECT job_id, ordinal FROM tool_invocations WHERE id=?",
                (invocation_id,),
            ).fetchone()
            if row["job_id"] is not None:
                self._conn.execute(
                    "UPDATE image_steps SET status='complete', updated_at=? "
                    "WHERE invocation_id=? AND status='running'",
                    (_now(), invocation_id),
                )
                self._conn.execute(
                    "UPDATE job_steps SET status='complete', updated_at=? "
                    "WHERE invocation_id=? AND status='running'",
                    (_now(), invocation_id),
                )
                self._conn.execute(
                    "UPDATE jobs SET next_step=?, progress='tool_complete', "
                    "updated_at=? WHERE id=? AND status='running' "
                    "AND cancel_requested=0",
                    (row["ordinal"] + 1, _now(), row["job_id"]),
                )
        return self.get_invocation(invocation_id)

    @_on_owner
    def reconcile_tool_result(
        self, invocation_id: str, receipt_json: str, result_json: str
    ) -> ToolInvocation:
        """Record a verified create-only postcondition without executing again."""
        with self._transaction():
            row = self._conn.execute(
                "SELECT name, job_id, ordinal FROM tool_invocations "
                "WHERE id=? AND status='outcome_unknown'",
                (invocation_id,),
            ).fetchone()
            if row is None or row["name"] != "create_text_file":
                raise StaleTurnError("File outcome is no longer uncertain")
            self._conn.execute(
                "UPDATE tool_invocations SET status='complete', receipt_json=?, "
                "result_json=?, completed_at=? WHERE id=? AND status='outcome_unknown'",
                (receipt_json, result_json, _now(), invocation_id),
            )
            if row["job_id"] is not None:
                self._conn.execute(
                    "UPDATE job_steps SET status='complete', updated_at=? "
                    "WHERE invocation_id=? AND status='outcome_unknown'",
                    (_now(), invocation_id),
                )
                self._conn.execute(
                    "UPDATE jobs SET next_step=?, progress='file_reconciled', updated_at=? "
                    "WHERE id=? AND status='interrupted' AND cancel_requested=0",
                    (row["ordinal"] + 1, _now(), row["job_id"]),
                )
        return self.get_invocation(invocation_id)

    @_on_owner
    def commit_image_result(
        self,
        invocation_id: str,
        artifact_id: str,
        relative_path: str,
        size_bytes: int,
        sha256: str,
        receipt_json: str,
        result_json: str,
    ) -> ToolInvocation:
        with self._transaction():
            row = self._conn.execute(
                "SELECT i.*, t.status AS turn_status, j.status AS job_status, "
                "j.cancel_requested, j.attempt AS active_attempt "
                "FROM tool_invocations i JOIN turns t ON t.id=i.turn_id "
                "JOIN jobs j ON j.id=i.job_id WHERE i.id=? AND i.name='generate_image'",
                (invocation_id,),
            ).fetchone()
            if (
                row is None
                or row["status"] != "intent"
                or row["turn_status"] != "pending"
                or row["job_status"] != "running"
                or row["cancel_requested"]
                or row["job_attempt"] != row["active_attempt"]
            ):
                raise StaleTurnError("Image intent is no longer active")
            step = self._conn.execute(
                "SELECT status FROM image_steps WHERE invocation_id=?",
                (invocation_id,),
            ).fetchone()
            if step is None or step["status"] != "running":
                raise StaleTurnError("Image step is not running")
            self._conn.execute(
                "INSERT INTO artifacts (id, job_id, relative_path, kind, size_bytes, sha256, status, created_at) "
                "VALUES (?, ?, ?, 'image/png', ?, ?, 'available', ?)",
                (artifact_id, row["job_id"], relative_path, size_bytes, sha256, _now()),
            )
            self._conn.execute(
                "UPDATE image_steps SET status='complete', artifact_id=?, updated_at=? WHERE invocation_id=?",
                (artifact_id, _now(), invocation_id),
            )
            self._conn.execute(
                "UPDATE tool_invocations SET status='complete', receipt_json=?, result_json=?, completed_at=? "
                "WHERE id=?",
                (receipt_json, result_json, _now(), invocation_id),
            )
            self._conn.execute(
                "UPDATE job_steps SET status='complete', updated_at=? WHERE invocation_id=? AND status='running'",
                (_now(), invocation_id),
            )
            self._conn.execute(
                "UPDATE jobs SET next_step=?, progress='image_complete', updated_at=? "
                "WHERE id=? AND status='running' AND cancel_requested=0",
                (row["ordinal"] + 1, _now(), row["job_id"]),
            )
        return self.get_invocation(invocation_id)

    @_on_owner
    def commit_browser_result(
        self,
        invocation_id: str,
        artifact_id: str,
        relative_path: str,
        size_bytes: int,
        sha256: str,
        receipt_json: str,
        result_json: str,
    ) -> ToolInvocation:
        with self._transaction():
            row = self._conn.execute(
                "SELECT i.*, t.status AS turn_status, j.status AS job_status, "
                "j.cancel_requested, j.attempt AS active_attempt "
                "FROM tool_invocations i JOIN turns t ON t.id=i.turn_id "
                "JOIN jobs j ON j.id=i.job_id "
                "WHERE i.id=? AND i.name='render_public_page'",
                (invocation_id,),
            ).fetchone()
            if (
                row is None
                or row["status"] != "intent"
                or row["turn_status"] != "pending"
                or row["job_status"] != "running"
                or row["cancel_requested"]
                or row["job_attempt"] != row["active_attempt"]
            ):
                raise StaleTurnError("Browser intent is no longer active")
            self._conn.execute(
                "INSERT INTO artifacts (id, job_id, relative_path, kind, size_bytes, sha256, status, created_at) "
                "VALUES (?, ?, ?, 'screenshot/png', ?, ?, 'available', ?)",
                (artifact_id, row["job_id"], relative_path, size_bytes, sha256, _now()),
            )
            self._conn.execute(
                "UPDATE tool_invocations SET status='complete', receipt_json=?, result_json=?, completed_at=? "
                "WHERE id=?",
                (receipt_json, result_json, _now(), invocation_id),
            )
            self._conn.execute(
                "UPDATE job_steps SET status='complete', updated_at=? "
                "WHERE invocation_id=? AND status='running'",
                (_now(), invocation_id),
            )
            self._conn.execute(
                "UPDATE jobs SET next_step=?, progress='tool_complete', updated_at=? "
                "WHERE id=? AND status='running' AND cancel_requested=0",
                (row["ordinal"] + 1, _now(), row["job_id"]),
            )
        return self.get_invocation(invocation_id)

    @_on_owner
    def set_image_progress(self, invocation_id: str, stage: str) -> None:
        if stage not in {"image_loading", "image_generating", "image_finalizing"}:
            raise ValueError("Invalid image stage")
        self.assert_tool_intent_active(invocation_id)
        row = self._conn.execute(
            "SELECT job_id FROM tool_invocations WHERE id=? AND name='generate_image'",
            (invocation_id,),
        ).fetchone()
        if row is None or row["job_id"] is None:
            raise StaleTurnError("Image step is unavailable")
        self._conn.execute(
            "UPDATE jobs SET progress=?, updated_at=? WHERE id=? AND status='running' AND cancel_requested=0",
            (stage, _now(), row["job_id"]),
        )

    @_on_owner
    def mark_tool_unknown(self, invocation_id: str) -> ToolInvocation:
        with self._transaction():
            changed = self._conn.execute(
                "UPDATE tool_invocations SET status='outcome_unknown' "
                "WHERE id=? AND status='intent'",
                (invocation_id,),
            )
            if changed.rowcount != 1:
                raise StaleTurnError("Tool execution intent is not active")
            self._conn.execute(
                "UPDATE job_steps SET status='outcome_unknown', updated_at=? "
                "WHERE invocation_id=? AND status='running'",
                (_now(), invocation_id),
            )
            self._conn.execute(
                "UPDATE image_steps SET status='outcome_unknown', updated_at=? "
                "WHERE invocation_id=? AND status='running'",
                (_now(), invocation_id),
            )
        return self.get_invocation(invocation_id)

    @_on_owner
    def get_answer(self, turn_id: str) -> Message | None:
        row = self._conn.execute(
            "SELECT m.*, t.status AS turn_status FROM messages m "
            "JOIN turns t ON t.id=m.turn_id WHERE m.turn_id=? AND m.role='assistant'",
            (turn_id,),
        ).fetchone()
        return self._message(row) if row is not None else None

    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        return Job(
            row["id"],
            row["turn_id"],
            row["conversation_id"],
            row["profile_name"],
            JobStatus(row["status"]),
            row["next_step"],
            row["attempt"],
            row["deadline_at"],
            row["progress"],
            bool(row["cancel_requested"]),
            row["error_code"],
        )

    @_on_owner
    def get_job(self, job_id: str) -> Job:
        row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise StaleTurnError("Job does not exist")
        return self._job(row)

    @_on_owner
    def get_job_by_turn(self, turn_id: str) -> Job | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE turn_id=?", (turn_id,)
        ).fetchone()
        return self._job(row) if row is not None else None

    @_on_owner
    def list_jobs(self, conversation_id: str | None = None) -> list[Job]:
        if conversation_id is None:
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY created_at, id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE conversation_id=? ORDER BY created_at, id",
                (conversation_id,),
            ).fetchall()
        return [self._job(row) for row in rows]

    @_on_owner
    def recovery_counts(self) -> dict[str, int]:
        return {
            "unknown_invocations": self._conn.execute(
                "SELECT COUNT(*) FROM tool_invocations WHERE status='outcome_unknown'"
            ).fetchone()[0],
            "unavailable_artifacts": self._conn.execute(
                "SELECT COUNT(*) FROM artifacts WHERE status='unavailable'"
            ).fetchone()[0],
        }

    @_on_owner
    def get_job_steps(self, job_id: str) -> list[JobStep]:
        rows = self._conn.execute(
            "SELECT * FROM job_steps WHERE job_id=? ORDER BY ordinal, kind",
            (job_id,),
        ).fetchall()
        return [
            JobStep(
                row["id"],
                row["job_id"],
                row["ordinal"],
                row["kind"],
                row["invocation_id"],
                row["status"],
                row["attempt"],
            )
            for row in rows
        ]

    @_on_owner
    def get_artifact_bytes(self, artifact_id: str) -> bytes:
        row = self._conn.execute(
            "SELECT relative_path, size_bytes, sha256 FROM artifacts "
            "WHERE id=? AND status='available'",
            (artifact_id,),
        ).fetchone()
        if row is None:
            raise StaleTurnError("Artifact is unavailable")
        if row["size_bytes"] > 16 * 1024 * 1024:
            raise ValueError("Artifact exceeds browser download limit")
        path = verify_artifact(
            self.data_dir, row["relative_path"], row["size_bytes"], row["sha256"]
        )
        data = path.read_bytes()
        if (
            len(data) != row["size_bytes"]
            or hashlib.sha256(data).hexdigest() != row["sha256"]
        ):
            raise ValueError("Artifact changed during download")
        return data

    @_on_owner
    def list_artifacts_for_conversation(
        self, conversation_id: str
    ) -> list[dict[str, str]]:
        rows = self._conn.execute(
            "SELECT a.id, a.kind, a.status, j.turn_id FROM artifacts a "
            "JOIN jobs j ON j.id=a.job_id WHERE j.conversation_id=? "
            "ORDER BY a.created_at, a.id",
            (conversation_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @_on_owner
    def backup_database(self, destination: Path) -> None:
        """Use SQLite's online backup API; never copy the live WAL database."""
        with closing(sqlite3.connect(destination)) as snapshot:
            self._conn.backup(snapshot)

    @_on_owner
    def continue_approved_job(self, job_id: str, expected_attempt: int) -> Job:
        """Resume an approved step without resetting its attempt or proposal."""
        with self._transaction():
            job = self.get_job(job_id)
            if (
                job.status is not JobStatus.WAITING_APPROVAL
                or job.attempt != expected_attempt
                or job.cancel_requested
            ):
                raise StaleTurnError("Approval belongs to an inactive job")
            if (
                self._conn.execute(
                    "SELECT 1 FROM jobs WHERE (status='running' OR (status='cancel_requested' AND attempt>0)) "
                    "AND id<>? LIMIT 1",
                    (job_id,),
                ).fetchone()
                is not None
            ):
                raise StaleTurnError("Another job is executing")
            self._conn.execute(
                "UPDATE jobs SET status='running', progress='approved', deadline_at=?, "
                "updated_at=? WHERE id=?",
                (
                    (
                        datetime.now(UTC) + timedelta(seconds=JOB_EXECUTION_SECONDS)
                    ).isoformat(timespec="microseconds"),
                    _now(),
                    job_id,
                ),
            )
        return self.get_job(job_id)

    @_on_owner
    def start_job(self, job_id: str) -> Job:
        with self._transaction():
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if (
                row is None
                or row["status"] not in ("queued", "interrupted", "waiting_approval")
                or row["cancel_requested"]
            ):
                raise StaleTurnError("Job cannot start")
            unknown = self._conn.execute(
                "SELECT 1 FROM tool_invocations WHERE job_id=? "
                "AND status IN ('intent', 'outcome_unknown') LIMIT 1",
                (job_id,),
            ).fetchone()
            if unknown is not None:
                raise StaleTurnError("Unknown tool outcome blocks job resume")
            if row["attempt"] >= 3:
                self._conn.execute(
                    "UPDATE turns SET status='failed', error_code='timeout' "
                    "WHERE id=? AND status IN ('pending', 'interrupted')",
                    (row["turn_id"],),
                )
                self._conn.execute(
                    "UPDATE tool_invocations SET status='cancelled' WHERE job_id=? "
                    "AND status IN ('proposed', 'waiting_approval')",
                    (job_id,),
                )
                self._conn.execute(
                    "UPDATE tool_approvals SET status='invalidated' WHERE job_id=? "
                    "AND status='issued'",
                    (job_id,),
                )
                self._conn.execute(
                    "UPDATE job_steps SET status='failed', updated_at=? "
                    "WHERE job_id=? AND status IN ('queued', 'interrupted', 'waiting_approval')",
                    (_now(), job_id),
                )
                self._conn.execute(
                    "UPDATE jobs SET status='failed', error_code='timeout', "
                    "progress='attempt_limit', updated_at=? WHERE id=?",
                    (_now(), job_id),
                )
                return self.get_job(job_id)
            busy = self._conn.execute(
                "SELECT 1 FROM jobs WHERE (status='running' OR (status='cancel_requested' AND attempt>0)) "
                "AND id<>? LIMIT 1",
                (job_id,),
            ).fetchone()
            if busy is not None:
                raise StaleTurnError("Another job is executing")
            self._conn.execute(
                "UPDATE tool_invocations SET status='cancelled' WHERE job_id=? "
                "AND status IN ('proposed', 'waiting_approval')",
                (job_id,),
            )
            self._conn.execute(
                "UPDATE job_steps SET status='cancelled', updated_at=? WHERE "
                "job_id=? AND kind='tool' AND status IN ('queued', 'waiting_approval')",
                (_now(), job_id),
            )
            self._conn.execute(
                "UPDATE tool_approvals SET status='invalidated' WHERE job_id=? "
                "AND status='issued'",
                (job_id,),
            )
            changed = self._conn.execute(
                "UPDATE turns SET status='pending', error_code=NULL WHERE id=? "
                "AND status IN ('pending', 'interrupted')",
                (row["turn_id"],),
            )
            if changed.rowcount != 1:
                raise StaleTurnError("Job turn cannot resume")
            self._conn.execute(
                "UPDATE jobs SET status='running', attempt=attempt+1, "
                "deadline_at=?, progress='generating', updated_at=? WHERE id=?",
                (
                    (
                        datetime.now(UTC) + timedelta(seconds=JOB_EXECUTION_SECONDS)
                    ).isoformat(timespec="microseconds"),
                    _now(),
                    job_id,
                ),
            )
            self._conn.execute(
                "UPDATE job_steps SET status='running', attempt=attempt+1, "
                "updated_at=? WHERE job_id=? AND kind='chat' "
                "AND status IN ('queued', 'interrupted', 'running')",
                (_now(), job_id),
            )
        return self.get_job(job_id)

    @_on_owner
    def request_job_cancel(self, job_id: str) -> Job:
        with self._transaction():
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise StaleTurnError("Job does not exist")
            if JobStatus(row["status"]) not in TERMINAL_JOBS:
                self._conn.execute(
                    "UPDATE jobs SET status='cancel_requested', cancel_requested=1, "
                    "progress='cancelling', updated_at=? WHERE id=?",
                    (_now(), job_id),
                )
        return self.get_job(job_id)

    @_on_owner
    def settle_job_cancel(self, job_id: str) -> Job:
        with self._transaction():
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None or row["status"] != "cancel_requested":
                raise StaleTurnError("Job is not cancelling")
            self._conn.execute(
                "UPDATE turns SET status='interrupted', error_code='cancelled' "
                "WHERE id=? AND status IN ('pending', 'interrupted')",
                (row["turn_id"],),
            )
            self._conn.execute(
                "UPDATE turn_drafts SET status='interrupted' WHERE turn_id=?",
                (row["turn_id"],),
            )
            self._conn.execute(
                "UPDATE tool_invocations SET status='cancelled' WHERE job_id=? "
                "AND status IN ('proposed', 'waiting_approval')",
                (job_id,),
            )
            self._conn.execute(
                "UPDATE tool_approvals SET status='invalidated' WHERE job_id=? "
                "AND status='issued'",
                (job_id,),
            )
            self._conn.execute(
                "UPDATE job_steps SET status='cancelled', updated_at=? "
                "WHERE job_id=? AND status IN ('queued', 'running', 'waiting_approval', 'interrupted')",
                (_now(), job_id),
            )
            self._conn.execute(
                "UPDATE jobs SET status='cancelled', progress='cancelled', "
                "updated_at=? WHERE id=?",
                (_now(), job_id),
            )
        return self.get_job(job_id)

    @_on_owner
    def mark_job_waiting(self, job_id: str) -> Job:
        with self._transaction():
            changed = self._conn.execute(
                "UPDATE jobs SET status='waiting_approval', progress='waiting_approval', "
                "updated_at=? WHERE id=? AND status='running' AND cancel_requested=0",
                (_now(), job_id),
            )
            if changed.rowcount != 1:
                raise StaleTurnError("Job is not running")
        return self.get_job(job_id)

    @_on_owner
    def begin_turn(
        self,
        conversation_id: str,
        text: str,
        submission_id: str,
        job_id: str | None = None,
        profile_name: str = "fake",
        vision: VisionInput | None = None,
    ) -> tuple[Turn, bool]:
        if vision is not None and job_id is None:
            raise ValueError("Vision submission requires a durable job")
        if not text or len(text) > 65_536:
            raise ValueError("Message must be 1–65,536 characters")
        if not submission_id or len(submission_id) > 200:
            raise ValueError("Submission ID must be 1–200 characters")
        with self._transaction():
            self.get_conversation(conversation_id)
            prior = self._conn.execute(
                "SELECT t.*, m.content_json, v.artifact_id AS vision_artifact_id, "
                "a.sha256 AS vision_sha256, v.profile_name AS vision_profile "
                "FROM turns t "
                "JOIN messages m ON m.turn_id=t.id AND m.role='user' "
                "LEFT JOIN vision_inputs v ON v.turn_id=t.id "
                "LEFT JOIN artifacts a ON a.id=v.artifact_id "
                "WHERE t.conversation_id=? AND t.submission_id=?",
                (conversation_id, submission_id),
            ).fetchone()
            if prior is not None:
                if json.loads(prior["content_json"])[0]["text"] != text:
                    raise SubmissionConflict(
                        "Submission ID was already used with different text"
                    )
                if (vision is None) != (prior["vision_artifact_id"] is None) or (
                    vision is not None
                    and (
                        vision.sha256 != prior["vision_sha256"]
                        or not same_profile_identity(
                            vision.profile_name, prior["vision_profile"]
                        )
                    )
                ):
                    raise SubmissionConflict(
                        "Submission ID was already used with a different screenshot"
                    )
                return self._turn(prior), False
            active = self._conn.execute(
                "SELECT 1 FROM turns WHERE conversation_id=? AND status='pending' "
                "UNION SELECT 1 FROM tool_invocations WHERE conversation_id=? "
                "AND (status='intent' OR (status='outcome_unknown' AND acknowledged_at IS NULL))",
                (conversation_id, conversation_id),
            ).fetchone()
            if active is not None:
                raise ActiveTurnError("This conversation already has an active turn")
            occupied = self._conn.execute(
                "SELECT 1 FROM jobs WHERE conversation_id=? AND "
                "(status NOT IN ('succeeded', 'failed', 'cancelled') OR "
                "EXISTS (SELECT 1 FROM tool_invocations WHERE job_id=jobs.id "
                "AND status='outcome_unknown' AND acknowledged_at IS NULL)) LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if occupied is not None:
                raise ActiveTurnError("This conversation has an unresolved job")
            if job_id is not None:
                queued = self._conn.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status='queued'"
                ).fetchone()[0]
                if queued >= 16:
                    raise ActiveTurnError("The job queue is full")
            turn_position = self._conn.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 FROM turns WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()[0]
            message_position = self._conn.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 FROM messages WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()[0]
            turn = Turn(
                str(uuid4()),
                conversation_id,
                turn_position,
                submission_id,
                TurnStatus.PENDING,
                _now(),
                None,
            )
            self._conn.execute(
                "INSERT INTO turns "
                "(id, conversation_id, position, submission_id, status, created_at, error_code) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    turn.id,
                    turn.conversation_id,
                    turn.position,
                    turn.submission_id,
                    turn.status.value,
                    turn.created_at,
                    turn.error_code,
                ),
            )
            self._conn.execute(
                "INSERT INTO messages "
                "(id, conversation_id, turn_id, position, role, content_json, status, created_at) "
                "VALUES (?, ?, ?, ?, 'user', ?, 'complete', ?)",
                (
                    f"{turn.id}:user",
                    conversation_id,
                    turn.id,
                    message_position,
                    _content(text),
                    _now(),
                ),
            )
            if job_id is not None:
                now = _now()
                deadline = (
                    datetime.now(UTC) + timedelta(seconds=JOB_EXECUTION_SECONDS)
                ).isoformat(timespec="microseconds")
                self._conn.execute(
                    "INSERT INTO jobs (id, turn_id, conversation_id, kind, version, profile_name, "
                    "status, next_step, attempt, deadline_at, progress, "
                    "cancel_requested, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'chat', 1, ?, 'queued', 1, 0, ?, 'queued', 0, ?, ?)",
                    (
                        job_id,
                        turn.id,
                        conversation_id,
                        profile_name,
                        deadline,
                        now,
                        now,
                    ),
                )
                self._conn.execute(
                    "INSERT INTO job_steps (id, job_id, ordinal, kind, status, "
                    "attempt, created_at, updated_at) "
                    "VALUES (?, ?, 0, 'chat', 'queued', 0, ?, ?)",
                    (f"{job_id}:chat", job_id, now, now),
                )
                if vision is not None:
                    if vision.job_id != job_id or vision.profile_name != profile_name:
                        raise ValueError("Vision submission identity mismatch")
                    self._conn.execute(
                        "INSERT INTO artifacts (id, job_id, relative_path, kind, "
                        "size_bytes, sha256, status, created_at) "
                        "VALUES (?, ?, ?, 'screenshot/png', ?, ?, 'available', ?)",
                        (
                            vision.artifact_id,
                            job_id,
                            vision.relative_path,
                            vision.size_bytes,
                            vision.sha256,
                            now,
                        ),
                    )
                    self._conn.execute(
                        "INSERT INTO vision_inputs (turn_id, artifact_id, profile_name, width, height) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            turn.id,
                            vision.artifact_id,
                            vision.profile_name,
                            vision.width,
                            vision.height,
                        ),
                    )
            return turn, True

    @_on_owner
    def get_vision_input(self, turn_id: str) -> VisionInput | None:
        row = self._conn.execute(
            "SELECT v.artifact_id, a.job_id, a.relative_path, a.size_bytes, "
            "a.sha256, v.width, v.height, v.profile_name "
            "FROM vision_inputs v JOIN artifacts a ON a.id=v.artifact_id "
            "WHERE v.turn_id=?",
            (turn_id,),
        ).fetchone()
        return VisionInput(**dict(row)) if row is not None else None

    @_on_owner
    def get_user_message(self, turn_id: str) -> Message:
        row = self._conn.execute(
            "SELECT m.*, t.status AS turn_status FROM messages m "
            "JOIN turns t ON t.id=m.turn_id WHERE m.turn_id=? AND m.role='user'",
            (turn_id,),
        ).fetchone()
        if row is None:
            raise StaleTurnError("User message is missing")
        return self._message(row)

    @_on_owner
    def complete_turn(
        self, turn_id: str, answer: str, expected_job_attempt: int | None = None
    ) -> Message:
        if not answer:
            raise ValueError("Provider returned an empty answer")
        with self._transaction():
            turn = self._conn.execute(
                "SELECT * FROM turns WHERE id=?", (turn_id,)
            ).fetchone()
            if turn is None or turn["status"] != TurnStatus.PENDING.value:
                raise StaleTurnError("Turn is no longer pending")
            job = self._conn.execute(
                "SELECT * FROM jobs WHERE turn_id=?", (turn_id,)
            ).fetchone()
            if job is not None and (
                job["status"] != "running"
                or job["cancel_requested"]
                or job["attempt"] != expected_job_attempt
            ):
                raise StaleTurnError("Job is not running or cancellation was requested")
            unresolved = self._conn.execute(
                "SELECT 1 FROM tool_invocations WHERE turn_id=? "
                "AND status NOT IN ('complete', 'denied', 'cancelled') LIMIT 1",
                (turn_id,),
            ).fetchone()
            if unresolved is not None:
                raise StaleTurnError("Tool invocation is unresolved")
            message_position = self._conn.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 FROM messages WHERE conversation_id=?",
                (turn["conversation_id"],),
            ).fetchone()[0]
            self._conn.execute(
                "INSERT INTO messages "
                "(id, conversation_id, turn_id, position, role, content_json, status, created_at) "
                "VALUES (?, ?, ?, ?, 'assistant', ?, 'complete', ?)",
                (
                    f"{turn_id}:assistant",
                    turn["conversation_id"],
                    turn_id,
                    message_position,
                    _content(answer),
                    _now(),
                ),
            )
            updated = self._conn.execute(
                "UPDATE turns SET status='complete' WHERE id=? AND status='pending'",
                (turn_id,),
            )
            if updated.rowcount != 1:
                raise StaleTurnError("Turn changed before completion")
            self._conn.execute("DELETE FROM turn_drafts WHERE turn_id=?", (turn_id,))
            if job is not None:
                self._conn.execute(
                    "UPDATE job_steps SET status='complete', updated_at=? "
                    "WHERE job_id=? AND kind='chat' AND status='running'",
                    (_now(), job["id"]),
                )
                self._conn.execute(
                    "UPDATE jobs SET status='succeeded', progress='complete', "
                    "updated_at=? WHERE id=? AND status='running'",
                    (_now(), job["id"]),
                )
        result = self.get_answer(turn_id)
        assert result is not None
        return result

    @_on_owner
    def fail_turn(
        self,
        turn_id: str,
        code: str = "provider_error",
        expected_job_attempt: int | None = None,
    ) -> Turn:
        with self._transaction():
            job = self._conn.execute(
                "SELECT attempt, status FROM jobs WHERE turn_id=?", (turn_id,)
            ).fetchone()
            if job is not None and (
                job["attempt"] != expected_job_attempt or job["status"] != "running"
            ):
                raise StaleTurnError("Stale job failure attempt")
            updated = self._conn.execute(
                "UPDATE turns SET status='failed', error_code=? WHERE id=? AND status='pending'",
                (code, turn_id),
            )
            if updated.rowcount != 1:
                raise StaleTurnError("Turn changed before failure was recorded")
            self._conn.execute(
                "UPDATE turn_drafts SET status='interrupted' WHERE turn_id=?",
                (turn_id,),
            )
            self._conn.execute(
                "UPDATE tool_invocations SET status='cancelled' WHERE turn_id=? "
                "AND status IN ('proposed', 'waiting_approval')",
                (turn_id,),
            )
            self._conn.execute(
                "UPDATE tool_approvals SET status='invalidated' WHERE turn_id=? "
                "AND status='issued'",
                (turn_id,),
            )
            self._conn.execute(
                "UPDATE jobs SET status='failed', progress='failed', error_code=?, "
                "updated_at=? WHERE turn_id=? AND status='running' AND cancel_requested=0",
                (code, _now(), turn_id),
            )
            self._conn.execute(
                "UPDATE job_steps SET status='failed', updated_at=? WHERE "
                "job_id=(SELECT id FROM jobs WHERE turn_id=?) AND kind='chat' "
                "AND status='running'",
                (_now(), turn_id),
            )
        row = self._conn.execute(
            "SELECT * FROM turns WHERE id=?", (turn_id,)
        ).fetchone()
        assert row is not None
        return self._turn(row)

    @_on_owner
    def grant_retrieval_root(
        self, path: str, conversation_id: str | None = None
    ) -> RetrievalRoot:
        scope_key = conversation_id or ""
        with self._transaction():
            row = self._conn.execute(
                "SELECT id FROM retrieval_roots WHERE path=? AND scope_key=?",
                (path, scope_key),
            ).fetchone()
            if row is None:
                root_id = str(uuid4())
                self._conn.execute(
                    "INSERT INTO retrieval_roots VALUES (?, ?, ?, ?, 1, ?)",
                    (root_id, path, conversation_id, scope_key, _now()),
                )
            else:
                root_id = row["id"]
                self._conn.execute(
                    "UPDATE retrieval_roots SET active=1 WHERE id=?", (root_id,)
                )
        return RetrievalRoot(root_id, path, conversation_id, True)

    @_on_owner
    def list_retrieval_roots(self) -> list[RetrievalRoot]:
        rows = self._conn.execute(
            "SELECT id, path, conversation_id, active FROM retrieval_roots ORDER BY path, scope_key"
        ).fetchall()
        return [
            RetrievalRoot(r["id"], r["path"], r["conversation_id"], bool(r["active"]))
            for r in rows
        ]

    @_on_owner
    def get_retrieval_root(self, root_id: str) -> RetrievalRoot:
        row = self._conn.execute(
            "SELECT id, path, conversation_id, active FROM retrieval_roots WHERE id=?",
            (root_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Unknown retrieval root")
        return RetrievalRoot(
            row["id"], row["path"], row["conversation_id"], bool(row["active"])
        )

    @_on_owner
    def revoke_retrieval_root(self, root_id: str) -> None:
        with self._transaction():
            changed = self._conn.execute(
                "UPDATE retrieval_roots SET active=0 WHERE id=? AND active=1",
                (root_id,),
            )
            if changed.rowcount != 1:
                raise ValueError("Retrieval root is not active")
            self._conn.execute(
                "DELETE FROM retrieval_fts WHERE kind='document' AND source_id IN "
                "(SELECT id FROM retrieval_documents WHERE root_id=?)",
                (root_id,),
            )
            self._conn.execute(
                "UPDATE retrieval_documents SET status='stale', updated_at=? WHERE root_id=?",
                (_now(), root_id),
            )

    @_on_owner
    def add_memory(self, text: str, conversation_id: str | None) -> Memory:
        memory_id = str(uuid4())
        with self._transaction():
            self._conn.execute(
                "INSERT INTO memories VALUES (?, ?, ?, ?, ?)",
                (memory_id, conversation_id, text, _now(), _now()),
            )
            self._conn.execute(
                "INSERT INTO retrieval_fts (kind, source_id, location, source_hash, conversation_id, text) "
                "VALUES ('memory', ?, ?, '', ?, ?)",
                (memory_id, f"memory:{memory_id}", conversation_id, text),
            )
        return Memory(memory_id, conversation_id, text)

    @_on_owner
    def list_memories(self, conversation_id: str | None = None) -> list[Memory]:
        if conversation_id is None:
            rows = self._conn.execute(
                "SELECT id, conversation_id, text FROM memories ORDER BY created_at, id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, conversation_id, text FROM memories "
                "WHERE conversation_id IS NULL OR conversation_id=? ORDER BY created_at, id",
                (conversation_id,),
            ).fetchall()
        return [Memory(r["id"], r["conversation_id"], r["text"]) for r in rows]

    @_on_owner
    def edit_memory(self, memory_id: str, text: str) -> Memory:
        with self._transaction():
            changed = self._conn.execute(
                "UPDATE memories SET text=?, updated_at=? WHERE id=?",
                (text, _now(), memory_id),
            )
            if changed.rowcount != 1:
                raise ValueError("Unknown memory")
            self._conn.execute(
                "UPDATE retrieval_fts SET text=? WHERE kind='memory' AND source_id=?",
                (text, memory_id),
            )
        row = self._conn.execute(
            "SELECT conversation_id FROM memories WHERE id=?", (memory_id,)
        ).fetchone()
        assert row is not None
        return Memory(memory_id, row["conversation_id"], text)

    @_on_owner
    def delete_memory(self, memory_id: str) -> None:
        with self._transaction():
            changed = self._conn.execute(
                "DELETE FROM memories WHERE id=?", (memory_id,)
            )
            if changed.rowcount != 1:
                raise ValueError("Unknown memory")
            self._conn.execute(
                "DELETE FROM retrieval_fts WHERE kind='memory' AND source_id=?",
                (memory_id,),
            )

    @_on_owner
    def upsert_document(
        self,
        root_id: str,
        path: str,
        sha256: str,
        chunks: list[tuple[str, str]],
        *,
        document_id: str | None = None,
    ) -> Document:
        with self._transaction():
            root = self._conn.execute(
                "SELECT active FROM retrieval_roots WHERE id=?", (root_id,)
            ).fetchone()
            if root is None or not root["active"]:
                raise ValueError("Retrieval root was revoked")
            prior = self._conn.execute(
                "SELECT id FROM retrieval_documents WHERE root_id=? AND path=?",
                (root_id, path),
            ).fetchone()
            if prior is not None:
                if document_id is not None and document_id != prior["id"]:
                    raise ValueError("Document identity changed")
                document_id = prior["id"]
                self._conn.execute(
                    "DELETE FROM retrieval_fts WHERE kind='document' AND source_id=?",
                    (document_id,),
                )
                self._conn.execute(
                    "UPDATE retrieval_documents SET sha256=?, status='current', updated_at=? WHERE id=?",
                    (sha256, _now(), document_id),
                )
            else:
                document_id = document_id or str(uuid4())
                self._conn.execute(
                    "INSERT INTO retrieval_documents VALUES (?, ?, ?, ?, 'current', ?, ?)",
                    (document_id, root_id, path, sha256, _now(), _now()),
                )
            for location, text in chunks:
                self._conn.execute(
                    "INSERT INTO retrieval_fts (kind, source_id, location, source_hash, conversation_id, text) "
                    "VALUES ('document', ?, ?, ?, NULL, ?)",
                    (document_id, location, sha256, text),
                )
        return Document(document_id, root_id, path, sha256, "current")

    @_on_owner
    def list_documents(self) -> list[Document]:
        rows = self._conn.execute(
            "SELECT id, root_id, path, sha256, status FROM retrieval_documents ORDER BY path"
        ).fetchall()
        return [Document(*row) for row in rows]

    @_on_owner
    def get_document(self, document_id: str) -> Document:
        row = self._conn.execute(
            "SELECT id, root_id, path, sha256, status FROM retrieval_documents WHERE id=?",
            (document_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Unknown document")
        return Document(*row)

    @_on_owner
    def invalidate_document(self, document_id: str, expected_sha256: str) -> None:
        with self._transaction():
            changed = self._conn.execute(
                "UPDATE retrieval_documents SET status='stale', updated_at=? "
                "WHERE id=? AND sha256=? AND status='current'",
                (_now(), document_id, expected_sha256),
            )
            if changed.rowcount:
                self._conn.execute(
                    "DELETE FROM retrieval_fts WHERE kind='document' AND source_id=?",
                    (document_id,),
                )

    @_on_owner
    def delete_document(self, document_id: str) -> None:
        with self._transaction():
            changed = self._conn.execute(
                "DELETE FROM retrieval_documents WHERE id=?", (document_id,)
            )
            if changed.rowcount != 1:
                raise ValueError("Unknown document")
            self._conn.execute(
                "DELETE FROM retrieval_fts WHERE kind='document' AND source_id=?",
                (document_id,),
            )

    @_on_owner
    def search_retrieval(
        self, query: str, conversation_id: str, limit: int = 8
    ) -> list[RetrievalHit]:
        rows = self._conn.execute(
            "SELECT f.kind, f.source_id, f.location, f.source_hash, f.text, "
            "r.path AS root_path, d.path AS path "
            "FROM retrieval_fts AS f "
            "LEFT JOIN memories AS m ON f.kind='memory' AND m.id=f.source_id "
            "LEFT JOIN retrieval_documents AS d ON f.kind='document' AND d.id=f.source_id "
            "LEFT JOIN retrieval_roots AS r ON d.root_id=r.id "
            "WHERE retrieval_fts MATCH ? AND "
            "((f.kind='memory' AND m.id IS NOT NULL AND "
            "(m.conversation_id IS NULL OR m.conversation_id=?)) OR "
            "(f.kind='document' AND d.status='current' AND r.active=1 AND "
            "(r.conversation_id IS NULL OR r.conversation_id=?))) "
            "ORDER BY bm25(retrieval_fts), f.rowid LIMIT ?",
            (query, conversation_id, conversation_id, limit),
        ).fetchall()
        return [RetrievalHit(*row) for row in rows]
