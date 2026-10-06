"""Private, structured diagnostics with a strict metadata allowlist."""

import json
import logging
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


class Diagnostics:
    _events = frozenset(
        {
            "turn_complete",
            "turn_failed",
            "turn_cancelled",
            "job_queued",
            "job_running",
            "job_waiting",
            "job_succeeded",
            "job_failed",
            "job_cancel_requested",
            "job_cancelled",
            "tool_proposed",
            "tool_complete",
            "tool_unknown",
        }
    )
    _fields = frozenset(
        {
            "turn_id",
            "job_id",
            "invocation_id",
            "attempt",
            "bytes",
            "code",
        }
    )

    def __init__(self, data_dir: Path) -> None:
        path = data_dir / "logs"
        path.mkdir(parents=True, exist_ok=True)
        self._logger = logging.getLogger(f"jarvis.{data_dir.resolve()}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        if not self._logger.handlers:
            handler = RotatingFileHandler(
                path / "diagnostics.jsonl",
                maxBytes=1_000_000,
                backupCount=2,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)

    def __call__(self, event: str, fields: dict[str, str | int]) -> None:
        if event not in self._events or set(fields) - self._fields:
            raise ValueError("Unsupported diagnostic metadata")
        self._logger.info(
            json.dumps(
                {"at": datetime.now(UTC).isoformat(), "event": event, **fields},
                separators=(",", ":"),
            )
        )

    def close(self) -> None:
        for handler in list(self._logger.handlers):
            self._logger.removeHandler(handler)
            handler.close()
