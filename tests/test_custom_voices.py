"""Offline lifecycle and vendor wire contracts; never use real cloud accounts."""
import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
import time
import uuid
import wave

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import custom_voice_providers as protocols
import providers
from custom_voices import Creation, CustomVoiceManager, Edit, install_routes
from db import Database
from desktop.data_transfer import copy_dataset
from audio_utils import AudioError


class Settings:
    def get_provider(self, service):
        return {"api_key": "offline-key-"+service, "region": "cn-beijing"}


def audio(seconds=4):
    result = io.BytesIO()
    with wave.open(result, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\x01\x00\xff\xff"*int(12000*seconds))
    return result.getvalue()


def options(**changes):
    return Creation(request_id=uuid.uuid4(), name="测试音色", service="mock", mode="design", prompt="自然温暖的声音", **changes)


@pytest.fixture
def manager(tmp_path):
    db = Database(tmp_path / "tts_dataset.sqlite3")
    pool, tasks = ThreadPoolExecutor(2), {}
    def create(kind, count, payload):
        identifier = uuid.uuid4().hex
        tasks[identifier] = {"type": kind, "payload": payload}
        return identifier
    def update(identifier, **fields):
        tasks[identifier].update(fields)
    manager = CustomVoiceManager(db, tmp_path, Settings(), create, lambda task, fn, *args: pool.submit(fn, task, *args), update)
    manager.test_tasks = tasks
    yield manager
    pool.shutdown(wait=True)


def wait(manager, identifier):
    deadline = time.monotonic()+15
    while time.monotonic() < deadline:
        value = manager.get(identifier)
        if not value["busy"]:
            return value
        time.sleep(.02)
    pytest.fail("custom task did not complete")


def test_lifecycle_idempotency_edit_archive_and_asset_move(manager, tmp_path):
    value = options()
    first = manager.create(value, None)
    identifier = first["voice"]["id"]
    ready = wait(manager, identifier)
    assert ready["status"] == "ready" and ready["voice_db_id"]
    assert manager.asset(ready["preview_rel"]).is_file()
    assert manager.create(value, None)["task_id"] is None
    assert len(manager.test_tasks) == 1
    manager.edit(identifier, Edit(name="已改名", tags="温暖,中文", enabled=False))
    row = manager.db.one("SELECT * FROM voices WHERE id=?", (ready["voice_db_id"],))
    assert row["name"] == "已改名" and not row["enabled"]
    destination = tmp_path.parent / (tmp_path.name+"-moved")
    copy_dataset(tmp_path, destination)
    assert (destination / ready["preview_rel"]).read_bytes() == manager.asset(ready["preview_rel"]).read_bytes()
    manager.remove(identifier)
    assert manager.get(identifier)["status"] == "removed"
    assert manager.asset(ready["preview_rel"]).is_file()
    manager.restore(identifier)
    assert manager.get(identifier)["status"] == "ready"
    assert manager.db.one("SELECT enabled FROM voices WHERE id=?", (ready["voice_db_id"],))["enabled"]


def test_invalid_reference_sends_no_request_and_leaves_no_draft(manager, monkeypatch):
    calls = []
    monkeypatch.setattr("custom_voices.create_voice", lambda *args: calls.append(args))
    value = options().model_copy(update={"mode": "clone"})
    with pytest.raises((ValueError, AudioError)):
        manager.create(value, b"not audio")
    assert not calls and not manager.db.all("SELECT * FROM custom_voices")
    assert not list(manager.assets.iterdir())


@pytest.mark.parametrize("service", ["volcengine_v1", "volcengine_v2", "xiaomi", "aliyun"])
@pytest.mark.parametrize("mode", ["clone", "design"])
def test_creation_protocols_are_bound_and_never_retry(tmp_path, monkeypatch, service, mode):
    calls = []
    reference = tmp_path / "ref.wav"
    reference.write_bytes(audio())
    def request(method, url, **kwargs):
        calls.append((url, kwargs))
        if "xiaomimimo" in url:
            response = {"choices": [{"finish_reason": "stop", "message": {"audio": {"data": base64.b64encode(audio()).decode()}}}]}
        elif "bytedance" in url:
            response = {"speaker_id": "S_offline", "status": 2, "available_training_times": 14}
        else:
            response = {"output": {"voice_id": "cosyvoice-v3.5-plus-offline", "preview_audio": {"data": base64.b64encode(audio()).decode()}}}
        return httpx.Response(200, json=response, request=httpx.Request(method, url))
    monkeypatch.setattr(protocols, "_request", request)
    monkeypatch.setattr(providers, "_request", request)
    value = options().model_dump(mode="json") | {"service": service, "mode": mode, "model": "cosyvoice-v3.5-plus", "speaker_id": "S_offline", "reference_url": "https://audio.example/reference.wav", "id": uuid.uuid4().hex}
    protocols.validate_options(value, Settings())
    result = protocols.create_voice(value, reference, Settings())
    assert result["voice_id"] and len(calls) == 1
    url, kwargs = calls[0]
    assert kwargs["retry_transient"] is False
    body = kwargs["json_body"]
    if service.startswith("volcengine"):
        assert url.endswith("voice_clone" if mode == "clone" else "voice_design")
        assert kwargs["headers"]["X-Api-Key"] == "offline-key-"+("volcengine" if service.endswith("v1") else "volcengine_v2")
        assert body["speaker_id"] == "S_offline" and body["language"] == 0
        assert ("audio" in body) == (mode == "clone")
    elif service == "xiaomi":
        assert body["model"] == "mimo-v2.5-tts-voice"+("clone" if mode == "clone" else "design")
        assert body["messages"][-1]["role"] == "assistant"
        assert ("voice" in body["audio"]) == (mode == "clone")
    else:
        assert body["model"] == "voice-enrollment" and body["input"]["action"] == "create_voice"
        assert body["input"]["target_model"] == "cosyvoice-v3.5-plus"
        assert ("url" in body["input"]) == (mode == "clone")


def test_mimo_design_preserves_reference_for_future_clone(manager, monkeypatch):
    monkeypatch.setattr("custom_voices.create_voice", lambda *args: {"voice_id": "local-mimo", "status": "ready", "preview": audio()})
    value = options().model_copy(update={"service": "xiaomi", "cost_acknowledged": True})
    ready = wait(manager, manager.create(value, None)["voice"]["id"])
    assert ready["reference_rel"] == ready["preview_rel"]
    voice = manager.db.one("SELECT * FROM voices WHERE id=?", (ready["voice_db_id"],))
    assert voice["model"] == "mimo-v2.5-tts-voiceclone"
    encoded = manager.synth_voice(voice)
    assert encoded.startswith("data:audio/wav;base64,")
    assert base64.b64decode(encoded.split(",", 1)[1]).startswith(b"RIFF")
    assert "base64" not in json.dumps(manager.public(ready))


def test_cloud_identity_survives_bad_preview_and_query_preserves_disable(manager, monkeypatch):
    monkeypatch.setattr("custom_voices.create_voice", lambda *args: {"voice_id": "cosyvoice-v3.5-plus-created", "status": "pending", "preview_base64": "bad!"})
    value = options().model_copy(update={"service": "aliyun", "model": "cosyvoice-v3.5-plus", "cost_acknowledged": True})
    ready = wait(manager, manager.create(value, None)["voice"]["id"])
    assert ready["remote_voice_id"] == "cosyvoice-v3.5-plus-created" and ready["error"]
    manager.edit(ready["id"], Edit(name="保留名字", enabled=False))
    monkeypatch.setattr("custom_voices.query_voice", lambda *args: {"voice_id": ready["remote_voice_id"], "status": "ready"})
    manager.refresh(ready["id"])
    ready = wait(manager, ready["id"])
    voice = manager.db.one("SELECT * FROM voices WHERE id=?", (ready["voice_db_id"],))
    assert voice["name"] == "保留名字" and not voice["enabled"]


def test_failures_and_restart_do_not_retry_or_expose_inputs(manager, monkeypatch):
    calls = []
    def fail(*args):
        calls.append(1)
        raise providers.ProviderRequestError("HTTP 500 data:audio/wav;base64,PRIVATE signature=secret")
    monkeypatch.setattr("custom_voices.create_voice", fail)
    value = options()
    ready = wait(manager, manager.create(value, None)["voice"]["id"])
    assert ready["status"] == "unknown" and len(calls) == 1
    assert "PRIVATE" not in json.dumps(manager.test_tasks) and "secret" not in ready["error"]
    manager.db.execute("UPDATE custom_voices SET busy=1 WHERE id=?", (ready["id"],))
    restarted = CustomVoiceManager(manager.db, manager.root, manager.settings, manager.create_task, manager.submit, manager.update_task)
    assert restarted.get(ready["id"])["status"] == "unknown" and not restarted.get(ready["id"])["busy"]
    assert len(calls) == 1


def test_reference_identity_shared_across_vendors(manager, monkeypatch):
    monkeypatch.setattr("custom_voices.create_voice", lambda value, *args: {"voice_id": value["id"], "status": "ready", "preview": audio()})
    value = options().model_copy(update={"mode": "clone"})
    first = wait(manager, manager.create(value, audio())["voice"]["id"])
    second = wait(manager, manager.create(value.model_copy(update={"request_id": uuid.uuid4(), "service": "xiaomi", "cost_acknowledged": True}), audio())["voice"]["id"])
    assert first["speaker_group"] == second["speaker_group"]


def test_endpoint_validation_never_dumps_input_secrets(manager):
    application = FastAPI()
    install_routes(application, manager.db, manager.root, manager.settings, manager.create_task, manager.submit, manager.update_task)
    with TestClient(application) as client:
        response = client.post("/api/custom-voices", data={"options": '{"secret":"PRIVATE"}'})
        assert response.status_code == 400 and "PRIVATE" not in response.text
        value = options().model_copy(update={"service": "xiaomi"})
        response = client.post("/api/custom-voices", data={"options": value.model_dump_json()})
        assert response.status_code == 400 and not manager.test_tasks


def test_catalog_adoption_keeps_identity_and_local_names(manager):
    voice_id = manager.db.upsert_voice({"provider": "aliyun", "voice_id": "cosyvoice-test", "model": "cosyvoice-v3-flash", "name": "云音色", "source": "account_custom"})
    manager.adopt_catalog()
    row = manager.db.one("SELECT * FROM custom_voices WHERE voice_db_id=?", (voice_id,))
    assert row and row["status"] == "ready"
    manager.edit(row["id"], Edit(name="我的名字", tags="收藏"))
    manager.adopt_catalog()
    assert manager.db.one("SELECT name FROM voices WHERE id=?", (voice_id,))["name"] == "我的名字"


def test_icl2_uses_correct_key_resource_and_expressive_variant(monkeypatch):
    calls = []
    def request(method, url, **kwargs):
        calls.append(kwargs)
        body = json.dumps({"code": 0, "data": base64.b64encode(b"\x00\x00"*16000).decode()})+"\n"+json.dumps({"code": 20000000})
        return httpx.Response(200, content=body.encode(), request=httpx.Request(method, url))
    monkeypatch.setattr(providers, "_request", request)
    providers.VolcTTSProvider(settings_store=Settings()).synthesize(text="你好", voice="S_test", model="seed-icl-2.0", instruction="温柔地说")
    assert calls[0]["headers"]["X-Api-Key"] == "offline-key-volcengine_v2"
    assert calls[0]["headers"]["X-Api-Resource-Id"] == "seed-icl-2.0"
    assert calls[0]["json_body"]["req_params"]["model"] == "seed-tts-2.0-expressive"


def test_ali_scope_change_blocks_before_new_task(manager, monkeypatch):
    monkeypatch.setattr("custom_voices.create_voice", lambda *args: {"voice_id": "cosyvoice-scope", "status": "ready"})
    value = options().model_copy(update={"service": "aliyun", "model": "cosyvoice-v3.5-plus", "cost_acknowledged": True})
    ready = wait(manager, manager.create(value, None)["voice"]["id"])
    monkeypatch.setattr(manager.settings, "get_provider", lambda _: {"api_key": "offline-key", "region": "ap-southeast-1"})
    voice = manager.db.one("SELECT * FROM voices WHERE id=?", (ready["voice_db_id"],))
    with pytest.raises(providers.ProviderRequestError, match="Workspace"):
        manager.validate_synthesis(voice)
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        manager.refresh(ready["id"])
    assert len(manager.test_tasks) == 1


def test_reference_from_recording_inherits_human_speaker(manager):
    raw = audio()
    import hashlib
    manager.db.execute("INSERT INTO samples(id,provider,voice_id,text,file_path,sha256,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                       ("owned-recording", "real", "person-one", "你好", str(manager.root/"recording.wav"), hashlib.sha256(raw).hexdigest(), json.dumps({"speaker_id": "person-one"}), "2026-10-15"))
    value = options().model_copy(update={"mode": "clone"})
    ready = wait(manager, manager.create(value, raw)["voice"]["id"])
    assert ready["speaker_group"] == "person-one"


@pytest.mark.parametrize("service", ["volcengine_v1", "volcengine_v2", "aliyun"])
def test_status_query_contract_is_read_only(monkeypatch, service):
    calls = []
    def request(method, url, **kwargs):
        calls.append((url, kwargs))
        payload = {"speaker_id": "S_query", "status": 2} if service.startswith("volcengine") else {"output": {"status": "OK", "target_model": "cosyvoice-v3.5-plus"}}
        return httpx.Response(200, json=payload, request=httpx.Request(method, url))
    monkeypatch.setattr(protocols, "_request", request)
    result = protocols.query_voice({"service": service, "remote_voice_id": "S_query", "model": "cosyvoice-v3.5-plus"}, Settings())
    assert result["status"] == "ready" and len(calls) == 1
    url, kwargs = calls[0]
    if service.startswith("volcengine"):
        assert url.endswith("get_voice") and kwargs["json_body"] == {"speaker_id": "S_query"}
    else:
        assert kwargs["json_body"]["input"] == {"action": "query_voice", "voice_id": "S_query"}
