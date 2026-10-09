"""Build the five existing model profiles and their relocatable offline workers.

Developer-only: reads explicit model assets, Git's pinned source archive and
wheel RECORD entries. Never packages a data directory or worker venv wholesale.
"""

import argparse
import base64
import csv
import hashlib
import importlib.metadata
import io
import json
import re
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

from jarvis.infrastructure.component_pack import safe_name
from jarvis.infrastructure.image_profile import LocalImageProfile
from jarvis.infrastructure.local_profile import LocalTextProfile
from jarvis.infrastructure.piper_profile import PiperProfile
from jarvis.infrastructure.speech_profile import SpeechProfile
from jarvis.infrastructure.vision_profile import LocalVisionProfile

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "b0b743566f65daafc423b4fea8a2fbda94b3384a"


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def dependency_files(site: Path, lock: Path):
    expected = dict(
        line.split("==")
        for line in lock.read_text().splitlines()
        if line and not line.startswith("#")
    )
    expected = {normalized(name): version for name, version in expected.items()}
    found = {}
    files: dict[str, tuple[Path, str | None]] = {}
    for package in importlib.metadata.distributions(path=[str(site)]):
        name = normalized(package.metadata["Name"])
        if name not in expected or package.version != expected[name]:
            raise ValueError("Worker dependency differs from lock: " + name)
        found[name] = package.version
        record = package.read_text("RECORD")
        if record is None:
            raise ValueError("Missing wheel RECORD: " + name)
        for filename, digest, _ in csv.reader(io.StringIO(record)):
            filename = filename.replace("\\", "/")
            # Console entrypoints and venv metadata are deliberately not used.
            if filename.startswith("../") or filename.endswith(".pyc"):
                continue
            safe_name(filename)
            path = site / filename
            if path.is_symlink() or not path.resolve().is_relative_to(site.resolve()):
                raise ValueError("Redirected wheel file")
            if filename.endswith(".pth"):
                # Isolated Python does not process .pth; do not ship executable hooks.
                continue
            if digest:
                algorithm, _, encoded = digest.partition("=")
                if algorithm != "sha256":
                    raise ValueError("Unsupported wheel checksum")
                with path.open("rb") as stream:
                    actual = hashlib.file_digest(stream, "sha256").digest()
                if base64.urlsafe_b64encode(actual).decode().rstrip("=") != encoded:
                    raise ValueError("Modified wheel file: " + filename)
            elif not filename.endswith(
                ("/RECORD", "/INSTALLER", "/REQUESTED", "/direct_url.json")
            ):
                raise ValueError("Unverified wheel file: " + filename)
            # Installation provenance may contain build-machine absolute paths.
            if filename.endswith(("/direct_url.json", "/REQUESTED", "/INSTALLER")):
                continue
            files[filename] = (path, digest or None)
    if found != expected:
        raise ValueError("Incomplete worker environment")
    return {name: path for name, (path, _) in files.items()}


def build() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    data = args.assets.resolve(strict=True)
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / "dist") or output.exists():
        raise ValueError("Use a fresh archive inside project/dist")
    output.parent.mkdir(parents=True, exist_ok=True)
    entries: dict[str, Path | bytes] = {}

    def add(name: str, source: Path | bytes) -> None:
        safe_name(name)
        if name in entries:
            raise ValueError("Duplicate build input")
        entries[name] = source

    profile_types = [
        ("model.toml", "qwen3-4b-instruct-2507", LocalTextProfile),
        ("image.toml", "sd15-512", LocalImageProfile),
        ("vision.toml", "qwen25-vl-3b", LocalVisionProfile),
        ("speech.toml", "speech-base", SpeechProfile),
        ("piper.toml", "piper-norman", PiperProfile),
    ]
    for filename, example, profile_type in profile_types:
        profile = profile_type.from_file(data / filename, data)
        profile.verify_artifacts()
        template = (ROOT / "profiles" / (example + ".example.toml")).read_text(
            encoding="utf-8"
        )
        template = re.sub(
            r'device_uuid = "[^"]+"', 'device_uuid = "@DEVICE_UUID@"', template
        )
        template = template.replace("E:/JarvisData/", "")
        template = template.replace("/Scripts/python.exe", "/python.exe")
        template = template.replace(
            "comfyui-v0.39.0/.venv/python.exe", "image-worker/python.exe"
        )
        if filename == "image.toml":
            template += "\npackaged_source = true\n"
        add(filename, template.encode())
        values = next(iter(tomllib.loads(template).values()))
        for field in ("model", "projector", "checkpoint", "voice_model"):
            if field in values:
                add(values[field], data / values[field])
        if filename == "speech.toml":
            for name in (
                "model.bin",
                "tokenizer.json",
                "config.json",
                "vocabulary.txt",
            ):
                relative = values["model_dir"] + "/" + name
                add(relative, data / relative)
        if filename == "piper.toml":
            voice = Path(values["voice_model"])
            for relative in (
                str(voice).replace("\\", "/") + ".json",
                voice.with_name("MODEL_CARD").as_posix(),
            ):
                add(relative, data / relative)

    # Use the committed source tree, including its licence, never local untracked files.
    comfy = data / "runtimes/comfyui-v0.39.0"
    archive = subprocess.check_output(
        ["git", "-C", str(comfy), "archive", "--format=tar", COMMIT]
    )
    source_hashes = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        for member in source:
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError("Unsupported pinned source entry")
            stream = source.extractfile(member)
            assert stream is not None
            content = stream.read()
            source_hashes[member.name] = hashlib.sha256(content).hexdigest()
            add("runtimes/comfyui-v0.39.0/" + member.name, content)

    llama = data / "runtimes/llama-b11429/bin"
    for source in sorted(llama.iterdir()):
        if (
            source.suffix == ".dll"
            or source.name == "llama-server.exe"
            or source.name.startswith("LICENSE")
        ):
            add("runtimes/llama-b11429/bin/" + source.name, source)

    base = Path(sys.base_prefix)
    excluded = {
        "site-packages",
        "__pycache__",
        "test",
        "tests",
        "tcl",
        "tkinter",
        "idlelib",
        "ensurepip",
        "Scripts",
        "include",
        "libs",
    }
    python_files = {
        path.relative_to(base).as_posix(): path
        for path in base.rglob("*")
        if path.is_file()
        and not (set(path.relative_to(base).parts) & excluded)
        and path.suffix != ".pyc"
    }
    for target, environment, lock in (
        ("image-worker", comfy / ".venv", "image-worker"),
        ("speech-base", data / "runtimes/speech-base", "speech-worker"),
        ("piper-norman", data / "runtimes/piper-norman", "piper-worker"),
    ):
        prefix = "runtimes/" + target + "/"
        for name, source in python_files.items():
            add(prefix + name, source)
        add(prefix + "python312._pth", b".\nLib\nDLLs\nLib/site-packages\n")
        for name, source in dependency_files(
            environment / "Lib/site-packages", ROOT / "docs" / (lock + ".lock.txt")
        ).items():
            add(prefix + "Lib/site-packages/" + name, source)
        add("notices/" + lock + ".lock.txt", ROOT / "docs" / (lock + ".lock.txt"))

    manifest = {"format": 1, "id": "jarvis-existing-models-win-x64-v1", "files": {}}
    metadata = manifest["files"]
    temporary = output.with_suffix(output.suffix + ".building")
    if temporary.exists():
        raise FileExistsError("Previous build is present")
    with zipfile.ZipFile(
        temporary,
        "x",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=1,
        allowZip64=True,
    ) as package:
        for index, (name, source) in enumerate(sorted(entries.items())):
            digest = hashlib.sha256()
            size = 0
            with (
                (
                    source.open("rb")
                    if isinstance(source, Path)
                    else io.BytesIO(source)
                ) as stream,
                package.open(name, "w", force_zip64=True) as target,
            ):
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                    target.write(chunk)
                    size += len(chunk)
            metadata[name] = {"size": size, "sha256": digest.hexdigest()}
            if index % 2000 == 0:
                print(json.dumps({"files": index, "total": len(entries)}), flush=True)
        raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        package.writestr("manifest.json", raw)
    temporary.rename(output)
    assets = ROOT / "src/jarvis/infrastructure/assets"
    assets.mkdir(exist_ok=True)
    (assets / "comfy-source.json").write_text(
        json.dumps({"commit": COMMIT, "files": source_hashes}, sort_keys=True),
        encoding="utf-8",
    )
    catalog = {
        "id": manifest["id"],
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "unpacked_bytes": sum(item["size"] for item in metadata.values()),
        "filename": output.name,
    }
    (assets / "model-pack.json").write_text(
        json.dumps(catalog, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(catalog), flush=True)


if __name__ == "__main__":
    build()
