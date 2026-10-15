"""Offline tests for TTS provider payloads, catalog parsing, and WAV output."""

from __future__ import annotations

import base64
import gzip
import io
import json
import sys
import wave
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import providers  # type: ignore  # noqa: E402
import voice_catalog  # type: ignore  # noqa: E402
import settings_store  # type: ignore  # noqa: E402


def _response(
    status: int = 200,
    *,
    payload: dict | None = None,
    content: bytes | None = None,
    content_type: str = "application/json",
) -> httpx.Response:
    request = httpx.Request("POST", "https://provider.test/api")
    if payload is not None:
        return httpx.Response(status, json=payload, headers={"content-type": content_type}, request=request)
    return httpx.Response(status, content=content or b"", headers={"content-type": content_type}, request=request)


def _volc_response(audio: bytes) -> httpx.Response:
    return _response(content=(json.dumps({"code": 0, "data": base64.b64encode(audio).decode()}) + "\n" + json.dumps({"code": 20000000})).encode())


def test_mock_provider_outputs_deterministic_kws_wav() -> None:
    adapter = providers.MockTTSProvider()
    first = adapter.synthesize(text="你好，小助手", voice="mock_female_young", seed=42)
    second = adapter.synthesize(text="你好，小助手", voice="mock_female_young", seed=42)
    changed = adapter.synthesize(text="你好，小助手", voice="mock_female_young", seed=43)

    assert first == second
    assert first != changed
    with wave.open(io.BytesIO(first), "rb") as wav_file:
        assert wav_file.getframerate() == 16_000
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getnframes() > 4_000


def test_provider_capability_contract_is_public_and_copy_safe() -> None:
    assert providers.PROVIDER_CAPABILITIES["mock"] == {
        "audio_formats": ("wav",),
        "sample_rates": (16_000,),
        "speed": True,
        "pitch": True,
        "volume": True,
    }
    assert providers.get_provider_capabilities("aliyun") == {
        "audio_formats": ["wav", "mp3"],
        "sample_rates": [8_000, 16_000, 24_000, 48_000],
        "speed": True,
        "pitch": True,
        "volume": True,
    }
    volc = providers.get_provider_capabilities("doubao")
    assert volc == {
        "audio_formats": ["wav", "mp3"],
        "sample_rates": [8_000, 16_000, 24_000],
        "speed": True,
        "pitch": False,
        "volume": True,
    }
    volc["audio_formats"].append("invalid")
    assert "invalid" not in providers.get_provider_capabilities("volcengine")["audio_formats"]


@pytest.mark.parametrize(
    ("audio_format", "sample_rate", "message"),
    [
        ("mp3", 16_000, "音频格式"),
        ("wav", 8_000, "采样率"),
        ("WAV", 16_000, "音频格式"),
    ],
)
def test_mock_provider_strictly_validates_audio_contract(
    audio_format: str, sample_rate: int, message: str,
) -> None:
    with pytest.raises(providers.ProviderRequestError, match=message):
        providers.MockTTSProvider().synthesize(
            text="你好，小助手",
            voice="mock_female_young",
            audio_format=audio_format,
            sample_rate=sample_rate,
        )


def test_available_providers_never_exposes_credentials(tmp_path: Path) -> None:
    secret = "unit-test-provider-secret"
    store = settings_store.ProviderSettingsStore(tmp_path / "settings.json")
    store.update_provider("aliyun", {"api_key": secret})
    store.update_provider(
        "volcengine",
        {
            "app_id": "app-secret-id",
            "api_key": "volc-secret-token",
            "access_key_id": "fixture-catalog-access-id",
            "access_key_secret": "volc-catalog-secret",
        },
    )

    statuses = providers.available_providers(store)
    rendered = repr(statuses)

    assert {item.id for item in statuses} == {"mock", "aliyun", "volcengine", "xiaomi"}
    assert next(item for item in statuses if item.id == "aliyun").configured is True
    assert secret not in rendered
    assert "volc-secret-token" not in rendered
    assert "volc-catalog-secret" not in rendered
    assert next(item for item in statuses if item.id == "volcengine").supports_voice_query


def test_cloud_credential_environment_variables_are_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "legacy-env-secret")
    monkeypatch.setenv("VOLC_TTS_APP_ID", "legacy-env-app")
    monkeypatch.setenv("VOLC_TTS_TOKEN", "legacy-env-token")
    store = settings_store.ProviderSettingsStore(tmp_path / "settings.json")

    statuses = providers.available_providers(store)

    assert next(item for item in statuses if item.id == "aliyun").configured is False
    assert next(item for item in statuses if item.id == "volcengine").configured is False
    with pytest.raises(providers.ProviderConfigurationError, match="设置页面"):
        providers.AlibabaTTSProvider(settings_store=store)


def test_alibaba_payload_and_audio_download(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def fake_request(method: str, url: str, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        if method == "POST":
            return _response(payload={
                "request_id": "request-1",
                "output": {"audio": {
                    "url": "https://dashscope-result.oss-cn-beijing.aliyuncs.com/result.wav"
                }},
            })
        return _response(content=b"RIFF-unit-test", content_type="audio/wav")

    monkeypatch.setattr(providers, "_request", fake_request)
    audio = providers.AlibabaTTSProvider({
        "api_key": "fixture-synthesis-credential",
        "endpoint": "https://dashscope.aliyuncs.com/SpeechSynthesizer",
    }).synthesize(
        text="你好，小助手", voice="longxiaoxia_v2", model="cosyvoice-v2",
        audio_format="mp3", sample_rate=24_000,
        speed=1.1, pitch=0.9, volume=62, seed=70_000,
    )

    request_input = calls[0]["json_body"]["input"]
    assert audio == b"RIFF-unit-test"
    assert request_input == {
        "text": "你好，小助手",
        "voice": "longxiaoxia_v2",
        "format": "mp3",
        "sample_rate": 24_000,
        "volume": 62,
        "rate": 1.1,
        "pitch": 0.9,
        "seed": 70_000 % 65_536,
    }
    assert calls[0]["retry_transient"] is False
    assert calls[1]["method"] == "GET"
    assert "headers" not in calls[1]


def test_alibaba_audio_url_rejects_non_vendor_host() -> None:
    with pytest.raises(providers.ProviderRequestError, match="非官方域名"):
        providers._alibaba_audio_url("https://127.0.0.1/latest/meta-data")

    upgraded = providers._alibaba_audio_url(
        "http://dashscope-result.oss-cn-beijing.aliyuncs.com/result.wav?token=opaque"
    )
    assert upgraded.startswith("https://dashscope-result.oss-cn-beijing.aliyuncs.com/")
    assert upgraded.endswith("?token=opaque")


def test_provider_returned_audio_query_is_preserved_in_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(200, content=b"RIFF-query-test", request=request)

    real_client = httpx.Client
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        providers.httpx,
        "Client",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    url = providers._alibaba_audio_url(
        "https://dashscope-result.oss-cn-beijing.aliyuncs.com/result.wav?token=opaque"
    )

    response = providers._request("GET", url, provider="阿里云音频")

    assert response.content == b"RIFF-query-test"
    assert requested_urls == [url]


def test_provider_request_decodes_gzip_once_and_removes_encoding_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b'{"code":0,"message":"ok"}'
    observed_accept_encoding: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed_accept_encoding.append(request.headers.get("accept-encoding", ""))
        return httpx.Response(
            200,
            stream=httpx.ByteStream(gzip.compress(payload)),
            headers={"content-encoding": "gzip", "content-type": "application/json"},
            request=request,
        )

    real_client = httpx.Client
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        providers.httpx,
        "Client",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    response = providers._request(
        "GET", "https://provider.test/catalog", provider="测试"
    )

    assert response.json() == {"code": 0, "message": "ok"}
    assert "content-encoding" not in response.headers
    assert observed_accept_encoding == ["gzip"]


def test_provider_request_accepts_transport_predecoded_gzip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b'{"code":0,"message":"already decoded by transport"}'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=gzip.compress(payload),
            headers={"content-encoding": "gzip", "content-type": "application/json"},
            request=request,
        )

    real_client = httpx.Client
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        providers.httpx,
        "Client",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    response = providers._request(
        "GET", "https://provider.test/catalog", provider="测试"
    )

    assert response.json()["message"] == "already decoded by transport"


def test_provider_request_rejects_invalid_gzip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_client = httpx.Client
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            stream=httpx.ByteStream(b"not-a-gzip-stream"),
            headers={"content-encoding": "gzip"},
            request=request,
        )
    )
    monkeypatch.setattr(
        providers.httpx,
        "Client",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    with pytest.raises(providers.ProviderRequestError, match="gzip 响应无效"):
        providers._request("GET", "https://provider.test/catalog", provider="测试")


def test_provider_request_rejects_oversized_decoded_gzip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_client = httpx.Client
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            stream=httpx.ByteStream(gzip.compress(b"x" * 32)),
            headers={"content-encoding": "gzip"},
            request=request,
        )
    )
    monkeypatch.setattr(
        providers.httpx,
        "Client",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    with pytest.raises(providers.ProviderRequestError, match="解压后响应超过"):
        providers._request(
            "GET", "https://provider.test/catalog", provider="测试", max_response_bytes=24
        )


@pytest.mark.parametrize(
    "field,url",
    [
        ("endpoint", "https://dashscope.aliyuncs.com/api/v1?token=forbidden"),
        ("customization_endpoint", "https://dashscope.aliyuncs.com/api/v1#fragment"),
    ],
)
def test_configured_endpoints_reject_query_and_fragment(field: str, url: str) -> None:
    with pytest.raises(providers.ProviderConfigurationError, match="查询参数或片段"):
        providers.validate_provider_configuration(
            "aliyun", {"region": "cn-beijing", field: url},
            require_credentials=False,
        )


@pytest.mark.parametrize(
    "provider,config",
    [
        ("aliyun", {"endpoint": "https://credentials.example/tts"}),
        (
            "aliyun",
            {"endpoint": "https://tenant.oss-cn-beijing.aliyuncs.com/tts"},
        ),
        ("volcengine", {"endpoint": "https://credentials.example/tts"}),
        (
            "volcengine",
            {"endpoint": "https://tenant.bytedance.com/tts"},
        ),
        (
            "volcengine",
            {"catalog_endpoint": "https://credentials.example/catalog"},
        ),
        ("volcengine", {"endpoint": "http://127.0.0.1:9000/tts"}),
    ],
)
def test_credential_bearing_endpoints_reject_non_vendor_hosts(
    monkeypatch: pytest.MonkeyPatch, provider: str, config: dict[str, str],
) -> None:
    monkeypatch.delenv("TTS_ALLOW_CUSTOM_PROVIDER_ENDPOINTS", raising=False)
    with pytest.raises(providers.ProviderConfigurationError, match="指定官方主机"):
        providers.validate_provider_configuration(
            provider, config, require_credentials=False,
        )


def test_custom_provider_endpoint_requires_explicit_server_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TTS_ALLOW_CUSTOM_PROVIDER_ENDPOINTS", "1")
    providers.validate_provider_configuration(
        "volcengine",
        {"endpoint": "https://trusted-private-proxy.example/tts"},
        require_credentials=False,
    )


def test_alibaba_saved_configuration_rejects_unsupported_region() -> None:
    with pytest.raises(providers.ProviderConfigurationError, match="cn-beijing"):
        providers.validate_provider_configuration(
            "aliyun", {"region": "invalid-region"}, require_credentials=False,
        )


def test_provider_request_does_not_follow_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_client = httpx.Client
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            302, headers={"location": "http://127.0.0.1/internal"}, request=request
        )
    )
    monkeypatch.setattr(
        providers.httpx,
        "Client",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    with pytest.raises(providers.ProviderRequestError, match="HTTP 302"):
        providers._request("GET", "https://provider.test/audio", provider="测试")


def test_provider_request_rejects_oversized_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_client = httpx.Client
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, content=b"0123456789", headers={"content-length": "10"}, request=request
        )
    )
    monkeypatch.setattr(
        providers.httpx,
        "Client",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    with pytest.raises(providers.ProviderRequestError, match="响应超过"):
        providers._request(
            "GET", "https://provider.test/audio", provider="测试", max_response_bytes=4
        )


def test_provider_request_retry_preserves_raw_request_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        status = 500 if len(bodies) == 1 else 200
        return httpx.Response(status, content=b"ok", request=request)

    real_client = httpx.Client
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        providers.httpx,
        "Client",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    monkeypatch.setenv("TTS_RETRY_COUNT", "1")
    monkeypatch.setattr(providers.time, "sleep", lambda _: None)
    request_body = b'{"stable":"request-body"}'

    response = providers._request(
        "POST",
        "https://provider.test/api",
        provider="测试",
        headers={"content-type": "application/json"},
        content=request_body,
    )

    assert response.status_code == 200
    assert bodies == [request_body, request_body]


def test_billable_request_can_disable_implicit_transient_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(503, json={"message": "temporary"}, request=request)

    real_client = httpx.Client
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        providers.httpx,
        "Client",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    monkeypatch.setenv("TTS_RETRY_COUNT", "5")

    with pytest.raises(providers.ProviderRequestError, match="HTTP 503"):
        providers._request(
            "POST",
            "https://provider.test/billable-synthesis",
            provider="计费合成",
            json_body={"text": "unit test"},
            retry_transient=False,
        )

    assert len(attempts) == 1


def test_alibaba_region_endpoint_rules() -> None:
    workspace_endpoint = providers._ali_service_endpoint(
        {"workspace_id": "ws-unit", "region": "ap-southeast-1"},
        "customization",
    )
    shared_endpoint = providers._ali_service_endpoint(
        {"region": "ap-southeast-1"}, "customization"
    )

    assert workspace_endpoint.startswith("https://ws-unit.ap-southeast-1.maas.aliyuncs.com/")
    assert shared_endpoint.startswith("https://dashscope-intl.aliyuncs.com/")
    assert providers._ali_service_endpoint(
        {"region": "ap-southeast-1"}, "SpeechSynthesizer"
    ).startswith("https://dashscope-intl.aliyuncs.com/")


@pytest.mark.parametrize(
    ("audio_format", "sample_rate", "message"),
    [
        ("ogg_opus", 16_000, "音频格式"),
        ("wav", 44_100, "采样率"),
    ],
)
def test_alibaba_rejects_unsupported_audio_contract_before_request(
    monkeypatch: pytest.MonkeyPatch,
    audio_format: str,
    sample_rate: int,
    message: str,
) -> None:
    monkeypatch.setattr(
        providers,
        "_request",
        lambda *args, **kwargs: pytest.fail("remote request must not run"),
    )
    adapter = providers.AlibabaTTSProvider({
        "api_key": "fixture-synthesis-credential",
    })

    with pytest.raises(providers.ProviderRequestError, match=message):
        adapter.synthesize(
            text="你好，小助手",
            voice="longxiaoxia_v2",
            model="cosyvoice-v2",
            audio_format=audio_format,
            sample_rate=sample_rate,
        )


def test_volc_payload_and_base64_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_request(method: str, url: str, **kwargs):
        captured.update(kwargs)
        captured["url"] = url
        return _volc_response(b"ID3-volc-test")

    monkeypatch.setattr(providers, "_request", fake_request)
    audio = providers.VolcTTSProvider({
        "api_key": "unit-token",
    }).synthesize(
        text="你好，小助手", voice="zh_female_cancan_mars_bigtts",
        audio_format="mp3", sample_rate=8_000,
        speed=1.2, pitch=1.0, volume=50,
    )

    payload = captured["json_body"]
    assert audio == b"ID3-volc-test"
    assert payload["req_params"]["audio_params"] == {"format": "mp3", "sample_rate": 8_000, "speech_rate": 20, "loudness_rate": 0}
    assert "app" not in payload and "request" not in payload
    assert captured["url"] == "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
    assert captured["headers"]["X-Api-Key"] == "unit-token"
    assert captured["headers"]["X-Api-Resource-Id"] == "seed-tts-1.0"
    assert "Authorization" not in captured["headers"]
    assert captured["retry_transient"] is False


def test_volc_v1_rejects_pitch_before_remote_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fake_request(*args, **kwargs):
        nonlocal called
        called = True
        return _response(payload={"code": 3000, "data": ""})

    monkeypatch.setattr(providers, "_request", fake_request)
    adapter = providers.VolcTTSProvider({
        "app_id": "unit-app", "api_key": "unit-token",
    })

    with pytest.raises(providers.ProviderRequestError, match="0 st"):
        adapter.synthesize(text="你好，小助手", voice="unit-voice", pitch=1.1)

    assert called is False


@pytest.mark.parametrize(
    ("audio_format", "sample_rate", "message"),
    [
        ("flac", 16_000, "音频格式"),
        ("wav", 48_000, "采样率"),
    ],
)
def test_volc_rejects_unsupported_audio_contract_before_request(
    monkeypatch: pytest.MonkeyPatch,
    audio_format: str,
    sample_rate: int,
    message: str,
) -> None:
    monkeypatch.setattr(
        providers,
        "_request",
        lambda *args, **kwargs: pytest.fail("remote request must not run"),
    )
    adapter = providers.VolcTTSProvider({
        "app_id": "fixture-app-id",
        "api_key": "fixture-synthesis-token",
    })

    with pytest.raises(providers.ProviderRequestError, match=message):
        adapter.synthesize(
            text="你好，小助手",
            voice="fixture-voice",
            audio_format=audio_format,
            sample_rate=sample_rate,
        )


@pytest.mark.parametrize(("volume", "expected"), [(0, -50), (100, 100)])
def test_volc_v1_maps_volume_to_loudness_boundary(
    monkeypatch: pytest.MonkeyPatch, volume: int, expected: float,
) -> None:
    captured: dict = {}

    def fake_request(method: str, url: str, **kwargs):
        captured.update(kwargs)
        return _volc_response(b"\0\0" * 100)

    monkeypatch.setattr(providers, "_request", fake_request)
    providers.VolcTTSProvider({
        "app_id": "unit-app", "api_key": "unit-token",
    }).synthesize(text="你好，小助手", voice="unit-voice", volume=volume)

    assert captured["json_body"]["req_params"]["audio_params"]["loudness_rate"] == expected


def test_volc_catalog_signs_and_paginates_without_synthesis_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def fake_request(method: str, url: str, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        page = len(calls)
        return _response(payload={
            "ResponseMetadata": {"RequestId": f"request-{page}"},
            "Result": {
                "Total": 2,
                "Speakers": [{
                    "VoiceType": f"voice-{page}",
                    "Name": f"音色 {page}",
                    "ResourceID": "seed-tts-1.0",
                }],
            },
        })

    monkeypatch.setattr(providers, "_request", fake_request)
    client = providers.VolcVoiceCatalogClient({
        "access_key_id": "fixture-catalog-access-id",
        "access_key_secret": "fixture-catalog-secret",
        "catalog_endpoint": "https://open.volcengineapi.com",
    })
    speakers = client.list_speakers(page_size=1)

    assert [item["VoiceType"] for item in speakers] == ["voice-1", "voice-2"]
    assert all(call["method"] == "POST" for call in calls)
    assert "Action=ListSpeakers&Version=2025-05-20" in calls[0]["url"]
    assert calls[0]["headers"]["Authorization"].startswith(
        "HMAC-SHA256 Credential=fixture-catalog-access-id/"
    )
    assert calls[0]["headers"]["X-Content-Sha256"] == providers.hashlib.sha256(
        calls[0]["content"]
    ).hexdigest()
    assert b'"ResourceIDs":["seed-tts-1.0"]' in calls[0]["content"]


def test_volc_catalog_preserves_trailing_slash_in_signed_request_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urls: list[str] = []

    def fake_request(method: str, url: str, **kwargs):
        urls.append(url)
        return _response(payload={
            "ResponseMetadata": {"RequestId": "request-1"},
            "Result": {"Total": 0, "Speakers": []},
        })

    monkeypatch.setattr(providers, "_request", fake_request)
    client = providers.VolcVoiceCatalogClient({
        "access_key_id": "fixture-catalog-access-id",
        "access_key_secret": "fixture-catalog-secret",
        "catalog_endpoint": "https://open.volcengineapi.com/custom/",
    })

    assert client.list_speakers() == []
    assert urls[0].startswith("https://open.volcengineapi.com/custom/?Action=")


def test_volc_account_catalog_uses_appid_api_region_and_paginates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def fake_request(method: str, url: str, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        page = len(calls)
        return _response(payload={
            "ResponseMetadata": {"RequestId": f"account-request-{page}"},
            "Result": {
                "AppID": "unit-app",
                "TotalCount": 2,
                "PageNumber": page,
                "PageSize": 1,
                "Statuses": [{"SpeakerID": f"S_unit_{page}", "State": "Success"}],
            },
        })

    monkeypatch.setattr(providers, "_request", fake_request)
    client = providers.VolcVoiceCatalogClient({
        "access_key_id": "fixture-catalog-access-id",
        "access_key_secret": "fixture-catalog-secret",
        "catalog_endpoint": "https://open.volcengineapi.com",
    })

    speakers = client.list_account_speakers("unit-app", page_size=1)

    assert [item["SpeakerID"] for item in speakers] == ["S_unit_1", "S_unit_2"]
    assert "Action=BatchListMegaTTSTrainStatus&Version=2023-11-07" in calls[0]["url"]
    assert "/cn-north-1/speech_saas_prod/request" in calls[0]["headers"]["Authorization"]
    assert calls[0]["content"] == b'{"AppID":"unit-app","PageNumber":1,"PageSize":1}'


def test_volc_catalog_voice_mapping(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = settings_store.ProviderSettingsStore(tmp_path / "settings.json")
    store.update_provider("volcengine", {
        "access_key_id": "fixture-catalog-access-id",
        "access_key_secret": "fixture-catalog-secret",
    })
    monkeypatch.setattr(
        providers.VolcVoiceCatalogClient,
        "list_speakers",
        lambda self, **kwargs: [{
            "VoiceType": "zh_female_test_mars_bigtts",
            "Name": "测试女声",
            "Gender": "女",
            "Age": "青年",
            "ResourceID": "seed-tts-1.0",
            "Languages": [{"Language": "zh-cn"}],
            "Description": "测试目录映射",
        }],
    )

    voices = voice_catalog.query_volc_public_voices(store)

    assert len(voices) == 1
    assert voices[0].gender == "female"
    assert voices[0].age_group == "young"
    assert voices[0].source == "provider_catalog"


def test_volc_account_voice_mapping_only_enables_compatible_ready_voice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    store = settings_store.ProviderSettingsStore(tmp_path / "settings.json")
    store.update_provider("volcengine", {
        "app_id": "unit-app",
        "access_key_id": "fixture-catalog-access-id",
        "access_key_secret": "fixture-catalog-secret",
    })
    monkeypatch.setattr(
        providers.VolcVoiceCatalogClient,
        "list_account_speakers",
        lambda self, app_id, **kwargs: [
            {
                "SpeakerID": "S_ready",
                "Alias": "我的复刻音色",
                "State": "Success",
                "ModelTypeDetails": [{"ResourceID": "seed-icl-1.0"}],
            },
            {
                "SpeakerID": "S_training",
                "State": "Training",
                "ModelTypeDetails": [{"ResourceID": "seed-icl-1.0"}],
            },
            {
                "SpeakerID": "S_v2",
                "State": "Active",
                "ModelTypeDetails": [{"ResourceID": "seed-icl-2.0"}],
            },
        ],
    )

    voices = voice_catalog.query_volc_account_voices(store)

    by_id = {item.voice_id: item for item in voices}
    assert by_id["S_ready"].name == "我的复刻音色"
    assert by_id["S_ready"].enabled is True
    assert by_id["S_training"].enabled is False
    assert by_id["S_v2"].enabled is True
    assert all(item.source == "account_clone" for item in voices)

    captured: dict = {}

    def fake_synthesis_request(method: str, url: str, **kwargs):
        captured.update(kwargs)
        return _volc_response(b"\0\0" * 100)

    monkeypatch.setattr(providers, "_request", fake_synthesis_request)
    ready = by_id["S_ready"]
    audio = providers.VolcTTSProvider({
        "app_id": "unit-app", "api_key": "unit-token",
    }).synthesize(
        text="你好，小助手", voice=ready.voice_id, model=ready.model,
    )

    with wave.open(io.BytesIO(audio)) as wav:
        assert wav.readframes(wav.getnframes()) == b"\0\0" * 100
    assert captured["json_body"]["req_params"]["speaker"] == "S_ready"
    assert captured["headers"]["X-Api-Resource-Id"] == "seed-icl-1.0"


@pytest.mark.parametrize("failed_source", ["public", "account"])
def test_volc_catalog_partial_failure_keeps_successful_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failed_source: str,
) -> None:
    store = settings_store.ProviderSettingsStore(tmp_path / "settings.json")
    store.update_provider("volcengine", {
        "app_id": "unit-app",
        "api_key": "unit-token",
        "access_key_id": "fixture-catalog-access-id",
        "access_key_secret": "fixture-catalog-secret",
    })

    def public_ok(settings_store, refresh_state):
        refresh_state.setdefault("complete_snapshots", []).append(
            {"provider": "volcengine", "source": "provider_catalog"}
        )
        return []

    def account_ok(settings_store, refresh_state):
        refresh_state.setdefault("complete_snapshots", []).append(
            {"provider": "volcengine", "source": "account_clone"}
        )
        return []

    def failed(*args, **kwargs):
        raise providers.ProviderRequestError("simulated directory failure")

    monkeypatch.setattr(
        voice_catalog,
        "query_volc_public_voices",
        failed if failed_source == "public" else public_ok,
    )
    monkeypatch.setattr(
        voice_catalog,
        "query_volc_account_voices",
        failed if failed_source == "account" else account_ok,
    )
    refresh_state: dict = {}

    catalog = voice_catalog.refresh_voice_catalog(
        provider="volcengine", settings_store=store, refresh_state=refresh_state,
    )

    assert catalog
    assert len(refresh_state["warnings"]) == 1
    snapshot_sources = {item["source"] for item in refresh_state["complete_snapshots"]}
    expected_success = "account_clone" if failed_source == "public" else "provider_catalog"
    expected_failure = "provider_catalog" if failed_source == "public" else "account_clone"
    assert expected_success in snapshot_sources
    assert expected_failure not in snapshot_sources


def test_volc_catalog_raises_when_both_dynamic_queries_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    store = settings_store.ProviderSettingsStore(tmp_path / "settings.json")
    store.update_provider("volcengine", {
        "app_id": "unit-app",
        "api_key": "unit-token",
        "access_key_id": "fixture-catalog-access-id",
        "access_key_secret": "fixture-catalog-secret",
    })

    def failed(*args, **kwargs):
        raise providers.ProviderRequestError("simulated directory failure")

    monkeypatch.setattr(voice_catalog, "query_volc_public_voices", failed)
    monkeypatch.setattr(voice_catalog, "query_volc_account_voices", failed)

    with pytest.raises(providers.ProviderError, match="公共音色目录.*账号复刻音色"):
        voice_catalog.refresh_voice_catalog(
            provider="volcengine", settings_store=store, refresh_state={},
        )


def test_remote_error_redacts_secret() -> None:
    secret = "unit-test-secret-should-not-leak"
    response = _response(401, payload={"code": "InvalidKey", "message": f"bad key {secret}"})

    message = providers._response_error(response, "阿里云", (secret,))

    assert secret not in message
    assert "***" in message


def test_volc_openapi_http_error_extracts_nested_metadata() -> None:
    response = _response(403, payload={
        "ResponseMetadata": {
            "RequestId": "request-nested-1",
            "Error": {
                "Code": "Forbidden.ErrAccountNotPermission",
                "Message": "account has no permission",
            },
        },
    })

    message = providers._response_error(response, "火山引擎账号复刻音色", ())

    assert "Forbidden.ErrAccountNotPermission" in message
    assert "account has no permission" in message
    assert "request-nested-1" in message


def test_builtin_catalog_is_chinese_friendly_and_model_bound(tmp_path: Path) -> None:
    store = settings_store.ProviderSettingsStore(tmp_path / "settings.json")
    catalog = voice_catalog.refresh_voice_catalog(settings_store=store)

    assert {item.provider for item in catalog} == {"mock", "aliyun", "volcengine", "xiaomi"}
    assert all(item.voice_id and item.model for item in catalog)
    assert any(item.gender == "female" and item.age_group == "senior" for item in catalog)
    assert any("zh" in language for item in catalog for language in item.languages)
    aliyun = [item for item in catalog if item.provider == "aliyun"]
    assert len(aliyun) == 30
    assert any(item.metadata.get("age_range") == "60+" for item in aliyun)


def test_custom_alibaba_voice_mapping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    store = settings_store.ProviderSettingsStore(tmp_path / "settings.json")
    store.update_provider("aliyun", {"api_key": "fixture-synthesis-credential"})
    monkeypatch.setattr(
        providers.AlibabaTTSProvider,
        "list_custom_voices",
        lambda self: [{
            "voice_id": "cosyvoice-v3.5-plus-vd-xiaozhushou-1234",
            "target_model": "cosyvoice-v3.5-plus",
            "voice_prompt": "沉稳的中年女性普通话播音员",
            "status": "OK",
            "design_model": "voice-enrollment",
        }],
    )
    # voice_catalog imports the same class object, so patching its method is sufficient.
    voices = voice_catalog.query_alibaba_custom_voices(store)

    assert len(voices) == 1
    assert voices[0].provider == "aliyun"
    assert voices[0].gender == "female"
    assert voices[0].age_group == "middle"
    assert voices[0].enabled is True


def test_qwen3_custom_voice_is_retained_but_not_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    store = settings_store.ProviderSettingsStore(tmp_path / "settings.json")
    store.update_provider("aliyun", {"api_key": "fixture-synthesis-credential"})
    monkeypatch.setattr(
        providers.AlibabaTTSProvider,
        "list_custom_voices",
        lambda self: [{
            "voice_id": "qwen3-tts-vc-xiaozhushou",
            "target_model": "qwen3-tts-vc-realtime-2026-01-15",
            "status": "OK",
            "design_model": "qwen-voice-enrollment",
        }],
    )

    voices = voice_catalog.query_alibaba_custom_voices(store)

    assert len(voices) == 1
    assert voices[0].enabled is False
    assert voices[0].metadata["synthesis_compatible"] is False


def test_alibaba_catalog_keeps_successful_design_when_another_is_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_request(method: str, url: str, **kwargs):
        model = kwargs["json_body"]["model"]
        if model == "voice-enrollment":
            raise providers.ProviderRequestError("unsupported action")
        if model == "qwen-voice-enrollment":
            return _response(payload={"output": {"voice_list": []}})
        return _response(payload={
            "output": {"voice_list": [{"voice_id": "qwen-design-voice"}]}
        })

    monkeypatch.setattr(providers, "_request", fake_request)
    adapter = providers.AlibabaTTSProvider({"api_key": "fixture-synthesis-credential"})

    voices = adapter.list_custom_voices()

    assert [item["voice_id"] for item in voices] == ["qwen-design-voice"]
    assert adapter.catalog_complete is False
