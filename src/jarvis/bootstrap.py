"""Wire the conversation application to its store, fake provider, and terminal UI."""

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

from jarvis.application.calibration import calibrate
from jarvis.application.context import ContextBuilder, PreflightTokenEstimator
from jarvis.application.conversations import ConversationService
from jarvis.application.resource_manager import ResourceManager
from jarvis.application.tools import ToolPolicy, ToolService
from jarvis.domain.ports import ChatProvider
from jarvis.domain.resources import ResourceError
from jarvis.domain.types import ConversationError
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
                    not args.profile or args.command not in {"send", "session"}
                ):
                    raise ValueError(
                        "Read tools require a model session or send command"
                    )
                if args.profile and args.command in {"send", "session", "calibrate"}:
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
                tool_service = None
                if args.profile and args.command in {"send", "session"}:
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
                return dispatch(args, service, manager)
            finally:
                diagnostics.close()
    except (
        ConversationError,
        ResourceError,
        ValueError,
        RuntimeError,
        sqlite3.Error,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
