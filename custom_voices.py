"""Persistent custom voice lifecycle, confined assets and asynchronous cloud jobs."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import uuid
from pathlib import Path
from typing import Literal

from fastapi import File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field, ValidationError

from audio_utils import AudioError, normalize_wav
from db import utc_now
from providers import ProviderError, ProviderRequestError, _decode_base64_audio
from custom_voice_providers import ALI_MODELS, VOLC_MODELS, create_voice, query_voice, download_preview, validate_options


class Creation(BaseModel):
    request_id: uuid.UUID
    name: str = Field(min_length=1, max_length=80)
    service: Literal["volcengine_v1", "volcengine_v2", "xiaomi", "aliyun", "mock"]
    mode: Literal["clone", "design"]
    model: str = Field("", max_length=100)
    speaker_id: str = Field("", max_length=150)
    speaker_group: str = Field("", max_length=120)
    prompt: str = Field("", max_length=500)
    preview_text: str = Field("你好，很高兴认识你。欢迎体验我的声音。", min_length=1, max_length=200)
    reference_url: str = Field("", max_length=4000)
    language: Literal["zh", "en"] = "zh"
    tags: str = Field("", max_length=200)
    cost_acknowledged: bool = False


class Edit(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    tags: str = Field("", max_length=200)
    enabled: bool = True


class CustomVoiceManager:
    def __init__(self, db, root, settings, create_task, submit, update_task):
        self.db, self.root, self.settings = db, Path(root), settings
        self.assets = self.root / "custom_voices"
        self.assets.mkdir(exist_ok=True)
        self.mutations = threading.RLock()
        self.create_task, self.submit, self.update_task = create_task, submit, update_task
        db.execute("""CREATE TABLE IF NOT EXISTS custom_voices (
            id TEXT PRIMARY KEY, voice_db_id INTEGER REFERENCES voices(id), service TEXT NOT NULL,
            mode TEXT NOT NULL, name TEXT NOT NULL, model TEXT NOT NULL, remote_voice_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL, busy INTEGER NOT NULL DEFAULT 0, desired_enabled INTEGER NOT NULL DEFAULT 1,
            reference_rel TEXT NOT NULL DEFAULT '', preview_rel TEXT NOT NULL DEFAULT '',
            speaker_group TEXT NOT NULL, language TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '',
            prompt TEXT NOT NULL DEFAULT '', preview_text TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '', remaining INTEGER, scope_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        # A process cannot resume an in-flight mutation. Keep its ID for status
        # queries; never reinterpret a timeout/restart as permission to retry.
        db.execute("UPDATE voices SET enabled=0 WHERE id IN (SELECT voice_db_id FROM custom_voices WHERE busy=1)")
        db.execute("UPDATE custom_voices SET busy=0,status='unknown',error='服务重启，结果未知；请先刷新状态，不要盲目重建' WHERE busy=1")

    def get(self, identifier):
        value = self.db.one("SELECT * FROM custom_voices WHERE id=?", (identifier,))
        if not value:
            raise HTTPException(404, "音色不存在")
        return value

    def account_scope(self, service):
        if service != "aliyun":
            return {}
        config = self.settings.get_provider("aliyun")
        return {"region": config.get("region") or "cn-beijing", "workspace_id": config.get("workspace_id") or ""}

    def check_scope(self, value):
        if value["service"] == "aliyun" and json.loads(value["scope_json"]) != self.account_scope("aliyun"):
            raise ProviderRequestError("此音色绑定的阿里云地域 / Workspace 与当前设置不同，请恢复创建时的设置")

    def validate_synthesis(self, voice):
        if voice.get("source") != "managed_custom":
            return
        value = self.db.one("SELECT * FROM custom_voices WHERE voice_db_id=?", (voice["id"],))
        if not value or value["status"] != "ready" or not value["desired_enabled"]:
            raise ProviderRequestError("自定义音色尚不可用，请在我的音色中检查状态")
        self.check_scope(value)
        if voice["provider"] == "xiaomi":
            self.asset(value["reference_rel"])

    def public(self, value):
        return {key: value.get(key) for key in ("id", "voice_db_id", "service", "mode", "name", "model", "remote_voice_id", "status", "busy", "desired_enabled", "speaker_group", "language", "tags", "prompt", "preview_text", "error", "remaining", "created_at")} | {"has_preview": bool(value["preview_rel"])}

    def adopt_catalog(self):
        with self.mutations:
            return self._adopt_catalog()

    def _adopt_catalog(self):
        for row in self.db.all("SELECT * FROM voices WHERE source IN ('account_custom','account_clone') AND id NOT IN (SELECT voice_db_id FROM custom_voices WHERE voice_db_id IS NOT NULL)"):
            metadata = self.db.decode_json_fields(dict(row)).get("metadata", {})
            service = "volcengine_v2" if row["model"] == "seed-icl-2.0" else "volcengine_v1" if row["provider"] == "volcengine" else row["provider"]
            if service not in {*VOLC_MODELS, "aliyun"}:
                continue
            if service == "aliyun" and row["model"] not in ALI_MODELS:
                continue  # Realtime Qwen voices require a different synthesizer.
            identifier, now = uuid.uuid4().hex, utc_now()
            group = metadata.get("speaker_id") or ("volc_clone_"+row["voice_id"] if row["provider"] == "volcengine" else "tts_"+"_".join((row["provider"], row["voice_id"], row["model"])))
            self.db.execute("""INSERT INTO custom_voices (id,voice_db_id,service,mode,name,model,remote_voice_id,status,desired_enabled,speaker_group,language,created_at,updated_at)
                VALUES(?,?,?,'imported',?,?,?,?,?,?,?, ?,?)""",
                (identifier, row["id"], service, row["name"], row["model"], row["voice_id"], "ready" if row["enabled"] else "unknown", row["enabled"], group, "zh", now, now))
            self.sync_voice(identifier)
            self.db.execute("UPDATE custom_voices SET scope_json=? WHERE id=?", (json.dumps(self.account_scope(service)), identifier))

    def asset(self, relative):
        path = (self.root / relative).resolve()
        if not relative or not path.is_relative_to(self.assets.resolve()) or not path.is_file():
            raise HTTPException(404, "音色音频不可用")
        return path

    def create(self, value, reference_bytes):
        # Concurrent ASGI commands must not reserve the same slot twice. This
        # covers local preparation only; the cloud worker never holds this lock.
        with self.mutations:
            return self._create(value, reference_bytes)

    def _create(self, value, reference_bytes):
        data = value.model_dump(mode="json")
        data.update(id=uuid.UUID(data["request_id"]).hex, name=data["name"].strip())
        validate_options(data, self.settings)
        if data["service"] != "mock" and not data["cost_acknowledged"]:
            raise HTTPException(400, "创建与预览可能产生云费用，请先确认")
        existing = self.db.one("SELECT * FROM custom_voices WHERE id=?", (data["id"],))
        if existing:
            return {"voice": self.public(existing), "task_id": None}
        if data["service"] in VOLC_MODELS and self.db.one("SELECT id FROM custom_voices WHERE remote_voice_id=? AND service LIKE 'volcengine%'", (data["speaker_id"],)):
            raise HTTPException(409, "此 Speaker ID 已在管理列表，不能重复训练覆盖；请先检查原音色")
        reference_rel = ""
        directory = self.assets / data["id"]
        directory.mkdir(exist_ok=False)
        try:
            if data["mode"] == "clone" and data["service"] != "aliyun":
                if not reference_bytes or len(reference_bytes) > 10*1024*1024:
                    raise ValueError("请选择不超过 10 MB 的参考音频")
                source = directory / "upload.audio"
                source.write_bytes(reference_bytes)
                try:
                    reference = directory / "reference.wav"
                    info = normalize_wav(source, reference, 24000)
                    if not 3 <= info["duration"] <= 60:
                        raise ValueError("参考音频时长需为 3–60 秒，建议清晰单人录音 10–20 秒")
                    if reference.stat().st_size*4/3 > 10*1024*1024:
                        raise ValueError("编码后的参考音频超过供应商 10 MB 限制")
                    reference_rel = reference.relative_to(self.root).as_posix()
                    # Same source bytes across vendor clones belong to one
                    # speaker. A user-provided group also connects recordings
                    # and different excerpts of the same real person.
                    digest = hashlib.sha256(reference_bytes).hexdigest()
                    source_sample = self.db.one("SELECT * FROM samples WHERE sha256=? AND deleted_at IS NULL ORDER BY created_at LIMIT 1", (digest,))
                    inferred_group = "reference_"+digest
                    if source_sample:
                        info = self.db.decode_json_fields(dict(source_sample)).get("metadata", {})
                        inferred_group = info.get("speaker_id") or (source_sample["voice_id"] if source_sample["provider"] == "real" else "tts_"+"_".join((source_sample["provider"], source_sample["voice_id"], source_sample["model"])).rstrip("_"))
                        if not info.get("speaker_id") and source_sample["provider"] == "volcengine" and source_sample["model"] in {"seed-icl-1.0", "seed-icl-2.0", "voice-clone-v1"}:
                            inferred_group = "volc_clone_"+source_sample["voice_id"]
                    data["speaker_group"] = data["speaker_group"].strip() or inferred_group
                finally:
                    source.unlink(missing_ok=True)
            data["speaker_group"] = data["speaker_group"].strip() or "custom_"+data["id"]
            model = VOLC_MODELS.get(data["service"], "mimo-v2.5-tts-voiceclone" if data["service"] == "xiaomi" else data["model"] or "mock")
            remote = data["speaker_id"] if data["service"] in VOLC_MODELS else ""
            now = utc_now()
            self.db.execute("""INSERT INTO custom_voices
                (id,service,mode,name,model,remote_voice_id,status,busy,reference_rel,speaker_group,language,tags,prompt,preview_text,created_at,updated_at)
                VALUES(?,?,?,?,?,?,'creating',1,?,?,?,?,?,?,?,?)""",
                (data["id"], data["service"], data["mode"], data["name"], model, remote, reference_rel, data["speaker_group"], data["language"], data["tags"], data["prompt"], data["preview_text"], now, now))
            self.db.execute("UPDATE custom_voices SET scope_json=? WHERE id=?", (json.dumps(self.account_scope(data["service"])), data["id"]))
        except Exception:
            for file in directory.iterdir():
                file.unlink()
            directory.rmdir()
            raise
        # The task snapshot deliberately excludes input URLs and reference
        # bytes. A cloud failure cannot leak signed URLs into technical details.
        task = self.create_task("custom_voice", 1, {"custom_voice_id": data["id"], "mode": data["mode"], "service": data["service"]})
        self.submit(task, self.run, data["id"], data, False)
        return {"voice": self.public(self.get(data["id"])), "task_id": task}

    def refresh(self, identifier):
        value = self.get(identifier)
        try:
            self.check_scope(value)
        except ProviderError as exc:
            raise HTTPException(400, str(exc)) from exc
        if value["busy"] or value["status"] == "removed":
            raise HTTPException(409, "音色正在处理或已移除")
        if not value["remote_voice_id"]:
            raise HTTPException(400, "没有远端音色 ID；请先在供应商控制台检查创建结果")
        self.db.execute("UPDATE custom_voices SET busy=1 WHERE id=?", (identifier,))
        task = self.create_task("custom_voice_status", 1, {"custom_voice_id": identifier})
        self.submit(task, self.run, identifier, None, True)
        return {"task_id": task}

    def run(self, task_id, identifier, creation, querying):
        value = self.get(identifier)
        try:
            reference = self.asset(value["reference_rel"]) if value["reference_rel"] else None
            result = query_voice(value, self.settings) if querying else create_voice(creation, reference, self.settings)
            # Persist the cloud identity BEFORE decoding/downloading a preview.
            # Preview failure must not throw away a successfully created slot.
            self.db.execute("UPDATE custom_voices SET remote_voice_id=?,status=?,error='',remaining=?,updated_at=? WHERE id=?",
                            (result["voice_id"], result["status"], result.get("remaining"), utc_now(), identifier))
            preview = result.get("preview")
            preview_error = ""
            try:
                if not preview and result.get("preview_base64"):
                    preview = _decode_base64_audio(result["preview_base64"], "音色预览")
                if not preview and result.get("preview_url"):
                    preview = download_preview(result["preview_url"])
                if preview:
                    directory = self.assets / identifier
                    directory.mkdir(exist_ok=True)
                    raw, target = directory / "preview.audio", directory / "preview.wav"
                    raw.write_bytes(preview)
                    try:
                        normalize_wav(raw, target, 24000)
                        relative = target.relative_to(self.root).as_posix()
                        self.db.execute("UPDATE custom_voices SET preview_rel=? WHERE id=?", (relative, identifier))
                        if value["service"] == "xiaomi" and value["mode"] == "design":
                            # Reuse the accepted identity through voiceclone;
                            # replaying the design prompt produces a new voice.
                            self.db.execute("UPDATE custom_voices SET reference_rel=? WHERE id=?", (relative, identifier))
                    finally:
                        raw.unlink(missing_ok=True)
            except (ProviderError, AudioError, OSError):
                preview_error = "音色状态已保存；预览下载或解码未完成"
                if value["service"] == "xiaomi" and value["mode"] == "design":
                    self.db.execute("UPDATE custom_voices SET status='unknown' WHERE id=?", (identifier,))
            self.sync_voice(identifier)
            self.db.execute("UPDATE custom_voices SET busy=0,error=? WHERE id=?", (preview_error, identifier))
            self.update_task(task_id, status="completed", completed=1, message="音色状态已更新", result_json={"custom_voice_id": identifier, "state": result["status"]})
        except Exception as exc:
            # Unexpected exceptions may contain reference Base64/URL/input.
            # Do not send exception text or traceback to the public task log.
            http = re.search(r"HTTP ([1-5][0-9]{2})", str(exc)) if isinstance(exc, ProviderError) else None
            message = "音色操作未完成" + (f"（HTTP {http[1]}）" if http else "") + "；请检查设置与供应商控制台，确认结果后再操作"
            if not querying:
                self.db.execute("UPDATE custom_voices SET status='unknown' WHERE id=?", (identifier,))
            self.db.execute("UPDATE custom_voices SET busy=0,error=?,updated_at=? WHERE id=?", (message, utc_now(), identifier))
            self.update_task(task_id, status="failed", failed=1, message=message, result_json={"custom_voice_id": identifier, "result_unknown": not querying})

    def sync_voice(self, identifier):
        value = self.get(identifier)
        provider = "volcengine" if value["service"] in VOLC_MODELS else value["service"]
        if not value["remote_voice_id"]:
            return
        previous = self.db.one("SELECT * FROM voices WHERE id=?", (value["voice_db_id"],)) if value["voice_db_id"] else None
        metadata = self.db.decode_json_fields(dict(previous)).get("metadata", {}) if previous else {}
        metadata.update(custom_voice_id=identifier, speaker_id=value["speaker_group"], tags=value["tags"])
        voice = {"provider": provider, "voice_id": value["remote_voice_id"], "model": value["model"], "name": value["name"],
                 "languages": self.db.decode_json_fields(dict(previous))["languages"] if previous else [value["language"]],
                 "gender": previous["gender"] if previous else "unknown", "age_group": previous["age_group"] if previous else "unknown",
                 "source": "managed_custom", "enabled": value["status"] == "ready" and bool(value["desired_enabled"]), "metadata": metadata}
        if provider == "xiaomi" and not value["reference_rel"]:
            voice["enabled"] = False
        voice_id = self.db.upsert_voice(voice)
        self.db.execute("UPDATE custom_voices SET voice_db_id=? WHERE id=?", (voice_id, identifier))

    def synth_voice(self, voice):
        self.validate_synthesis(voice)
        if voice.get("source") != "managed_custom" or voice["provider"] != "xiaomi":
            return voice["voice_id"]
        value = self.db.one("SELECT * FROM custom_voices WHERE voice_db_id=? AND status='ready'", (voice["id"],))
        if not value:
            raise ProviderRequestError("自定义音色不可用，请先刷新状态")
        path = self.asset(value["reference_rel"])
        limit = 10*1024*1024*3//4
        if path.stat().st_size > limit:
            raise ProviderRequestError("参考音频超过 10 MB Base64 限制")
        with path.open("rb") as source:
            data = source.read(limit+1)
        if len(data) > limit:
            raise ProviderRequestError("参考音频超过 10 MB Base64 限制")
        return "data:audio/wav;base64,"+base64.b64encode(data).decode("ascii")

    def edit(self, identifier, value):
        old = self.get(identifier)
        if old["busy"] or old["status"] == "removed":
            raise HTTPException(409, "音色正在处理或已移除")
        if not value.name.strip():
            raise HTTPException(400, "名称不能为空")
        self.db.execute("UPDATE custom_voices SET name=?,tags=?,desired_enabled=?,updated_at=? WHERE id=?",
                        (value.name.strip(), value.tags.strip(), int(value.enabled), utc_now(), identifier))
        self.sync_voice(identifier)
        return self.public(self.get(identifier))

    def remove(self, identifier):
        value = self.get(identifier)
        if value["busy"]:
            raise HTTPException(409, "音色正在处理，请稍后操作")
        self.db.execute("UPDATE custom_voices SET status='removed',desired_enabled=0 WHERE id=?", (identifier,))
        if value["voice_db_id"]:
            self.db.execute("UPDATE voices SET enabled=0 WHERE id=?", (value["voice_db_id"],))
        return {"ok": True}

    def restore(self, identifier):
        value = self.get(identifier)
        if value["status"] != "removed" or value["busy"]:
            raise HTTPException(409, "此音色不需要恢复或正在处理")
        local_ready = value["service"] in {"mock", "xiaomi"} and value["voice_db_id"] and value["remote_voice_id"]
        status = "ready" if local_ready else "unknown"
        # Restoring a local row never creates/retrains a cloud voice. Cloud
        # voices must be explicitly queried before enabling them again.
        self.db.execute("UPDATE custom_voices SET status=?,desired_enabled=1,error='',updated_at=? WHERE id=?", (status, utc_now(), identifier))
        self.sync_voice(identifier)
        return self.public(self.get(identifier))


def install_routes(app, db, root, settings, create_task, submit, update_task):
    manager = CustomVoiceManager(db, root, settings, create_task, submit, update_task)

    @app.get("/api/custom-voices")
    def custom_voices(include_removed: bool = False):
        manager.adopt_catalog()
        return [manager.public(row) for row in db.all("SELECT * FROM custom_voices"+("" if include_removed else " WHERE status!='removed'")+" ORDER BY created_at DESC")]

    @app.post("/api/custom-voices")
    def create(options: str = Form(...), audio: UploadFile | None = File(None)):
        try:
            value = Creation.model_validate_json(options)
            reference = audio.file.read(10*1024*1024+1) if audio else None
            return manager.create(value, reference)
        except (ValueError, AudioError) as exc:
            # ValidationError includes input values; never echo its raw dump.
            detail = "创建参数格式无效" if isinstance(exc, ValidationError) else str(exc)
            raise HTTPException(400, detail) from exc

    @app.patch("/api/custom-voices/{identifier}")
    def edit(identifier: str, value: Edit):
        return manager.edit(identifier, value)

    @app.delete("/api/custom-voices/{identifier}")
    def remove(identifier: str):
        return manager.remove(identifier)

    @app.post("/api/custom-voices/{identifier}/refresh")
    def refresh(identifier: str):
        return manager.refresh(identifier)

    @app.post("/api/custom-voices/{identifier}/restore")
    def restore(identifier: str):
        return manager.restore(identifier)

    return manager
