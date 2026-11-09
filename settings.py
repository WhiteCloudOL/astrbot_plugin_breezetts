"""Validate configuration at the AstrBot and WebUI boundaries."""

from __future__ import annotations

import copy
import math
import re
from urllib.parse import urlsplit, urlunsplit

PROMPT_VARIABLES = {
    "platform_name": {
        "label": "当前语音平台",
        "description": "所选 TTS 平台或模型预设的名称，例如 Breeze 本地、MiniMax。",
        "scope": ["speech_prompt", "tone_prompt"],
    },
    "platform_rules": {
        "label": "平台控制规则",
        "description": "当前平台的固定情绪或自由指令约束，以及模型能力说明。建议保留。",
        "scope": ["speech_prompt", "tone_prompt"],
    },
    "audio_tags": {
        "label": "原生标签表",
        "description": "当前生效的正文标签表；Breeze 为推荐参考，允许表外自由语气。自定义内容会自动带入，标签不要求每次添加。",
        "scope": ["speech_prompt", "tone_prompt"],
    },
    "style_values": {
        "label": "固定情绪允许值",
        "description": "例如 happy、sad 等精确 API 值；自由指令平台会显示可使用自然语言。",
        "scope": ["speech_prompt", "tone_prompt"],
    },
    "control_syntax": {
        "label": "控制语法说明",
        "description": "分段语气、整体指令、停顿及文字过滤规则。建议保留。",
        "scope": ["speech_prompt", "tone_prompt"],
    },
    "output_protocol": {
        "label": "自适应输出格式",
        "description": "提供 JSON 输出及台词保留规则；请保留此变量或手写等价 JSON 契约。格式不符或改写台词会回退；可不加标签、留空或省略语气。",
        "scope": ["tone_prompt"],
    },
    "user_message": {
        "label": "本轮用户消息",
        "description": "本轮消息的引用内容，最多 1000 字符，只用于判断语境。建议放在模板末尾；自适应会将此变量及后续内容放入 user 消息，保持 system 前缀稳定。",
        "scope": ["speech_prompt", "tone_prompt"],
    },
    "passage": {
        "label": "待朗读台词",
        "description": "概率命中后已选出的台词，必须保留原文；仅用于概率语音自适应。建议放末尾；默认已通过独立 user 消息传入，避免影响 LLM 前缀缓存。",
        "scope": ["tone_prompt"],
    },
}

DEFAULTS = {
    "backend": "local",
    "local": {
        "implementation": "python",
        "base_url": "http://127.0.0.1:7860",
        "api_key": "",
        "mode": "reference",
        "reference_id": "",
        "voice_id": "",
        "output_format": "pcm",
        "temperature": 0.0,
        "top_k": 0,
        "top_p": 0.0,
        "repetition_penalty": 0.0,
        "split_chars": -1,
        "max_new_tokens": 0,
        "instruction": "",
        "cfg_scale": 1.0,
        "random_seed": True,
        "seed": 42,
    },
    "cloud": {
        "base_url": "https://api.breeze.blue/v1",
        "api_key": "",
        "voice_id": "",
        "model_id": "",
        "language_code": "",
        "instructions": "",
        "guidance_scale": 1.0,
        "speed": 1.0,
        "volume": 1.0,
        "speaker_boost": False,
        "output_format": "wav",
        "delivery": "sync",
    },
    "provider": {"provider_id": "", "preset": ""},
    "expression": {
        key + "_" + field: ""
        for key in (
            "breeze_local",
            "breeze_cloud",
            "minimax",
            "elevenlabs",
            "fish",
            "fish_s1",
            "mimo",
            "openai",
            "gemini",
            "qwen",
            "cosyvoice",
            "cosyvoice_fixed",
            "azure",
            "volcengine",
            "gsvi",
        )
        for field in ("tags", "styles")
    },
    "behavior": {
        "auto_tts": False,
        "dynamic_tone": False,
        "random_tone_adaptive": False,
        "tone_provider_id": "",
        "speech_prompt": (
            "通过 {platform_name} 表达自然对话，可在闲聊、安慰、惊喜或角色互动中主动使用语音；"
            "尊重只要文字的要求，代码、表格和长篇资料保留文字。动态语调开启时，"
            "推荐贴合语境的情绪、节奏或重音，避免总用温柔平缓的腔调；标签与指令均可省略。\n"
            "短句优先一种整体语气，长句仅在自然句界调整语气。instruction 简短描述稳定的整体表现，"
            "不写先做什么、后做什么的表演脚本，也不重复正文中的吸气等事件。普通停连用标点，"
            "不为表现力刻意插入停顿。\n"
            "{platform_rules}\n原生标签参考：{audio_tags}\n{control_syntax}\n"
            "调用工具时 text 只放台词及可选控制，instruction 单独传参；文字会隐藏控制标记。"
            "成功后只补充新内容，不复述台词或发送回执；失败不在本轮重复调用。"
        ),
        "tone_prompt": (
            "你是 {platform_name} 的语音表演导演，只为 user 消息中的 passage 推荐贴合语境的表达，"
            "保留说话人的身份和声线。可主动考虑情绪、节奏或重音，避免总套用同一种腔调；"
            "所有控制均可省略，自然朗读合适时返回 {}。\n"
            "短句优先一种整体语气，不拆成多个短片段。instruction 用简短的稳定语气描述，"
            "不写先吸气、再打断等多步骤脚本，不重复正文事件。普通停连用标点，"
            "仅在确需明显停顿时使用停顿标签。\n"
            "严格只输出 JSON 对象，不加 Markdown、代码块或解释；格式示例和原文保留要求见下方。\n"
            "{platform_rules}\n原生标签参考：{audio_tags}\n{control_syntax}\n{output_protocol}"
        ),
        "trigger_probability": 20,
        "private_trigger_probability": -1,
        "group_trigger_probability": -1,
        "min_interval_seconds": 90,
        "keep_text": True,
        "integer_second_audio": True,
        "quote_keep_text": False,
        "tool_enabled": True,
        "max_characters": 1000,
        "timeout": 600,
        "history_limit": 50,
        "history_max_mb": 256,
    },
}
STREAM_FORMATS = {"pcm", "mp3", "wav"}
FORMATS = {
    "wav",
    "mp3",
    "flac",
    "pcm",
    "aac",
    "opus",
    "mp3_24000_64",
    "mp3_24000_96",
    "mp3_24000_128",
    "mp3_24000_160",
    "wav_24000",
    "pcm_24000",
    "flac_24000",
    "aac_24000_128",
}


class BreezeError(ValueError):
    """Represent a safe error that can be displayed without exposing credentials."""

    def __init__(self, message: str, *, code: str = "validation"):
        super().__init__(message)
        self.code = code


def validate_identifier(value: object, label: str, limit: int = 500) -> str:
    """Validate identifiers that occupy one URL path segment.

    Args:
        value: Identifier from configuration or an upstream response.
        label: Safe field label used in validation errors.
        limit: Maximum identifier length.

    Returns:
        The unchanged valid identifier.

    Raises:
        BreezeError: If the identifier is empty, oversized, or a dot segment.
    """
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= limit
        or value in {".", ".."}
        or value != value.strip()
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise BreezeError(f"{label} 无效，请检查配置或服务响应。")
    return value


def api_root(base_url: str) -> str:
    """Normalize a service root while retaining reverse proxy path prefixes.

    Args:
        base_url: HTTP root, version root, or local speech endpoint.

    Returns:
        The URL ending in /v1, without credentials or query parameters.

    Raises:
        BreezeError: If the address cannot safely identify a service root.
    """
    if not isinstance(base_url, str) or len(base_url) > 2048 or "\\" in base_url:
        raise BreezeError("服务地址无效，请填写完整的 HTTP(S) 地址。")
    try:
        parsed = urlsplit(base_url.strip())
        port = parsed.port
    except ValueError:
        raise BreezeError(
            "服务地址无效，请填写完整的 http:// 或 https:// 地址。"
        ) from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(ord(c) < 33 for c in base_url)
        or (port is not None and port < 1)
        or any(part in {".", ".."} for part in parsed.path.split("/"))
    ):
        raise BreezeError(
            "服务地址须为 HTTP(S) 地址，不能包含账号、密钥、查询参数或片段。"
        )
    path = parsed.path.rstrip("/")
    if path.endswith("/audio/speech"):
        path = path[: -len("/audio/speech")]
    if not path.endswith("/v1"):
        path += "/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def validate_config(value: dict) -> dict:
    """Merge defaults and validate settings without mutating the input.

    Args:
        value: AstrBot settings or a complete WebUI settings draft.

    Returns:
        A validated configuration with every default present.

    Raises:
        BreezeError: If settings contain unknown fields or invalid values.
    """
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise BreezeError("配置必须为有效对象，且不能包含未知字段。")
    result = copy.deepcopy(DEFAULTS)
    for group, default in DEFAULTS.items():
        if isinstance(default, dict):
            incoming = value.get(group, {})
            if not isinstance(incoming, dict) or set(incoming) - set(default):
                raise BreezeError(f"{group} 配置包含未知字段或格式不正确。")
            result[group].update(incoming)
            for key, expected in default.items():
                actual = result[group][key]
                if isinstance(expected, float):
                    valid = type(actual) is int or (
                        type(actual) is float and math.isfinite(actual)
                    )
                else:
                    valid = type(actual) is type(expected)
                if not valid:
                    raise BreezeError(f"{group}.{key} 的类型或数值不正确。")
        else:
            result[group] = value.get(group, default)
    if not isinstance(result["backend"], str) or result["backend"] not in {
        "local",
        "cloud",
        "provider",
    }:
        raise BreezeError("请选择本地 API、BreezeBlue 或 AstrBot TTS 提供商。")
    for group in ("local", "cloud"):
        api_root(result[group]["base_url"])
        key = result[group]["api_key"]
        if len(key) > 512 or any(not 33 <= ord(c) <= 126 for c in key):
            raise BreezeError(
                "API Key 只能包含非空白 ASCII 字符，请勿添加 Bearer 前缀。"
            )
        for name, item in result[group].items():
            if isinstance(item, str) and len(item) > (
                4000 if "instruction" in name else 1000
            ):
                raise BreezeError(f"{group}.{name} 内容过长。")
    if result["provider"]["preset"] not in {"", "cosyvoice", "cosyvoice_fixed", "qwen"}:
        raise BreezeError("TTS 指令协议预设无效。")
    selected = result["provider"]["provider_id"]
    if selected and (
        len(selected) > 200
        or selected != selected.strip()
        or any(ord(c) < 32 for c in selected)
    ):
        raise BreezeError("TTS 提供商 ID 无效。")
    for field, catalog in result["expression"].items():
        lines = [line.strip() for line in catalog.splitlines() if line.strip()]
        if (
            len(catalog) > 12000
            or len(lines) > 100
            or any(ord(c) < 32 and c not in "\n\r" for c in catalog)
        ):
            raise BreezeError("自定义控制表最多 100 行、12000 字符。")
        for line in lines:
            if field.endswith("_tags"):
                if not re.fullmatch(
                    r"(?:\[[^\[\]<>`]{1,100}\]|\([^()<>`]{1,100}\))", line
                ) or re.match(r"\[(?:语气|style|話し方)\s*[:：]", line, re.I):
                    raise BreezeError(
                        "每行填写一个完整原生标签，例如 [laughs]；语气标记不属于原生标签。"
                    )
            elif not 1 <= len(line) <= 100 or any(c in line for c in "[]<>()`"):
                raise BreezeError("每行填写一个平台实际支持的情绪值，不能包含标记。")
    local, cloud = result["local"], result["cloud"]
    local["voice_id"] = re.sub(
        r"^[\s\u200b\ufeff]+|[\s\u200b\ufeff]+$", "", local["voice_id"]
    )
    # Clipboard padding is not part of the opaque ID; never infer a required prefix.
    cloud["voice_id"] = re.sub(
        r"^[\s\u200b\ufeff]+|[\s\u200b\ufeff]+$", "", cloud["voice_id"]
    )
    provider_id = result["behavior"]["tone_provider_id"]
    if (
        len(provider_id) > 200
        or provider_id != provider_id.strip()
        or any(ord(char) < 32 for char in provider_id)
    ):
        raise BreezeError("语调 LLM 提供商 ID 无效。")
    for name in ("speech_prompt", "tone_prompt"):
        prompt = result["behavior"][name]
        unknown = set(re.findall(r"\{([A-Za-z_][A-Za-z_0-9]*)\}", prompt)) - {
            key for key, item in PROMPT_VARIABLES.items() if name in item["scope"]
        }
        if unknown:
            raise BreezeError(
                "此提示词不支持的变量："
                + "、".join("{" + key + "}" for key in sorted(unknown))
            )
        if len(prompt) > 6000 or any(
            ord(char) < 32 and char not in "\n\r\t" for char in prompt
        ):
            raise BreezeError(
                f"{name} 须为不超过 6000 字符的提示词，不能包含无效控制字符。"
            )
    if local["implementation"] not in {"python", "cpp"}:
        raise BreezeError("本地服务端须选择 Python API 或 Breeze-TTS-2.cpp。")
    if local["mode"] not in {"reference", "design", "plain", "voice"}:
        raise BreezeError("本地合成模式无效。")
    if local["mode"] == "voice" and local["implementation"] != "cpp":
        raise BreezeError("服务端音色模式需要 Breeze-TTS-2.cpp。")
    if local["voice_id"] and not re.fullmatch(
        r"[A-Za-z0-9_-]{1,64}", local["voice_id"]
    ):
        raise BreezeError("服务端音色 ID 须为 1–64 个字母、数字、短横线或下划线。")
    if local["output_format"] not in {"pcm", "wav", "mp3"}:
        raise BreezeError("C++ 输出格式须为 PCM、WAV 或 MP3。")
    if cloud["output_format"] not in FORMATS or cloud["delivery"] not in {
        "sync",
        "async",
        "stream",
    }:
        raise BreezeError("官方输出格式或交付方式无效。")
    if cloud["delivery"] == "stream" and cloud["output_format"] not in STREAM_FORMATS:
        raise BreezeError("官方流式接收仅支持 pcm、mp3、wav，不支持带后缀的格式。")
    ranges = [
        ("local", "cfg_scale", 0.01, 10),
        ("local", "seed", 0, 2**32 - 1),
        ("local", "temperature", 0, 5),
        ("local", "top_k", 0, 10000),
        ("local", "top_p", 0, 1),
        ("local", "repetition_penalty", 0, 5),
        ("local", "split_chars", -1, 10000),
        ("local", "max_new_tokens", 0, 15000),
        ("cloud", "guidance_scale", 1, 10),
        ("cloud", "speed", 0.5, 2),
        ("cloud", "volume", 0.01, 2),
        ("behavior", "max_characters", 1, 2000),
        ("behavior", "trigger_probability", 0, 100),
        ("behavior", "private_trigger_probability", -1, 100),
        ("behavior", "group_trigger_probability", -1, 100),
        ("behavior", "min_interval_seconds", 0, 3600),
        ("behavior", "timeout", 10, 1800),
        ("behavior", "history_limit", 1, 200),
        ("behavior", "history_max_mb", 64, 4096),
    ]
    for group, name, low, high in ranges:
        if not low <= result[group][name] <= high:
            raise BreezeError(f"{group}.{name} 须介于 {low} 和 {high}。")
    if cloud["language_code"] and not re.fullmatch(
        r"[A-Za-z]{2}", cloud["language_code"]
    ):
        raise BreezeError("语言代码须为空或两位字母，如 zh、en、ja。")
    if len(cloud["model_id"]) > 120 or len(cloud["voice_id"]) > 500:
        raise BreezeError("模型或音色 ID 过长。")
    if cloud["voice_id"]:
        validate_identifier(cloud["voice_id"], "音色 ID")
    return result
