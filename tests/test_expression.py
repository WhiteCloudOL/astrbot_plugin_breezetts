"""Exercise model presets, isolated host controls, and caption consistency."""

import copy
import io
import json
import wave
from types import SimpleNamespace

import pytest
from aiohttp import web
from breezetts_test.breeze_control import clean_breeze_text, control_prompt
from breezetts_test.expression import (
    check_directions,
    expression_profile,
    provider_preset,
)
from breezetts_test.provider_audio import provider_audio
from breezetts_test.settings import BreezeError, validate_config


class FileProvider:
    """Record effective per-request state while returning real WAV audio."""

    def __init__(self, kind, model, path):
        self.provider_config = {
            "id": "voice",
            "type": kind,
            "private_key": "never-publish",
        }
        self.model_name = model
        self.path = path
        self.voice_setting = {"emotion": "calm", "voice_id": "speaker"}
        self.audio_setting = {"format": "mp3"}
        self.emotion = "默认"
        self.headers = {"model": model}
        self.style_prompt = "existing style"
        self.prefix = "existing context"
        self.calls = []
        self.provider = SimpleNamespace(
            voice_params={"style": "cheerful"}, _client="original"
        )

    def meta(self):
        return SimpleNamespace(id="voice")

    async def get_audio(self, text):
        self.calls.append(
            {
                "text": text,
                "emotion": self.emotion,
                "voice": dict(self.voice_setting),
                "style": self.style_prompt,
                "prefix": self.prefix,
                "inner": dict(self.provider.voice_params),
            }
        )
        return str(self.path)


@pytest.mark.parametrize(
    ("kind", "model", "expected"),
    [
        ("minimax_tts_api", "speech-2.8-hd", "minimax"),
        ("elevenlabs_tts_api", "eleven_v3", "elevenlabs"),
        ("elevenlabs_tts_api", "eleven_v4_turbo", "elevenlabs"),
        ("elevenlabs_tts_api", "eleven_multilingual_v2", ""),
        ("fishaudio_tts_api", "s2.1-pro", "fish"),
        ("fishaudio_tts_api", "s1", "fish_s1"),
        ("openai_tts_api", "gpt-4o-mini-tts", "openai"),
        ("openai_tts_api", "tts-1-hd", ""),
        ("dashscope_tts", "qwen3-tts-instruct-flash", "qwen"),
        ("dashscope_tts", "qwen3-tts-flash", ""),
        ("azure_tts", "", "azure"),
        ("volcengine_tts", "", "volcengine"),
        ("gsvi_tts_api", "", "gsvi"),
        ("gemini_tts", "gemini-2.5-flash-preview-tts", "gemini"),
        ("mimo_tts_api", "mimo-v2.5-tts", "mimo"),
        ("edge_tts", "", ""),
    ],
)
def test_model_capability_detection(kind, model, expected):
    provider = SimpleNamespace(provider_config={"type": kind}, model_name=model)
    assert provider_preset(provider) == expected


def test_custom_catalogs_and_strict_vs_free_prompts(config, tmp_path):
    config["backend"] = "provider"
    provider = FileProvider("minimax_tts_api", "speech-2.8-hd", tmp_path / "audio.wav")
    config["expression"]["minimax_tags"] = "(laughs)\n(custom event)"
    config["expression"]["minimax_styles"] = "happy\ncalm"
    profile = expression_profile(validate_config(config), provider)
    prompt = control_prompt(local=False, profile=profile)
    assert "STRICT fixed styles" in prompt and "happy, calm" in prompt
    check_directions(profile, "", "happy")
    with pytest.raises(BreezeError):
        check_directions(profile, "warm and gentle")
    text = (
        "(custom event)Hello `[语气:literal]` [laughs](https://example.com) [ordinary]"
    )
    assert (
        clean_breeze_text(text, profile["tags"])
        == "Hello `[语气:literal]` [laughs](https://example.com) [ordinary]"
    )
    provider.provider_config["type"] = "fishaudio_tts_api"
    provider.model_name = "s2-pro"
    free = expression_profile(config, provider)
    assert "FREE delivery instructions" in control_prompt(local=False, profile=free)
    check_directions(free, "quietly, with a hint of excitement")
    assert "(custom event)" not in free["tags"]


@pytest.mark.parametrize(
    "catalog",
    [
        "laughs",
        "[style:hacked]",
        "[nested[tag]]",
        "[line\x00]",
        "[a]" * 101,
        "[tag]\n" * 101,
    ],
)
def test_invalid_custom_native_catalog(config, catalog):
    config["expression"]["fish_tags"] = catalog
    with pytest.raises(BreezeError):
        validate_config(config)


@pytest.mark.parametrize(
    ("kind", "model", "direction", "field", "expected"),
    [
        (
            "minimax_tts_api",
            "speech-2.8-hd",
            "happy",
            "voice",
            {"emotion": "happy", "voice_id": "speaker"},
        ),
        ("gsvi_tts_api", "", "激动", "emotion", "激动"),
        ("azure_tts", "", "whispering", "inner", {"style": "whispering"}),
        (
            "mimo_tts_api",
            "mimo-v2.5-tts",
            "restrained excitement",
            "style",
            "restrained excitement",
        ),
        (
            "gemini_tts",
            "gemini-2.5-flash-preview-tts",
            "nervously",
            "prefix",
            "existing context nervously",
        ),
        ("fishaudio_tts_api", "s2-pro", "warmly", "text", "[warmly]Hello."),
        ("fishaudio_tts_api", "s1", "sad", "text", "(sad)Hello."),
        ("elevenlabs_tts_api", "eleven_v3", "whispering", "text", "[whispering]Hello."),
    ],
)
async def test_provider_controls_do_not_mutate_shared_instance(
    config, tmp_path, wav_audio, kind, model, direction, field, expected
):
    path = tmp_path / "audio.wav"
    path.write_bytes(wav_audio)
    provider = FileProvider(kind, model, path)
    original = copy.deepcopy(provider.__dict__)
    config["backend"] = "provider"
    if kind == "gsvi_tts_api":
        config["expression"]["gsvi_styles"] = "默认\n激动"
    profile = expression_profile(config, provider)
    raw, extension = await provider_audio(provider, "Hello.", direction, profile)
    assert provider.calls[0][field] == expected
    assert extension == "wav"
    with wave.open(io.BytesIO(raw)) as audio:
        assert audio.getsampwidth() == 2 and audio.getnchannels() == 1
    for name, value in original.items():
        if name != "calls":
            assert getattr(provider, name) == value


async def test_host_synthesis_sections_fixed_validation_and_off_gate(
    service, config, tmp_path, wav_audio
):
    path = tmp_path / "voice.wav"
    path.write_bytes(wav_audio)
    provider = FileProvider("minimax_tts_api", "speech-2.8-hd", path)
    service.context = SimpleNamespace(
        get_provider_by_id=lambda _: provider,
        get_all_tts_providers=lambda: [provider],
        get_all_providers=lambda: [],
    )
    config["backend"] = "provider"
    config["provider"]["provider_id"] = "voice"
    config["behavior"]["dynamic_tone"] = True
    result = await service.synthesize("[语气:happy](laughs)Hello.[语气:sad]Goodbye.")
    assert [call["voice"]["emotion"] for call in provider.calls] == ["happy", "sad"]
    assert service.store.path(result["id"]).is_file()
    assert result["name"] == "Hello.Goodbye."
    assert provider.voice_setting["emotion"] == "calm"
    with pytest.raises(BreezeError, match="固定"):
        await service.synthesize("[语气:whisper warmly]Hello.")
    assert len(provider.calls) == 2
    config["behavior"]["dynamic_tone"] = False
    await service.synthesize("[语气:invented](laughs)Hello.", instruction="invented")
    assert provider.calls[-1]["text"] == "Hello."
    assert provider.calls[-1]["voice"]["emotion"] == "calm"
    state = await service.state()
    assert state["tts_providers"][0]["profile"]["key"] == "minimax"
    assert "never-publish" not in json.dumps(state)
    assert (await service.connection())["status"] == "configured"


async def test_openai_instructions_reach_real_sdk(config, wav_audio):
    pytest.importorskip("openai")
    import httpx
    from openai import AsyncOpenAI

    calls = []

    def capture(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200, content=wav_audio, headers={"content-type": "audio/wav"}
        )

    async with AsyncOpenAI(
        api_key="fake-test-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(capture)),
    ) as client:
        provider = SimpleNamespace(
            provider_config={"type": "openai_tts_api"},
            model_name="gpt-4o-mini-tts",
            voice="coral",
            client=client,
        )
        config["backend"] = "provider"
        raw, _ = await provider_audio(
            provider,
            "Hello.",
            "Speak with restrained excitement.",
            expression_profile(config, provider),
        )
    assert raw[:4] == b"RIFF"
    assert calls[0]["instructions"] == "Speak with restrained excitement."
    assert calls[0]["input"] == "Hello." and calls[0]["response_format"] == "wav"


async def test_volcengine_emotion_payload(config, wav_audio, server):
    import base64

    calls = []

    async def upstream(request):
        calls.append(await request.json())
        return web.json_response({"data": base64.b64encode(wav_audio).decode()})

    provider = SimpleNamespace(
        provider_config={"type": "volcengine_tts"},
        model_name="",
        api_base=await server(upstream),
        api_key="private",
        timeout=5,
        request_headers={},
        _build_request_payload=lambda text: {
            "audio": {"voice_type": "speaker"},
            "request": {"text": text},
        },
    )
    config["backend"] = "provider"
    await provider_audio(
        provider, "Hello.", "happy", expression_profile(config, provider)
    )
    assert calls == [
        {
            "audio": {"voice_type": "speaker", "emotion": "happy"},
            "request": {"text": "Hello."},
        }
    ]


async def test_dashscope_instruction_protocols(config, wav_audio, monkeypatch):
    pytest.importorskip("dashscope")
    import dashscope
    from dashscope.aigc.multimodal_conversation import MultiModalConversation
    from dashscope.audio import tts_v2

    calls = []

    class Synthesizer:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            self.request = SimpleNamespace(apikey="wrong-global")

        def call(self, text, timeout):
            assert self.apikey == self.request.apikey == "own-key"
            assert dashscope.api_key == "different-key"
            return wav_audio

    monkeypatch.setattr(tts_v2, "SpeechSynthesizer", Synthesizer)
    monkeypatch.setattr(dashscope, "api_key", "different-key")
    provider = SimpleNamespace(
        provider_config={"type": "dashscope_tts"},
        model_name="cosyvoice-v3-flash",
        voice="longhuhu_v3",
        chosen_api_key="own-key",
        request_headers={},
        timeout_ms=1000,
    )
    config["backend"] = "provider"
    await provider_audio(
        provider, "Hello.", "happy", expression_profile(config, provider)
    )
    assert calls[0]["instruction"] == "你说话的情感是happy。"
    provider.model_name = "qwen3-tts-instruct-flash"

    def capture(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            output={
                "audio": {"data": __import__("base64").b64encode(wav_audio).decode()}
            }
        )

    async def extract(response):
        assert response == "response"
        return wav_audio

    provider._extract_audio_from_response = extract
    monkeypatch.setattr(MultiModalConversation, "call", capture)
    await provider_audio(
        provider, "Hello.", "warmly", expression_profile(config, provider)
    )
    assert calls[-1]["instructions"] == "warmly" and calls[-1]["api_key"] == "own-key"


@pytest.mark.parametrize("value", ["warm [laughing]", "soft</style>"])
def test_free_directions_cannot_embed_native_markup(config, value):
    provider = SimpleNamespace(
        provider_config={"type": "fishaudio_tts_api"}, model_name="s2-pro"
    )
    config["backend"] = "provider"
    with pytest.raises(BreezeError, match="嵌套"):
        check_directions(expression_profile(config, provider), value)


async def test_missing_provider_has_safe_caption_fallback(service, config):
    config["backend"] = "provider"
    config["provider"]["provider_id"] = "removed"
    config["expression"]["fish_tags"] = "[custom event]"
    assert service.clean_text("[custom event][style:soft]Hello.") == "Hello."
    with pytest.raises(BreezeError, match="提供商"):
        await service.synthesize("Hello.")


@pytest.mark.parametrize("backend", ["local", "cloud"])
def test_breeze_reference_table_does_not_limit_free_directions(config, backend):
    config["backend"] = backend
    config["expression"][f"breeze_{backend}_tags"] = "[自定义事件]"
    profile = expression_profile(config)
    assert profile["tag_policy"] == "reference"
    assert profile["prompt_tags"] == ["[自定义事件]"]
    assert "[轻笑]" in profile["tags"] and "[自定义事件]" in profile["tags"]
    direction = "努力藏住突然重逢的惊喜，语速先快后慢，最后轻声安慰"
    check_directions(profile, direction)
    rendered = control_prompt(local=backend == "local", profile=profile)
    assert (
        "OPEN performance vocabulary" in rendered
        and "not an exhaustive allowlist" in rendered
    )
    assert "Use only exact native token spellings from the catalog" not in rendered
    assert "Breeze 推荐参考" in rendered and "[自定义事件]" in rendered
    assert (
        clean_breeze_text(
            f"[语气:{direction}][轻笑][自定义事件]你好。", profile["tags"]
        )
        == "你好。"
    )
    assert (
        clean_breeze_text(
            "[普通括号] `[语气:literal]` [笑](https://example.com)", profile["tags"]
        )
        == "[普通括号] `[语气:literal]` [笑](https://example.com)"
    )


async def test_host_provider_is_captured_once_for_all_sections(
    service, config, tmp_path, wav_audio
):
    path = tmp_path / "captured.wav"
    path.write_bytes(wav_audio)
    provider = FileProvider("minimax_tts_api", "speech-2.8-hd", path)
    lookups = []

    def lookup(provider_id):
        lookups.append(provider_id)
        return provider if len(lookups) == 1 else None

    service.context = SimpleNamespace(get_provider_by_id=lookup)
    config["backend"] = "provider"
    config["provider"]["provider_id"] = "voice"
    config["behavior"]["dynamic_tone"] = True
    item = await service.synthesize("[语气:happy]Hello.[语气:sad]Goodbye.")
    assert item["backend"] == "provider" and lookups == ["voice"]
    assert [call["voice"]["emotion"] for call in provider.calls] == ["happy", "sad"]


@pytest.mark.parametrize(
    ("kind", "model"),
    [("fishaudio_tts_api", "s2-pro"), ("elevenlabs_tts_api", "eleven_v3")],
)
async def test_inline_provider_combined_directions_are_single_line(
    config, tmp_path, wav_audio, kind, model
):
    path = tmp_path / "inline.wav"
    path.write_bytes(wav_audio)
    provider = FileProvider(kind, model, path)
    config["backend"] = "provider"
    await provider_audio(
        provider,
        "Hello.",
        "warmly\nwith restrained excitement",
        expression_profile(config, provider),
    )
    assert provider.calls[0]["text"] == "[warmly with restrained excitement]Hello."


async def test_qwen_generation_timeout_is_forwarded_to_sdk(
    config, wav_audio, monkeypatch
):
    pytest.importorskip("dashscope")
    import base64

    from dashscope.aigc.multimodal_conversation import MultiModalConversation

    calls = []

    def capture(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            output={"audio": {"data": base64.b64encode(wav_audio).decode()}}
        )

    monkeypatch.setattr(MultiModalConversation, "call", capture)
    provider = SimpleNamespace(
        provider_config={"type": "dashscope_tts"},
        model_name="qwen3-tts-instruct-flash",
        voice="Cherry",
        chosen_api_key="own-key",
        request_headers={},
        timeout_ms=3000,
    )
    config["backend"] = "provider"
    await provider_audio(
        provider, "Hello.", "warmly", expression_profile(config, provider)
    )
    assert calls[0]["request_timeout"] == 3
