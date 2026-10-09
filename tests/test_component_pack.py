"""First-install transactions and the trusted archive boundary, without models."""

import hashlib
import json
import stat
import subprocess
import zipfile
from threading import Event

import pytest

from jarvis.infrastructure import component_pack as pack
from jarvis.infrastructure.image_profile import LocalImageProfile

GPU = "GPU-bfa7eedf-5d52-9ccf-76e2-86b3891ab4d7"


def archive(tmp_path, *, extra=None, mutate=None):
    files = {name: b'device_uuid = "@DEVICE_UUID@"\n' for name in pack.PROFILES}
    files["runtimes/worker/python.exe"] = b"test fixture, never executed"
    if extra:
        files.update(extra)
    manifest = {
        "format": 1,
        "id": "test",
        "files": {
            name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in files.items()
        },
    }
    raw = json.dumps(manifest).encode()
    catalog = {
        "id": "test",
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "unpacked_bytes": sum(len(data) for data in files.values()),
    }
    if mutate:
        mutate(files)
    path = tmp_path / "models.zip"
    with zipfile.ZipFile(path, "w") as result:
        result.writestr("manifest.json", raw)
        for name, data in files.items():
            result.writestr(name, data)
    return path, catalog


def run_install(tmp_path, source, catalog, *, cancel=None, progress=lambda *_: None):
    destination = tmp_path / "new-data"
    pack.install_pack(source, destination, catalog, GPU, cancel or Event(), progress)
    return destination


def assert_no_partial(tmp_path):
    assert not (tmp_path / "new-data").exists()
    assert not list(tmp_path.glob(".jarvis-install-*"))


def test_install_publishes_only_verified_files_with_current_device(tmp_path):
    source, catalog = archive(tmp_path)
    updates = []
    destination = run_install(
        tmp_path, source, catalog, progress=lambda *p: updates.append(p)
    )
    assert GPU in (destination / "model.toml").read_text()
    assert "@DEVICE_UUID@" not in (destination / "model.toml").read_text()
    assert updates[-1] == (catalog["unpacked_bytes"], catalog["unpacked_bytes"])
    assert (destination / "installed-components.json").is_file()
    assert not list(tmp_path.glob(".jarvis-install-*"))


def test_existing_data_never_overwritten(tmp_path):
    source, catalog = archive(tmp_path)
    destination = tmp_path / "new-data"
    destination.mkdir()
    history = destination / "conversations.sqlite3"
    history.write_bytes(b"precious history")
    with pytest.raises(FileExistsError):
        run_install(tmp_path, source, catalog)
    assert history.read_bytes() == b"precious history"
    assert list(destination.iterdir()) == [history]


def test_catalog_is_not_taken_from_archive(tmp_path):
    source, catalog = archive(tmp_path)
    catalog["manifest_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="does not match"):
        run_install(tmp_path, source, catalog)
    assert_no_partial(tmp_path)


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/escape",
        "x/../../escape",
        "x\\escape",
        "C:/escape",
        "x:stream",
        "CON",
        "x/nul.txt",
        "x/file.",
        "x/file ",
        "x//y",
    ],
)
def test_unsafe_paths_rejected_before_extraction(tmp_path, name):
    source, catalog = archive(tmp_path, extra={name: b"bad"})
    with pytest.raises(ValueError):
        run_install(tmp_path, source, catalog)
    assert_no_partial(tmp_path)


def test_windows_case_alias_rejected(tmp_path):
    source, catalog = archive(tmp_path, extra={"MODEL.toml": b"ambiguous"})
    with pytest.raises(ValueError, match="Duplicate"):
        run_install(tmp_path, source, catalog)
    assert_no_partial(tmp_path)


@pytest.mark.parametrize("change", ["corrupt", "extra", "size"])
def test_damaged_or_extra_file_rolls_back(tmp_path, change):
    def mutate(files):
        if change == "extra":
            files["evil.py"] = b"unlisted"
        elif change == "size":
            files["model.toml"] += b"extra"
        else:
            files["model.toml"] = b"x" * len(files["model.toml"])

    source, catalog = archive(tmp_path, mutate=mutate)
    with pytest.raises(ValueError):
        run_install(tmp_path, source, catalog)
    assert_no_partial(tmp_path)


def test_cancellation_during_copy_removes_only_own_staging(tmp_path):
    source, catalog = archive(tmp_path, extra={"large.bin": b"x" * (pack.CHUNK * 2)})
    unrelated = tmp_path / ".jarvis-install-unrelated"
    unrelated.mkdir()
    sentinel = unrelated / "keep"
    sentinel.write_text("keep")
    cancel = Event()

    def progress(done, _):
        if done:
            cancel.set()

    with pytest.raises(pack.SetupCancelled):
        run_install(tmp_path, source, catalog, cancel=cancel, progress=progress)
    assert not (tmp_path / "new-data").exists()
    assert list(tmp_path.glob(".jarvis-install-*")) == [unrelated]
    assert sentinel.read_text() == "keep"


def test_disk_space_checked_before_any_unpack(tmp_path, monkeypatch):
    source, catalog = archive(tmp_path)
    monkeypatch.setattr(
        pack.shutil, "disk_usage", lambda _: type("Usage", (), {"free": 0})()
    )
    with pytest.raises(OSError, match="disk space"):
        run_install(tmp_path, source, catalog)
    assert_no_partial(tmp_path)


def test_new_destination_appearing_during_copy_is_preserved(tmp_path):
    source, catalog = archive(tmp_path)

    def progress(done, total):
        if done == total:
            (tmp_path / "new-data").mkdir()

    with pytest.raises(FileExistsError):
        run_install(tmp_path, source, catalog, progress=progress)
    assert (tmp_path / "new-data").is_dir()
    assert not list(tmp_path.glob(".jarvis-install-*"))


def test_symlink_entry_rejected(tmp_path):
    source, catalog = archive(tmp_path)
    with zipfile.ZipFile(source, "a") as result:
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        result.writestr(info, "../outside")
    with pytest.raises(ValueError):
        run_install(tmp_path, source, catalog)
    assert_no_partial(tmp_path)


def test_packaged_source_uses_application_hashes_without_git(tmp_path, monkeypatch):
    assets = tmp_path / "assets"
    assets.mkdir()
    root = tmp_path / "comfy"
    root.mkdir()
    source = root / "main.py"
    source.write_bytes(b"verified source")
    (assets / "comfy-source.json").write_text(
        json.dumps(
            {
                "commit": "pinned",
                "files": {"main.py": hashlib.sha256(source.read_bytes()).hexdigest()},
            }
        )
    )
    monkeypatch.setattr(pack, "ASSETS", assets)
    pack.verify_comfy_source(root, "pinned")
    source.write_bytes(b"tampered source")
    with pytest.raises(ValueError, match="modified"):
        pack.verify_comfy_source(root, "pinned")


@pytest.mark.parametrize("packaged", [False, True])
def test_existing_and_packaged_image_profiles_verify_without_subprocess(
    tmp_path, monkeypatch, packaged
):
    assets = tmp_path / "assets"
    assets.mkdir()
    root = tmp_path / "comfy"
    root.mkdir()
    source = root / "main.py"
    source.write_bytes(b"pinned source")
    checkpoint = root / "models/checkpoints/test.safetensors"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(b"small test checkpoint")
    python = tmp_path / "python.exe"
    python.touch()
    commit = "b0b743566f65daafc423b4fea8a2fbda94b3384a"
    (assets / "comfy-source.json").write_text(
        json.dumps(
            {
                "commit": commit,
                "files": {"main.py": hashlib.sha256(source.read_bytes()).hexdigest()},
            }
        )
    )
    monkeypatch.setattr(pack, "ASSETS", assets)

    def no_subprocess(*args, **kwargs):
        pytest.fail("Image verification must never spawn Git or inherit control pipes")

    monkeypatch.setattr(subprocess, "Popen", no_subprocess)
    # Construct a tiny fixture instead of allocating the pinned multi-GiB model.
    profile = LocalImageProfile.model_construct(
        runtime_commit=commit,
        root=root,
        python=python,
        checkpoint=checkpoint,
        checkpoint_sha256=hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
        packaged_source=packaged,
    )
    profile.verify_artifacts()
    source.write_bytes(b"modified source")
    with pytest.raises(ValueError, match="modified"):
        profile.verify_artifacts()
