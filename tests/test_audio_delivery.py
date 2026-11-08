"""Preserve final speech samples through chat preparation and delayed HTTP chunks."""

import asyncio
import io
import wave

import pytest
import soundfile as sf
from aiohttp import web
from breezetts_test import breeze_audio
from breezetts_test.breeze_audio import join_breeze_audio
from breezetts_test.settings import BreezeError


@pytest.mark.parametrize(
    "width,channels,rate",
    [(1, 1, 16000), (2, 1, 24000), (2, 2, 48000), (3, 1, 24000), (4, 2, 24000)],
)
@pytest.mark.parametrize("quiet_ms", [0, 120, 600])
def test_delivery_tail_preserves_every_sample_and_counts_existing_silence(
    width, channels, rate, quiet_ms
):
    sample = b"\xc0" if width == 1 else (1 << (8 * width - 3)).to_bytes(width, "little")
    zero = (b"\x80" if width == 1 else b"\x00") * width * channels
    frames = sample * channels * (rate // 10) + zero * (rate * quiet_ms // 1000)
    source = io.BytesIO()
    with wave.open(source, "wb") as audio:
        audio.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        audio.writeframes(frames)
    output = join_breeze_audio(
        [source.getvalue()], ["Last syllable."], minimum_tail_ms=400, whole_seconds=True
    )
    with wave.open(io.BytesIO(output)) as audio:
        actual = audio.readframes(audio.getnframes())
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (
            channels,
            width,
            rate,
        )
        assert audio.getnframes() % rate == 0
    assert actual[: len(frames)] == frames
    assert len(actual) >= len(frames)
    assert actual[len(frames) :] == zero * ((len(actual) - len(frames)) // len(zero))
    assert len(actual) // len(zero) - rate // 10 >= rate * 400 // 1000
    assert (
        join_breeze_audio(
            [output], ["Last syllable."], minimum_tail_ms=400, whole_seconds=True
        )
        == output
    )


def test_delivery_tail_cannot_exceed_audio_limit(monkeypatch):
    source = io.BytesIO()
    with wave.open(source, "wb") as audio:
        audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\xff\x1f" * 2400)
    raw = source.getvalue()
    # Padding 400 ms fits; the additional whole-second alignment does not.
    monkeypatch.setattr(breeze_audio, "AUDIO_LIMIT", len(raw) + 400 * 48)
    with pytest.raises(BreezeError, match="上限"):
        join_breeze_audio(
            [raw], ["Last syllable."], minimum_tail_ms=400, whole_seconds=True
        )


def test_low_level_tail_noise_does_not_add_an_extra_whole_second():
    rate = 24000
    voice = b"\xff\x1f" * (rate * 24 // 10)
    # Sparse quiet noise exceeds the old per-sample threshold but remains below RMS.
    noise = (b"\x40\x00" + b"\x00\x00" * 119) * 70
    original = voice + noise
    source = io.BytesIO()
    with wave.open(source, "wb") as audio:
        audio.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        audio.writeframes(original)
    output = join_breeze_audio(
        [source.getvalue()], ["Final words."], minimum_tail_ms=400, whole_seconds=True
    )
    with wave.open(io.BytesIO(output)) as audio:
        assert audio.getnframes() == rate * 3
        frames = audio.readframes(audio.getnframes())
    assert frames.startswith(original)
    assert frames[len(original) :] == b"\x00" * (len(frames) - len(original))
    assert (len(frames) - len(voice)) // 2 >= rate * 400 // 1000


@pytest.mark.parametrize("rate", [22050, 24000])
@pytest.mark.parametrize(
    "ending", ["040", "080", "120", "490", "500-1", "500", "500+1", "510", "960", "999"]
)
def test_integer_platform_duration_never_cuts_the_last_syllable(rate, ending):
    milliseconds = int(ending[:3])
    frames = rate * 6 + rate * milliseconds // 1000
    frames += -1 if ending.endswith("-1") else 1 if ending.endswith("+1") else 0
    pcm = b"\xff\x1f" * frames
    source = io.BytesIO()
    with wave.open(source, "wb") as audio:
        audio.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        audio.writeframes(pcm)
    output = join_breeze_audio(
        [source.getvalue()], ["Final word."], minimum_tail_ms=400, whole_seconds=True
    )
    with wave.open(io.BytesIO(output)) as audio:
        count = audio.getnframes()
        actual = audio.readframes(count)
    # SnowLuma uses Math.max(1, Math.round(duration)) for uploaded voice metadata.
    reported_seconds = max(1, int(count / rate + 0.5))
    assert count % rate == 0
    assert reported_seconds * rate >= frames + rate * 400 // 1000
    assert actual[: len(pcm)] == pcm
    assert actual[len(pcm) :] == b"\x00\x00" * (count - frames)


@pytest.mark.parametrize("delivery", ["local", "sync", "stream", "async"])
@pytest.mark.parametrize("for_delivery", [False, True])
@pytest.mark.parametrize("integer_seconds", [False, True])
async def test_chat_audio_preserves_last_http_chunk_and_workbench_format(
    service, config, server, delivery, for_delivery, integer_seconds
):
    pcm = b"\xff\x1f" * 2398 + b"\xff\x7f\x00\x40"
    source = io.BytesIO()
    with wave.open(source, "wb") as audio:
        audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        audio.writeframes(pcm)
    wav = source.getvalue()
    mp3 = io.BytesIO()
    sf.write(mp3, [0.1] * 2400, 24000, format="MP3")
    expected = "wav" if for_delivery else "mp3"

    async def upstream(request):
        if delivery == "local":
            assert (await request.post())["text"] == "保留最后两个字。"
            raw, mime = pcm, "audio/pcm"
        else:
            if request.method == "POST":
                assert (await request.json())["text"] == "保留最后两个字。"
                assert request.query["output_format"] == expected
                if delivery == "async":
                    return web.json_response(
                        {"generation_job_id": "job-test"}, status=202
                    )
            elif not request.path.endswith("/audio"):
                return web.json_response(
                    {"generation_job_id": "job-test", "status": "ready"}
                )
            else:
                assert request.query["output_format"] == expected
            raw, mime = (
                (wav, "audio/wav") if for_delivery else (mp3.getvalue(), "audio/mpeg")
            )
        response = web.StreamResponse(
            headers={"Content-Type": mime, "X-Sample-Rate": "24000"}
        )
        await response.prepare(request)
        await response.write(raw[:-4])
        # The final response chunk contains the last audible samples.
        await asyncio.sleep(0.01)
        await response.write(raw[-4:])
        await response.write_eof()
        return response

    url = await server(upstream)
    config["behavior"]["integer_second_audio"] = integer_seconds
    if delivery == "local":
        config["local"].update(base_url=url, mode="plain")
    else:
        config["backend"] = "cloud"
        config["cloud"].update(
            base_url=url,
            api_key="test-key",
            voice_id="opaque",
            delivery=delivery,
            output_format="mp3",
        )
    original_format = config["cloud"]["output_format"]
    result = await service.synthesize("保留最后两个字。", for_delivery=for_delivery)
    raw = service.store.path(result["id"]).read_bytes()
    if delivery == "local" or for_delivery:
        assert result["extension"] == "wav"
        with wave.open(io.BytesIO(raw)) as audio:
            actual = audio.readframes(audio.getnframes())
        assert actual[: len(pcm)] == pcm
        expected_tail = 21600 if integer_seconds else 9600
        assert actual[len(pcm) :] == (
            b"\x00\x00" * expected_tail if for_delivery else b""
        )
    else:
        assert result["extension"] == "mp3" and raw == mp3.getvalue()
    assert config["cloud"]["output_format"] == original_format
