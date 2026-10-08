# Desktop application verification — 2026-10-08

The user requested a dedicated, lightweight application and a visual refresh. This supersedes the previous controller-only UI restriction in AGENTS.md. M1–M15 inference, retrieval, permissions and persistence scope remains unchanged.

## Implemented

- The existing desktop and Start menu shortcuts open a dedicated WebView2 window, with automatic per-launch sign-in. Python is an internal dependency; no terminal interaction is needed.
- The existing Tk controller is visible during startup or failures and hidden when the application window is ready. The window requests server shutdown on close and remains present until cleanup completes.
- The host starts under the existing Windows process supervisor, assigned at creation to a kill-on-close job. Its WebView2 descendants use a unique private profile and inherit the job without breakaway. The setup token goes through stdin. Failed exit verification retains the owned process and its directory for another cleanup attempt.
- All chat operations use the original session/CSRF-protected API. Page navigation is restricted to the launch origin. No custom Python bridge or extra inference capability is exposed.
- Dark layout, Russian main controls, keyboard sending, compact screenshot attachment, history, and a collapsible attention panel. New approvals and uncertain actions open the panel and remain counted when it is closed. New conversations are created on first send and titled from the message, avoiding empty history entries.
- Both installed shortcuts now use the Jarvis icon. Existing profile selection and private data directory are preserved.

## Checks performed

- Offline Python suite: **296 passed, 3 skipped** by existing environment conditions.
- JavaScript suite: **10 passed** (authentication, durable retry identity, first-message creation, new-conversation selection, attention visibility, keyboard behavior, audio lifecycle).
- Ruff checks and formatting; mypy for all 60 source files: passed.
- Real Windows/WebView2 window, temporary SQLite database, deterministic fake provider: automatic login, message submission with Ctrl+Enter, visible saved reply, attention panel, close, restart, restored history, and repeated launch focusing the same window without new child processes.
- After the final close, all **9 recorded process identities** (PID plus creation time) had exited, and the test server's loopback port was closed. No user data was reset or deleted, and no heavy model was loaded for these tests.

## Resource sample and limits

At idle, a five-second sample of the fake-provider server, desktop host and its descendants measured approximately **295 MiB private memory**, **519 MiB summed working sets**, and **0.62% of one logical CPU core**. Working-set sums include shared pages and are not the same as private allocation. These are one-machine samples, not upper bounds. Real model residency and other activity add resource usage; the existing resource admission checks remain authoritative.

This uses installed Microsoft Edge WebView2 and pinned pywebview 6.1. It does not bundle Electron or a second browser distribution. It still requires this project's installed Python environment and the configured models. Missing WebView2 causes an explicit startup failure rather than a fallback to an obsolete browser engine. Microphone hardware, actual voice synthesis, image generation and real GPU inference were not requalified as part of the shell change; their existing offline suites passed.

Normal startup: double-click **Jarvis** on the desktop or in Start. Normal shutdown: close its window and let cleanup finish. Launching the explicit terminal `web` command still serves the same UI in a regular browser; closing only that browser tab does not stop a terminal-launched server.

## Core feature repairs — 2026-10-09

- Enter sends; Shift+Enter inserts a newline. IME composition does not submit prematurely.
- The composer accepts clipboard images and dropped files, with local PNG normalization, bounded dimensions/size, preview and removal. Text paste keeps its native behavior. A conversion finishing after conversation selection changes cannot attach to the new conversation.
- Fully committed legacy assistant replies without an M6 job record can be spoken. Existing checks still reject incomplete/failed turns, wrong messages and non-successful existing jobs.
- Shortcut installation repairs the physical interpreter homes of the explicitly configured image, speech and Piper worker environments. On this machine all three had the same Store AppData redirection problem as the core environment. Only their `pyvenv.cfg` home entries were changed.
- Image admission failures retain their durable no-effect receipt and now include available/required memory when known. The UI explains the failure instead of labeling the failed result as successful. Missing telemetry remains unknown.

Validation: **299 Python tests passed, 3 skipped** in the offline suite; **14 JavaScript tests passed**. Ruff checks/format, mypy (60 source files), and whitespace checks passed. The Piper hardware test passed with the installed voice. In a native WebView2 window with a temporary database, a completed legacy answer played through the audio lifecycle to “Озвучивание завершено”. This verifies synthesis and player completion, not an acoustic recording. The installed voice is English (`en_US-norman-medium`); Russian voice quality is not qualified and no new voice model was added.

Real GPU checks used temporary databases and the existing pinned models. Text → image → text completed with no remaining owned runtime processes. Three further cold SD1.5 512×512 generations each produced a valid PNG in about 13.4 seconds. [Measured free-memory deltas](verification/image-cold-peaks-20261009.json) peaked at 2.902 GiB VRAM and 2.316 GiB RAM; these system-wide samples can miss transients. A final native-window request submitted with Enter invoked the image tool, displayed its generated cat image and completed the assistant reply.

Machine-local settings in `E:\JarvisData` were adjusted with margin above measurements: image cold VRAM/RAM from 5.5/6 to 4/4 GiB, text cold RAM from 6 to 4 GiB using its existing calibration (2.947 GiB measured peak). Text VRAM remains 5 GiB. System reserves remain **1 GiB VRAM and 3 GiB RAM**. Image `calibrated=false` remains because warm/missing-telemetry admission was not qualified. Original files are retained as `image.toml.before-feature-fix-20261009` and `model.toml.before-feature-fix-20261009`. Repository example bounds remain conservative for other machines.

In that same native window, Alt+PrintScreen followed by Ctrl+V inserted the test window image, decoded it, reduced it to 1024×679 and displayed its preview as ready to send. Shift+Enter inserted a newline without submitting. File-drop dispatch, image normalization/submission and removal are covered by JavaScript tests; an Explorer-to-window drag was not manually exercised.

The separate real vision fixture suite was attempted but refused before model loading: available RAM was 8.705 GiB against the unchanged 9 GiB admission requirement. Its semantic recognition checks therefore did not pass in this run. This does not test the same path as clipboard decoding; no vision bounds were relaxed to bypass that refusal.

## Repeated image repair — 2026-10-09

The two user-reported cat artifacts had identical stored arguments (`a cute cat`, seed `123`) and the same SHA-256. The text model had selected a canned fixed seed for both requests. Missing seeds also previously defaulted to zero.

The model-facing image schema now accepts only the prompt. For a seedless invocation, the image service derives a bounded 31-bit seed from its committed random invocation ID. This provides a new variation for a new request and a stable seed for the same durable identity, without changing the deterministic fake provider or adding dependencies. The effective seed is recorded in both result and receipt. Explicit seeds in legacy stored invocations remain supported; no historical arguments or artifacts are rewritten.

The offline suite passed **300 tests with 3 existing skips**, plus all **14 JavaScript tests**. Ruff formatting/checks and mypy passed. The new regression covers two identical requests, distinct default seeds, duplicate-submission reuse without another provider call, saved seed metadata, and reopening SQLite. Existing fixed-seed workflow and persistence tests still pass.

With the real Qwen/SD1.5 runtimes and temporary SQLite databases, two successive “Сгенерируй фото кота” requests produced the same prompt and different seeds/images. This passed both in a fresh conversation and with a completed legacy seed-123 tool call in its history. All four jobs succeeded and the full owned runtime trees exited. [Verification data](verification/image-variations-20261009.json) includes hashes of the files and their decompressed PNG pixel data, confirming the difference is not just metadata.
