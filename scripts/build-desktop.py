"""Build a relocatable Windows application; never copy user data or models."""

import argparse
import json
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def build() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dotnet", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    version = ET.parse(root / "desktop/Jarvis.Desktop/Jarvis.Desktop.csproj").findtext(
        ".//Version"
    )
    output = args.output.resolve()
    if not output.is_relative_to(root / "dist") or output == root / "dist":
        raise ValueError("Output must be a new directory inside project/dist")
    if output.exists():
        raise FileExistsError("Use a fresh versioned output directory")
    dotnet = args.dotnet.resolve(strict=True)
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("Build machine requires uv")
    work = root / "run" / "desktop-build"
    work.mkdir(parents=True, exist_ok=True)
    requirements = work / "requirements.txt"
    subprocess.run(
        [
            uv,
            "export",
            "--locked",
            "--no-dev",
            "--extra",
            "browser",
            "--no-emit-project",
            "--output-file",
            str(requirements),
        ],
        cwd=root,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(work)], cwd=root, check=True
    )
    subprocess.run(
        [
            str(dotnet),
            "publish",
            "Jarvis.Desktop/Jarvis.Desktop.csproj",
            "-c",
            "Release",
            "-r",
            "win-x64",
            "--self-contained",
            "true",
            "-p:RestoreLockedMode=true",
            "-o",
            str(output),
            "--nologo",
        ],
        cwd=root / "desktop",
        check=True,
    )
    backend = output / "backend"
    # Managed CPython is copied as a private runtime, not a machine-specific venv.
    # Retain its licenses and native libraries; omit development/cache/GUI extras.
    shutil.copytree(
        Path(sys.base_prefix),
        backend,
        ignore=shutil.ignore_patterns(
            "site-packages",
            "__pycache__",
            "*.pyc",
            "test",
            "tests",
            "tcl",
            "tkinter",
            "idlelib",
            "ensurepip",
            "Scripts",
        ),
    )
    site = backend / "Lib" / "site-packages"
    wheel = next(work.glob("jarvis-*.whl"))
    subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            sys.executable,
            "--target",
            str(site),
            "-r",
            str(requirements),
            str(wheel),
        ],
        cwd=root,
        check=True,
    )
    # Keep installed dependencies isolated from PYTHONPATH/user site packages.
    (backend / "python312._pth").write_text(
        ".\nLib\nDLLs\nLib/site-packages\n", encoding="utf-8"
    )
    subprocess.run(
        [
            str(backend / "python.exe"),
            "-c",
            "import jarvis.desktop_backend; import psutil; print('Packaged backend ready')",
        ],
        cwd=output,
        check=True,
    )
    (output / "build-info.json").write_text(
        json.dumps(
            {
                "application": "Jarvis",
                "version": version,
                "protocol": 1,
                "python": sys.version.split()[0],
                "models_included": False,
                "data_included": False,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(output)


if __name__ == "__main__":
    build()
