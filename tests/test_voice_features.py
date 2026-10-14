"""Language, exact voice capabilities and real vendor expression contracts."""
import base64
import json

import httpx
import pytest

import providers
from voice_features import expression_features, language_groups, resolve_expression


@pytest.mark.parametrize("languages,expected", [
    (["zh-CN"], {"chinese"}), (["yue", "wuu"], {"chinese"}),
    (["en-US", "ja"], {"foreign"}), (["zh-cn", "en"], {"chinese", "foreign"}),
    (["中文", "English"], {"chinese", "foreign"}), (["und"], set()),
    ([], set()), ('["en"]', {"foreign"}), (["unknown"], set()),
])
def test_language_classification_uses_declared_languages(languages, expected):
    assert language_groups({"languages": languages}) == expected


def test_doubao2_instruction_emotion_preset_does_not_invent_native_parameters():
    voice = {"provider": "volcengine", "model": "seed-tts-2.0", "voice_id": "zh_female_vv_uranus_bigtts"}
    features = expression_features(voice)
    assert features["instruction"] == "free" and not features["emotion_scale"]
    result = resolve_expression(voice, "慢一些，强调开灯", "happy")
    assert result["instruction"] == "请用开心的语气朗读。\n慢一些，强调开灯"
    assert not result["emotion"]


@pytest.mark.parametrize("identifier,supported", [
    ("zh_female_jiaochuannv_uranus_bigtts", True),
    ("ICL_uranus_zh_female_aojiaonvyou_tob", False),
    ("unlisted_voice_uranus_bigtts", False),
])
def test_doubao_instruction_requires_explicit_official_voice_declaration(identifier, supported):
    voice = {"provider": "volcengine", "model": "seed-tts-2.0", "voice_id": identifier}
    assert (expression_features(voice)["instruction"] == "free") == supported
    if supported:
        assert resolve_expression(voice, "用 ASMR 的语气撩我")["instruction"] == "用 ASMR 的语气撩我"
    else:
        with pytest.raises(ValueError, match="官方未标注支持表达指令"):
            resolve_expression(voice, "用 ASMR 的语气撩我")


def test_doubao_native_emotions_require_voice_metadata_and_support_both_models(monkeypatch):
    calls = []
    def request(method, url, **options):
        calls.append(options)
        return httpx.Response(200, json={"code": 0, "data": base64.b64encode(b"ID3-complete").decode(), "usage": {"text_words": 2}})
    monkeypatch.setattr(providers, "_request", request)
    for model in ("seed-tts-1.0", "seed-tts-2.0"):
        voice = {"provider": "volcengine", "model": model, "metadata_json": json.dumps({"emotions": [{"Value": "happy", "Label": "开心"}]})}
        expression = resolve_expression(voice, emotion="happy", emotion_scale=2.5)
        providers.VolcTTSProvider({"api_key": "fixture"}).synthesize(text="测试", voice="fixture", model=model, audio_format="mp3", **expression)
        assert calls[-1]["json_body"]["req_params"]["audio_params"]["emotion"] == "happy"
        assert calls[-1]["json_body"]["req_params"]["audio_params"]["emotion_scale"] == 2.5
        assert calls[-1]["headers"]["X-Api-Resource-Id"] == model
        assert calls[-1]["retry_transient"] is False
        with pytest.raises(ValueError, match="不支持所选情感"):
            resolve_expression(voice, emotion="sad")


@pytest.mark.parametrize("model,voice", [("cosyvoice-v3-flash", "longanyang"), ("cosyvoice-v3-plus", "longanhuan"), ("cosyvoice-v3-flash", "longhuhu_v3")])
def test_aliyun_fixed_emotion_instruction(model, voice):
    row = {"provider": "aliyun", "model": model, "voice_id": voice, "source": "builtin"}
    assert resolve_expression(row, emotion="happy")["instruction"] == "你说话的情感是happy。"
    with pytest.raises(ValueError, match="固定格式"):
        resolve_expression(row, instruction="自由发挥，开心一点")


@pytest.mark.parametrize("row", [
    {"provider": "volcengine", "model": "seed-tts-1.0"},
    {"provider": "aliyun", "model": "cosyvoice-v2", "voice_id": "longhuhu"},
    {"provider": "aliyun", "model": "cosyvoice-v3-flash", "voice_id": "longyingxiao_v3", "source": "builtin"},
    {"provider": "aliyun", "model": "cosyvoice-v3-plus", "voice_id": "fixture-clone", "source": "account_custom"},
])
def test_unsupported_voices_reject_style_before_request(row):
    with pytest.raises(ValueError, match="不支持表达指令"):
        resolve_expression(row, instruction="开心一些")
    with pytest.raises(ValueError, match="不支持所选情感"):
        resolve_expression(row, emotion="happy")


def test_aliyun_custom_instruction_cjk_length_limit():
    row = {"provider": "aliyun", "model": "cosyvoice-v3.5-plus", "voice_id": "fixture", "source": "account_custom"}
    assert resolve_expression(row, instruction="中" * 50)["instruction"] == "中" * 50
    with pytest.raises(ValueError, match="100 字符"):
        resolve_expression(row, instruction="中" * 51)


def test_mimo_emotion_is_style_guidance_not_spoken_text(monkeypatch):
    row = {"provider": "xiaomi", "model": "mimo-v2.5-tts"}
    expression = resolve_expression(row, instruction="保持自然", emotion="sad")
    calls = []
    def request(method, url, **options):
        calls.append(options["json_body"])
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"audio": {"data": "AAAA"}}}]})
    monkeypatch.setattr(providers, "_request", request)
    providers.XiaomiTTSProvider({"api_key": "fixture"}).synthesize(text="开灯", voice="冰糖", instruction=expression["instruction"])
    assert calls[0]["messages"][0] == {"role": "user", "content": "请用悲伤的语气朗读。\n保持自然"}
    assert calls[0]["messages"][1] == {"role": "assistant", "content": "开灯"}
    assert "emotion" not in calls[0] and "emotion_scale" not in calls[0]
    with pytest.raises(ValueError, match="数值情感强度"):
        resolve_expression(row, emotion="sad", emotion_scale=2)


def test_aliyun_supported_emotion_is_sent_in_instruction(monkeypatch):
    row = {"provider": "aliyun", "model": "cosyvoice-v3-flash", "voice_id": "longanyang"}
    expression = resolve_expression(row, emotion="angry")
    calls = []
    def request(method, url, **options):
        calls.append(options["json_body"])
        return httpx.Response(200, headers={"content-type": "audio/wav"}, content=b"RIFF-fixture")
    monkeypatch.setattr(providers, "_request", request)
    providers.AlibabaTTSProvider({"api_key": "fixture"}).synthesize(text="开灯", voice="longanyang", model="cosyvoice-v3-flash", instruction=expression["instruction"])
    assert calls[0]["input"]["instruction"] == "你说话的情感是angry。"
    assert "emotion" not in calls[0]["input"]
