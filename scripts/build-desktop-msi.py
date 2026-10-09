"""Build a per-user Windows Installer package using the Windows MSI/FCI APIs.

Build-only Python 3.12 msilib is required; it is not used by the application.
The package owns only application files. Models, settings and chat data are external.
"""

import argparse
import ctypes
import hashlib
import json
import uuid
import warnings
from pathlib import Path

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import msilib
    from msilib import schema, sequence

UPGRADE = "{6D7EA984-78C7-4582-A46B-6C364594060E}"
NAMESPACE = uuid.UUID(UPGRADE)


def file_version(path: Path) -> tuple[str | None, str | None]:
    """MSI must compare the real PE version, not treat versioned binaries as 0."""
    get_version = ctypes.WinDLL("msi").MsiGetFileVersionW
    get_version.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_uint),
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_uint),
    ]
    get_version.restype = ctypes.c_uint
    version, language = (
        ctypes.create_unicode_buffer(256),
        ctypes.create_unicode_buffer(256),
    )
    version_size, language_size = ctypes.c_uint(256), ctypes.c_uint(256)
    result = get_version(
        str(path),
        version,
        ctypes.byref(version_size),
        language,
        ctypes.byref(language_size),
    )
    if result == 1006:  # ERROR_FILE_INVALID: an unversioned data file.
        return None, None
    if result:
        raise OSError(result, "Cannot read MSI file version")
    return version.value or None, language.value or None


def identifier(path: str) -> str:
    return "F" + hashlib.sha256(path.encode()).hexdigest()[:30]


def build() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("application", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--version")
    args = parser.parse_args()
    source = args.application.resolve(strict=True)
    if args.version is None:
        args.version = json.loads((source / "build-info.json").read_text())["version"]
    target = args.output.resolve()
    if target.exists():
        raise FileExistsError("Use a fresh output path")
    if not (source / "Jarvis.exe").is_file():
        raise ValueError("A published application is required")
    target.parent.mkdir(parents=True, exist_ok=True)
    db = msilib.init_database(
        str(target), schema, "Jarvis Preview", msilib.gen_uuid(), args.version, "Jarvis"
    )
    msilib.add_tables(db, sequence)
    # Per-user installation. Do not require elevation or change shared runtimes.
    summary = db.GetSummaryInformation(20)
    summary.SetProperty(msilib.PID_WORDCOUNT, 10)
    summary.Persist()
    msilib.add_data(
        db,
        "Property",
        [
            ("UpgradeCode", UPGRADE),
            ("ARPNOMODIFY", "1"),
            ("ARPNOREPAIR", "1"),
            ("SecureCustomProperties", "OLDPRODUCTS;NEWERPRODUCTS"),
        ],
    )
    msilib.add_data(
        db,
        "Upgrade",
        [
            (UPGRADE, None, args.version, None, 1, None, "OLDPRODUCTS"),
            (UPGRADE, args.version, None, None, 258, None, "NEWERPRODUCTS"),
        ],
    )
    # Related products must be discovered before checking downgrade conditions.
    for table in ("InstallExecuteSequence", "InstallUISequence"):
        view = db.OpenView(
            f"UPDATE `{table}` SET `Sequence`=250 WHERE `Action`='LaunchConditions'"
        )
        view.Execute(None)
    msilib.add_data(
        db,
        "LaunchCondition",
        [
            ("VersionNT64", "Jarvis requires 64-bit Windows."),
            ("NOT NEWERPRODUCTS", "A newer version of Jarvis Preview is installed."),
        ],
    )
    # Remove old files inside the transaction, before new files are installed.
    view = db.OpenView(
        "UPDATE `InstallExecuteSequence` SET `Sequence`=1510 WHERE `Action`='RemoveExistingProducts'"
    )
    view.Execute(None)
    # No custom UI dialogs are authored: Windows Installer supplies its basic UI.
    view = db.OpenView("DELETE FROM `InstallUISequence` WHERE `Sequence`<0")
    view.Execute(None)
    feature = msilib.Feature(
        db, "Application", "Jarvis", "Desktop application", 1, directory="INSTALLDIR"
    )
    feature.set_current()
    cab = msilib.CAB("JarvisPayload")
    root = msilib.Directory(db, cab, None, str(source), "TARGETDIR", "SourceDir")
    local = msilib.Directory(db, cab, root, str(source), "LocalAppDataFolder", ".")
    programs = msilib.Directory(
        db, cab, local, str(source), "LocalPrograms", "Programs"
    )
    install = msilib.Directory(
        db, cab, programs, str(source), "INSTALLDIR", "JARVIS~1|Jarvis Preview"
    )
    folders = {Path(): install}
    executable = None
    for path in sorted(source.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ValueError("Symlinks are not allowed in the distribution")
        relative = path.relative_to(source)
        for parent in reversed(relative.parents):
            if parent in folders:
                continue
            ancestor = folders[parent.parent]
            folders[parent] = msilib.Directory(
                db,
                cab,
                ancestor,
                str(source / parent),
                "D" + identifier(parent.as_posix()),
                ancestor.make_short(parent.name) + "|" + parent.name,
            )
        directory = folders[relative.parent]
        component = "C" + identifier(relative.as_posix())
        directory.start_component(
            component,
            feature,
            0,
            path.name,
            "{" + str(uuid.uuid5(NAMESPACE, relative.as_posix())) + "}",
        )
        version, language = file_version(path)
        file_id = directory.add_file(path.name, version=version, language=language)
        if relative.as_posix() == "Jarvis.exe":
            executable = (component, file_id)
    assert executable is not None
    msilib.add_data(db, "Directory", [("ProgramMenuFolder", "TARGETDIR", ".")])
    msilib.add_data(
        db,
        "Shortcut",
        [
            (
                "JarvisShortcut",
                "ProgramMenuFolder",
                "JARVIS~1|Jarvis Preview",
                executable[0],
                "[#" + executable[1] + "]",
                None,
                "Jarvis local assistant",
                None,
                None,
                None,
                1,
                "INSTALLDIR",
            ),
        ],
    )
    cab.commit(db)
    db.Commit()
    print(target)


if __name__ == "__main__":
    build()
