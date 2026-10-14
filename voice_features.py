"""Model/voice-bound language and expression rules shared by UI and backend."""
import json
import re

EMOTION_LABELS = {
    "neutral": "自然", "happy": "开心", "sad": "悲伤", "angry": "生气",
    "fearful": "害怕", "surprised": "惊讶", "disgusted": "厌恶",
    "gentle": "温柔", "calm": "平静", "excited": "兴奋",
}
ALI_EMOTIONS = ("neutral", "fearful", "angry", "sad", "surprised", "happy", "disgusted")
ALI_FIXED_VOICES = {
    ("cosyvoice-v3-flash", "longanyang"), ("cosyvoice-v3-plus", "longanyang"),
    ("cosyvoice-v3-flash", "longanhuan"), ("cosyvoice-v3-plus", "longanhuan"),
    ("cosyvoice-v3-flash", "longhuhu_v3"),
}

# Explicit "instruction following" entries in the official 2.0 voice table,
# checked 2026-10-14: https://docs.volcengine.com/docs/DoubaoVoice/Tonelist-1
# Resource/model 2.0 alone is not a declaration of per-voice support. In
# particular the table's ICL_* roles have an empty capability cell. Keep unknown
# IDs conservative rather than guessing from a suffix or claiming ASMR success.
VOLC_INSTRUCTION_VOICES = frozenset("""
zh_female_vv_uranus_bigtts zh_female_xiaohe_uranus_bigtts
zh_male_m191_uranus_bigtts zh_male_taocheng_uranus_bigtts
zh_male_liufei_uranus_bigtts zh_female_sophie_uranus_bigtts
zh_female_qingxinnvsheng_uranus_bigtts zh_female_cancan_uranus_bigtts
zh_female_sajiaoxuemei_uranus_bigtts zh_female_tianmeixiaoyuan_uranus_bigtts
zh_female_tianmeitaozi_uranus_bigtts zh_female_shuangkuaisisi_uranus_bigtts
zh_female_peiqi_uranus_bigtts zh_female_linjianvhai_uranus_bigtts
zh_male_shaonianzixin_uranus_bigtts zh_male_sunwukong_uranus_bigtts
zh_female_yingyujiaoxue_uranus_bigtts zh_female_kefunvsheng_uranus_bigtts
zh_female_xiaoxue_uranus_bigtts zh_male_dayi_uranus_bigtts
zh_female_mizai_uranus_bigtts zh_female_jitangnv_uranus_bigtts
zh_female_meilinvyou_uranus_bigtts zh_female_liuchangnv_uranus_bigtts
zh_male_ruyayichen_uranus_bigtts en_male_tim_uranus_bigtts
en_female_dacey_uranus_bigtts en_female_stokie_uranus_bigtts
zh_female_wenroumama_uranus_bigtts zh_male_jieshuoxiaoming_uranus_bigtts
zh_female_tvbnv_uranus_bigtts zh_male_yizhipiannan_uranus_bigtts
zh_female_qiaopinv_uranus_bigtts zh_female_zhishuaiyingzi_uranus_bigtts
zh_male_linjiananhai_uranus_bigtts zh_male_silang_uranus_bigtts
zh_male_ruyaqingnian_uranus_bigtts zh_male_qingcang_uranus_bigtts
zh_male_xionger_uranus_bigtts zh_female_yingtaowanzi_uranus_bigtts
zh_male_wennuanahu_uranus_bigtts zh_male_naiqimengwa_uranus_bigtts
zh_female_popo_uranus_bigtts zh_female_gaolengyujie_uranus_bigtts
zh_male_aojiaobazong_uranus_bigtts zh_male_lanyinmianbao_uranus_bigtts
zh_male_fanjuanqingnian_uranus_bigtts zh_female_wenroushunv_uranus_bigtts
zh_female_gufengshaoyu_uranus_bigtts zh_male_huolixiaoge_uranus_bigtts
zh_male_baqiqingshu_uranus_bigtts zh_male_xuanyijieshuo_uranus_bigtts
zh_female_mengyatou_uranus_bigtts zh_female_tiexinnvsheng_uranus_bigtts
zh_female_jitangmei_uranus_bigtts zh_male_cixingjieshuonan_uranus_bigtts
zh_male_liangsangmengzai_uranus_bigtts zh_female_kailangjiejie_uranus_bigtts
zh_male_gaolengchenwen_uranus_bigtts zh_male_shenyeboke_uranus_bigtts
zh_male_lubanqihao_uranus_bigtts zh_female_jiaochuannv_uranus_bigtts
zh_female_linxiao_uranus_bigtts zh_female_lingling_uranus_bigtts
zh_female_chunribu_uranus_bigtts zh_male_tangseng_uranus_bigtts
zh_male_zhuangzhou_uranus_bigtts zh_male_kailangdidi_uranus_bigtts
zh_male_zhubajie_uranus_bigtts zh_female_ganmaodianyin_uranus_bigtts
zh_female_chanmeinv_uranus_bigtts zh_female_nvleishen_uranus_bigtts
zh_female_qinqienv_uranus_bigtts zh_male_kuailexiaodong_uranus_bigtts
zh_male_kailangxuezhang_uranus_bigtts zh_male_youyoujunzi_uranus_bigtts
zh_female_wenjingmaomao_uranus_bigtts zh_female_zhixingnv_uranus_bigtts
zh_male_qingshuangnanda_uranus_bigtts zh_male_yuanboxiaoshu_uranus_bigtts
zh_male_yangguangqingnian_uranus_bigtts zh_female_qingchezizi_uranus_bigtts
zh_female_tianmeiyueyue_uranus_bigtts zh_female_xinlingjitang_uranus_bigtts
zh_male_wenrouxiaoge_uranus_bigtts zh_female_roumeinvyou_uranus_bigtts
zh_male_dongfanghaoran_uranus_bigtts zh_female_wenrouxiaoya_uranus_bigtts
zh_male_tiancaitongsheng_uranus_bigtts zh_female_wuzetian_uranus_bigtts
zh_female_gujie_uranus_bigtts zh_male_guanggaojieshuo_uranus_bigtts
zh_female_shaoergushi_uranus_bigtts
""".split())


def metadata(voice):
    value = voice.get("metadata", voice.get("metadata_json", {}))
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def language_groups(voice):
    values = voice.get("languages", [])
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except ValueError:
            values = [values]
    groups = set()
    aliases = {"中文": "zh", "普通话": "zh", "chinese": "zh", "mandarin": "zh",
               "英语": "en", "英文": "en", "english": "en", "日语": "ja", "japanese": "ja"}
    for value in values if isinstance(values, list) else []:
        code = str(value).strip().lower().replace("_", "-")
        code = aliases.get(code, code).split("-")[0]
        if code in {"zh", "cmn", "yue", "wuu", "hak", "nan", "gan", "hsn"}:
            groups.add("chinese")
        elif re.fullmatch(r"[a-z]{2,3}", code) and code not in {"und", "mul"}:
            groups.add("foreign")
    return groups


def expression_features(voice):
    provider, model = voice.get("provider"), voice.get("model", "")
    info = metadata(voice)
    result = {"instruction": "none", "emotion_mode": "none", "emotions": [], "emotion_scale": False}
    if provider == "volcengine":
        # Emotion enums come from this exact voice's ListSpeakers metadata.
        # Context instructions are a separate 2.0 capability: never claim every
        # voice supports the native emotion parameter just because it is V3.
        native = []
        for item in info.get("emotions", []) if isinstance(info.get("emotions"), list) else []:
            if isinstance(item, dict) and isinstance(item.get("Value"), str) and item["Value"].strip():
                value = str(item["Value"]).strip()
                native.append({"value": value, "label": str(item.get("Label") or EMOTION_LABELS.get(value, value))})
        if model == "seed-tts-2.0" and voice.get("voice_id") in VOLC_INSTRUCTION_VOICES:
            result["instruction"] = "free"
        elif model == "seed-tts-2.0":
            result["instruction_unknown"] = True
        if native:
            result.update(emotion_mode="parameter", emotions=native, emotion_scale=True)
        elif result["instruction"] == "free":
            result["emotion_mode"] = "instruction"
    elif provider == "xiaomi" and model == "mimo-v2.5-tts":
        result.update(instruction="free", emotion_mode="instruction")
    elif provider == "aliyun":
        if (model, voice.get("voice_id")) in ALI_FIXED_VOICES:
            result.update(instruction="fixed", emotion_mode="fixed",
                          emotions=[{"value": value, "label": EMOTION_LABELS[value]} for value in ALI_EMOTIONS])
        elif voice.get("source") == "account_custom" and model in {"cosyvoice-v3.5-flash", "cosyvoice-v3.5-plus", "cosyvoice-v3-flash"}:
            result.update(instruction="free", emotion_mode="instruction")
    elif provider == "mock":
        result.update(instruction="free", emotion_mode="instruction")
    if result["emotion_mode"] == "instruction":
        result["emotions"] = [{"value": value, "label": label} for value, label in EMOTION_LABELS.items()]
    return result


def _validate_fixed_instruction(voice, instruction):
    emotion = "|".join(ALI_EMOTIONS)
    suffix = rf"你说话的情感是(?:{emotion})[。.]"
    name = voice.get("voice_id")
    if name == "longanyang":
        scenarios = ("闲聊互动", "新闻播报", "广告促销", "比赛解说", "一些儿童内容解说", "语音导航", "脱口秀表演")
        roles, identities = ("一个旁白",), ("故事机",)
    elif name == "longanhuan":
        scenarios = ("闲聊对话", "比赛解说", "深夜电台广播", "剧情解说", "诗歌朗诵", "科普知识推广", "产品推广", "脱口秀表演")
        roles, identities = ("温和客服",), ()
    else:
        scenarios = ("自由对话", "广告促销")
        roles, identities = ("傲娇公主", "元气少女", "可爱孩童", "机器人", "小猪佩奇"), ("故事机", "儿童玩具")
    prefixes = [""]
    prefixes += [f"你正在进行{value}，" for value in scenarios]
    prefixes += [f"你现在说话的角色是{value}，" for value in roles]
    prefixes += [f"你正在以一个{value}的身份说话，" for value in identities]
    if not any(re.fullmatch(re.escape(prefix) + suffix, instruction) for prefix in prefixes):
        raise ValueError("此阿里云音色需要官方固定格式，如：你说话的情感是happy。支持的场景和角色以该音色官方说明为准")


def resolve_expression(voice, instruction="", emotion="", emotion_scale=4):
    """Validate before billing and turn UI presets into real vendor controls."""
    features = expression_features(voice)
    instruction, emotion = instruction.strip(), emotion.strip()
    if len(instruction) > 500:
        raise ValueError("表达指令不能超过 500 字符")
    if not 1 <= emotion_scale <= 5:
        raise ValueError("情感强度必须在 1 到 5 之间")
    if instruction and features["instruction"] == "none":
        if features.get("instruction_unknown"):
            raise ValueError(f"音色 {voice.get('name') or voice.get('voice_id')} 官方未标注支持表达指令，请选择标注“指令遵循”的音色")
        raise ValueError(f"音色 {voice.get('name') or voice.get('voice_id')} 不支持表达指令")
    emotions = {item["value"]: item["label"] for item in features["emotions"]}
    if emotion and emotion not in emotions:
        raise ValueError(f"音色 {voice.get('name') or voice.get('voice_id')} 不支持所选情感")
    if emotion_scale != 4 and (not emotion or not features["emotion_scale"]):
        raise ValueError("当前音色不支持数值情感强度，请使用默认值")
    native_emotion = emotion if features["emotion_mode"] == "parameter" else ""
    if features["instruction"] == "fixed":
        if instruction:
            _validate_fixed_instruction(voice, instruction)
        if emotion:
            instruction = re.sub(r"你说话的情感是[a-z]+[。.]$", f"你说话的情感是{emotion}。", instruction) if instruction else f"你说话的情感是{emotion}。"
    elif emotion and features["emotion_mode"] == "instruction":
        instruction = f"请用{emotions[emotion]}的语气朗读。" + ("\n" + instruction if instruction else "")
    if voice.get("provider") == "aliyun" and instruction:
        # Alibaba counts CJK ideographs as two characters; a Python len() check
        # alone accepts over-limit instructions that the server rejects.
        length = sum(2 if "\u3400" <= char <= "\u9fff" else 1 for char in instruction)
        if length > 100:
            raise ValueError("阿里云表达指令超过 100 字符（汉字按 2 字符计算）")
    return {"instruction": instruction, "emotion": native_emotion, "emotion_scale": emotion_scale}
