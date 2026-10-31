"""Validate configuration at the AstrBot and WebUI boundaries."""

from __future__ import annotations

import copy
import math
import re
from urllib.parse import urlsplit, urlunsplit

DEFAULTS = {
    "backend": "local",
    "local": {
        "base_url": "http://127.0.0.1:7860",
        "api_key": "",
        "mode": "reference",
        "reference_id": "",
        "instruction": "",
        "cfg_scale": 1.0,
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
    "behavior": {
        "auto_tts": False,
        "keep_text": True,
        "tool_enabled": True,
        "max_characters": 1000,
        "timeout": 600,
        "history_limit": 50,
    },
}
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
    }:
        raise BreezeError("请选择本地 API 或 BreezeBlue 官方 API。")
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
    local, cloud = result["local"], result["cloud"]
    if local["mode"] not in {"reference", "design", "plain"}:
        raise BreezeError("本地模式须为参考音色、声音设计或普通朗读。")
    if cloud["output_format"] not in FORMATS or cloud["delivery"] not in {
        "sync",
        "async",
        "stream",
    }:
        raise BreezeError("官方输出格式或交付方式无效。")
    ranges = [
        ("local", "cfg_scale", 0.01, 10),
        ("local", "seed", 0, 2**32 - 1),
        ("cloud", "guidance_scale", 1, 10),
        ("cloud", "speed", 0.5, 2),
        ("cloud", "volume", 0.01, 2),
        ("behavior", "max_characters", 1, 2000),
        ("behavior", "timeout", 10, 1800),
        ("behavior", "history_limit", 1, 200),
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
