"""Live dataset switching with exclusive locks and transactional migration."""
from __future__ import annotations

import os
import shutil
import uuid
import sqlite3
from contextlib import closing
from pathlib import Path
from PySide6.QtCore import QLockFile
from .data_transfer import copy_dataset, DIRECTORIES, SETTINGS, _is_link
from .runtime import BusinessRuntime


def acquire_directory_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(directory / ".desktop.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        raise ValueError("该数据目录正被另一窗口使用，或没有写入权限")
    return lock


class DirectorySession:
    def __init__(self, runtime, preferences, lock):
        self.runtime = runtime
        self.preferences = preferences
        self.lock = lock

    @classmethod
    def open(cls, directory, preferences):
        directory = Path(directory).resolve()
        lock = acquire_directory_lock(directory)
        try:
            return cls(BusinessRuntime(directory), preferences, lock)
        except Exception:
            lock.unlock()
            raise

    def switch(self, value, mode="empty"):
        source = self.runtime.data_directory
        target = self.preferences.validate_data_directory(value)
        if source == target:
            return {"directory": str(source), "warning": ""}
        if source in target.parents or target in source.parents:
            raise ValueError("新旧数据目录不能互相包含")
        if mode not in {"empty", "move", "existing"}:
            raise ValueError("未知数据目录切换方式")
        # The caller freezes command submission first. Once its single worker
        # reaches here, all previous UI commands have finished. Refuse to stop
        # real jobs rather than marking paid/recording work interrupted.
        status = self.runtime.call("/api/status")
        if status["running_tasks"] or status["queued_tasks"]:
            raise ValueError("仍有任务在执行或排队，请等待任务结束后切换目录")
        old_runtime = self.runtime
        new_runtime = None
        new_lock = acquire_directory_lock(target)
        original_entries = {item.name for item in target.iterdir() if item.name != ".desktop.lock"}
        stage = target.parent / (".tts-switch-" + uuid.uuid4().hex)
        retired = source / (".tts-relocated-" + uuid.uuid4().hex)
        promoted = []
        detached = []
        old_stopped = False
        preference_written = False
        try:
            if mode == "existing":
                if not (target / "tts_dataset.sqlite3").is_file():
                    raise ValueError("该目录不是现有数据集")
                database = target / "tts_dataset.sqlite3"
                if _is_link(database):
                    raise ValueError("不能打开链接数据库")
                with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as probe:
                    tables = {row[0] for row in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if not {"samples", "tasks", "voices", "noises", "exports"}.issubset(tables) or probe.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("目标目录不是完整有效的语音数据集")
            elif original_entries:
                raise ValueError("目标目录必须为空；已有数据集可选择直接打开，其他文件不会被覆盖")
            old_runtime.close()
            old_stopped = True
            if mode == "move":
                # Staging/SQLite backup is preparation, not the final semantic
                # operation. Old data is retired only after the target starts
                # and preferences persist. This supports cross-volume moves
                # without ever deleting the sole working dataset on failure.
                copy_dataset(source, stage, path_root=target)
                for item in stage.iterdir():
                    destination = target / item.name
                    if destination.exists():
                        raise ValueError("目标目录在切换期间被修改，已停止迁移")
                    os.replace(item, destination)
                    promoted.append(destination)
                stage.rmdir()
            new_runtime = BusinessRuntime(target)
            new_runtime.ready()
            new_runtime.call("/api/status")
            self.preferences.save_data_directory(target)
            preference_written = True
            if mode == "move":
                # Detach each known application entry by atomic rename on the
                # source volume. If any rename fails, undo every previous one
                # before restoring the old service. Unknown files stay put.
                retired.mkdir()
                for name in (*DIRECTORIES, *SETTINGS, "tmp", "tts_dataset.sqlite3", "tts_dataset.sqlite3-wal", "tts_dataset.sqlite3-shm"):
                    path = source / name
                    if not path.exists():
                        continue
                    if _is_link(path):
                        raise ValueError("源数据包含链接，无法安全迁移")
                    os.replace(path, retired / name)
                    detached.append(name)
            self.runtime = new_runtime
            self.lock.unlock()
            self.lock = new_lock
            warning = ""
            if retired.exists():
                # Only this UUID-owned child can be removed; never recursively
                # delete the user-selected source root or its unknown files.
                assert retired.parent == source and retired.name.startswith(".tts-relocated-")
                try:
                    shutil.rmtree(retired)
                except OSError:
                    warning = "目录已切换；旧位置的迁移暂存文件未能清理：" + str(retired)
            return {"directory": str(target), "warning": warning}
        except Exception:
            if new_runtime is not None:
                new_runtime.close()
            for name in reversed(detached):
                os.replace(retired / name, source / name)
            if retired.exists() and not any(retired.iterdir()):
                retired.rmdir()
            if preference_written:
                self.preferences.save_data_directory(source)
            if old_stopped:
                self.runtime = BusinessRuntime(source)
                self.runtime.ready()
            # Target content present before the transaction is never deleted.
            # Empty-target initialization creates only application-owned files;
            # rollback removes those known entries while its lock is held.
            if mode != "existing":
                owned_names = {*DIRECTORIES, *SETTINGS, "tmp", "tts_dataset.sqlite3", "tts_dataset.sqlite3-wal", "tts_dataset.sqlite3-shm"}
                for name in owned_names - original_entries:
                    path = target / name
                    if path.is_dir() and not _is_link(path):
                        shutil.rmtree(path)
                    elif path.is_file() and not _is_link(path):
                        path.unlink()
            new_lock.unlock()
            raise
        finally:
            if stage.exists():
                assert stage.parent == target.parent and stage.name.startswith(".tts-switch-")
                shutil.rmtree(stage)

    def close(self):
        self.runtime.close()
        self.lock.unlock()
