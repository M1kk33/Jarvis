"""Wire the conversation application to its store, fake provider, and terminal UI."""

import asyncio
import json
import socket
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import psutil

from jarvis.application.browser import BrowserService
from jarvis.application.calibration import calibrate
from jarvis.application.context import ContextBuilder, PreflightTokenEstimator
from jarvis.application.conversations import ConversationService
from jarvis.application.file_creation import FileCreateService
from jarvis.application.images import ImageService
from jarvis.application.jobs import JobRunner
from jarvis.application.resource_manager import (
    CpuWorkerGate,
    ResourceManager,
    RuntimeRouter,
)
from jarvis.application.retrieval import RetrievalService
from jarvis.application.settings import SettingsService
from jarvis.application.speech import SpeechService
from jarvis.application.spoken import SpokenPlaybackService
from jarvis.application.statistics import StatisticsService
from jarvis.application.tools import ToolPolicy, ToolService
from jarvis.application.vision import VisionService
from jarvis.domain.ports import ChatProvider
from jarvis.domain.resource_ports import ManagedRuntime
from jarvis.domain.resources import ResourceError
from jarvis.domain.types import ConversationError
from jarvis.infrastructure.backup import create_backup, restore_backup
from jarvis.infrastructure.browser_profile import BrowserProfile
from jarvis.infrastructure.browser_renderer import SafeBrowserRenderer
from jarvis.infrastructure.create_tools import SafeFileCreator
from jarvis.infrastructure.diagnostics import Diagnostics
from jarvis.infrastructure.gpu_lock import NamedGpuLock
from jarvis.infrastructure.image_profile import LocalImageProfile
from jarvis.infrastructure.local_profile import LocalTextProfile
from jarvis.infrastructure.piper_adapter import CpuPiperSynthesizer
from jarvis.infrastructure.piper_profile import PiperProfile
from jarvis.infrastructure.providers.comfyui import ComfyImageProvider, ComfyRuntime
from jarvis.infrastructure.providers.fake import FakeChatProvider
from jarvis.infrastructure.providers.llama_cpp import (
    LlamaChatProvider,
    LlamaServerRuntime,
)
from jarvis.infrastructure.read_tools import SafeDocumentReader, SafeReadTools
from jarvis.infrastructure.resource_probe import NvidiaResourceProbe, SystemClock
from jarvis.infrastructure.speech import CpuSpeechTranscriber, SystemRamProbe
from jarvis.infrastructure.speech_profile import SpeechProfile
from jarvis.infrastructure.sqlite_store import SqliteStore
from jarvis.infrastructure.vision_profile import LocalVisionProfile
from jarvis.infrastructure.web_fetch import PublicPageFetcher
from jarvis.interfaces.cli import dispatch, parse_args

if TYPE_CHECKING:
    from jarvis.interfaces.web_control import WebControl

def main(
    argv: list[str] | None = None,
    *,
    web_control: "WebControl | None" = None,
    web_socket: socket.socket | None = None,
) -> int:
    args = parse_args(argv)
    try:
        if args.command == "restore":
            print(restore_backup(Path(args.backup_dir), Path(args.data_dir)))
            return 0
        with SqliteStore(Path(args.data_dir)) as store:
            preferences = store.get_preferences()
            diagnostics = Diagnostics(store.data_dir)
            retrieval = RetrievalService(store, SafeDocumentReader(store.data_dir))
            try:
                manager = None
                provider: ChatProvider = FakeChatProvider()
                profile: LocalTextProfile | None = None
                image_profile: LocalImageProfile | None = None
                vision_profile: LocalVisionProfile | None = None
                vision_runtime: LlamaServerRuntime | None = None
                probe = None
                if args.image_profile and not args.profile:
                    raise ValueError("Image profile requires the pinned text profile")
                if args.speech_profile and args.command != "web":
                    raise ValueError("Speech profile is available only in web")
                if args.piper_profile and args.command != "web":
                    raise ValueError("Piper profile is available only in web")
                if args.vision_profile and (args.command != "web" or not args.profile):
                    raise ValueError("Vision requires a model-backed web session")
                if args.command == "calibrate" and not args.profile:
                    raise ValueError("Calibration requires --profile")
                if args.read_root and (
                    not args.profile
                    or args.command not in {"send", "session", "resume-job", "web"}
                ):
                    raise ValueError(
                        "Read tools require a model session or send command"
                    )
                if args.write_root and (
                    args.command not in {"web", "reconcile-file"}
                    or (args.command == "web" and not args.profile)
                ):
                    raise ValueError(
                        "Write roots require a model-backed web session or reconciliation"
                    )
                if args.command == "reconcile-file" and not args.write_root:
                    raise ValueError("File reconciliation requires --write-root")
                if args.enable_network and (args.command != "web" or not args.profile):
                    raise ValueError(
                        "Network retrieval requires a model-backed web session"
                    )
                if args.enable_browser and not args.enable_network:
                    raise ValueError("Browser requires --enable-network")
                network_enabled = browser_enabled = False
                browser_profile = None
                if args.command == "web" and args.profile:
                    network_enabled, browser_enabled = preferences.network_options(
                        args.enable_network, args.enable_browser
                    )
                    try:
                        browser_profile = BrowserProfile.installed()
                    except (ValueError, OSError):
                        # Keep chat/settings usable when an optional component is
                        # missing or changed; never advertise an unverified browser.
                        pass
                    browser_enabled = browser_enabled and browser_profile is not None
                if args.profile and args.command in {
                    "send",
                    "session",
                    "calibrate",
                    "resume-job",
                    "diagnostics",
                    "web",
                }:
                    profile = LocalTextProfile.from_file(
                        Path(args.profile), store.data_dir
                    )
                    runtime = LlamaServerRuntime(profile, store.data_dir / "run")
                    runtimes: dict[str, ManagedRuntime] = {profile.name: runtime}
                    if args.image_profile and args.command in {
                        "send",
                        "session",
                        "resume-job",
                        "web",
                        "diagnostics",
                    }:
                        image_profile = LocalImageProfile.from_file(
                            Path(args.image_profile), store.data_dir
                        )
                        if (
                            image_profile.device_uuid != profile.device_uuid
                            or image_profile.device_index != profile.device_index
                        ):
                            raise ValueError(
                                "Text and image profiles must select the same device"
                            )
                        image_runtime = ComfyRuntime(
                            image_profile, store.data_dir / "run"
                        )
                        runtimes[image_profile.name] = image_runtime
                    if args.vision_profile:
                        vision_profile = LocalVisionProfile.from_file(
                            Path(args.vision_profile), store.data_dir
                        )
                        if (
                            vision_profile.device_uuid != profile.device_uuid
                            or vision_profile.device_index != profile.device_index
                        ):
                            raise ValueError(
                                "Text and vision profiles must select the same GPU"
                            )
                        vision_runtime = LlamaServerRuntime(
                            vision_profile, store.data_dir / "run"
                        )
                        runtimes[vision_profile.name] = vision_runtime
                    probe = NvidiaResourceProbe(
                        profile.device_uuid, profile.device_index
                    )
                    manager = ResourceManager(
                        RuntimeRouter(runtimes),
                        probe,
                        NamedGpuLock(profile.device_uuid),
                        SystemClock(),
                    )
                    provider = LlamaChatProvider(
                        runtime,
                        max_output_tokens=preferences.output_tokens,
                        request_seconds=300,
                    )
                if args.command == "calibrate":
                    assert (
                        profile is not None
                        and manager is not None
                        and probe is not None
                    )
                    assert isinstance(provider, LlamaChatProvider)
                    report = asyncio.run(
                        calibrate(
                            profile.admission_profile(),
                            manager,
                            provider,
                            probe,
                            store.data_dir,
                            {
                                "runtime_version": profile.runtime_version,
                                "executable_sha256": profile.executable_sha256,
                                "model_sha256": profile.model_sha256,
                                "device_uuid": profile.device_uuid,
                            },
                        )
                    )
                    print(json.dumps(report, indent=2))
                    return 0 if report["passed_profile_bounds"] else 1
                if args.command == "backup":
                    print(create_backup(store))
                    return 0
                if args.command == "diagnostics":
                    jobs = store.list_jobs()
                    sample = probe.snapshot() if probe is not None else None
                    print(
                        json.dumps(
                            {
                                "backend_state": (
                                    manager.view.state.value
                                    if manager
                                    else "not_configured"
                                ),
                                "profile": (
                                    {
                                        "name": profile.name,
                                        "runtime_version": profile.runtime_version,
                                        "executable_sha256": profile.executable_sha256,
                                        "model_sha256": profile.model_sha256,
                                    }
                                    if profile
                                    else None
                                ),
                                "image_profile": (
                                    {
                                        "name": image_profile.name,
                                        "runtime_commit": image_profile.runtime_commit,
                                        "checkpoint_sha256": image_profile.checkpoint_sha256,
                                        "device_uuid": image_profile.device_uuid,
                                    }
                                    if image_profile is not None
                                    else None
                                ),
                                "available_ram_gib": (
                                    sample.available_ram_gib
                                    if sample is not None
                                    else psutil.virtual_memory().available / 1024**3
                                ),
                                "available_vram_gib": (
                                    sample.available_vram_gib
                                    if sample is not None
                                    else None
                                ),
                                "queue_depth": sum(
                                    job.status.value == "queued" for job in jobs
                                ),
                                **store.recovery_counts(),
                                "jobs_by_status": {
                                    status: sum(
                                        job.status.value == status for job in jobs
                                    )
                                    for status in sorted(
                                        {job.status.value for job in jobs}
                                    )
                                },
                                "recent_failures": [
                                    {"job_id": job.id, "error_code": job.error_code}
                                    for job in jobs
                                    if job.error_code is not None
                                ][-10:],
                                "log_path": str(
                                    store.data_dir / "logs" / "diagnostics.jsonl"
                                ),
                            },
                            indent=2,
                        )
                    )
                    return 0
                tool_service = None
                if (
                    args.profile or args.command == "reconcile-file"
                ) and args.command in {
                    "send",
                    "session",
                    "resume-job",
                    "web",
                    "reconcile-file",
                }:
                    backend = SafeReadTools(
                        [Path(root) for root in args.read_root], store.data_dir
                    )
                    capabilities = {"clock"}
                    if args.read_root:
                        capabilities.add("read_files")
                    if network_enabled:
                        capabilities.add("network")
                    if browser_enabled:
                        capabilities.add("browser")
                    file_service = None
                    if args.write_root:
                        capabilities.add("write_files")
                        file_service = FileCreateService(
                            store,
                            SafeFileCreator(
                                [Path(root) for root in args.write_root], store.data_dir
                            ),
                        )
                    image_service = None
                    if image_profile is not None:
                        assert manager is not None
                        capabilities.add("image")
                        image_service = ImageService(
                            store,
                            ComfyImageProvider(image_runtime),
                            manager,
                            image_profile.admission_profile(),
                        )
                    web_backend = PublicPageFetcher() if network_enabled else None
                    browser_service = None
                    if browser_enabled:
                        assert (
                            manager is not None
                            and web_backend is not None
                            and browser_profile is not None
                        )
                        browser_service = BrowserService(
                            store,
                            web_backend,
                            SafeBrowserRenderer(
                                browser_profile,
                                store.data_dir / "run" / "browser",
                            ),
                            SystemRamProbe(),
                            manager,
                        )
                    tool_service = ToolService(
                        store,
                        backend,
                        ToolPolicy(
                            version=(
                                "m14-v1"
                                if browser_enabled
                                else "m13-v1"
                                if network_enabled
                                else "m9-v1"
                                if file_service is not None
                                else "m5-v1"
                            ),
                            capabilities=frozenset(capabilities),
                        ),
                        diagnostics,
                        image_service=image_service,
                        file_service=file_service,
                        web_backend=web_backend,
                        browser_service=browser_service,
                    )
                if args.command == "reconcile-file":
                    assert tool_service is not None
                    result = asyncio.run(
                        tool_service.reconcile_file(args.invocation_id)
                    )
                    print("reconciled" if result is not None else "unresolved")
                    return 0
                service = ConversationService(
                    store,
                    provider,
                    context=(
                        ContextBuilder(
                            PreflightTokenEstimator(),
                            output_allowance=preferences.output_tokens,
                        )
                        if manager
                        else None
                    ),
                    diagnostics=diagnostics,
                    resource_manager=manager,
                    runtime_profile=(profile.admission_profile() if profile else None),
                    tool_service=tool_service,
                    retrieval=retrieval,
                )
                profile_identity = (
                    ":".join(
                        (
                            profile.name,
                            profile.device_uuid,
                            str(profile.device_index),
                            profile.executable_sha256,
                            profile.model_sha256,
                        )
                    )
                    if profile is not None
                    else "fake"
                )
                vision_service = None
                if vision_profile is not None:
                    assert vision_runtime is not None and manager is not None
                    vision_identity = ":".join(
                        (
                            vision_profile.name,
                            vision_profile.device_uuid,
                            str(vision_profile.device_index),
                            vision_profile.executable_sha256,
                            vision_profile.model_sha256,
                            vision_profile.projector_sha256,
                        )
                    )
                    vision_service = VisionService(
                        store,
                        LlamaChatProvider(vision_runtime),
                        manager,
                        vision_profile.admission_profile(),
                        vision_identity,
                    )
                runner = JobRunner(store, service, profile_identity, vision_service)
                if args.command == "web":
                    if not 1024 <= args.port <= 65535:
                        raise ValueError("Web port must be between 1024 and 65535")
                    import uvicorn

                    from jarvis.interfaces.web import create_web_app

                    cpu_gate = CpuWorkerGate()
                    speech = None
                    if args.speech_profile:
                        speech_profile = SpeechProfile.from_file(
                            Path(args.speech_profile), store.data_dir
                        )
                        speech_profile.verify_artifacts()
                        speech = SpeechService(
                            CpuSpeechTranscriber(
                                speech_profile, store.data_dir / "run" / "speech"
                            ),
                            SystemRamProbe(),
                            manager,
                            cpu_gate,
                        )

                    spoken = None
                    if args.piper_profile:
                        piper_profile = PiperProfile.from_file(
                            Path(args.piper_profile), store.data_dir
                        )
                        piper_profile.verify_artifacts()
                        spoken = SpokenPlaybackService(
                            store,
                            CpuPiperSynthesizer(
                                piper_profile, store.data_dir / "run" / "piper"
                            ),
                            SystemRamProbe(),
                            manager,
                            cpu_gate,
                        )

                    app, setup_token = create_web_app(
                        service,
                        runner,
                        tool_service,
                        args.port,
                        speech,
                        spoken,
                        statistics=StatisticsService(store),
                        settings=SettingsService(
                            store,
                            model_name=profile.name if profile else "Тестовая модель",
                            active_output_tokens=service.context.output_allowance,
                            context_tokens=service.context.window,
                            active_network=network_enabled,
                            active_browser=browser_enabled,
                            browser_available=browser_profile is not None,
                            network_available=profile is not None,
                        ),
                    )
                    if web_control is None:
                        print(f"Open http://127.0.0.1:{args.port}/setup")
                        print(f"One-time setup token: {setup_token}", flush=True)

                    async def serve() -> None:
                        server = uvicorn.Server(
                            uvicorn.Config(
                                app,
                                host="127.0.0.1",
                                port=args.port,
                                workers=1,
                                reload=False,
                                access_log=False,
                                proxy_headers=False,
                                server_header=False,
                            )
                        )
                        try:
                            if web_control is None:
                                await server.serve()
                            else:
                                from jarvis.interfaces.web_control import (
                                    serve_controlled,
                                )

                                await serve_controlled(
                                    server,
                                    web_control,
                                    f"http://127.0.0.1:{args.port}/setup#token={setup_token}",
                                    sockets=[web_socket] if web_socket else None,
                                )
                        finally:
                            if spoken is not None:
                                await spoken.stop()
                            if speech is not None:
                                await speech.cancel()
                            await runner.shutdown()
                            if manager is not None:
                                await manager.close()

                    try:
                        asyncio.run(serve())
                    except KeyboardInterrupt:
                        pass
                    return 0
                return dispatch(args, service, manager, runner, retrieval)
            finally:
                diagnostics.close()
    except (
        ConversationError,
        ResourceError,
        ValueError,
        RuntimeError,
        OSError,
        sqlite3.Error,
    ) as exc:
        if web_control is None:
            print(f"error: {exc}", file=sys.stderr)
        else:
            web_control.error(str(exc))
        return 1


def desktop_main(argv: list[str]) -> int:
    """Wire a desktop controller to the same web command, with no extra server."""
    from tkinter import messagebox

    from jarvis.infrastructure.desktop_instance import DesktopInstance
    from jarvis.infrastructure.desktop_window import DesktopWindow
    from jarvis.interfaces.desktop import run_desktop

    args = parse_args(argv)
    if args.command != "web":
        raise ValueError("Desktop launcher supports only the web interface")
    instance = DesktopInstance(Path(args.data_dir))
    try:
        if not instance.acquired:
            if not instance.focus():
                messagebox.showinfo(
                    "Jarvis", "Jarvis уже запускается. Подождите немного."
                )
            return 0
        return run_desktop(
            lambda control: main(argv, web_control=control),
            instance.title,
            DesktopWindow(instance.title),
        )
    finally:
        instance.close()
