"""Real child-process, data-transfer and Windows protection regressions."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from desktop.runtime import BusinessRuntime, RuntimeErrorDetail
from desktop.data_transfer import copy_dataset
from private_settings import encode_settings, decode_settings
from settings_store import ProviderSettingsStore
from audio_utils import AudioError, ffmpeg_executable


def test_child_process_owns_database_and_closes(tmp_path):
    runtime = BusinessRuntime(tmp_path / "data")
    try:
        assert runtime.ready()["ready"]
        assert runtime.call("/api/status")["samples"] == 0
        runtime.call("/api/settings/project", "PUT", json={"display_text": "测试唤醒"})
        assert runtime.call("/api/settings/project")["display_text"] == "测试唤醒"
        with pytest.raises(RuntimeErrorDetail):
            runtime.call("/api/tasks/absent")
    finally:
        runtime.close()
    assert not runtime.process.is_alive()
    again = BusinessRuntime(tmp_path / "data")
    try:
        again.ready()
        assert again.call("/api/settings/project")["display_text"] == "测试唤醒"
    finally:
        again.close()


def test_api_validation_does_not_echo_secret_input(tmp_path):
    runtime = BusinessRuntime(tmp_path)
    try:
        runtime.ready()
        with pytest.raises(RuntimeErrorDetail) as failure:
            runtime.call("/api/settings/providers/aliyun", "PUT", json={"pricing_rate": "not-a-number-secret"})
        assert "not-a-number-secret" not in str(failure.value)
    finally:
        runtime.close()


def test_copy_cannot_overwrite_data_directory(tmp_path):
    import time
    runtime = BusinessRuntime(tmp_path / "data")
    try:
        runtime.ready()
        runtime.call("/api/voices/refresh", "POST", json={"provider": "mock"})
        voices = runtime.call("/api/voices", params={"provider": "mock"})
        task = runtime.call("/api/tasks/generate", "POST", json={"voice_ids": [voices[0]["id"]], "text": "测试"})
        deadline = time.monotonic() + 30
        while runtime.call(f"/api/tasks/{task['id']}")["status"] in ("queued", "running"):
            assert time.monotonic() < deadline
            time.sleep(.03)
        sample = runtime.call("/api/samples")["items"][0]
        assert runtime.call("/api/samples", params={"quality": "accepted"})["total"] == 1
        runtime.call(f"/api/samples/{sample['id']}", "PATCH", json={"quality_status": "accepted"})
        assert runtime.call("/api/samples", params={"quality": "pass"})["total"] == 1
        with pytest.raises(RuntimeErrorDetail, match="覆盖源数据"):
            runtime.call(op="copy", kind="samples", id=sample["id"], destination=str(tmp_path / "data/overwrite.wav"))
        assert not (tmp_path / "data/overwrite.wav").exists()
    finally:
        runtime.close()


def test_close_during_generation_records_interruption_on_restart(tmp_path):
    runtime = BusinessRuntime(tmp_path)
    try:
        runtime.ready()
        voice = runtime.call("/api/voices", params={"provider": "mock"})[0]
        task = runtime.call("/api/tasks/generate", "POST", json={"voice_ids": [voice["id"]],
                            "text": "长批次退出测试", "count": 1000})
    finally:
        runtime.close()
    assert not runtime.process.is_alive()
    again = BusinessRuntime(tmp_path)
    try:
        again.ready()
        interrupted = again.call(f"/api/tasks/{task['id']}")
        assert interrupted["status"] == "failed"
        assert "中断" in interrupted["message"]
        assert interrupted["completed"] < interrupted["total"]
    finally:
        again.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree ownership")
def test_windows_job_closes_spawned_descendants(tmp_path):
    import subprocess
    import sys
    import time
    import ctypes
    from ctypes import wintypes
    from desktop.process_job import ProcessJob
    marker, start = tmp_path / "child-pid.txt", tmp_path / "go.txt"
    code = ("import pathlib,subprocess,sys,time; marker=pathlib.Path(sys.argv[1]); "
            "go=pathlib.Path(sys.argv[2]);\n"
            "while not go.exists(): time.sleep(.01)\n"
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],creationflags=subprocess.CREATE_NO_WINDOW); "
            "marker.write_text(str(child.pid));time.sleep(60)")
    parent = subprocess.Popen([sys.executable, "-c", code, str(marker), str(start)], creationflags=subprocess.CREATE_NO_WINDOW)
    job = ProcessJob(parent.pid)
    child_handle = None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    try:
        start.write_text("go")
        deadline = time.monotonic() + 5
        while not marker.exists():
            assert time.monotonic() < deadline
            time.sleep(.02)
        child_handle = kernel.OpenProcess(0x00100000, False, int(marker.read_text()))
        assert child_handle
        job.close()
        parent.wait(5)
        assert kernel.WaitForSingleObject(child_handle, 5000) == 0
    finally:
        job.close()
        if parent.poll() is None:
            parent.terminate()
            parent.wait(5)
        if child_handle:
            kernel.CloseHandle(child_handle)


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI test")
def test_user_dpapi_roundtrip_and_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv("TTS_DESKTOP_PROTECT_SETTINGS", "1")
    value = {"secret": "desktop-test-credential"}
    encoded = encode_settings(json.dumps(value).encode())
    assert b"desktop-test-credential" not in encoded
    assert decode_settings(json.loads(encoded)) == value
    path = tmp_path / "provider_settings.json"
    store = ProviderSettingsStore(path)
    store.update_provider("aliyun", {"api_key": "desktop-test-credential"})
    assert "desktop-test-credential" not in path.read_text(encoding="utf-8")
    assert ProviderSettingsStore(path).get_provider("aliyun")["api_key"] == "desktop-test-credential"
    assert "desktop-test-credential" not in json.dumps(store.public_all())


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI test")
def test_legacy_plaintext_is_sealed_on_open(tmp_path, monkeypatch):
    monkeypatch.delenv("TTS_DESKTOP_PROTECT_SETTINGS", raising=False)
    path = tmp_path / "provider_settings.json"
    ProviderSettingsStore(path).update_provider("aliyun", {"api_key": "legacy-test-credential"})
    assert "legacy-test-credential" in path.read_text(encoding="utf-8")
    monkeypatch.setenv("TTS_DESKTOP_PROTECT_SETTINGS", "1")
    store = ProviderSettingsStore(path)
    assert store.get_provider("aliyun")["api_key"] == "legacy-test-credential"
    assert "legacy-test-credential" not in path.read_text(encoding="utf-8")


def test_missing_ffmpeg_override_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("TTS_FFMPEG_PATH", str(tmp_path / "missing.exe"))
    with pytest.raises(AudioError, match="不存在"):
        ffmpeg_executable()


def create_dataset(root: Path, filename="/data/samples/voice/sample.wav"):
    root.mkdir()
    (root / "samples/voice").mkdir(parents=True)
    (root / "samples/voice/sample.wav").write_bytes(b"audio-placeholder-for-transfer-test")
    with sqlite3.connect(root / "tts_dataset.sqlite3") as db:
        for table in ("samples", "noises", "exports"):
            db.execute(f"CREATE TABLE {table}(id TEXT,file_path TEXT,deleted_at TEXT)")
        db.execute("INSERT INTO samples VALUES('one',?,NULL)", (filename,))


def test_web_import_rebases_linux_paths_and_preserves_source(tmp_path):
    source, destination = tmp_path / "old", tmp_path / "中文 新目录"
    create_dataset(source)
    copied = copy_dataset(source, destination)
    with sqlite3.connect(copied / "tts_dataset.sqlite3") as db:
        path = db.execute("SELECT file_path FROM samples").fetchone()[0]
    assert Path(path) == destination / "samples/voice/sample.wav"
    assert Path(path).is_file()
    with sqlite3.connect(source / "tts_dataset.sqlite3") as db:
        assert db.execute("SELECT file_path FROM samples").fetchone()[0] == "/data/samples/voice/sample.wav"


@pytest.mark.parametrize("filename", ["/data/samples/../outside.wav", "/data/samples/voice/missing.wav"])
def test_import_invalid_paths_do_not_overwrite_destination(tmp_path, filename):
    source, destination = tmp_path / "old", tmp_path / "new"
    create_dataset(source, filename)
    with pytest.raises(ValueError):
        copy_dataset(source, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".tts-import-*"))


def test_import_rejects_nonempty_or_nested_destination(tmp_path):
    source, destination = tmp_path / "old", tmp_path / "new"
    create_dataset(source)
    destination.mkdir()
    (destination / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="为空"):
        copy_dataset(source, destination)
    assert (destination / "keep.txt").read_text() == "keep"
    with pytest.raises(ValueError, match="包含"):
        copy_dataset(source, source / "nested")


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction")
def test_import_rejects_windows_junction_without_reading_other_tree(tmp_path):
    import subprocess
    source, outside = tmp_path / "old", tmp_path / "outside"
    create_dataset(source)
    outside.mkdir()
    (outside / "keep.txt").write_text("private-test-data")
    script = tmp_path / "junction.ps1"
    script.write_text("param($LinkPath,$TargetPath)\nNew-Item -ItemType Junction -Path $LinkPath -Target $TargetPath | Out-Null\n", encoding="utf-8")
    link = source / "samples/linked"
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                    "-LinkPath", str(link), "-TargetPath", str(outside)], check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    assert link.is_junction() and not link.is_symlink()
    with pytest.raises(ValueError, match="链接"):
        copy_dataset(source, tmp_path / "new")
    assert (outside / "keep.txt").read_text() == "private-test-data"
    assert not (tmp_path / "new").exists()
