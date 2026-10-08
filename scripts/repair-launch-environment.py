"""Record the real interpreter home so shortcuts work outside a packaged editor."""

import os
import sys
import tomllib
from pathlib import Path
from uuid import uuid4

import psutil


def repair_home(config: Path, physical_home: Path) -> bool:
    if not all(
        (physical_home / name).is_file() for name in ("python.exe", "pythonw.exe")
    ):
        raise RuntimeError(
            "The physical Python installation lacks a required executable"
        )
    original = config.read_bytes()
    lines = original.decode("utf-8-sig").splitlines(keepends=True)
    matches = [
        i for i, line in enumerate(lines) if line.split("=", 1)[0].strip() == "home"
    ]
    if len(matches) != 1:
        raise RuntimeError("Expected one interpreter home in pyvenv.cfg")
    index = matches[0]
    if lines[index].split("=", 1)[1].strip() == str(physical_home):
        return False
    ending = "\r\n" if lines[index].endswith("\r\n") else "\n"
    lines[index] = f"home = {physical_home}{ending}"
    temporary = config.with_name(f".jarvis-python-{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as output:
            output.write("".join(lines).encode("utf-8"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, config)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def repair_workers(data_dir: Path) -> None:
    """Resolve only interpreters explicitly referenced by installed worker profiles."""
    for filename, section in (
        ("image.toml", "image_model"),
        ("speech.toml", "speech"),
        ("piper.toml", "piper"),
    ):
        profile = data_dir / filename
        if not profile.is_file():
            continue
        values = tomllib.loads(profile.read_text(encoding="utf-8"))[section]
        interpreter = Path(values["python"])
        if not interpreter.is_absolute():
            interpreter = data_dir / interpreter
        config = interpreter.parent.parent / "pyvenv.cfg"
        if not config.is_file():
            continue  # An explicitly configured system Python has no venv home.
        homes = [
            line.split("=", 1)[1].strip()
            for line in config.read_text(encoding="utf-8-sig").splitlines()
            if line.split("=", 1)[0].strip() == "home"
        ]
        if len(homes) != 1:
            raise RuntimeError("Expected one worker interpreter home")
        # resolve(strict=True) follows Store AppData redirection to the actual
        # installation. Keep this worker's interpreter, not the core version.
        physical = (Path(homes[0]) / "python.exe").resolve(strict=True).parent
        changed = repair_home(config, physical)
        print(f"{filename}: Python home {'repaired' if changed else 'verified'}")


if __name__ == "__main__":
    if sys.prefix == sys.base_prefix:
        raise RuntimeError("Run this helper through the project virtual environment")
    # Native process telemetry returns the image's real path. sys.base_prefix
    # may instead name an AppData path visible only inside a Store application.
    physical_home = Path(psutil.Process().exe()).parent
    changed = repair_home(Path(sys.prefix) / "pyvenv.cfg", physical_home)
    print("Python home repaired" if changed else "Python home already verified")
    if "--worker-profiles" in sys.argv:
        repair_workers(
            Path(sys.argv[sys.argv.index("--worker-profiles") + 1]).resolve(strict=True)
        )
