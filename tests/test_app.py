"""Offline API integration tests using only the deterministic Mock provider."""

from __future__ import annotations

import io
import json
import math
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import wave
import zipfile
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient


TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="kws-tts-app-test-"))
os.environ["TTS_APP_DATA_DIR"] = str(TEST_DATA_DIR)
os.environ.pop("TTS_APP_API_TOKEN", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module  # type: ignore  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_data():
    yield
    app_module.executor.shutdown(wait=True)
    shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)


@pytest.fixture(scope="session")
def client() -> TestClient:
    with TestClient(app_module.app) as test_client:
        yield test_client


def _wait_for_task(client: TestClient, task_id: str, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/tasks/{task_id}")
        assert response.status_code == 200
        task = response.json()
        if task["status"] not in {"queued", "running"}:
            return task
        time.sleep(0.05)
    pytest.fail(f"task {task_id} did not finish within {timeout} seconds")


def _first_mock_voice(client: TestClient) -> dict:
    voices = client.get("/api/voices", params={"provider": "mock"}).json()
    if not voices:
        refresh = client.post("/api/voices/refresh", json={"provider": "mock"})
        assert refresh.status_code == 200
        voices = client.get("/api/voices", params={"provider": "mock"}).json()
    return voices[0]


def _wav_bytes(duration: float = 2.0, frequency: float = 173.0) -> bytes:
    sample_rate = 16_000
    timeline = np.arange(int(sample_rate * duration), dtype=np.float64) / sample_rate
    samples = (0.08 * np.sin(2.0 * math.pi * frequency * timeline) * 32767.0).astype("<i2")
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(samples.tobytes())
    return output.getvalue()


def test_legacy_noise_table_migrates_deleted_at() -> None:
    database_path = TEST_DATA_DIR / "legacy-noises.sqlite3"
    with sqlite3.connect(database_path) as legacy:
        legacy.execute(
            """
            CREATE TABLE noises (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
                file_path TEXT NOT NULL, duration REAL NOT NULL, sample_rate INTEGER NOT NULL,
                weight REAL NOT NULL, min_snr REAL NOT NULL, max_snr REAL NOT NULL,
                enabled INTEGER NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL
            )
            """
        )
    migrated = app_module.Database(database_path)
    with migrated.connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(noises)")}
    assert {"metadata_json", "deleted_at"} <= columns


def test_legacy_export_table_migrates_deleted_at() -> None:
    database_path = TEST_DATA_DIR / "legacy-exports.sqlite3"
    with sqlite3.connect(database_path) as legacy:
        legacy.execute(
            """
            CREATE TABLE exports (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, file_path TEXT NOT NULL,
                sample_count INTEGER NOT NULL, format TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
    migrated = app_module.Database(database_path)
    app_module.Database(database_path)
    with migrated.connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(exports)")}
        indexes = {row["name"] for row in connection.execute("PRAGMA index_list(exports)")}
    assert "deleted_at" in columns
    assert "idx_exports_active" in indexes


def test_provider_settings_api_masks_secrets_and_separates_volc_credentials(
    client: TestClient,
) -> None:
    client.delete("/api/settings/providers/aliyun")
    client.delete("/api/settings/providers/volcengine")

    unsupported_region = client.put("/api/settings/providers/aliyun", json={
        "region": "ap-southeast-1",
    })
    assert unsupported_region.status_code == 400
    assert "cn-beijing" in unsupported_region.json()["detail"]

    aliyun_secret = "TEST_ONLY_ALI_SECRET_5678"
    saved_aliyun = client.put("/api/settings/providers/aliyun", json={
        "api_key": aliyun_secret,
        "workspace_id": "workspace-unit-test",
        "region": "cn-beijing",
    })
    assert saved_aliyun.status_code == 200
    assert saved_aliyun.json()["api_key"] == {
        "configured": True, "masked": "****5678",
    }
    assert aliyun_secret not in saved_aliyun.text
    all_settings = client.get("/api/settings/providers")
    assert all_settings.status_code == 200
    assert aliyun_secret not in all_settings.text
    settings_path = TEST_DATA_DIR / "provider_settings.json"
    assert stat.S_IMODE(settings_path.stat().st_mode) == 0o600

    preserved = client.put("/api/settings/providers/aliyun", json={
        "api_key": "", "workspace_id": "workspace-changed",
    })
    assert preserved.status_code == 200
    assert preserved.json()["api_key"]["configured"] is True
    tested = client.post("/api/settings/providers/aliyun/test")
    assert tested.status_code == 200
    assert tested.json()["remote_verified"] is False

    synthesis_only = client.put("/api/settings/providers/volcengine", json={
        "app_id": "volc-app",
        "access_token": "volc-synthesis-secret",
        "cluster": "volcano_tts",
    })
    assert synthesis_only.status_code == 200
    assert synthesis_only.json()["synthesis_configured"] is True
    assert synthesis_only.json()["catalog_configured"] is False
    provider_rows = client.get("/api/providers").json()
    volc_status = next(item for item in provider_rows if item["id"] == "volcengine")
    assert volc_status["supports_voice_query"] is False

    with_catalog = client.put("/api/settings/providers/volcengine", json={
        "access_key_id": "volc-catalog-ak",
        "access_key_secret": "volc-catalog-secret-9012",
    })
    assert with_catalog.status_code == 200
    assert with_catalog.json()["catalog_configured"] is True
    assert "volc-catalog-secret-9012" not in with_catalog.text
    volc_status = next(
        item for item in client.get("/api/providers").json()
        if item["id"] == "volcengine"
    )
    assert volc_status["supports_voice_query"] is True
    assert client.post("/api/settings/providers/volcengine/test").status_code == 200

    app_module.db.upsert_voice({
        "provider": "aliyun", "voice_id": "account-delete-test",
        "name": "阿里账号音色", "model": "cosyvoice-v3.5-plus",
        "source": "account_custom", "enabled": True,
    })
    app_module.db.upsert_voice({
        "provider": "volcengine", "voice_id": "catalog-delete-test",
        "name": "火山账号音色", "model": "seed-tts-1.0",
        "source": "provider_catalog", "enabled": True,
    })

    cleared_aliyun = client.delete("/api/settings/providers/aliyun")
    cleared_volc = client.delete("/api/settings/providers/volcengine")
    assert cleared_aliyun.json()["configured"] is False
    assert cleared_volc.json()["configured"] is False
    assert cleared_aliyun.json()["disabled_dynamic_voices"] == 1
    assert cleared_volc.json()["disabled_dynamic_voices"] == 1
    assert app_module.db.one(
        "SELECT enabled FROM voices WHERE provider=? AND voice_id=?",
        ("aliyun", "account-delete-test"),
    )["enabled"] == 0
    assert app_module.db.one(
        "SELECT enabled FROM voices WHERE provider=? AND voice_id=?",
        ("volcengine", "catalog-delete-test"),
    )["enabled"] == 0
    assert app_module.db.one(
        "SELECT COUNT(*) AS n FROM voices WHERE provider='aliyun' AND source='builtin' AND enabled=1"
    )["n"] > 0


def test_project_settings_api_derives_metadata_and_requires_app_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset = client.put("/api/settings/project", json={"display_text": "你好，芯宝"})
    assert reset.status_code == 200
    assert client.get("/api/settings/project").json()["keyword_id"] == "ni_hao_xin_bao"

    updated = client.put("/api/settings/project", json={"display_text": "  海思，小 宝！  "})
    assert updated.status_code == 200
    project = updated.json()
    assert project["display_text"] == "海思，小 宝！"
    assert project["label"] == "海思小宝"
    assert project["keyword_id"].startswith("kws_")
    assert len(project["keyword_id"]) == 16
    assert project["updated_at"]
    assert stat.S_IMODE((TEST_DATA_DIR / "project_settings.json").stat().st_mode) == 0o600

    assert client.get("/healthz").json() == {"ok": True}
    status = client.get("/api/status")
    assert status.status_code == 200
    assert status.json()["app"] == "海思语音数据集工坊"
    assert status.json()["project"] == project
    assert app_module.app.title == "海思语音数据集工坊"

    assert client.put("/api/settings/project", json={"display_text": "，！？"}).status_code == 422
    assert client.put("/api/settings/project", json={"display_text": "x" * 101}).status_code == 422

    monkeypatch.setenv("TTS_APP_API_TOKEN", "project-settings-test-token")
    assert client.get("/api/settings/project").status_code == 401
    assert client.put(
        "/api/settings/project", json={"display_text": "未授权更改"}
    ).status_code == 401
    authorized = client.get(
        "/api/settings/project",
        headers={"X-App-Token": "project-settings-test-token"},
    )
    assert authorized.status_code == 200
    assert authorized.json() == project
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/status").status_code == 401
    assert client.get(
        "/api/status", headers={"X-App-Token": "project-settings-test-token"}
    ).status_code == 200

    monkeypatch.delenv("TTS_APP_API_TOKEN")
    restored = client.put("/api/settings/project", json={"display_text": "你好，芯宝"})
    assert restored.status_code == 200
    assert restored.json()["keyword_id"] == "ni_hao_xin_bao"


def test_voice_refresh_disables_stale_snapshot_and_surfaces_provider_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_module.db.upsert_voice({
        "provider": "catalog-test",
        "voice_id": "stale-voice",
        "name": "旧音色",
        "model": "model-v1",
        "source": "builtin",
        "enabled": True,
    })

    def fake_refresh(*, provider, settings_store, refresh_state):
        refresh_state["complete_snapshots"] = [{
            "provider": "catalog-test", "source": "builtin",
        }]
        return [{
            "provider": "catalog-test",
            "voice_id": "current-voice",
            "name": "当前音色",
            "model": "model-v1",
            "source": "builtin",
            "enabled": True,
        }]

    monkeypatch.setattr(app_module, "refresh_voice_catalog", fake_refresh)
    refreshed = client.post("/api/voices/refresh", json={"provider": "catalog-test"})
    assert refreshed.status_code == 200
    assert refreshed.json()["disabled_stale"] == 1
    stale = app_module.db.one(
        "SELECT enabled FROM voices WHERE provider=? AND voice_id=?",
        ("catalog-test", "stale-voice"),
    )
    assert stale["enabled"] == 0

    def failed_refresh(**kwargs):
        raise app_module.ProviderError("脱敏后的目录鉴权失败")

    monkeypatch.setattr(app_module, "refresh_voice_catalog", failed_refresh)
    failed = client.post("/api/voices/refresh", json={"provider": "aliyun"})
    assert failed.status_code == 502
    assert "目录鉴权失败" in failed.json()["detail"]


def test_generate_task_preserves_provider_error_in_final_message(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    voice = _first_mock_voice(client)

    class FailedProvider:
        def synthesize(self, **kwargs):
            raise app_module.ProviderError(
                "模拟厂商拒绝：code=AccessDenied，message=voice not authorized"
            )

    monkeypatch.setattr(app_module, "get_provider", lambda *args, **kwargs: FailedProvider())
    created = client.post("/api/tasks/generate", json={
        "voice_ids": [voice["id"]], "count": 1,
    })
    assert created.status_code == 200

    task = _wait_for_task(client, created.json()["task_id"])

    assert task["status"] == "failed"
    assert "AccessDenied" in task["message"]
    assert task["result"]["errors"]
    assert "voice not authorized" in task["result"]["errors"][0]


def test_generate_task_hides_unexpected_exception_detail(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    voice = _first_mock_voice(client)
    secret = "TEST_ONLY_UNEXPECTED_VALUE"

    class BrokenProvider:
        def synthesize(self, **kwargs):
            raise RuntimeError(f"unexpected failure containing {secret}")

    monkeypatch.setattr(app_module, "get_provider", lambda *args, **kwargs: BrokenProvider())
    created = client.post("/api/tasks/generate", json={
        "voice_ids": [voice["id"]], "count": 1,
    })
    task = _wait_for_task(client, created.json()["task_id"])

    rendered = json.dumps(task, ensure_ascii=False)
    assert task["status"] == "failed"
    assert "RuntimeError" in rendered
    assert secret not in rendered


def test_volc_pitch_is_rejected_before_task_creation(client: TestClient) -> None:
    app_module.db.upsert_voice({
        "provider": "volcengine",
        "voice_id": "volc-pitch-test",
        "name": "火山音高测试",
        "model": "seed-tts-1.0",
        "source": "provider_catalog",
        "enabled": True,
    })
    voice = app_module.db.one(
        "SELECT id FROM voices WHERE provider=? AND voice_id=?",
        ("volcengine", "volc-pitch-test"),
    )

    response = client.post("/api/tasks/generate", json={
        "voice_ids": [voice["id"]], "count": 1, "pitch": 1.1,
    })

    assert response.status_code == 400
    assert "0 st" in response.json()["detail"]


def test_voice_upsert_applies_enabled_state(tmp_path: Path) -> None:
    database = app_module.Database(tmp_path / "voice-enabled.sqlite3")
    voice = {
        "provider": "test",
        "voice_id": "voice-1",
        "name": "测试音色",
        "model": "model-v1",
        "source": "account_custom",
        "enabled": True,
    }
    database.upsert_voice(voice)
    database.upsert_voice({**voice, "enabled": False})

    row = database.one(
        "SELECT enabled FROM voices WHERE provider=? AND voice_id=? AND model=?",
        ("test", "voice-1", "model-v1"),
    )
    assert row["enabled"] == 0


def test_startup_sync_seeds_builtin_voices_and_disables_removed_rows(
    client: TestClient,
) -> None:
    builtin = app_module.builtin_voices()
    assert builtin
    first = builtin[0]
    app_module.db.execute(
        "UPDATE voices SET enabled=0 WHERE provider=? AND voice_id=? AND model=?",
        (first.provider, first.voice_id, first.model),
    )
    app_module.db.upsert_voice({
        "provider": "mock",
        "voice_id": "removed-builtin-test",
        "name": "已移除内置音色",
        "model": "removed-v1",
        "source": "builtin",
        "enabled": True,
    })

    assert app_module._sync_builtin_voice_catalog(app_module.db) == len(builtin)
    restored = app_module.db.one(
        "SELECT enabled FROM voices WHERE provider=? AND voice_id=? AND model=?",
        (first.provider, first.voice_id, first.model),
    )
    removed = app_module.db.one(
        "SELECT enabled FROM voices WHERE provider=? AND voice_id=? AND model=?",
        ("mock", "removed-builtin-test", "removed-v1"),
    )
    assert restored["enabled"] == 1
    assert removed["enabled"] == 0
    assert client.get("/api/voices", params={"provider": "mock"}).json()


def test_mock_generate_analyze_augment_export_and_restore(client: TestClient) -> None:
    refresh = client.post("/api/voices/refresh", json={"provider": "mock"})
    assert refresh.status_code == 200
    assert refresh.json()["count"] >= 2
    voices = client.get("/api/voices", params={"provider": "mock"}).json()
    assert voices

    generated = client.post("/api/tasks/generate", json={
        "text": "你好，芯宝", "label": "你好芯宝", "keyword_id": "ni_hao_xin_bao",
        "voice_ids": [voices[0]["id"]], "count": 1, "speed": 1.0,
        "pitch": 1.0, "volume": 50, "seed": 20260805,
    })
    assert generated.status_code == 200
    generated_task = _wait_for_task(client, generated.json()["task_id"])
    assert generated_task["status"] == "completed"
    assert generated_task["completed"] == 1

    sample_page = client.get("/api/samples").json()
    assert sample_page["total"] == 1
    sample = sample_page["items"][0]
    assert "file_path" not in sample
    sample_id = sample["id"]
    analysis = client.get(f"/api/samples/{sample_id}/analysis")
    assert analysis.status_code == 200
    assert analysis.json()["info"]["sample_rate"] == 16_000
    assert analysis.json()["info"]["channels"] == 1
    assert analysis.json()["metrics"]["peak"] > 0
    assert analysis.json()["waveform"]
    assert analysis.json()["spectrum"]["frequency"]

    review = client.patch(
        f"/api/samples/{sample_id}",
        json={"quality_status": "accepted", "notes": "offline integration test"},
    )
    assert review.status_code == 200
    assert review.json()["quality_status"] == "accepted"

    noise_upload = client.post(
        "/api/noises/upload",
        files={"file": ("room.wav", _wav_bytes(), "audio/wav")},
        data={
            "name": "test room", "category": "home", "min_snr": "8", "max_snr": "12",
            "source": "offline test", "license": "self-recorded",
        },
    )
    assert noise_upload.status_code == 200
    noise_id = noise_upload.json()["id"]
    noise_rows = client.get("/api/noises").json()
    assert "file_path" not in noise_rows[0]
    assert client.get(f"/api/noises/{noise_id}/audio").status_code == 200

    augmented = client.post("/api/augment", json={
        "sample_ids": [sample_id], "noise_ids": [noise_id], "count": 1, "seed": 99,
    })
    assert augmented.status_code == 200
    augmented_task = _wait_for_task(client, augmented.json()["task_id"])
    assert augmented_task["status"] == "completed"
    augmented_rows = client.get("/api/samples", params={"sample_type": "augmented"}).json()["items"]
    augmented_sample = next(item for item in augmented_rows if item["parent_id"] == sample_id)
    assert isinstance(augmented_sample["metadata"]["noise_offset"], int)

    deleted_noise = client.delete(f"/api/noises/{noise_id}")
    assert deleted_noise.status_code == 200
    assert deleted_noise.json()["recoverable"] is True
    assert noise_id not in {item["id"] for item in client.get("/api/noises").json()}
    deleted_noise_rows = client.get("/api/noises", params={"include_deleted": True}).json()
    deleted_noise_row = next(item for item in deleted_noise_rows if item["id"] == noise_id)
    assert deleted_noise_row["metadata"]["source"] == "offline test"
    assert "file_path" not in deleted_noise_row
    assert client.get(f"/api/noises/{noise_id}/audio").status_code == 404
    restored_noise = client.post(f"/api/noises/{noise_id}/restore")
    assert restored_noise.status_code == 200
    assert client.get(f"/api/noises/{noise_id}/audio").status_code == 200

    export_project = client.put(
        "/api/settings/project", json={"display_text": "海思，小宝"}
    ).json()
    exported = client.post("/api/exports", json={
        "name": "ni_hao_xin_bao_test", "format": "wekws", "include_audio": True,
        "train_ratio": 0.8, "dev_ratio": 0.1, "seed": 20260805,
    })
    assert exported.status_code == 200
    export_task = _wait_for_task(client, exported.json()["task_id"])
    assert export_task["status"] == "completed"
    export_id = export_task["result"]["export_id"]
    archive = client.get(
        f"/api/exports/{export_id}/download"
    )
    assert archive.status_code == 200
    with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
        names = set(bundle.namelist())
        assert "dataset.json" in names
        assert "SHA256SUMS" in names
        dataset_metadata = json.loads(bundle.read("dataset.json"))
        assert dataset_metadata["wake_word"] == export_project["display_text"]
        assert dataset_metadata["label"] == export_project["label"]
        assert dataset_metadata["keyword_id"] == export_project["keyword_id"]
        assert any(name.startswith("manifests/") for name in names)
        assert any(name.startswith("audio/") for name in names)
        manifest_records = []
        for name in names:
            if name.startswith("manifests/") and name.endswith(".jsonl"):
                manifest_records.extend(
                    json.loads(line) for line in bundle.read(name).decode().splitlines() if line
                )
        exported_augmented = next(item for item in manifest_records if item["utt_id"] == augmented_sample["id"])
        assert exported_augmented["noise_offset"] == augmented_sample["metadata"]["noise_offset"]
        assert exported_augmented["source"] == "tts_augmented"
    assert client.put(
        "/api/settings/project", json={"display_text": "你好，芯宝"}
    ).status_code == 200

    export_rows = client.get("/api/exports").json()
    assert export_rows and "file_path" not in export_rows[0]

    deleted_export = client.delete(f"/api/exports/{export_id}")
    assert deleted_export.status_code == 200
    assert deleted_export.json()["recoverable"] is True
    assert export_id not in {item["id"] for item in client.get("/api/exports").json()}
    deleted_export_rows = client.get(
        "/api/exports", params={"include_deleted": True}
    ).json()
    deleted_export_row = next(
        item for item in deleted_export_rows if item["id"] == export_id
    )
    assert deleted_export_row["deleted_at"]
    assert "file_path" not in deleted_export_row
    assert client.get(f"/api/exports/{export_id}/download").status_code == 404
    stored_deleted_export = app_module.db.one(
        "SELECT file_path FROM exports WHERE id=?", (export_id,)
    )
    deleted_export_path = Path(stored_deleted_export["file_path"])
    assert deleted_export_path.parent == app_module.TRASH_DIR / "exports" / export_id
    assert deleted_export_path.exists()

    restored_export = client.post(f"/api/exports/{export_id}/restore")
    assert restored_export.status_code == 200
    assert client.get(f"/api/exports/{export_id}/download").status_code == 200
    restored_export_row = app_module.db.one(
        "SELECT file_path,deleted_at FROM exports WHERE id=?", (export_id,)
    )
    assert restored_export_row["deleted_at"] is None
    assert Path(restored_export_row["file_path"]).parent == app_module.EXPORTS_DIR

    manifest_only = client.post("/api/exports", json={
        "sample_ids": [sample_id], "name": "manifest_only", "format": "wekws",
        "include_audio": False, "train_ratio": 0.8, "dev_ratio": 0.1,
        "seed": 20260805,
    })
    assert manifest_only.status_code == 200
    manifest_task = _wait_for_task(client, manifest_only.json()["task_id"])
    assert manifest_task["status"] == "completed"
    manifest_archive = client.get(
        f"/api/exports/{manifest_task['result']['export_id']}/download"
    )
    with zipfile.ZipFile(io.BytesIO(manifest_archive.content)) as bundle:
        names = set(bundle.namelist())
        assert "SHA256SUMS" not in names
        assert not any(name.startswith("audio/") for name in names)
        dataset = json.loads(bundle.read("dataset.json"))
        assert dataset["include_audio"] is False
        assert dataset["audio_path_type"] == "server_absolute"
        assert dataset["manifest_data_root"] == str(TEST_DATA_DIR.resolve())
        assert dataset["split_config"]["seed"] == 20260805
        records = []
        for name in names:
            if name.startswith("manifests/") and name.endswith(".jsonl"):
                records.extend(json.loads(line) for line in bundle.read(name).decode().splitlines() if line)
        record = next(item for item in records if item["utt_id"] == sample_id)
        assert record["audio_path_type"] == "server_absolute"
        assert Path(record["audio"]).is_absolute()
        assert Path(record["audio"]).exists()
        wav_scp = "\n".join(
            bundle.read(name).decode() for name in names if name.endswith("/wav.scp")
        )
        assert f"{sample_id} {record['audio']}" in wav_scp

    deleted = client.delete(f"/api/samples/{sample_id}")
    assert deleted.status_code == 200
    assert deleted.json()["recoverable"] is True
    restored = client.post(f"/api/samples/{sample_id}/restore")
    assert restored.status_code == 200


def test_import_real_recording(client: TestClient) -> None:
    imported = client.post(
        "/api/samples/upload",
        files={"files": ("speaker.wav", _wav_bytes(0.8, 330.0), "audio/wav")},
        data={
            "text": "你好，芯宝", "label": "你好芯宝", "speaker_id": "spk_001",
            "split": "train", "gender": "female", "age_group": "young",
        },
    )
    assert imported.status_code == 200
    assert imported.json()["imported"] == 1
    sample_id = imported.json()["sample_ids"][0]
    page = client.get(
        "/api/samples",
        params={"provider": "real", "gender": "female", "age_group": "young"},
    ).json()
    assert page["total"] == 1
    assert page["items"][0]["metadata"]["speaker_id"] == "spk_001"

    explicit_export = client.post("/api/exports", json={
        "sample_ids": [sample_id], "name": "explicit_real_split", "format": "generic",
        "include_audio": False, "train_ratio": 0.01, "dev_ratio": 0.01, "seed": 1,
    })
    assert explicit_export.status_code == 200
    explicit_task = _wait_for_task(client, explicit_export.json()["task_id"])
    assert explicit_task["status"] == "completed"
    archive = client.get(f"/api/exports/{explicit_task['result']['export_id']}/download")
    with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
        train_records = [
            json.loads(line)
            for line in bundle.read("manifests/train.jsonl").decode().splitlines()
            if line
        ]
        assert any(record["utt_id"] == sample_id for record in train_records)

    conflicting = client.post(
        "/api/samples/upload",
        files={"files": ("speaker-test.wav", _wav_bytes(0.9, 440.0), "audio/wav")},
        data={
            "text": "你好，芯宝", "label": "你好芯宝", "speaker_id": "spk_001",
            "split": "test", "gender": "female", "age_group": "young",
        },
    )
    assert conflicting.status_code == 200
    conflict_id = conflicting.json()["sample_ids"][0]
    conflict_export = client.post("/api/exports", json={
        "sample_ids": [sample_id, conflict_id], "name": "conflicting_real_split",
        "format": "generic", "include_audio": False,
        "train_ratio": 0.8, "dev_ratio": 0.1,
    })
    assert conflict_export.status_code == 400
    assert "spk_001" in conflict_export.json()["detail"]


def test_omitted_kws_fields_follow_project_and_explicit_values_are_preserved(
    client: TestClient,
) -> None:
    custom = client.put(
        "/api/settings/project", json={"display_text": "海思，小宝"}
    ).json()
    created_ids: list[str] = []
    try:
        voice = _first_mock_voice(client)
        generated = client.post("/api/tasks/generate", json={
            "voice_ids": [voice["id"]], "count": 1, "seed": 20260910,
            "instruction": "dynamic-project-defaults",
        })
        assert generated.status_code == 200
        assert _wait_for_task(client, generated.json()["task_id"])["status"] == "completed"
        generated_row = app_module.db.one(
            "SELECT id,text,label,keyword_id FROM samples WHERE seed=? AND deleted_at IS NULL",
            (20260910,),
        )
        created_ids.append(generated_row["id"])
        assert generated_row["text"] == custom["display_text"]
        assert generated_row["label"] == custom["label"]
        assert generated_row["keyword_id"] == custom["keyword_id"]

        explicit = client.post("/api/tasks/generate", json={
            "text": "你好，芯宝", "label": "你好芯宝",
            "keyword_id": "ni_hao_xin_bao", "voice_ids": [voice["id"]],
            "count": 1, "seed": 20260911, "instruction": "explicit-values",
        })
        assert explicit.status_code == 200
        assert _wait_for_task(client, explicit.json()["task_id"])["status"] == "completed"
        explicit_row = app_module.db.one(
            "SELECT id,text,label,keyword_id FROM samples WHERE seed=? AND deleted_at IS NULL",
            (20260911,),
        )
        created_ids.append(explicit_row["id"])
        assert explicit_row["text"] == "你好，芯宝"
        assert explicit_row["label"] == "你好芯宝"
        assert explicit_row["keyword_id"] == "ni_hao_xin_bao"

        uploaded = client.post(
            "/api/samples/upload",
            files={"files": ("project-default.wav", _wav_bytes(0.7, 487.0), "audio/wav")},
            data={"speaker_id": "spk_project_default", "split": "dev"},
        )
        assert uploaded.status_code == 200
        assert uploaded.json()["imported"] == 1
        upload_id = uploaded.json()["sample_ids"][0]
        created_ids.append(upload_id)
        upload_row = app_module.db.one(
            "SELECT text,label,keyword_id FROM samples WHERE id=?", (upload_id,)
        )
        assert upload_row["text"] == custom["display_text"]
        assert upload_row["label"] == custom["label"]
        assert upload_row["keyword_id"] == custom["keyword_id"]
    finally:
        client.put("/api/settings/project", json={"display_text": "你好，芯宝"})
        for sample_id in created_ids:
            client.delete(f"/api/samples/{sample_id}")


def test_upload_duration_boundaries(client: TestClient) -> None:
    too_short_real = client.post(
        "/api/samples/upload",
        files={"files": ("short.wav", _wav_bytes(0.1), "audio/wav")},
        data={"speaker_id": "spk_short", "text": "你好，芯宝", "label": "你好芯宝"},
    )
    assert too_short_real.status_code == 200
    assert too_short_real.json()["imported"] == 0
    assert "0.2" in too_short_real.json()["failures"][0]["error"]

    too_short_noise = client.post(
        "/api/noises/upload",
        files={"file": ("short-noise.wav", _wav_bytes(0.1), "audio/wav")},
    )
    assert too_short_noise.status_code == 400
    assert "0.2" in too_short_noise.json()["detail"]


def test_generation_deduplication_includes_instruction_and_retry(client: TestClient) -> None:
    voice = _first_mock_voice(client)
    base = {
        "text": "你好，芯宝", "label": "你好芯宝", "keyword_id": "ni_hao_xin_bao",
        "voice_ids": [voice["id"]], "count": 1, "speed": 1.0,
        "pitch": 1.0, "volume": 50, "seed": 20260901,
    }
    first = client.post("/api/tasks/generate", json={**base, "instruction": "风格 A"})
    assert first.status_code == 200
    assert _wait_for_task(client, first.json()["task_id"])["status"] == "completed"
    second = client.post("/api/tasks/generate", json={**base, "instruction": "风格 B"})
    assert second.status_code == 200
    second_task = _wait_for_task(client, second.json()["task_id"])
    assert second_task["status"] == "completed"
    count = app_module.db.one(
        "SELECT COUNT(*) AS n FROM samples WHERE provider='mock' AND seed=? AND deleted_at IS NULL",
        (base["seed"],),
    )["n"]
    assert count == 2

    retried = client.post(f"/api/tasks/{second.json()['task_id']}/retry")
    assert retried.status_code == 200
    assert _wait_for_task(client, retried.json()["task_id"])["status"] == "completed"
    count_after_retry = app_module.db.one(
        "SELECT COUNT(*) AS n FROM samples WHERE provider='mock' AND seed=? AND deleted_at IS NULL",
        (base["seed"],),
    )["n"]
    assert count_after_retry == 2


def test_missing_sample_file_is_not_reported_recoverable(client: TestClient) -> None:
    voice = _first_mock_voice(client)
    created = client.post("/api/tasks/generate", json={
        "text": "你好，芯宝", "label": "你好芯宝", "keyword_id": "ni_hao_xin_bao",
        "voice_ids": [voice["id"]], "count": 1, "speed": 1.0,
        "pitch": 1.0, "volume": 50, "seed": 20260902,
        "instruction": "missing-file-test",
    })
    assert created.status_code == 200
    assert _wait_for_task(client, created.json()["task_id"])["status"] == "completed"
    row = app_module.db.one(
        "SELECT id,file_path FROM samples WHERE provider='mock' AND seed=? AND deleted_at IS NULL",
        (20260902,),
    )
    Path(row["file_path"]).unlink()
    deleted = client.delete(f"/api/samples/{row['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["recoverable"] is False
    assert client.post(f"/api/samples/{row['id']}/restore").status_code == 404
