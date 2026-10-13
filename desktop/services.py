"""UI service identities, independent from historical sample provenance."""

SERVICES = [
    ("豆包 1.0", "volcengine_v1"), ("豆包 2.0", "volcengine_v2"),
    ("小米 MiMo", "xiaomi"), ("阿里云", "aliyun"), ("离线测试", "mock"),
]
BASE_URLS = {"volcengine_v1": "https://openspeech.bytedance.com", "volcengine_v2": "https://openspeech.bytedance.com",
             "xiaomi": "https://api.xiaomimimo.com/v1", "aliyun": "https://dashscope.aliyuncs.com/api/v1"}


def settings_id(service):
    return "volcengine" if service == "volcengine_v1" else service


def audio_provider(service):
    return "volcengine" if service in {"volcengine_v1", "volcengine_v2"} else service

