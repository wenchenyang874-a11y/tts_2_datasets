"""LAN-accessible web application for building a synthetic KWS dataset."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import shutil
import tempfile
import threading
import uuid
import zipfile
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator


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
        get_provider_capabilities,
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
        get_provider_capabilities, validate_provider_configuration,
    )
    from project_settings import ProjectSettingsStore, normalize_display_text  # type: ignore
    from settings_store import ProviderSettingsStore, set_default_settings_store  # type: ignore
    from voice_catalog import builtin_voices, refresh_voice_catalog  # type: ignore


APP_DIR = Path(__file__).resolve().parent
# Audio, manifests, databases, and provider configuration may contain private
# data. Restrict permissions for newly created files without rewriting any
# existing deployment data.
os.umask(0o077)
# Keep the source-tree default convenient without assuming that the application
# is always nested under ``tools/``. Container images copy it directly to /app.
DEFAULT_DATA_ROOT = (
    APP_DIR.parent.parent / "data" / "tts_app"
    if APP_DIR.parent.name == "tools"
    else APP_DIR / "data"
)
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
WORKER_LIMIT = max(1, int(os.getenv("TTS_APP_WORKERS", "8")))
# In-process workers cannot survive a service restart. Make interrupted work
# explicit instead of leaving tasks permanently displayed as running.
startup_time = utc_now()
db.execute(
    """
    UPDATE tasks
    SET status='failed', message='服务重启，任务已中断，请重新创建',
        finished_at=COALESCE(finished_at, ?), updated_at=?
    WHERE status IN ('queued', 'running')
    """,
    (startup_time, startup_time),
)
executor = ThreadPoolExecutor(max_workers=WORKER_LIMIT)

app = FastAPI(
    title="语音数据集工坊",
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
    response = await call_next(request)
    if request.url.path == "/" or Path(request.url.path).suffix in {".html", ".js", ".css"}:
        # The workshop is updated in place. Reusing an older app.js can restore
        # removed form behavior, so browser-facing application assets must not
        # outlive the currently deployed server version.
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


def _canonical_target_kind(value: Any) -> str:
    normalized = str(value or "positive").strip().lower()
    if normalized == "noise":
        normalized = "background"
    if normalized not in {"positive", "negative", "background"}:
        raise ValueError("target_kind 必须是 positive、negative 或 background")
    return normalized


class GenerateRequest(BaseModel):
    text: str = "你好，小助手"
    label: str = "你好小助手"
    keyword_id: str = "ni_hao_xiao_zhu_shou"
    voice_ids: list[int]
    count: int = Field(1, ge=1, le=1000)
    speed: float = Field(1.0, ge=0.5, le=2.0)
    pitch: float = Field(1.0, ge=0.5, le=2.0)
    volume: int = Field(50, ge=0, le=100)
    seed: int = Field(20260804, ge=0, le=2**31 - 1)
    instruction: str = ""
    target_kind: str = "positive"
    provider_audio: dict[str, dict[str, Any]] = Field(default_factory=dict)
    cost_acknowledged: bool = False

    @field_validator("text", "label", "keyword_id")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value cannot be blank")
        if len(value) > 10_000:
            raise ValueError("value is too long")
        return value.strip()

    @field_validator("target_kind", mode="before")
    @classmethod
    def validate_target_kind(cls, value: Any) -> str:
        return _canonical_target_kind(value)


class GenerateBatchRequest(BaseModel):
    texts: list[str]
    label: str = "你好小助手"
    keyword_id: str = "ni_hao_xiao_zhu_shou"
    target_kind: str = "positive"
    voice_ids: list[int]
    count: int = Field(1, ge=1, le=1000)
    concurrency: int = Field(3, ge=1, le=16)
    speed: float = Field(1.0, ge=0.5, le=2.0)
    pitch: float = Field(1.0, ge=0.5, le=2.0)
    volume: int = Field(50, ge=0, le=100)
    seed: int = Field(20260804, ge=0, le=2**31 - 1)
    instruction: str = ""
    random_speed: bool = False
    speed_min: float = Field(0.8, ge=0.5, le=2.0)
    speed_max: float = Field(1.2, ge=0.5, le=2.0)
    random_pitch: bool = False
    pitch_min: float = Field(0.9, ge=0.5, le=2.0)
    pitch_max: float = Field(1.1, ge=0.5, le=2.0)
    random_volume: bool = False
    volume_min: int = Field(40, ge=0, le=100)
    volume_max: int = Field(60, ge=0, le=100)
    provider_audio: dict[str, dict[str, Any]] = Field(default_factory=dict)
    cost_acknowledged: bool = False

    @field_validator("texts")
    @classmethod
    def validate_texts(cls, values: list[str]) -> list[str]:
        texts = [str(value).strip() for value in values if str(value).strip()]
        if not texts:
            raise ValueError("texts cannot be empty")
        if len(texts) > 1000:
            raise ValueError("texts contains too many items")
        if any(len(text) > 10_000 for text in texts):
            raise ValueError("text is too long")
        return texts

    @field_validator("label", "keyword_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value cannot be blank")
        return value

    @field_validator("target_kind", mode="before")
    @classmethod
    def validate_target_kind(cls, value: Any) -> str:
        return _canonical_target_kind(value)

    @model_validator(mode="after")
    def validate_random_ranges(self) -> "GenerateBatchRequest":
        if self.speed_min > self.speed_max:
            raise ValueError("speed_min 不能大于 speed_max")
        if self.pitch_min > self.pitch_max:
            raise ValueError("pitch_min 不能大于 pitch_max")
        if self.volume_min > self.volume_max:
            raise ValueError("volume_min 不能大于 volume_max")
        return self


class AugmentRequest(BaseModel):
    sample_ids: list[str] = Field(default_factory=list)
    noise_ids: list[str] = Field(default_factory=list)
    keyword_id: str = ""
    count: int = Field(1, ge=1, le=100)
    seed: int = Field(20260804, ge=0, le=2**31 - 1)


class DeleteSamplesRequest(BaseModel):
    ids: list[str]


class ExportRequest(BaseModel):
    sample_ids: list[str] = Field(default_factory=list)
    keyword_id: str = ""
    name: str = "ni_hao_xiao_zhu_shou_dataset"
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
    pricing_unit: str | None = Field(None, pattern="^(per_call|per_1k_chars)$")
    pricing_rate: float | None = Field(None, ge=0)
    pricing_currency: str | None = None


class VolcengineProviderSettingsRequest(BaseModel):
    app_id: str | None = None
    access_token: str | None = None
    cluster: str | None = None
    endpoint: str | None = None
    access_key_id: str | None = None
    access_key_secret: str | None = None
    catalog_endpoint: str | None = None
    pricing_unit: str | None = Field(None, pattern="^(per_call|per_1k_chars)$")
    pricing_rate: float | None = Field(None, ge=0)
    pricing_currency: str | None = None


class NoiseMaterializeRequest(BaseModel):
    noise_ids: list[str]
    keyword_id: str

    @field_validator("noise_ids")
    @classmethod
    def validate_noise_ids(cls, values: list[str]) -> list[str]:
        values = [str(value).strip() for value in values if str(value).strip()]
        if not values:
            raise ValueError("noise_ids cannot be empty")
        return values

    @field_validator("keyword_id")
    @classmethod
    def validate_keyword_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("keyword_id cannot be empty")
        return value


def _json_row(row: dict[str, Any]) -> dict[str, Any]:
    row = db.decode_json_fields(row)
    row.pop("file_path", None)
    for raw_json_key in ("metadata_json", "payload_json", "result_json"):
        row.pop(raw_json_key, None)
    for key in ("enabled",):
        if key in row:
            row[key] = bool(row[key])
    if "status" in row:
        task_result = row.get("result") if isinstance(row.get("result"), dict) else {}
        for key in (
            "cost_available", "cost_disclaimer", "cost_by_currency",
            "requested_concurrency", "applied_concurrency",
        ):
            if key in task_result:
                row[key] = task_result[key]
        row["cost_is_estimate"] = True
    if "status" in row and "created_at" in row:
        created_at = row.get("created_at")
        started_at = row.get("started_at")
        finished_at = row.get("finished_at")
        try:
            created = datetime.fromisoformat(str(created_at)) if created_at else None
            started = datetime.fromisoformat(str(started_at)) if started_at else None
            if finished_at:
                end = datetime.fromisoformat(str(finished_at))
            elif row.get("status") in {"queued", "running"}:
                end = datetime.now(timezone.utc)
            else:
                end = None
            queue_duration = (
                max(0.0, (started - created).total_seconds())
                if created and started else None
            )
            execution_duration = (
                max(0.0, (end - started).total_seconds()) if started and end else None
            )
            total_duration = (
                max(0.0, (end - created).total_seconds()) if created and end else None
            )
            row["queue_duration_seconds"] = (
                round(queue_duration, 3) if queue_duration is not None else None
            )
            row["execution_duration_seconds"] = (
                round(execution_duration, 3) if execution_duration is not None else None
            )
            row["total_duration_seconds"] = (
                round(total_duration, 3) if total_duration is not None else None
            )
            # Backward-compatible alias for clients that previously displayed
            # only execution time.
            row["duration_seconds"] = row["execution_duration_seconds"]
        except ValueError:
            row["queue_duration_seconds"] = None
            row["execution_duration_seconds"] = None
            row["total_duration_seconds"] = None
            row["duration_seconds"] = None
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


def _wekws_keyword_token(keyword_id: Any) -> str:
    """Build one ASCII token for WeKWS max-pooling classification."""
    value = "".join(
        character.upper()
        if character.isascii() and character.isalnum()
        else "_"
        for character in str(keyword_id)
    ).strip("_")
    if not value:
        digest = hashlib.sha256(str(keyword_id).encode("utf-8")).hexdigest()[:12].upper()
        value = f"KWS_{digest}"
    return f"<{value[:80]}>"


def _validate_duration(info: dict[str, Any], minimum: float, maximum: float, kind: str) -> None:
    duration = float(info.get("duration", 0.0))
    if duration < minimum or duration > maximum:
        raise AudioError(f"{kind}时长必须在 {minimum:g} 到 {maximum:g} 秒之间，当前为 {duration:.3f} 秒")


TERMINAL_TASK_STATES = {"completed", "partial", "failed"}
PARENT_TASK_TYPES = {"generate_batch", "augment_batch"}


def _create_task(
    task_type: str,
    total: int,
    payload: dict[str, Any],
    *,
    parent_id: str | None = None,
    item_index: int | None = None,
    estimated_cost: float = 0.0,
    currency: str = "",
    billable_units: float = 0.0,
    result: dict[str, Any] | None = None,
) -> str:
    task_id = uuid.uuid4().hex
    now = utc_now()
    db.execute(
        """
        INSERT INTO tasks(
            id,parent_id,item_index,type,status,total,payload_json,result_json,
            estimated_cost,currency,billable_units,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            task_id, parent_id, item_index, task_type, "queued", total,
            json.dumps(payload, ensure_ascii=False),
            json.dumps(result or {}, ensure_ascii=False),
            round(max(0.0, float(estimated_cost)), 8), currency,
            max(0.0, float(billable_units)), now, now,
        ),
    )
    return task_id


def _update_task(task_id: str, **fields: Any) -> None:
    allowed = {
        "status", "completed", "failed", "message", "result_json",
        "estimated_cost", "currency", "billable_units",
    }
    updates, values = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        updates.append(f"{key}=?")
        values.append(json.dumps(value, ensure_ascii=False) if key == "result_json" else value)
    now = utc_now()
    status_value = fields.get("status")
    if status_value == "running":
        updates.append("started_at=COALESCE(started_at, ?)")
        values.append(now)
    elif status_value in TERMINAL_TASK_STATES:
        updates.append("started_at=COALESCE(started_at, ?)")
        values.append(now)
        updates.append("finished_at=COALESCE(finished_at, ?)")
        values.append(now)
    updates.append("updated_at=?")
    values.extend((now, task_id))
    db.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id=?", tuple(values))


def _task_result(task_id: str) -> dict[str, Any]:
    row = db.one("SELECT result_json FROM tasks WHERE id=?", (task_id,))
    if not row:
        return {}
    return db.decode_json_fields(row).get("result", {})


def _execute_task(task_id: str, function, *args) -> None:
    _update_task(task_id, status="running")
    try:
        function(task_id, *args)
    except Exception as exc:
        result = _task_result(task_id)
        result["internal_error"] = type(exc).__name__
        _update_task(
            task_id,
            status="failed",
            message=str(exc)[:1000],
            result_json=result,
        )


def _submit(task_id: str, function, *args) -> Future[Any]:
    return executor.submit(_execute_task, task_id, function, *args)


def _insert_sample(sample: dict[str, Any]) -> None:
    db.execute(
        """
        INSERT INTO samples(
            id,provider,voice_id,voice_name,model,text,label,keyword_id,speed,pitch,volume,seed,
            file_path,duration,sample_rate,channels,sha256,parent_id,noise_id,snr_db,split,
            quality_status,notes,metadata_json,target_kind,source_kind,variant_kind,
            origin_task_id,origin_job_id,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            sample["id"], sample["provider"], sample["voice_id"], sample.get("voice_name", ""),
            sample.get("model", ""), sample["text"], sample.get("label", "你好小助手"),
            sample.get("keyword_id", "ni_hao_xiao_zhu_shou"), sample.get("speed", 1.0),
            sample.get("pitch", 1.0), sample.get("volume", 50), sample.get("seed", 0),
            sample["file_path"], sample["duration"], sample["sample_rate"], sample.get("channels", 1),
            sample["sha256"], sample.get("parent_id"), sample.get("noise_id"), sample.get("snr_db"),
            sample.get("split", "unassigned"), sample.get("quality_status", "pass"),
            sample.get("notes", ""), json.dumps(sample.get("metadata", {}), ensure_ascii=False),
            _canonical_target_kind(sample.get("target_kind", "positive")),
            sample.get("source_kind", "tts"),
            sample.get("variant_kind", "clean"), sample.get("origin_task_id"),
            sample.get("origin_job_id"), utc_now(),
        ),
    )


BILLABLE_PROVIDERS = {"aliyun", "volcengine"}


def _local_cost_result() -> dict[str, Any]:
    return {
        "cost_available": True,
        "estimated_cost": 0.0,
        "cost_disclaimer": "本地操作，不产生云费用",
    }


def _select_voices(voice_ids: list[int]) -> list[dict[str, Any]]:
    if not voice_ids:
        return []
    placeholders = ",".join("?" for _ in voice_ids)
    return db.all(
        f"SELECT * FROM voices WHERE enabled=1 AND id IN ({placeholders})",
        tuple(voice_ids),
    )


def _require_cost_acknowledgement(
    voices: list[dict[str, Any]], cost_acknowledged: bool,
) -> None:
    providers = sorted({str(voice["provider"]) for voice in voices} & BILLABLE_PROVIDERS)
    if providers and not cost_acknowledged:
        raise HTTPException(
            400,
            "选择云供应商会产生费用；请明确设置 cost_acknowledged=true 后重新提交："
            + ", ".join(providers),
        )


def _provider_audio_for(request: dict[str, Any], provider_name: str) -> dict[str, Any]:
    configured = request.get("provider_audio") or {}
    raw = configured.get(provider_name) if isinstance(configured, dict) else None
    value = raw if isinstance(raw, dict) else {}
    capabilities = get_provider_capabilities(provider_name)
    audio_format = str(value.get("format") or value.get("audio_format") or "wav").strip().lower()
    try:
        sample_rate = int(value.get("sample_rate", 16_000))
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"{provider_name} sample_rate 必须是整数") from exc
    if audio_format not in capabilities["audio_formats"]:
        raise HTTPException(
            400,
            f"{provider_name} 不支持格式 {audio_format}；可选值："
            + ", ".join(capabilities["audio_formats"]),
        )
    if sample_rate not in capabilities["sample_rates"]:
        raise HTTPException(
            400,
            f"{provider_name} 不支持采样率 {sample_rate}；可选值："
            + ", ".join(str(item) for item in capabilities["sample_rates"]),
        )
    return {"format": audio_format, "sample_rate": sample_rate}


def _validate_provider_audio(
    request: dict[str, Any], voices: list[dict[str, Any]],
) -> None:
    for provider_name in {str(voice["provider"]) for voice in voices}:
        _provider_audio_for(request, provider_name)


def _estimate_generation_cost(
    text: str, voices: list[dict[str, Any]], count: int,
) -> dict[str, Any]:
    voice_counts: dict[str, int] = {}
    for voice in voices:
        provider_name = str(voice["provider"])
        voice_counts[provider_name] = voice_counts.get(provider_name, 0) + 1
    breakdown: list[dict[str, Any]] = []
    currencies: set[str] = set()
    estimated_cost = 0.0
    billable_units = 0.0
    cost_available = True
    cost_by_currency: dict[str, float] = {}
    units_by_pricing_unit: dict[str, float] = {}
    for provider_name, voice_count in sorted(voice_counts.items()):
        calls = voice_count * count
        if provider_name not in BILLABLE_PROVIDERS:
            breakdown.append({
                "provider": provider_name, "cost_available": True,
                "pricing_unit": "free", "billable_units": 0.0,
                "pricing_rate": 0.0, "currency": "", "estimated_cost": 0.0,
            })
            continue
        try:
            config = provider_settings.get_provider(provider_name)
        except (ValueError, RuntimeError):
            config = {}
        unit = str(config.get("pricing_unit") or "").strip()
        currency = str(config.get("pricing_currency") or "").strip().upper()
        try:
            rate = float(config.get("pricing_rate"))
        except (TypeError, ValueError):
            rate = -1.0
        available = unit in {"per_call", "per_1k_chars"} and rate >= 0 and bool(currency)
        if unit == "per_call":
            units = float(calls)
        elif unit == "per_1k_chars":
            units = len(text) * calls / 1000.0
        else:
            units = 0.0
        provider_cost = units * rate if available else 0.0
        cost_available = cost_available and available
        if available:
            currencies.add(currency)
            estimated_cost += provider_cost
            billable_units += units
            cost_by_currency[currency] = cost_by_currency.get(currency, 0.0) + provider_cost
            units_by_pricing_unit[unit] = units_by_pricing_unit.get(unit, 0.0) + units
        breakdown.append({
            "provider": provider_name, "cost_available": available,
            "pricing_unit": unit, "billable_units": round(units, 8),
            "pricing_rate": rate if rate >= 0 else 0.0,
            "currency": currency, "estimated_cost": round(provider_cost, 8),
        })
    mixed_currency = len(currencies) > 1
    cost_available = cost_available and not mixed_currency
    currency = next(iter(currencies)) if len(currencies) == 1 else ("MIXED" if currencies else "")
    total_cost = estimated_cost if cost_available else 0.0
    comparable_units = billable_units if len(units_by_pricing_unit) <= 1 else 0.0
    return {
        "cost_available": cost_available,
        "estimated_cost": round(total_cost, 8),
        "currency": currency,
        "billable_units": round(comparable_units, 8),
        "cost_by_currency": {
            key: round(value, 8) for key, value in sorted(cost_by_currency.items())
        },
        "billable_units_by_pricing_unit": {
            key: round(value, 8) for key, value in sorted(units_by_pricing_unit.items())
        },
        "cost_breakdown": breakdown,
        "cost_disclaimer": "预估费用，最终费用以供应商账单为准",
    }


def _deterministic_generation_parameters(
    request: dict[str, Any], voice: dict[str, Any], index: int,
) -> tuple[float, float, int, int]:
    if not request.get("batch_mode"):
        return (
            float(request["speed"]), float(request["pitch"]), int(request["volume"]),
            int(request["seed"]) + index,
        )
    material = "\0".join((
        str(request["seed"]), str(request["text"]), str(voice["provider"]),
        str(voice["voice_id"]), str(voice.get("model") or ""),
        str(request.get("item_index", 0)), str(index),
    )).encode("utf-8")
    digest = hashlib.sha256(material).digest()
    rng = random.Random(int.from_bytes(digest[:8], "big"))
    speed = (
        rng.uniform(float(request["speed_min"]), float(request["speed_max"]))
        if request.get("random_speed") else float(request["speed"])
    )
    pitch = (
        rng.uniform(float(request["pitch_min"]), float(request["pitch_max"]))
        if request.get("random_pitch") else float(request["pitch"])
    )
    volume = (
        rng.randint(int(request["volume_min"]), int(request["volume_max"]))
        if request.get("random_volume") else int(request["volume"])
    )
    if voice["provider"] == "volcengine":
        pitch = 1.0
    item_seed = int.from_bytes(digest[8:12], "big") & 0x7FFFFFFF
    return round(speed, 6), round(pitch, 6), volume, item_seed


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
            speed, pitch, volume, item_seed = _deterministic_generation_parameters(
                request, voice, index
            )
            provider_audio = _provider_audio_for(request, provider_name)
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
                      AND target_kind=?
                      AND speed=? AND pitch=? AND volume=? AND seed=?
                      AND COALESCE(json_extract(metadata_json, '$.instruction'), '')=?
                      AND COALESCE(json_extract(metadata_json, '$.provider_audio_request.format'), 'wav')=?
                      AND COALESCE(json_extract(metadata_json, '$.provider_audio_request.sample_rate'), 16000)=?
                    """,
                    (
                        provider_name, voice["voice_id"], voice["model"], request["text"],
                        request["label"], request["keyword_id"], request.get("target_kind", "positive"),
                        speed, pitch, volume, item_seed,
                        request.get("instruction", ""),
                        provider_audio["format"], provider_audio["sample_rate"],
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
                    audio_format=provider_audio["format"],
                    sample_rate=provider_audio["sample_rate"],
                    speed=speed, pitch=pitch, volume=volume,
                    seed=item_seed, instruction=request.get("instruction", ""),
                )
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                raw_path.write_bytes(audio)
                info = normalize_wav(raw_path, wav_path, 16000)
                digest = sha256_file(wav_path)
                if db.one(
                    """
                    SELECT id FROM samples
                    WHERE sha256=? AND keyword_id=? AND target_kind=?
                      AND deleted_at IS NULL
                    """,
                    (
                        digest, request["keyword_id"],
                        request.get("target_kind", "positive"),
                    ),
                ):
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
                    "target_kind": request.get("target_kind", "positive"),
                    "source_kind": "tts", "variant_kind": "clean",
                    "origin_task_id": task_id,
                    "origin_job_id": request.get("origin_job_id") or task_id,
                    "speed": speed, "pitch": pitch, "volume": volume,
                    "seed": item_seed, "file_path": str(wav_path), "duration": info["duration"],
                    "sample_rate": info["sample_rate"], "channels": info["channels"], "sha256": digest,
                    "quality_status": quality,
                    "metadata": {
                        "voice_db_id": voice["id"], "instruction": request.get("instruction", ""),
                        "gender": voice.get("gender", "unknown"),
                        "age_group": voice.get("age_group", "unknown"),
                        "provider_audio_request": provider_audio,
                        "normalized_output_audio": {
                            "format": "wav", "sample_rate": info["sample_rate"],
                            "channels": info["channels"], "sample_width_bits": 16,
                        },
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
    result = _task_result(task_id)
    result.update({"errors": errors, "duplicates": duplicates})
    _update_task(task_id, status=status, completed=completed, failed=failed,
                 message=summary[:1000], result_json=result)


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
    errors: list[str] = []
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
                if db.one(
                    """
                    SELECT id FROM samples
                    WHERE sha256=? AND keyword_id=? AND target_kind=?
                      AND deleted_at IS NULL
                    """,
                    (
                        digest, sample["keyword_id"],
                        sample.get("target_kind", "positive"),
                    ),
                ):
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
                    "quality_status": "pending",
                    "target_kind": sample.get("target_kind", "positive"),
                    "source_kind": sample.get("source_kind") or (
                        "human" if sample.get("provider") == "real" else "tts"
                    ),
                    "variant_kind": "augmented", "origin_task_id": task_id,
                    "origin_job_id": request.get("origin_job_id") or task_id,
                    "metadata": {
                        **source_metadata,
                        "augmentation": "additive_noise", "noise_name": noise["name"],
                        "noise_offset": int(info.get("noise_offset", 0)),
                        "augmentation_task_id": task_id,
                        "source_sample_id": sample["id"],
                        "source_quality_status": sample.get("quality_status", "unchecked"),
                    },
                })
                completed += 1
            except Exception as exc:
                failed += 1
                detail = f"{sample['id'][:12]}: {str(exc)[:500]}"
                if detail not in errors and len(errors) < 10:
                    errors.append(detail)
                _update_task(task_id, message=detail)
            _update_task(task_id, completed=completed, failed=failed)
    status = "completed" if failed == 0 else ("partial" if completed else "failed")
    result = _task_result(task_id)
    result.update({"errors": errors, "duplicates": duplicates})
    _update_task(task_id, status=status, completed=completed, failed=failed,
                 message=f"增强完成 {completed}，失败 {failed}，重复 {duplicates}",
                 result_json=result)


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
    project = request.get("project") or project_settings.get()
    wekws_keyword_token = _wekws_keyword_token(project["keyword_id"])
    wekws_filler_token = "<FILLER>"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, sample in enumerate(samples):
            split = _split_for(sample, request, explicit_splits)
            source_path = _safe_file(sample["file_path"])
            if not request["include_audio"]:
                audio_name = str(MANIFEST_DATA_ROOT / source_path.relative_to(DATA_ROOT))
            elif request["format"] == "folders":
                audio_name = (
                    f"folders/{split}/{_safe_component(sample.get('target_kind', 'positive'), 'target')}/"
                    f"{_safe_component(sample['keyword_id'], 'keyword')}/"
                    f"{sample['id']}.wav"
                )
            else:
                audio_name = f"audio/{split}/{sample['id']}.wav"
            sample_metadata = db.decode_json_fields(dict(sample)).get("metadata", {})
            source_kind = sample.get("source_kind") or (
                "human" if sample["provider"] == "real"
                else "noise" if sample["provider"] == "noise"
                else "tts"
            )
            variant_kind = sample.get("variant_kind") or (
                "augmented" if sample.get("parent_id") else "clean"
            )
            source_prefix = "human_recording" if source_kind == "human" else source_kind
            source = (
                f"{source_prefix}_augmented"
                if variant_kind == "augmented" else source_prefix
            )
            record = {
                "utt_id": sample["id"], "audio": audio_name, "label": sample["label"],
                "audio_path_type": audio_path_type,
                "keyword_id": sample["keyword_id"], "text": sample["text"],
                "target_kind": sample.get("target_kind", "positive"),
                "target": 1 if sample.get("target_kind", "positive") == "positive" else 0,
                "speaker_id": _speaker_for(sample),
                "split": split,
                "source": source,
                "source_kind": source_kind,
                "variant_kind": variant_kind,
                "origin_task_id": sample.get("origin_task_id"),
                "origin_job_id": sample.get("origin_job_id"),
                "provider": sample["provider"], "voice_id": sample["voice_id"],
                "voice_name": sample.get("voice_name", ""),
                "model": sample["model"], "speed": sample["speed"], "pitch": sample["pitch"],
                "volume": sample.get("volume", 50),
                "seed": sample["seed"], "sha256": sample["sha256"],
                "duration": sample.get("duration", 0.0),
                "sample_rate": sample.get("sample_rate", 16_000),
                "channels": sample.get("channels", 1),
                "sample_width_bits": sample_metadata.get(
                    "normalized_output_audio", {}
                ).get("sample_width_bits", 16),
                "audio_format": sample_metadata.get(
                    "normalized_output_audio", {}
                ).get("format", "wav"),
                "quality_status": sample.get("quality_status", "unchecked"),
                "notes": sample.get("notes", ""),
                "created_at": sample.get("created_at"),
                "parent_id": sample.get("parent_id"), "noise_id": sample.get("noise_id"),
                "snr_db": sample.get("snr_db"),
                "noise_offset": sample_metadata.get("noise_offset"),
                "provider_audio_request": sample_metadata.get("provider_audio_request"),
                "normalized_output_audio": sample_metadata.get(
                    "normalized_output_audio",
                    {
                        "format": "wav",
                        "sample_rate": sample.get("sample_rate", 16_000),
                        "channels": sample.get("channels", 1),
                        "sample_width_bits": 16,
                    },
                ),
                "metadata": sample_metadata,
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
                    "".join(
                        f"{record['utt_id']} "
                        f"{wekws_keyword_token if record['target_kind'] == 'positive' else wekws_filler_token}\n"
                        for record in records
                    ),
                )
                archive.writestr(
                    f"wekws/{split}/wav.scp",
                    "".join(f"{record['utt_id']} {record['audio']}\n" for record in records),
                )
                archive.writestr(
                    f"wekws/{split}/wav.dur",
                    "".join(
                        f"{record['utt_id']} {float(record['duration']):.6f}\n"
                        for record in records
                    ),
                )
                archive.writestr(
                    f"wekws/{split}/target",
                    "".join(f"{record['utt_id']} {record['target']}\n" for record in records),
                )
                archive.writestr(
                    f"wekws/{split}/utt2target_kind",
                    "".join(
                        f"{record['utt_id']} {record['target_kind']}\n" for record in records
                    ),
                )
                archive.writestr(
                    f"wekws/{split}/utt2source",
                    "".join(
                        f"{record['utt_id']} {record['source_kind']}\n" for record in records
                    ),
                )
                archive.writestr(
                    f"wekws/{split}/utt2variant",
                    "".join(
                        f"{record['utt_id']} {record['variant_kind']}\n" for record in records
                    ),
                )
                archive.writestr(
                    f"wekws/{split}/utt2job",
                    "".join(
                        f"{record['utt_id']} {record['origin_job_id'] or '-'}\n"
                        for record in records
                    ),
                )
        if request["format"] == "wekws":
            archive.writestr(
                "wekws/dict/dict.txt",
                f"{wekws_filler_token} -1\n{wekws_keyword_token} 0\n",
            )
            archive.writestr(
                "wekws/dict/words.txt",
                f"{wekws_filler_token}\n{wekws_keyword_token}\n",
            )
            archive.writestr(
                "wekws/README.txt",
                (
                    "WeKWS max-pooling single-keyword export\n"
                    f"keyword token: {wekws_keyword_token}\n"
                    f"filler token: {wekws_filler_token}\n\n"
                    "Run make_list.py from the extracted archive root, for example:\n"
                    "python /path/to/wekws/tools/make_list.py "
                    "wekws/train/wav.scp wekws/train/text wekws/train/wav.dur "
                    "wekws/train/data.list\n"
                ),
            )
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
            "targets": {
                target_kind: sum(
                    1 for records in manifests.values() for record in records
                    if record["target_kind"] == target_kind
                )
                for target_kind in ("positive", "negative", "background")
            },
            "wekws_text_semantics": {
                "positive": wekws_keyword_token,
                "negative": wekws_filler_token,
                "background": wekws_filler_token,
                "class_ids": {wekws_filler_token: -1, wekws_keyword_token: 0},
            } if request["format"] == "wekws" else None,
            "display_text_unavailable": bool(project.get("display_text_unavailable", False)),
        }, ensure_ascii=False, indent=2))
        if checksums:
            archive.writestr("SHA256SUMS", "\n".join(checksums) + "\n")
    db.execute("INSERT INTO exports(id,name,file_path,sample_count,format,created_at) VALUES(?,?,?,?,?,?)",
               (export_id, safe_name, str(archive_path), len(samples), request["format"], utc_now()))
    result = _task_result(task_id)
    result["export_id"] = export_id
    _update_task(
        task_id, status="completed", completed=len(samples),
        message=f"已导出 {len(samples)} 条样本", result_json=result,
    )


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
        "SELECT COUNT(*) AS n FROM tasks WHERE status='running'"
    )["n"]
    queued_tasks = db.one(
        "SELECT COUNT(*) AS n FROM tasks WHERE status='queued'"
    )["n"]
    voice_count = db.one("SELECT COUNT(*) AS n FROM voices WHERE enabled=1")["n"]
    noise_count = db.one(
        "SELECT COUNT(*) AS n FROM noises WHERE enabled=1 AND deleted_at IS NULL"
    )["n"]
    disk = shutil.disk_usage(DATA_ROOT)
    project = project_settings.get()
    return {
        "ok": True, "app": "语音数据集工坊", "version": app.version,
        "wake_word": project["display_text"], "keyword_id": project["keyword_id"],
        "project": project,
        "samples": sample_count, "augmented_samples": augmented_count,
        "sample_duration_seconds": round(float(duration), 3),
        "running_tasks": running_tasks, "queued_tasks": queued_tasks,
        "worker_limit": WORKER_LIMIT, "voices": voice_count, "noises": noise_count,
        "disk_free_gb": round(disk.free / 1024**3, 1),
    }


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": True, "app": "语音数据集工坊", "version": app.version}


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


@app.get("/api/providers/capabilities")
def provider_capabilities() -> dict[str, dict[str, Any]]:
    return {
        provider_id: get_provider_capabilities(provider_id)
        for provider_id in ("mock", "aliyun", "volcengine")
    }


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


def _aggregate_parent_task(parent_id: str) -> None:
    children = db.all(
        "SELECT * FROM tasks WHERE parent_id=? ORDER BY item_index,created_at", (parent_id,)
    )
    if not children:
        return
    completed = sum(int(child.get("completed") or 0) for child in children)
    recorded_failed = sum(int(child.get("failed") or 0) for child in children)
    failed = recorded_failed + sum(
        max(
            0,
            int(child.get("total") or 0)
            - int(child.get("completed") or 0)
            - int(child.get("failed") or 0),
        )
        for child in children
        if child.get("status") == "failed"
    )
    statuses = [str(child.get("status") or "queued") for child in children]
    all_terminal = all(status in TERMINAL_TASK_STATES for status in statuses)
    if all_terminal:
        if failed == 0 and all(status == "completed" for status in statuses):
            status_value = "completed"
        elif completed > 0:
            status_value = "partial"
        else:
            status_value = "failed"
    else:
        status_value = "running"
    estimated_cost = sum(float(child.get("estimated_cost") or 0.0) for child in children)
    billable_units = sum(float(child.get("billable_units") or 0.0) for child in children)
    currencies = {str(child.get("currency") or "") for child in children if child.get("currency")}
    currency = next(iter(currencies)) if len(currencies) == 1 else ("MIXED" if currencies else "")
    parent_result = _task_result(parent_id)
    child_results = [db.decode_json_fields(dict(child)).get("result", {}) for child in children]
    disclaimers = {
        str(result.get("cost_disclaimer") or "").strip()
        for result in child_results
        if str(result.get("cost_disclaimer") or "").strip()
    }
    cost_by_currency: dict[str, float] = {}
    for result in child_results:
        for key, value in (result.get("cost_by_currency") or {}).items():
            cost_by_currency[str(key)] = cost_by_currency.get(str(key), 0.0) + float(value)
    parent_result.update({
        "child_ids": [child["id"] for child in children],
        "child_status_counts": {
            state: statuses.count(state)
            for state in ("queued", "running", "completed", "partial", "failed")
        },
        "cost_available": all(
            bool(result.get("cost_available")) for result in child_results
        ),
        "cost_by_currency": {
            key: round(value, 8) for key, value in sorted(cost_by_currency.items())
        },
        "cost_disclaimer": (
            next(iter(disclaimers))
            if len(disclaimers) == 1
            else "预估费用，最终费用以供应商账单为准"
        ),
    })
    message = (
        f"子任务 {sum(status in TERMINAL_TASK_STATES for status in statuses)}/{len(children)}；"
        f"样本完成 {completed}，失败 {failed}"
    )
    _update_task(
        parent_id,
        status=status_value,
        completed=completed,
        failed=failed,
        message=message,
        result_json=parent_result,
        estimated_cost=estimated_cost,
        currency=currency,
        billable_units=billable_units,
    )


def _run_generate_batch(
    parent_id: str,
    child_specs: list[tuple[str, dict[str, Any], list[dict[str, Any]]]],
    applied_concurrency: int,
) -> None:
    _update_task(parent_id, status="running")
    pending = iter(child_specs)
    active: set[Future[Any]] = set()

    def submit_next() -> bool:
        try:
            child_id, payload, voices = next(pending)
        except StopIteration:
            return False
        active.add(_submit(child_id, _generate_task, payload, voices))
        return True

    try:
        for _ in range(applied_concurrency):
            if not submit_next():
                break
        while active:
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            active.difference_update(done)
            _aggregate_parent_task(parent_id)
            for _ in done:
                submit_next()
        _aggregate_parent_task(parent_id)
    except Exception as exc:
        result = _task_result(parent_id)
        result["internal_error"] = type(exc).__name__
        _update_task(
            parent_id, status="failed", message=str(exc)[:1000], result_json=result
        )


def _run_augment_batch(
    parent_id: str,
    child_specs: list[tuple[str, dict[str, Any], list[dict[str, Any]]]],
    applied_concurrency: int,
) -> None:
    """Run one augmentation child per source sample and aggregate the parent."""
    _update_task(parent_id, status="running")
    pending = iter(child_specs)
    active: set[Future[Any]] = set()

    def submit_next() -> bool:
        try:
            child_id, payload, samples = next(pending)
        except StopIteration:
            return False
        active.add(_submit(child_id, _augment_task, payload, samples))
        return True

    try:
        for _ in range(applied_concurrency):
            if not submit_next():
                break
        while active:
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            active.difference_update(done)
            _aggregate_parent_task(parent_id)
            for _ in done:
                submit_next()
        _aggregate_parent_task(parent_id)
    except Exception as exc:
        result = _task_result(parent_id)
        result["internal_error"] = type(exc).__name__
        _update_task(
            parent_id, status="failed", message=str(exc)[:1000], result_json=result
        )


@app.post("/api/tasks/generate")
def create_generate_task(request: GenerateRequest) -> dict[str, Any]:
    selected = _select_voices(request.voice_ids)
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
    _require_cost_acknowledgement(selected, request.cost_acknowledged)
    _validate_provider_audio(payload, selected)
    estimate = _estimate_generation_cost(payload["text"], selected, request.count)
    task_id = _create_task(
        "generate", len(selected) * request.count, payload,
        estimated_cost=estimate["estimated_cost"], currency=estimate["currency"],
        billable_units=estimate["billable_units"], result=estimate,
    )
    _submit(task_id, _generate_task, payload, selected)
    return {
        "id": task_id, "task_id": task_id,
        "estimated_cost": estimate["estimated_cost"], "currency": estimate["currency"],
        "cost_available": estimate["cost_available"], "cost_is_estimate": True,
    }


@app.post("/api/tasks/generate-batch")
def create_generate_batch(request: GenerateBatchRequest) -> dict[str, Any]:
    selected = _select_voices(request.voice_ids)
    if not selected:
        raise HTTPException(400, "请选择至少一个可用音色")
    has_volcengine = any(
        voice["provider"] == "volcengine" for voice in selected
    )
    if has_volcengine and request.random_pitch:
        raise HTTPException(
            400,
            "火山引擎 V1 暂不支持随机音高；混合供应商需要随机音高时请拆分批次",
        )
    if has_volcengine and abs(request.pitch - 1.0) > 1e-9:
        raise HTTPException(
            400, "火山引擎 V1 暂不支持音高调节，请将固定音高设为 0 st"
        )
    total = len(request.texts) * len(selected) * request.count
    if total > MAX_TASK_ITEMS:
        raise HTTPException(400, f"单个批次最多 {MAX_TASK_ITEMS} 个合成项，请拆分任务")
    payload = request.model_dump()
    project = project_settings.get()
    if "label" not in request.model_fields_set:
        payload["label"] = project["label"]
    if "keyword_id" not in request.model_fields_set:
        payload["keyword_id"] = project["keyword_id"]
    _require_cost_acknowledgement(selected, request.cost_acknowledged)
    _validate_provider_audio(payload, selected)
    applied_concurrency = min(request.concurrency, WORKER_LIMIT, len(request.texts))
    child_estimates = [
        _estimate_generation_cost(text, selected, request.count) for text in request.texts
    ]
    currencies = {
        str(estimate["currency"]) for estimate in child_estimates if estimate["currency"]
    }
    currency = next(iter(currencies)) if len(currencies) == 1 else ("MIXED" if currencies else "")
    parent_result = {
        "requested_concurrency": request.concurrency,
        "applied_concurrency": applied_concurrency,
        "cost_available": all(estimate["cost_available"] for estimate in child_estimates),
        "cost_by_currency": {
            currency_key: round(sum(
                estimate.get("cost_by_currency", {}).get(currency_key, 0.0)
                for estimate in child_estimates
            ), 8)
            for currency_key in sorted({
                key for estimate in child_estimates
                for key in estimate.get("cost_by_currency", {})
            })
        },
        "cost_disclaimer": "预估费用，最终费用以供应商账单为准",
    }
    parent_id = _create_task(
        "generate_batch", total,
        {**payload, "applied_concurrency": applied_concurrency},
        estimated_cost=sum(estimate["estimated_cost"] for estimate in child_estimates),
        currency=currency,
        billable_units=sum(estimate["billable_units"] for estimate in child_estimates),
        result=parent_result,
    )
    child_specs: list[tuple[str, dict[str, Any], list[dict[str, Any]]]] = []
    child_ids: list[str] = []
    for item_index, (text, estimate) in enumerate(zip(request.texts, child_estimates)):
        child_payload = {
            key: value for key, value in payload.items() if key not in {"texts", "concurrency"}
        }
        child_payload.update({
            "text": text, "item_index": item_index, "batch_mode": True,
            "origin_job_id": parent_id,
        })
        child_id = _create_task(
            "generate", len(selected) * request.count, child_payload,
            parent_id=parent_id, item_index=item_index,
            estimated_cost=estimate["estimated_cost"], currency=estimate["currency"],
            billable_units=estimate["billable_units"], result=estimate,
        )
        child_ids.append(child_id)
        child_specs.append((child_id, child_payload, selected))
    parent_result["child_ids"] = child_ids
    _update_task(parent_id, result_json=parent_result)
    threading.Thread(
        target=_run_generate_batch,
        args=(parent_id, child_specs, applied_concurrency),
        name=f"generate-batch-{parent_id[:8]}",
        daemon=True,
    ).start()
    return {
        "id": parent_id, "task_id": parent_id, "parent_id": parent_id,
        "child_ids": child_ids, "requested_concurrency": request.concurrency,
        "applied_concurrency": applied_concurrency,
        "estimated_cost": round(sum(e["estimated_cost"] for e in child_estimates), 8),
        "currency": currency,
        "cost_available": parent_result["cost_available"], "cost_is_estimate": True,
    }


@app.get("/api/tasks")
def tasks(
    limit: int = Query(50, ge=1, le=500), parent_id: str | None = None,
) -> list[dict[str, Any]]:
    if parent_id is not None:
        rows = db.all(
            "SELECT * FROM tasks WHERE parent_id=? ORDER BY item_index,created_at LIMIT ?",
            (parent_id, limit),
        )
    else:
        rows = db.all(
            "SELECT * FROM tasks WHERE parent_id IS NULL "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    result = [_json_row(row) for row in rows]
    if parent_id is None:
        for item, row in zip(result, rows):
            if row.get("type") in PARENT_TASK_TYPES:
                item["children"] = [
                    _json_row(child)
                    for child in db.all(
                        "SELECT * FROM tasks WHERE parent_id=? "
                        "ORDER BY item_index,created_at",
                        (row["id"],),
                    )
                ]
    return result


@app.get("/api/tasks/{task_id}")
def task(task_id: str) -> dict[str, Any]:
    row = db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not row:
        raise HTTPException(404, "任务不存在")
    result = _json_row(row)
    if row.get("type") in PARENT_TASK_TYPES:
        result["children"] = [
            _json_row(child)
            for child in db.all(
                "SELECT * FROM tasks WHERE parent_id=? ORDER BY item_index,created_at",
                (task_id,),
            )
        ]
    return result


@app.post("/api/tasks/{task_id}/retry")
def retry_task(
    task_id: str, confirmation: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    previous = db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not previous:
        raise HTTPException(404, "任务不存在")
    if previous["status"] in {"queued", "running"}:
        raise HTTPException(409, "任务仍在运行，不能重复创建")
    payload = db.decode_json_fields(previous).get("payload", {})
    payload["cost_acknowledged"] = bool(confirmation.get("cost_acknowledged", False))
    try:
        if previous["type"] == "generate_batch":
            payload.pop("applied_concurrency", None)
            return create_generate_batch(GenerateBatchRequest(**payload))
        if previous["type"] == "augment_batch":
            payload.pop("applied_concurrency", None)
            return augment(AugmentRequest(**payload))
        if previous["type"] == "generate":
            if previous.get("parent_id"):
                child_payload = dict(payload)
                child_payload["texts"] = [child_payload.pop("text")]
                child_payload["concurrency"] = 1
                child_payload.pop("batch_mode", None)
                child_payload.pop("item_index", None)
                child_payload.pop("origin_job_id", None)
                return create_generate_batch(GenerateBatchRequest(**child_payload))
            return create_generate_task(GenerateRequest(**payload))
        if previous["type"] == "augment":
            return augment(AugmentRequest(**payload))
        if previous["type"] == "export":
            return create_export(ExportRequest(**payload))
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"旧任务参数已不兼容：{exc}") from exc
    raise HTTPException(400, "该任务类型不支持重试")


def _sample_filter(
    provider: str = "", voice_id: str = "", quality: str = "", split: str = "",
    gender: str = "", age_group: str = "", sample_type: str = "",
    search: str = "", keyword_id: str = "", target_kind: str = "",
    origin_job_id: str = "",
) -> tuple[str, list[Any]]:
    if target_kind:
        try:
            target_kind = _canonical_target_kind(target_kind)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    clauses, params = ["deleted_at IS NULL"], []
    for column, value in (("provider", provider), ("voice_id", voice_id),
                          ("split", split),
                          ("keyword_id", keyword_id), ("target_kind", target_kind),
                          ("origin_job_id", origin_job_id)):
        if value:
            clauses.append(f"{column}=?"); params.append(value)
    if quality:
        # Generation historically writes pass/reject while interactive review
        # writes accepted/rejected/pending. Filtering the UI's canonical value
        # literally hid otherwise identical existing samples. Match aliases
        # without rewriting provenance or the stored review decision.
        review_groups = {
            "pending": ("pending", "unchecked", "review"),
            "accepted": ("accepted", "pass"),
            "rejected": ("rejected", "reject"),
        }
        canonical = {"pass": "accepted", "reject": "rejected", "unchecked": "pending", "review": "pending"}.get(quality, quality)
        aliases = review_groups.get(canonical, (quality,))
        clauses.append("quality_status IN (" + ",".join("?" for _ in aliases) + ")")
        params.extend(aliases)
    if gender:
        clauses.append("json_extract(metadata_json, '$.gender')=?"); params.append(gender)
    if age_group:
        clauses.append("json_extract(metadata_json, '$.age_group')=?"); params.append(age_group)
    if sample_type == "augmented":
        clauses.append("parent_id IS NOT NULL")
    elif sample_type == "clean":
        clauses.append("parent_id IS NULL")
    elif sample_type:
        raise HTTPException(
            400,
            "sample_type 只支持 clean（原音）或 augmented（增强样本）；"
            "真人录音请使用 provider=real 筛选来源",
        )
    if search:
        clauses.append("(text LIKE ? OR voice_name LIKE ? OR id LIKE ? OR keyword_id LIKE ?)")
        params.extend((f"%{search}%", f"%{search}%", f"%{search}%", f"%{search}%"))
    return " AND ".join(clauses), params


@app.get("/api/samples")
def samples(
    provider: str = "", voice_id: str = "", quality: str = "", split: str = "",
    gender: str = "", age_group: str = "", sample_type: str = "",
    search: str = "", keyword_id: str = "", target_kind: str = "",
    origin_job_id: str = "",
    page: int = Query(1, ge=1), page_size: int = Query(24, ge=1, le=200),
) -> dict[str, Any]:
    where, params = _sample_filter(
        provider, voice_id, quality, split, gender, age_group, sample_type,
        search, keyword_id, target_kind, origin_job_id,
    )
    total = db.one(f"SELECT COUNT(*) AS n FROM samples WHERE {where}", tuple(params))["n"]
    page_params = [*params, page_size, (page - 1) * page_size]
    rows = db.all(
        f"SELECT * FROM samples WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        tuple(page_params),
    )
    return {"items": [_json_row(row) for row in rows], "total": total, "page": page, "page_size": page_size}


@app.get("/api/samples/ids")
def sample_ids(
    provider: str = "", voice_id: str = "", quality: str = "", split: str = "",
    gender: str = "", age_group: str = "", sample_type: str = "",
    search: str = "", keyword_id: str = "", target_kind: str = "",
    origin_job_id: str = "",
) -> dict[str, Any]:
    where, params = _sample_filter(
        provider, voice_id, quality, split, gender, age_group, sample_type,
        search, keyword_id, target_kind, origin_job_id,
    )
    rows = db.all(
        f"SELECT id FROM samples WHERE {where} ORDER BY created_at DESC", tuple(params)
    )
    return {"ids": [row["id"] for row in rows], "total": len(rows)}


@app.get("/api/samples/keywords")
def sample_keywords() -> list[dict[str, Any]]:
    rows = db.all(
        """
        SELECT keyword_id,
               COALESCE(
                   MAX(CASE WHEN target_kind='positive' THEN NULLIF(label, '') END),
                   MAX(CASE WHEN target_kind='negative' THEN NULLIF(label, '') END),
                   keyword_id
               ) AS label,
               COUNT(*) AS sample_count,
               SUM(CASE WHEN target_kind='positive' THEN 1 ELSE 0 END) AS positive_count,
               SUM(CASE WHEN target_kind='negative' THEN 1 ELSE 0 END) AS negative_count,
               SUM(CASE WHEN target_kind='background' THEN 1 ELSE 0 END) AS background_count
        FROM samples
        WHERE deleted_at IS NULL
        GROUP BY keyword_id
        ORDER BY keyword_id
        """
    )
    current = project_settings.get()
    for row in rows:
        is_current = row["keyword_id"] == current["keyword_id"]
        row["display_text"] = (
            current["display_text"] if is_current else row["label"] or row["keyword_id"]
        )
        row["display_text_unavailable"] = not is_current
    return rows


@app.get("/api/samples/speakers")
def sample_speakers() -> list[dict[str, Any]]:
    """List distinct speaker IDs already used by active real recordings."""
    return db.all(
        """
        SELECT voice_id AS speaker_id,
               COUNT(*) AS sample_count,
               SUM(CASE WHEN parent_id IS NULL THEN 1 ELSE 0 END) AS original_count,
               MAX(created_at) AS last_used_at
        FROM samples
        WHERE deleted_at IS NULL AND provider='real' AND TRIM(voice_id)!=''
        GROUP BY voice_id
        ORDER BY last_used_at DESC, voice_id
        """
    )


@app.post("/api/samples/upload")
async def upload_samples(
    files: list[UploadFile] = File(...), text: str | None = Form(None),
    label: str | None = Form(None), speaker_id: str = Form(...),
    keyword_id: str | None = Form(None),
    target_kind: str = Form("positive"),
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
    try:
        target_kind = _canonical_target_kind(target_kind)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
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
            if db.one(
                """
                SELECT id FROM samples
                WHERE sha256=? AND keyword_id=? AND target_kind=?
                  AND deleted_at IS NULL
                """,
                (digest, effective_keyword_id, target_kind),
            ):
                target.unlink(missing_ok=True)
                duplicates.append(upload.filename or sample_id)
                continue
            _insert_sample({
                "id": sample_id, "provider": "real", "voice_id": speaker_id,
                "voice_name": speaker_id, "model": "recording", "text": effective_text,
                "label": effective_label, "keyword_id": effective_keyword_id, "speed": 1.0,
                "target_kind": target_kind, "source_kind": "human", "variant_kind": "clean",
                "pitch": 1.0, "volume": 50, "seed": 0, "file_path": str(target),
                "duration": info["duration"], "sample_rate": info["sample_rate"],
                "channels": info["channels"], "sha256": digest, "split": split,
                "quality_status": "pending",
                "metadata": {
                    "speaker_id": speaker_id, "gender": gender, "age_group": age_group,
                    "original_filename": upload.filename or "",
                    "original_content_type": str(upload.content_type or "")[:255],
                    "source": "human_recording",
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
    allowed = {
        "text", "label", "keyword_id", "target_kind", "quality_status", "notes", "split",
    }
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
    if "target_kind" in values:
        try:
            values["target_kind"] = _canonical_target_kind(values["target_kind"])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    for key in ("text", "label", "keyword_id"):
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


def _materialize_noise_task(
    task_id: str, request: dict[str, Any], selected: list[dict[str, Any]],
) -> None:
    created: list[str] = []
    duplicates: list[str] = []
    failed = 0
    for noise in selected:
        existing = db.one(
            """
            SELECT id FROM samples
            WHERE deleted_at IS NULL AND provider='noise' AND voice_id=?
              AND keyword_id=? AND target_kind='background'
            """,
            (noise["id"], request["keyword_id"]),
        )
        if existing:
            duplicates.append(existing["id"])
            _update_task(task_id, completed=len(created) + len(duplicates), failed=failed)
            continue
        sample_id = uuid.uuid4().hex
        source = _safe_file(noise["file_path"])
        target = (
            SAMPLES_DIR / "noise" / _safe_component(request["keyword_id"], "keyword")
            / f"{sample_id}.wav"
        )
        try:
            if not source.is_file():
                raise AudioError("背景噪声文件丢失")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            metadata = db.decode_json_fields(dict(noise)).get("metadata", {})
            _insert_sample({
                "id": sample_id, "provider": "noise", "voice_id": noise["id"],
                "voice_name": noise["name"], "model": "materialized",
                "text": noise["name"], "label": "__noise__",
                "keyword_id": request["keyword_id"], "target_kind": "background",
                "source_kind": "noise", "variant_kind": "clean",
                "origin_task_id": task_id, "origin_job_id": task_id,
                "speed": 1.0, "pitch": 1.0, "volume": 50, "seed": 0,
                "file_path": str(target), "duration": noise["duration"],
                "sample_rate": noise["sample_rate"], "channels": 1,
                "sha256": noise["sha256"], "quality_status": "pass",
                "metadata": {
                    **metadata, "noise_id": noise["id"],
                    "noise_category": noise["category"], "materialized": True,
                },
            })
            created.append(sample_id)
        except (AudioError, OSError) as exc:
            target.unlink(missing_ok=True)
            failed += 1
            _update_task(task_id, message=str(exc)[:500])
        except Exception as exc:
            target.unlink(missing_ok=True)
            logger.error(
                "Unexpected noise materialization failure: task_id=%s noise_id=%s type=%s",
                task_id, noise["id"], type(exc).__name__,
            )
            raise RuntimeError("噪声物化发生内部错误，请查看服务器日志") from exc
        _update_task(task_id, completed=len(created) + len(duplicates), failed=failed)
    completed = len(created) + len(duplicates)
    status_value = "completed" if failed == 0 else ("partial" if completed else "failed")
    result = {
        "sample_ids": created, "duplicate_sample_ids": duplicates,
        **_local_cost_result(),
    }
    _update_task(
        task_id, status=status_value, completed=completed, failed=failed,
        message=f"已物化 {len(created)}，复用 {len(duplicates)}，失败 {failed}",
        result_json=result,
    )


@app.post("/api/noises/materialize")
def materialize_noises(request: NoiseMaterializeRequest) -> dict[str, Any]:
    unique_noise_ids = list(dict.fromkeys(request.noise_ids))
    placeholders = ",".join("?" for _ in unique_noise_ids)
    selected = db.all(
        f"SELECT * FROM noises WHERE deleted_at IS NULL AND enabled=1 "
        f"AND id IN ({placeholders}) ORDER BY created_at",
        tuple(unique_noise_ids),
    )
    if len(selected) != len(unique_noise_ids):
        raise HTTPException(400, "选择中包含不存在、已删除或已禁用的背景噪声")
    task_id = _create_task(
        "materialize_noise", len(selected), request.model_dump(),
        result=_local_cost_result(),
    )
    _execute_task(task_id, _materialize_noise_task, request.model_dump(), selected)
    task_row = _json_row(db.one("SELECT * FROM tasks WHERE id=?", (task_id,)))
    task_result = task_row.get("result", {})
    if task_result.get("internal_error"):
        remaining = max(0, int(task_row["total"]) - int(task_row["completed"]))
        if remaining > int(task_row["failed"]):
            _update_task(task_id, failed=remaining)
        raise HTTPException(
            500,
            {
                "message": "噪声物化失败，请查看任务详情和服务器日志",
                "task_id": task_id,
            },
        )
    return {
        "id": task_id, "task_id": task_id, "status": task_row["status"],
        "created": len(task_result.get("sample_ids", [])),
        "sample_ids": task_result.get("sample_ids", []),
        "duplicates": task_result.get("duplicate_sample_ids", []),
        "failed": task_row["failed"],
    }


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


def _select_explicit_samples(
    sample_ids: list[str], keyword_id: str, operation: str,
) -> list[dict[str, Any]]:
    """Resolve an explicit selection without silently dropping invalid IDs."""

    unique_ids = list(dict.fromkeys(str(sample_id).strip() for sample_id in sample_ids))
    placeholders = ",".join("?" for _ in unique_ids)
    selected = db.all(
        f"SELECT * FROM samples WHERE deleted_at IS NULL AND id IN ({placeholders})",
        tuple(unique_ids),
    )
    if len(selected) != len(unique_ids):
        raise HTTPException(
            400,
            f"{operation}选择中包含不存在或已删除的样本；请刷新样本列表后重试",
        )
    selected_keyword_ids = {
        str(sample.get("keyword_id") or "").strip() for sample in selected
    }
    if len(selected_keyword_ids) != 1:
        raise HTTPException(400, f"{operation}选择必须属于同一个 keyword_id")
    expected_keyword_id = keyword_id.strip()
    if expected_keyword_id:
        mismatched = sorted({
            str(sample.get("keyword_id") or "")
            for sample in selected
            if str(sample.get("keyword_id") or "") != expected_keyword_id
        })
        if mismatched:
            raise HTTPException(
                400,
                f"{operation}选择包含其他关键词样本；keyword_id 必须全部为 "
                f"{expected_keyword_id}",
            )
    return selected


def _export_project_metadata(
    samples: list[dict[str, Any]], requested_keyword_id: str,
) -> dict[str, Any]:
    """Describe the selected keyword instead of blindly using current settings."""

    keyword_ids = {
        str(sample.get("keyword_id") or "").strip() for sample in samples
    }
    if len(keyword_ids) != 1:
        raise HTTPException(400, "导出样本必须属于同一个 keyword_id")
    keyword_id = next(iter(keyword_ids))
    if not keyword_id:
        raise HTTPException(400, "导出样本缺少 keyword_id")
    if requested_keyword_id.strip() and requested_keyword_id.strip() != keyword_id:
        raise HTTPException(400, "导出样本与请求的 keyword_id 不一致")

    preferred = [
        sample for sample in samples if sample.get("target_kind") == "positive"
    ] or [
        sample for sample in samples if sample.get("target_kind") != "background"
    ]
    labels = [
        str(sample.get("label") or "").strip() for sample in preferred
        if str(sample.get("label") or "").strip()
    ]
    representative_label = (
        sorted(set(labels), key=lambda value: (-labels.count(value), value))[0]
        if labels else keyword_id
    )
    current = project_settings.get()
    is_current = current.get("keyword_id") == keyword_id
    return {
        "display_text": current["display_text"] if is_current else representative_label,
        "label": representative_label,
        "keyword_id": keyword_id,
        "display_text_unavailable": not is_current,
    }


@app.post("/api/augment")
def augment(request: AugmentRequest) -> dict[str, Any]:
    if request.sample_ids:
        selected = _select_explicit_samples(
            request.sample_ids, request.keyword_id, "增强"
        )
    elif request.keyword_id.strip():
        selected = db.all(
            """
            SELECT * FROM samples
            WHERE deleted_at IS NULL AND parent_id IS NULL AND keyword_id=?
              AND target_kind!='background'
            """,
            (request.keyword_id.strip(),),
        )
    else:
        raise HTTPException(400, "未显式选择样本时必须提供 keyword_id")
    if not selected:
        raise HTTPException(400, "没有可增强的样本")
    if any(sample.get("target_kind") == "background" for sample in selected):
        raise HTTPException(400, "背景噪声样本不能再次执行噪声增强")
    if len(selected) * request.count > MAX_TASK_ITEMS:
        raise HTTPException(400, f"单个任务最多 {MAX_TASK_ITEMS} 个增强项，请拆分任务")
    selected = sorted(selected, key=lambda sample: sample["id"])
    payload = request.model_dump()
    applied_concurrency = min(WORKER_LIMIT, len(selected))
    parent_result = {
        **_local_cost_result(),
        "applied_concurrency": applied_concurrency,
    }
    parent_id = _create_task(
        "augment_batch", len(selected) * request.count,
        {**payload, "applied_concurrency": applied_concurrency},
        result=parent_result,
    )
    child_specs: list[tuple[str, dict[str, Any], list[dict[str, Any]]]] = []
    child_ids: list[str] = []
    for item_index, sample in enumerate(selected):
        child_payload = {
            **payload,
            "sample_ids": [sample["id"]],
            "seed": (request.seed + item_index * request.count) % (2**31),
            "item_index": item_index,
            "source_sample_id": sample["id"],
            "text": sample.get("text") or sample["id"],
            "origin_job_id": parent_id,
        }
        child_id = _create_task(
            "augment", request.count, child_payload,
            parent_id=parent_id, item_index=item_index,
            result=_local_cost_result(),
        )
        child_ids.append(child_id)
        child_specs.append((child_id, child_payload, [sample]))
    parent_result["child_ids"] = child_ids
    _update_task(parent_id, result_json=parent_result)
    threading.Thread(
        target=_run_augment_batch,
        args=(parent_id, child_specs, applied_concurrency),
        name=f"augment-batch-{parent_id[:8]}",
        daemon=True,
    ).start()
    return {
        "id": parent_id,
        "task_id": parent_id,
        "parent_id": parent_id,
        "child_ids": child_ids,
        "applied_concurrency": applied_concurrency,
    }


@app.post("/api/exports")
def create_export(request: ExportRequest) -> dict[str, str]:
    if request.train_ratio + request.dev_ratio >= 1:
        raise HTTPException(400, "train_ratio + dev_ratio 必须小于 1")
    if request.sample_ids:
        selected = _select_explicit_samples(
            request.sample_ids, request.keyword_id, "导出"
        )
    elif request.keyword_id.strip():
        selected = db.all(
            "SELECT * FROM samples WHERE deleted_at IS NULL AND keyword_id=? ORDER BY created_at",
            (request.keyword_id.strip(),),
        )
    else:
        raise HTTPException(400, "未显式选择样本时必须提供 keyword_id")
    if not selected:
        raise HTTPException(400, "没有可导出的样本")
    _explicit_split_map(selected)
    payload = request.model_dump()
    payload["keyword_id"] = str(selected[0]["keyword_id"])
    payload["project"] = _export_project_metadata(selected, request.keyword_id)
    task_id = _create_task(
        "export", len(selected), payload, result=_local_cost_result()
    )
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
