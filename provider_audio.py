"""Apply per-request performance controls to configured AstrBot TTS providers."""

from __future__ import annotations

import asyncio
import base64
import copy
import io
import math
from pathlib import Path

import aiohttp
import soundfile as sf

from .expression import check_directions, provider_preset
from .settings import BreezeError
from .storage import AUDIO_LIMIT


async def provider_audio(
    provider, text: str, direction: str, profile: dict
) -> tuple[bytes, str]:
    """Synthesize without changing the shared provider or its stored settings.

    Args:
        provider: Initialized host provider with its existing credentials and voice.
        text: One bounded speech section, retaining supported native events.
        direction: Validated overall or local direction, empty for default delivery.
        profile: Effective capability profile for this model.

    Returns:
        Bounded PCM WAV bytes and the wav extension.

    Raises:
        BreezeError: If the provider fails or returns invalid/oversized audio.
    """
    if provider is None:
        raise BreezeError("所选 TTS 提供商已不可用，请重新选择。")
    detected = provider_preset(provider)
    if detected != profile["key"] and not (
        provider.provider_config.get("type") == "dashscope_tts"
        and profile["key"] in {"qwen", "cosyvoice", "cosyvoice_fixed"}
    ):
        raise BreezeError("TTS 模型已更改，请刷新工作台并重新选择控制协议。")
    check_directions(profile, direction)
    instance = copy.copy(provider)
    instance.provider_config = copy.deepcopy(provider.provider_config)
    key = profile["key"]
    raw = None
    path = None
    try:
        if key == "minimax":
            instance.voice_setting = dict(provider.voice_setting)
            instance.audio_setting = {**provider.audio_setting, "format": "wav"}
            if direction:
                instance.voice_setting["emotion"] = direction
        elif key == "gsvi":
            if direction:
                instance.emotion = direction
        elif key == "azure":
            instance.provider = copy.copy(provider.provider)
            if hasattr(instance.provider, "voice_params"):
                instance.provider.voice_params = dict(provider.provider.voice_params)
                instance.provider._client = None
                if direction:
                    instance.provider.voice_params["style"] = direction
            if direction:
                instance.provider_config["azure_tts_style"] = direction
        elif key == "mimo":
            instance.style_prompt = direction or provider.style_prompt
        elif key == "gemini":
            instance.prefix = " ".join(filter(None, (provider.prefix, direction)))
        elif key in {"fish", "fish_s1", "elevenlabs"}:
            if hasattr(provider, "headers"):
                instance.headers = dict(provider.headers)
            if direction:
                inline_direction = " ".join(direction.split())
                text = (
                    f"({inline_direction})"
                    if key == "fish_s1"
                    else f"[{inline_direction}]"
                ) + text
        elif key == "openai":
            kwargs = {
                "model": provider.model_name,
                "voice": provider.voice,
                "input": text,
                "response_format": "wav",
            }
            if direction:
                kwargs["instructions"] = direction
            output = bytearray()
            async with provider.client.audio.speech.with_streaming_response.create(
                **kwargs
            ) as response:
                async for chunk in response.iter_bytes(chunk_size=65536):
                    output.extend(chunk)
                    if len(output) > AUDIO_LIMIT:
                        raise BreezeError("TTS 返回音频超过大小上限。")
            raw = bytes(output)
        elif key in {"qwen", "cosyvoice", "cosyvoice_fixed"}:
            # These SDK calls have no native asynchronous interface.
            timeout = max(1, min(provider.timeout_ms / 1000, 120))
            if key == "qwen":
                from dashscope.aigc.multimodal_conversation import (
                    MultiModalConversation,
                )

                kwargs = {
                    "model": provider.model_name,
                    "voice": provider.voice,
                    "text": text,
                    "messages": None,
                    "api_key": provider.chosen_api_key,
                    "headers": dict(provider.request_headers),
                    "request_timeout": timeout,
                }
                if direction:
                    kwargs["instructions"] = direction
                response = await asyncio.to_thread(
                    MultiModalConversation.call, **kwargs
                )
                output = getattr(response, "output", None)
                audio = (
                    output.get("audio")
                    if isinstance(output, dict)
                    else getattr(output, "audio", None)
                )
                data = (
                    audio.get("data")
                    if isinstance(audio, dict)
                    else getattr(audio, "data", None)
                )
                url = (
                    audio.get("url")
                    if isinstance(audio, dict)
                    else getattr(audio, "url", None)
                )
                if data:
                    if len(data) > AUDIO_LIMIT * 4 // 3 + 4:
                        raise BreezeError("TTS 返回音频超过大小上限。")
                    raw = base64.b64decode(data, validate=True)
                elif url:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            url,
                            timeout=aiohttp.ClientTimeout(total=timeout),
                        ) as audio_response:
                            if audio_response.status != 200:
                                raise BreezeError("TTS 音频下载失败。")
                            content = bytearray()
                            async for chunk in audio_response.content.iter_chunked(
                                65536
                            ):
                                content.extend(chunk)
                                if len(content) > AUDIO_LIMIT:
                                    raise BreezeError("TTS 返回音频超过大小上限。")
                            raw = bytes(content)
            else:
                from dashscope.audio.tts_v2 import AudioFormat, SpeechSynthesizer

                if key == "cosyvoice_fixed" and direction:
                    direction = f"你说话的情感是{direction}。"
                if len(direction) > 128:
                    raise BreezeError("CosyVoice 指令不能超过 128 字符。")
                synthesizer = SpeechSynthesizer(
                    model=provider.model_name,
                    voice=provider.voice,
                    format=AudioFormat.WAV_24000HZ_MONO_16BIT,
                    instruction=direction or None,
                    headers={
                        name.lower(): value
                        for name, value in provider.request_headers.items()
                    },
                )
                # Override SDK instance credentials, never the process-wide API key.
                synthesizer.apikey = provider.chosen_api_key
                synthesizer.request.apikey = provider.chosen_api_key
                raw = await asyncio.to_thread(
                    synthesizer.call, text, int(timeout * 1000)
                )
        elif key == "volcengine":
            # The legacy host adapter logs authorization headers; use a sanitized request.
            payload = provider._build_request_payload(text)
            if direction:
                payload["audio"]["emotion"] = direction
            async with aiohttp.ClientSession(
                headers=provider.request_headers
            ) as session:
                async with session.post(
                    provider.api_base,
                    json=payload,
                    headers={"Authorization": f"Bearer; {provider.api_key}"},
                    timeout=aiohttp.ClientTimeout(total=provider.timeout),
                ) as response:
                    if response.status != 200:
                        raise BreezeError("火山 TTS 请求失败，请检查服务配置。")
                    content = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        content.extend(chunk)
                        if len(content) > AUDIO_LIMIT * 4 // 3 + 65536:
                            raise BreezeError("TTS 返回音频超过大小上限。")
                    import json

                    data = json.loads(content)
                    raw = base64.b64decode(data["data"], validate=True)
        if raw is None:
            if key in {"openai", "qwen", "cosyvoice", "cosyvoice_fixed", "volcengine"}:
                raise BreezeError("TTS 未返回有效音频。")
            path = Path(await instance.get_audio(text))
            if not path.is_file() or not 44 <= path.stat().st_size <= AUDIO_LIMIT:
                raise BreezeError("TTS 返回的音频文件无效或超过大小上限。")
            raw = await asyncio.to_thread(path.read_bytes)
        if not raw or len(raw) > AUDIO_LIMIT:
            raise BreezeError("TTS 返回的音频为空或超过大小上限。")
        with sf.SoundFile(io.BytesIO(raw)) as audio:
            if (
                not 8000 <= audio.samplerate <= 192000
                or not 1 <= audio.channels <= 2
                or audio.frames < 1
                or audio.frames * audio.channels * 4 > AUDIO_LIMIT
            ):
                raise BreezeError("TTS 返回音频的采样参数或解码大小无效。")
            samples = audio.read(dtype="float32", always_2d=True)
            if (
                len(samples) != audio.frames
                or not math.isfinite(float(samples.max()))
                or not math.isfinite(float(samples.min()))
            ):
                raise BreezeError("TTS 返回音频包含无效采样。")
            output = io.BytesIO()
            sf.write(
                output,
                samples.mean(axis=1),
                audio.samplerate,
                format="WAV",
                subtype="PCM_16",
            )
            return output.getvalue(), "wav"
    except BreezeError:
        raise
    except Exception:
        # Host exceptions can contain keys, dialogue, URLs, or SDK response bodies.
        raise BreezeError(
            "AstrBot TTS 合成失败，请检查提供商、模型、音色及控制表配置。",
            code="upstream",
        ) from None
    finally:
        if path is not None:
            try:
                from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

                resolved = path.resolve()
                if resolved.is_relative_to(Path(get_astrbot_temp_path()).resolve()):
                    resolved.unlink(missing_ok=True)
            except (ImportError, OSError):
                pass
