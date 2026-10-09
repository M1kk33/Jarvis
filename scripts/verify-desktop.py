"""Run the installed host against an isolated fake database and verify tree exit."""

import argparse
import json
import os
import time
from pathlib import Path

import psutil

from jarvis.infrastructure.windows_process import WindowsProcessSupervisor


def verify() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("application", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--component-pack", type=Path)
    parser.add_argument("--temp-root", type=Path)
    args = parser.parse_args()
    application = args.application.resolve(strict=True)
    report = args.report.resolve()
    if report.exists():
        raise FileExistsError("Use a fresh verification report")
    report.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    # A packaged app must not depend on the development SDK or interpreter paths.
    for name in ("DOTNET_ROOT", "PYTHONPATH", "PYTHONHOME", "__PYVENV_LAUNCHER__"):
        env.pop(name, None)
    env["PATH"] = os.environ["SystemRoot"] + "\\System32"
    arguments = ["--smoke-test", str(report)]
    if args.temp_root:
        temporary = args.temp_root.resolve(strict=True)
        env["TEMP"] = env["TMP"] = str(temporary)
    if args.component_pack:
        arguments += [
            "--setup-smoke-pack",
            str(args.component_pack.resolve(strict=True)),
        ]
    owned = WindowsProcessSupervisor().spawn(application, arguments, report.parent, env)
    peak = 0
    try:
        deadline = time.monotonic() + (1800 if args.component_pack else 90)
        while not owned.root_exited() and time.monotonic() < deadline:
            try:
                root = psutil.Process(owned.pid)
                members = [root, *root.children(recursive=True)]
                peak = max(peak, sum(p.memory_info().private for p in members))
            except psutil.Error:
                pass
            time.sleep(0.1)
        if not owned.root_exited():
            raise TimeoutError(
                "Native host did not complete; owned tree will be stopped"
            )
        if not owned.verify_stopped():
            raise RuntimeError("Native host left descendants")
        result = json.loads(report.read_text(encoding="utf-8-sig"))
        if not result.get("success"):
            raise RuntimeError(result)
        result["peak_private_mib"] = round(peak / 1024**2, 1)
        result["verified_tree_exit"] = True
        report.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result))
    finally:
        if not owned.stop(0, 10):
            raise RuntimeError("Cannot verify cleanup of test processes")


if __name__ == "__main__":
    verify()
