"""Wire the conversation application to its store, fake provider, and terminal UI."""

import sqlite3
import sys
from pathlib import Path

from jarvis.application.conversations import ConversationService
from jarvis.domain.types import ConversationError
from jarvis.infrastructure.diagnostics import Diagnostics
from jarvis.infrastructure.providers.fake import FakeChatProvider
from jarvis.infrastructure.sqlite_store import SqliteStore
from jarvis.interfaces.cli import dispatch, parse_args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        with SqliteStore(Path(args.data_dir)) as store:
            diagnostics = Diagnostics(store.data_dir)
            try:
                service = ConversationService(
                    store, FakeChatProvider(), diagnostics=diagnostics
                )
                return dispatch(args, service)
            finally:
                diagnostics.close()
    except (ConversationError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
