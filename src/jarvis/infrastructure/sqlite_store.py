"""SQLite conversation store and per-data-directory process lock."""

import asyncio
import hashlib
import json
import msvcrt
import sqlite3
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import wraps
from importlib import resources
from pathlib import Path
from typing import Self, TypeVar
from uuid import uuid4

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
)

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
        with self._transaction():
            self._conn.execute(
                "UPDATE turns SET status='interrupted', error_code='interrupted' WHERE status='pending'"
            )
            self._conn.execute(
                "UPDATE turn_drafts SET status='interrupted' WHERE status='draft'"
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
        for version, filename in ((1, "001_initial.sql"), (2, "002_drafts.sql")):
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

    async def run(self, operation: Callable[[], T]) -> T:
        """Serialize database work; cancellation does not cancel its commit."""
        future = self._worker.submit(operation)
        try:
            return await asyncio.shield(asyncio.wrap_future(future))
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
    def checkpoint_draft(self, turn_id: str, text: str) -> None:
        with self._transaction():
            row = self._conn.execute(
                "SELECT status FROM turns WHERE id=?", (turn_id,)
            ).fetchone()
            if row is None or row["status"] != "pending":
                raise StaleTurnError("Turn is no longer pending")
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
    def interrupt_turn(self, turn_id: str, code: str = "cancelled") -> Turn:
        with self._transaction():
            self._conn.execute(
                "UPDATE turns SET status='interrupted', error_code=? WHERE id=? AND status='pending'",
                (code, turn_id),
            )
            self._conn.execute(
                "UPDATE turn_drafts SET status='interrupted' WHERE turn_id=?",
                (turn_id,),
            )
        return self.get_turn(turn_id)

    @staticmethod
    def _conversation(row: sqlite3.Row) -> Conversation:
        return Conversation(row["id"], row["title"], row["created_at"])

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
            "SELECT id, title, created_at FROM conversations ORDER BY created_at, id"
        ).fetchall()
        return [self._conversation(row) for row in rows]

    @_on_owner
    def get_conversation(self, conversation_id: str) -> Conversation:
        row = self._conn.execute(
            "SELECT id, title, created_at FROM conversations WHERE id=?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            raise ConversationNotFound(f"Conversation {conversation_id} does not exist")
        return self._conversation(row)

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

    @_on_owner
    def get_answer(self, turn_id: str) -> Message | None:
        row = self._conn.execute(
            "SELECT m.*, t.status AS turn_status FROM messages m "
            "JOIN turns t ON t.id=m.turn_id WHERE m.turn_id=? AND m.role='assistant'",
            (turn_id,),
        ).fetchone()
        return self._message(row) if row is not None else None

    @_on_owner
    def begin_turn(
        self, conversation_id: str, text: str, submission_id: str
    ) -> tuple[Turn, bool]:
        if not text or len(text) > 65_536:
            raise ValueError("Message must be 1–65,536 characters")
        if not submission_id or len(submission_id) > 200:
            raise ValueError("Submission ID must be 1–200 characters")
        with self._transaction():
            self.get_conversation(conversation_id)
            prior = self._conn.execute(
                "SELECT t.*, m.content_json FROM turns t "
                "JOIN messages m ON m.turn_id=t.id AND m.role='user' "
                "WHERE t.conversation_id=? AND t.submission_id=?",
                (conversation_id, submission_id),
            ).fetchone()
            if prior is not None:
                if json.loads(prior["content_json"])[0]["text"] != text:
                    raise SubmissionConflict(
                        "Submission ID was already used with different text"
                    )
                return self._turn(prior), False
            active = self._conn.execute(
                "SELECT 1 FROM turns WHERE conversation_id=? AND status='pending'",
                (conversation_id,),
            ).fetchone()
            if active is not None:
                raise ActiveTurnError("This conversation already has an active turn")
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
            return turn, True

    @_on_owner
    def complete_turn(self, turn_id: str, answer: str) -> Message:
        if not answer:
            raise ValueError("Provider returned an empty answer")
        with self._transaction():
            turn = self._conn.execute(
                "SELECT * FROM turns WHERE id=?", (turn_id,)
            ).fetchone()
            if turn is None or turn["status"] != TurnStatus.PENDING.value:
                raise StaleTurnError("Turn is no longer pending")
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
        result = self.get_answer(turn_id)
        assert result is not None
        return result

    @_on_owner
    def fail_turn(self, turn_id: str, code: str = "provider_error") -> Turn:
        with self._transaction():
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
        row = self._conn.execute(
            "SELECT * FROM turns WHERE id=?", (turn_id,)
        ).fetchone()
        assert row is not None
        return self._turn(row)
