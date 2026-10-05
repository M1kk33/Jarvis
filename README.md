# Jarvis (M3)

Jarvis currently supports durable terminal conversations using a deterministic fake provider. M2 added bounded prompt selection, streamed terminal output, recoverable partial drafts, cancellation handling, and private redacted diagnostics. M3 adds an exclusive resource manager and Windows Job Object supervisor exercised with fake child processes. It does not run a language model. Messages and turn status live in SQLite so a conversation can be reopened after the application exits.

## Setup

On Windows, install [uv](https://docs.astral.sh/uv/) and run from this directory:

```powershell
uv sync --python 3.12
```

This installs the locked Python dependencies. M3 requires native Windows for its lifecycle tests; neither terminal chat nor the fake workers require a model download, GPU, or network service. The data directory is always chosen explicitly; use a private local folder outside the source repository, for example `E:\JarvisData`.

## Terminal commands

```powershell
uv run --python 3.12 python -m jarvis --data-dir E:\JarvisData create "My chat"
uv run --python 3.12 python -m jarvis --data-dir E:\JarvisData list
uv run --python 3.12 python -m jarvis --data-dir E:\JarvisData send CONVERSATION_ID "Hello"
uv run --python 3.12 python -m jarvis --data-dir E:\JarvisData resume CONVERSATION_ID
```

`create` prints the conversation ID. `send` prints a submission ID before calling the provider. Reuse it with `send CONVERSATION_ID "Hello" --submission-id SUBMISSION_ID` if a command's outcome is unclear; the same ID and text return the stored outcome without calling the provider again. A different text with that ID is rejected. An interrupted or failed turn remains visible in `resume` and is not silently replayed. Only one application instance may open a data directory at a time.

The fake provider streams an echo with the count of selected messages it received, making restart reconstruction visible. The default budget is 4,096 estimated tokens including 512 for output and 256 for overhead; oversized required input fails explicitly and older exchanges are removed as whole groups. A partial response is kept as a draft and marked interrupted after failure or restart, never shown as a completed reply. Diagnostic metadata is stored in `logs/diagnostics.jsonl` under the chosen data directory; prompts and response text are excluded. The fake provider is for testing behavior, not useful AI responses.

M3's fake-process lifecycle is exercised by the offline tests; terminal chat still uses the deterministic in-process fake. The manager admits at most one resident heavy worker, retains a named per-device lock while warm, and switches only after the owned process tree exits. It checks measured profile bounds against 1 GiB free VRAM and 3 GiB available RAM reserves. Missing GPU telemetry permits only a previously calibrated profile and is reported as degraded; missing RAM telemetry blocks admission. The Windows supervisor assigns each worker to a kill-on-close Job Object at creation and bounds cleanup. No real model profile is shipped in M3.

## Checks

```powershell
uv run --offline --locked --python 3.12 pytest
uv run --offline --locked --python 3.12 ruff check .
uv run --offline --locked --python 3.12 ruff format --check .
uv run --offline --locked --python 3.12 mypy src/jarvis
```

The tests use temporary real SQLite databases and fake Windows processes, and run offline. See `ARCHITECTURE.md` for the design and later milestones; M1 through M3 are implemented here.
