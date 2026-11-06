"""Describe verified provider controls without exposing host credentials."""

from __future__ import annotations

from .breeze_control import AUDIO_TAGS, EVENTS
from .settings import BreezeError

# Official references and model restrictions are documented in README.md.
PRESETS = {
    "breeze_local": {
        "name": "Breeze 本地",
        "mode": "free",
        "tag_policy": "reference",
        "tags": sorted(EVENTS),
        "styles": [],
        "note": "本地模型以中英为主；部分事件需按实际模型试听。",
    },
    "breeze_cloud": {
        "name": "BreezeBlue",
        "mode": "free",
        "tag_policy": "reference",
        "tags": sorted(EVENTS),
        "styles": [],
        "note": "指令与台词使用相同语言；CFG=1 不代表指令无效。",
    },
    "minimax": {
        "name": "MiniMax",
        "mode": "fixed",
        "tags": [],
        "styles": "happy sad angry fearful disgusted surprised calm".split(),
        "note": "情绪值随模型变化；正文事件只在支持的模型上开放。",
    },
    "elevenlabs": {
        "name": "ElevenLabs v3/v4",
        "mode": "free",
        "tags": "[curious]|[crying]|[mischievously]|[whispers]|[shouts]|[laughs]|[clears throat]|[sighs]".split(
            "|"
        ),
        "styles": [],
        "note": "自由语气会转换为英文方括号形式；音色需适配相应表演。",
    },
    "fish": {
        "name": "Fish Audio S2",
        "mode": "free",
        "tags": "[whisper]|[angry]|[excited]|[sad]|[laughing]|[inhale]|[exhale]|[sigh]|[short pause]|[clearing throat]".split(
            "|"
        ),
        "styles": [],
        "note": "S2 接受自由自然语言标签；请用统一语气标记书写自由描述。",
    },
    "fish_s1": {
        "name": "Fish Audio S1",
        "mode": "fixed",
        "tags": [],
        "styles": "angry sad excited surprised satisfied delighted scared worried upset nervous frustrated depressed empathetic embarrassed disgusted moved proud relaxed grateful confident interested curious confused joyful disapproving hesitating".split(),
        "note": "S1 使用圆括号情绪；不要混用 S2 方括号。",
    },
    "mimo": {
        "name": "MiMo",
        "mode": "free",
        "tags": "[whispering]|[sighs]|[inhale]|[choked up]|[emphasis]|[laughs]|[hearty laugh]".split(
            "|"
        ),
        "styles": [],
        "note": "自由描述通过 style_prompt 传入；保留提供商中的方言设定。",
    },
    "openai": {
        "name": "OpenAI 指令型 TTS",
        "mode": "free",
        "tags": [],
        "styles": [],
        "note": "使用独立 instructions 参数；不向正文插入未经文档定义的事件标签。",
    },
    "gemini": {
        "name": "Gemini TTS",
        "mode": "free",
        "tags": [],
        "styles": [],
        "note": "使用自然语言前导指令；不假定支持特定正文音频事件。",
    },
    "qwen": {
        "name": "Qwen Instruct TTS",
        "mode": "free",
        "tags": [],
        "styles": [],
        "note": "使用独立 instructions 字段；普通 Qwen TTS 不会自动获得指令能力。",
    },
    "cosyvoice": {
        "name": "CosyVoice 自由指令",
        "mode": "free",
        "tags": [],
        "styles": [],
        "note": "仅适用于支持指令的模型及复刻/设计音色；最长 128 字符。",
    },
    "cosyvoice_fixed": {
        "name": "CosyVoice 预置情感",
        "mode": "fixed",
        "tags": [],
        "styles": "neutral fearful angry sad surprised happy disgusted".split(),
        "note": "插件转换为严格的中文指令句式；各预置音色情感范围不同。",
    },
    "azure": {
        "name": "Azure Speech",
        "mode": "fixed",
        "tags": [],
        "styles": "cheerful sad angry excited friendly hopeful shouting terrified unfriendly whispering".split(),
        "note": "必须使用当前音色支持的 style。可自定义白名单，移除不支持的项。",
    },
    "volcengine": {
        "name": "火山引擎多情感音色",
        "mode": "fixed",
        "tags": [],
        "styles": "happy sad angry scare hate surprise tear comfort".split(),
        "note": "使用已有 v1 接口的 audio.emotion；按音色文档自定义白名单。",
    },
    "gsvi": {
        "name": "GSVI",
        "mode": "fixed",
        "tags": [],
        "styles": ["默认"],
        "note": "情绪来自实际角色模型；请填写模型已安装的情绪名称。",
    },
}


def provider_preset(provider) -> str:
    """Identify expressive host models, excluding known unsupported variants.

    Args:
        provider: An already initialized AstrBot TTS provider.

    Returns:
        A preset key, or empty when no verified control protocol is available.
    """
    kind = provider.provider_config.get("type", "")
    model = (getattr(provider, "model_name", "") or "").lower()
    if kind == "elevenlabs_tts_api":
        return "elevenlabs" if model.startswith(("eleven_v3", "eleven_v4")) else ""
    if kind == "fishaudio_tts_api":
        return (
            "fish"
            if model.startswith("s2")
            else "fish_s1"
            if model.startswith("s1")
            else ""
        )
    if kind == "openai_tts_api":
        return "openai" if "tts" in model and not model.startswith("tts-1") else ""
    if kind == "dashscope_tts":
        if model.startswith("qwen"):
            return "qwen" if "instruct" in model and "realtime" not in model else ""
        voice = getattr(provider, "voice", "")
        if model.startswith("cosyvoice-v3"):
            if voice in {"longanhuan", "longhuhu_v3"}:
                return "cosyvoice_fixed"
            if voice.startswith("cosyvoice-") and (
                "v3.5" in model or "v3-plus" in model
            ):
                return "cosyvoice"
        return ""
    return {
        "minimax_tts_api": "minimax",
        "mimo_tts_api": "mimo",
        "gemini_tts": "gemini",
        "azure_tts": "azure",
        "volcengine_tts": "volcengine",
        "gsvi_tts_api": "gsvi",
    }.get(kind, "")


def expression_profile(config: dict, provider=None) -> dict:
    """Combine a model-specific preset with bounded user control catalogs.

    Args:
        config: Validated plugin settings.
        provider: Selected host provider when using the provider backend.

    Returns:
        Public capability data and exact tokens for parsing and prompts.

    Raises:
        BreezeError: If the selected provider is absent or unsupported.
    """
    if config["backend"] == "provider":
        if provider is None:
            raise BreezeError("请先在 AstrBot 配置并选择可用的 TTS 提供商。")
        key = provider_preset(provider)
        override = config["provider"]["preset"]
        if override and provider.provider_config.get("type") == "dashscope_tts":
            model = provider.model_name.lower()
            if (override == "qwen") != model.startswith("qwen"):
                raise BreezeError("所选指令协议与 Dashscope 模型不匹配。")
            key = override
        if not key:
            raise BreezeError(
                "当前 TTS 模型没有已验证的动态语调协议，请切换支持的模型。"
            )
    else:
        key = "breeze_" + config["backend"]
    profile = {"tag_policy": "closed", **PRESETS[key], "key": key}
    profile["styles"] = list(profile["styles"])
    profile["tags"] = list(profile["tags"])
    if key.startswith("breeze_"):
        language = config["cloud"]["language_code"].lower()
        languages = (
            [language]
            if key == "breeze_cloud" and language in AUDIO_TAGS
            else ["zh", "en"]
        )
        profile["prompt_tags"] = [tag for code in languages for tag in AUDIO_TAGS[code]]
    if key == "minimax" and provider is not None:
        model = provider.model_name
        if "2.6" in model:
            profile["styles"] += ["fluent", "whisper"]
        if "2.8" in model:
            profile["tags"] = (
                "(laughs) (sighs) (coughs) (breath) (groans) (sniffs) (gasps) (chuckle) (humming) (snorts) (burps) (lip-smacking) (hissing)".split()
            )
    if key == "fish_s1":
        profile["tags"] = [f"({style})" for style in profile["styles"]]
    if key == "breeze_local" and "prompt_tags" in profile:
        profile["prompt_tags"] = [
            {
                "[清嗓]": "[清嗓子]",
                "(laughs)": "(laugh)",
                "(coughs)": "(cough)",
                "(sighs)": "(sigh)",
            }.get(tag, tag)
            for tag in profile["prompt_tags"]
        ]
    if key == "gsvi" and provider is not None:
        profile["styles"] = list(dict.fromkeys(["默认", provider.emotion]))
    for field in ("tags", "styles"):
        if field == "styles" and profile["mode"] != "fixed":
            continue
        custom = config["expression"][key + "_" + field].strip()
        if custom:
            entries = list(
                dict.fromkeys(
                    line.strip() for line in custom.splitlines() if line.strip()
                )
            )
            if field == "tags" and profile["tag_policy"] == "reference":
                profile["tags"] = list(set(profile["tags"]) | set(entries))
                profile["prompt_tags"] = entries
            else:
                profile[field] = entries
                profile.pop("prompt_tags", None)
    profile["tags"] = tuple(sorted(profile["tags"]))
    return profile


def check_directions(profile: dict, *directions: str) -> None:
    """Enforce the selected API's fixed vocabulary before any network request.

    Args:
        profile: Effective capability preset.
        *directions: Overall and local performance directions.

    Raises:
        BreezeError: If a fixed API receives unsupported prose or a style value.
    """
    if profile["key"] in {"fish", "elevenlabs", "mimo"} and any(
        any(char in value for char in "[]<>") for value in directions
    ):
        raise BreezeError("自由语气描述不能嵌套标签，请将原生事件写入正文。")
    if profile["mode"] == "fixed" and any(
        value and value not in profile["styles"] for value in directions
    ):
        raise BreezeError("当前平台只接受固定语气值：" + "、".join(profile["styles"]))
