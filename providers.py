"""Server-side TTS provider adapters.

Credentials are read from the application's private settings store at adapter
construction time.  The module intentionally returns only audio bytes to callers so
credentials and vendor response objects cannot leak into dataset metadata.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import io
import json
import math
import os
import random
import re
import struct
import time
import uuid
import wave
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import quote, urlencode, urlparse

import httpx

try:
    from .settings_store import ProviderSettingsStore, get_default_settings_store
except ImportError:  # Support direct ``uvicorn app:app`` execution.
    from settings_store import ProviderSettingsStore, get_default_settings_store  # type: ignore


class ProviderError(RuntimeError):
    """Base error exposed to the application task runner."""


class ProviderConfigurationError(ProviderError):
    """A provider is missing credentials or has an unsafe configuration."""


class ProviderRequestError(ProviderError):
    """A remote provider rejected a request or returned malformed data."""


@dataclass(frozen=True)
class ProviderInfo:
    """Public, non-secret provider status returned by ``/api/providers``."""

    id: str
    name: str
    configured: bool
    available: bool
    supports_voice_query: bool
    supports_seed: bool
    detail: str
    synthesis_configured: bool = False
    catalog_configured: bool = False


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _redact(message: object, secrets: tuple[str, ...] = ()) -> str:
    value = str(message)
    for secret in secrets:
        if len(secret) >= 4:
            value = value.replace(secret, "***")
    value = re.sub(r"\bsk-[A-Za-z0-9_-]{6,}\b", "sk-***", value)
    value = re.sub(r"Bearer[; ]+[A-Za-z0-9._~+/=-]{6,}", "Bearer ***", value, flags=re.I)
    return value[:500]


def _validate_endpoint(
    url: str, provider: str, *, allow_query: bool = False,
) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ProviderConfigurationError(f"{provider} endpoint 必须是有效的 HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ProviderConfigurationError(f"{provider} endpoint 不允许在 URL 中携带凭据")
    if parsed.fragment or (parsed.query and not allow_query):
        raise ProviderConfigurationError(
            f"{provider} endpoint 不允许包含查询参数或片段"
        )
    allow_insecure = os.getenv("TTS_ALLOW_INSECURE_PROVIDER_HTTP", "").lower() in {
        "1", "true", "yes", "on",
    }
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not (allow_insecure or loopback):
        raise ProviderConfigurationError(
            f"{provider} endpoint 必须使用 HTTPS；本地调试可设置 TTS_ALLOW_INSECURE_PROVIDER_HTTP=1"
        )
    return url


def _response_error(response: httpx.Response, provider: str, secrets: tuple[str, ...]) -> str:
    code: object = response.status_code
    message: object = "remote request failed"
    request_id: object = response.headers.get("x-request-id") or response.headers.get("x-tt-logid") or ""
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict):
        metadata = payload.get("ResponseMetadata")
        nested_error = metadata.get("Error") if isinstance(metadata, dict) else None
        if isinstance(nested_error, dict):
            code = nested_error.get("Code") or code
            message = nested_error.get("Message") or message
            request_id = metadata.get("RequestId") or request_id
        else:
            code = payload.get("code") or payload.get("Code") or payload.get("error_code") or code
            message = (
                payload.get("message") or payload.get("Message") or payload.get("error_message")
                or payload.get("msg") or message
            )
            request_id = payload.get("request_id") or payload.get("RequestId") or request_id
    suffix = f"，request_id={_redact(request_id, secrets)}" if request_id else ""
    return f"{provider} 请求失败：HTTP {response.status_code}，code={_redact(code, secrets)}，message={_redact(message, secrets)}{suffix}"


def _request(
    method: str,
    url: str,
    *,
    provider: str,
    headers: Mapping[str, str] | None = None,
    json_body: Mapping[str, Any] | None = None,
    content: bytes | None = None,
    secrets: tuple[str, ...] = (),
    max_response_bytes: int | None = None,
) -> httpx.Response:
    """Make a bounded, retried request without including request data in errors."""

    # Request URLs may contain provider-generated signatures or OpenAPI query
    # parameters. User-configured base endpoints are validated separately and
    # never permit a query or fragment.
    endpoint = _validate_endpoint(url, provider, allow_query=True)
    timeout = _env_float("TTS_HTTP_TIMEOUT_SECONDS", 60.0, 1.0, 600.0)
    retry_count = _env_int("TTS_RETRY_COUNT", 3, 0, 5)
    attempts = retry_count + 1
    response_limit = max_response_bytes or 4 * 1024 * 1024
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=False) as client:
                request_headers = {"Accept-Encoding": "gzip"}
                request_headers.update(headers or {})
                request_kwargs: dict[str, Any] = {"headers": request_headers}
                if content is not None:
                    request_kwargs["content"] = content
                elif json_body is not None:
                    request_kwargs["json"] = json_body
                with client.stream(method, endpoint, **request_kwargs) as incoming:
                    content_length = incoming.headers.get("content-length", "")
                    if content_length.isdigit() and int(content_length) > response_limit:
                        raise ProviderRequestError(
                            f"{provider} 响应超过 {response_limit // 1024 // 1024} MiB 限制"
                        )
                    if incoming.is_stream_consumed:
                        # A custom/mock transport may return a buffered response.
                        # HTTPX has already applied Content-Encoding in that case.
                        response_content = incoming.content
                    else:
                        wire_content = bytearray()
                        for chunk in incoming.iter_raw():
                            wire_content.extend(chunk)
                            if len(wire_content) > response_limit:
                                raise ProviderRequestError(
                                    f"{provider} 响应超过 {response_limit // 1024 // 1024} MiB 限制"
                                )
                        content_encoding = incoming.headers.get("content-encoding", "").strip().lower()
                        if content_encoding in {"", "identity"}:
                            response_content = bytes(wire_content)
                        elif content_encoding == "gzip":
                            try:
                                with gzip.GzipFile(fileobj=io.BytesIO(wire_content)) as compressed:
                                    response_content = compressed.read(response_limit + 1)
                            except (EOFError, OSError, zlib.error) as exc:
                                raise ProviderRequestError(
                                    f"{provider} 返回的 gzip 响应无效"
                                ) from exc
                        else:
                            raise ProviderRequestError(
                                f"{provider} 返回了不支持的 Content-Encoding：{_redact(content_encoding)}"
                            )
                    if len(response_content) > response_limit:
                        raise ProviderRequestError(
                            f"{provider} 解压后响应超过 {response_limit // 1024 // 1024} MiB 限制"
                        )
                    response_headers = {
                        key: value
                        for key, value in incoming.headers.items()
                        if key.lower() not in {"content-encoding", "content-length"}
                    }
                    response = httpx.Response(
                        incoming.status_code,
                        headers=response_headers,
                        content=response_content,
                        request=incoming.request,
                    )
        except httpx.HTTPError as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(min(0.25 * (2**attempt), 2.0))
                continue
            raise ProviderRequestError(
                f"{provider} 网络请求失败：{_redact(type(exc).__name__, secrets)}"
            ) from exc

        if response.status_code == 429 or response.status_code >= 500:
            if attempt + 1 < attempts:
                time.sleep(min(0.25 * (2**attempt), 2.0))
                continue
        if response.status_code < 200 or response.status_code >= 300:
            raise ProviderRequestError(_response_error(response, provider, secrets))
        return response

    raise ProviderRequestError(
        f"{provider} 网络请求失败：{_redact(type(last_error).__name__ if last_error else 'unknown', secrets)}"
    )


def _bounded_audio(audio: bytes, provider: str) -> bytes:
    limit = _max_audio_bytes()
    if not audio:
        raise ProviderRequestError(f"{provider} 返回了空音频")
    if len(audio) > limit:
        raise ProviderRequestError(f"{provider} 返回音频超过 {limit // 1024 // 1024} MiB 限制")
    return audio


def _max_audio_bytes() -> int:
    return _env_int("TTS_MAX_PROVIDER_AUDIO_MB", 32, 1, 512) * 1024 * 1024


def _max_audio_wire_bytes() -> int:
    # JSON responses may carry Base64 audio, which is roughly 4/3 larger than
    # the decoded file. Leave a small allowance for JSON metadata.
    return _max_audio_bytes() * 4 // 3 + 1024 * 1024


def _alibaba_audio_url(value: str) -> str:
    """Validate the provider-returned download URL before any server-side GET."""

    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.username or parsed.password:
        raise ProviderRequestError("阿里云返回的音频 URL 不允许携带 URL 凭据")
    if not (hostname == "aliyuncs.com" or hostname.endswith(".aliyuncs.com")):
        raise ProviderRequestError("阿里云返回了非官方域名的音频 URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProviderRequestError("阿里云返回的音频 URL 端口无效") from exc
    if port not in (None, 80, 443):
        raise ProviderRequestError("阿里云返回的音频 URL 使用了非标准端口")
    if parsed.scheme == "http":
        parsed = parsed._replace(scheme="https", netloc=hostname)
    elif parsed.scheme != "https":
        raise ProviderRequestError("阿里云返回的音频 URL 必须使用 HTTPS")
    return parsed.geturl()


def _decode_base64_audio(value: object, provider: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ProviderRequestError(f"{provider} 响应中缺少音频数据")
    try:
        return _bounded_audio(base64.b64decode(value, validate=True), provider)
    except (ValueError, base64.binascii.Error) as exc:
        raise ProviderRequestError(f"{provider} 返回的音频 Base64 无效") from exc


def _extract_json(response: httpx.Response, provider: str, secrets: tuple[str, ...]) -> dict[str, Any]:
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise ProviderRequestError(f"{provider} 返回了无法解析的 JSON") from exc
    if not isinstance(payload, dict):
        raise ProviderRequestError(f"{provider} 返回的 JSON 结构无效")
    error_code = payload.get("code") or payload.get("Code")
    if error_code not in (None, 0, "0", 200, "200", 3000, "3000"):
        message = payload.get("message") or payload.get("Message") or "remote request failed"
        request_id = payload.get("request_id") or payload.get("RequestId") or ""
        suffix = f"，request_id={_redact(request_id, secrets)}" if request_id else ""
        raise ProviderRequestError(
            f"{provider} 请求失败：code={_redact(error_code, secrets)}，message={_redact(message, secrets)}{suffix}"
        )
    return payload


class BaseTTSProvider:
    """Synchronous provider interface used by the background task worker."""

    provider_id = "base"

    def synthesize(
        self,
        *,
        text: str,
        voice: str,
        model: str,
        speed: float = 1.0,
        pitch: float = 1.0,
        volume: int = 50,
        seed: int = 0,
        instruction: str = "",
    ) -> bytes:
        raise NotImplementedError

    @staticmethod
    def _validate_input(
        text: str, voice: str, speed: float, pitch: float, volume: int,
    ) -> None:
        if not text.strip():
            raise ProviderRequestError("合成文本不能为空")
        if len(text) > 10_000:
            raise ProviderRequestError("合成文本超过 10000 字符限制")
        if not voice.strip():
            raise ProviderRequestError("音色 ID 不能为空")
        if not 0.5 <= speed <= 2.0:
            raise ProviderRequestError("语速必须在 0.5 到 2.0 之间")
        if not 0.5 <= pitch <= 2.0:
            raise ProviderRequestError("音高必须在 0.5 到 2.0 之间")
        if not 0 <= volume <= 100:
            raise ProviderRequestError("音量必须在 0 到 100 之间")


class MockTTSProvider(BaseTTSProvider):
    """Deterministic offline tone synthesizer for integration tests and UI demos."""

    provider_id = "mock"

    def synthesize(
        self,
        *,
        text: str,
        voice: str,
        model: str = "mock-v1",
        speed: float = 1.0,
        pitch: float = 1.0,
        volume: int = 50,
        seed: int = 0,
        instruction: str = "",
    ) -> bytes:
        self._validate_input(text, voice, speed, pitch, volume)
        sample_rate = 16_000
        symbols = [character for character in text.strip() if not character.isspace()]
        symbol_count = max(1, len(symbols))
        duration = max(0.55, min(8.0, 0.13 + symbol_count * 0.19 / speed))
        frame_count = int(round(duration * sample_rate))
        digest = hashlib.sha256(
            f"{text}\0{voice}\0{model}\0{instruction}\0{seed}".encode("utf-8")
        ).digest()
        rng = random.Random(int.from_bytes(digest[:8], "big"))
        voice_offset = int.from_bytes(hashlib.sha256(voice.encode()).digest()[:2], "big") % 90
        base_frequency = (155.0 + voice_offset) * pitch
        segment_length = max(1, frame_count // symbol_count)
        gain = 0.62 * (volume / 100.0)
        samples: list[int] = []

        for index in range(frame_count):
            segment = min(symbol_count - 1, index // segment_length)
            position = (index % segment_length) / segment_length
            edge = min(1.0, position / 0.08, (1.0 - position) / 0.12)
            envelope = max(0.0, edge) * (0.82 + 0.18 * math.sin(math.pi * position))
            symbol = symbols[segment] if symbols else "音"
            symbol_shift = (ord(symbol) % 19 - 9) * 2.4
            frequency = max(80.0, base_frequency + symbol_shift)
            t = index / sample_rate
            phase_jitter = (rng.random() - 0.5) * 0.02
            voiced = (
                math.sin(2.0 * math.pi * frequency * t + phase_jitter)
                + 0.34 * math.sin(2.0 * math.pi * frequency * 2.0 * t)
                + 0.12 * math.sin(2.0 * math.pi * frequency * 3.0 * t)
            ) / 1.46
            breath = (rng.random() * 2.0 - 1.0) * 0.012
            value = max(-1.0, min(1.0, gain * envelope * (voiced + breath)))
            samples.append(int(round(value * 32767.0)))

        output = io.BytesIO()
        with wave.open(output, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(struct.pack(f"<{len(samples)}h", *samples))
        return output.getvalue()


def _ali_service_endpoint(config: Mapping[str, Any], service: str) -> str:
    explicit = str(
        config.get("endpoint" if service == "SpeechSynthesizer" else "customization_endpoint", "")
    ).strip()
    if explicit:
        return explicit

    # An override for one service can also act as a model-studio base from which
    # the sibling service URL is derived. This keeps the UI compact while still
    # allowing a dedicated customization override when required.
    configured_endpoint = str(config.get("endpoint", "")).strip()
    if configured_endpoint:
        base = configured_endpoint.rstrip("/")
        for current in ("SpeechSynthesizer", "customization"):
            suffix = f"/services/audio/tts/{current}"
            if base.endswith(suffix):
                return base[: -len(current)] + service
        if base.endswith("/api/v1"):
            return f"{base}/services/audio/tts/{service}"
        if "/api/v1/" in base:
            return f"{base}/services/audio/tts/{service}"
        return f"{base}/api/v1/services/audio/tts/{service}"

    region = str(config.get("region", "cn-beijing")).strip() or "cn-beijing"
    if region not in {"cn-beijing", "ap-southeast-1"}:
        raise ProviderConfigurationError("阿里云区域仅支持 cn-beijing 或 ap-southeast-1")
    if service == "SpeechSynthesizer" and region != "cn-beijing":
        raise ProviderConfigurationError(
            "阿里云 SpeechSynthesizer 当前仅支持北京区域；如官方另行提供地址，请填写合成 endpoint"
        )
    workspace_id = str(config.get("workspace_id", "")).strip()
    if workspace_id:
        if not re.fullmatch(r"[A-Za-z0-9-]+", workspace_id):
            raise ProviderConfigurationError("阿里云 Workspace ID 格式无效")
        return f"https://{workspace_id}.{region}.maas.aliyuncs.com/api/v1/services/audio/tts/{service}"

    if service == "customization" and region == "ap-southeast-1":
        return "https://dashscope-intl.aliyuncs.com/api/v1/services/audio/tts/customization"

    shared_host = (
        "dashscope-intl.aliyuncs.com"
        if region == "ap-southeast-1"
        else "dashscope.aliyuncs.com"
    )
    return f"https://{shared_host}/api/v1/services/audio/tts/{service}"


class AlibabaTTSProvider(BaseTTSProvider):
    """Alibaba Cloud Model Studio Qwen-Audio-TTS/CosyVoice HTTP adapter."""

    provider_id = "aliyun"

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        settings_store: ProviderSettingsStore | None = None,
    ) -> None:
        self.config = dict(
            config
            if config is not None
            else (settings_store or get_default_settings_store()).get_provider("aliyun")
        )
        self.api_key = str(self.config.get("api_key", "")).strip()
        if not self.api_key:
            raise ProviderConfigurationError("阿里云未配置：请在设置页面填写 API Key")
        self.endpoint = _validate_endpoint(
            _ali_service_endpoint(self.config, "SpeechSynthesizer"), "阿里云"
        )
        self.catalog_complete = False

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def synthesize(
        self,
        *,
        text: str,
        voice: str,
        model: str,
        speed: float = 1.0,
        pitch: float = 1.0,
        volume: int = 50,
        seed: int = 0,
        instruction: str = "",
    ) -> bytes:
        self._validate_input(text, voice, speed, pitch, volume)
        if not model.strip():
            raise ProviderRequestError("阿里云音色必须绑定模型 ID")
        input_data: dict[str, Any] = {
            "text": text,
            "voice": voice,
            "format": "wav",
            "sample_rate": 16_000,
            "volume": volume,
            "rate": speed,
            "pitch": pitch,
            "seed": seed % 65_536,
        }
        if instruction.strip():
            input_data["instruction"] = instruction.strip()
        response = _request(
            "POST", self.endpoint, provider="阿里云", headers=self._headers,
            json_body={"model": model, "input": input_data}, secrets=(self.api_key,),
            max_response_bytes=_max_audio_wire_bytes(),
        )
        content_type = response.headers.get("content-type", "").lower()
        if "audio/" in content_type or response.content.startswith((b"RIFF", b"ID3", b"OggS")):
            return _bounded_audio(response.content, "阿里云")

        payload = _extract_json(response, "阿里云", (self.api_key,))
        output = payload.get("output") if isinstance(payload.get("output"), dict) else {}
        audio = output.get("audio") if isinstance(output.get("audio"), dict) else {}
        encoded = audio.get("data") or output.get("audio_data")
        if encoded:
            return _decode_base64_audio(encoded, "阿里云")
        audio_url = audio.get("url") or output.get("audio_url")
        if not isinstance(audio_url, str) or not audio_url:
            request_id = payload.get("request_id", "")
            suffix = f"，request_id={_redact(request_id)}" if request_id else ""
            raise ProviderRequestError(f"阿里云响应中缺少音频 URL 或数据{suffix}")
        audio_url = _alibaba_audio_url(audio_url)
        download = _request(
            "GET", audio_url, provider="阿里云音频下载", secrets=(self.api_key,),
            max_response_bytes=_max_audio_bytes(),
        )
        return _bounded_audio(download.content, "阿里云")

    def list_custom_voices(self) -> list[dict[str, Any]]:
        """Return custom/designed voices visible to this workspace.

        Both CosyVoice and Qwen voice-design catalogs are queried because their
        request actions and returned voice-ID fields differ.
        """

        endpoint = _validate_endpoint(
            _ali_service_endpoint(self.config, "customization"), "阿里云"
        )
        page_size = 100
        max_pages = 10
        designs = (
            ("voice-enrollment", "list_voice"),
            ("qwen-voice-enrollment", "list"),
            ("qwen-voice-design", "list"),
        )
        result: list[dict[str, Any]] = []
        errors: list[str] = []
        successful_designs = 0
        for design_model, action in designs:
            try:
                for page_index in range(max_pages):
                    response = _request(
                        "POST", endpoint, provider="阿里云音色目录", headers=self._headers,
                        json_body={
                            "model": design_model,
                            "input": {
                                "action": action,
                                "page_size": page_size,
                                "page_index": page_index,
                            },
                        },
                        secrets=(self.api_key,),
                    )
                    payload = _extract_json(response, "阿里云音色目录", (self.api_key,))
                    output = payload.get("output") if isinstance(payload.get("output"), dict) else {}
                    raw_voices = output.get("voice_list") or output.get("voices") or []
                    if not isinstance(raw_voices, list):
                        raise ProviderRequestError(
                            "阿里云音色目录响应中的 voice_list 结构无效"
                        )
                    if page_index == 0:
                        successful_designs += 1
                    for item in raw_voices:
                        if not isinstance(item, dict):
                            continue
                        enriched = dict(item)
                        if design_model == "voice-enrollment" and not enriched.get("target_model"):
                            voice_id = str(
                                enriched.get("voice_id") or enriched.get("voice") or ""
                            ).strip()
                            if voice_id:
                                try:
                                    detail_response = _request(
                                        "POST",
                                        endpoint,
                                        provider="阿里云音色目录",
                                        headers=self._headers,
                                        json_body={
                                            "model": design_model,
                                            "input": {
                                                "action": "query_voice",
                                                "voice_id": voice_id,
                                            },
                                        },
                                        secrets=(self.api_key,),
                                    )
                                    detail_payload = _extract_json(
                                        detail_response, "阿里云音色目录", (self.api_key,)
                                    )
                                    detail_output = detail_payload.get("output")
                                    if isinstance(detail_output, dict):
                                        detail_voice = detail_output.get("voice")
                                        if isinstance(detail_voice, dict):
                                            enriched.update(detail_voice)
                                        else:
                                            enriched.update(detail_output)
                                except ProviderError:
                                    # Keep the list result; model inference in
                                    # voice_catalog will keep it disabled until
                                    # its target model can be verified.
                                    enriched["target_model_query_failed"] = True
                        result.append({**enriched, "design_model": design_model})
                    if len(raw_voices) < page_size:
                        break
            except ProviderError as exc:
                errors.append(f"{design_model}: {exc}")
        if successful_designs == 0 and errors:
            raise ProviderRequestError(
                "阿里云账号音色目录查询全部失败：" + _redact("；".join(errors), (self.api_key,))
            )
        self.catalog_complete = not errors
        return result


# Backwards-compatible class name for code that uses the vendor product name.
AliyunTTSProvider = AlibabaTTSProvider


class VolcTTSProvider(BaseTTSProvider):
    """Volcengine OpenSpeech JSON TTS adapter."""

    provider_id = "volcengine"

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        settings_store: ProviderSettingsStore | None = None,
    ) -> None:
        self.config = dict(
            config
            if config is not None
            else (settings_store or get_default_settings_store()).get_provider("volcengine")
        )
        self.app_id = str(self.config.get("app_id", "")).strip()
        configured_token = str(self.config.get("access_token", "")).strip()
        self.token = re.sub(r"^Bearer;?\s*", "", configured_token, flags=re.I)
        self.cluster = str(self.config.get("cluster", "volcano_tts")).strip() or "volcano_tts"
        if not self.app_id or not self.token:
            raise ProviderConfigurationError(
                "火山引擎未配置：请在设置页面填写 App ID 和 Access Token"
            )
        base = str(self.config.get("endpoint", "")).strip()
        if not base:
            base = "https://openspeech.bytedance.com/api/v1/tts"
        self.endpoint = _validate_endpoint(base, "火山引擎")

    def synthesize(
        self,
        *,
        text: str,
        voice: str,
        model: str = "",
        speed: float = 1.0,
        pitch: float = 1.0,
        volume: int = 50,
        seed: int = 0,
        instruction: str = "",
    ) -> bytes:
        self._validate_input(text, voice, speed, pitch, volume)
        if len(text.encode("utf-8")) > 1024:
            raise ProviderRequestError("火山引擎在线短文本接口单次输入不能超过 1024 字节")
        if model and model not in {
            "volcano_tts", "seed-tts-1.0", "seed-icl-1.0", "voice-clone-v1",
        }:
            raise ProviderRequestError(
                f"火山引擎 V1 不支持模型 {model}；Seed-TTS 2.0 需要 V3 接口"
            )
        if not math.isclose(pitch, 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise ProviderRequestError("火山引擎 V1 暂不支持音高调节，请将音高设为 0 st")
        request_id = str(uuid.uuid4())
        payload = {
            "app": {"appid": self.app_id, "token": self.token, "cluster": self.cluster},
            "user": {"uid": "kws-tts-dataset-app"},
            "audio": {
                "voice_type": voice,
                "encoding": "wav",
                "rate": 16_000,
                "speed_ratio": speed,
                "loudness_ratio": max(0.5, min(2.0, volume / 50.0)),
            },
            "request": {
                "reqid": request_id,
                "text": text,
                "text_type": "plain",
                "operation": "query",
            },
        }
        response = _request(
            "POST", self.endpoint, provider="火山引擎",
            headers={
                "Authorization": f"Bearer;{self.token}",
                "Content-Type": "application/json",
            },
            json_body=payload, secrets=(self.token, self.app_id),
            max_response_bytes=_max_audio_wire_bytes(),
        )
        content_type = response.headers.get("content-type", "").lower()
        if "audio/" in content_type or response.content.startswith((b"RIFF", b"ID3", b"OggS")):
            return _bounded_audio(response.content, "火山引擎")
        body = _extract_json(response, "火山引擎", (self.token, self.app_id))
        code = body.get("code")
        if code not in (None, 0, "0", 3000, "3000"):
            message = body.get("message") or "remote request failed"
            log_id = response.headers.get("x-tt-logid", "")
            suffix = f"，log_id={_redact(log_id)}" if log_id else ""
            raise ProviderRequestError(
                f"火山引擎请求失败：code={_redact(code)}，message={_redact(message, (self.token, self.app_id))}{suffix}"
            )
        return _decode_base64_audio(body.get("data"), "火山引擎")


class VolcVoiceCatalogClient:
    """Volcengine OpenAPI client for public and account speaker catalogs.

    This uses an Access Key pair, which is separate from the AppID/Access Token
    accepted by the legacy V1 synthesis endpoint. Public speakers and legacy
    AppID-bound cloned speakers use different API versions and signing regions.
    """

    service = "speech_saas_prod"
    region = "cn-beijing"
    action = "ListSpeakers"
    version = "2025-05-20"

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        settings_store: ProviderSettingsStore | None = None,
    ) -> None:
        self.config = dict(
            config
            if config is not None
            else (settings_store or get_default_settings_store()).get_provider("volcengine")
        )
        self.access_key_id = str(self.config.get("access_key_id", "")).strip()
        self.access_key_secret = str(self.config.get("access_key_secret", "")).strip()
        if not self.access_key_id or not self.access_key_secret:
            raise ProviderConfigurationError(
                "火山音色目录未配置：请在设置页面填写 Access Key ID 和 Access Key Secret"
            )
        endpoint = str(self.config.get("catalog_endpoint", "")).strip()
        self.endpoint = _validate_endpoint(
            endpoint or "https://open.volcengineapi.com", "火山引擎音色目录"
        )

    @staticmethod
    def _hmac(key: bytes, value: str) -> bytes:
        return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()

    def _signed_request(
        self,
        body: Mapping[str, Any],
        *,
        action: str | None = None,
        version: str | None = None,
        region: str | None = None,
        provider_label: str = "火山引擎音色目录",
    ) -> dict[str, Any]:
        request_action = action or self.action
        request_version = version or self.version
        signing_region = region or self.region
        parsed = urlparse(self.endpoint)
        if parsed.query or parsed.fragment:
            raise ProviderConfigurationError("火山音色目录 endpoint 不允许包含查询参数或片段")
        host = parsed.netloc.lower()
        canonical_uri = parsed.path or "/"
        query_items = sorted((("Action", request_action), ("Version", request_version)))
        canonical_query = urlencode(query_items, quote_via=quote, safe="-_.~")
        request_url = f"{self.endpoint}?{canonical_query}"
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        payload_hash = hashlib.sha256(payload).hexdigest()
        now = datetime.now(timezone.utc)
        x_date = now.strftime("%Y%m%dT%H%M%SZ")
        short_date = now.strftime("%Y%m%d")
        signed_headers = "host;x-content-sha256;x-date"
        canonical_headers = (
            f"host:{host}\n"
            f"x-content-sha256:{payload_hash}\n"
            f"x-date:{x_date}\n"
        )
        canonical_request = (
            f"POST\n{canonical_uri}\n{canonical_query}\n{canonical_headers}\n"
            f"{signed_headers}\n{payload_hash}"
        )
        scope = f"{short_date}/{signing_region}/{self.service}/request"
        string_to_sign = (
            f"HMAC-SHA256\n{x_date}\n{scope}\n"
            f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
        )
        date_key = self._hmac(self.access_key_secret.encode("utf-8"), short_date)
        region_key = self._hmac(date_key, signing_region)
        service_key = self._hmac(region_key, self.service)
        signing_key = self._hmac(service_key, "request")
        signature = hmac.new(
            signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        authorization = (
            f"HMAC-SHA256 Credential={self.access_key_id}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        response = _request(
            "POST",
            request_url,
            provider=provider_label,
            headers={
                "Authorization": authorization,
                "Content-Type": "application/json; charset=UTF-8",
                "Host": host,
                "X-Content-Sha256": payload_hash,
                "X-Date": x_date,
            },
            content=payload,
            secrets=(self.access_key_id, self.access_key_secret),
        )
        try:
            result = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderRequestError(f"{provider_label}返回了无法解析的 JSON") from exc
        if not isinstance(result, dict):
            raise ProviderRequestError(f"{provider_label}返回的 JSON 结构无效")
        metadata = result.get("ResponseMetadata")
        error = metadata.get("Error") if isinstance(metadata, dict) else None
        if isinstance(error, dict):
            code = _redact(error.get("Code", "unknown"), (self.access_key_secret,))
            message = _redact(error.get("Message", "remote request failed"), (self.access_key_secret,))
            request_id = _redact(metadata.get("RequestId", ""), (self.access_key_secret,))
            suffix = f"，request_id={request_id}" if request_id else ""
            raise ProviderRequestError(
                f"{provider_label}请求失败：code={code}，message={message}{suffix}"
            )
        return result

    def list_speakers(
        self,
        *,
        resource_ids: tuple[str, ...] = ("seed-tts-1.0",),
        page_size: int = 100,
        max_pages: int = 100,
    ) -> list[dict[str, Any]]:
        """Return all public speakers visible to the Access Key account."""

        page_size = max(1, min(100, page_size))
        max_pages = max(1, min(100, max_pages))
        speakers: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            payload = self._signed_request(
                {"ResourceIDs": list(resource_ids), "Page": page, "Limit": page_size}
            )
            result = payload.get("Result")
            if not isinstance(result, dict):
                raise ProviderRequestError("火山引擎音色目录响应中缺少 Result")
            raw_speakers = result.get("Speakers") or []
            if not isinstance(raw_speakers, list):
                raise ProviderRequestError("火山引擎音色目录响应中的 Speakers 结构无效")
            speakers.extend(item for item in raw_speakers if isinstance(item, dict))
            total = result.get("Total")
            if len(raw_speakers) < page_size:
                break
            if isinstance(total, int) and len(speakers) >= total:
                break
        return speakers

    def list_account_speakers(
        self,
        app_id: str,
        *,
        page_size: int = 100,
        max_pages: int = 100,
    ) -> list[dict[str, Any]]:
        """Return cloned speakers owned by an AppID using the documented legacy API."""

        normalized_app_id = str(app_id).strip()
        if not normalized_app_id:
            raise ProviderConfigurationError("火山账号复刻音色查询缺少 App ID")
        if len(normalized_app_id) > 128:
            raise ProviderConfigurationError("火山 App ID 长度无效")
        page_size = max(1, min(100, page_size))
        max_pages = max(1, min(100, max_pages))
        statuses: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            payload = self._signed_request(
                {
                    "AppID": normalized_app_id,
                    "PageNumber": page,
                    "PageSize": page_size,
                },
                action="BatchListMegaTTSTrainStatus",
                version="2023-11-07",
                region="cn-north-1",
                provider_label="火山引擎账号复刻音色",
            )
            result = payload.get("Result")
            if not isinstance(result, dict):
                raise ProviderRequestError("火山引擎账号复刻音色响应中缺少 Result")
            raw_statuses = result.get("Statuses") or []
            if not isinstance(raw_statuses, list):
                raise ProviderRequestError("火山引擎账号复刻音色响应中的 Statuses 结构无效")
            statuses.extend(item for item in raw_statuses if isinstance(item, dict))
            total_raw = result.get("TotalCount")
            try:
                total = int(total_raw) if total_raw is not None else None
            except (TypeError, ValueError):
                total = None
            if len(raw_statuses) < page_size or (total is not None and len(statuses) >= total):
                break
        return statuses


def _cloud_configured(
    provider_id: str, settings_store: ProviderSettingsStore | None = None
) -> bool:
    config = (settings_store or get_default_settings_store()).get_provider(provider_id)
    if provider_id == "aliyun":
        return bool(config.get("api_key"))
    if provider_id == "volcengine":
        return bool(config.get("app_id") and config.get("access_token"))
    return True


def validate_provider_configuration(
    name: str,
    config: Mapping[str, Any],
    *,
    require_credentials: bool = True,
) -> None:
    """Validate stored configuration without making a billable cloud request."""

    normalized = name.strip().lower().replace("-", "_")
    if normalized in {"aliyun", "alibaba", "ali", "dashscope"}:
        workspace_id = str(config.get("workspace_id", "")).strip()
        if workspace_id and not re.fullmatch(r"[A-Za-z0-9-]+", workspace_id):
            raise ProviderConfigurationError("阿里云 Workspace ID 格式无效")
        region = str(config.get("region", "cn-beijing")).strip() or "cn-beijing"
        if region != "cn-beijing":
            raise ProviderConfigurationError(
                "阿里云 SpeechSynthesizer 当前仅支持 cn-beijing"
            )
        for field, label in (
            ("endpoint", "阿里云合成"),
            ("customization_endpoint", "阿里云音色查询"),
        ):
            endpoint = str(config.get(field, "")).strip()
            if endpoint:
                _validate_endpoint(endpoint, label)
        if require_credentials:
            AlibabaTTSProvider(config=config)
        return
    if normalized in {"volcengine", "volc", "doubao", "bytedance"}:
        for field, label in (
            ("endpoint", "火山引擎合成"),
            ("catalog_endpoint", "火山引擎音色目录"),
        ):
            endpoint = str(config.get(field, "")).strip()
            if endpoint:
                _validate_endpoint(endpoint, label)
        if require_credentials:
            synthesis_present = bool(config.get("app_id") or config.get("access_token"))
            catalog_present = bool(
                config.get("access_key_id") or config.get("access_key_secret")
            )
            if not synthesis_present and not catalog_present:
                raise ProviderConfigurationError(
                    "火山引擎未配置：合成需要 App ID/Access Token，目录需要 AK/SK"
                )
            if synthesis_present:
                VolcTTSProvider(config=config)
            if catalog_present:
                VolcVoiceCatalogClient(config=config)
        return
    raise ProviderConfigurationError(f"不支持的 TTS 供应商：{_redact(name)}")


def available_providers(
    settings_store: ProviderSettingsStore | None = None,
) -> list[ProviderInfo]:
    """Describe adapters without exposing any credential values."""

    aliyun_configured = _cloud_configured("aliyun", settings_store)
    volc_configured = _cloud_configured("volcengine", settings_store)
    volc_config = (settings_store or get_default_settings_store()).get_provider("volcengine")
    volc_catalog_configured = bool(
        volc_config.get("access_key_id") and volc_config.get("access_key_secret")
    )
    return [
        ProviderInfo(
            id="mock", name="离线 Mock", configured=True, available=True,
            supports_voice_query=True, supports_seed=True,
            detail="离线生成 16 kHz 单声道 PCM16 WAV，用于验证完整流程，不代表真实人声",
            synthesis_configured=True, catalog_configured=True,
        ),
        ProviderInfo(
            id="volcengine", name="火山引擎", configured=volc_configured,
            available=volc_configured, supports_voice_query=volc_catalog_configured,
            supports_seed=False,
            detail=(
                "合成使用 App ID/Access Token；公共音色目录使用独立的 Access Key ID/Secret"
            ),
            synthesis_configured=volc_configured,
            catalog_configured=volc_catalog_configured,
        ),
        ProviderInfo(
            id="aliyun", name="阿里云百炼", configured=aliyun_configured,
            available=aliyun_configured, supports_voice_query=aliyun_configured,
            supports_seed=True,
            detail="支持 CosyVoice/Qwen HTTP 合成，并可查询工作空间自定义音色",
            synthesis_configured=aliyun_configured,
            catalog_configured=aliyun_configured,
        ),
    ]


def get_provider(
    name: str, settings_store: ProviderSettingsStore | None = None
) -> BaseTTSProvider:
    """Construct a provider using a stable ID or accepted vendor alias."""

    normalized = name.strip().lower().replace("-", "_")
    if normalized in {"mock", "offline", "demo"}:
        return MockTTSProvider()
    if normalized in {"aliyun", "alibaba", "ali", "dashscope"}:
        return AlibabaTTSProvider(settings_store=settings_store)
    if normalized in {"volcengine", "volc", "doubao", "bytedance"}:
        return VolcTTSProvider(settings_store=settings_store)
    raise ProviderConfigurationError(f"不支持的 TTS 供应商：{_redact(name)}")


__all__ = [
    "AlibabaTTSProvider",
    "AliyunTTSProvider",
    "BaseTTSProvider",
    "MockTTSProvider",
    "ProviderConfigurationError",
    "ProviderError",
    "ProviderInfo",
    "ProviderRequestError",
    "VolcTTSProvider",
    "VolcVoiceCatalogClient",
    "available_providers",
    "get_provider",
    "validate_provider_configuration",
]
