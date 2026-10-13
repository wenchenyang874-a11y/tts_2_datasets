"""MiMo and Doubao V3 protocol/failure regressions without any cloud calls."""
import base64
import io
import json
import wave

import httpx
import pytest

import providers
from settings_store import ProviderSettingsStore
from voice_catalog import builtin_voices, query_volc_public_voices


def wav_bytes():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\0\0" * 2400)
    return stream.getvalue()


def test_mimo_speaks_assistant_content_and_preserves_style_and_audio(monkeypatch):
    calls = []
    audio = wav_bytes()

    def request(method, url, **options):
        calls.append((method, url, options))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"audio": {"data": base64.b64encode(audio).decode()}}}]})

    monkeypatch.setattr(providers, "_request", request)
    adapter = providers.XiaomiTTSProvider({"api_key": "fixture-key", "endpoint": "https://api.xiaomimimo.com/v1"})
    assert adapter.synthesize(text="请开灯", voice="冰糖", instruction="轻快一些") == audio
    method, url, options = calls[0]
    assert method == "POST" and url.endswith("/v1/chat/completions")
    assert options["headers"]["api-key"] == "fixture-key"
    assert options["json_body"] == {"model": "mimo-v2.5-tts", "stream": False, "messages": [{"role": "user", "content": "轻快一些"}, {"role": "assistant", "content": "请开灯"}], "audio": {"voice": "冰糖", "format": "wav"}}
    assert options["retry_transient"] is False


@pytest.mark.parametrize("parameter", [{"speed": 1.2}, {"pitch": 1.2}, {"volume": 70}, {"sample_rate": 16000}, {"model": "mimo-v2.5-tts-voiceclone"}])
def test_mimo_rejects_unsupported_controls_without_http(monkeypatch, parameter):
    monkeypatch.setattr(providers, "_request", lambda *a, **k: pytest.fail("Unexpected cloud request"))
    with pytest.raises(providers.ProviderRequestError):
        providers.XiaomiTTSProvider({"api_key": "fixture"}).synthesize(text="测试", voice="冰糖", **parameter)


@pytest.mark.parametrize("payload", [{"choices": []}, {"choices": [{"finish_reason": "length", "message": {"audio": {"data": "AAAA"}}}]}, {"choices": [{"message": {"audio": {"data": "invalid!"}}}]}])
def test_mimo_rejects_missing_partial_or_invalid_audio(monkeypatch, payload):
    monkeypatch.setattr(providers, "_request", lambda *a, **k: httpx.Response(200, json=payload))
    with pytest.raises(providers.ProviderRequestError):
        providers.XiaomiTTSProvider({"api_key": "fixture"}).synthesize(text="测试", voice="冰糖")


def test_doubao2_v3_header_parameter_mapping_and_chunk_assembly(monkeypatch):
    calls = []
    audio = wav_bytes()
    records = [{"code": 0, "data": base64.b64encode(audio[:100]).decode()}, {"code": 0, "data": base64.b64encode(audio[100:]).decode()}, {"code": 20000000, "data": None}]

    def request(method, url, **options):
        calls.append((url, options))
        return httpx.Response(200, content="\n".join(json.dumps(row) for row in records).encode())

    monkeypatch.setattr(providers, "_request", request)
    adapter = providers.VolcTTSProvider({"api_key": "fixture-v3-key"})
    assert adapter.synthesize(text="请开灯", voice="zh_female_vv_uranus_bigtts", model="seed-tts-2.0", speed=1.5, volume=75, instruction="轻快一些") == audio
    url, options = calls[0]
    assert url.endswith("/api/v3/tts/unidirectional")
    assert options["headers"]["X-Api-Key"] == "fixture-v3-key"
    assert options["headers"]["X-Api-Resource-Id"] == "seed-tts-2.0"
    assert options["retry_transient"] is False
    params = options["json_body"]["req_params"]
    assert params["audio_params"]["speech_rate"] == 50
    assert params["audio_params"]["loudness_rate"] == 50
    assert json.loads(params["additions"])["context_texts"] == ["轻快一些"]


@pytest.mark.parametrize("response", [b'{"code":0,"data":"AA=="}\n{"code":123,"message":"failed"}', b'{"code":0,"data":"AA=="}\n{"code":', b'{"code":0,"data":"invalid!"}', b'{"code":20000000}'])
def test_doubao2_never_returns_partial_failed_audio(monkeypatch, response):
    monkeypatch.setattr(providers, "_request", lambda *a, **k: httpx.Response(200, content=response))
    with pytest.raises(providers.ProviderRequestError):
        providers.VolcTTSProvider({"api_key": "fixture"}).synthesize(text="测试", voice="vv", model="seed-tts-2.0")


@pytest.mark.parametrize("provider", ["xiaomi", "volcengine"])
def test_new_billable_post_does_not_retry_503_and_redacts_key(monkeypatch, provider):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(503, json={"message": "fixture-secret-api-key"})

    client = httpx.Client
    monkeypatch.setattr(providers.httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(handle), **kwargs))
    adapter = providers.get_provider
    store = ProviderSettingsStore.__new__(ProviderSettingsStore)
    store.get_provider = lambda name: {"api_key": "fixture-secret-api-key"}
    with pytest.raises(providers.ProviderRequestError) as error:
        adapter(provider, store).synthesize(text="测试", voice="vv", model="seed-tts-2.0" if provider == "volcengine" else "mimo-v2.5-tts")
    assert len(calls) == 1 and "fixture-secret-api-key" not in str(error.value)


def test_public_catalog_queries_both_model_resources(monkeypatch, tmp_path):
    captured = []
    monkeypatch.setattr(providers.VolcVoiceCatalogClient, "list_speakers", lambda self, resource_ids: captured.append(resource_ids) or [{"VoiceType": "vv", "ResourceID": "seed-tts-2.0"}])
    store = ProviderSettingsStore(tmp_path / "settings.json")
    store.update_provider("volcengine", {"access_key_id": "fixture", "access_key_secret": "fixture"})
    voices = query_volc_public_voices(store)
    assert captured == [("seed-tts-1.0", "seed-tts-2.0")]
    assert voices[0].model == "seed-tts-2.0"
    assert len(builtin_voices("xiaomi")) == 9
    assert any(voice.model == "seed-tts-2.0" for voice in builtin_voices("volcengine"))


@pytest.mark.parametrize("base", ["https://openspeech.bytedance.com", "https://openspeech.bytedance.com/", "https://openspeech.bytedance.com/api/v3"])
def test_doubao2_base_url_expands_to_request_path(base):
    assert providers._volc_endpoint(base, True) == "https://openspeech.bytedance.com/api/v3/tts/unidirectional"


def test_all_base_url_forms_expand_without_breaking_full_legacy_urls():
    assert providers._ali_service_endpoint({"endpoint":"https://dashscope.aliyuncs.com/api/v1"}, "SpeechSynthesizer").endswith("/api/v1/services/audio/tts/SpeechSynthesizer")
    assert providers._ali_service_endpoint({"endpoint":"https://dashscope.aliyuncs.com/api/v1"}, "customization").endswith("/api/v1/services/audio/tts/customization")
    assert providers._volc_endpoint("https://openspeech.bytedance.com", False).endswith("/api/v1/tts")
    assert providers._mimo_endpoint({"endpoint":"https://api.xiaomimimo.com/v1"}).endswith("/v1/chat/completions")
    full = "https://openspeech.bytedance.com/api/v1/tts"
    assert providers._volc_endpoint(full, False) == full


def test_doubao_configuration_clear_is_independent(tmp_path):
    store = ProviderSettingsStore(tmp_path / "settings.json")
    store.update_provider("volcengine", {"app_id":"fixture-app", "access_token":"fixture-v1-token"})
    store.update_provider("volcengine_v2", {"api_key":"fixture-v2-key"})
    store.clear_provider("volcengine_v2")
    store = ProviderSettingsStore(store.path)
    assert store.get_provider("volcengine_v2") == {}
    adapter = providers.VolcTTSProvider(settings_store=store)
    assert adapter.api_key == "" and adapter.token == "fixture-v1-token"
