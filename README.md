# Jarvis (M13)

Jarvis supports durable terminal and local-browser conversations with a deterministic offline fake or one pinned local Qwen3-4B text model. M8 adds one optional local SD 1.5 image tool with verified text/image handoff and durable PNG artifacts. M9 adds one approval-only tool to create a new UTF-8 `.txt` file in an explicitly granted directory. The browser shows chat, history, jobs, progress, exact-action approvals, and locally generated images on `127.0.0.1`. Durable cancellation, explicit recovery, and verified backup/restore remain available. The three initial read-only tools remain current UTC time, bounded directory listing, and bounded UTF-8 file reading. M13 adds optional, approved public-page retrieval. No shell, edit, or delete tool is available.

M10 adds optional push-to-talk transcription. A short microphone recording becomes editable text in the browser composer; it is never sent or treated as a tool command until the user explicitly submits it. The pinned faster-whisper base INT8 worker runs on CPU in a separate environment. See [speech worker setup](docs/SPEECH_WORKER.md) and add `--speech-profile E:\JarvisData\speech.toml` before `web` to enable Record.

M11 adds optional local spoken playback for completed assistant replies. A **Play voice** control reads the saved text through one pinned CPU Piper voice; **Stop voice** interrupts playback, and synthesis failure leaves the text available. See [Piper voice setup](docs/PIPER_WORKER.md) and add `--piper-profile E:\JarvisData\piper.toml` before `web` to enable it. Voice never authorizes a tool action.

M12 adds one explicit screenshot upload and question in the browser. Add `--vision-profile E:\JarvisData\vision.toml` beside the text `--profile` to enable the preview and local vision model. The screenshot is saved with its question and answer as a private artifact and included in backups; M12 provides no per-screenshot deletion control. See [vision setup and limits](docs/VISION_WORKER.md). The model does not control the screen or authorize tools.

M13 adds `--enable-network` for a model-backed `web` session. The browser asks for a fresh approval showing the exact public HTTP(S) URL, including its path and query, before each GET. Check that URL for private data before approving: the whole path and query are sent to the site. Retrieval accepts HTML or plain text, extracts bounded text as untrusted evidence, and never follows redirects or loads images/subresources. Local/private/metadata destinations, URL credentials, uploads, cookies, nonstandard ports, and search/browser tools are unavailable. Ordinary chat remains offline without the flag.

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
uv run --python 3.12 python -m jarvis --data-dir E:\JarvisData jobs
uv run --python 3.12 python -m jarvis --data-dir E:\JarvisData status JOB_ID
uv run --python 3.12 python -m jarvis --data-dir E:\JarvisData cancel JOB_ID
uv run --python 3.12 python -m jarvis --data-dir E:\JarvisData resume-job JOB_ID
uv run --python 3.12 python -m jarvis --data-dir E:\JarvisData diagnostics
uv run --python 3.12 python -m jarvis --data-dir E:\JarvisData backup
uv run --python 3.12 python -m jarvis --data-dir E:\RestoredJarvisData restore E:\JarvisData\backups\BACKUP_ID
uv run --python 3.12 python -m jarvis --data-dir E:\JarvisData web
```

For the local model, copy [the example profile](profiles/qwen3-4b-instruct-2507.example.toml) to `E:\JarvisData\model.toml` and place its exact pinned files at the paths in that profile. M4 pins llama.cpp b11429 (Windows CUDA 12.4, `llama-server.exe` SHA-256 `7e236243c9b670c6a49410c03186fd575c57a0efe3fd8a21d8727503d571d5b6`) and `bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF` revision `ae44f08e1392f39c0e474af10c3ff8355c8b6688`, Q4_K_M GGUF SHA-256 `2fde00ce69dd4899c70d020845e2638353015bba0fdf161b3eb965f2bca4464e`. Change `device_uuid` and `device_index` only to match the GPU being used. Downloads are explicit setup actions; normal chat works offline. The runtime and model remain outside this repository. See the [llama.cpp releases](https://github.com/ggml-org/llama.cpp/releases/tag/b11429) and [GGUF conversion](https://huggingface.co/bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF/tree/ae44f08e1392f39c0e474af10c3ff8355c8b6688) for the pinned sources.

For image generation, install the separately pinned ComfyUI environment and FP16 SD 1.5 checkpoint as described in [the M8 worker setup](docs/IMAGE_WORKER.md). Copy [the image profile](profiles/sd15-512.example.toml) to `E:\JarvisData\image.toml`, set the same GPU identity as the text profile, and add `--image-profile E:\JarvisData\image.toml` alongside `--profile` for `send`, `session`, `resume-job`, or `web`. The tool is exposed only when both profiles are supplied. It generates at most one 512×512 image per turn with a fixed workflow; no other image graph or model is available.

```powershell
uv run --offline --locked --python 3.12 python -m jarvis --data-dir E:\JarvisData --profile E:\JarvisData\model.toml calibrate
uv run --offline --locked --python 3.12 python -m jarvis --data-dir E:\JarvisData --profile E:\JarvisData\model.toml session CONVERSATION_ID
```

`calibrate` records system-wide free-memory samples about every 250 ms, load time, first-token time, throughput, hashes, and driver version in private `calibration.json`. It checks a long prompt and direct unauthenticated requests before reporting whether the profile bounds passed. It does not change the profile automatically; set `calibrated = true` in the private profile only after a successful run on this hardware. In `session`, type `/jobs`, `/status JOB_ID`, `/cancel JOB_ID`, `/resume-job JOB_ID`, `/unload`, or `/exit`. A one-shot `send` also accepts `--profile` and unloads before exiting. The model server uses one 4,096-token slot and a per-launch secret.

For a model-backed browser session, add `--profile E:\JarvisData\model.toml` before `web`; add narrow `--read-root` grants in the same way as a terminal session. `web` binds only to `127.0.0.1` on port 8765 by default (`--port` selects another local port). Open the printed `/setup` address and enter the one-time token printed in the terminal. The token is exchanged once for a browser-session cookie; keep the server running while using the page. A second tab in that browser can view or cancel the same job. Closing a tab leaves active work running; stopping the server waits for active jobs to settle.

To enable M9 file creation in the browser, add `--write-root E:\YourApprovedFolder` to the model-backed `web` command. The directory must already exist on a local fixed drive and must not include Jarvis private state or installed source. The tool creates only a new `.txt` file directly or inside an existing subdirectory under that grant, with at most 8 KiB of UTF-8 content. The browser shows the exact destination and full content before a fresh, single-use approval. Existing files are never overwritten. If a crash leaves the result uncertain, use the browser's **Verify file** control or `uv run --offline --locked --python 3.12 python -m jarvis --data-dir E:\JarvisData --write-root E:\YourApprovedFolder reconcile-file INVOCATION_ID`. Verification checks the original parent identity, path, and content; it never repeats the write. See [M9 file creation](docs/FILE_CREATION.md).

The page renders assistant and tool text literally, without Markdown previews or external images. Its restrictive Content Security Policy blocks external resource loads. Locally generated PNGs appear through a verified, authenticated same-origin preview route and can be downloaded. Mutations require the session cookie, same-origin requests, and a CSRF token. Artifact downloads require the same session and serve only verified stored artifacts as attachments. Approval cards display the tool, canonical arguments, target, policy, and owning job before the user approves or rejects. Current default read-only tools normally do not require approval.

To enable file reads, create a narrow workspace directory and grant it explicitly. The private data directory itself cannot be a read root. Without `--read-root` or `--enable-network`, model sessions expose only `current_time`.

```powershell
uv run --offline --locked --python 3.12 python -m jarvis --data-dir E:\JarvisData --profile E:\JarvisData\model.toml --read-root E:\JarvisData\workspaces\default session CONVERSATION_ID
```

Paths must be absolute and remain inside an approved root. Jarvis rejects traversal, reparse points, hard links, alternate data streams, device/UNC paths, private configuration, secrets, and non-regular files. A file read is capped at 16 KiB; a listing at 100 entries. Tool calls run sequentially with a four-call limit per turn. Completed calls and results remain available to later conversation context after restart, including when the final reply was interrupted. `resume` shows each tool's durable status. The approval record and trusted fake UI are exercised in tests; the three initial tools are allowed by policy when their capabilities are enabled, so no approval prompt is presented in the terminal.

`create` prints the conversation ID. `send` prints a submission ID before calling the provider. Reuse it with `send CONVERSATION_ID "Hello" --submission-id SUBMISSION_ID` if a command's outcome is unclear; the same ID and text return the stored outcome without calling the provider again. A different text with that ID is rejected. An interrupted or failed turn remains visible in `resume` and is not silently replayed. Only one application instance may open a data directory at a time.

Every new send atomically stores the user turn and a queued job. The runner executes one job at a time and keeps at most 16 queued jobs. A restart retains queued jobs and marks unfinished running jobs interrupted; neither resumes automatically. Use `resume-job` explicitly, with the original `--profile` and `--read-root` settings for a model job. Three attempts are allowed for safe unfinished computation. Unknown tool effects block replay and further turns in that conversation. Cancellation is recorded before cleanup; a real-model cancellation remains pending after a crash until the prior worker tree can be verified gone with the original profile. Completed tool results remain visible even if the surrounding job is cancelled. Closing an interactive session waits for active work; request `/cancel JOB_ID` first when you want it stopped.

`backup` creates a SQLite snapshot and an artifact manifest under the private `backups` directory. `restore` requires an empty destination and verifies the snapshot and every referenced artifact without needing the original data directory. Model binaries and caches are not included; keep their pinned copies separately. Incomplete backups remain marked as incomplete for inspection. `diagnostics` reports queue state, recent normalized failures, memory availability, and configured model hashes without prompt or file contents. Image files are staged, flushed, verified, and atomically renamed before their SQLite result is committed; any unreferenced file after a crash remains for inspection rather than being replayed.

The fake provider streams an echo with the count of selected messages it received, making restart reconstruction visible. The default budget is 4,096 estimated tokens including 512 for output and 256 for overhead; oversized required input fails explicitly and older exchanges are removed as whole groups. A partial response is kept as a draft and marked interrupted after failure or restart, never shown as a completed reply. Diagnostic metadata is stored in `logs/diagnostics.jsonl` under the chosen data directory; prompts and response text are excluded. The fake provider is for testing behavior, not useful AI responses.

The manager admits at most one resident heavy worker, retains a named per-device lock while warm, and releases it only after the owned process tree exits. It checks the profile's cold peak or warm additional headroom against 1 GiB free VRAM and 3 GiB available RAM reserves. Missing GPU telemetry permits only a previously calibrated profile and is reported as degraded; missing RAM telemetry blocks admission. The Windows supervisor assigns the model process to a kill-on-close Job Object at creation and verifies cleanup.

On this PC (RTX 3060 Ti 8 GiB, driver 576.80), the M4 qualification used a 2,810-token prompt and observed a 3.136 GiB VRAM / 2.947 GiB RAM cold peak, then up to 0.082 GiB VRAM / 0.170 GiB RAM additional demand while warm. Load took 3.315 s, first token 0.993 s, and the 371-token reply averaged 99.87 tokens/s after first token. These are sampled system-wide observations, not guarantees; the conservative profile bounds remain 5/6 GiB cold and 0.5/0.5 GiB warm.

For M6, ten successive real load/chat/unload cycles completed through durable jobs on this PC. After cycles 2–10, free VRAM ranged from 7,039 to 7,082 MiB and zero `llama-server.exe` processes remained. Available system RAM was 10.24 GiB before the repeated cycles and 10.41 GiB afterward; other desktop activity can affect these system-wide samples. The offline suite also repeats fake text/image/text handoffs and injects OOM rather than deliberately exhausting the GPU.

## Checks

```powershell
uv run --offline --locked --python 3.12 pytest
uv run --offline --locked --python 3.12 ruff check .
uv run --offline --locked --python 3.12 ruff format --check .
uv run --offline --locked --python 3.12 mypy src/jarvis
```

The tests use temporary real SQLite databases, fake Windows processes, recorded HTTP/stream fixtures, and local ASGI requests; they run offline. M8 also checks the fixed image workflow, artifact crash windows, cancellation, and fake text/image/text handoff. The M5 live check used `read_text_file`, saved its receipt/result, then answered a follow-up after restart using that result without another file read. See `ARCHITECTURE.md` for the design and later milestones.
