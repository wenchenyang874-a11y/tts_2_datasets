"""Write version resources and archive only the built runtime, never user data."""
import argparse
import hashlib
import json
import runpy
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
version_module = runpy.run_path(str(ROOT / "app_version.py"))
VERSION = version_module["VERSION"]
NUMERIC = version_module["windows_version"]()
PREFIX = f"TTS-Dataset-Studio-{VERSION}-windows-x64"
APP_ID = "{C8EF7556-B9EA-44AF-96C4-5982E4212033}"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare():
    build = ROOT / "build"
    build.mkdir(exist_ok=True)
    strings = {"CompanyName": "TTS Dataset Studio", "FileDescription": "TTS Dataset Studio",
               "FileVersion": VERSION, "ProductVersion": VERSION, "ProductName": "TTS Dataset Studio",
               "OriginalFilename": "TTS Dataset Studio.exe"}
    # PyInstaller evaluates this Python resource description, so repr escapes
    # all strings; it is generated exclusively from validated source constants.
    resource = f"VSVersionInfo(ffi=FixedFileInfo(filevers={NUMERIC!r}, prodvers={NUMERIC!r}, mask=0x3f, flags=0, OS=0x40004, fileType=1, subtype=0, date=(0, 0)), kids=[StringFileInfo([StringTable('040904B0', ["
    resource += ",".join(f"StringStruct({key!r}, {value!r})" for key, value in strings.items())
    resource += "])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])])\n"
    (build / "version_info.txt").write_text(resource, encoding="utf-8")
    print(json.dumps({"version": VERSION, "numeric": ".".join(map(str, NUMERIC)), "prefix": PREFIX, "app_id": APP_ID}))


def package():
    bundle = ROOT / "dist/TTS Dataset Studio"
    output = ROOT / "dist/releases" / VERSION
    setup = output / f"{PREFIX}-setup.exe"
    if not setup.is_file() or not (bundle / "TTS Dataset Studio.exe").is_file():
        raise RuntimeError("Build the runtime and installer first")
    archive = output / f"{PREFIX}-portable.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as writer:
        for path in sorted(bundle.rglob("*")):
            if path.is_symlink():
                raise RuntimeError("Runtime bundle cannot contain links")
            if path.is_file():
                writer.write(path, path.relative_to(bundle.parent).as_posix())
    entries = [{"name": path.name, "size": path.stat().st_size, "sha256": digest(path)} for path in (setup, archive)]
    checksums = output / "SHA256SUMS.txt"
    checksums.write_text("".join(f"{item['sha256']}  {item['name']}\n" for item in entries), encoding="utf-8")
    entries.append({"name": checksums.name, "size": checksums.stat().st_size, "sha256": digest(checksums)})
    manifest = {"version": VERSION, "tag": f"v{VERSION}", "app_id": APP_ID,
                "numeric_version": ".".join(map(str, NUMERIC)), "prerelease": "-" in VERSION,
                "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "assets": entries}
    (output / "release-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["prepare", "package"])
    args = parser.parse_args()
    prepare() if args.operation == "prepare" else package()
