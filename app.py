"""LAN-accessible web application for building a synthetic KWS dataset."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import shutil
import tempfile
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator


logger = logging.getLogger(__name__)

try:
    from .audio_utils import (
        AudioError, audio_metrics,
        mix_noise,
        normalize_wav,
        probe_wav,
        sha256_file,
        spectrum_points,
        waveform_points,
    )
    from .db import Database, utc_now
    from .providers import (
        ProviderError,
        ProviderConfigurationError,
        available_providers,
        get_provider,
        validate_provider_configuration,
    )
    from .project_settings import ProjectSettingsStore, normalize_display_text
    from .settings_store import ProviderSettingsStore, set_default_settings_store
    from .voice_catalog import builtin_voices, refresh_voice_catalog
except ImportError:  # Support direct `uvicorn app:app` execution.
    from audio_utils import (  # type: ignore
        AudioError, audio_metrics, mix_noise, normalize_wav, probe_wav, sha256_file,
        spectrum_points, waveform_points,
    )
    from db import Database, utc_now  # type: ignore
    from providers import (  # type: ignore
        ProviderError, ProviderConfigurationError, available_providers, get_provider,
        validate_provider_configuration,
    )
    from project_settings import ProjectSettingsStore, normalize_display_text  # type: ignore
    from settings_store import ProviderSettingsStore, set_default_settings_store  # type: ignore
    from voice_catalog import builtin_voices, refresh_voice_catalog  # type: ignore


APP_DIR = Path(__file__).resolve().parent
# Keep runtime data outside source-controlled files in standalone deployments.
DEFAULT_DATA_ROOT = APP_DIR / "var"
DATA_ROOT = Path(os.getenv("TTS_APP_DATA_DIR", DEFAULT_DATA_ROOT)).resolve()
# Manifest-only exports may be consumed by training jobs on the Docker host.
# This logical root can therefore differ from the container's /data mount.
MANIFEST_DATA_ROOT = Path(
    os.getenv("TTS_MANIFEST_DATA_ROOT", str(DATA_ROOT))
).expanduser().resolve()
MAX_UPLOAD_BYTES = max(1, int(os.getenv("TTS_MAX_UPLOAD_MB", "512"))) * 1024 * 1024
MAX_TASK_ITEMS = max(1, int(os.getenv("TTS_MAX_TASK_ITEMS", "5000")))
REAL_SAMPLE_DURATION_RANGE = (0.2, 30.0)
NOISE_DURATION_RANGE = (0.2, 3600.0)
STATIC_DIR = APP_DIR / "static"
SAMPLES_DIR = DATA_ROOT / "samples"
NOISES_DIR = DATA_ROOT / "noises"
EXPORTS_DIR = DATA_ROOT / "exports"
TRASH_DIR = DATA_ROOT / "trash"
TMP_DIR = DATA_ROOT / "tmp"
for directory in (SAMPLES_DIR, NOISES_DIR, EXPORTS_DIR, TRASH_DIR, TMP_DIR):
    directory.mkdir(parents=True, exist_ok=True)
export_file_lock = threading.Lock()

def _sync_builtin_voice_catalog(database: Database) -> int:
    """Seed maintained voices and softly disable removed built-in entries."""

    active_by_provider: dict[str, set[tuple[str, str]]] = {}
    catalog = builtin_voices()
    for voice in catalog:
        value = asdict(voice) if is_dataclass(voice) else dict(voice)
        database.upsert_voice(value)
        active_by_provider.setdefault(str(value["provider"]), set()).add(
            (str(value["voice_id"]), str(value.get("model") or ""))
        )
    existing_providers = {
        str(row["provider"])
        for row in database.all("SELECT DISTINCT provider FROM voices WHERE source='builtin'")
    }
    for provider in existing_providers | set(active_by_provider):
        database.disable_missing_voices(
            provider, "builtin", active_by_provider.get(provider, set())
        )
    return len(catalog)


db = Database(DATA_ROOT / "tts_dataset.sqlite3")
_sync_builtin_voice_catalog(db)
provider_settings = ProviderSettingsStore(DATA_ROOT / "provider_settings.json")
project_settings = ProjectSettingsStore(DATA_ROOT / "project_settings.json")
set_default_settings_store(provider_settings)
# In-process workers cannot survive a service restart. Make interrupted work
# explicit instead of leaving tasks permanently displayed as running.
db.execute(
    """
    UPDATE tasks
    SET status='failed', message='服务重启，任务已中断，请重新创建', updated_at=?
    WHERE status IN ('queued', 'running')
    """,
    (utc_now(),),
)
executor = ThreadPoolExecutor(max_workers=max(1, int(os.getenv("TTS_APP_WORKERS", "2"))))

app = FastAPI(
    title="海思语音数据集工坊",
    version="0.1.0",
    description="Generate, review, augment, and export synthetic wake-word audio.",
)


@app.middleware("http")
async def optional_api_token(request: Request, call_next):
    token = os.getenv("TTS_APP_API_TOKEN", "").strip()
    if token and request.url.path.startswith("/api/"):
        supplied = request.headers.get("x-app-token", "")
        authorization = request.headers.get("authorization", "")
        if supplied != token and authorization != f"Bearer {token}":
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "Invalid application token"}, status_code=401)
    return await call_next(request)


class GenerateRequest(BaseModel):
    text: str = "你好，芯宝"
    label: str = "你好芯宝"
    keyword_id: str = "ni_hao_xin_bao"
    voice_ids: list[int]
    count: int = Field(1, ge=1, le=1000)
    speed: float = Field(1.0, ge=0.5, le=2.0)
    pitch: float = Field(1.0, ge=0.5, le=2.0)
    volume: int = Field(50, ge=0, le=100)
    seed: int = Field(20260804, ge=0, le=2**31 - 1)
    instruction: str = ""

    @field_validator("text", "label", "keyword_id")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value cannot be blank")
        if len(value) > 10_000:
            raise ValueError("value is too long")
        return value.strip()


class AugmentRequest(BaseModel):
    sample_ids: list[str] = []
    noise_ids: list[str] = []
    count: int = Field(1, ge=1, le=100)
    seed: int = Field(20260804, ge=0, le=2**31 - 1)


class DeleteSamplesRequest(BaseModel):
    ids: list[str]


class ExportRequest(BaseModel):
    sample_ids: list[str] = []
    name: str = "ni_hao_xin_bao_dataset"
    format: str = Field("generic", pattern="^(generic|jsonl|folders|wekws)$")
    include_audio: bool = True
    train_ratio: float = Field(0.8, gt=0, lt=1)
    dev_ratio: float = Field(0.1, ge=0, lt=1)
    seed: int = 20260804


class ProjectSettingsRequest(BaseModel):
    display_text: str

    @field_validator("display_text")
    @classmethod
    def validate_display_text(cls, value: str) -> str:
        return normalize_display_text(value)


class AliyunProviderSettingsRequest(BaseModel):
    api_key: str | None = None
    workspace_id: str | None = None
    region: str | None = None
    endpoint: str | None = None
    customization_endpoint: str | None = None


class VolcengineProviderSettingsRequest(BaseModel):
    app_id: str | None = None
    access_token: str | None = None
    cluster: str | None = None
    endpoint: str | None = None
    access_key_id: str | None = None
    access_key_secret: str | None = None
    catalog_endpoint: str | None = None


def _json_row(row: dict[str, Any]) -> dict[str, Any]:
    row = db.decode_json_fields(row)
    row.pop("file_path", None)
    for raw_json_key in ("metadata_json", "payload_json", "result_json"):
        row.pop(raw_json_key, None)
    for key in ("enabled",):
        if key in row:
            row[key] = bool(row[key])
    return row


def _safe_file(path_text: str, base: Path = DATA_ROOT) -> Path:
    path = Path(path_text).resolve()
    if path != base and base not in path.parents:
        raise HTTPException(400, "Invalid data path")
    return path


def _safe_component(value: Any, fallback: str = "item") -> str:
    component = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in str(value)
    ).strip("_")[:100]
    return component or fallback


def _validate_duration(info: dict[str, Any], minimum: float, maximum: float, kind: str) -> None:
    duration = float(info.get("duration", 0.0))
    if duration < minimum or duration > maximum:
        raise AudioError(f"{kind}时长必须在 {minimum:g} 到 {maximum:g} 秒之间，当前为 {duration:.3f} 秒")


def _create_task(task_type: str, total: int, payload: dict[str, Any]) -> str:
    task_id = uuid.uuid4().hex
    now = utc_now()
    db.execute(
        "INSERT INTO tasks(id,type,status,total,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (task_id, task_type, "queued", total, json.dumps(payload, ensure_ascii=False), now, now),
    )
    return task_id


def _update_task(task_id: str, **fields: Any) -> None:
    allowed = {"status", "completed", "failed", "message", "result_json"}
    updates, values = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        updates.append(f"{key}=?")
        values.append(json.dumps(value, ensure_ascii=False) if key == "result_json" else value)
    updates.append("updated_at=?")
    values.extend((utc_now(), task_id))
    db.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id=?", tuple(values))


def _submit(task_id: str, function, *args) -> None:
    def runner() -> None:
        _update_task(task_id, status="running")
        try:
            function(task_id, *args)
        except Exception as exc:
            _update_task(task_id, status="failed", message=str(exc)[:1000])
    executor.submit(runner)


def _insert_sample(sample: dict[str, Any]) -> None:
    db.execute(
        """
        INSERT INTO samples(
            id,provider,voice_id,voice_name,model,text,label,keyword_id,speed,pitch,volume,seed,
            file_path,duration,sample_rate,channels,sha256,parent_id,noise_id,snr_db,split,
            quality_status,notes,metadata_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            sample["id"], sample["provider"], sample["voice_id"], sample.get("voice_name", ""),
            sample.get("model", ""), sample["text"], sample.get("label", "你好芯宝"),
            sample.get("keyword_id", "ni_hao_xin_bao"), sample.get("speed", 1.0),
            sample.get("pitch", 1.0), sample.get("volume", 50), sample.get("seed", 0),
            sample["file_path"], sample["duration"], sample["sample_rate"], sample.get("channels", 1),
            sample["sha256"], sample.get("parent_id"), sample.get("noise_id"), sample.get("snr_db"),
            sample.get("split", "unassigned"), sample.get("quality_status", "pass"),
            sample.get("notes", ""), json.dumps(sample.get("metadata", {}), ensure_ascii=False), utc_now(),
        ),
    )


def _generate_task(task_id: str, request: dict[str, Any], voices: list[dict[str, Any]]) -> None:
    completed = failed = duplicates = 0
    errors: list[str] = []

    def remember_error(label: str, exc: Exception) -> str:
        if isinstance(exc, (ProviderError, AudioError)):
            message = str(exc)[:500]
        else:
            logger.error(
                "Unexpected TTS generation failure: task_id=%s voice=%s exception_type=%s",
                task_id,
                label,
                type(exc).__name__,
            )
            message = f"内部处理失败（{type(exc).__name__}），请查看服务器日志"
        detail = f"{label}: {message}"
        if detail not in errors and len(errors) < 10:
            errors.append(detail)
        return detail

    for voice in voices:
        provider_name = voice["provider"]
        try:
            provider = get_provider(provider_name, provider_settings)
        except Exception as exc:
            failed += request["count"]
            detail = remember_error(provider_name, exc)
            _update_task(task_id, failed=failed, message=detail)
            continue
        for index in range(request["count"]):
            item_seed = request["seed"] + index
            sample_id = uuid.uuid4().hex
            raw_path = TMP_DIR / f"{sample_id}.audio"
            wav_path = (
                SAMPLES_DIR
                / _safe_component(provider_name, "provider")
                / _safe_component(voice["voice_id"], "voice")
                / f"{sample_id}.wav"
            )
            try:
                existing = db.one(
                    """
                    SELECT id FROM samples
                    WHERE deleted_at IS NULL AND provider=? AND voice_id=? AND model=?
                      AND text=? AND label=? AND keyword_id=?
                      AND speed=? AND pitch=? AND volume=? AND seed=?
                      AND COALESCE(json_extract(metadata_json, '$.instruction'), '')=?
                    """,
                    (
                        provider_name, voice["voice_id"], voice["model"], request["text"],
                        request["label"], request["keyword_id"],
                        request["speed"], request["pitch"], request["volume"], item_seed,
                        request.get("instruction", ""),
                    ),
                )
                if existing:
                    duplicates += 1
                    completed += 1
                    _update_task(
                        task_id, completed=completed, failed=failed,
                        message=f"已复用 {duplicates} 个已有样本",
                    )
                    continue
                audio = provider.synthesize(
                    text=request["text"], voice=voice["voice_id"], model=voice["model"],
                    speed=request["speed"], pitch=request["pitch"], volume=request["volume"],
                    seed=item_seed, instruction=request.get("instruction", ""),
                )
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                raw_path.write_bytes(audio)
                info = normalize_wav(raw_path, wav_path, 16000)
                digest = sha256_file(wav_path)
                if db.one("SELECT id FROM samples WHERE sha256=? AND deleted_at IS NULL", (digest,)):
                    duplicates += 1
                    wav_path.unlink(missing_ok=True)
                    completed += 1
                    _update_task(task_id, completed=completed, failed=failed,
                                 message=f"已跳过 {duplicates} 个重复音频")
                    continue
                quality = "pass" if 0.25 <= info["duration"] <= 10.0 else "review"
                _insert_sample({
                    "id": sample_id, "provider": provider_name, "voice_id": voice["voice_id"],
                    "voice_name": voice["name"], "model": voice["model"], "text": request["text"],
                    "label": request["label"], "keyword_id": request["keyword_id"],
                    "speed": request["speed"], "pitch": request["pitch"], "volume": request["volume"],
                    "seed": item_seed, "file_path": str(wav_path), "duration": info["duration"],
                    "sample_rate": info["sample_rate"], "channels": info["channels"], "sha256": digest,
                    "quality_status": quality,
                    "metadata": {
                        "voice_db_id": voice["id"], "instruction": request.get("instruction", ""),
                        "gender": voice.get("gender", "unknown"),
                        "age_group": voice.get("age_group", "unknown"),
                    },
                })
                completed += 1
            except Exception as exc:
                failed += 1
                detail = remember_error(voice["name"], exc)
                _update_task(task_id, message=detail)
            finally:
                raw_path.unlink(missing_ok=True)
            _update_task(task_id, completed=completed, failed=failed)
    status = "completed" if failed == 0 else ("partial" if completed else "failed")
    summary = f"完成 {completed}，失败 {failed}，重复 {duplicates}"
    if errors:
        summary += f"；首个错误：{errors[0]}"
    _update_task(task_id, status=status, completed=completed, failed=failed,
                 message=summary[:1000], result_json={"errors": errors})


def _choose_noises(noise_ids: list[str]) -> list[dict[str, Any]]:
    if noise_ids:
        placeholders = ",".join("?" for _ in noise_ids)
        return db.all(
            f"SELECT * FROM noises WHERE enabled=1 AND deleted_at IS NULL AND id IN ({placeholders})",
            tuple(noise_ids),
        )
    return db.all("SELECT * FROM noises WHERE enabled=1 AND deleted_at IS NULL ORDER BY created_at")


def _augment_task(task_id: str, request: dict[str, Any], samples: list[dict[str, Any]]) -> None:
    noises = sorted(_choose_noises(request["noise_ids"]), key=lambda item: item["id"])
    samples = sorted(samples, key=lambda item: item["id"])
    if not noises:
        raise RuntimeError("没有可用背景噪声，请先上传并启用噪声")
    weights = [max(0.001, float(item["weight"])) for item in noises]
    rng = random.Random(request["seed"])
    completed = failed = duplicates = 0
    for sample_index, sample in enumerate(samples):
        source_metadata = db.decode_json_fields(dict(sample)).get("metadata", {})
        for copy_index in range(request["count"]):
            noise = rng.choices(noises, weights=weights, k=1)[0]
            snr = rng.uniform(float(noise["min_snr"]), float(noise["max_snr"]))
            item_seed = request["seed"] + sample_index * request["count"] + copy_index
            new_id = uuid.uuid4().hex
            target = SAMPLES_DIR / "augmented" / f"{new_id}.wav"
            try:
                info = mix_noise(sample["file_path"], noise["file_path"], target, snr, item_seed)
                digest = sha256_file(target)
                if db.one("SELECT id FROM samples WHERE sha256=? AND deleted_at IS NULL", (digest,)):
                    target.unlink(missing_ok=True)
                    duplicates += 1
                    completed += 1
                    _update_task(
                        task_id, completed=completed, failed=failed,
                        message=f"已复用 {duplicates} 个已有增强样本",
                    )
                    continue
                _insert_sample({
                    **sample, "id": new_id, "file_path": str(target), "sha256": digest,
                    "duration": info["duration"], "sample_rate": info["sample_rate"],
                    "parent_id": sample.get("parent_id") or sample["id"], "noise_id": noise["id"],
                    "snr_db": snr, "seed": item_seed,
                    "metadata": {
                        **source_metadata,
                        "augmentation": "additive_noise", "noise_name": noise["name"],
                        "noise_offset": int(info.get("noise_offset", 0)),
                    },
                })
                completed += 1
            except Exception as exc:
                failed += 1
                _update_task(task_id, message=str(exc)[:500])
            _update_task(task_id, completed=completed, failed=failed)
    status = "completed" if failed == 0 else ("partial" if completed else "failed")
    _update_task(task_id, status=status, completed=completed, failed=failed,
                 message=f"增强完成 {completed}，失败 {failed}，重复 {duplicates}")


def _speaker_for(sample: dict[str, Any]) -> str:
    if sample.get("provider") == "real":
        metadata = db.decode_json_fields(dict(sample)).get("metadata", {})
        return str(metadata.get("speaker_id") or sample.get("voice_id") or sample["id"])
    return "tts_" + "_".join((
        str(sample.get("provider", "unknown")),
        str(sample.get("voice_id", "unknown")),
        str(sample.get("model", "")),
    )).rstrip("_")


def _explicit_split_map(samples: list[dict[str, Any]]) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for sample in samples:
        if sample.get("provider") != "real" or sample.get("split") not in {"train", "dev", "test"}:
            continue
        speaker = _speaker_for(sample)
        split = str(sample["split"])
        previous = assignments.get(speaker)
        if previous and previous != split:
            raise HTTPException(
                400,
                f"真人说话人 {speaker} 同时被显式分配到 {previous} 和 {split}，请先修正数据划分",
            )
        assignments[speaker] = split
    return assignments


def _split_for(
    sample: dict[str, Any], request: dict[str, Any], explicit_splits: dict[str, str] | None = None,
) -> str:
    import hashlib
    # A TTS voice is a synthetic speaker, so keep all of its clean and
    # augmented samples in one split. Real recordings use their speaker ID.
    group = _speaker_for(sample)
    if sample.get("provider") == "real":
        assigned = (explicit_splits or {}).get(group)
        if assigned:
            return assigned
        if sample.get("split") in {"train", "dev", "test"}:
            return str(sample["split"])
    digest = hashlib.sha256(f"{request['seed']}:{group}".encode()).digest()
    value = int.from_bytes(digest[:8], "big") / 2**64
    if value < request["train_ratio"]:
        return "train"
    if value < request["train_ratio"] + request["dev_ratio"]:
        return "dev"
    return "test"


def _export_task(task_id: str, request: dict[str, Any], samples: list[dict[str, Any]]) -> None:
    export_id = uuid.uuid4().hex
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in request["name"])[:80]
    archive_path = EXPORTS_DIR / f"{safe_name}_{export_id[:8]}.zip"
    manifests: dict[str, list[dict[str, Any]]] = {"train": [], "dev": [], "test": []}
    checksums: list[str] = []
    explicit_splits = _explicit_split_map(samples)
    audio_path_type = "archive_relative" if request["include_audio"] else "server_absolute"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, sample in enumerate(samples):
            split = _split_for(sample, request, explicit_splits)
            source_path = _safe_file(sample["file_path"])
            if not request["include_audio"]:
                audio_name = str(MANIFEST_DATA_ROOT / source_path.relative_to(DATA_ROOT))
            elif request["format"] == "folders":
                audio_name = (
                    f"folders/{split}/{_safe_component(sample['keyword_id'], 'keyword')}/"
                    f"{sample['id']}.wav"
                )
            else:
                audio_name = f"audio/{split}/{sample['id']}.wav"
            sample_metadata = db.decode_json_fields(dict(sample)).get("metadata", {})
            record = {
                "utt_id": sample["id"], "audio": audio_name, "label": sample["label"],
                "audio_path_type": audio_path_type,
                "keyword_id": sample["keyword_id"], "text": sample["text"],
                "speaker_id": _speaker_for(sample),
                "split": split,
                "source": (
                    ("human_recording_augmented" if sample.get("parent_id") else "human_recording")
                    if sample["provider"] == "real"
                    else "tts" if not sample.get("parent_id") else "tts_augmented"
                ),
                "provider": sample["provider"], "voice_id": sample["voice_id"],
                "model": sample["model"], "speed": sample["speed"], "pitch": sample["pitch"],
                "seed": sample["seed"], "sha256": sample["sha256"],
                "parent_id": sample.get("parent_id"), "noise_id": sample.get("noise_id"),
                "snr_db": sample.get("snr_db"),
                "noise_offset": sample_metadata.get("noise_offset"),
            }
            manifests[split].append(record)
            if request["include_audio"]:
                archive.write(source_path, audio_name)
                checksums.append(f"{sample['sha256']}  {audio_name}")
            _update_task(task_id, completed=index + 1)

        for split, records in manifests.items():
            jsonl = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
            archive.writestr(f"manifests/{split}.jsonl", jsonl)
            if request["format"] == "wekws":
                archive.writestr(
                    f"wekws/{split}/text",
                    "".join(f"{record['utt_id']} {record['label']}\n" for record in records),
                )
                archive.writestr(
                    f"wekws/{split}/wav.scp",
                    "".join(f"{record['utt_id']} {record['audio']}\n" for record in records),
                )
        project = request.get("project") or project_settings.get()
        archive.writestr("dataset.json", json.dumps({
            "wake_word": project["display_text"], "label": project["label"],
            "keyword_id": project["keyword_id"],
            "format": request["format"], "sample_count": len(samples), "created_at": utc_now(),
            "include_audio": request["include_audio"], "audio_path_type": audio_path_type,
            "manifest_data_root": str(MANIFEST_DATA_ROOT) if not request["include_audio"] else None,
            "split_config": {
                "train_ratio": request["train_ratio"],
                "dev_ratio": request["dev_ratio"],
                "test_ratio": 1.0 - request["train_ratio"] - request["dev_ratio"],
                "seed": request["seed"],
                "real_explicit_split_precedence": True,
            },
            "splits": {key: len(value) for key, value in manifests.items()},
        }, ensure_ascii=False, indent=2))
        if checksums:
            archive.writestr("SHA256SUMS", "\n".join(checksums) + "\n")
    db.execute("INSERT INTO exports(id,name,file_path,sample_count,format,created_at) VALUES(?,?,?,?,?,?)",
               (export_id, safe_name, str(archive_path), len(samples), request["format"], utc_now()))
    _update_task(task_id, status="completed", completed=len(samples),
                 message=f"已导出 {len(samples)} 条样本", result_json={"export_id": export_id})


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.get("/api/status")
def status() -> dict[str, Any]:
    sample_count = db.one("SELECT COUNT(*) AS n FROM samples WHERE deleted_at IS NULL")["n"]
    augmented_count = db.one(
        "SELECT COUNT(*) AS n FROM samples WHERE deleted_at IS NULL AND parent_id IS NOT NULL"
    )["n"]
    duration = db.one(
        "SELECT COALESCE(SUM(duration), 0) AS seconds FROM samples WHERE deleted_at IS NULL"
    )["seconds"]
    running_tasks = db.one(
        "SELECT COUNT(*) AS n FROM tasks WHERE status IN ('queued','running')"
    )["n"]
    voice_count = db.one("SELECT COUNT(*) AS n FROM voices WHERE enabled=1")["n"]
    noise_count = db.one(
        "SELECT COUNT(*) AS n FROM noises WHERE enabled=1 AND deleted_at IS NULL"
    )["n"]
    disk = shutil.disk_usage(DATA_ROOT)
    project = project_settings.get()
    return {
        "ok": True, "app": "海思语音数据集工坊", "version": app.version,
        "wake_word": project["display_text"], "keyword_id": project["keyword_id"],
        "project": project,
        "samples": sample_count, "augmented_samples": augmented_count,
        "sample_duration_seconds": round(float(duration), 3),
        "running_tasks": running_tasks, "voices": voice_count, "noises": noise_count,
        "disk_free_gb": round(disk.free / 1024**3, 1),
    }


@app.get("/api/settings/project")
def get_project_settings() -> dict[str, str]:
    return project_settings.get()


@app.put("/api/settings/project")
def put_project_settings(request: ProjectSettingsRequest) -> dict[str, str]:
    return project_settings.update(request.display_text)


@app.get("/api/providers")
def providers() -> list[dict[str, Any]]:
    result = []
    for item in available_providers(provider_settings):
        result.append(asdict(item) if is_dataclass(item) else dict(item) if isinstance(item, dict) else {"id": str(item)})
    return result


def _provider_settings_model(
    provider: str, payload: dict[str, Any]
) -> AliyunProviderSettingsRequest | VolcengineProviderSettingsRequest:
    if provider == "aliyun":
        return AliyunProviderSettingsRequest.model_validate(payload)
    if provider == "volcengine":
        return VolcengineProviderSettingsRequest.model_validate(payload)
    raise HTTPException(404, "不支持的供应商")


@app.get("/api/settings/providers")
def get_provider_settings() -> dict[str, Any]:
    """Return provider settings with secrets replaced by masked status objects."""

    return {"providers": provider_settings.public_all()}


@app.put("/api/settings/providers/{provider}")
def put_provider_settings(
    provider: str, payload: dict[str, Any] = Body(default_factory=dict)
) -> dict[str, Any]:
    model = _provider_settings_model(provider, payload)
    values = model.model_dump(exclude_unset=True)
    current = provider_settings.get_provider(provider)
    candidate = dict(current)
    for key, raw_value in values.items():
        value = str(raw_value).strip() if raw_value is not None else ""
        if not value and key in {"api_key", "access_token", "access_key_secret"}:
            continue
        if value:
            candidate[key] = value
        else:
            candidate.pop(key, None)
    try:
        validate_provider_configuration(provider, candidate, require_credentials=False)
        provider_settings.update_provider(provider, values)
    except (ProviderError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return provider_settings.public_provider(provider)


@app.delete("/api/settings/providers/{provider}")
def delete_provider_settings(provider: str) -> dict[str, Any]:
    if provider not in {"aliyun", "volcengine"}:
        raise HTTPException(404, "不支持的供应商")
    provider_settings.clear_provider(provider)
    disabled = 0
    dynamic_sources = db.all(
        "SELECT DISTINCT source FROM voices WHERE provider=? AND source!='builtin'",
        (provider,),
    )
    for row in dynamic_sources:
        disabled += db.disable_missing_voices(provider, str(row["source"]), set())
    result = provider_settings.public_provider(provider)
    result["disabled_dynamic_voices"] = disabled
    return result


@app.post("/api/settings/providers/{provider}/test")
def test_provider_settings(provider: str) -> dict[str, Any]:
    if provider not in {"aliyun", "volcengine"}:
        raise HTTPException(404, "不支持的供应商")
    config = provider_settings.get_provider(provider)
    try:
        validate_provider_configuration(provider, config, require_credentials=True)
    except ProviderError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "ok": True,
        "provider": provider,
        "message": "配置格式和必填项验证通过；未向云端发送请求，不会产生合成费用",
        "remote_verified": False,
        "capabilities": provider_settings.public_provider(provider),
    }


@app.get("/api/voices")
def voices(
    provider: str = "", gender: str = "", age_group: str = "", enabled: bool = True,
) -> list[dict[str, Any]]:
    clauses, params = [], []
    if enabled:
        clauses.append("enabled=1")
    if provider:
        clauses.append("provider=?"); params.append(provider)
    if gender:
        clauses.append("gender=?"); params.append(gender)
    if age_group:
        clauses.append("age_group=?"); params.append(age_group)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return [_json_row(row) for row in db.all(f"SELECT * FROM voices{where} ORDER BY provider,name", tuple(params))]


@app.post("/api/voices/refresh")
def refresh_voices(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    provider = payload.get("provider") or None
    refresh_state: dict[str, Any] = {}
    try:
        catalog = refresh_voice_catalog(
            provider=provider,
            settings_store=provider_settings,
            refresh_state=refresh_state,
        )
    except ProviderConfigurationError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(502, str(exc)) from exc
    added = []
    values: list[dict[str, Any]] = []
    for voice in catalog:
        value = asdict(voice) if is_dataclass(voice) else dict(voice)
        values.append(value)
        voice_id = db.upsert_voice(value)
        added.append(voice_id)
    disabled_stale = 0
    for snapshot in refresh_state.get("complete_snapshots", []):
        snapshot_provider = str(snapshot.get("provider") or "")
        snapshot_source = str(snapshot.get("source") or "")
        active_keys = {
            (str(value.get("voice_id") or ""), str(value.get("model") or ""))
            for value in values
            if value.get("provider") == snapshot_provider
            and value.get("source", "builtin") == snapshot_source
        }
        disabled_stale += db.disable_missing_voices(
            snapshot_provider, snapshot_source, active_keys
        )
    return {
        "count": len(added),
        "voice_ids": added,
        "disabled_stale": disabled_stale,
        "warnings": refresh_state.get("warnings", []),
    }


@app.post("/api/tasks/generate")
def create_generate_task(request: GenerateRequest) -> dict[str, str]:
    placeholders = ",".join("?" for _ in request.voice_ids)
    selected = db.all(
        f"SELECT * FROM voices WHERE enabled=1 AND id IN ({placeholders})", tuple(request.voice_ids)
    ) if request.voice_ids else []
    if not selected:
        raise HTTPException(400, "请选择至少一个可用音色")
    if any(voice["provider"] == "volcengine" for voice in selected) and abs(
        request.pitch - 1.0
    ) > 1e-9:
        raise HTTPException(400, "火山引擎 V1 暂不支持音高调节，请将音高设为 0 st")
    if len(selected) * request.count > MAX_TASK_ITEMS:
        raise HTTPException(400, f"单个任务最多 {MAX_TASK_ITEMS} 个合成项，请拆分任务")
    payload = request.model_dump()
    project = project_settings.get()
    for field, project_field in (
        ("text", "display_text"),
        ("label", "label"),
        ("keyword_id", "keyword_id"),
    ):
        if field not in request.model_fields_set:
            payload[field] = project[project_field]
    task_id = _create_task("generate", len(selected) * request.count, payload)
    _submit(task_id, _generate_task, payload, selected)
    return {"id": task_id, "task_id": task_id}


@app.get("/api/tasks")
def tasks(limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
    return [_json_row(row) for row in db.all("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,))]


@app.get("/api/tasks/{task_id}")
def task(task_id: str) -> dict[str, Any]:
    row = db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not row:
        raise HTTPException(404, "任务不存在")
    return _json_row(row)


@app.post("/api/tasks/{task_id}/retry")
def retry_task(task_id: str) -> dict[str, str]:
    previous = db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not previous:
        raise HTTPException(404, "任务不存在")
    if previous["status"] in {"queued", "running"}:
        raise HTTPException(409, "任务仍在运行，不能重复创建")
    payload = db.decode_json_fields(previous).get("payload", {})
    try:
        if previous["type"] == "generate":
            return create_generate_task(GenerateRequest(**payload))
        if previous["type"] == "augment":
            return augment(AugmentRequest(**payload))
        if previous["type"] == "export":
            return create_export(ExportRequest(**payload))
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"旧任务参数已不兼容：{exc}") from exc
    raise HTTPException(400, "该任务类型不支持重试")


@app.get("/api/samples")
def samples(
    provider: str = "", voice_id: str = "", quality: str = "", split: str = "",
    gender: str = "", age_group: str = "", sample_type: str = "",
    search: str = "", page: int = Query(1, ge=1), page_size: int = Query(24, ge=1, le=200),
) -> dict[str, Any]:
    clauses, params = ["deleted_at IS NULL"], []
    for column, value in (("provider", provider), ("voice_id", voice_id),
                          ("quality_status", quality), ("split", split)):
        if value:
            clauses.append(f"{column}=?"); params.append(value)
    if gender:
        clauses.append("json_extract(metadata_json, '$.gender')=?"); params.append(gender)
    if age_group:
        clauses.append("json_extract(metadata_json, '$.age_group')=?"); params.append(age_group)
    if sample_type == "real":
        clauses.append("provider='real'")
    elif sample_type == "augmented":
        clauses.append("parent_id IS NOT NULL")
    elif sample_type == "clean":
        clauses.append("provider!='real' AND parent_id IS NULL")
    if search:
        clauses.append("(text LIKE ? OR voice_name LIKE ? OR id LIKE ?)")
        params.extend((f"%{search}%", f"%{search}%", f"%{search}%"))
    where = " AND ".join(clauses)
    total = db.one(f"SELECT COUNT(*) AS n FROM samples WHERE {where}", tuple(params))["n"]
    params.extend((page_size, (page - 1) * page_size))
    rows = db.all(f"SELECT * FROM samples WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?", tuple(params))
    return {"items": [_json_row(row) for row in rows], "total": total, "page": page, "page_size": page_size}


@app.post("/api/samples/upload")
async def upload_samples(
    files: list[UploadFile] = File(...), text: str | None = Form(None),
    label: str | None = Form(None), speaker_id: str = Form(...),
    keyword_id: str | None = Form(None),
    split: str = Form("unassigned"), gender: str = Form("unknown"),
    age_group: str = Form("unknown"),
) -> dict[str, Any]:
    """Import real recordings while enforcing the deployment audio contract."""
    speaker_id = speaker_id.strip()
    if not speaker_id:
        raise HTTPException(400, "speaker_id 不能为空")
    project = project_settings.get()
    effective_text = project["display_text"] if text is None else text.strip()
    effective_label = project["label"] if label is None else label.strip()
    effective_keyword_id = (
        project["keyword_id"] if keyword_id is None else keyword_id.strip()
    )
    if not effective_text or not effective_label or not effective_keyword_id:
        raise HTTPException(400, "text、label 和 keyword_id 不能为空")
    if len(effective_text) > 10_000 or len(effective_label) > 10_000:
        raise HTTPException(400, "text 或 label 过长")
    if len(effective_keyword_id) > 100:
        raise HTTPException(400, "keyword_id 不能超过 100 个字符")
    if split == "unspecified":
        split = "unassigned"
    if split not in {"unassigned", "train", "dev", "test"}:
        raise HTTPException(400, "无效的数据划分")
    if len(files) > 500:
        raise HTTPException(400, "单次最多上传 500 个文件")

    imported: list[str] = []
    duplicates: list[str] = []
    failures: list[dict[str, str]] = []
    for upload in files:
        sample_id = uuid.uuid4().hex
        suffix = Path(upload.filename or "recording.audio").suffix[:10]
        temp = TMP_DIR / f"{sample_id}{suffix}"
        target = SAMPLES_DIR / "real" / _safe_component(speaker_id, "speaker") / f"{sample_id}.wav"
        try:
            uploaded = 0
            with temp.open("wb") as stream:
                while chunk := await upload.read(1024 * 1024):
                    uploaded += len(chunk)
                    if uploaded > MAX_UPLOAD_BYTES:
                        raise AudioError(
                            f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MiB 限制"
                        )
                    stream.write(chunk)
            info = await asyncio.to_thread(normalize_wav, temp, target, 16000)
            _validate_duration(info, *REAL_SAMPLE_DURATION_RANGE, "真人样本")
            digest = await asyncio.to_thread(sha256_file, target)
            if db.one("SELECT id FROM samples WHERE sha256=? AND deleted_at IS NULL", (digest,)):
                target.unlink(missing_ok=True)
                duplicates.append(upload.filename or sample_id)
                continue
            _insert_sample({
                "id": sample_id, "provider": "real", "voice_id": speaker_id,
                "voice_name": speaker_id, "model": "recording", "text": effective_text,
                "label": effective_label, "keyword_id": effective_keyword_id, "speed": 1.0,
                "pitch": 1.0, "volume": 50, "seed": 0, "file_path": str(target),
                "duration": info["duration"], "sample_rate": info["sample_rate"],
                "channels": info["channels"], "sha256": digest, "split": split,
                "quality_status": "pending",
                "metadata": {
                    "speaker_id": speaker_id, "gender": gender, "age_group": age_group,
                    "original_filename": upload.filename or "", "source": "human_recording",
                },
            })
            imported.append(sample_id)
        except (AudioError, OSError) as exc:
            target.unlink(missing_ok=True)
            failures.append({"file": upload.filename or sample_id, "error": str(exc)[:500]})
        finally:
            temp.unlink(missing_ok=True)
    return {
        "imported": len(imported), "sample_ids": imported,
        "duplicates": duplicates, "failures": failures,
    }


@app.patch("/api/samples/{sample_id}")
def update_sample(sample_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Update review metadata without changing immutable audio provenance."""
    row = db.one("SELECT * FROM samples WHERE id=? AND deleted_at IS NULL", (sample_id,))
    if not row:
        raise HTTPException(404, "样本不存在")
    allowed = {"text", "label", "quality_status", "notes", "split"}
    values = {key: value for key, value in payload.items() if key in allowed}
    metadata_keys = {"speaker_id", "gender", "age_group", "tags"}
    metadata_updates = {key: payload[key] for key in metadata_keys if key in payload}
    if metadata_updates:
        metadata = db.decode_json_fields(dict(row)).get("metadata", {})
        metadata.update(metadata_updates)
        values["metadata_json"] = json.dumps(metadata, ensure_ascii=False)
        if "speaker_id" in metadata_updates and row["provider"] == "real":
            speaker = str(metadata_updates["speaker_id"]).strip()
            if not speaker:
                raise HTTPException(400, "speaker_id 不能为空")
            values["voice_id"] = speaker
            values["voice_name"] = speaker
    if not values:
        raise HTTPException(400, "没有可更新字段")
    if "quality_status" in values and values["quality_status"] not in {
        "unchecked", "review", "pass", "reject", "pending", "accepted", "rejected",
    }:
        raise HTTPException(400, "无效的审核状态")
    if "split" in values and values["split"] not in {
        "unassigned", "unspecified", "train", "dev", "test",
    }:
        raise HTTPException(400, "无效的数据划分")
    for key in ("text", "label"):
        if key in values and not str(values[key]).strip():
            raise HTTPException(400, f"{key} 不能为空")
    assignments = ", ".join(f"{key}=?" for key in values)
    db.execute(f"UPDATE samples SET {assignments} WHERE id=?", (*values.values(), sample_id))
    return _json_row(db.one("SELECT * FROM samples WHERE id=?", (sample_id,)))


def _soft_delete_sample(sample_id: str) -> tuple[bool, bool]:
    row = db.one("SELECT * FROM samples WHERE id=? AND deleted_at IS NULL", (sample_id,))
    if not row:
        return False, False
    source = _safe_file(row["file_path"])
    recoverable = source.exists()
    if recoverable:
        target = TRASH_DIR / "samples" / f"{sample_id}.wav"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), target)
        db.execute("UPDATE samples SET file_path=?,deleted_at=? WHERE id=?", (str(target), utc_now(), sample_id))
    else:
        db.execute("UPDATE samples SET deleted_at=? WHERE id=?", (utc_now(), sample_id))
    return True, recoverable


@app.delete("/api/samples/{sample_id}")
def delete_sample(sample_id: str) -> dict[str, Any]:
    deleted, recoverable = _soft_delete_sample(sample_id)
    if not deleted:
        raise HTTPException(404, "样本不存在")
    return {"deleted": 1, "recoverable": recoverable, "sample_id": sample_id}


@app.post("/api/samples/delete")
def delete_samples(request: DeleteSamplesRequest) -> dict[str, Any]:
    results = [_soft_delete_sample(sample_id) for sample_id in request.ids]
    count = sum(1 for deleted, _ in results if deleted)
    recoverable_count = sum(1 for deleted, recoverable in results if deleted and recoverable)
    return {
        "deleted": count,
        "recoverable": bool(count) and recoverable_count == count,
        "recoverable_count": recoverable_count,
    }


@app.post("/api/samples/{sample_id}/restore")
def restore_sample(sample_id: str) -> dict[str, Any]:
    row = db.one("SELECT * FROM samples WHERE id=? AND deleted_at IS NOT NULL", (sample_id,))
    if not row:
        raise HTTPException(404, "回收站中不存在该样本")
    source = _safe_file(row["file_path"])
    if not source.exists():
        raise HTTPException(404, "回收站音频文件丢失")
    target = SAMPLES_DIR / "restored" / f"{sample_id}.wav"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), target)
    db.execute(
        "UPDATE samples SET file_path=?, deleted_at=NULL WHERE id=?",
        (str(target), sample_id),
    )
    return {"restored": 1, "sample_id": sample_id}


@app.get("/api/samples/{sample_id}/audio")
def sample_audio(sample_id: str) -> FileResponse:
    row = db.one("SELECT file_path FROM samples WHERE id=? AND deleted_at IS NULL", (sample_id,))
    if not row:
        raise HTTPException(404, "样本不存在")
    path = _safe_file(row["file_path"])
    if not path.exists():
        raise HTTPException(404, "音频文件丢失")
    return FileResponse(path, media_type="audio/wav", filename=f"{sample_id}.wav")


@app.get("/api/samples/{sample_id}/analysis")
def sample_analysis(sample_id: str) -> dict[str, Any]:
    row = db.one("SELECT file_path FROM samples WHERE id=? AND deleted_at IS NULL", (sample_id,))
    if not row:
        raise HTTPException(404, "样本不存在")
    path = _safe_file(row["file_path"])
    return {
        "info": probe_wav(path), "metrics": audio_metrics(path),
        "waveform": waveform_points(path), "spectrum": spectrum_points(path),
    }


@app.get("/api/noises")
def noises(include_deleted: bool = False) -> list[dict[str, Any]]:
    where = "" if include_deleted else " WHERE deleted_at IS NULL"
    return [
        _json_row(row)
        for row in db.all(f"SELECT * FROM noises{where} ORDER BY created_at DESC")
    ]


@app.get("/api/noises/{noise_id}/audio")
def noise_audio(noise_id: str) -> FileResponse:
    row = db.one(
        "SELECT file_path,name FROM noises WHERE id=? AND deleted_at IS NULL", (noise_id,)
    )
    if not row:
        raise HTTPException(404, "噪声不存在")
    path = _safe_file(row["file_path"])
    if not path.exists():
        raise HTTPException(404, "噪声音频文件丢失")
    return FileResponse(path, media_type="audio/wav", filename=f"{noise_id}.wav")


@app.post("/api/noises/upload")
async def upload_noise(
    file: UploadFile = File(...), name: str = Form(""), category: str = Form("other"),
    weight: float = Form(1.0), min_snr: float = Form(0.0), max_snr: float = Form(20.0),
    source: str = Form(""), license_info: str = Form("", alias="license"),
) -> dict[str, Any]:
    if max_snr < min_snr:
        raise HTTPException(400, "max_snr 必须大于或等于 min_snr")
    noise_id = uuid.uuid4().hex
    suffix = Path(file.filename or "noise.audio").suffix[:10]
    temp = TMP_DIR / f"{noise_id}{suffix}"
    target = NOISES_DIR / f"{noise_id}.wav"
    try:
        uploaded = 0
        with temp.open("wb") as stream:
            while chunk := await file.read(1024 * 1024):
                uploaded += len(chunk)
                if uploaded > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        413,
                        f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MiB 限制",
                    )
                stream.write(chunk)
        info = await asyncio.to_thread(normalize_wav, temp, target, 16000)
        _validate_duration(info, *NOISE_DURATION_RANGE, "背景噪声")
    except AudioError as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(400, str(exc)) from exc
    finally:
        temp.unlink(missing_ok=True)
    digest = await asyncio.to_thread(sha256_file, target)
    metadata = {"source": source.strip(), "license": license_info.strip()}
    db.execute(
        """
        INSERT INTO noises(
            id,name,category,file_path,duration,sample_rate,weight,min_snr,max_snr,
            sha256,metadata_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (noise_id, name.strip() or file.filename or noise_id, category, str(target), info["duration"],
         info["sample_rate"], max(0.001, weight), min_snr, max_snr, digest,
         json.dumps(metadata, ensure_ascii=False), utc_now()),
    )
    return {"id": noise_id, **info, "sha256": digest, "metadata": metadata}


@app.patch("/api/noises/{noise_id}")
def update_noise(noise_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    current = db.one("SELECT * FROM noises WHERE id=? AND deleted_at IS NULL", (noise_id,))
    if not current:
        raise HTTPException(404, "噪声不存在")
    allowed = {"name", "category", "weight", "min_snr", "max_snr", "enabled"}
    candidate = {**current, **{key: value for key, value in payload.items() if key in allowed}}
    if float(candidate["weight"]) <= 0:
        raise HTTPException(400, "weight 必须大于 0")
    if float(candidate["max_snr"]) < float(candidate["min_snr"]):
        raise HTTPException(400, "max_snr 必须大于或等于 min_snr")
    fields, values = [], []
    for key, value in payload.items():
        if key in allowed:
            fields.append(f"{key}=?"); values.append(int(value) if key == "enabled" else value)
    if not fields:
        raise HTTPException(400, "没有可更新字段")
    values.append(noise_id)
    db.execute(f"UPDATE noises SET {', '.join(fields)} WHERE id=?", tuple(values))
    return _json_row(db.one("SELECT * FROM noises WHERE id=?", (noise_id,)))


@app.delete("/api/noises/{noise_id}")
def delete_noise(noise_id: str) -> dict[str, Any]:
    row = db.one("SELECT * FROM noises WHERE id=? AND deleted_at IS NULL", (noise_id,))
    if not row:
        raise HTTPException(404, "噪声不存在")
    source = _safe_file(row["file_path"])
    recoverable = source.exists()
    if recoverable:
        target = TRASH_DIR / "noises" / f"{noise_id}.wav"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), target)
        db.execute(
            "UPDATE noises SET file_path=?, deleted_at=? WHERE id=?",
            (str(target), utc_now(), noise_id),
        )
    else:
        db.execute("UPDATE noises SET deleted_at=? WHERE id=?", (utc_now(), noise_id))
    return {"deleted": 1, "recoverable": recoverable, "noise_id": noise_id}


@app.post("/api/noises/{noise_id}/restore")
def restore_noise(noise_id: str) -> dict[str, Any]:
    row = db.one("SELECT * FROM noises WHERE id=? AND deleted_at IS NOT NULL", (noise_id,))
    if not row:
        raise HTTPException(404, "回收站中不存在该噪声")
    source = _safe_file(row["file_path"])
    if not source.exists():
        raise HTTPException(404, "回收站噪声文件丢失")
    target = NOISES_DIR / f"{noise_id}.wav"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), target)
    db.execute(
        "UPDATE noises SET file_path=?, deleted_at=NULL WHERE id=?", (str(target), noise_id)
    )
    return {"restored": 1, "noise_id": noise_id}


@app.post("/api/augment")
def augment(request: AugmentRequest) -> dict[str, str]:
    if request.sample_ids:
        placeholders = ",".join("?" for _ in request.sample_ids)
        selected = db.all(f"SELECT * FROM samples WHERE deleted_at IS NULL AND id IN ({placeholders})",
                          tuple(request.sample_ids))
    else:
        selected = db.all("SELECT * FROM samples WHERE deleted_at IS NULL AND parent_id IS NULL")
    if not selected:
        raise HTTPException(400, "没有可增强的样本")
    if len(selected) * request.count > MAX_TASK_ITEMS:
        raise HTTPException(400, f"单个任务最多 {MAX_TASK_ITEMS} 个增强项，请拆分任务")
    payload = request.model_dump()
    task_id = _create_task("augment", len(selected) * request.count, payload)
    _submit(task_id, _augment_task, payload, selected)
    return {"id": task_id, "task_id": task_id}


@app.post("/api/exports")
def create_export(request: ExportRequest) -> dict[str, str]:
    if request.train_ratio + request.dev_ratio >= 1:
        raise HTTPException(400, "train_ratio + dev_ratio 必须小于 1")
    if request.sample_ids:
        placeholders = ",".join("?" for _ in request.sample_ids)
        selected = db.all(f"SELECT * FROM samples WHERE deleted_at IS NULL AND id IN ({placeholders})",
                          tuple(request.sample_ids))
    else:
        selected = db.all("SELECT * FROM samples WHERE deleted_at IS NULL ORDER BY created_at")
    if not selected:
        raise HTTPException(400, "没有可导出的样本")
    _explicit_split_map(selected)
    payload = request.model_dump()
    payload["project"] = project_settings.get()
    task_id = _create_task("export", len(selected), payload)
    _submit(task_id, _export_task, payload, selected)
    return {"id": task_id, "task_id": task_id}


@app.get("/api/exports")
def exports(include_deleted: bool = False) -> list[dict[str, Any]]:
    where = "" if include_deleted else " WHERE deleted_at IS NULL"
    return [
        _json_row(row)
        for row in db.all(f"SELECT * FROM exports{where} ORDER BY created_at DESC")
    ]


@app.get("/api/exports/{export_id}/download")
def download_export(export_id: str) -> FileResponse:
    row = db.one(
        "SELECT * FROM exports WHERE id=? AND deleted_at IS NULL", (export_id,)
    )
    if not row:
        raise HTTPException(404, "导出不存在")
    path = _safe_file(row["file_path"], EXPORTS_DIR)
    if not path.exists():
        raise HTTPException(404, "导出文件丢失")
    return FileResponse(path, media_type="application/zip", filename=path.name)


@app.delete("/api/exports/{export_id}")
def delete_export(export_id: str) -> dict[str, Any]:
    with export_file_lock:
        row = db.one(
            "SELECT * FROM exports WHERE id=? AND deleted_at IS NULL", (export_id,)
        )
        if not row:
            raise HTTPException(404, "导出不存在")
        source = _safe_file(row["file_path"], EXPORTS_DIR)
        target = TRASH_DIR / "exports" / export_id / source.name
        recoverable = source.is_file()
        if target.exists():
            raise HTTPException(409, "回收目录中已存在该导出文件，请先处理后重试")
        if recoverable:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), target)
        try:
            db.execute(
                "UPDATE exports SET file_path=?,deleted_at=? WHERE id=?",
                (str(target), utc_now(), export_id),
            )
        except Exception:
            if recoverable and target.exists() and not source.exists():
                shutil.move(str(target), source)
            raise
        return {"deleted": 1, "recoverable": recoverable, "export_id": export_id}


@app.post("/api/exports/{export_id}/restore")
def restore_export(export_id: str) -> dict[str, Any]:
    with export_file_lock:
        row = db.one(
            "SELECT * FROM exports WHERE id=? AND deleted_at IS NOT NULL", (export_id,)
        )
        if not row:
            raise HTTPException(404, "回收目录中不存在该导出")
        source = _safe_file(row["file_path"], TRASH_DIR / "exports")
        if not source.is_file():
            raise HTTPException(404, "回收目录中的导出文件丢失")
        target = EXPORTS_DIR / source.name
        if target.exists():
            raise HTTPException(409, "导出目录中已存在同名文件，无法恢复")
        shutil.move(str(source), target)
        try:
            db.execute(
                "UPDATE exports SET file_path=?,deleted_at=NULL WHERE id=?",
                (str(target), export_id),
            )
        except Exception:
            if target.exists() and not source.exists():
                shutil.move(str(target), source)
            raise
        try:
            source.parent.rmdir()
        except OSError:
            pass
        return {"restored": 1, "export_id": export_id}


if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
