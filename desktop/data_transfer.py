"""Offline dataset import preserving the original and rebasing confined paths."""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path, PurePosixPath

DIRECTORIES = ("samples", "noises", "exports", "trash", "custom_voices")
SETTINGS = ("provider_settings.json", "project_settings.json")


def _is_link(path: Path) -> bool:
    # NTFS junctions are not reported by is_symlink(). Treat both as links so
    # copytree cannot read a different tree through a Windows-only reparse path.
    # st_file_attributes is available on older Windows Python versions too,
    # unlike Path.is_junction (3.12+). Reject all reparse-backed backup entries.
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)


def copy_dataset(source: Path, destination: Path, *, path_root: Path | None = None) -> Path:
    source, destination = source.resolve(), destination.resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("源与目标目录不能相同或互相包含")
    if not (source / "tts_dataset.sqlite3").is_file():
        raise ValueError("源目录缺少 tts_dataset.sqlite3")
    if _is_link(source / "tts_dataset.sqlite3"):
        raise ValueError("源数据库不能是符号链接")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("目标目录必须为空，不覆盖已有数据")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".tts-import-", dir=destination.parent))
    try:
        # Never follow symlinks out of a backup. SQLite.backup copies a
        # consistent snapshot (including WAL), unlike copying only the .db.
        # The caller must stop the Web server so audio and DB agree as well.
        for directory in DIRECTORIES:
            tree = source / directory
            if not tree.exists():
                continue
            if _is_link(tree) or any(_is_link(path) for path in tree.rglob("*")):
                raise ValueError("源数据包含符号链接，请先制作普通文件备份")
            shutil.copytree(tree, stage / directory)
        for filename in SETTINGS:
            path = source / filename
            if path.exists():
                if _is_link(path):
                    raise ValueError("配置文件不能是符号链接")
                shutil.copyfile(path, stage / filename)
        database = stage / "tts_dataset.sqlite3"
        # sqlite3.Connection's context manager commits/rolls back but DOES NOT
        # close the handle. Windows then refuses both directory promotion and
        # failure cleanup with a sharing violation. Explicitly close before
        # os.replace, rather than relying on eventual GC like POSIX might hide.
        with closing(sqlite3.connect((source / "tts_dataset.sqlite3").as_uri() + "?mode=ro", uri=True)) as old:
            with closing(sqlite3.connect(database)) as new:
                old.backup(new)
        with closing(sqlite3.connect(database)) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("源数据库完整性检查失败")
            for table in ("samples", "noises", "exports"):
                for identifier, filename, deleted in db.execute(f"SELECT id,file_path,deleted_at FROM {table}").fetchall():
                    pieces = PurePosixPath(str(filename).replace("\\", "/")).parts
                    indices = [index for index, piece in enumerate(pieces) if piece in DIRECTORIES]
                    relative = None
                    for index in indices:
                        candidate = Path(*pieces[index:])
                        if ".." in candidate.parts:
                            continue
                        if (stage / candidate).is_file():
                            relative = candidate
                            break
                    if relative is None:
                        if not deleted:
                            raise ValueError(f"源数据缺少 {table} 中的音频或导出文件")
                        if not indices or ".." in pieces[indices[0]:]:
                            raise ValueError("源数据含无效文件路径")
                        relative = Path(*pieces[indices[0]:])
                    db.execute(f"UPDATE {table} SET file_path=? WHERE id=?", (str((path_root or destination) / relative), identifier))
            db.commit()
        # Keep the original untouched. A failed import removes only this newly
        # created staging tree; it never empties the user's chosen destination.
        if destination.exists():
            destination.rmdir()  # fails if another process added any content
        os.replace(stage, destination)
        return destination
    finally:
        if stage.exists():
            shutil.rmtree(stage)
