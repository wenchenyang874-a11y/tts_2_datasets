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
import threading
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


def test_wekws_keyword_token_is_one_ascii_symbol() -> None:
    assert app_module._wekws_keyword_token("ni_hao_xiao_zhu_shou") == "<NI_HAO_XIAO_ZHU_SHOU>"
    fallback = app_module._wekws_keyword_token("你好小助手")
    assert fallback.startswith("<KWS_") and fallback.endswith(">")
    assert fallback.isascii() and " " not in fallback


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


def test_voice_language_filters_handle_bilingual_foreign_and_unknown(client):
    chinese = client.get("/api/voices", params={"provider": "xiaomi", "language_group": "chinese"}).json()
    foreign = client.get("/api/voices", params={"provider": "xiaomi", "language_group": "foreign"}).json()
    unknown = client.get("/api/voices", params={"provider": "xiaomi", "language_group": "unknown"}).json()
    assert {row["voice_id"] for row in chinese} == {"冰糖", "茉莉", "苏打", "白桦"}
    assert {row["voice_id"] for row in foreign} == {"Mia", "Chloe", "Milo", "Dean"}
    assert [row["voice_id"] for row in unknown] == ["mimo_default"]
    aliyun_chinese = client.get("/api/voices", params={"provider": "aliyun", "language_group": "chinese"}).json()
    aliyun_foreign = client.get("/api/voices", params={"provider": "aliyun", "language_group": "foreign"}).json()
    assert {row["id"] for row in aliyun_chinese} & {row["id"] for row in aliyun_foreign}
    assert client.get("/api/voices", params={"language_group": "invalid"}).status_code == 400


def test_voice_capability_filter_matches_exact_models_and_languages(client):
    from voice_features import ALI_FIXED_VOICES
    supported = client.get("/api/voices", params={"provider": "aliyun", "capability": "instruction", "language_group": "foreign"}).json()
    assert {(row["model"], row["voice_id"]) for row in supported} == ALI_FIXED_VOICES
    emotions = client.get("/api/voices", params={"provider": "aliyun", "capability": "emotion"}).json()
    assert {row["id"] for row in emotions} == {row["id"] for row in supported}
    unsupported = client.get("/api/voices", params={"provider": "aliyun", "capability": "no_instruction"}).json()
    assert unsupported and all(row["expression_features"]["instruction"] == "none" for row in unsupported)
    assert not ({row["id"] for row in unsupported} & {row["id"] for row in supported})
    assert client.get("/api/voices", params={"capability": "invalid"}).status_code == 400


def test_unsupported_expression_blocks_task_and_batch_before_creation(client, monkeypatch, tmp_path):
    from settings_store import ProviderSettingsStore
    store = ProviderSettingsStore(tmp_path / "expression-settings.json")
    store.update_provider("volcengine", {"api_key": "fixture"})
    monkeypatch.setattr(app_module, "provider_settings", store)
    voices = client.get("/api/voices", params={"provider": "volcengine_v1"}).json()
    voice = next(row for row in voices if not row["expression_features"]["emotions"])
    monkeypatch.setattr(app_module, "_submit", lambda *args: pytest.fail("No task may be submitted"))
    before = app_module.db.one("SELECT COUNT(*) AS n FROM tasks")["n"]
    for path in ("/api/generation/preflight", "/api/tasks/generate", "/api/tasks/generate-batch"):
        response = client.post(path, json={"voice_ids": [voice["id"]], "texts": ["测试"], "instruction": "开心一些", "cost_acknowledged": True})
        assert response.status_code == 400 and "不支持表达指令" in response.json()["detail"]
    assert app_module.db.one("SELECT COUNT(*) AS n FROM tasks")["n"] == before


def test_emotion_participates_in_dedup_and_sample_metadata(client):
    voice = _first_mock_voice(client)
    base = {"voice_ids": [voice["id"]], "text": "情感去重验收", "label": "情感去重验收", "keyword_id": "emotion_dedup_fixture", "seed": 826519}
    ids = []
    for emotion in ("happy", "sad", "happy"):
        result = client.post("/api/tasks/generate", json={**base, "emotion": emotion})
        assert result.status_code == 200
        task = _wait_for_task(client, result.json()["id"])
        assert task["status"] == "completed"
        ids.append(result.json()["id"])
    rows = app_module.db.all("SELECT metadata_json FROM samples WHERE keyword_id=? AND deleted_at IS NULL", (base["keyword_id"],))
    assert len(rows) == 2
    assert {json.loads(row["metadata_json"])["emotion"] for row in rows} == {"happy", "sad"}
    assert all("provider_expression_request" in json.loads(row["metadata_json"]) for row in rows)
    owned = app_module.db.all("SELECT file_path FROM samples WHERE keyword_id=?", (base["keyword_id"],))
    for row in owned:
        path = Path(row["file_path"]).resolve()
        assert path.is_relative_to(TEST_DATA_DIR.resolve())
        path.unlink(missing_ok=True)
    app_module.db.execute("DELETE FROM samples WHERE keyword_id=?", (base["keyword_id"],))


def test_current_provider_form_validation_is_non_persisting_and_keeps_saved_secret(client, monkeypatch, tmp_path):
    from settings_store import ProviderSettingsStore
    store = ProviderSettingsStore(tmp_path / "settings.json")
    monkeypatch.setattr(app_module, "provider_settings", store)
    result = client.post("/api/settings/providers/xiaomi/test", json={"api_key": "unsaved-fixture-key"})
    assert result.status_code == 200 and result.json()["remote_verified"] is False
    assert "unsaved-fixture-key" not in result.text and store.get_provider("xiaomi") == {}
    client.put("/api/settings/providers/xiaomi", json={"api_key": "saved-fixture-key"})
    assert client.post("/api/settings/providers/xiaomi/test", json={"api_key": ""}).status_code == 200
    invalid = client.post("/api/settings/providers/xiaomi/test", json={"endpoint": "https://untrusted.example/v1"})
    assert invalid.status_code == 400
    assert store.get_provider("xiaomi")["api_key"] == "saved-fixture-key"


def test_xiaomi_cloud_cost_never_defaults_to_free(monkeypatch, tmp_path):
    from settings_store import ProviderSettingsStore
    monkeypatch.setattr(app_module, "provider_settings", ProviderSettingsStore(tmp_path / "settings.json"))
    voice = {"provider": "xiaomi"}
    assert app_module._provider_audio_for({}, "xiaomi")["sample_rate"] == 24000
    with pytest.raises(app_module.HTTPException) as error:
        app_module._require_cost_acknowledgement([voice], False)
    assert error.value.status_code == 400
    assert app_module._estimate_generation_cost("请开灯", [voice], 1)["cost_available"] is False


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
    # Production startup may execute initialization repeatedly; migrations
    # must remain idempotent after the first schema upgrade.
    app_module.Database(database_path)
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


def test_legacy_sample_and_task_tables_gain_batch_and_semantic_columns() -> None:
    database_path = TEST_DATA_DIR / "legacy-batch-and-semantics.sqlite3"
    with sqlite3.connect(database_path) as legacy:
        legacy.executescript(
            """
            CREATE TABLE samples (
                id TEXT PRIMARY KEY, provider TEXT NOT NULL, voice_id TEXT NOT NULL,
                voice_name TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL, label TEXT NOT NULL, keyword_id TEXT NOT NULL,
                speed REAL NOT NULL, pitch REAL NOT NULL, volume INTEGER NOT NULL,
                seed INTEGER NOT NULL, file_path TEXT NOT NULL, duration REAL NOT NULL,
                sample_rate INTEGER NOT NULL, channels INTEGER NOT NULL,
                sha256 TEXT NOT NULL, parent_id TEXT, noise_id TEXT, snr_db REAL,
                split TEXT NOT NULL, quality_status TEXT NOT NULL, notes TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                target_kind TEXT NOT NULL DEFAULT 'positive',
                created_at TEXT NOT NULL, deleted_at TEXT
            );
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, type TEXT NOT NULL, status TEXT NOT NULL,
                total INTEGER NOT NULL, completed INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}', result_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            INSERT INTO samples(
                id,provider,voice_id,text,label,keyword_id,speed,pitch,volume,seed,
                file_path,duration,sample_rate,channels,sha256,split,quality_status,
                notes,metadata_json,target_kind,created_at
            ) VALUES(
                'legacy-noise','noise','room','room','__noise__','legacy_kw',1,1,50,0,
                '/tmp/legacy.wav',1,16000,1,'legacy-sha','unassigned','pass','',
                '{}','noise','2026-01-01T00:00:00+00:00'
            );
            """
        )

    migrated = app_module.Database(database_path)
    app_module.Database(database_path)
    with migrated.connect() as connection:
        sample_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(samples)")
        }
        task_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(tasks)")
        }
        sample_indexes = {
            row["name"] for row in connection.execute("PRAGMA index_list(samples)")
        }
        task_indexes = {
            row["name"] for row in connection.execute("PRAGMA index_list(tasks)")
        }
        semantic_row = connection.execute(
            "SELECT target_kind,source_kind,variant_kind FROM samples WHERE id='legacy-noise'"
        ).fetchone()

    assert {
        "target_kind", "source_kind", "variant_kind", "origin_task_id", "origin_job_id",
    } <= sample_columns
    assert {
        "parent_id", "item_index", "started_at", "finished_at",
        "estimated_cost", "currency", "billable_units",
    } <= task_columns
    assert {"idx_samples_keyword_target", "idx_samples_origin_job"} <= sample_indexes
    assert {"idx_tasks_parent", "idx_tasks_status"} <= task_indexes
    assert tuple(semantic_row) == ("background", "noise", "clean")


def test_provider_settings_api_masks_secrets_and_separates_volc_credentials(
    client: TestClient,
) -> None:
    client.delete("/api/settings/providers/aliyun")
    client.delete("/api/settings/providers/volcengine")

    unsupported_region = client.put("/api/settings/providers/aliyun", json={
        "region": "invalid-region",
    })
    assert unsupported_region.status_code == 400
    assert "cn-beijing" in unsupported_region.json()["detail"]

    aliyun_secret = "unit-test-api-route-secret-5678"
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
    if os.name != "nt":
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
        "api_key": "volc-synthesis-secret",
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
    reset = client.put("/api/settings/project", json={"display_text": "你好，小助手"})
    assert reset.status_code == 200
    assert client.get("/api/settings/project").json()["keyword_id"] == "ni_hao_xiao_zhu_shou"

    updated = client.put("/api/settings/project", json={"display_text": "  测试，唤 醒！  "})
    assert updated.status_code == 200
    project = updated.json()
    assert project["display_text"] == "测试，唤 醒！"
    assert project["label"] == "测试唤醒"
    assert project["keyword_id"].startswith("kws_")
    assert len(project["keyword_id"]) == 16
    assert project["updated_at"]
    if os.name != "nt":
        assert stat.S_IMODE((TEST_DATA_DIR / "project_settings.json").stat().st_mode) == 0o600

    status = client.get("/api/status")
    assert status.status_code == 200
    assert status.json()["app"] == "语音数据集工坊"
    assert status.json()["project"] == project
    assert app_module.app.title == "语音数据集工坊"

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
    assert client.get("/api/status").status_code == 401
    assert client.get(
        "/api/status", headers={"X-App-Token": "project-settings-test-token"}
    ).status_code == 200
    assert client.get("/healthz").status_code == 200

    monkeypatch.delenv("TTS_APP_API_TOKEN")
    restored = client.put("/api/settings/project", json={"display_text": "你好，小助手"})
    assert restored.status_code == 200
    assert restored.json()["keyword_id"] == "ni_hao_xiao_zhu_shou"


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
    secret = "unit-test-unexpected-secret-value"

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
        "text": "你好，小伙伴", "label": "你好小伙伴", "keyword_id": "ni_hao_xiao_huo_ban",
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
    for next_status in ("rejected", "pending", "accepted"):
        cycled_review = client.patch(
            f"/api/samples/{sample_id}", json={"quality_status": next_status}
        )
        assert cycled_review.status_code == 200
        assert cycled_review.json()["quality_status"] == next_status

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
    augmented_result = augmented.json()
    assert len(augmented_result["child_ids"]) == 1
    augmented_task = _wait_for_task(client, augmented_result["task_id"])
    assert augmented_task["status"] == "completed"
    assert augmented_task["type"] == "augment_batch"
    assert len(augmented_task["children"]) == 1
    assert augmented_task["children"][0]["type"] == "augment"
    assert augmented_task["children"][0]["parent_id"] == augmented_task["id"]
    assert augmented_task["cost_available"] is True
    assert augmented_task["estimated_cost"] == 0
    assert "本地操作" in augmented_task["cost_disclaimer"]
    augmented_rows = client.get("/api/samples", params={"sample_type": "augmented"}).json()["items"]
    augmented_sample = next(item for item in augmented_rows if item["parent_id"] == sample_id)
    assert augmented_sample["variant_kind"] == "augmented"
    assert augmented_sample["quality_status"] == "pending"
    assert augmented_sample["metadata"]["source_quality_status"] == "accepted"
    assert augmented_sample["origin_job_id"] == augmented_task["id"]
    assert augmented_sample["origin_task_id"] == augmented_task["children"][0]["id"]
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
        "/api/settings/project", json={"display_text": "你好，小助手"}
    ).json()
    exported = client.post("/api/exports", json={
        "name": "ni_hao_xiao_huo_ban_test", "format": "wekws", "include_audio": True,
        "train_ratio": 0.8, "dev_ratio": 0.1, "seed": 20260805,
        "keyword_id": "ni_hao_xiao_huo_ban",
    })
    assert exported.status_code == 200
    export_task = _wait_for_task(client, exported.json()["task_id"])
    assert export_task["status"] == "completed"
    assert export_task["cost_available"] is True
    assert export_task["estimated_cost"] == 0
    assert "本地操作" in export_task["cost_disclaimer"]
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
        assert dataset_metadata["wake_word"] == "你好小伙伴"
        assert dataset_metadata["label"] == "你好小伙伴"
        assert dataset_metadata["keyword_id"] == "ni_hao_xiao_huo_ban"
        assert dataset_metadata["display_text_unavailable"] is True
        assert dataset_metadata["keyword_id"] != export_project["keyword_id"]
        assert any(name.startswith("manifests/") for name in names)
        assert any(name.startswith("audio/") for name in names)
        assert bundle.read("wekws/dict/dict.txt").decode() == (
            "<FILLER> -1\n<NI_HAO_XIAO_HUO_BAN> 0\n"
        )
        assert bundle.read("wekws/dict/words.txt").decode() == (
            "<FILLER>\n<NI_HAO_XIAO_HUO_BAN>\n"
        )
        assert "max-pooling single-keyword" in bundle.read("wekws/README.txt").decode()
        assert any(name.endswith("/wav.dur") for name in names)
        manifest_records = []
        for name in names:
            if name.startswith("manifests/") and name.endswith(".jsonl"):
                manifest_records.extend(
                    json.loads(line) for line in bundle.read(name).decode().splitlines() if line
                )
        exported_augmented = next(item for item in manifest_records if item["utt_id"] == augmented_sample["id"])
        assert exported_augmented["noise_offset"] == augmented_sample["metadata"]["noise_offset"]
        assert exported_augmented["source"] == "tts_augmented"
        assert exported_augmented["sample_rate"] == 16_000
        assert exported_augmented["channels"] == 1
        assert exported_augmented["sample_width_bits"] == 16
        assert exported_augmented["audio_format"] == "wav"
        assert isinstance(exported_augmented["volume"], int)
        assert "duration" in exported_augmented
        assert "quality_status" in exported_augmented
        assert "metadata" in exported_augmented
    assert client.put(
        "/api/settings/project", json={"display_text": "你好，小助手"}
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

    audit_export = client.post("/api/exports", json={
        "sample_ids": [sample_id], "keyword_id": "ni_hao_xiao_huo_ban",
        "name": "audit_jsonl", "format": "jsonl", "include_audio": False,
        "train_ratio": 0.8, "dev_ratio": 0.1, "seed": 20260805,
    })
    assert audit_export.status_code == 200
    audit_task = _wait_for_task(client, audit_export.json()["task_id"])
    assert audit_task["status"] == "completed"
    audit_archive = client.get(
        f"/api/exports/{audit_task['result']['export_id']}/download"
    )
    with zipfile.ZipFile(io.BytesIO(audit_archive.content)) as bundle:
        audit_records = [
            json.loads(line)
            for name in bundle.namelist()
            if name.startswith("manifests/") and name.endswith(".jsonl")
            for line in bundle.read(name).decode().splitlines()
            if line
        ]
        audit_record = next(item for item in audit_records if item["utt_id"] == sample_id)
        assert audit_record["volume"] == 50
        assert audit_record["duration"] > 0
        assert audit_record["sample_rate"] == 16_000
        assert audit_record["channels"] == 1
        assert audit_record["sample_width_bits"] == 16
        assert audit_record["audio_format"] == "wav"
        assert audit_record["quality_status"] == "accepted"
        assert audit_record["notes"] == "offline integration test"
        assert audit_record["created_at"]
        assert audit_record["provider_audio_request"] == {
            "format": "wav", "sample_rate": 16_000,
        }
        assert audit_record["normalized_output_audio"] == {
            "format": "wav", "sample_rate": 16_000,
            "channels": 1, "sample_width_bits": 16,
        }
        assert audit_record["metadata"]["voice_db_id"] == voices[0]["id"]

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
            "text": "你好，小助手", "label": "你好小助手", "speaker_id": "spk_001",
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
    speaker_catalog = client.get("/api/samples/speakers")
    assert speaker_catalog.status_code == 200
    speaker = next(
        item for item in speaker_catalog.json() if item["speaker_id"] == "spk_001"
    )
    assert speaker["sample_count"] == 1
    assert speaker["original_count"] == 1
    assert speaker["last_used_at"]
    page_response = client.get("/")
    page_html = page_response.text
    assert page_response.headers["cache-control"] == "no-store, max-age=0"
    assert 'id="sampleExistingSpeaker"' in page_html
    assert "手动输入新 ID" in page_html
    assert "/app.js?v=1.2.3" in page_html
    script_response = client.get("/app.js?v=1.2.3")
    assert script_response.headers["cache-control"] == "no-store, max-age=0"
    assert '$("uploadSampleDialog").close()' not in script_response.text
    assert 'data-action="cycle-review"' in script_response.text
    assert "cycleSampleReview" in script_response.text
    original_page = client.get(
        "/api/samples", params={"provider": "real", "sample_type": "clean"}
    )
    assert original_page.status_code == 200
    assert original_page.json()["total"] == 1
    ambiguous_type = client.get("/api/samples", params={"sample_type": "real"})
    assert ambiguous_type.status_code == 400
    assert "provider=real" in ambiguous_type.json()["detail"]

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
            "text": "你好，小助手", "label": "你好小助手", "speaker_id": "spk_001",
            "split": "test", "gender": "female", "age_group": "young",
        },
    )
    assert conflicting.status_code == 200
    conflict_id = conflicting.json()["sample_ids"][0]
    updated_speaker = next(
        item for item in client.get("/api/samples/speakers").json()
        if item["speaker_id"] == "spk_001"
    )
    assert updated_speaker["sample_count"] == 2
    assert updated_speaker["original_count"] == 2
    conflict_export = client.post("/api/exports", json={
        "sample_ids": [sample_id, conflict_id], "name": "conflicting_real_split",
        "format": "generic", "include_audio": False,
        "train_ratio": 0.8, "dev_ratio": 0.1,
    })
    assert conflict_export.status_code == 400
    assert "spk_001" in conflict_export.json()["detail"]


def test_augmentation_uses_parent_children_and_resets_review_status(
    client: TestClient,
) -> None:
    source_ids: list[str] = []
    for index, frequency in enumerate((731.0, 733.0), start=1):
        uploaded = client.post(
            "/api/samples/upload",
            files={
                "files": (
                    f"augment-source-{index}.wav",
                    _wav_bytes(0.7, frequency),
                    "audio/wav",
                )
            },
            data={
                "text": "增强批次测试",
                "label": "增强批次测试",
                "keyword_id": "augment_batch_keyword",
                "speaker_id": f"augment_speaker_{index}",
                "target_kind": "positive",
            },
        )
        assert uploaded.status_code == 200
        sample_id = uploaded.json()["sample_ids"][0]
        source_ids.append(sample_id)
        accepted = client.patch(
            f"/api/samples/{sample_id}", json={"quality_status": "accepted"}
        )
        assert accepted.status_code == 200

    noise_upload = client.post(
        "/api/noises/upload",
        files={
            "file": (
                "augment-batch-noise.wav",
                _wav_bytes(0.9, 89.0),
                "audio/wav",
            )
        },
        data={"name": "增强批次测试噪声", "category": "test"},
    )
    assert noise_upload.status_code == 200

    created = client.post(
        "/api/augment",
        json={
            "sample_ids": source_ids,
            "keyword_id": "augment_batch_keyword",
            "noise_ids": [noise_upload.json()["id"]],
            "count": 2,
            "seed": 20261004,
        },
    )
    assert created.status_code == 200
    created_json = created.json()
    assert len(created_json["child_ids"]) == 2

    parent = _wait_for_task(client, created_json["parent_id"])
    assert parent["type"] == "augment_batch"
    assert parent["status"] == "completed"
    assert parent["total"] == 4
    assert parent["completed"] == 4
    assert len(parent["children"]) == 2
    assert [child["item_index"] for child in parent["children"]] == [0, 1]
    assert all(child["type"] == "augment" for child in parent["children"])
    assert all(child["total"] == 2 and child["completed"] == 2 for child in parent["children"])
    assert {
        child["payload"]["source_sample_id"] for child in parent["children"]
    } == set(source_ids)

    rows = client.get(
        "/api/samples",
        params={
            "keyword_id": "augment_batch_keyword",
            "sample_type": "augmented",
            "origin_job_id": parent["id"],
            "page_size": 20,
        },
    ).json()
    assert rows["total"] == 4
    assert all(sample["variant_kind"] == "augmented" for sample in rows["items"])
    assert all(sample["quality_status"] == "pending" for sample in rows["items"])
    assert all(
        sample["metadata"]["source_quality_status"] == "accepted"
        for sample in rows["items"]
    )
    assert {sample["origin_task_id"] for sample in rows["items"]} == set(
        created_json["child_ids"]
    )


def test_omitted_kws_fields_follow_project_and_explicit_values_are_preserved(
    client: TestClient,
) -> None:
    custom = client.put(
        "/api/settings/project", json={"display_text": "你好，小助手"}
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
            "text": "你好，小助手", "label": "你好小助手",
            "keyword_id": "ni_hao_xiao_zhu_shou", "voice_ids": [voice["id"]],
            "count": 1, "seed": 20260911, "instruction": "explicit-values",
        })
        assert explicit.status_code == 200
        assert _wait_for_task(client, explicit.json()["task_id"])["status"] == "completed"
        explicit_row = app_module.db.one(
            "SELECT id,text,label,keyword_id FROM samples WHERE seed=? AND deleted_at IS NULL",
            (20260911,),
        )
        created_ids.append(explicit_row["id"])
        assert explicit_row["text"] == "你好，小助手"
        assert explicit_row["label"] == "你好小助手"
        assert explicit_row["keyword_id"] == "ni_hao_xiao_zhu_shou"

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
        client.put("/api/settings/project", json={"display_text": "你好，小助手"})
        for sample_id in created_ids:
            client.delete(f"/api/samples/{sample_id}")


def test_upload_duration_boundaries(client: TestClient) -> None:
    too_short_real = client.post(
        "/api/samples/upload",
        files={"files": ("short.wav", _wav_bytes(0.1), "audio/wav")},
        data={"speaker_id": "spk_short", "text": "你好，小助手", "label": "你好小助手"},
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
        "text": "你好，小助手", "label": "你好小助手", "keyword_id": "ni_hao_xiao_zhu_shou",
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
        "text": "你好，小助手", "label": "你好小助手", "keyword_id": "ni_hao_xiao_zhu_shou",
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


def test_batch_generation_caps_concurrency_and_preserves_lineage(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    voice = _first_mock_voice(client)
    requested_concurrency = min(16, max(3, app_module.WORKER_LIMIT + 3))
    state = {"active": 0, "max_active": 0}
    lock = threading.Lock()

    class TrackingProvider:
        def synthesize(self, **kwargs):
            with lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            try:
                time.sleep(0.08)
                return _wav_bytes(0.6, 220.0 + int(kwargs["seed"]) % 500)
            finally:
                with lock:
                    state["active"] -= 1

    provider = TrackingProvider()
    monkeypatch.setattr(app_module, "get_provider", lambda *args, **kwargs: provider)
    payload = {
        "texts": ["批量甲", "批量乙", "批量丙"],
        "label": "批量唤醒", "keyword_id": "batch_keyword",
        "target_kind": "noise", "voice_ids": [voice["id"]], "count": 1,
        "concurrency": requested_concurrency, "seed": 20261001,
        "random_speed": True, "speed_min": 0.81, "speed_max": 1.19,
        "random_pitch": True, "pitch_min": 0.91, "pitch_max": 1.09,
        "random_volume": True, "volume_min": 42, "volume_max": 58,
        "provider_audio": {"mock": {"format": "wav", "sample_rate": 16000}},
    }

    created = client.post("/api/tasks/generate-batch", json=payload)
    assert created.status_code == 200
    created_json = created.json()
    assert created_json["requested_concurrency"] == requested_concurrency
    assert created_json["applied_concurrency"] == min(
        requested_concurrency, app_module.WORKER_LIMIT, len(payload["texts"])
    )
    assert len(created_json["child_ids"]) == 3

    parent = _wait_for_task(client, created_json["parent_id"])
    assert parent["status"] == "completed"
    assert parent["completed"] == 3
    assert parent["requested_concurrency"] == requested_concurrency
    assert parent["applied_concurrency"] == created_json["applied_concurrency"]
    assert parent["started_at"] and parent["finished_at"]
    assert parent["total_duration_seconds"] is not None
    assert [child["item_index"] for child in parent["children"]] == [0, 1, 2]
    assert all(child["parent_id"] == parent["id"] for child in parent["children"])
    assert all(child["status"] == "completed" for child in parent["children"])
    assert all(child["queue_duration_seconds"] is not None for child in parent["children"])
    assert all(child["execution_duration_seconds"] is not None for child in parent["children"])
    assert state["max_active"] <= created_json["applied_concurrency"]
    assert state["max_active"] == created_json["applied_concurrency"]

    page = client.get("/api/samples", params={
        "keyword_id": "batch_keyword", "target_kind": "background",
        "origin_job_id": parent["id"],
    }).json()
    assert page["total"] == 3
    assert {sample["text"] for sample in page["items"]} == set(payload["texts"])
    assert all(sample["target_kind"] == "background" for sample in page["items"])
    assert all(sample["source_kind"] == "tts" for sample in page["items"])
    assert all(sample["variant_kind"] == "clean" for sample in page["items"])
    assert all(sample["origin_job_id"] == parent["id"] for sample in page["items"])
    assert all(sample["origin_task_id"] in created_json["child_ids"] for sample in page["items"])
    assert all(0.81 <= sample["speed"] <= 1.19 for sample in page["items"])
    assert all(0.91 <= sample["pitch"] <= 1.09 for sample in page["items"])
    assert all(42 <= sample["volume"] <= 58 for sample in page["items"])

    ids = client.get("/api/samples/ids", params={
        "keyword_id": "batch_keyword", "target_kind": "noise",
        "origin_job_id": parent["id"],
    }).json()
    assert ids["total"] == 3
    assert set(ids["ids"]) == {sample["id"] for sample in page["items"]}
    keyword_summary = next(
        item for item in client.get("/api/samples/keywords").json()
        if item["keyword_id"] == "batch_keyword"
    )
    assert keyword_summary["background_count"] == 3

    # The same seed and item identity must reproduce the same randomized
    # parameters, allowing the existing samples to be reused deterministically.
    repeated = client.post("/api/tasks/generate-batch", json=payload)
    assert repeated.status_code == 200
    repeated_parent = _wait_for_task(client, repeated.json()["parent_id"])
    assert repeated_parent["status"] == "completed"
    assert all(child["result"]["duplicates"] == 1 for child in repeated_parent["children"])
    assert client.get("/api/samples", params={"keyword_id": "batch_keyword"}).json()["total"] == 3


def test_cost_estimate_distinguishes_unavailable_and_mixed_currency(
    client: TestClient,
) -> None:
    client.delete("/api/settings/providers/aliyun")
    client.delete("/api/settings/providers/volcengine")
    aliyun_voice = {"provider": "aliyun"}
    volc_voice = {"provider": "volcengine"}

    unavailable = app_module._estimate_generation_cost("计费未知", [aliyun_voice], 1)
    assert unavailable["cost_available"] is False
    assert unavailable["estimated_cost"] == 0
    assert unavailable["cost_breakdown"][0]["cost_available"] is False
    assert "最终费用" in unavailable["cost_disclaimer"]

    assert client.put("/api/settings/providers/aliyun", json={
        "pricing_unit": "per_call", "pricing_rate": 1.0,
        "pricing_currency": "CNY",
    }).status_code == 200
    assert client.put("/api/settings/providers/volcengine", json={
        "pricing_unit": "per_call", "pricing_rate": 2.0,
        "pricing_currency": "USD",
    }).status_code == 200
    mixed = app_module._estimate_generation_cost(
        "混合币种", [aliyun_voice, volc_voice], 1
    )
    assert mixed["cost_available"] is False
    assert mixed["estimated_cost"] == 0
    assert mixed["currency"] == "MIXED"
    assert mixed["cost_by_currency"] == {"CNY": 1.0, "USD": 2.0}

    client.delete("/api/settings/providers/aliyun")
    client.delete("/api/settings/providers/volcengine")


def test_cloud_generation_requires_fresh_cost_ack_and_reports_estimate(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client.delete("/api/settings/providers/aliyun")
    configured = client.put("/api/settings/providers/aliyun", json={
        "api_key": "test-only-aliyun-key",
        "workspace_id": "test-workspace",
        "pricing_unit": "per_1k_chars",
        "pricing_rate": 2.0,
        "pricing_currency": "cny",
    })
    assert configured.status_code == 200
    app_module.db.upsert_voice({
        "provider": "aliyun", "voice_id": "billing-test-voice",
        "name": "计费测试音色", "model": "cosyvoice-v3.5-plus",
        "source": "account_custom", "enabled": True,
    })
    voice = app_module.db.one(
        "SELECT id FROM voices WHERE provider='aliyun' AND voice_id='billing-test-voice'"
    )

    class BillingProvider:
        def synthesize(self, **kwargs):
            return _wav_bytes(0.6, 280.0 + int(kwargs["seed"]) % 400)

    monkeypatch.setattr(app_module, "get_provider", lambda *args, **kwargs: BillingProvider())
    payload = {
        "text": "计费测试", "label": "计费测试", "keyword_id": "billing_keyword",
        "voice_ids": [voice["id"]], "count": 2, "seed": 20261002,
    }
    task_count_before = app_module.db.one("SELECT COUNT(*) AS n FROM tasks")["n"]
    rejected = client.post("/api/tasks/generate", json=payload)
    assert rejected.status_code == 400
    assert "cost_acknowledged=true" in rejected.json()["detail"]
    assert app_module.db.one("SELECT COUNT(*) AS n FROM tasks")["n"] == task_count_before

    created = client.post(
        "/api/tasks/generate", json={**payload, "cost_acknowledged": True}
    )
    assert created.status_code == 200
    assert created.json()["cost_available"] is True
    assert created.json()["cost_is_estimate"] is True
    assert created.json()["currency"] == "CNY"
    assert created.json()["estimated_cost"] == pytest.approx(0.016)
    completed = _wait_for_task(client, created.json()["task_id"])
    assert completed["status"] == "completed"
    assert completed["estimated_cost"] == pytest.approx(0.016)
    assert completed["billable_units"] == pytest.approx(0.008)
    assert completed["cost_available"] is True
    assert "供应商账单" in completed["cost_disclaimer"]

    no_ack_retry = client.post(f"/api/tasks/{completed['id']}/retry")
    assert no_ack_retry.status_code == 400
    acknowledged_retry = client.post(
        f"/api/tasks/{completed['id']}/retry",
        json={"cost_acknowledged": True},
    )
    assert acknowledged_retry.status_code == 200
    assert _wait_for_task(client, acknowledged_retry.json()["task_id"])["status"] == "completed"

    status = client.get("/api/status").json()
    assert status["worker_limit"] == app_module.WORKER_LIMIT
    assert "queued_tasks" in status and "running_tasks" in status
    assert client.get("/healthz").json()["app"] == "语音数据集工坊"
    capabilities = client.get("/api/providers/capabilities").json()
    assert 16000 in capabilities["aliyun"]["sample_rates"]
    assert "wav" in capabilities["volcengine"]["audio_formats"]

    client.delete("/api/settings/providers/aliyun")


def test_batch_rejects_unsupported_volcengine_pitch_before_billing(
    client: TestClient,
) -> None:
    app_module.db.upsert_voice({
        "provider": "volcengine", "voice_id": "volc-batch-pitch-test",
        "name": "火山批量音高测试", "model": "seed-tts-1.0",
        "source": "provider_catalog", "enabled": True,
    })
    voice = app_module.db.one(
        "SELECT id FROM voices WHERE provider='volcengine' "
        "AND voice_id='volc-batch-pitch-test'"
    )
    base = {
        "texts": ["音高测试"], "voice_ids": [voice["id"]],
        "cost_acknowledged": True,
    }
    # Use the API so validation order and response text are covered.
    random_response = client.post(
        "/api/tasks/generate-batch",
        json={**base, "random_pitch": True, "pitch_min": 1.0, "pitch_max": 1.0},
    )
    assert random_response.status_code == 400
    assert "随机音高" in random_response.json()["detail"]
    fixed_response = client.post(
        "/api/tasks/generate-batch", json={**base, "pitch": 1.1}
    )
    assert fixed_response.status_code == 400
    assert "固定音高" in fixed_response.json()["detail"]


def test_top_level_task_list_keeps_large_batch_group_complete(
    client: TestClient,
) -> None:
    parent_id = app_module._create_task(
        "generate_batch", 101, {"texts": [f"文本{i}" for i in range(101)]},
        result={"requested_concurrency": 3, "applied_concurrency": 2},
    )
    child_ids = [
        app_module._create_task(
            "generate", 1, {"text": f"文本{index}"},
            parent_id=parent_id, item_index=index,
        )
        for index in range(101)
    ]
    app_module._update_task(parent_id, status="completed", completed=101)

    top_level = client.get("/api/tasks", params={"limit": 100}).json()
    parent = next(task for task in top_level if task["id"] == parent_id)
    assert len(parent["children"]) == 101
    assert [child["id"] for child in parent["children"]] == child_ids
    assert not set(child_ids) & {task["id"] for task in top_level}
    detail = client.get(f"/api/tasks/{parent_id}").json()
    assert len(detail["children"]) == 101


def test_sample_isolation_semantics_materialization_and_wekws_export(
    client: TestClient,
) -> None:
    first_upload = client.post(
        "/api/samples/upload",
        files={"files": ("same-a.wav", _wav_bytes(0.7, 612.0), "audio/wav")},
        data={
            "text": "唤醒甲", "label": "唤醒甲", "keyword_id": "keyword_a",
            "speaker_id": "speaker_a", "target_kind": "positive",
        },
    )
    assert first_upload.status_code == 200
    first_id = first_upload.json()["sample_ids"][0]
    first_sample = client.get(
        "/api/samples", params={"keyword_id": "keyword_a"}
    ).json()["items"][0]
    assert first_sample["metadata"]["original_content_type"] == "audio/wav"

    # Identical bytes are still valid distinct rows across keyword/target
    # semantics; global SHA-only deduplication would incorrectly drop them.
    second_upload = client.post(
        "/api/samples/upload",
        files={"files": ("same-b.wav", _wav_bytes(0.7, 612.0), "audio/wav")},
        data={
            "text": "干扰短语", "label": "唤醒乙", "keyword_id": "keyword_b",
            "speaker_id": "speaker_b", "target_kind": "negative",
        },
    )
    assert second_upload.status_code == 200
    assert second_upload.json()["imported"] == 1
    second_id = second_upload.json()["sample_ids"][0]
    cross_target_upload = client.post(
        "/api/samples/upload",
        files={"files": ("same-c.wav", _wav_bytes(0.7, 612.0), "audio/wav")},
        data={
            "text": "甲的干扰短语", "label": "唤醒甲", "keyword_id": "keyword_a",
            "speaker_id": "speaker_c", "target_kind": "negative",
        },
    )
    assert cross_target_upload.status_code == 200
    assert cross_target_upload.json()["imported"] == 1

    assert client.post("/api/exports", json={
        "sample_ids": [], "name": "missing-keyword",
    }).status_code == 400
    assert client.post("/api/augment", json={
        "sample_ids": [], "noise_ids": [],
    }).status_code == 400
    missing_id = client.post("/api/exports", json={
        "sample_ids": [first_id, "missing-sample"], "keyword_id": "keyword_a",
        "name": "missing-id",
    })
    assert missing_id.status_code == 400
    assert "不存在或已删除" in missing_id.json()["detail"]
    mismatched_keyword = client.post("/api/exports", json={
        "sample_ids": [first_id], "keyword_id": "keyword_b",
        "name": "mismatch",
    })
    assert mismatched_keyword.status_code == 400
    mixed_keywords = client.post("/api/exports", json={
        "sample_ids": [first_id, second_id], "name": "mixed-keywords",
    })
    assert mixed_keywords.status_code == 400
    assert "同一个 keyword_id" in mixed_keywords.json()["detail"]

    duplicate_selection = client.post("/api/exports", json={
        "sample_ids": [first_id, first_id], "keyword_id": "keyword_a",
        "name": "deduplicated-selection", "include_audio": False,
    })
    assert duplicate_selection.status_code == 200
    duplicate_task = _wait_for_task(client, duplicate_selection.json()["task_id"])
    assert duplicate_task["status"] == "completed"
    assert duplicate_task["completed"] == 1

    noise_upload = client.post(
        "/api/noises/upload",
        files={"file": ("semantic-noise.wav", _wav_bytes(0.8, 97.0), "audio/wav")},
        data={"name": "语义测试噪声", "category": "test"},
    )
    assert noise_upload.status_code == 200
    noise_id = noise_upload.json()["id"]
    missing_noise = client.post("/api/noises/materialize", json={
        "noise_ids": [noise_id, "missing-noise"], "keyword_id": "keyword_b",
    })
    assert missing_noise.status_code == 400
    materialized = client.post("/api/noises/materialize", json={
        "noise_ids": [noise_id], "keyword_id": "keyword_b",
    })
    assert materialized.status_code == 200
    background_id = materialized.json()["sample_ids"][0]
    materialize_task = client.get(
        f"/api/tasks/{materialized.json()['task_id']}"
    ).json()
    assert materialize_task["cost_available"] is True
    assert materialize_task["estimated_cost"] == 0
    assert "本地操作" in materialize_task["cost_disclaimer"]
    background = next(
        item for item in client.get("/api/samples", params={
            "keyword_id": "keyword_b", "target_kind": "background",
        }).json()["items"]
        if item["id"] == background_id
    )
    assert background["source_kind"] == "noise"
    assert background["variant_kind"] == "clean"

    reject_background_augment = client.post("/api/augment", json={
        "sample_ids": [background_id], "keyword_id": "keyword_b",
        "noise_ids": [noise_id],
    })
    assert reject_background_augment.status_code == 400
    assert "背景噪声样本" in reject_background_augment.json()["detail"]

    augmented = client.post("/api/augment", json={
        "sample_ids": [second_id], "keyword_id": "keyword_b",
        "noise_ids": [noise_id], "count": 1, "seed": 20261003,
    })
    assert augmented.status_code == 200
    augmented_task = _wait_for_task(client, augmented.json()["task_id"])
    assert augmented_task["status"] == "completed"
    assert augmented_task["type"] == "augment_batch"
    assert len(augmented_task["children"]) == 1
    augmented_sample = next(
        item for item in client.get("/api/samples", params={
            "keyword_id": "keyword_b", "sample_type": "augmented",
        }).json()["items"]
        if item["parent_id"] == second_id
    )
    assert augmented_sample["target_kind"] == "negative"
    assert augmented_sample["source_kind"] == "human"
    assert augmented_sample["variant_kind"] == "augmented"
    assert augmented_sample["quality_status"] == "pending"
    assert augmented_sample["origin_task_id"] == augmented_task["children"][0]["id"]
    assert augmented_sample["origin_job_id"] == augmented_task["id"]

    exported = client.post("/api/exports", json={
        "sample_ids": [second_id, background_id, augmented_sample["id"]],
        "keyword_id": "keyword_b", "name": "semantic-wekws",
        "format": "wekws", "include_audio": False,
    })
    assert exported.status_code == 200
    export_task = _wait_for_task(client, exported.json()["task_id"])
    assert export_task["status"] == "completed"
    archive = client.get(
        f"/api/exports/{export_task['result']['export_id']}/download"
    )
    with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
        names = set(bundle.namelist())
        records = [
            json.loads(line)
            for name in names
            if name.startswith("manifests/") and name.endswith(".jsonl")
            for line in bundle.read(name).decode().splitlines()
            if line
        ]
        by_id = {record["utt_id"]: record for record in records}
        assert by_id[second_id]["target"] == 0
        assert by_id[second_id]["target_kind"] == "negative"
        assert by_id[augmented_sample["id"]]["source"] == "human_recording_augmented"
        assert by_id[background_id]["source"] == "noise"
        assert by_id[background_id]["source_kind"] == "noise"
        text_lines = "".join(
            bundle.read(name).decode() for name in names if name.endswith("/text")
        )
        target_lines = "".join(
            bundle.read(name).decode() for name in names if name.endswith("/target")
        )
        assert f"{second_id} <FILLER>\n" in text_lines
        assert f"{background_id} <FILLER>\n" in text_lines
        assert f"{second_id} 0\n" in target_lines
        assert f"{background_id} 0\n" in target_lines
        metadata = json.loads(bundle.read("dataset.json"))
        assert metadata["keyword_id"] == "keyword_b"
        assert metadata["label"] == "唤醒乙"
        assert metadata["wake_word"] == "唤醒乙"
        assert metadata["display_text_unavailable"] is True
        assert metadata["targets"] == {
            "positive": 0, "negative": 2, "background": 1,
        }
        assert metadata["wekws_text_semantics"] == {
            "positive": "<KEYWORD_B>",
            "negative": "<FILLER>",
            "background": "<FILLER>",
            "class_ids": {"<FILLER>": -1, "<KEYWORD_B>": 0},
        }
        assert bundle.read("wekws/dict/dict.txt").decode() == (
            "<FILLER> -1\n<KEYWORD_B> 0\n"
        )
        assert all(
            len(line.split()) == 2
            for line in text_lines.splitlines()
            if line
        )

    summaries = client.get("/api/samples/keywords").json()
    keyword_b = next(item for item in summaries if item["keyword_id"] == "keyword_b")
    assert keyword_b["label"] == "唤醒乙"
    assert keyword_b["display_text"] == "唤醒乙"
    assert keyword_b["display_text_unavailable"] is True
    assert keyword_b["negative_count"] == 2
    assert keyword_b["background_count"] == 1


def test_materialize_unexpected_error_finishes_task(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploaded = client.post(
        "/api/noises/upload",
        files={"file": ("failure-noise.wav", _wav_bytes(0.7, 83.0), "audio/wav")},
        data={"name": "异常收敛测试噪声", "category": "test"},
    )
    assert uploaded.status_code == 200
    noise_id = uploaded.json()["id"]
    original_insert = app_module._insert_sample

    def fail_noise_insert(sample: dict) -> None:
        if sample.get("provider") == "noise":
            raise RuntimeError("test-only-sensitive-unexpected-detail")
        original_insert(sample)

    monkeypatch.setattr(app_module, "_insert_sample", fail_noise_insert)
    response = client.post("/api/noises/materialize", json={
        "noise_ids": [noise_id], "keyword_id": "materialize_failure_keyword",
    })
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail["message"] == "噪声物化失败，请查看任务详情和服务器日志"
    task_id = detail["task_id"]
    task = client.get(f"/api/tasks/{task_id}").json()
    assert task["status"] == "failed"
    assert task["failed"] == 1
    assert task["started_at"] and task["finished_at"]
    assert task["result"]["internal_error"] == "RuntimeError"
    assert "test-only-sensitive-unexpected-detail" not in json.dumps(
        task, ensure_ascii=False
    )
    assert client.get("/api/samples", params={
        "keyword_id": "materialize_failure_keyword",
    }).json()["total"] == 0


def test_doubao_versions_have_independent_credentials_and_preflight(client):
    client.delete("/api/settings/providers/volcengine")
    client.delete("/api/settings/providers/volcengine_v2")
    assert client.put("/api/settings/providers/volcengine", json={"app_id":"fixture-app", "api_key":"fixture-token"}).status_code == 200
    v1 = client.get("/api/voices", params={"provider":"volcengine_v1"}).json()[0]
    v2 = client.get("/api/voices", params={"provider":"volcengine_v2"}).json()[0]
    before = app_module.db.one("SELECT COUNT(*) AS n FROM tasks")["n"]
    assert client.post("/api/generation/preflight", json={"voice_ids":[v1["id"]]}).status_code == 200
    missing = client.post("/api/tasks/generate-batch", json={"texts":["测试"],"voice_ids":[v2["id"]],"cost_acknowledged":True})
    assert missing.status_code == 400 and "豆包 2.0" in missing.json()["detail"]
    assert app_module.db.one("SELECT COUNT(*) AS n FROM tasks")["n"] == before
    assert client.put("/api/settings/providers/volcengine_v2", json={"api_key":"fixture-v2-key"}).status_code == 200
    assert client.post("/api/generation/preflight", json={"voice_ids":[v2["id"]]}).status_code == 200
    client.delete("/api/settings/providers/volcengine")
    assert client.post("/api/generation/preflight", json={"voice_ids":[v2["id"]]}).status_code == 200
    assert client.post("/api/generation/preflight", json={"voice_ids":[v1["id"]]}).status_code == 400
    client.delete("/api/settings/providers/volcengine_v2")


def test_scoped_catalog_refresh_does_not_disable_other_doubao_version(client):
    rows = client.get("/api/voices", params={"provider":"volcengine_v1"}).json()
    assert rows and all(row["model"] != "seed-tts-2.0" for row in rows)
    result = client.post("/api/voices/refresh", json={"provider":"volcengine_v2"})
    assert result.status_code == 200 and result.json()["count"] >= 2
    after = client.get("/api/voices", params={"provider":"volcengine_v1"}).json()
    assert {row["id"] for row in rows} == {row["id"] for row in after}


def test_sample_pages_and_bulk_selection_follow_doubao_service_boundary(client, tmp_path):
    ids = ["owned-service-v1-filter", "owned-service-v2-filter", "owned-service-mimo-filter"]
    try:
        for identifier, provider, model in zip(ids, ["volcengine", "volcengine", "xiaomi"], ["seed-tts-1.0", "seed-tts-2.0", "mimo-v2.5-tts"]):
            app_module.db.execute("INSERT INTO samples(id,provider,voice_id,model,text,file_path,sha256,created_at) VALUES(?,?,?,?,?,?,?,?)",
                                  (identifier,provider,"owned-service-filter-voice",model,"filter fixture",str(tmp_path/(identifier+".wav")),identifier,"2026-10-13"))
        for provider, expected in zip(["volcengine_v1", "volcengine_v2", "xiaomi"], ids):
            params = {"provider":provider,"voice_id":"owned-service-filter-voice"}
            rows = client.get("/api/samples",params=params).json()
            selected = client.get("/api/samples/ids",params=params).json()
            assert rows["total"] == selected["total"] == 1
            assert rows["items"][0]["id"] == selected["ids"][0] == expected
    finally:
        for identifier in ids:
            app_module.db.execute("DELETE FROM samples WHERE id=?", (identifier,))
