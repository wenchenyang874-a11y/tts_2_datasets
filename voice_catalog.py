"""Versioned Chinese-friendly voice catalog for all TTS adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

try:
    from .providers import (
        AlibabaTTSProvider,
        ProviderConfigurationError,
        ProviderError,
        VolcVoiceCatalogClient,
        available_providers,
    )
    from .settings_store import ProviderSettingsStore
except ImportError:  # Support direct ``uvicorn app:app`` execution.
    from providers import (  # type: ignore
        AlibabaTTSProvider,
        ProviderConfigurationError,
        ProviderError,
        VolcVoiceCatalogClient,
        available_providers,
    )
    from settings_store import ProviderSettingsStore  # type: ignore


CATALOG_VERSION = "2026-10-13"


@dataclass(frozen=True)
class Voice:
    """A model-bound voice entry safe to persist in SQLite."""

    provider: str
    voice_id: str
    name: str
    model: str
    gender: str = "unknown"
    age_group: str = "unknown"
    languages: list[str] = field(default_factory=lambda: ["zh"])
    source: str = "builtin"
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


def _voice(
    provider: str,
    voice_id: str,
    name: str,
    model: str,
    gender: str,
    age_group: str,
    *,
    description: str,
    languages: list[str] | None = None,
    source_url: str = "",
    age_range: str = "",
) -> Voice:
    metadata: dict[str, Any] = {
        "description": description,
        "catalog_version": CATALOG_VERSION,
        "classification_source": "curated_from_vendor_description",
    }
    if source_url:
        metadata["source_url"] = source_url
    if age_range:
        metadata["age_range"] = age_range
    return Voice(
        provider=provider,
        voice_id=voice_id,
        name=name,
        model=model,
        gender=gender,
        age_group=age_group,
        languages=languages if languages is not None else ["zh-CN"],
        metadata=metadata,
    )


_ALIYUN_SOURCE = "https://help.aliyun.com/zh/model-studio/cosyvoice-voice-list"
_VOLC_SOURCE = "https://www.volcengine.com/docs/6561/2228192"


_ALIYUN_SYSTEM_VOICES: tuple[tuple[str, str, str, str, str, str], ...] = (
    ("longanyang", "龙安洋", "cosyvoice-v3-flash", "male", "young", "20-30"),
    ("longanhuan", "龙安欢", "cosyvoice-v3-flash", "female", "young", "20-30"),
    ("longhuhu_v3", "龙呼呼", "cosyvoice-v3-flash", "female", "child", "6-10"),
    ("longjielidou_v3", "龙杰力豆", "cosyvoice-v3-flash", "male", "child", "10"),
    ("longniuniu_v3", "龙牛牛", "cosyvoice-v3-flash", "male", "child", "6-15"),
    ("longyingxiao_v3", "龙应笑", "cosyvoice-v3-flash", "female", "young", "20-25"),
    ("longyingxun_v3", "龙应询", "cosyvoice-v3-flash", "male", "young", "20-25"),
    ("longyingjing_v3", "龙应静", "cosyvoice-v3-flash", "female", "young", "25-35"),
    ("longxiaochun_v3", "龙小淳", "cosyvoice-v3-flash", "female", "young", "25-30"),
    ("longxiaoxia_v3", "龙小夏", "cosyvoice-v3-flash", "female", "young", "25-30"),
    ("longanyun_v3", "龙安昀", "cosyvoice-v3-flash", "male", "young", "30-35"),
    ("longanlang_v3", "龙安朗", "cosyvoice-v3-flash", "male", "young", "20-25"),
    ("longyingmu_v3", "龙应沐", "cosyvoice-v3-flash", "female", "young", "25-30"),
    ("longcheng_v3", "龙橙", "cosyvoice-v3-flash", "male", "young", "20-25"),
    ("longze_v3", "龙泽", "cosyvoice-v3-flash", "male", "young", "25-30"),
    ("longyan_v3", "龙颜", "cosyvoice-v3-flash", "female", "young", "30-35"),
    ("longtian_v3", "龙天", "cosyvoice-v3-flash", "male", "young", "30-35"),
    ("longwan_v3", "龙婉", "cosyvoice-v3-flash", "female", "young", "20-30"),
    ("longsanshu_v3", "龙三叔", "cosyvoice-v3-flash", "male", "middle", "25-45"),
    ("longyuan_v3", "龙媛", "cosyvoice-v3-flash", "female", "middle", "35-40"),
    ("longlaobo_v3", "龙老伯", "cosyvoice-v3-flash", "male", "senior", "60+"),
    ("longlaoyi_v3", "龙老姨", "cosyvoice-v3-flash", "female", "senior", "60+"),
    ("longshuo_v3", "龙硕", "cosyvoice-v3-flash", "male", "young", "25-30"),
    ("loongbella_v3", "Bella3.0", "cosyvoice-v3-flash", "female", "young", "25-30"),
    ("longanyang", "龙安洋", "cosyvoice-v3-plus", "male", "young", "20-30"),
    ("longanhuan", "龙安欢", "cosyvoice-v3-plus", "female", "young", "20-30"),
    ("longanli", "龙安莉", "cosyvoice-v2", "female", "unknown", "unknown"),
    ("longanlang", "龙安朗", "cosyvoice-v2", "male", "unknown", "unknown"),
    ("longhuhu", "龙呼呼", "cosyvoice-v2", "female", "child", "child"),
    ("longniuniu", "龙牛牛", "cosyvoice-v2", "male", "child", "child"),
)


_BUILTIN_VOICES: tuple[Voice, ...] = (
    *(
        _voice("xiaomi", identifier, name, "mimo-v2.5-tts", gender, "unknown",
               description="小米 MiMo 官方预置音色；实际可用性由账号和集群决定。", languages=languages,
               source_url="https://mimo.mi.com/docs/zh-CN/api/audio/tts")
        for identifier, name, gender, languages in (
            ("mimo_default", "MiMo 默认", "unknown", ["und"]),
            ("冰糖", "冰糖", "female", ["zh-CN"]), ("茉莉", "茉莉", "female", ["zh-CN"]),
            ("苏打", "苏打", "male", ["zh-CN"]), ("白桦", "白桦", "male", ["zh-CN"]),
            ("Mia", "Mia", "female", ["en"]), ("Chloe", "Chloe", "female", ["en"]),
            ("Milo", "Milo", "male", ["en"]), ("Dean", "Dean", "male", ["en"]),
        )
    ),
    _voice("volcengine", "zh_female_vv_uranus_bigtts", "Vivi 2.0", "seed-tts-2.0", "female", "unknown",
           description="豆包语音 2.0 官方接口示例音色；需开通对应资源。",
           source_url="https://docs.volcengine.com/docs/DoubaoVoice/unidirectional-streaming-text-to-speech-http?lang=zh"),
    _voice("volcengine", "zh_female_xueayi_saturn_bigtts", "儿童绘本 2.0", "seed-tts-2.0", "female", "unknown",
           description="豆包语音 2.0 儿童绘本音色；不将应用场景误标为儿童说话人。",
           source_url="https://www.volcengine.com/docs/6561/162929"),
    _voice(
        "mock", "mock_female_young", "Mock 青年女声", "mock-v1", "female", "young",
        description="离线流程验证音色，不代表真人或云端音色。",
    ),
    _voice(
        "mock", "mock_male_young", "Mock 青年男声", "mock-v1", "male", "young",
        description="离线流程验证音色，不代表真人或云端音色。",
    ),
    _voice(
        "mock", "mock_female_middle", "Mock 中年女声", "mock-v1", "female", "middle",
        description="离线流程验证音色，不代表真人或云端音色。",
    ),
    _voice(
        "mock", "mock_male_middle", "Mock 中年男声", "mock-v1", "male", "middle",
        description="离线流程验证音色，不代表真人或云端音色。",
    ),
    *(
        _voice(
            "aliyun", voice_id, name, model, gender, age_group,
            description="阿里云 CosyVoice 官方系统音色，具体语言和调用权限以控制台为准。",
            languages=["zh-CN", "en"], source_url=_ALIYUN_SOURCE, age_range=age_range,
        )
        for voice_id, name, model, gender, age_group, age_range in _ALIYUN_SYSTEM_VOICES
    ),
    _voice(
        "volcengine", "zh_female_cancan_mars_bigtts", "灿灿", "volcano_tts",
        "female", "young", description="火山引擎大模型中文女声；调用权限以控制台为准。",
        source_url=_VOLC_SOURCE,
    ),
    _voice(
        "volcengine", "zh_male_M392_conversation_wvae_bigtts", "M392 对话男声",
        "volcano_tts", "male", "middle",
        description="火山引擎官方接口示例中文男声；调用权限以控制台为准。",
        source_url=_VOLC_SOURCE,
    ),
)


def _canonical_provider(provider: str | None) -> str | None:
    if provider is None or not provider.strip() or provider.strip().lower() in {"all", "*"}:
        return None
    normalized = provider.strip().lower().replace("-", "_")
    aliases = {
        "alibaba": "aliyun",
        "ali": "aliyun",
        "dashscope": "aliyun",
        "volc": "volcengine",
        "doubao": "volcengine",
        "offline": "mock",
        "demo": "mock",
        "mimo": "xiaomi",
    }
    normalized = aliases.get(normalized, normalized)
    supported = {"mock", "aliyun", "volcengine", "xiaomi"}
    if normalized not in supported:
        raise ProviderConfigurationError(f"不支持的音色目录供应商：{provider}")
    return normalized


def builtin_voices(provider: str | None = None) -> list[Voice]:
    """Return a copy of the maintained, model-bound public catalog."""

    selected = _canonical_provider(provider)
    return [item for item in _BUILTIN_VOICES if selected is None or item.provider == selected]


def list_voices(
    provider: str | None = None,
    *,
    gender: str = "",
    age_group: str = "",
) -> list[Voice]:
    result = builtin_voices(provider)
    if gender:
        result = [item for item in result if item.gender == gender]
    if age_group:
        result = [item for item in result if item.age_group == age_group]
    return result


def get_voice_catalog(provider: str | None = None) -> list[Voice]:
    """Compatibility alias used by scripts that only need the built-in catalog."""

    return builtin_voices(provider)


def _infer_custom_model(voice_id: str, item: dict[str, Any]) -> str:
    if item.get("target_model_query_failed"):
        return "unknown"
    target = str(item.get("target_model") or "").strip()
    if target:
        return target
    if "-vd-" in voice_id and voice_id.startswith("cosyvoice-"):
        return voice_id.split("-vd-", 1)[0]
    if str(item.get("design_model") or "").startswith("qwen-"):
        return "unsupported-qwen3-tts"
    return "cosyvoice-v3.5-plus"


def _speech_synthesizer_compatible(model: str) -> bool:
    normalized = model.strip().lower()
    return normalized.startswith(("cosyvoice-v2", "cosyvoice-v3", "qwen-audio-3"))


def _classify_custom_voice(item: dict[str, Any]) -> tuple[str, str]:
    """Conservatively classify explicit descriptions; unknown beats a false claim."""

    text = " ".join(
        str(item.get(key, "")) for key in ("voice_prompt", "description", "name")
    ).lower()
    gender = "unknown"
    if any(token in text for token in ("女性", "女声", "female", "woman", "girl")):
        gender = "female"
    elif any(token in text for token in ("男性", "男声", "male", "man", "boy")):
        gender = "male"

    age_group = "unknown"
    if any(token in text for token in ("儿童", "child", "kid")):
        age_group = "child"
    elif any(token in text for token in ("少年", "teen")):
        age_group = "teen"
    elif any(token in text for token in ("青年", "年轻", "young")):
        age_group = "young"
    elif any(token in text for token in ("中年", "middle-aged", "middle aged")):
        age_group = "middle"
    elif any(token in text for token in ("老年", "老人", "senior", "elderly")):
        age_group = "senior"
    return gender, age_group


def query_alibaba_custom_voices(
    settings_store: ProviderSettingsStore | None = None,
    refresh_state: dict[str, Any] | None = None,
) -> list[Voice]:
    """Query model-bound custom voices from the configured Alibaba workspace."""

    provider = AlibabaTTSProvider(settings_store=settings_store)
    result: list[Voice] = []
    for item in provider.list_custom_voices():
        voice_id = str(item.get("voice_id") or item.get("voice") or "").strip()
        if not voice_id:
            continue
        model = _infer_custom_model(voice_id, item)
        synthesis_compatible = _speech_synthesizer_compatible(model)
        gender, age_group = _classify_custom_voice(item)
        language = str(item.get("language") or "").strip()
        status = str(item.get("status") or "OK").upper()
        metadata = {
            "catalog_version": CATALOG_VERSION,
            "classification_source": "inferred_from_custom_voice_prompt" if gender != "unknown" or age_group != "unknown" else "unknown",
            "design_model": item.get("design_model", ""),
            "status": status,
            "gmt_create": item.get("gmt_create", ""),
            "gmt_modified": item.get("gmt_modified", ""),
            "voice_prompt": item.get("voice_prompt", ""),
            "preview_text": item.get("preview_text", ""),
            "synthesis_compatible": synthesis_compatible,
        }
        result.append(
            Voice(
                provider="aliyun",
                voice_id=voice_id,
                name=str(item.get("preferred_name") or item.get("name") or voice_id),
                model=model,
                gender=gender,
                age_group=age_group,
                languages=[language] if language else [],
                source="account_custom",
                enabled=(
                    synthesis_compatible
                    and status in {"OK", "DEPLOYED", "SUCCESS"}
                ),
                metadata=metadata,
            )
        )
    if refresh_state is not None and provider.catalog_complete:
        refresh_state.setdefault("complete_snapshots", []).append(
            {"provider": "aliyun", "source": "account_custom"}
        )
    return result


def _volc_gender(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"女", "女性", "female", "woman", "girl"}:
        return "female"
    if normalized in {"男", "男性", "male", "man", "boy"}:
        return "male"
    return "unknown"


def _volc_age_group(value: object) -> str:
    normalized = str(value or "").strip().lower()
    mapping = {
        "儿童": "child", "child": "child",
        "少年": "teen", "teen": "teen",
        "青年": "young", "young": "young",
        "中年": "middle", "middle": "middle",
        "老年": "senior", "senior": "senior",
    }
    return mapping.get(normalized, "unknown")


def query_volc_public_voices(
    settings_store: ProviderSettingsStore | None = None,
    refresh_state: dict[str, Any] | None = None,
) -> list[Voice]:
    """Query both model-bound public catalogs using independent AK/SK auth."""

    client = VolcVoiceCatalogClient(settings_store=settings_store)
    result: list[Voice] = []
    for item in client.list_speakers(resource_ids=("seed-tts-1.0", "seed-tts-2.0")):
        voice_id = str(item.get("VoiceType") or "").strip()
        if not voice_id:
            continue
        language_entries = item.get("Languages")
        languages: list[str] = []
        if isinstance(language_entries, list):
            for language in language_entries:
                if isinstance(language, dict):
                    value = str(language.get("Language") or "").strip()
                    if value:
                        languages.append(value)
        metadata = {
            "catalog_version": CATALOG_VERSION,
            "classification_source": "volcengine_list_speakers",
            "description": str(item.get("Description") or ""),
            "resource_id": str(item.get("ResourceID") or "seed-tts-1.0"),
            "normal_labels": item.get("NormalLabels") or [],
            "special_labels": item.get("SpecialLabels") or [],
            "emotions": item.get("Emotions") or [],
            "source_url": "https://api.volcengine.com/api-docs/view?action=ListSpeakers&serviceCode=speech_saas_prod&version=2025-05-20",
        }
        result.append(
            Voice(
                provider="volcengine",
                voice_id=voice_id,
                name=str(item.get("Name") or voice_id),
                model=str(item.get("ResourceID") or "seed-tts-1.0"),
                gender=_volc_gender(item.get("Gender")),
                age_group=_volc_age_group(item.get("Age")),
                languages=languages,
                source="provider_catalog",
                enabled=True,
                metadata=metadata,
            )
        )
    if refresh_state is not None:
        refresh_state.setdefault("complete_snapshots", []).append(
            {"provider": "volcengine", "source": "provider_catalog"}
        )
    return result


def query_volc_account_voices(
    settings_store: ProviderSettingsStore | None = None,
    refresh_state: dict[str, Any] | None = None,
) -> list[Voice]:
    """Query AppID-bound cloned speakers with the legacy account management API."""

    if settings_store is None:
        raise ProviderConfigurationError("火山账号复刻音色查询缺少设置存储")
    config = settings_store.get_provider("volcengine")
    app_id = str(config.get("app_id") or "").strip()
    client = VolcVoiceCatalogClient(settings_store=settings_store)
    result: list[Voice] = []
    for item in client.list_account_speakers(app_id):
        speaker_id = str(item.get("SpeakerID") or "").strip()
        if not speaker_id:
            continue
        state = str(item.get("State") or "Unknown").strip()
        model_details = item.get("ModelTypeDetails")
        details = [entry for entry in model_details if isinstance(entry, dict)] \
            if isinstance(model_details, list) else []
        resource_ids = {
            str(entry.get("ResourceID") or "").strip()
            for entry in details
            if str(entry.get("ResourceID") or "").strip()
        }
        if not resource_ids:
            resource_ids = {"voice-clone-v1"}
        for resource_id in sorted(resource_ids):
            synthesis_compatible = resource_id in {"seed-icl-1.0", "voice-clone-v1"}
            metadata = {
                "catalog_version": CATALOG_VERSION,
                "classification_source": "volcengine_account_voice",
                "source_url": "https://www.volcengine.com/docs/6561/2235883",
                "state": state,
                "instance_no": str(item.get("InstanceNO") or ""),
                "is_activable": item.get("IsActivable"),
                "demo_audio": str(item.get("DemoAudio") or ""),
                "version": str(item.get("Version") or ""),
                "create_time": item.get("CreateTime"),
                "expire_time": item.get("ExpireTime"),
                "order_time": item.get("OrderTime"),
                "available_training_times": item.get("AvailableTrainingTimes"),
                "resource_id": resource_id,
                "model_type_details": details,
                "synthesis_compatible": synthesis_compatible,
            }
            result.append(
                Voice(
                    provider="volcengine",
                    voice_id=speaker_id,
                    name=str(item.get("Alias") or f"复刻音色 {speaker_id}"),
                    model=resource_id,
                    gender="unknown",
                    age_group="unknown",
                    languages=["zh-CN"],
                    source="account_clone",
                    enabled=synthesis_compatible and state in {"Success", "Active"},
                    metadata=metadata,
                )
            )
    if refresh_state is not None:
        refresh_state.setdefault("complete_snapshots", []).append(
            {"provider": "volcengine", "source": "account_clone"}
        )
    return result


def _deduplicate(voices: Iterable[Voice]) -> list[Voice]:
    result: dict[tuple[str, str, str], Voice] = {}
    for voice in voices:
        result[(voice.provider, voice.voice_id, voice.model)] = voice
    return list(result.values())


def refresh_voice_catalog(
    provider: str | None = None,
    settings_store: ProviderSettingsStore | None = None,
    refresh_state: dict[str, Any] | None = None,
) -> list[Voice]:
    """Refresh the selected catalog without ever returning credentials.

    Public cloud voice lists are versioned in this module because vendor APIs do
    not necessarily expose every public voice.  If Alibaba credentials are
    configured, account custom/designed voices are appended through its official
    list API.  Volcengine account voice listing uses a separate AK/SK signed API,
    so this adapter intentionally keeps it out of the AppID/Token synthesis path.
    """

    selected = _canonical_provider(provider)
    result = builtin_voices(selected)
    if refresh_state is not None:
        builtin_providers = (
            {selected} if selected is not None else {item.provider for item in result}
        )
        refresh_state.setdefault("complete_snapshots", []).extend(
            {"provider": current, "source": "builtin"}
            for current in sorted(builtin_providers)
        )
    should_query_aliyun = selected in (None, "aliyun") and any(
        item.id == "aliyun" and item.configured
        for item in available_providers(settings_store)
    )
    if should_query_aliyun:
        result.extend(query_alibaba_custom_voices(settings_store, refresh_state))
    should_query_volc = selected in (None, "volcengine") and any(
        item.id == "volcengine" and item.catalog_configured
        for item in available_providers(settings_store)
    )
    if should_query_volc:
        successful_queries = 0
        errors: list[str] = []
        try:
            result.extend(query_volc_public_voices(settings_store, refresh_state))
            successful_queries += 1
        except ProviderError as exc:
            errors.append(f"公共音色目录：{exc}")

        config = settings_store.get_provider("volcengine") if settings_store else {}
        if config.get("app_id"):
            try:
                result.extend(query_volc_account_voices(settings_store, refresh_state))
                successful_queries += 1
            except ProviderError as exc:
                errors.append(f"账号复刻音色：{exc}")

        if errors:
            if refresh_state is not None:
                refresh_state.setdefault("warnings", []).extend(errors)
            if successful_queries == 0:
                raise ProviderError("；".join(errors))
    return _deduplicate(result)


__all__ = [
    "CATALOG_VERSION",
    "Voice",
    "builtin_voices",
    "get_voice_catalog",
    "list_voices",
    "query_alibaba_custom_voices",
    "query_volc_account_voices",
    "query_volc_public_voices",
    "refresh_voice_catalog",
]
