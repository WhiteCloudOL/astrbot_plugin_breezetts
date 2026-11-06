"""Verify actual host adapters serialize controlled requests correctly."""

import base64
import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("astrbot")

from aiohttp import web
from breezetts_test.expression import expression_profile
from breezetts_test.provider_audio import provider_audio


@pytest.mark.parametrize(
    ("source", "class_name", "kind", "model", "direction"),
    [
        (
            "minimax_tts_api_source",
            "ProviderMiniMaxTTSAPI",
            "minimax_tts_api",
            "speech-2.8-hd",
            "happy",
        ),
        (
            "fishaudio_tts_api_source",
            "ProviderFishAudioTTSAPI",
            "fishaudio_tts_api",
            "s2-pro",
            "warmly\nwith restrained excitement",
        ),
        (
            "elevenlabs_tts_source",
            "ProviderElevenLabsTTSAPI",
            "elevenlabs_tts_api",
            "eleven_v3",
            "warmly\nwith restrained excitement",
        ),
        (
            "mimo_tts_api_source",
            "ProviderMiMoTTSAPI",
            "mimo_tts_api",
            "mimo-v2.5-tts",
            "带着惊喜轻声说",
        ),
        ("gsvi_tts_source", "ProviderGSVITTS", "gsvi_tts_api", "", "默认"),
    ],
)
async def test_real_host_adapters_wire_payloads(
    config, server, wav_audio, source, class_name, kind, model, direction
):
    module = importlib.import_module("astrbot.core.provider.sources." + source)
    calls = []
    base = ""

    async def upstream(request):
        if request.method == "GET":
            return web.Response(body=wav_audio, content_type="audio/wav")
        if kind == "fishaudio_tts_api":
            import ormsgpack

            payload = ormsgpack.unpackb(await request.read())
        else:
            payload = await request.json()
        calls.append((payload, dict(request.query), request.path))
        if kind == "minimax_tts_api":
            return web.Response(
                body=(
                    "data: " + json.dumps({"data": {"audio": wav_audio.hex()}}) + "\n\n"
                ).encode(),
                content_type="text/event-stream",
            )
        if kind == "mimo_tts_api":
            return web.json_response(
                {
                    "choices": [
                        {
                            "message": {
                                "audio": {"data": base64.b64encode(wav_audio).decode()}
                            }
                        }
                    ]
                }
            )
        if kind == "gsvi_tts_api":
            return web.json_response({"msg": "合成成功", "audio_url": base + "/audio"})
        return web.Response(body=wav_audio, content_type="audio/wav")

    base = await server(upstream)
    provider = getattr(module, class_name)(
        {
            "id": "wire-test",
            "type": kind,
            "model": model,
            "api_key": "fake-test-key",
            "api_base": base,
        },
        {},
    )
    if kind == "fishaudio_tts_api":
        provider.reference_id = "a" * 32
    if kind == "elevenlabs_tts_api":
        provider.output_format = "wav_24000"
        provider.voice_settings = {"stability": 0.4}
    if kind == "mimo_tts_api":
        provider.dialect = "东北话"
    original = dict(provider.provider_config)
    original_headers = dict(getattr(provider, "headers", {}))
    config["backend"] = "provider"
    try:
        raw, extension = await provider_audio(
            provider, "Hello.", direction, expression_profile(config, provider)
        )
        assert raw[:4] == b"RIFF" and extension == "wav"
        assert len(calls) == 1
        payload, query, path = calls[0]
        if kind == "minimax_tts_api":
            assert payload["voice_setting"]["emotion"] == "happy"
            assert payload["audio_setting"]["format"] == "wav"
            assert payload["text"] == "Hello."
        elif kind == "fishaudio_tts_api":
            assert payload["text"] == "[warmly with restrained excitement]Hello."
            assert payload["reference_id"] == "a" * 32 and payload["format"] == "wav"
        elif kind == "elevenlabs_tts_api":
            assert payload["text"] == "[warmly with restrained excitement]Hello."
            assert payload["voice_settings"] == {"stability": 0.4}
            assert (
                payload["model_id"] == model and query["output_format"] == "wav_24000"
            )
            assert path.endswith("/" + provider.voice_id)
        elif kind == "mimo_tts_api":
            assert (
                payload["messages"][-1]["content"]
                == "<style>带着惊喜轻声说 东北话</style>Hello."
            )
            assert payload["audio"]["voice"] == provider.voice
        else:
            assert payload["emotion"] == "默认" and payload["text"] == "Hello."
        assert provider.provider_config == original
        assert getattr(provider, "headers", {}) == original_headers
    finally:
        if hasattr(provider, "client"):
            await provider.terminate()


async def test_real_azure_ssml_preserves_escaping_and_shared_backend(
    config, monkeypatch, wav_audio
):
    import httpx
    from astrbot.core.provider.sources.azure_tts_source import (
        AzureNativeProvider,
        AzureTTSProvider,
    )

    calls = []

    def capture(request):
        if "issuetoken" in str(request.url).lower():
            return httpx.Response(200, text="fake-token")
        calls.append(request.content.decode())
        return httpx.Response(200, content=wav_audio)

    async def enter(instance):
        instance._client = httpx.AsyncClient(transport=httpx.MockTransport(capture))
        return instance

    monkeypatch.setattr(AzureNativeProvider, "__aenter__", enter)
    provider = AzureTTSProvider(
        {
            "id": "azure-wire",
            "type": "azure_tts",
            "azure_tts_subscription_key": "a" * 32,
            "azure_tts_voice": "zh-CN-XiaoxiaoNeural",
            "azure_tts_style": "cheerful",
        },
        {},
    )
    config["backend"] = "provider"
    await provider_audio(
        provider, "A&B <example>", "whispering", expression_profile(config, provider)
    )
    assert "style='whispering'" in calls[0]
    assert "A&amp;B &lt;example&gt;" in calls[0]
    assert provider.provider.voice_params["style"] == "cheerful"
    assert provider.provider._client is None and not provider.provider.token


async def test_real_gemini_adapter_preserves_voice_and_direction(config, wav_audio):
    from astrbot.core.provider.sources.gemini_tts_source import ProviderGeminiTTSAPI

    provider = object.__new__(ProviderGeminiTTSAPI)
    provider.provider_config = {"type": "gemini_tts"}
    provider.model_name = ""
    provider.model = "gemini-2.5-flash-preview-tts"
    provider.voice_name = "Leda"
    provider.prefix = "Original context"
    generate = AsyncMock(
        return_value=SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(
                        parts=[
                            SimpleNamespace(
                                inline_data=SimpleNamespace(data=wav_audio[44:])
                            )
                        ]
                    )
                )
            ]
        )
    )
    provider.client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
    config["backend"] = "provider"
    await provider_audio(
        provider, "Hello.", "Speak quietly", expression_profile(config, provider)
    )
    sent = generate.await_args.kwargs
    assert sent["contents"] == "Original context Speak quietly: Hello."
    assert sent["model"] == provider.model
    assert (
        sent["config"].speech_config.voice_config.prebuilt_voice_config.voice_name
        == "Leda"
    )
    assert provider.prefix == "Original context"


async def test_real_qwen_sdk_payload_and_credentials(config, wav_audio, monkeypatch):
    import dashscope
    from dashscope.api_entities.dashscope_response import DashScopeAPIResponse
    from dashscope.api_entities.http_request import HttpRequest

    calls = []

    def capture(request):
        _, _, payload = request.data.get_http_payload()
        if isinstance(payload, str):
            payload = json.loads(payload)
        calls.append((payload, request.timeout, request.headers["Authorization"]))
        return DashScopeAPIResponse(
            status_code=200,
            request_id="wire",
            output={"audio": {"data": base64.b64encode(wav_audio).decode()}},
        )

    monkeypatch.setattr(HttpRequest, "call", capture)
    monkeypatch.setattr(dashscope, "api_key", "other-provider-key")
    provider = SimpleNamespace(
        provider_config={"type": "dashscope_tts"},
        model_name="qwen3-tts-instruct-flash",
        voice="Cherry",
        chosen_api_key="own-provider-key",
        request_headers={},
        timeout_ms=3000,
    )
    config["backend"] = "provider"
    await provider_audio(
        provider, "Hello.", "Speak softly", expression_profile(config, provider)
    )
    payload, timeout, auth = calls[0]
    assert payload["input"] == {"text": "Hello.", "voice": "Cherry"}
    assert payload["parameters"]["instructions"] == "Speak softly"
    assert timeout == 3 and auth == "Bearer own-provider-key"
    assert dashscope.api_key == "other-provider-key"


async def test_real_cosyvoice_sdk_payload_and_credentials(
    config, wav_audio, monkeypatch
):
    import dashscope
    from dashscope.audio.tts_v2 import SpeechSynthesizer

    calls = []

    def capture(synthesizer, text, timeout):
        payload = json.loads(
            synthesizer.request.get_start_request(synthesizer.additional_params)
        )
        headers = synthesizer.request.get_websocket_headers(
            headers=synthesizer.headers, workspace=None
        )
        calls.append((payload, headers, timeout))
        return wav_audio

    monkeypatch.setattr(SpeechSynthesizer, "call", capture)
    monkeypatch.setattr(dashscope, "api_key", "other-provider-key")
    provider = SimpleNamespace(
        provider_config={"type": "dashscope_tts"},
        model_name="cosyvoice-v3-flash",
        voice="longhuhu_v3",
        chosen_api_key="own-provider-key",
        request_headers={},
        timeout_ms=0,
    )
    config["backend"] = "provider"
    await provider_audio(
        provider, "Hello.", "happy", expression_profile(config, provider)
    )
    payload, headers, timeout = calls[0]
    assert payload["payload"]["parameters"]["instruction"] == "你说话的情感是happy。"
    assert headers["Authorization"] == "Bearer own-provider-key"
    assert timeout == 1000 and dashscope.api_key == "other-provider-key"
