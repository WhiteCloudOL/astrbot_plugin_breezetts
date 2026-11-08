"""Validate dynamic controls and real per-section HTTP requests."""

import copy
import io
import wave
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from breezetts_test.breeze_audio import join_breeze_audio
from breezetts_test.breeze_control import (
    AUDIO_TAGS,
    clean_breeze_text,
    parse_breeze_text,
)
from breezetts_test.settings import BreezeError


def test_controls_preserve_literals_and_off_mode():
    text = "[语气:温柔][轻笑]你好。[用坚定的语气][停顿]一起走。"
    assert clean_breeze_text(text) == "你好。一起走。"
    assert parse_breeze_text(text, enabled=True) == [
        ("[轻笑]你好。", "温柔"),
        ("[停顿]一起走。", "用坚定的语气"),
    ]
    assert parse_breeze_text(text, enabled=False) == [("你好。一起走。", "")]
    literal = r"[普通括号] [笑](https://example.com) \[笑] `[语气:解释][笑]`"
    assert clean_breeze_text(literal) == literal
    assert parse_breeze_text(literal, enabled=True) == [(literal, "")]
    for values in AUDIO_TAGS.values():
        assert clean_breeze_text("".join(values)) == ""
    assert parse_breeze_text("[style:gentle](laugh)Hello.", enabled=True) == [
        ("(laugh)Hello.", "gentle")
    ]


@pytest.mark.parametrize(
    "prefix,tags",
    [("[吸气]", None), ("[轻笑][停顿]", None), ("(custom breath)", ("(custom breath)",))],
)
def test_leading_events_stay_with_the_following_spoken_section(prefix, tags):
    assert parse_breeze_text(
        prefix + "[语气:急促]少在那里自顾自[语气:认真]脑补奇怪的设定！",
        enabled=True,
        tags=tags,
    ) == [(prefix + "少在那里自顾自", "急促"), ("脑补奇怪的设定！", "认真")]
    assert parse_breeze_text(prefix, enabled=True, tags=tags) == [(prefix, "")]
    assert parse_breeze_text(
        "[语气:轻柔]" + prefix + "[语气:认真]你好。", enabled=True, tags=tags
    ) == [(prefix, "轻柔"), ("你好。", "认真")]


@pytest.mark.parametrize(
    "text",
    [
        "[语气:]你好",
        "[语气:温柔]",
        "[语气:温柔][语气:开心]你好",
        "[语气:" + "a" * 201 + "]Hi",
        "[语气:未结束",
        "[语气:跨\n行]你好",
        "[语气:开心]好。" * 9,
    ],
)
def test_invalid_directions_are_rejected_only_when_enabled(text):
    with pytest.raises(BreezeError):
        parse_breeze_text(text, enabled=True)
    parse_breeze_text(text, enabled=False)


@pytest.mark.parametrize("backend", ["local", "cloud"])
async def test_section_protocols_merge_and_keep_configuration(
    service, config, server, wav_audio, backend
):
    calls = []

    async def handle(request):
        if backend == "local":
            fields = await request.post()
            calls.append((fields["text"], fields["instruction"], fields["cfg_scale"]))
            return web.Response(body=b"\x10\x00" * 2400, content_type="audio/pcm")
        fields = await request.json()
        calls.append(
            (fields["text"], fields["instructions"], request.query["output_format"])
        )
        return web.Response(body=wav_audio, content_type="audio/wav")

    config["backend"] = backend
    config["behavior"]["dynamic_tone"] = True
    config["local"].update(mode="plain", instruction="default voice", cfg_scale=4)
    config["cloud"].update(
        api_key="test",
        voice_id="voice",
        instructions="default voice",
        output_format="mp3",
    )
    config[backend]["base_url"] = await server(handle)
    before = copy.deepcopy(config)
    item = await service.synthesize(
        "[语气:温柔][轻笑]你好。[语气:坚定]一起走。", instruction="slowly"
    )
    assert calls == [
        (
            "[轻笑]你好。",
            "default voice\nslowly\n温柔",
            "4" if backend == "local" else "wav",
        ),
        (
            "一起走。",
            "default voice\nslowly\n坚定",
            "4" if backend == "local" else "wav",
        ),
    ]
    assert config == before
    assert item["name"] == "你好。一起走。"
    with wave.open(str(service.store.path(item["id"])), "rb") as audio:
        assert audio.getnframes() > 4800


async def test_disabled_controls_do_not_reach_upstream(
    service, config, wav_audio, monkeypatch
):
    config["local"].update(mode="plain", instruction="fixed")
    synthesize = AsyncMock(return_value=(wav_audio, "wav"))
    monkeypatch.setattr(service.client, "synthesize", synthesize)
    await service.synthesize("[语气:温柔][笑]你好", instruction="ignored")
    args = synthesize.await_args.args
    assert args[1] == "你好" and args[0]["local"]["instruction"] == "fixed"


async def test_failed_section_leaves_no_partial_output(
    service, config, wav_audio, monkeypatch
):
    config["local"]["mode"] = "plain"
    config["behavior"]["dynamic_tone"] = True
    monkeypatch.setattr(
        service.client,
        "synthesize",
        AsyncMock(
            side_effect=[(wav_audio, "wav"), BreezeError("second section failed")]
        ),
    )
    with pytest.raises(BreezeError):
        await service.synthesize("[语气:温柔]你好。[语气:坚定]走。")
    assert not service.store.items and service.active == 0 and not service.requests


def test_audio_merge_pauses_and_mismatch(wav_audio):
    target = io.BytesIO()
    with wave.open(target, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x01\x00" * 1600)
    with pytest.raises(BreezeError):
        join_breeze_audio([wav_audio, target.getvalue()], ["Hi.", "Hello."])
    with pytest.raises(BreezeError):
        join_breeze_audio([wav_audio, wav_audio[:-10]], ["Hi.", "Hello."])
    merged = join_breeze_audio([wav_audio, wav_audio], ["Hi.[停顿]", "Hello."])
    with wave.open(io.BytesIO(merged), "rb") as audio:
        # This fixture is already quiet: existing silence is counted, not doubled.
        assert audio.getnframes() == 24000 * 8


def test_complete_prompt_template_replaces_variables_once_without_hidden_suffix():
    from breezetts_test.breeze_control import control_prompt

    profile = {
        "name": "Example voice",
        "mode": "fixed",
        "tags": ("[laughs]",),
        "styles": ["happy", "sad"],
        "note": "voice-specific note",
    }
    assert (
        control_prompt(local=False, profile=profile, prompt="Only {platform_name}.")
        == "Only Example voice."
    )
    result = control_prompt(
        local=False,
        profile=profile,
        prompt="{platform_rules}\n{audio_tags}\n{style_values}\n{control_syntax}\n{output_protocol}\n{user_message}\n{passage}",
        user_message="literal {audio_tags}",
        passage="original {platform_name}",
    )
    assert "STRICT fixed styles" in result and "never mandatory" in result
    assert "[laughs]\nhappy, sad" in result
    assert '"literal {audio_tags}"' in result and '"original {platform_name}"' in result
    assert "Both fields are optional" in result
    assert "Return STRICT JSON only" in result and "No Markdown, code fences" in result
    assert '{"text":"你好呀。","instruction":""}' in result
    assert "{control_syntax}" not in result


@pytest.mark.parametrize(
    ("ending", "milliseconds"), [(".", 300), (",", 150), ("word", 100)]
)
def test_custom_provider_events_do_not_hide_boundary_punctuation(ending, milliseconds):
    source = io.BytesIO()
    with wave.open(source, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\xff\x1f" * 2400)
    merged = join_breeze_audio(
        [source.getvalue(), source.getvalue()],
        ["Hello" + ending + "[custom event]", "Next."],
        tags=("[custom event]",),
    )
    with wave.open(io.BytesIO(merged), "rb") as audio:
        assert audio.getnframes() == 4800 + 24000 * milliseconds // 1000
