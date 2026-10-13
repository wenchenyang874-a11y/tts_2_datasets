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
    pcm = b"\0\0" * 2400
    records = [{"code": 0, "data": base64.b64encode(pcm[:100]).decode()}, {"code": 0, "data": base64.b64encode(pcm[100:]).decode()}, {"code": 20000000, "data": None}]

    def request(method, url, **options):
        calls.append((url, options))
        return httpx.Response(200, content="\n".join(json.dumps(row) for row in records).encode())

    monkeypatch.setattr(providers, "_request", request)
    adapter = providers.VolcTTSProvider({"api_key": "fixture-v3-key"})
    audio = adapter.synthesize(text="请开灯", voice="zh_female_vv_uranus_bigtts", model="seed-tts-2.0", speed=1.5, volume=75, instruction="轻快一些")
    with wave.open(io.BytesIO(audio)) as wav:
        assert wav.getnchannels() == 1 and wav.getsampwidth() == 2 and wav.getframerate() == 16000
        assert wav.readframes(wav.getnframes()) == pcm
    url, options = calls[0]
    assert url.endswith("/api/v3/tts/unidirectional")
    assert options["headers"]["X-Api-Key"] == "fixture-v3-key"
    assert options["headers"]["X-Api-Resource-Id"] == "seed-tts-2.0"
    assert options["retry_transient"] is False
    params = options["json_body"]["req_params"]
    assert params["audio_params"]["format"] == "pcm"
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


def test_direct_adapter_base_url_forms_expand():
    assert providers._ali_service_endpoint({"endpoint":"https://dashscope.aliyuncs.com/api/v1"}, "SpeechSynthesizer").endswith("/api/v1/services/audio/tts/SpeechSynthesizer")
    assert providers._ali_service_endpoint({"endpoint":"https://dashscope.aliyuncs.com/api/v1"}, "customization").endswith("/api/v1/services/audio/tts/customization")
    assert providers._mimo_endpoint({"endpoint":"https://api.xiaomimimo.com/v1"}).endswith("/v1/chat/completions")


def test_doubao_configuration_clear_is_independent(tmp_path):
    store = ProviderSettingsStore(tmp_path / "settings.json")
    store.update_provider("volcengine", {"api_key":"fixture-v1-key"})
    store.update_provider("volcengine_v2", {"api_key":"fixture-v2-key"})
    store.clear_provider("volcengine_v2")
    store = ProviderSettingsStore(store.path)
    assert store.get_provider("volcengine_v2") == {}
    adapter = providers.VolcTTSProvider(settings_store=store)
    assert adapter.configs["volcengine"]["api_key"] == "fixture-v1-key"
    assert not adapter.configs["volcengine_v2"].get("api_key")


@pytest.mark.parametrize("resource,key", [("seed-tts-1.0", "fixture-v1-key"), ("seed-tts-2.0", "fixture-v2-key")])
def test_both_doubao_models_use_v3_with_their_own_api_key(monkeypatch, tmp_path, resource, key):
    store = ProviderSettingsStore(tmp_path / "settings.json")
    store.update_provider("volcengine", {"api_key": "fixture-v1-key"})
    store.update_provider("volcengine_v2", {"api_key": "fixture-v2-key"})
    calls = []
    def request(method, url, **options):
        calls.append((url, options))
        # Match the current official HTTP guide's code=0 + usage response.
        return httpx.Response(200, json={"code": 0, "message": "OK", "data": base64.b64encode(b"ID3-complete").decode(), "usage": {"text_words": 2}})
    monkeypatch.setattr(providers, "_request", request)
    audio = providers.VolcTTSProvider(settings_store=store).synthesize(text="测试", voice="fixture", model=resource, audio_format="mp3")
    assert audio == b"ID3-complete"
    url, options = calls[0]
    assert url == "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
    assert options["headers"]["X-Api-Key"] == key
    assert options["headers"]["X-Api-Resource-Id"] == resource
    assert options["headers"]["X-Api-Request-Id"]
    assert set(options["json_body"]) == {"req_params"}
    assert options["retry_transient"] is False


@pytest.mark.parametrize("missing_model,configured_service", [("seed-tts-1.0", "volcengine_v2"), ("seed-tts-2.0", "volcengine")])
def test_doubao_never_falls_back_to_other_models_key(monkeypatch, tmp_path, missing_model, configured_service):
    store = ProviderSettingsStore(tmp_path / "settings.json")
    store.update_provider(configured_service, {"api_key": "fixture-only-other-model"})
    monkeypatch.setattr(providers, "_request", lambda *a, **k: pytest.fail("Must block before HTTP"))
    with pytest.raises(providers.ProviderConfigurationError, match="API Key"):
        providers.VolcTTSProvider(settings_store=store).synthesize(text="测试", voice="fixture", model=missing_model)


@pytest.mark.parametrize("resource", ["seed-tts-1.0", "seed-tts-2.0"])
def test_doubao_discards_unfinished_valid_json_chunks(monkeypatch, resource):
    monkeypatch.setattr(providers, "_request", lambda *a, **k: httpx.Response(200, json={"code": 0, "data": "AAAA"}))
    with pytest.raises(providers.ProviderRequestError, match="缺少完成标记"):
        providers.VolcTTSProvider({"api_key": "fixture"}).synthesize(text="测试", voice="fixture", model=resource)


@pytest.mark.parametrize("region,workspace,host", [("cn-beijing", "", "dashscope.aliyuncs.com"), ("ap-southeast-1", "", "dashscope-intl.aliyuncs.com"), ("cn-beijing", "ws-fixture", "ws-fixture.cn-beijing.maas.aliyuncs.com"), ("ap-southeast-1", "ws-fixture", "ws-fixture.ap-southeast-1.maas.aliyuncs.com")])
def test_region_selection_routes_synthesis_and_catalog_together(monkeypatch, region, workspace, host):
    calls = []
    def request(method, url, **options):
        calls.append(url)
        if url.endswith("customization"):
            return httpx.Response(200, json={"output": {"voice_list": []}})
        return httpx.Response(200, headers={"content-type": "audio/wav"}, content=wav_bytes())
    monkeypatch.setattr(providers, "_request", request)
    config = {"api_key": "fixture", "region": region, "workspace_id": workspace}
    providers.validate_provider_configuration("aliyun", config)
    adapter = providers.AlibabaTTSProvider(config)
    adapter.synthesize(text="测试", voice="longxiaoxia", model="cosyvoice-v2")
    adapter.list_custom_voices()
    assert calls[0] == f"https://{host}/api/v1/services/audio/tts/SpeechSynthesizer"
    assert all(url == f"https://{host}/api/v1/services/audio/tts/customization" for url in calls[1:])
