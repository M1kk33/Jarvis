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
from jarvis.application.jobs import JobRunner
from jarvis.application.resource_manager import ResourceManager
from jarvis.application.tools import ToolPolicy, ToolService
from jarvis.domain.ports import ChatProvider
from jarvis.domain.resources import ResourceError
from jarvis.domain.types import ConversationError
from jarvis.infrastructure.backup import create_backup, restore_backup
from jarvis.infrastructure.diagnostics import Diagnostics
from jarvis.infrastructure.gpu_lock import NamedGpuLock
from jarvis.infrastructure.local_profile import LocalTextProfile
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
                probe = None
                if args.command == "calibrate" and not args.profile:
                    raise ValueError("Calibration requires --profile")
                if args.read_root and (
                    not args.profile
                    or args.command not in {"send", "session", "resume-job"}
                ):
                    raise ValueError(
                        "Read tools require a model session or send command"
                    )
                if args.profile and args.command in {
                    "send",
                    "session",
                    "calibrate",
                    "resume-job",
                    "diagnostics",
                }:
                    profile = LocalTextProfile.from_file(
                        Path(args.profile), store.data_dir
                    )
                    runtime = LlamaServerRuntime(profile, store.data_dir / "run")
                    probe = NvidiaResourceProbe(
                        profile.device_uuid, profile.device_index
                    )
                    manager = ResourceManager(
                        runtime,
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
                if args.profile and args.command in {"send", "session", "resume-job"}:
                    backend = SafeReadTools(
                        [Path(root) for root in args.read_root], store.data_dir
                    )
                    capabilities = {"clock"}
                    if args.read_root:
                        capabilities.add("read_files")
                    tool_service = ToolService(
                        store,
                        backend,
                        ToolPolicy(capabilities=frozenset(capabilities)),
                        diagnostics,
                    )
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
                return dispatch(
                    args,
                    service,
                    manager,
                    JobRunner(store, service, profile_identity),
                )
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
