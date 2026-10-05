"""Tiny child process for M3 lifecycle tests; no model or GPU access."""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=(
            "normal",
            "hang",
            "crash",
            "crash_later",
            "stall",
            "child_tree",
            "child",
            "chatty",
        ),
        required=True,
    )
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--child-pid-file", type=Path, required=True)
    parser.add_argument("--ready-delay", type=float, default=0)
    args = parser.parse_args()
    if args.mode == "crash":
        return 7
    if args.mode == "chatty":
        for _ in range(128):
            os.write(1, b"x" * 8192)
            os.write(2, b"e" * 8192)
    if args.mode == "child_tree":
        child = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--mode",
                "child",
                "--ready-file",
                str(args.ready_file),
                "--stop-file",
                str(args.stop_file),
                "--child-pid-file",
                str(args.child_pid_file),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            close_fds=True,
        )
        args.child_pid_file.write_text(str(child.pid), encoding="ascii")
    deadline = time.monotonic() + args.ready_delay
    while time.monotonic() < deadline:
        if args.stop_file.exists():
            return 0
        time.sleep(0.025)
    if args.mode not in {"stall", "child"}:
        args.ready_file.touch()
    crash_at = time.monotonic() + 0.3 if args.mode == "crash_later" else None
    while True:
        if crash_at is not None and time.monotonic() >= crash_at:
            return 8
        if args.mode not in {"hang", "child"} and args.stop_file.exists():
            return 0
        time.sleep(0.025)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        os._exit(130)
