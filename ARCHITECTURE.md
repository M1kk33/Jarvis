# Jarvis: practical local-first architecture

Design date: 5 October 2026. Target: Windows PC, RTX 3060 Ti with 8 GB VRAM, Ryzen 5 5600X, 16 GB RAM, NVMe SSD. M1–M15 are implemented. Memory figures below are planning estimates and admission targets; measured results are recorded in the verification documents. The legacy desktop shell is described in [desktop verification](docs/DESKTOP_APP.md); the first installed C# + WebView2 preview is described in [native desktop verification](docs/NATIVE_DESKTOP.md). The [desktop architecture](docs/DESKTOP_ARCHITECTURE.md) records the full-release direction and outstanding requirements.

Product direction, updated 9 October 2026: build a distributable Windows application with a thin C#/.NET + WebView2 host and the existing Python application core. The user's first priority is a convenient modern out-of-box application, improving the current interface and preserving all currently working features. Optimize where useful and fix measurable regressions; do not require a minimum-hardware specification, arbitrary overhead limit or a new design approval before implementation. Validate primarily on the user's current PC and test installation in a clean environment. Continue fixes and functional development after the first release. See [the product requirements](docs/DESKTOP_ARCHITECTURE_REVIEW.md#product-requirements). These desktop requirements supersede the historical terminal-first/browser-only rollout below, while preserving the core ownership, persistence, resource and process invariants. The original PC above is a qualification machine, not a declared minimum supported configuration.

## 1. Recommended high-level architecture

Build a **single Python application with a durable SQLite store, a small sequential task runner, and application-owned model subprocesses**. Start with a terminal interface. Add a local browser interface after the core works.

Keep at most **one heavy model process resident at a time**, including while it is idle. A central resource manager owns that process and all transitions between models. Small CPU speech workers come later and must also pass RAM admission checks.

Use llama.cpp for text inference, initially with Qwen3-4B-Instruct-2507 in a verified Q4_K_M GGUF conversion and a 4,096-token context. Its upstream model is a 4B non-thinking instruction model; this is a conservative starting choice, not a claim that it is the best available model. Pin and test the exact conversion, tokenizer, chat template, and runtime together. [Qwen model card](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507)

The application owns conversation history, context selection, permissions, jobs, and artifacts. Models produce text or proposed actions. They do not own application state or execute operating-system actions.

The key invariant is: **stopping every model process must leave a usable application, intact conversations, and inspectable task state**. Reopening a model rebuilds its prompt from stored records. There is no requirement to preserve CUDA allocations, model sessions, or KV caches.

One logical assistant loop is enough. No agent framework, multiple autonomous agents, service mesh, message broker, or distributed scheduler is needed.

## 2. ASCII architecture diagram

```text
 User
  |
  +--> Terminal UI (MVP)
  +--> Local browser UI (later; loopback + session authentication)
                 |
 +---------------v------- ONE PYTHON APP PROCESS ----------------+
 | UI adapters -> Application services                          |
 |                 |                                            |
 |                 +-- Conversation/context builder             |
 |                 +-- Durable jobs + bounded assistant loop     |
 |                 +-- Tool validation/policy/approval           |
 |                 +-- Resource manager                         |
 |                        |                                     |
 |             Backend adapters + Process supervisor            |
 |                                                              |
 | SQLite store       Artifact store       Structured logs       |
 +-------|------------------|---------------------|---------------+
         v                  v                     v
      jarvis.db         files on NVMe          JSONL files

       Resource manager: ONE heavy resident slot
                          |
             +------------+------------+
             |            |            |        mutually exclusive
       llama-server    ComfyUI     llama-server
        text model    image model   vision model
             application-owned child processes

 Later, budgeted CPU-only speech workers: faster-whisper / Piper
 Approved tools -> bounded local file operations / network / browser
                  (external browser is started only when needed)
```

Arrows show calls and ownership. In-process calls, callbacks for progress, SQLite rows, and a small in-memory wakeup mechanism are sufficient. The database is authoritative; a progress callback is not a durable event system.

## 3. Process boundaries

| Boundary | Decision and reason |
|---|---|
| Application | One process, one application instance per data directory, one async task runner. Keeps coordination simple. |
| Text/vision inference | Dedicated llama-server child. Native/CUDA crashes cannot directly corrupt Python state; exit provides a strong resource cleanup boundary. |
| Image inference | Dedicated, on-demand ComfyUI child with its own environment. Isolates PyTorch dependencies and releases CPU/GPU allocations when stopped. |
| Speech, later | CPU workers with a small versioned JSON-lines protocol over pipes, using file references for audio. Supports cancellation without importing model runtimes into the core. |
| Tools | Small trusted file operations can run in-process. Blocking native tools and browsers use supervised children. A separate process is crash isolation, not a security sandbox. |
| UI and desktop host | Current static UI is served by FastAPI inside the Python app process. The distributable product adds a thin C#/.NET + WebView2 host for the window, system integration and backend lifecycle; it does not introduce a second business server or database owner. The ordinary browser remains a development interface. |

Model servers speak their existing local HTTP protocols. Do not write a universal model-worker RPC server around them. Jarvis launches, configures, and stops its own copies; it does not depend on a user's independently running inference server.

Bind model HTTP endpoints to loopback and authenticate them with a random per-launch secret. Use llama-server's native API-key support. For the later ComfyUI integration, include a small request-authentication middleware in the app-owned launcher, covering HTTP and WebSocket routes before handlers run. A proxy in front of an otherwise accessible unauthenticated port is insufficient. Keep these secrets out of model context, browser JavaScript, URLs, and logs. Test direct unauthorized requests as well as calls through Jarvis; loopback binding alone is not authentication.

Disable backend-provided agent/tools/MCP execution, built-in web UI, automatic model routing, and independent idle-sleep policies. Jarvis owns those responsibilities. Restrict CORS explicitly. A runtime's minimal public health endpoint may remain read-only; inference and state-changing routes require authentication. Verify the actual exposed routes in the pinned build. [llama-server security and configuration](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)

On supported Windows versions, the process supervisor uses Job Objects with kill-on-close and no breakaway, assigning the child at process creation with `PROC_THREAD_ATTRIBUTE_JOB_LIST`; fail cleanly if assignment cannot be established. Creating a suspended child and attaching it afterwards leaves a parent-crash window with an unowned suspended process. The app alone holds the non-inherited lifetime handle. [Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects), [assignment at process creation](https://devblogs.microsoft.com/oldnewthing/20230209-00/?p=107812)

Use hidden child windows, argument arrays, explicit executable paths, bounded stdout/stderr readers, private working directories, and a minimal environment. Never kill unrelated processes by executable name. Native Windows is the initial deployment target; Docker and WSL are unnecessary here.

## 4. Main components and dependency direction

| Component | Owns | Does not own |
|---|---|---|
| Conversation service | Turns, message order, append transactions, per-conversation serialization | Model sessions |
| Context builder | Token budget, valid historical message/tool groups, attachment excerpts | Permission decisions |
| Assistant runner | Bounded plan/validate/execute/respond loop | Shell access or backend-specific payloads |
| Job runner | Queue, step checkpoints, cancellation, restart reconciliation | GPU loading policy |
| Resource manager | Resident slot, admission, switch policy, idle unloading | Conversation content |
| Process supervisor | Child lifecycle, readiness, deadlines, process-tree termination | Which user actions are allowed |
| Provider adapters | Protocol translation, streaming normalization, backend error mapping | SQLite writes or tool execution |
| Tool service | Registry, validation, policy, approvals, execution receipts | Model inference |
| Store | Transactions, migrations, queries, artifact metadata | Business decisions |
| UI adapters | Display, user input, approvals, progress | Direct tool or model calls |

Dependency rule:

```text
interfaces -> application -> domain
infrastructure -----------> domain contracts
bootstrap imports and wires all concrete implementations
```

Use ordinary modules and constructor injection. Domain types and the few needed Protocols have no UI, HTTP, SQLite, or inference imports. Pydantic validates external inputs at boundaries; ordinary dataclasses/enums are adequate internally. A single concrete SQLite store is sufficient; do not create a repository class for every table.

Keep resource scheduling and job scheduling separate responsibilities, but initially each is one small module. Add interfaces only for actual replaceable boundaries or useful test seams: inference, managed runtime, resource probe, clock, process control, and tool handlers. Do not build a dependency-injection framework.

## 5. Resource management for 8 GB VRAM and 16 GB RAM

### Starting profiles

These are estimates to validate with cold loads, near-limit inputs, and repeated switches. Profile limits are configuration bounds enforced before dispatch.

| Workload | Initial bound | Estimated worker VRAM | Estimated incremental RAM peak |
|---|---|---:|---:|
| Text LLM | 4B Q4_K_M, context 4,096 total, one request, max output 512 tokens | 3-5 GiB | 3-6 GiB |
| Image, later | SD 1.5, FP16, one 512x512 image, batch 1, fixed workflow | 3-5.5 GiB | 4-7 GiB |
| Vision, later | Qwen2.5-VL-3B Q4 with matching projector, one bounded image | 3-6 GiB | 4-7 GiB |
| Speech recognition, later | faster-whisper base, CPU INT8, short clips, no batching | 0 | approximately 0.5-1.5 GiB |
| Speech synthesis, later | One small Piper voice, CPU, short text chunks | 0 | approximately 0.2-0.8 GiB |
| Jarvis core | No AI libraries; bounded streams and buffers | 0 | target below 300 MiB idle |

Image support starts with an SD 1.5 workflow because its native training resolution was 512x512, a useful conservative baseline here. [SD 1.5 model card](https://huggingface.co/stable-diffusion-v1-5/stable-diffusion-v1-5)

### Admission and scheduling rules

1. Allow one heavy resident backend and one active model request. A warm idle model still owns the resident slot. Real heavy runtimes also require a named Windows GPU-ownership lock keyed by device, independent of data directory, so separate Jarvis instances cannot load concurrently. Hold it from before admission/loading through confirmed process-tree cleanup, including idle and faulted residency. Each new owner verifies previous owned-worker cleanup before loading, including after an abandoned lock; uncertain cleanup blocks admission. This is separate from the per-data-directory instance lock and does not reserve memory against unrelated applications. Ordinary fake-backend tests need no real GPU lock; lifecycle tests exercise it with a test device key. No speculative loading, overlapping loads, or background embeddings on GPU.
2. Set an initial maximum measured worker GPU peak of 6 GiB. Before loading, require available VRAM minus the profiled peak to leave at least 1 GiB free. This accounts for actual display/browser usage; an unrelated GPU workload can prevent admission.
3. Aim to keep Jarvis's total incremental RAM below 8 GiB. Require projected system available RAM to remain at least 3 GiB at peak. Measure the whole child tree and loading transients; do not add overlapping RSS and shared mmap pages as if all were independent.
4. Stop CPU speech workers and indexing before a heavy model switch if their memory is needed. Do not use system RAM as a parking area for several inactive models. Load from NVMe again.
5. Keep the system-managed pagefile as crash protection, not as model capacity. Paging and Windows shared GPU memory are not acceptable substitutes for a profile that fits.
6. Use a bounded FIFO job queue, initially 16 pending jobs. Execute one assistant/model step at a time. Long jobs yield between durable steps; no preemption during a GPU kernel. Status, cancellation, and approval input remain responsive.
7. Reuse the warm text backend for adjacent requests. Unload after a configurable 120-second idle period, on memory pressure, before a different heavy profile, or on explicit release. Image/vision workers exit after their job.
8. Limit CPU inference threads initially to four; do not run simultaneous speech/indexing jobs. Tune on this CPU only after measuring responsiveness.

Measure cold-load peaks relative to the worker being absent, and record warm residency plus additional worst-case request headroom separately. Cold admission uses the full incremental peak; warm reuse rechecks current availability against additional headroom, without subtracting the already-resident footprint twice. Include all admitted CPU/browser workers in aggregate RAM projections without double-counting memory already reflected in current availability. For example, a 7 GiB incremental RAM peak requires at least 10 GiB available before loading to retain the 3 GiB reserve; an ordinary desktop workload may prevent admission.

Use psutil for system/process RAM and NVIDIA telemetry via an optional NVML adapter; expose unavailable metrics honestly. On Windows, per-process GPU readings may be incomplete. Total free memory, owned-process state, measured profiles, and OOM recovery matter more than exact attribution. Telemetry is advisory, never a reservation against other applications.

If GPU telemetry is unavailable, permit only previously calibrated conservative profiles in a clearly reported degraded mode. Do not guess whether an unfamiliar model fits. A calibration command should sample at roughly 250 ms intervals and log peaks, load time, first-token time, throughput, and post-exit free memory, recognizing sampling can miss brief peaks.

Explicitly configure llama.cpp context, one slot, batch sizes, and zero application-unneeded RAM prompt cache; do not inherit model-sized context or large server cache defaults. Start with conservative batch/microbatch sizes, such as 256/128, and FP16 KV. Test Q8 KV only if useful later. llama.cpp exposes these controls, but their names/defaults must be checked against the pinned binary. [llama-server configuration](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)

A 7B/8B Q4 model may fit with a short context, but it is not the MVP baseline. Validate larger models as separate profiles. Do not promise a fixed response speed before measuring this PC.

## 6. Model loading, unloading, and recovery

The resource manager owns one state machine:

```text
STOPPED -> STARTING -> READY <-> BUSY -> STOPPING -> STOPPED
               \        \        \         \
                +--------+--------+-------> FAULTED
FAULTED -> supervised cleanup -> STOPPED or UNAVAILABLE
```

Only READY can accept a request. STARTING includes readiness checks and a tiny warmup; port availability alone is insufficient. FAULTED still occupies the slot until cleanup succeeds. UNAVAILABLE blocks new heavy work pending an explicit health check or user intervention.

An operation handle gives one request temporary use of the resident backend. Releasing that handle does **not** mean the model is unloaded or the GPU is free. Residency and active-request ownership are distinct. There are no nested GPU acquisitions.

Resource transitions have one serialized owner. Readiness, exit, idle-timer, and request callbacks carry runtime generation/request identity; stale callbacks cannot change a replacement runtime, release its slot, or stop a busy request. Cancellation of a caller or waiter does not cancel supervisor cleanup: retain ownership until exit is confirmed or mark the slot unavailable. Use the existing manager/supervisor modules, not another scheduler.

### Text -> image -> text example

1. Atomically commit the user message, turn, and job before inference.
2. Run text inference; assemble and validate the complete proposed image request. Commit the assistant tool call and normalized arguments. Never execute partially streamed arguments.
3. Finish the text request and release its operation handle. End any DB transaction. A tool handler must not wait for image inference while still holding the text handle.
4. Mark the durable next step as image generation. Stop llama-server, await the entire owned process tree's exit, and check resource admission again.
5. Start the pinned ComfyUI profile. Execute an application-owned workflow with bounded prompt, seed, dimensions, and step count.
6. Save the output to a staging file, finalize the artifact, and atomically record artifact metadata, tool receipt/conversation result, and next step. Stop ComfyUI and verify exit before reusing the slot.
7. If a textual follow-up is useful, reload the text model and rebuild context from SQLite, including the tool result. Otherwise return the artifact directly and leave the GPU unloaded.

“Restore the conversation” means reconstructing semantic context, not resuming a token stream or restoring the exact model state. KV cache persistence is an optional future optimization, never a correctness dependency.

### Failure policy

| Failure | Required behavior |
|---|---|
| CUDA OOM / native crash | Record normalized failure; stop/reap the child tree; retain committed conversation and completed tool results. Do not continue using the damaged runtime. |
| Load timeout | Stop the child tree; show the loading stage and log reference. Initial deadline 180 seconds, profile configurable. |
| Request timeout / stalled stream | Distinguish total deadline and no-progress timeout. Cancel, then terminate if needed. Long image work has a profile-specific deadline. |
| Failed unload | Request graceful stop, wait 5 seconds, terminate tree, wait up to 10 seconds. If exit is not confirmed, block the slot; never load the next model. |
| Backend gone but GPU memory remains unavailable | Bounded recheck, then mark resources unavailable. Driver failures may require user intervention; never reboot or reset drivers automatically. |
| Repeated failure | At most one automatic retry of a safe step using a prevalidated lower-memory profile. Then fail visibly; no restart loop. |

A fallback may lower text context/batch size only after rebuilding a valid prompt budget; it must preserve required instructions and tool-result relationships. Do not silently switch to CPU, cloud, a new model, or different requested image dimensions. If no suitable profile fits, stop with an actionable explanation.

Retries happen at the failed inference step, not by replaying an entire turn. A response already streamed to the user is marked interrupted before a replacement attempt is shown. Side-effecting tools have stricter recovery rules below.

## 7. Conversation and context persistence

SQLite is the source of truth. Persist messages in order with conversation ID, turn ID, sequence number, role, typed content parts, tool-call IDs, timestamps, completion status, and model/profile metadata. Use stable IDs and uniqueness constraints to prevent duplicate append operations.

Commit user input before invoking a backend. Persist complete assistant tool calls before execution and tool results before another model call. Stream output to the UI, but save a bounded draft periodically, for example once a second or every 2 KiB. Only a completed response is a normal assistant message. After a crash, label the last stored draft interrupted; the uncheckpointed tail may be lost, and it is never treated as a completed answer.

Conversation storage is not the context window. Keep the full transcript while selecting a bounded prompt:

```text
system instructions + enabled tool schemas + selected history
  + bounded attachments/tool results + output allowance + safety margin
  <= configured context size
```

Start at 4,096 tokens with 512 reserved for output and 256 for estimation/template overhead. Count the rendered prompt with the adapter's tokenizer/template support when available. Use a conservative estimate and final backend validation otherwise. Preserve the current user turn and complete tool-call/result groups; trim older complete turns first. Oversized mandatory content is rejected or explicitly excerpted, never silently discarded.

An interrupted/failed/cancelled turn can contain completed tool effects. When selecting that turn, preserve relevant completed results rather than dropping them solely because its final reply is incomplete. Exclude unfinished assistant prose. For unresolved calls, build valid tool-call/result groups using explicit application-generated cancelled/error/`outcome_unknown` status from durable records; these context records are not fabricated execution receipts. Unknown means the action may have happened, not that it failed or did nothing. Context reconstruction never executes a tool. Before tools exist, preserve incomplete user input with its turn status without presenting it as an answered exchange.

A large file is not automatically a large prompt: store it as an artifact and provide a bounded excerpt. If a required tool exchange cannot fit, stop and offer a narrower request. Do not let server context shifting silently decide application history.

MVP uses recent turns only. Add summaries only after demonstrating a real context problem. A summary stores source message ranges and is explicitly derived, fallible data. Raw messages remain available and summaries never become system instructions.

Explicit user preferences are separate from historical chat. Later memory has provenance, inspect/edit/delete controls, and scope. Retrieved documents and memories remain untrusted context. Begin with SQLite FTS5 over approved material; add CPU embeddings only if retrieval evaluation shows a benefit. No always-running indexer or vector database.

## 8. Tool architecture and security model

Treat every proposed action as untrusted input, including action-like content from webpages, files, screenshots, and past model messages. Prompt instructions are helpful behavior guidance, not the enforcement boundary.

Each statically registered tool has a name/version, strict input schema, bounded output schema, risk category, required capability, timeout, maximum output size, and retry/side-effect declaration. Initial tools: current time, list a permitted directory, and read a bounded text file. The model cannot install tools or alter the registry.

```text
complete proposal -> strict schema -> normalize -> policy decision
 -> user approval if required -> revalidate target/preconditions
 -> execute bounded operation -> persist receipt/result -> model context
```

Unknown fields, coercions, unsupported names, duplicate JSON keys, oversized payloads, and invalid paths fail closed. Structured generation reduces malformed calls; application validation remains mandatory. llama.cpp tool parsing is model/template dependent, so validate the exact model/runtime combination with contract fixtures. [Function-calling documentation](https://github.com/ggml-org/llama.cpp/blob/master/docs/function-calling.md)

### Permission policy

| Action | Initial policy |
|---|---|
| Clock / application status | Allow without confirmation. |
| List/read | Allow only inside user-configured read roots, with size and type limits. Exclude credentials, secrets, app configuration, and private runtime state. |
| Produce app artifact | Allow writes only to an app-generated job directory. Model supplies content, never arbitrary output paths. |
| Create/edit external file, later | Preview exact path and diff; require per-action approval. Recheck destination and expected original hash immediately before writing. |
| Delete, arbitrary shell/code, elevated actions | Not available initially. Adding these requires a separate security design. |
| Network, later | Explicit user-enabled capability and approved request scope, including destination, method, path/query, and any transmitted data; destination checks and bounded requests. Public Internet access alone is not an upload grant. |
| Browser mutation, messages, purchases | Not available initially. Future consequential actions require exact-target previews and fresh approval. |

Approval comes only through the trusted UI, never through model output or a tool result. Bind it to durable turn and invocation IDs, tool/version, canonical arguments, target identity/preconditions, and policy version; make it single-use and expiring. M5 uses these existing identities without jobs; from M6, also bind to the owning job ID while retaining turn/invocation identity. Existing M5 records are not made executable by attaching a job; obtain fresh approval for any eligible resumed action. Editing arguments invalidates approval. Persist the decision for audit, but invalidate unconsumed approvals on app restart. Waiting for approval holds no operation handle or DB transaction.

### Filesystem and network enforcement

Path checks use normalized absolute paths and actual filesystem identity, not string-prefix tests. Handle Windows case rules, drive-relative paths, UNC/device paths, alternate data streams, reserved names, traversal, symlinks, and junctions. Initially reject reparse-point traversal and non-regular files. Revalidate after opening using the final handle path where supported. New files use an approved, checked parent and exclusive creation; edits stage and atomically replace after checking preconditions. App private state, runtimes, and tool policy are never model-writable.

These checks defend against planner mistakes and prompt injection. They do not make a same-user Python process a hardened sandbox against malicious installed code or a hostile process racing filesystem changes. Arbitrary code execution stays excluded; audited sandboxing would be a separate feature.

Future HTTP tools allow only HTTP(S), block loopback/private/link-local/metadata destinations by default, validate DNS and redirects, cap body/decompressed size and time, and avoid forwarding credentials. Network policy must check the connected destination to address DNS rebinding. Browser traffic needs equivalent request controls; when those cannot be enforced, deny the navigation. Keep a dedicated browser profile without personal login state.

An explicitly requested URL authorizes only that bounded retrieval and policy-permitted redirects, not model-added path/query data or disclosure of local context. Changes outside the approved request scope require fresh trusted-UI authorization. A public destination or credential stripping alone does not prevent exfiltration. For M14, Playwright request hooks alone are not proof of connected-destination enforcement; block service workers and unsupported traffic paths, and deny navigation unless equivalent controls are demonstrated. Do not introduce browser enforcement infrastructure into the MVP.

Do not pass retrieved text into privileged instruction fields. Tool results carry source and trust labels, but labels alone cannot prevent injection; deterministic capability checks must withstand a persuaded model. Render outputs as text or sanitized Markdown, never executable HTML.

From M7, assistant/tool output must not automatically fetch external images, embeds, or link previews. Serve permitted local artifacts through controlled application routes and enforce a restrictive Content Security Policy that blocks external resource loads; sanitization alone does not stop a Markdown image URL from transmitting data. Test that displaying malicious output makes no external requests even with network tools disabled.

A child process has the current user's OS privileges unless separately restricted. Pin trusted model runtimes and code; no arbitrary ComfyUI custom nodes, remote-code model loaders, or LLM-supplied workflow graphs. Run as a standard user. OS compromise and malicious same-user applications are outside this initial threat model.

### Side effects and crash ambiguity

Before a side effect, commit an execution intent and single-use approval consumption. Afterwards, commit its receipt. Those commits cannot be atomic with an external action.

If a crash happens between the action and the receipt, mark the invocation **outcome_unknown**. Never automatically retry it. Inspect a verifiable postcondition, use a genuine destination-supported idempotency key, or ask the user to reconcile it. The tool's idempotency declaration alone is insufficient. A future create-file tool can use exclusive creation plus an expected content hash to reconcile safely.

Cancellation cannot undo an action already committed. Show completed effects even when the surrounding job is cancelled. Serial tool execution and a default cap of four tool calls per turn make this behavior understandable.

## 9. Long-running tasks and cancellation

Use a jobs table and a small in-process runner, not Celery or a workflow engine. Store job type/version, input references, status, next step, attempt count, deadlines, progress, result references, cancellation request, and error classification. Store step state in ordinary typed records; never pickle a running Python coroutine.

```text
queued -> running -> succeeded / failed
              | -> waiting_approval -> queued
              | -> cancel_requested -> cancelled
              | -> interrupted (detected on restart)
```

Cancellation can also move queued, waiting_approval, or interrupted work to cancel_requested. The persisted cancellation request is authoritative until cleanup completes; the status reflects its handling. A completion committed before cancellation is terminal and is not rewritten by a late cancel request.

Progress is stage-based unless the backend supplies a real fraction. Display “loading image model” or “step 12 of 25,” not invented percentages. A closed UI does not cancel a job; an explicit cancel request does.

Cancellation is durable and checked before each step and side effect. During inference, request backend cancellation and await acknowledgement; close/stop the owned backend if cancellation cannot be verified. Escalate to tree termination using bounded deadlines. Do not just cancel an asyncio task while the model continues using VRAM. A stalled cancellation can leave the resource slot unavailable rather than falsely free.

On startup, acquire the per-data-directory instance lock, open/migrate the database, and complete idempotent reconciliation before accepting new work. Apply the following rules to records that exist at the current milestone; M1-M5 do not create jobs merely to perform recovery. Separately verify prior runtime cleanup under the GPU-ownership rule before any heavy load.

| Persisted state | Startup action |
|---|---|
| queued job | Retain queued, but hold dispatch until explicit user resume. |
| running job | Mark interrupted; preserve completed steps and require explicit resume for safe remaining computation. |
| interrupted job | Keep interrupted; no automatic retry or replay. |
| waiting_approval job | Invalidate unconsumed approval; explicit resume revalidates the proposal and requests fresh approval. Cancellation remains available. |
| cancel_requested job or nonterminal job with cancellation requested | Preserve cancellation, finish/verify owned-worker cleanup, then mark cancelled. Failed cleanup leaves cancellation pending and the resource slot unavailable; never resume computation. |
| succeeded / failed / cancelled job | Preserve terminal state and committed results; a late cancellation cannot rewrite them. Still inspect associated invocations for unresolved outcomes. |
| Side-effect execution intent without receipt, regardless of job/turn status | Mark invocation outcome_unknown; block dependent execution until reconciliation. Cancellation does not turn an unknown effect into a known failure. |
| Invocation with committed receipt | Preserve receipt and result; do not repeat a completed action. |
| Incomplete turn or draft | Before M6, mark interrupted and release the active-turn claim without replay unless an invocation requires reconciliation. From M6, reconcile the turn with its owning job. Keep completed tool effects; apply section 7 when building context. |

From M6, rebuild per-conversation occupancy after reconciliation from retained queued/running/waiting/interrupted/cancellation-pending jobs. Release a claim only when its turn is settled and no invocation requires reconciliation; terminal jobs with unknown effects still require resolution. Pre-M6 turns retain the claim only for unresolved invocations as specified above. Resume must revalidate policy, honor cancellation, and retain attempt/retry limits. No arbitrary autonomous resumption after reboot.

One conversation has at most one active turn. Pending approval blocks that conversation's next turn, but the runner can work on another queued job. In the first version, an image operation can delay chat until it finishes or is cancelled; the UI reports that tradeoff plainly. Later multi-step jobs yield between steps. Do not promise arbitrary code checkpointing or invisible background autonomy.

## 10. Provider/backend abstraction strategy

Separate **capability** from **runtime lifecycle**:

| Contract | Minimal responsibility |
|---|---|
| ChatProvider | Stream a typed chat request into text deltas, completed tool proposals, usage, and terminal result; support cancellation. |
| ManagedRuntime | Start a profile, check readiness, request stop, await/force exit, report health. Only the resource manager controls it. |
| ImageProvider, later | Execute a bounded image request and return artifact references/progress. |
| Transcriber / Synthesizer, later | Audio-to-text / text-to-audio with typed file references. |
| VisionProvider, later | Answer a bounded question about an approved image; may reuse llama.cpp transport code. |

Do not force audio, diffusion, and chat into a giant generate() interface. Add capability contracts only when implementing that feature. Keep a single model-profile configuration registry, not a discovery service.

Normalize errors to a small set: invalid_request, unsupported_capability, unavailable, out_of_memory, timeout, cancelled, backend_crashed. Preserve the backend detail and exit code in redacted diagnostics. Normalized streaming requires exactly one terminal result, bounded buffers, tool-call assembly limits, and explicit incomplete-stream handling.

Profiles identify backend version, model path/hash, template version, supported capabilities, resource measurements, context/resolution bounds, startup limits, and an optional tested fallback. Backend launch flags and HTTP details live only in adapters/profile configuration. Tools cannot select arbitrary executables or model paths.

Implement **one real chat backend and one deterministic fake** first. A backend replacement must pass shared behavioral contract tests; it need not reproduce identical text or support every capability. Conversation data is backend-neutral and unsupported features fail explicitly.

Ollama was considered: it supports explicit unload through keep_alive=0. The recommendation remains an application-owned llama-server because direct process ownership makes mixed-backend lifecycle control easier to reason about. Do not implement an Ollama adapter without a concrete need. [Ollama lifecycle documentation](https://docs.ollama.com/faq)

## 11. Persistence and filesystem layout

Use Python's sqlite3 with short explicit transactions, foreign keys, a busy timeout, WAL, and synchronous=FULL for important state. Keep the database on a local disk. WAL allows readers alongside a writer, but SQLite still has one writer at a time; the app does not need multiple writer processes. [SQLite WAL documentation](https://www.sqlite.org/wal.html)

Initially use a single owner thread/connection for database work so disk calls do not block streaming/cancellation. Expose a few explicit store methods rather than arbitrary SQL through every service. Do not hold a transaction across inference, filesystem operations, network calls, or human approval.

Each business transition below is one short atomic store operation, not several independently committed service calls. Add only the records supported by the current milestone:

- Submit: user message and turn together from M1; include the job from M6.
- Record a tool outcome: invocation receipt and conversation result together from M5; include job-step completion/next-step advancement from M6 and artifact metadata when applicable. Intent and approval consumption remain a separate pre-execution transaction as specified in section 8.
- Finish a response: final assistant message and turn completion together from M1; include job completion from M6.

Use stable IDs/uniqueness constraints and conditional writes against the expected turn or step attempt and state. Stale callbacks cannot append results to a newer attempt or overwrite terminal state. During cancellation, still record completed effects against their original invocation without advancing cancelled work. Dispatch dependent work and acknowledge durable success only after commit acknowledgement. Cancelling an await on the database thread does not prove its queued or executing transaction rolled back; resolve its outcome by operation identity before retrying or advancing. Failure to commit an execution intent prevents the external action.

| Records | Purpose |
|---|---|
| schema_migrations | Ordered, tested SQL migrations and checksums. |
| conversations, turns, messages | Canonical transcript, sequence, completion/draft status, selected profile. |
| jobs, job_steps | Durable operation state, cancellation, attempts, restart checkpoints. |
| tool_invocations, approvals | Turn/invocation identity from M5; owning job link from M6. Canonical proposal, policy decision, execution intent, receipt/unknown outcome. |
| artifacts | App-generated relative path, kind, size, content hash, owning job/message. |
| summaries, memories, document_chunks | Added only with the corresponding later feature. |

Use JSON columns for bounded typed content and versioned job payloads, not for one giant application snapshot. Frequently queried fields remain ordinary columns. JSON schema versions change through explicit migrations or compatible readers.

Recommended development layout separates application source from private runtime data:

```text
E:\Jarvis\                         source repository
E:\JarvisData\                     private, configurable data root
  config.toml                      user settings and permitted roots
  jarvis.db                        authoritative local state
  artifacts\<job-id>\              finalized images/audio/files
  staging\<job-id>\                incomplete outputs
  logs\                           rotating JSONL + child logs
  models\                         GGUF/checkpoints/voices; no Git
  runtimes\                       pinned binaries and separate environments
  workspaces\default\             default permitted tool workspace
  cache\                          disposable derived data
  backups\                        DB snapshots + manifests + referenced artifacts
  run\                            instance lock and runtime metadata
```

The locations are proposed defaults, not directories created by this design. File tools operate on explicitly granted roots; the data root itself is not an allowed root. Store only relative app-generated artifact paths in the database, resolve through the artifact store, and reject paths returned by a backend that escape its job output directory.

Finalize an artifact by writing staging content, flushing application buffers and file data to storage, closing it, verifying size/hash, atomically renaming on the same volume, then committing metadata with its owning result/step transition. A flush failure prevents committing success. Filesystem and database changes are not one transaction: this protocol provides process-crash consistency through reconciliation, not an unconditional power-loss guarantee for rename metadata or storage hardware. After an unclean restart, verify referenced artifacts' existence, size, and hash; mark missing/corrupt outputs unavailable rather than reporting usable success. Retain suspicious staging/orphans for inspection before age-based cleanup.

Use SQLite's backup API for a consistent database snapshot. Suspend artifact deletion/cleanup before taking the snapshot; derive the artifact manifest from that completed snapshot, not the live database, then copy the referenced immutable files and verify their sizes/hashes. Publish the backup as complete only after the snapshot, manifest, and all required bytes are present; retain cleanup protection until copying finishes or the incomplete backup is abandoned. Restore into an empty data directory with the original data unavailable, verifying all references and hashes. M6 tests this with small artifact fixtures; real generated artifacts arrive in M8. Copying only jarvis.db while WAL is active is not a backup strategy. Do not back up model caches by default. Disk-full errors stop new work without clearing successful history.

### Logs and diagnostics

Use standard Python logging with structured JSON output and rotating files. Include conversation_id, turn_id, job_id, step_id, invocation_id, backend/profile, PID, duration, transition, and normalized error code. Default logs record metadata rather than raw prompts, file contents, credentials, or audio. Bounded child logs may still contain sensitive content and remain private.

A local diagnostics command shows runtime/model hashes, current residency, queue state, RAM/VRAM availability, recent failures, and log locations. Content-rich debugging is explicit, temporary, and redacted before export. No automatic telemetry uploads, metrics server, or tracing backend.

## 12. Recommended technology stack

| Area | Chosen technology | Reason / timing |
|---|---|---|
| Core | Python 3.12, asyncio, standard library | Conservative compatibility baseline; one application loop. |
| Environment | uv, pyproject.toml, committed lockfile | Reproducible core dependencies; model workers keep separate locks/environments. |
| Validation | Pydantic v2, strict schemas | Validate configuration, tool proposals, and API input. |
| Storage | sqlite3 + numbered SQL migrations | Enough for one local user; no ORM or DB server needed initially. |
| HTTP client | httpx | Timeouts, async streaming, loopback model APIs. |
| LLM | Pinned CUDA llama.cpp llama-server | Owned process, GGUF, explicit resource settings. |
| First model | Qwen3-4B-Instruct-2507, Q4_K_M GGUF | Small baseline; exact conversion/template must pass evaluation. |
| Image generation | Pinned ComfyUI, later | Reuse diffusion runtime and a fixed local workflow; separate environment. |
| First image profile | SD 1.5, FP16 safetensors, 512x512, batch 1 | Conservative capability test before larger diffusion models. |
| Speech-to-text | faster-whisper base, CPU INT8, later | Avoids GPU contention; bounded clip processing. |
| Text-to-speech | Piper CPU worker, later | Local speech with a small voice model. |
| Vision | llama.cpp + Qwen2.5-VL-3B GGUF/projector, later | Reuses supervision/transport, runs in place of the text model. |
| CLI | argparse + a small terminal presentation layer | Minimal starting interface; add Rich only for clear UX benefit. |
| Web UI | FastAPI/Uvicorn, static HTML/CSS/JavaScript, later | Single server worker, no large frontend framework required. |
| Browser tools | Playwright, later | Dedicated controlled browser process/profile. |
| Resource probes | psutil + optional NVIDIA NVML adapter | Runtime admission and diagnostics. |
| Windows lifecycle | pywin32 behind one supervisor module | Owned process-tree lifetime and termination. |
| Tests/quality | pytest, pytest-asyncio, Ruff, mypy | Fast model-free core tests, separate optional hardware tests. |

ComfyUI provides a local API, workflow files, and model memory management. Jarvis still governs cross-backend residency and executes only a checked-in workflow. Disable cloud/partner nodes, automatic model downloads, custom-node installation, and external listening in the pinned setup. [ComfyUI project documentation](https://github.com/Comfy-Org/ComfyUI)

faster-whisper supports CPU INT8. Its published small-model CPU figures were measured on different hardware and are not a latency guarantee for this PC. Start with base and measure transcription quality in the user's actual language. [faster-whisper documentation](https://github.com/SYSTRAN/faster-whisper)

Use the maintained OHF-Voice Piper project; record engine and individual voice licenses before redistribution. [Piper project](https://github.com/OHF-Voice/piper1-gpl)

llama.cpp documents Qwen2.5-VL support and matching multimodal projector loading. Cap image dimensions and image-token budget explicitly; a successful fit does not establish reliable small-text reading. [Multimodal support](https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md)

whisper.cpp is a reasonable runtime, but implementing it alongside faster-whisper adds no initial value. The selected CPU speech worker is already replaceable. Avoid LangChain, LangGraph, an agent framework, a vector server, Redis, and Celery until a demonstrated requirement justifies them.

Pin exact backend builds and model hashes during implementation, rather than inventing version pins in this document. Every upgrade reruns contract tests and the relevant hardware profile checks. Runtime/model downloads are explicit setup actions; normal use works offline unless a network tool is deliberately enabled.

## 13. Concrete repository structure

This is the target shape, not a request to create empty packages in milestone 1. Create a file when its behavior is implemented.

```text
Jarvis/
  README.md
  ARCHITECTURE.md
  AGENTS.md
  pyproject.toml
  uv.lock
  .gitignore
  config.example.toml
  src/jarvis/
    __main__.py
    bootstrap.py                 composition and startup/shutdown
    domain/
      types.py                   messages, IDs, statuses, typed requests
      errors.py
      ports.py                   only currently needed Protocols
    application/
      conversations.py
      context.py
      assistant.py
      jobs.py
      resources.py
      tools.py
      policy.py
    infrastructure/
      sqlite_store.py
      artifacts.py
      logging.py
      resource_probe.py
      processes_windows.py
      providers/
        fake.py
        llama_cpp.py
        comfyui.py               later
        speech.py                later
      tools/
        clock.py
        filesystem.py
        web.py                   later
        browser.py               later
    interfaces/
      cli.py
      web.py                     later
      static/                    later
  migrations/
    001_initial.sql
  profiles/
    text-small.example.toml
  workflows/
    sd15-basic.json               later; reviewed and pinned
  workers/
    comfy_launcher.py            later; authenticated backend entrypoint
    speech/                      later; separate dependency environment
  tests/
    unit/
    integration/                 actual SQLite, fake child processes
    contracts/                   provider/stream/tool behavior
    fixtures/                    malicious paths, streams, fake workers
    hardware/                    opt-in, models explicitly configured
  docs/
    adr/
    milestones/
    SECURITY.md
    DEVELOPMENT.md
    OPERATIONS.md
    RESOURCE_PROFILES.md
```

No backend dependencies in application imports. `bootstrap.py` chooses concrete adapters. Run import-boundary checks in CI and verify that importing the core never imports torch, transformers, faster_whisper, ComfyUI, or a GPU library.

## 14. Achievable MVP

The MVP is a **persistent local terminal assistant with one text model, a few read-only tools, visible task status, and reliable stop/restart behavior**.

It includes milestones M1-M6 below:

- Start offline with a previously installed model; chat and list/resume stored conversations.
- Stream bounded responses while preserving a durable, backend-independent transcript.
- Run clock, permitted-directory listing, and bounded text-file reading through deterministic policy.
- Limit each turn to four tool calls, a configured token budget, and a finite deadline.
- Cancel work and explicitly unload/reload the LLM without losing history.
- Recover to an inspectable state after backend failure; never repeat an unknown side effect.
- Show backend state and actionable errors; provide useful private logs.
- Prove text -> other-heavy-worker -> text switching with fake processes before adding real image generation.

The policy/approval mechanism is tested with a fake mutating tool, but no real mutation tool ships in the MVP. This establishes the boundary without expanding filesystem risk.

MVP success is behavioral reliability, not a cinematic voice experience. A 4B model will have limits in planning and knowledge. Failure to select a tool correctly should produce a harmless refusal/error, not an unsafe fallback.

## 15. Incremental development roadmap

Each milestone is a separate coding-agent assignment after its dependencies are complete. Each handoff names affected modules, carries its tests and scope limits, and ends with a short implementation note. Milestones do not authorize parallel agents modifying the same core. No milestone may silently pull in a later feature.

### M1 — Durable chat with a fake backend

- **Goal:** Prove that conversation state belongs to the application.
- **Scope:** Minimal package, domain message types, ChatProvider contract, deterministic FakeChatProvider, SQLite migration/store, terminal create/list/resume/send commands, and incomplete-turn status. Choose a data directory explicitly. Add README and initial AGENTS rules from this design.
- **Acceptance:** Send two turns, exit, restart, resume the same conversation, and observe the fake receive reconstructed prior messages. User message/turn creation and final reply/turn completion use the atomic operations in section 11, without job records. A fake failure after user persistence leaves that input intact and the turn marked failed/interrupted. Reusing a submission ID cannot create duplicate messages; stale completion cannot overwrite settled state.
- **Tests:** Temporary real SQLite round trip; second application instance over the same database; stable ordering; duplicate submission; injected provider exception; rollback and crash boundaries around submission/completion; stale completion. All tests offline without models or GPU packages.
- **Out of scope:** Real inference, streaming, context truncation, model processes, job queue, tools, approvals, web UI, downloads, voice, RAG.

### M2 — Bounded context, streaming, and diagnostics

- **Goal:** Make chat requests and partial responses predictable.
- **Scope:** Context budget builder, fake tokenizer seam, bounded streaming/draft checkpoints, normalized errors, cancellation signal, structured redacted logs. Keep the simple single-turn execution path until M6.
- **Acceptance:** Overlong histories retain required instructions/current input and complete exchanges; oversized mandatory content fails explicitly. Interrupted drafts are distinct from completed replies. Exactly one terminal result per stream.
- **Tests:** Exact budget edges, huge tool-result and interrupted-turn/tool-group fixtures (without tool execution), malformed/incomplete streams, slow consumer buffer limits, cancelled fake stream, cancelled database await with commit-outcome verification, no prompt/secrets in default logs.
- **Out of scope:** Summaries, embeddings, real backends, tools, automatic retries.

### M3 — Exclusive runtime lifecycle using fake processes

- **Goal:** Prove safe ownership and switching without CUDA.
- **Scope:** Resource manager with serialized transitions and runtime/request identities, profile bounds, injected RAM/VRAM probes, Windows process supervisor/creation-time Job Objects, cross-data-directory GPU-ownership lock tested with a test device key, fake workers with readiness/hang/crash modes, idle release and cleanup deadlines.
- **Acceptance:** Concurrent requests for different profiles, including separate data-directory instances sharing a test device key, never overlap heavy process residency. A warm model retains ownership. Failed or unverified previous-owner cleanup blocks the next start. Cancelling during load and killing the parent leave no owned child tree behind on Windows; caller cancellation cannot abandon cleanup.
- **Tests:** Fake-clock state transitions; stale readiness/idle/exit callbacks; start/stop races; cancelled waiter; parent death during spawn; child/grandchild termination; abandoned ownership lock; hung stop; readiness failure; cold versus warm RAM/VRAM accounting; unavailable telemetry; text-fake -> image-fake -> text-fake. Observe spawn/exit intervals to assert no overlap, without real GPU access.
- **Out of scope:** Real models, automatic model downloads, preemptive GPU scheduling, resource prediction models.

### M4 — First local LLM

- **Goal:** Replace fake text generation with a supervised real model.
- **Scope:** llama.cpp adapter, pinned local runtime/profile, Qwen 4B GGUF configuration, authenticated loopback endpoint, streamed chat translation, template/token-count validation, typed backend failures, explicit unload command.
- **Acceptance:** Chat across an unload/reload and app restart; previous conversation is reconstructed. One slot, cross-instance GPU ownership, and 4,096-token context are explicit. Calibrate cold-load peaks, warm residency, and additional long-prompt request headroom on this PC; reject the profile if it exceeds admission limits.
- **Tests:** Recorded HTTP/stream contract fixtures offline; unauthorized direct HTTP requests; bad tool-call payload fixtures; optional hardware load/chat/unload/reload and cancellation smoke tests. Record version/hash and actual peaks, not only a successful launch.
- **Out of scope:** Additional LLM backends/models, 7B tuning, CPU fallback, cloud inference, real tool execution.

### M5 — Validated read-only tools

- **Goal:** Establish a deterministic boundary between proposals and actions.
- **Scope:** Static registry, strict schemas, policy decisions, approval data model bound to durable turn/invocation IDs with fake UI/test mutator, bounded sequential assistant loop, clock/list/read tools, atomic durable receipt/result records. No job identity or scheduler is required yet.
- **Acceptance:** Allowed reads work; unknown tools and denied paths never reach a handler. A real write tool is unavailable. A completed call/result pair survives a backend restart and remains valid context if its turn is interrupted. Approval cannot come from model text, cannot be reused, and fails after arguments change or restart.
- **Tests:** Path traversal, sibling-prefix paths, junctions, symlinks, UNC/device/ADS cases, oversized payloads, duplicated JSON keys, prompt-injection fixtures, policy changes after approval, step cap, partial calls, duplicate execution intent, receipt/result transaction faults, completed and unknown fake effects in interrupted-turn context.
- **Out of scope:** Jobs/steps/queue (M6), shell, filesystem mutation, network/browser tools, dynamic plugins, image tools.

### M6 — Durable jobs, recovery, and MVP qualification

- **Goal:** Make cancellation and recovery reliable end to end.
- **Scope:** Jobs/steps migrations and owning-job links to turn/invocation identities, extension of section 11's atomic operations to job state, bounded runner, progress, durable cancellation, explicit resume, per-conversation serialization, section 9 startup reconciliation, safe-step retry budget, backup/restore and diagnostics. Adding job links never renews a prior approval.
- **Acceptance:** UI status/cancel stays responsive during a fake stalled request. Startup reconciles every persisted state before accepting work; completed steps remain completed, pending cancellation is honored, and expired approvals require renewal. Conversation occupancy is rebuilt consistently. Unknown side effects are never rerun. A backup restores into an empty directory without the original data, including referenced artifact fixtures. App remains usable after backend OOM/crash. Complete the MVP hardware checklist on this PC.
- **Tests:** Fault injection before/after every atomic transition and simulated side effect; stale-attempt writes; restart in every job state, including terminal jobs with unknown invocations; cancel during queued/start/running/approval/stop phases; M5 identity/approval migration; DB-full/write failure simulation; backup snapshot/manifest agreement, cleanup exclusion, missing/corrupt fixture detection, and independent restore. Run 10 real load/chat/unload cycles plus fake text/image/text switches; inspect for increasing memory and orphan processes. Inject OOM for mandatory tests; do not deliberately exhaust the user's PC.
- **Out of scope:** Automatic boot resumption, cron, arbitrary workflows, real images, new modalities, multiple executing model jobs. **MVP ends here.**

### M7 — Local browser interface

- **Goal:** Make existing behavior convenient without moving policy into the UI.
- **Scope:** FastAPI in the same process, static chat/history/job/approval screens, authenticated streaming progress, session setup, loopback binding, single server worker, restricted output rendering/CSP and controlled local-artifact routes.
- **Acceptance:** Reconnecting reconstructs status from SQLite; closing a tab leaves the job running. Approval identifies the exact action. Unauthenticated/cross-origin mutation requests fail. Displaying assistant/tool output never automatically loads external images, embeds, or link previews. Start in one browser session through a one-time local setup token, exchanged for a protected session cookie.
- **Tests:** API authentication, Host/Origin checks, CSRF on writes, bounded upload/body limits, escaped tool output, remote-resource exfiltration fixtures with network tools disabled, controlled artifact access, stream reconnect, cancel from a second authorized tab, duplicate-submit handling.
- **Out of scope:** Internet/LAN hosting, multiple users, Electron, mobile app, frontend framework migration.

### M8 — Real image generation and GPU handoff

- **Goal:** Fulfill an image request without losing conversational state.
- **Scope:** ComfyUI adapter/environment and authenticated launcher, one reviewed SD 1.5 workflow, fixed profile limits, artifact staging/finalization, durable image step, lifecycle switching.
- **Acceptance:** Chat -> generate one 512x512 image -> chat works with zero overlapping heavy workers. Finalization follows section 11's flush/rename/atomic-result protocol and stated durability limits; the output survives application restart and independent backup restore. Cancellation stops the runtime. Follow-up chat can name the artifact without loading the image into text context.
- **Tests:** Fake ComfyUI protocol; direct unauthorized HTTP/WebSocket and browser-origin requests; invalid workflow parameters, output path escape, missing/corrupt output, flush failure, OOM cleanup, crashes around artifact rename/result commit, image cancellation, backup/restore of generated artifacts. Hardware repeated text/image/text cycles within measured RAM/VRAM limits.
- **Out of scope:** Arbitrary graphs, custom nodes, SDXL/large diffusion, high-resolution fix, batching, ControlNet, LoRA management.

### M9 — One approved filesystem mutation

- **Goal:** Safely create a user-requested text file.
- **Scope:** Create-only tool under a granted root, exact preview, exclusive creation, single-use approval, execution intent, content-hash receipt, outcome reconciliation.
- **Acceptance:** Nothing is written before approval. Existing files are never overwritten. Destination changes invalidate approval. A crash after creation is reconciled using the exact expected file/hash, not blindly retried.
- **Tests:** Approval replay/expiry, restart before action, crash before receipt, parent-directory replacement/junction attempt, destination collision, cancellation after creation with completed-effect context preserved, unresolved-outcome context without replay, disk full.
- **Out of scope:** Editing existing files, deletion, recursive operations, shell commands, silent auto-approval. Those need separate later milestones.

### M10 — Push-to-talk transcription

- **Goal:** Accept speech without competing for GPU memory.
- **Scope:** CPU faster-whisper worker, bounded microphone clips, explicit recording UI, transcript preview/correction, RAM admission, stop/release worker when necessary.
- **Acceptance:** A short recording becomes editable user input; no tool is executed merely because uncertain audio resembles a command. No GPU allocations; memory and thread caps remain within profile.
- **Tests:** Audio fixtures, silence, invalid media, duration limit, worker crash, cancellation, RAM pressure. Optional real microphone/language quality check on this PC.
- **Out of scope:** Wake word, always-listening capture, diarization, live translation, full duplex.

### M11 — Spoken responses

- **Goal:** Read completed assistant text aloud locally.
- **Scope:** CPU Piper worker, one licensed voice, bounded text chunks/audio queue, playback cancel, worker idle release.
- **Acceptance:** Speech starts/stops predictably; cancelled/stale turns cannot continue playback. Text remains available if speech fails. No spoken approval is accepted as authorization in this milestone.
- **Tests:** Fake synthesis/playback, queue bounds, cancellation during synthesis and playback, wrong turn ID, worker exit, RAM admission. Optional listening test.
- **Out of scope:** Voice cloning, emotion controls, full duplex, interruption detection, speech-based privileged approvals.

### M12 — Explicit screenshot understanding

- **Goal:** Answer questions about a user-selected screenshot.
- **Scope:** Opt-in one-shot capture or image upload, preview, bounded image dimensions/token budget, vision profile and matching projector, GPU handoff, text result with source artifact.
- **Acceptance:** Text model unloads before vision loads. Screenshot storage/retention is visible. An unclear image produces uncertainty instead of assumed UI actions. Qualify accuracy on representative UI screenshots.
- **Tests:** Bad/oversized image, excessive image-token budget, cancellation, OOM, handoff/reload, artifact privacy, screenshot prompt injection. Hardware fit and a fixed screenshot evaluation set.
- **Out of scope:** Continuous screen recording, background monitoring, autonomous mouse/keyboard actions, multi-image/video analysis.

### M13 — Bounded web retrieval

- **Goal:** Fetch an explicitly requested public page as untrusted evidence.
- **Scope:** HTTP fetch tool, user-enabled network permission bound to the approved URL/request data, URL/destination policy, plain-text extraction, source metadata, time/size caps. Search API integration is a separate future decision.
- **Acceptance:** Approved public retrieval works; private-network and redirected private destinations are denied. Model-added URL parameters or other transmitted data outside the grant require fresh trusted-UI authorization. Content cannot alter policy or become instructions. Network is unnecessary for ordinary chat.
- **Tests:** DNS rebinding simulation, redirects and request-scope changes, local-data exfiltration through public URL paths/query strings, IPv4/IPv6 special ranges, decompression bombs, unsupported schemes, timeout, credential stripping, injected instructions, output truncation.
- **Out of scope:** Login cookies, uploads, web search accounts, purchases, browser control, Internet-wide crawling.

### M14 — Read-only browser tools

- **Goal:** Read pages that require JavaScript through a separate browser.
- **Scope:** Dedicated Playwright profile, controlled navigation/DOM extraction/screenshot, the same network policy, bounded tab count and memory, explicit close/cleanup.
- **Acceptance:** No personal browser profile is touched. Navigation cannot reach denied destinations through subresources or redirects or transmit data outside the approved scope. Browser RAM participates in admission. Request hooks alone do not establish connected-destination enforcement: block service workers and unsupported traffic paths; deny navigation unless equivalent controls are demonstrated.
- **Tests:** Local controlled test pages, actual destination checks including DNS rebinding, service-worker/unsupported-traffic denial, request scope and blocked destinations, popup/download denial, navigation races, crash/cancel cleanup, stale page state, injected page instructions.
- **Out of scope:** Logged-in automation, form submission, messages, arbitrary JavaScript supplied by the model, desktop control.

### M15 — Useful persistent retrieval

- **Goal:** Recover relevant information from explicitly approved local material.
- **Scope:** User-managed memory CRUD, document import/chunking with provenance, SQLite FTS5, bounded retrieval context, deletion/reindexing. Background indexing runs only on explicit request and yields to foreground jobs.
- **Acceptance:** Retrieval cites the source and location; changes/deletions invalidate stale chunks; permission revocation prevents future retrieval. A small labeled query set demonstrates value over recent chat alone.
- **Tests:** Ranking fixtures, provenance, stale source hashes, scope isolation, deletion, revoked root, malicious document text, context budget, interrupted indexing.
- **Out of scope:** Automatic life logging, speculative memory extraction, knowledge graphs, vector DB, embeddings until measured FTS failures justify a separate milestone.

## 16. Architecture rules for future AI coding agents

Copy these enforceable rules into AGENTS.md when implementation starts:

1. Implement only the assigned milestone. Do not scaffold speculative features or pull future dependencies into the core.
2. Preserve dependency direction. UI and provider code cannot bypass application services; only bootstrap wires concrete implementations.
3. Conversation and task state live in SQLite. Use ARCHITECTURE.md section 11's atomic transitions and expected-attempt/state checks; advance only after commit acknowledgement. Cancelling a database await is not proof of rollback.
4. All heavy runtime starts/stops go through the resource manager and process supervisor. Preserve one heavy resident process, including idle, across data directories using GPU ownership. Serialize transitions, reject stale callbacks, and retain ownership through confirmed cleanup even if the caller cancels.
5. Never hold a database transaction, model operation handle, or short-lived state/transition lock while waiting for human approval. A warm idle runtime may retain its lifetime GPU-ownership lock. End the text operation before invoking a different model.
6. Never execute partial/invalid tool calls. All actions pass validation, policy, current preconditions, and any required single-use approval bound to turn/invocation identity (plus job identity from M6). Restart invalidates unconsumed approvals.
7. Treat tool results, retrieved documents, screenshots, and model output as untrusted data. They cannot grant permission or modify configuration. Rendering cannot automatically fetch external resources; network access must stay within the approved request/data scope.
8. No arbitrary eval/exec/shell, unreviewed model code, custom nodes, dynamic tool installation, or elevation.
9. Every external operation has input/output limits, deadlines, cancellation behavior, and a declared retry/side-effect policy.
10. Never automatically retry a side effect with an unknown result. Preserve completed effects in interrupted-turn context without inventing receipts. Reconcile every persisted state before new work, honor pending cancellation, and require explicit resume after restart (ARCHITECTURE.md section 9).
11. Core imports and default tests require no GPU, model downloads, network, or inference libraries. Use fakes plus real temporary SQLite for core integration tests.
12. Pin dependencies, binary/model hashes, workflows, and templates. Backend upgrades rerun contract and hardware profile tests.
13. Migrations preserve user data and require migration/restore tests; never reset a real database to fix a development error. Follow ARCHITECTURE.md section 11's artifact flush/reconciliation and snapshot-derived backup protocol; verify restore without original files. Do not claim unconditional power-loss durability.
14. Logs are bounded and private by default; redact secrets and avoid raw content. Surface failures rather than hiding exceptions or silently falling back.
15. Prefer an explicit function or small module over a framework. Add an abstraction only for a demonstrated boundary. Do not create microservices, agent teams, brokers, or a plugin system.
16. Update the relevant docs with each behavior change. Changing an invariant requires an ADR and corresponding tests; unresolved design conflicts must be reported, not silently bypassed.

CI should enforce import boundaries, formatting/type checks, and model-free tests. Review should check that new tools cannot bypass policy and new model calls cannot bypass residency. These rules are more useful than a large collection of style mandates.

## 17. Recommended project documentation

| Document | Required content |
|---|---|
| README.md | What currently works, offline setup, exact run/test commands, data location, current milestone. Clearly distinguish implemented from planned. |
| ARCHITECTURE.md | This design, invariants, state machines, boundaries, tradeoffs; update when decisions change. |
| AGENTS.md | Short mandatory rules above, repository map, validation commands, milestone scope discipline. No duplicate architecture essay. |
| docs/SECURITY.md | Threat model, capability table, approval semantics, filesystem/network restrictions, unsupported operations. |
| docs/DEVELOPMENT.md | Fake-provider workflow, test markers, adapter/tool addition recipes, dependency policy. |
| docs/OPERATIONS.md | Start/stop, stuck-worker recovery, logs, backups/restores, disk-full and OOM troubleshooting. |
| docs/RESOURCE_PROFILES.md | Measured cold/warm peaks and timings, hardware/driver/runtime/model hashes, context/image limits. Never label estimates as measured. |
| docs/milestones/Mxx.md | Goal, dependencies, affected modules, acceptance criteria, tests, exclusions, completion evidence. |
| docs/adr/*.md | Short decision records with context, decision, consequences, and supersession links. |

Initial ADRs: 0001 single Python app; 0002 disposable owned model processes and exclusive residency; 0003 durable transcript/context separation; 0004 deterministic tool policy and single-use approval; 0005 checkpointed jobs with unknown side effects; 0006 selected local backend and resource profile.

Add ADRs when making those decisions in code, not as administrative paperwork for every small edit. M1 needs only the first relevant records; later milestones add their own. This task delivers the architecture document only, not empty implementation scaffolding.

## 18. Major risks, mitigations, and critical review

| Risk | Mitigation and remaining limit |
|---|---|
| GPU fits but RAM loading peak does not | Measure both cold-load and execution peaks, evict CPU workers, serialize loads, reserve available RAM. A configuration that pages heavily is rejected. |
| Other applications consume GPU/RAM | Admission samples real availability, reports waiting/failure, never kills other apps. Measurements cannot reserve capacity against an external process. |
| Model/template/tool-call mismatch | Pin exact tuple and test fixtures plus actual tool-selection examples. Schema validity alone does not establish planning quality. |
| Model is too weak for reliable planning | Keep tools narrow and deterministic; prefer explicit user actions; evaluate useful tasks before adding complexity. |
| OOM leaves a damaged CUDA runtime | Dispose of process tree, bounded safe retry, then stop. Driver-level failure may still require manual intervention. |
| Reload latency makes interaction slow | Keep text warm briefly, switch only when needed, show loading stage, avoid unnecessary post-image LLM calls. No speculative second model. |
| Prompt injection causes unsafe proposals | Deterministic capability checks, no generic shell, exact approvals. Never rely only on a system prompt. |
| App crash duplicates an action | Durable intent/receipt and outcome_unknown; reconcile instead of promising exactly-once execution. |
| Same-user malicious code bypasses policy | Outside the initial trust boundary; trusted pinned runtimes and no arbitrary extensions. Process separation alone is not sandboxing. |
| Native dependencies conflict | Separate runtime environments; keep AI libraries out of core. Record driver/backend compatibility for each profile. |
| Local web interface becomes an attack surface | Loopback, validated Host/Origin, session auth, CSRF protection, escaped output, and CSP blocking automatic external resource loads; backend endpoints separately require per-launch authentication. |
| Agent-generated changes erode boundaries | Small scoped milestones, concise AGENTS rules, import/contract tests, ADRs for invariant changes. |
| Local data grows or is corrupted | Rotation/quotas, disk admission, transactional writes, artifact reconciliation, tested backup/restore. Plaintext data relies on OS account/disk protection. |

### Self-review performed before recommending this design

| Question | Review result / revision included |
|---|---|
| Realistic on 8 GB VRAM and 16 GB RAM? | Yes for the proposed small sequential workloads, subject to measured qualification. Revised the default from an aggressive 7B-class assumption to 4B and reserved system RAM as well as VRAM. Large models and high-resolution image workflows are not promised. |
| Overengineered? | Reduced to one app, one store, one runner, one resident slot. No generic workflow language, universal inference interface, agent framework, distributed queue, or second real chat adapter. |
| Replaceable backends? | Application-owned typed capability contracts and adapter-private protocols. Shared contract tests establish behavior; model quality and capabilities can still differ. |
| Conversation survives unloading? | Full transcript is durable and every context is rebuilt. A model cache cannot be authoritative. Interrupted partial output is identified explicitly. |
| OOM/crash recovery credible? | Cleanup requires actual process-tree exit; failed cleanup never frees the slot. RAM cache defaults are explicitly bounded. Unknown driver state blocks further loading. |
| Tools safely controlled? | Validation alone was insufficient: added deterministic policy, bound approvals, revalidation, and unknown-outcome handling. Added backend authentication because loopback alone does not protect local model endpoints. Arbitrary execution is excluded; hostile local code is not claimed to be sandboxed. |
| Mostly testable without GPU? | Fake providers/processes/probes/clocks exercise scheduler, policy, persistence, recovery, and context. Hardware tests only qualify actual fit, performance, template compatibility, and runtime cleanup. |
| Understandable to future coding agents? | Concrete file responsibilities, one-way imports, enforceable invariants, scoped milestones, and explicit exclusions. Future packages are not pre-created. |

This review supports the architecture, not a claim of hardware validation. No code, GPU benchmark, or application test was run as part of this design task.

## 19. Deliberately do not build yet

- Multi-agent execution, autonomous agent hierarchies, self-modifying tools, or a plugin marketplace.
- Microservices, Kubernetes, Docker orchestration, Redis, distributed locks, message brokers, or event sourcing.
- Multiple simultaneously resident heavy models, speculative decoding with a second model, background GPU embeddings, or dynamic optimization of model placement.
- Arbitrary shell/Python execution, administrator actions, unattended deletion, unrestricted desktop control, personal-browser automation, or automatic message sending.
- Always-listening voice, wake words, full-duplex audio, continuous screen surveillance, or automatic recording of personal activity.
- Large diffusion/video models, large image batches, complicated ComfyUI graphs, or unrestricted custom nodes.
- Vector databases, automatic memory extraction, knowledge graphs, or indiscriminate filesystem indexing.
- Cloud fallback, remote access, multiple users, mobile clients, or enterprise authentication.
- Persistent KV-cache formats, exact token-stream resumption, automatic replay of external actions, or a generic workflow editor.
- A polished desktop shell before reliable terminal behavior and hardware qualification.

## 20. Final recommended architecture and first implementation handoff

Build one native Windows Python application, using SQLite for conversations and jobs, a terminal MVP, deterministic tool policy, and supervised disposable model processes. Use llama.cpp with a small quantized text model first. Enforce one heavy resident runtime through a single resource manager. Later add ComfyUI, CPU speech workers, vision through llama.cpp, and constrained web/browser capabilities through the same application boundaries.

The first implementation milestone is **M1: durable chat with a deterministic fake backend**. It establishes the hardest-to-retrofit ownership decision without introducing GPU installation, model quality, or operating-system actions into the first iteration.

Give a coding agent this prompt:

> Implement only milestone M1 in E:\Jarvis\ARCHITECTURE.md. Build a minimal Python 3.12 package with a terminal interface, typed conversation/message records, a small ChatProvider Protocol, a deterministic FakeChatProvider, and a SQLite store with its first migration. Support create/list/resume/send and an explicit temporary/custom data directory. Atomically persist user input with its turn before calling the provider, and the final response with turn completion; reconstruct prior messages from SQLite after application restart. Preserve incomplete/failed turns and prevent duplicate submissions and stale completion writes using stable IDs and expected state. Add README setup/test instructions and concise AGENTS.md rules from section 16. Add offline pytest tests using real temporary SQLite for restart persistence, ordering, duplicate submission, provider failure, rollback, and submission/completion crash boundaries. Do not add real models, downloads, GPU dependencies, streaming, tools, jobs, web UI, voice, or RAG. Do not scaffold later modules. Run the relevant tests and report changed files, results, and any unresolved issues. Preserve this architecture document and do not reset real user data.
