"""Exercise documented C++ wire formats without a running GPU server."""

import asyncio
import copy
import io
import wave

import pytest
import soundfile as sf
from aiohttp import web
from breezetts_test.client import BreezeClient
from breezetts_test.settings import BreezeError, validate_config


@pytest.fixture
def cpp_config(config):
    config["local"].update(
        implementation="cpp", mode="voice", voice_id="narrator", random_seed=False
    )
    return config


@pytest.fixture
def server_voice():
    return {
        "id": "narrator",
        "frames": 50,
        "seconds": 4.0,
        "encode_ms": 12,
        "saved": False,
        "ref_text": "参考录音文字。",
        "description": "清亮的声音",
        "metadata": {"accent": "zh"},
    }


@pytest.mark.parametrize("key", ["", "cpp-test-key"])
@pytest.mark.parametrize("queue_available", [False, True])
async def test_cpp_health_model_and_optional_fifo(
    service, cpp_config, server, key, queue_available
):
    async def handler(request):
        assert request.headers.get("Authorization") == (
            "Bearer " + key if key else None
        )
        if request.path == "/proxy/health":
            return web.json_response({"status": "ok", "sample_rate": 24000})
        if request.path.endswith("/model"):
            return web.json_response(
                {
                    "model": "breeze-tts-2",
                    "backend": "CUDA0",
                    "sample_rate": 24000,
                    "private": "omit",
                }
            )
        if request.path.endswith("/queue") and queue_available:
            return web.json_response({"active": 1, "waiting": 2, "capacity": 1})
        return web.Response(status=404)

    cpp_config["local"].update(
        base_url=await server(handler) + "/proxy/v1", api_key=key
    )
    result = await service.connection()
    assert result["health"]["status"] == "ok"
    assert result["model"] == {
        "model": "breeze-tts-2",
        "backend": "CUDA0",
        "sample_rate": 24000,
    }
    assert result["queue"] == (
        {"active": 1, "waiting": 2, "capacity": 1} if queue_available else None
    )


async def test_optional_cpp_endpoint_does_not_hide_auth_failure(cpp_config, server):
    async def handler(_request):
        return web.json_response({"detail": "private upstream body"}, status=401)

    cpp_config["local"]["base_url"] = await server(handler)
    client = BreezeClient()
    try:
        with pytest.raises(BreezeError, match="401"):
            await client.cpp.status(cpp_config["local"])
    finally:
        await client.close()


@pytest.mark.parametrize("mode", ["voice", "reference", "plain", "design"])
async def test_cpp_native_forms_sampling_and_split_stream(
    cpp_config, server, wav_audio, mode
):
    async def handler(request):
        assert request.path == "/v1/audio/speech"
        fields = await request.post()
        assert fields["text"] == "[轻笑]你好。"
        assert fields["seed"] == "42" and fields["cfg_scale"] == "4"
        assert fields["temperature"] == "0.7" and fields["top_p"] == "0.9"
        assert fields["top_k"] == "40" and fields["repetition_penalty"] == "1.1"
        assert fields["split_chars"] == "0" and fields["max_new_tokens"] == "900"
        if mode == "voice":
            assert fields["voice_id"] == "narrator" and "ref_audio" not in fields
        elif mode == "reference":
            assert fields["ref_audio"].file.read() == wav_audio
            assert fields["ref_text"] == "参考录音文字。"
        else:
            assert "voice_id" not in fields and "ref_audio" not in fields
        response = web.StreamResponse(
            headers={
                "Content-Type": "audio/pcm",
                "X-Sample-Rate": "24000",
                "X-Sample-Format": "s16le",
            }
        )
        await response.prepare(request)
        await response.write(b"\x01")
        await asyncio.sleep(0.01)
        await response.write(b"\x00\x02\x00")
        await response.write_eof()
        return response

    cpp_config["local"].update(
        base_url=await server(handler),
        mode=mode,
        cfg_scale=4,
        instruction="清晰自然" if mode == "design" else "",
        temperature=0.7,
        top_k=40,
        top_p=0.9,
        repetition_penalty=1.1,
        split_chars=0,
        max_new_tokens=900,
    )
    before = copy.deepcopy(cpp_config)
    client = BreezeClient()
    try:
        raw, extension = await client.synthesize(
            cpp_config,
            "[轻笑]你好。",
            (wav_audio, "参考录音文字。") if mode == "reference" else None,
        )
        with wave.open(io.BytesIO(raw)) as audio:
            assert extension == "wav" and audio.getnframes() == 2
            assert audio.readframes(2) == b"\x01\x00\x02\x00"
        assert cpp_config == before
    finally:
        await client.close()


@pytest.mark.parametrize("requested", ["wav", "mp3"])
async def test_cpp_json_reference_cache_and_streaming_wav_fallback(
    cpp_config, server, wav_audio, server_voice, requested
):
    calls = []
    streamed = (
        wav_audio[:4] + b"\xff" * 4 + wav_audio[8:40] + b"\xff" * 4 + wav_audio[44:]
    )

    async def handler(request):
        calls.append(request.path)
        if request.path.endswith("/voices"):
            form = await request.post()
            assert (
                form["ref_audio"].file.read() == wav_audio
                and form["ref_text"] == "参考录音文字。"
            )
            assert "name" not in form
            return web.json_response(server_voice)
        payload = await request.json()
        assert payload["input"] == "你好。" and payload["voice"] == "narrator"
        assert (
            payload["response_format"] == requested
            and payload["stream_format"] == "audio"
        )
        assert (
            "text" not in payload
            and "ref_audio" not in payload
            and "split_chars" not in payload
        )
        return web.Response(
            body=streamed, content_type="audio/wav", headers={"X-Sample-Rate": "24000"}
        )

    cpp_config["local"].update(
        base_url=await server(handler), mode="reference", output_format=requested
    )
    client = BreezeClient()
    try:
        raw, extension = await client.synthesize(
            cpp_config, "你好。", (wav_audio, "参考录音文字。")
        )
        assert extension == "wav" and raw == wav_audio
        assert calls == ["/v1/voices", "/v1/audio/speech"]
    finally:
        await client.close()


@pytest.mark.parametrize(
    "seed,wire_seed", [(2**31 - 1, 2**31 - 1), (2**31, -(2**31)), (2**32 - 1, -1)]
)
async def test_cpp_seed_preserves_uint32_bits_without_signed_overflow(
    cpp_config, server, seed, wire_seed
):
    async def handler(request):
        assert int((await request.post())["seed"]) == wire_seed
        return web.Response(body=b"\x01\x00", content_type="audio/pcm")

    cpp_config["local"].update(base_url=await server(handler), seed=seed)
    client = BreezeClient()
    try:
        await client.synthesize(cpp_config, "固定种子。")
        assert cpp_config["local"]["seed"] == seed
    finally:
        await client.close()


@pytest.mark.parametrize("delivery", [False, True])
async def test_cpp_chat_and_section_outputs_force_lossless_pcm(
    service, cpp_config, server, wav_audio, delivery
):
    async def handler(request):
        assert request.content_type != "application/json"
        assert "voice_id" in await request.post()
        return web.Response(body=wav_audio[44:], content_type="audio/pcm")

    cpp_config["local"].update(base_url=await server(handler), output_format="mp3")
    cpp_config["behavior"]["dynamic_tone"] = True
    item = await service.synthesize(
        "你好。" if delivery else "[语气:轻快]你好。[语气:认真]一起走。",
        for_delivery=delivery,
    )
    assert item["extension"] == "wav"
    assert cpp_config["local"]["output_format"] == "mp3"
    await service.release_audio({item["id"]})


async def test_cpp_json_mp3_decodes_actual_response(cpp_config, server):
    target = io.BytesIO()
    sf.write(target, [0.2] * 24000, 48000, format="MP3")
    raw = target.getvalue()

    async def handler(request):
        assert (await request.json())["voice"] == "narrator"
        return web.Response(
            body=raw, content_type="audio/mpeg", headers={"X-Sample-Rate": "48000"}
        )

    cpp_config["local"].update(base_url=await server(handler), output_format="mp3")
    client = BreezeClient()
    try:
        assert await client.synthesize(cpp_config, "你好。") == (raw, "mp3")
    finally:
        await client.close()


async def test_cpp_voice_register_edit_lookup_and_unload(
    cpp_config, server, wav_audio, server_voice
):
    async def handler(request):
        assert request.headers["Authorization"] == "Bearer cpp-test-key"
        if request.method == "POST":
            form = await request.post()
            assert form["name"] == "narrator" and form["description"] == "清亮的声音"
            assert form["ref_audio"].file.read() == wav_audio
            server_voice["saved"] = True
        if request.method == "PATCH":
            patch = await request.json()
            assert patch == {"description": "新的描述", "metadata": {"accent": None}}
            server_voice.update(description="新的描述", metadata={})
        if request.method == "DELETE":
            return web.json_response({"deleted": "narrator", "file_kept": True})
        if request.path == "/v1/voices" and request.method == "GET":
            return web.json_response(
                [dict(server_voice, api_key="private-response-field")]
            )
        return web.json_response(server_voice)

    cpp_config["local"].update(base_url=await server(handler), api_key="cpp-test-key")
    client = BreezeClient()
    try:
        saved = await client.cpp.voice(
            cpp_config["local"],
            "register",
            {"name": "narrator", "description": "清亮的声音"},
            (wav_audio, "参考录音文字。"),
        )
        assert saved["saved"]
        assert "api_key" not in (await client.cpp.voices(cpp_config["local"]))[0]
        assert (await client.cpp.voices(cpp_config["local"], "narrator"))[
            "id"
        ] == "narrator"
        edited = await client.cpp.voice(
            cpp_config["local"],
            "edit",
            {"id": "narrator", "description": "新的描述", "metadata": {"accent": None}},
        )
        assert edited["metadata"] == {}
        assert await client.cpp.voice(
            cpp_config["local"], "remove", {"id": "narrator"}
        ) == {"deleted": "narrator", "file_kept": True}
    finally:
        await client.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "../escape"},
        {"description": "啊" * 1366},
        {"metadata": {"bad key": "x"}},
        {"metadata": {"description": "x"}},
        {"metadata": {"a": 1}},
        {"metadata": {"a": "啊" * 342}},
    ],
)
async def test_cpp_rejects_invalid_voice_fields_before_http(
    cpp_config, server, wav_audio, payload
):
    calls = []

    async def handler(request):
        calls.append(request.path)
        return web.Response(status=500)

    cpp_config["local"]["base_url"] = await server(handler)
    client = BreezeClient()
    try:
        with pytest.raises(BreezeError):
            await client.cpp.voice(
                cpp_config["local"], "register", payload, (wav_audio, "参考文字。")
            )
        assert not calls
    finally:
        await client.close()


@pytest.mark.parametrize("use_voice", [False, True])
async def test_cpp_conversion_source_and_target_serialization(
    service, cpp_config, server, wav_audio, use_voice
):
    source = service.store.upload(wav_audio, "source.wav")
    target = service.store.upload(wav_audio, "target.wav")
    service.store.items[target["id"]]["ref_text"] = "目标参考文字。"

    async def handler(request):
        assert request.path == "/v1/audio/convert"
        form = await request.post()
        assert form["source"].file.read() == wav_audio
        assert (
            form["temperature"] == "0.3"
            and form["top_k"] == "1"
            and form["keep_acoustic"] == "0"
        )
        assert form["seed"] == "42" and form["text"] == "源录音文字。"
        if use_voice:
            assert form["voice_id"] == "narrator" and "ref_audio" not in form
        else:
            assert (
                form["ref_audio"].file.read() == wav_audio
                and form["ref_text"] == "目标参考文字。"
            )
        return web.Response(body=wav_audio[44:], content_type="audio/pcm")

    cpp_config["local"]["base_url"] = await server(handler)
    result = await service.convert_audio(
        {
            "source_id": source["id"],
            "reference_id": target["id"],
            "voice_id": "narrator" if use_voice else "",
            "text": "源录音文字。",
            "seed": 42,
        }
    )
    assert result["conversion"] is True and result["extension"] == "wav"
    assert service.store.path(result["id"]).read_bytes() == wav_audio


@pytest.mark.parametrize(
    "field,value",
    [
        ("temperature", float("nan")),
        ("top_k", 1.1),
        ("keep_acoustic", 16),
        ("seed", True),
        ("cfg_scale", 0),
    ],
)
async def test_cpp_conversion_parameter_bounds(cpp_config, wav_audio, field, value):
    client = BreezeClient()
    try:
        with pytest.raises(BreezeError):
            await client.cpp.convert(
                cpp_config["local"], {"voice_id": "narrator", field: value}, wav_audio
            )
        assert client.session is None
    finally:
        await client.close()


async def test_cpp_cannot_remove_selected_voice_or_target_changed_server(
    service, cpp_config
):
    with pytest.raises(BreezeError, match="当前服务端音色"):
        await service.local_voice("remove", {"id": "narrator"})
    with pytest.raises(BreezeError, match="服务地址已变化"):
        await service.local_voice(
            "edit", {"id": "narrator", "server_url": "http://another-server"}
        )


async def test_cpp_metadata_edit_remains_available_while_audio_is_generating(
    service, cpp_config, server, server_voice
):
    async def handler(request):
        assert request.method == "PATCH"
        return web.json_response(server_voice)

    cpp_config["local"]["base_url"] = await server(handler)
    async with service.local_slot:
        result = await asyncio.wait_for(
            service.local_voice("edit", {"id": "narrator", "description": "更新描述"}),
            2,
        )
    assert result["id"] == "narrator"


@pytest.mark.parametrize(
    "field,value",
    [
        ("implementation", "unknown"),
        ("top_p", 1.01),
        ("split_chars", -2),
        ("top_k", 1.5),
        ("voice_id", "../voice"),
        ("output_format", "opus"),
    ],
)
def test_cpp_config_validation(cpp_config, field, value):
    cpp_config["local"][field] = value
    with pytest.raises(BreezeError):
        validate_config(cpp_config)
