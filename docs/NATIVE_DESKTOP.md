# Native Windows desktop — installed 0.3.1 preview

Date: 9 October 2026. Status: installed preview with an offline first-install component pack; not a signed public release or a clean-Windows certification.

## Implemented

- A C#/.NET 10 WinForms host uses WebView2 directly. It displays the existing interface and owns backend startup/shutdown without the Tk controller, pywebview window process or a console. The old launcher remains available.
- A self-contained .NET publish and a private CPython 3.12.13 runtime include the backend and locked Python packages. The packaged backend runs with an isolated module search path. No system Python, uv, .NET installation or source checkout is required for the packaged core.
- The host joins its own kill-on-close Windows Job Object **before** starting children. Backend and WebView2 processes inherit that job at creation without breakaway. Existing model supervisors create nested jobs. Normal close waits for backend exit and the job's active process count to return to the host alone. A failed verification retains the window/ownership; no process is killed by executable name.
- A bounded versioned JSON-lines lifecycle protocol uses inherited private pipes. Setup credentials never enter the command line or logs. The backend reserves its loopback socket before announcing readiness, eliminating the choose-port/close/rebind race. Lost stdin ownership requests graceful shutdown. An absent model profile fails explicitly instead of starting the fake provider.
- Backend lifecycle control no longer imports Tk. The legacy launcher reuses the extracted `web_control` module, preserving its behavior.
- Microphone requests from the current local document and a user action use a Russian panel in the host. Other origins, frames and unused permission kinds are denied. Consent is limited to the current launch. Refusal, timeout, navigation and closing cancel the request. Windows privacy controls still apply.
- Generated images use an owned Windows Save As dialog, PNG filename and an in-app completion message. Non-local download URLs are rejected. Valid generated images/screenshots now have `image/png` attachment responses; other artifact bytes retain the generic binary response. The visible browser file-input text is replaced with a styled Russian attachment control; paste/drop use the existing tested path.
- A per-user MSI installs the preview independently and registers a Start-menu shortcut. It owns application binaries only; settings, databases and models are outside that directory. The build uses Windows Installer/FCI through Python 3.12's build-only `msilib`, not a downloaded installer compiler.
- On first launch an existing data folder can be selected using a system folder picker. The application remembers that explicit choice and starts it. On this PC the separate desktop shortcut `Jarvis Preview` supplies the existing `E:\JarvisData` directly. No real history was reset or migrated by a test.
- First launch also offers installation of the existing five model profiles from an offline pack into a **new** `JarvisData` folder on a chosen drive. It shows progress and supports cancellation. The setup helper inherits the host's job; EOF cancels setup. Normal cancellation removes only that attempt's staging directory. A process/power loss can leave an unpublished staging directory; automatic recovery/resume is not implemented.
- The pack contains relocatable private Python workers, exact locked wheel files, pinned models and committed ComfyUI source. It excludes venv configuration, user history, arbitrary source-tree files, worker input/output, cache and private GPU calibration. Fresh profiles use the actual first NVIDIA device UUID and conservative, uncalibrated example peaks. Resource reserves/admission rules are unchanged.
- The pack manifest's SHA-256 is pinned **inside the application**. Every extracted file is checked against its size/hash before publication by directory rename. Windows path aliases, traversal, duplicate entries, links, unexpected files and insufficient disk space fail without overwriting an existing data directory. This depends on trust in the application build; an unsigned application does not authenticate its publisher.
- Both existing and packaged ComfyUI installations check source hashes against the application-owned pinned manifest. Runtime startup never invokes Git. The `packaged_source` profile field is retained for compatibility, but no longer selects a different verification path. Inference code and one-heavy-runtime scheduling are unchanged.

## Build and checks

Build prerequisites: Windows x64, the repository's locked Python 3.12 environment, uv and .NET SDK 10.0.401. The SDK and WebView2 package version are pinned under `desktop`; Python 3.12 is necessary for the current MSI generator. These are build-machine requirements only.

```powershell
uv run --locked python scripts/build-component-pack.py --assets PATH_TO_PREPARED_COMPONENTS --output dist/Jarvis-models-v1.zip
uv run --locked python scripts/build-desktop.py --dotnet PATH_TO_DOTNET_EXE --output dist/Jarvis-preview
uv run --locked python scripts/build-desktop-msi.py dist/Jarvis-preview dist/Jarvis-preview.msi
uv run --offline --locked python scripts/verify-desktop.py dist/Jarvis-preview/Jarvis.exe run/new-report.json
```

Use a fresh versioned build directory and report path. Production builds retain the existing library licenses. They currently include some unused legacy-shell dependencies from the shared project lock; these are not loaded by the new host and can be split out in a later packaging change.

Build the component pack before the application: it updates `infrastructure/assets/model-pack.json` and `comfy-source.json`, which are included in the wheel. The builder reads explicitly selected model paths, the pinned Git archive and locked wheel RECORD entries, checking recorded dependency hashes. The current full bundle occupies 18,023,263,655 unpacked bytes (about 16.8 GiB). The app-only MSI excludes this large optional archive; existing users keep their current model folder.

To exercise the actual native installer in a fresh test directory on a drive with enough space:

```powershell
uv run --offline --locked python scripts/verify-desktop.py dist/Jarvis-preview/Jarvis.exe run/setup-report.json --component-pack dist/Jarvis-models-v1.zip --temp-root E:/Jarvis/run
```

This fixed scenario never saves desktop settings or opens a supplied real data directory. After installing components it runs the ordinary fake-provider smoke and verifies full tree exit.

The fixed native smoke scenario always creates its own temporary data directory, uses the deterministic fake provider, opens the actual WebView2 interface, authenticates, submits and verifies a completed turn, exercises microphone refusal, captures the page and closes. It offers no arbitrary page-to-native API. `verify-desktop.py` clears development-runtime environment variables, limits PATH to Windows system tools and independently verifies the complete owned tree.

## Initial 0.2 qualification

- Offline Python suite: **308 passed, 3 hardware opt-in tests skipped**. After the final download MIME change, the affected artifact/image/vision/backend suites passed **39 tests**. JavaScript: **14 passed**. Ruff and mypy passed (62 source files). Native Release publish succeeded without compiler warnings.
- Portable and MSI-installed native smoke runs passed: authenticated chat, durable fake reply, host microphone panel and refusal, graceful shutdown, zero owned descendants. No microphone permission was automatically granted and no speech was recorded by the smoke test.
- The installed native smoke sampled a **251.9 MiB peak private memory** across the observed application tree. This short fake-provider startup/chat run is not an idle budget, a sustained benchmark or a real-model memory measurement.
- A root-only crash test terminated the test host's exact process handle after five owned processes existed. All owned processes exited; the outer test supervisor verified zero remained.
- MSI installation and uninstallation of a separate test copy both returned success. The application executable was removed while the test conversation database outside the installation directory remained intact. The final preview was then installed into the current user's `Programs\Jarvis Preview` directory and passed its native smoke test.
- A packaged-core check using the actual existing profiles and a temporary database passed real text generation, image generation, Piper WAV synthesis and a silence transcription fixture. It advertised the existing speech/voice/vision capabilities. This was a separate backend qualification, not a manual end-to-end microphone recording or save-dialog interaction.
- Repeating the real-model qualification after installing the final preview was refused at text admission with `out_of_memory`. Available RAM was then 6.73 GiB; the current text profile requires 4 GiB plus the unchanged 3 GiB reserve. No profile/reserve was lowered and no user application was closed. Consequently the final installed build has native fake-provider coverage and prior packaged-core model coverage; its final full hardware repeat did not pass.

### 0.3 component-install and upgrade qualification

- Offline Python suite: **331 passed, 3 opt-in hardware tests skipped**; JavaScript **14 passed**. Native Release build, Ruff and mypy passed.
- The actual host installed the full archive into a fresh directory, opened authenticated chat, completed a fake turn, exercised microphone refusal and verified zero remaining descendants on close. The sampled peak for this installation/startup/chat scenario was **257.2 MiB private memory**, without an inference model.
- Using these newly installed components and temporary conversation databases, the packaged backend passed real text generation, SD1.5 generation, authenticated PNG download, Piper WAV synthesis, silent-WAV transcription and a vision job with a synthetic PNG. PATH contained only Windows System32; workers had no development Python/Git environment. The vision check verified successful processing/completion, not broad semantic quality.
- An upgrade test exposed missing PE versions in the MSI File table: Windows Installer omitted binaries after removing the older release. The builder now supplies actual version/language using `MsiGetFileVersionW`; see Microsoft's [file replacement rules](https://learn.microsoft.com/en-us/windows/win32/msi/replacing-existing-files). The failed test package is superseded by **`Jarvis-0.3.0-preview-final.msi`**.
- The corrected **0.2 → 0.3 upgrade** succeeded. All **2,056** installed application files matched the published build byte for byte. The installed native smoke passed (256.3 MiB sampled peak, microphone refusal, complete tree exit). An attempted downgrade to 0.2 was blocked and 0.3 remained installed. These tests did not reset user profiles, models or conversations.
- Local cleanup was rejected by automatic tool approval review (`blocked by policy`, no further reason). The temporary component test directory `run/jarvis-native-test-603851ba6a1244e58dada4adf5128d4d` remains (about 16.8 GiB), as does the superseded test MSI. Use only the `-final.msi` artifact for installation. No alternate deletion mechanism was attempted.

### 0.3.1 image-startup fix

- Reproduced a stall with the real desktop JSON-lines entry point and an existing `image.toml` without `packaged_source`. Two text requests completed; image generation remained at `image_loading`, before a ComfyUI worker was created. A timed stack capture located the wait in `subprocess.check_output` for Git `rev-parse`; after its timeout, Python's Windows pipe reader was still waiting on a descendant Git process. The earlier standalone-bootstrap and packaged-model checks did not exercise this combination of inherited desktop pipes and the legacy verification branch.
- Removed runtime Git invocation for both profile variants. Source bytes still have to match the manifest shipped inside Jarvis and checkpoint verification is unchanged. Tests cover existing/package profiles, prohibit subprocess creation during verification, and reject modified source.
- The message card now shows live image preparation/generation/saving stages. Cached job timestamps prevent an older HTTP snapshot replacing a newer event; events from a closed conversation stream are ignored. Status nodes remain accessible without opening the activity panel.
- Offline checks: **333 Python tests passed, 3 hardware opt-ins skipped; 15 JavaScript tests passed**. Mypy and Ruff passed. A real test through the desktop backend using existing profiles completed the image turn in **19.3 seconds** and produced an available PNG artifact. Test conversations used temporary databases; real user history and uncertain/cancelled jobs were not reset or replayed.
- Patch installer: `dist/Jarvis-0.3.1-preview.msi`. This replaces the 0.3 preview while retaining its data/model paths and the same optional offline component archive.
- After installing the patch, the same desktop-entry-point check completed the image request in **19.0 seconds**, downloaded a valid PNG (422,457 bytes, `image/png`) and verified full process cleanup. The installed native-window smoke also passed, including microphone refusal and tree exit (249.5 MiB sampled peak without an inference model).

## Remaining before a complete out-of-box release

- The offline full archive is implemented. Online delivery, resumable setup, separate capability packs and component upgrades are not implemented. The app-only MSI requires either that archive or an existing configured data folder.
- WebView2 Evergreen and a compatible NVIDIA driver must already be present. Clean-Windows/second-machine checks, automatic prerequisites, complete distribution licence/source/notice review, signing and public distribution remain outstanding. Preserved wheel/source licences alone are not a completed redistribution review.
- Upgrade/downgrade checks passed for these two previews. Installer rollback/failure injection and schema compatibility for future versions still need qualification; these tests do not prove arbitrary future upgrades safe.
- Standard Windows dialogs are retained. Saving/cancelling/overwriting files and microphone allow/revocation still need interactive verification; the native test exercised refusal only. Russian voice quality and vision semantics retain their previously documented limitations.
- Large-history pagination, broader UI polish, long-session measurements and the full repeated lifecycle matrix remain future work. The current interface is preserved as the baseline.

Architecture and acceptance direction: [desktop review](DESKTOP_ARCHITECTURE_REVIEW.md). Previous shell verification: [desktop app](DESKTOP_APP.md).
