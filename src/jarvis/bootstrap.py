"""Wire the conversation application to its store, fake provider, and terminal UI."""

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import psutil

from jarvis.application.calibration import calibrate
from jarvis.application.context import ContextBuilder, PreflightTokenEstimator
from jarvis.application.conversations import ConversationService
from jarvis.application.file_creation import FileCreateService
from jarvis.application.images import ImageService
from jarvis.application.jobs import JobRunner
from jarvis.application.resource_manager import ResourceManager, RuntimeRouter
from jarvis.application.tools import ToolPolicy, ToolService
from jarvis.domain.ports import ChatProvider
from jarvis.domain.resource_ports import ManagedRuntime
from jarvis.domain.resources import ResourceError
from jarvis.domain.types import ConversationError
from jarvis.infrastructure.backup import create_backup, restore_backup
from jarvis.infrastructure.create_tools import SafeFileCreator
from jarvis.infrastructure.diagnostics import Diagnostics
from jarvis.infrastructure.gpu_lock import NamedGpuLock
from jarvis.infrastructure.image_profile import LocalImageProfile
from jarvis.infrastructure.local_profile import LocalTextProfile
from jarvis.infrastructure.providers.comfyui import ComfyImageProvider, ComfyRuntime
from jarvis.infrastructure.providers.fake import FakeChatProvider
from jarvis.infrastructure.providers.llama_cpp import (
    LlamaChatProvider,
    LlamaServerRuntime,
)
from jarvis.infrastructure.read_tools import SafeReadTools
from jarvis.infrastructure.resource_probe import NvidiaResourceProbe, SystemClock
from jarvis.infrastructure.sqlite_store import SqliteStore
from jarvis.interfaces.cli import dispatch, parse_args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "restore":
            print(restore_backup(Path(args.backup_dir), Path(args.data_dir)))
            return 0
        with SqliteStore(Path(args.data_dir)) as store:
            diagnostics = Diagnostics(store.data_dir)
            try:
                manager = None
                provider: ChatProvider = FakeChatProvider()
                profile: LocalTextProfile | None = None
                image_profile: LocalImageProfile | None = None
                probe = None
                if args.image_profile and not args.profile:
                    raise ValueError("Image profile requires the pinned text profile")
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
                    probe = NvidiaResourceProbe(
                        profile.device_uuid, profile.device_index
                    )
                    manager = ResourceManager(
                        RuntimeRouter(runtimes),
                        probe,
                        NamedGpuLock(profile.device_uuid),
                        SystemClock(),
                    )
                    provider = LlamaChatProvider(runtime)
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
                    tool_service = ToolService(
                        store,
                        backend,
                        ToolPolicy(
                            version="m9-v1" if file_service is not None else "m5-v1",
                            capabilities=frozenset(capabilities),
                        ),
                        diagnostics,
                        image_service=image_service,
                        file_service=file_service,
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
                        ContextBuilder(PreflightTokenEstimator()) if manager else None
                    ),
                    diagnostics=diagnostics,
                    resource_manager=manager,
                    runtime_profile=(profile.admission_profile() if profile else None),
                    tool_service=tool_service,
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
                runner = JobRunner(store, service, profile_identity)
                if args.command == "web":
                    if not 1024 <= args.port <= 65535:
                        raise ValueError("Web port must be between 1024 and 65535")
                    import uvicorn

                    from jarvis.interfaces.web import create_web_app

                    app, setup_token = create_web_app(
                        service, runner, tool_service, args.port
                    )
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
                            await server.serve()
                        finally:
                            await runner.wait_for_idle()
                            if manager is not None:
                                await manager.close()

                    try:
                        asyncio.run(serve())
                    except KeyboardInterrupt:
                        pass
                    return 0
                return dispatch(args, service, manager, runner)
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
        print(f"error: {exc}", file=sys.stderr)
        return 1
