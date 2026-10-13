"""Collect installed distributions' notices and exact versions for local builds."""
import importlib.metadata
import json
import shutil
import sys
import subprocess
import os
from pathlib import Path

output = Path(sys.argv[1])
notices = output / "licenses"
notices.mkdir(parents=True, exist_ok=True)
inventory = []
for distribution in importlib.metadata.distributions():
    name = distribution.metadata["Name"]
    if name.lower() in {"pytest", "pyinstaller", "pyinstaller-hooks-contrib", "iniconfig", "pluggy", "pygments", "altgraph", "pefile", "pywin32-ctypes", "setuptools", "pip"}:
        continue
    inventory.append({"name": name, "version": distribution.version})
    for filename in distribution.files or []:
        if any(word in str(filename).lower() for word in ("license", "copying", "notice")):
            source = Path(distribution.locate_file(filename))
            if source.is_file():
                safe_parts = [part for part in filename.parts if part not in ("..", ".", "", "/", "\\")]
                destination = notices / name / Path(*safe_parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
(output / "dependencies.json").write_text(json.dumps(inventory, indent=2), encoding="utf-8")
root = Path(__file__).resolve().parents[1]
shutil.copyfile(root / "docs/product/windows-quickstart.md", output / "使用说明.md")
import imageio_ffmpeg
binary = imageio_ffmpeg.get_ffmpeg_exe()
flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
for option, filename in [("-L", "FFMPEG-LICENSE.txt"), ("-version", "FFMPEG-VERSION.txt"), ("-buildconf", "FFMPEG-BUILD.txt")]:
    result = subprocess.run([binary, option], capture_output=True, text=True, check=True, creationflags=flags)
    (notices / filename).write_text(result.stdout + result.stderr, encoding="utf-8")
(notices / "SOURCE-LINKS.md").write_text(
    "# Corresponding upstream sources\n\n"
    "- Qt / PySide6 6.8.3: https://download.qt.io/archive/qt/6.8/6.8.3/ and https://code.qt.io/cgit/pyside/pyside-setup.git/tag/?h=v6.8.3\n"
    "- imageio-ffmpeg 0.6.0 and binary build scripts: https://github.com/imageio/imageio-ffmpeg/tree/v0.6.0\n"
    "- FFmpeg sources (see FFMPEG-VERSION.txt for the exact binary version): https://ffmpeg.org/releases/\n"
    "- Binary license and build configuration are recorded beside this file. Public redistribution requires review of these actual artifacts.\n",
    encoding="utf-8")
