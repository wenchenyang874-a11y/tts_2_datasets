"""Actual isolated process/lock/migration regressions; no user data or cloud."""
import time
import sqlite3
from contextlib import closing
from pathlib import Path
import pytest
from desktop.preferences import DesktopPreferences
from desktop.session import DirectorySession, acquire_directory_lock


def open_session(tmp_path):
    prefs = DesktopPreferences(tmp_path / "preferences.json")
    source = tmp_path / "原 数据"
    prefs.save_data_directory(source)
    session = DirectorySession.open(source, prefs)
    session.runtime.ready()
    return session


def test_hot_empty_directory_preserves_old_dataset_and_switches_now(tmp_path):
    session = open_session(tmp_path)
    old = session.runtime
    try:
        old.call("/api/settings/project", "PUT", json={"display_text": "旧项目"})
        target = tmp_path / "新 数据"
        session.switch(target, "empty")
        assert session.runtime.data_directory == target.resolve()
        assert session.preferences.data_directory() == target.resolve()
        assert not old.process.is_alive()
        assert session.runtime.call("/api/settings/project")["display_text"] != "旧项目"
        session.switch(old.data_directory, "existing")
        assert session.runtime.call("/api/settings/project")["display_text"] == "旧项目"
    finally:
        session.close()


def test_move_rebases_audio_and_removes_old_app_data_but_not_unknown_files(tmp_path):
    session = open_session(tmp_path)
    source = session.runtime.data_directory
    unknown = source / "unrelated.txt"
    unknown.write_text("keep unrelated file")
    try:
        voice = session.runtime.call("/api/voices", params={"provider": "mock"})[0]
        task = session.runtime.call("/api/tasks/generate", "POST", json={"voice_ids": [voice["id"]], "text": "迁移音频"})
        deadline = time.monotonic() + 30
        while session.runtime.call(f"/api/tasks/{task['id']}")["status"] in {"queued", "running"}:
            assert time.monotonic() < deadline
            time.sleep(.03)
        before = session.runtime.call("/api/samples")["items"][0]
        with closing(sqlite3.connect(source / "tts_dataset.sqlite3")) as db:
            old_path = Path(db.execute("SELECT file_path FROM samples WHERE id=?", (before["id"],)).fetchone()[0])
        target = tmp_path / "迁移 数据"
        session.switch(target, "move")
        after = session.runtime.call("/api/samples")["items"][0]
        assert after["id"] == before["id"]
        with closing(sqlite3.connect(target / "tts_dataset.sqlite3")) as db:
            new_path = Path(db.execute("SELECT file_path FROM samples WHERE id=?", (after["id"],)).fetchone()[0])
        assert new_path.is_relative_to(target) and new_path.is_file()
        assert not old_path.exists()
        assert not (source / "tts_dataset.sqlite3").exists()
        assert unknown.read_text() == "keep unrelated file"
        assert not list(source.glob(".tts-relocated-*"))
    finally:
        session.close()


def test_busy_task_refuses_switch_without_stopping_service(tmp_path, monkeypatch):
    session = open_session(tmp_path)
    old = session.runtime
    original = old.call
    monkeypatch.setattr(old, "call", lambda path, *a, **k: {"running_tasks": 1, "queued_tasks": 0} if path == "/api/status" else original(path, *a, **k))
    try:
        with pytest.raises(ValueError, match="任务"):
            session.switch(tmp_path / "target")
        assert old.process.is_alive() and session.runtime is old
        assert session.preferences.data_directory() == old.data_directory
    finally:
        session.close()


def test_target_lock_refuses_switch_and_preserves_original(tmp_path):
    session = open_session(tmp_path)
    target = tmp_path / "occupied"
    lock = acquire_directory_lock(target)
    try:
        with pytest.raises(ValueError, match="另一窗口"):
            session.switch(target)
        assert session.runtime.process.is_alive()
        assert session.preferences.data_directory() == session.runtime.data_directory
    finally:
        lock.unlock()
        session.close()


def test_preference_failure_restores_old_service_and_no_duplicate_data(tmp_path, monkeypatch):
    session = open_session(tmp_path)
    source = session.runtime.data_directory
    target = tmp_path / "failed target"
    monkeypatch.setattr(session.preferences, "save_data_directory", lambda value: (_ for _ in ()).throw(OSError("fixture write denied")))
    try:
        with pytest.raises(OSError, match="fixture"):
            session.switch(target, "move")
        assert session.runtime.data_directory == source
        assert session.runtime.call("/api/status")["ok"]
        assert (source / "tts_dataset.sqlite3").is_file()
        assert not (target / "tts_dataset.sqlite3").exists()
        assert not list(tmp_path.glob(".tts-switch-*"))
    finally:
        session.close()


def test_failed_target_start_restores_old_service(tmp_path, monkeypatch):
    import desktop.session as module
    session = open_session(tmp_path)
    source = session.runtime.data_directory
    target = tmp_path / "failed target"
    original = module.BusinessRuntime
    def create(path):
        if path == target:
            raise OSError("fixture target start denied")
        return original(path)
    monkeypatch.setattr(module, "BusinessRuntime", create)
    try:
        with pytest.raises(OSError, match="fixture"):
            session.switch(target, "move")
        assert session.runtime.data_directory == source and session.runtime.call("/api/status")["ok"]
        assert session.preferences.data_directory() == source
        assert not (target / "tts_dataset.sqlite3").exists()
    finally:
        session.close()
