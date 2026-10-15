"""Documented customization protocols; no cloud mutations are retried.

This module deals with vendor IDs and bytes only. The manager owns persistence,
speaker provenance and task lifetime; the Qt layer never sees reference data.
"""
from __future__ import annotations

import base64
import re
import uuid
from pathlib import Path
from urllib.parse import urlparse

from providers import (AlibabaTTSProvider, XiaomiTTSProvider, ProviderRequestError,
                       _request, _extract_json, _decode_base64_audio,
                       _ali_service_endpoint, _max_audio_wire_bytes)

ALI_MODELS = ("cosyvoice-v3.5-plus", "cosyvoice-v3.5-flash", "cosyvoice-v3-flash", "cosyvoice-v3-plus")
VOLC_MODELS = {"volcengine_v1": "seed-icl-1.0", "volcengine_v2": "seed-icl-2.0"}


def validate_options(value, settings):
    service, mode = value.get("service"), value.get("mode")
    if service not in {*VOLC_MODELS, "xiaomi", "aliyun", "mock"} or mode not in {"clone", "design"}:
        raise ValueError("请选择有效的供应商和创建方式")
    if not str(value.get("name", "")).strip() or len(value["name"]) > 80:
        raise ValueError("音色名称需为 1–80 个字符")
    if not str(value.get("preview_text", "")).strip() or len(value["preview_text"]) > 200:
        raise ValueError("预览文本需为 1–200 个字符")
    if mode == "design" and not str(value.get("prompt", "")).strip():
        raise ValueError("请填写声音描述")
    if len(value.get("prompt", "")) > (200 if service in VOLC_MODELS else 500):
        raise ValueError("声音描述过长（豆包最多 200 字，其他供应商最多 500 字）")
    if service in VOLC_MODELS:
        if mode == "clone" and len(value["preview_text"].strip()) < 4:
            raise ValueError("豆包复刻的预览文本至少 4 字")
        if not re.fullmatch(r"S_[A-Za-z0-9_\-]{1,120}", value.get("speaker_id", "")):
            raise ValueError("请填写控制台已购买的 Speaker ID（S_ 开头）")
    if service != "mock":
        config_id = "volcengine" if service == "volcengine_v1" else service
        key = str(settings.get_provider(config_id).get("api_key") or "").strip()
        if not key or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise ValueError("请先在设置填写所选供应商的有效 API Key")
    if service == "aliyun":
        if value.get("model") not in ALI_MODELS:
            raise ValueError("请选择支持自定义音色的 CosyVoice 模型")
        config = settings.get_provider("aliyun")
        if mode == "design" and config.get("region", "cn-beijing") != "cn-beijing":
            raise ValueError("阿里云声音设计仅支持北京地域，请先修改供应商设置")
        if mode == "design" and len(value["preview_text"].strip()) < 15:
            raise ValueError("阿里云设计的预览文本需为 15–200 个字符")
        if mode == "clone":
            parsed = urlparse(value.get("reference_url", ""))
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("阿里云 CosyVoice 复刻需要可公网访问的 HTTPS 音频链接")


def _volc(value, settings, action, extra=None):
    config = settings.get_provider("volcengine" if value["service"] == "volcengine_v1" else "volcengine_v2")
    key = str(config.get("api_key") or "").strip()
    if not key or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ProviderRequestError("请先在设置填写对应豆包版本的 API Key")
    body = {"speaker_id": value["remote_voice_id"] if action == "get_voice" else value["speaker_id"], **(extra or {})}
    response = _request("POST", "https://openspeech.bytedance.com/api/v3/tts/"+action,
                        provider="豆包音色", headers={"X-Api-Key": key, "Content-Type": "application/json", "X-Api-Request-Id": str(uuid.uuid4())},
                        json_body=body, secrets=(key,), retry_transient=False)
    result = _extract_json(response, "豆包音色", (key,))
    if result.get("code") not in (None, 0, 20000000):
        raise ProviderRequestError("豆包未完成音色操作，请在控制台检查状态")
    status = {0: "missing", 1: "pending", 2: "ready", 3: "failed", 4: "ready"}.get(result.get("status"), "unknown")
    previews = result.get("speaker_status", [])
    wanted = {1, 2, 3} if value["service"] == "volcengine_v1" else {4, 5}
    preview = result.get("demo_audio") or next((r.get("demo_audio") for r in previews if isinstance(r, dict) and r.get("model_type") in wanted), "")
    return {"voice_id": str(result.get("speaker_id") or body["speaker_id"]), "status": status,
            "preview_url": preview, "remaining": result.get("available_training_times")}


def _ali(value, settings, action, extra=None):
    provider = AlibabaTTSProvider(settings_store=settings)
    response = _request("POST", _ali_service_endpoint(provider.config, "customization"), provider="阿里云自定义音色",
                        headers=provider._headers, json_body={"model": "voice-enrollment", "input": {"action": action, **(extra or {})}},
                        secrets=(provider.api_key,), max_response_bytes=_max_audio_wire_bytes(), retry_transient=False)
    output = _extract_json(response, "阿里云自定义音色", (provider.api_key,)).get("output")
    if not isinstance(output, dict):
        raise ProviderRequestError("阿里云自定义音色响应缺少 output")
    return output


def create_voice(value, reference: Path | None, settings):
    service, mode = value["service"], value["mode"]
    if service in VOLC_MODELS:
        extra = {"language": 0 if value.get("language", "zh") == "zh" else 1}
        if mode == "clone":
            extra.update(audio={"data": base64.b64encode(reference.read_bytes()).decode("ascii"), "format": "wav"},
                         extra_params={"demo_text": value["preview_text"]})
        else:
            extra.update(prompt={"text_prompt": value["prompt"]}, text=value["preview_text"])
        return _volc(value, settings, "voice_clone" if mode == "clone" else "voice_design", extra)
    if service == "aliyun":
        data = {"target_model": value["model"], "prefix": "tts"+uuid.uuid4().hex[:7], "language_hints": [value.get("language", "zh")]}
        if mode == "clone":
            data["url"] = value["reference_url"]
        else:
            data.update(voice_prompt=value["prompt"], preview_text=value["preview_text"])
        output = _ali(value, settings, "create_voice", data)
        identifier = output.get("voice_id")
        if not isinstance(identifier, str) or not identifier:
            raise ProviderRequestError("阿里云未返回音色 ID；请先检查控制台，不要盲目重建")
        result = {"voice_id": identifier, "status": "pending"}
        audio = output.get("preview_audio", {})
        if audio.get("data"):
            result["preview_base64"] = audio["data"]
        return result
    if service == "xiaomi":
        provider = XiaomiTTSProvider(settings_store=settings)
        if mode == "clone":
            voice = "data:audio/wav;base64,"+base64.b64encode(reference.read_bytes()).decode("ascii")
            model, prompt = "mimo-v2.5-tts-voiceclone", ""
        else:
            voice, model, prompt = "", "mimo-v2.5-tts-voicedesign", value["prompt"]
        audio = provider.synthesize(text=value["preview_text"], voice=voice, model=model, instruction=prompt)
        return {"voice_id": value["id"], "status": "ready", "preview": audio}
    from providers import MockTTSProvider
    return {"voice_id": value["id"], "status": "ready", "preview": MockTTSProvider().synthesize(text=value["preview_text"], voice="mock", model="mock")}


def query_voice(value, settings):
    if value["service"] in VOLC_MODELS:
        return _volc(value, settings, "get_voice")
    if value["service"] == "aliyun":
        output = _ali(value, settings, "query_voice", {"voice_id": value["remote_voice_id"]})
        if output.get("target_model") and output["target_model"] != value["model"]:
            raise ProviderRequestError("云端音色模型与本地绑定不同，请检查控制台")
        return {"voice_id": value["remote_voice_id"], "status": {"OK": "ready", "DEPLOYING": "pending", "UNDEPLOYED": "failed"}.get(output.get("status"), "unknown")}
    return {"voice_id": value["remote_voice_id"], "status": value["status"]}


def download_preview(url):
    # Only provider-owned result hosts, never credentials, arbitrary URLs or
    # redirects. Signed URLs live only in this worker frame, not task JSON.
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or not any((parsed.hostname or "").endswith("."+h) or parsed.hostname == h for h in ("bytespeech.com", "aliyuncs.com", "volces.com")):
        raise ProviderRequestError("供应商预览地址不在允许的 HTTPS 音频域名中")
    return _request("GET", url, provider="音色预览下载", max_response_bytes=10*1024*1024, retry_transient=False).content
