"""UI service identities, independent from historical sample provenance."""

SERVICES = [
    ("豆包 1.0", "volcengine_v1"), ("豆包 2.0", "volcengine_v2"),
    ("小米 MiMo", "xiaomi"), ("阿里云", "aliyun"), ("离线测试", "mock"),
]


def settings_id(service):
    return "volcengine" if service == "volcengine_v1" else service


def audio_provider(service):
    return "volcengine" if service in {"volcengine_v1", "volcengine_v2"} else service


def service_for_voice(voice):
    if voice.get("provider") == "volcengine":
        return "volcengine_v2" if voice.get("model") == "seed-tts-2.0" else "volcengine_v1"
    return voice.get("provider", "mock")

