# Jarvis (M5)

Jarvis supports durable terminal conversations with a deterministic offline fake or one pinned local Qwen3-4B text model. M5 adds three validated read-only tools: current UTC time, a bounded directory listing, and a bounded UTF-8 file read. Tool proposals, approvals, execution intents, receipts, and results are durable SQLite records. No shell, network, or file-writing tool is available.

## Setup

On Windows, install [uv](https://docs.astral.sh/uv/) and run from this directory:

```powershell
uv sync --python 3.12
```

This installs the locked Python dependencies. The offline fake and tests require no model download or GPU. The local model requires native Windows, a compatible NVIDIA GPU and driver, and the pinned runtime/model files. The data directory is always chosen explicitly; use a private local folder outside the source repository, for example `E:\JarvisData`.

## Terminal commands

```powershell
uv run --python 3.12 python -m jarvis --data-dir E:\JarvisData create "My chat"
uv run --python 3.12 python -m jarvis --data-dir E:\JarvisData list
uv run --python 3.12 python -m jarvis --data-dir E:\JarvisData send CONVERSATION_ID "Hello"
uv run --python 3.12 python -m jarvis --data-dir E:\JarvisData resume CONVERSATION_ID
```

For the local model, copy [the example profile](profiles/qwen3-4b-instruct-2507.example.toml) to `E:\JarvisData\model.toml` and place its exact pinned files at the paths in that profile. M4 pins llama.cpp b11429 (Windows CUDA 12.4, `llama-server.exe` SHA-256 `7e236243c9b670c6a49410c03186fd575c57a0efe3fd8a21d8727503d571d5b6`) and `bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF` revision `ae44f08e1392f39c0e474af10c3ff8355c8b6688`, Q4_K_M GGUF SHA-256 `2fde00ce69dd4899c70d020845e2638353015bba0fdf161b3eb965f2bca4464e`. Change `device_uuid` and `device_index` only to match the GPU being used. Downloads are explicit setup actions; normal chat works offline. The runtime and model remain outside this repository. See the [llama.cpp releases](https://github.com/ggml-org/llama.cpp/releases/tag/b11429) and [GGUF conversion](https://huggingface.co/bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF/tree/ae44f08e1392f39c0e474af10c3ff8355c8b6688) for the pinned sources.

```powershell
uv run --offline --locked --python 3.12 python -m jarvis --data-dir E:\JarvisData --profile E:\JarvisData\model.toml calibrate
uv run --offline --locked --python 3.12 python -m jarvis --data-dir E:\JarvisData --profile E:\JarvisData\model.toml session CONVERSATION_ID
```

`calibrate` records system-wide free-memory samples about every 250 ms, load time, first-token time, throughput, hashes, and driver version in private `calibration.json`. It checks a long prompt and direct unauthenticated requests before reporting whether the profile bounds passed. It does not change the profile automatically; set `calibrated = true` in the private profile only after a successful run on this hardware. In `session`, type `/unload` to release the model and `/exit` to close the app. A one-shot `send` also accepts `--profile` and unloads before exiting. The model server uses one 4,096-token slot and a per-launch secret.

To enable file reads, create a narrow workspace directory and grant it explicitly. The private data directory itself cannot be a read root. Without `--read-root`, model sessions expose only `current_time`.

```powershell
uv run --offline --locked --python 3.12 python -m jarvis --data-dir E:\JarvisData --profile E:\JarvisData\model.toml --read-root E:\JarvisData\workspaces\default session CONVERSATION_ID
```

Paths must be absolute and remain inside an approved root. Jarvis rejects traversal, reparse points, hard links, alternate data streams, device/UNC paths, private configuration, secrets, and non-regular files. A file read is capped at 16 KiB; a listing at 100 entries. Tool calls run sequentially with a four-call limit per turn. Completed calls and results remain available to later conversation context after restart, including when the final reply was interrupted. `resume` shows each tool's durable status. The approval record and trusted fake UI are exercised in tests; the three initial tools are allowed by policy when their capabilities are enabled, so no approval prompt is presented in the terminal.

`create` prints the conversation ID. `send` prints a submission ID before calling the provider. Reuse it with `send CONVERSATION_ID "Hello" --submission-id SUBMISSION_ID` if a command's outcome is unclear; the same ID and text return the stored outcome without calling the provider again. A different text with that ID is rejected. An interrupted or failed turn remains visible in `resume` and is not silently replayed. Only one application instance may open a data directory at a time.

The fake provider streams an echo with the count of selected messages it received, making restart reconstruction visible. The default budget is 4,096 estimated tokens including 512 for output and 256 for overhead; oversized required input fails explicitly and older exchanges are removed as whole groups. A partial response is kept as a draft and marked interrupted after failure or restart, never shown as a completed reply. Diagnostic metadata is stored in `logs/diagnostics.jsonl` under the chosen data directory; prompts and response text are excluded. The fake provider is for testing behavior, not useful AI responses.

The manager admits at most one resident heavy worker, retains a named per-device lock while warm, and releases it only after the owned process tree exits. It checks the profile's cold peak or warm additional headroom against 1 GiB free VRAM and 3 GiB available RAM reserves. Missing GPU telemetry permits only a previously calibrated profile and is reported as degraded; missing RAM telemetry blocks admission. The Windows supervisor assigns the model process to a kill-on-close Job Object at creation and verifies cleanup.

On this PC (RTX 3060 Ti 8 GiB, driver 576.80), the M4 qualification used a 2,810-token prompt and observed a 3.136 GiB VRAM / 2.947 GiB RAM cold peak, then up to 0.082 GiB VRAM / 0.170 GiB RAM additional demand while warm. Load took 3.315 s, first token 0.993 s, and the 371-token reply averaged 99.87 tokens/s after first token. These are sampled system-wide observations, not guarantees; the conservative profile bounds remain 5/6 GiB cold and 0.5/0.5 GiB warm.

## Checks

```powershell
uv run --offline --locked --python 3.12 pytest
uv run --offline --locked --python 3.12 ruff check .
uv run --offline --locked --python 3.12 ruff format --check .
uv run --offline --locked --python 3.12 mypy src/jarvis
```

The tests use temporary real SQLite databases, fake Windows processes, and recorded HTTP/stream fixtures; they run offline. Hardware calibration and chat are optional separate checks. The M5 live check used `read_text_file`, saved its receipt/result, then answered a follow-up after restart using that result without another file read. See `ARCHITECTURE.md` for the design and later milestones; M1 through M5 are implemented here.
