"""Exercise real HTTP serialization for both Breeze protocols."""

import asyncio
import base64
import copy
import io
import threading
import wave

import pytest
from aiohttp import web
from breezetts_test.client import BreezeClient
from breezetts_test.settings import BreezeError


@pytest.mark.parametrize("api_key", ["", "test-local-key"])
@pytest.mark.parametrize("suffix", ["", "/v1", "/v1/audio/speech"])
async def test_local_multipart_auth_and_pcm(server, config, wav_audio, api_key, suffix):
    async def handle(request):
        assert request.path == "/proxy/v1/audio/speech"
        assert request.headers.get("Authorization") == (
            f"Bearer {api_key}" if api_key else None
        )
        fields = {}
        async for part in await request.multipart():
            fields[part.name] = bytes(await part.read())
        assert fields["ref_audio"] == wav_audio
        assert fields["ref_text"].decode() == "录音的完整文字。"
        assert fields["text"].decode() == "你好，世界！"
        assert fields["instruction"].decode() == "轻声"
        assert float(fields["cfg_scale"]) == 4
        assert fields["seed"] == b"43"
        response = web.StreamResponse(
            headers={
                "Content-Type": "audio/pcm",
                "X-Sample-Rate": "24000",
                "X-Sample-Format": "s16le",
            }
        )
        await response.prepare(request)
        await response.write(b"\x01")
        await response.write(b"\x00\x02\x00")
        await response.write_eof()
        return response

    config["local"].update(
        base_url=await server(handle) + "/proxy" + suffix,
        api_key=api_key,
        cfg_scale=4,
        seed=43,
        instruction="轻声",
    )
    client = BreezeClient()
    try:
        data, extension = await client.synthesize(
            config, "你好，世界！", (wav_audio, "录音的完整文字。")
        )
        assert extension == "wav"
        with wave.open(io.BytesIO(data)) as result:
            assert result.getframerate() == 24000
            assert result.getnchannels() == 1
            assert result.readframes(2) == b"\x01\x00\x02\x00"
    finally:
        await client.close()


@pytest.mark.parametrize("mode", ["design", "plain"])
async def test_local_without_recording(server, service, config, mode):
    async def handle(request):
        fields = await request.post()
        assert "ref_audio" not in fields and "ref_text" not in fields
        assert fields["text"] == "声音设计"
        return web.Response(body=b"\0\0", content_type="audio/pcm")

    config["local"].update(
        base_url=await server(handle),
        mode=mode,
        instruction="清晰的年轻女声" if mode == "design" else "",
    )
    item = await service.synthesize("声音设计")
    assert service.store.path(item["id"]).read_bytes().startswith(b"RIFF")


@pytest.mark.parametrize("delivery", ["sync", "stream", "async"])
async def test_cloud_delivery_protocol(server, config, wav_audio, delivery):
    seen = []

    async def handle(request):
        seen.append(request.path)
        assert request.headers["Authorization"] == "Bearer test-cloud-key"
        if request.method == "POST":
            assert request.path == "/v1/text-to-speech/voc_test" + (
                "/stream" if delivery == "stream" else ""
            )
            assert request.query["output_format"] == "wav"
            body = await request.json()
            assert body == {
                "text": "官方测试",
                "model_id": "breeze-tts-2",
                "language_code": "zh",
                "instructions": "温柔",
                "voice_settings": {
                    "guidance_scale": 4,
                    "speed": 1.25,
                    "volume": 0.8,
                    "use_speaker_boost": True,
                },
            }
            if delivery == "async":
                return web.json_response({"generation_job_id": "job_test"}, status=202)
        elif request.path.endswith("/job_test"):
            return web.json_response(
                {
                    "generation_job_id": "job_test",
                    "status": "ready",
                    "download_url": "https://untrusted.invalid/never-use",
                }
            )
        return web.Response(body=wav_audio, content_type="audio/wav")

    config["backend"] = "cloud"
    config["cloud"].update(
        base_url=await server(handle),
        api_key="test-cloud-key",
        voice_id="voc_test",
        model_id="breeze-tts-2",
        language_code="zh",
        instructions="温柔",
        guidance_scale=4,
        speed=1.25,
        volume=0.8,
        speaker_boost=True,
        delivery=delivery,
    )
    client = BreezeClient()
    try:
        assert await client.synthesize(config, "官方测试") == (wav_audio, "wav")
        assert len(seen) == (3 if delivery == "async" else 1)
    finally:
        await client.close()


@pytest.mark.parametrize("status", [302, 400, 401, 402, 403, 404, 422, 429, 500])
async def test_errors_do_not_leak_or_retry(server, service, config, status):
    requests = []

    async def handle(request):
        requests.append(request.path)
        return web.Response(
            status=status,
            text="private-user-key and sensitive upstream response",
            headers={"Location": "http://127.0.0.1:1/private-user-key"},
        )

    config["local"].update(
        base_url=await server(handle), mode="plain", api_key="private-user-key"
    )
    with pytest.raises(BreezeError) as failure:
        await service.synthesize("测试")
    assert f"HTTP {status}" in str(failure.value)
    assert "private-user-key" not in str(failure.value)
    assert len(requests) == 1
    assert not service.store.items


@pytest.mark.parametrize(
    "headers,data",
    [
        ({"X-Sample-Rate": "invalid"}, b"\0\0"),
        ({"X-Sample-Format": "f32"}, b"\0\0"),
        ({}, b"\0"),
        ({"X-Sample-Rate": "99999"}, b"\0\0"),
        ({}, b""),
    ],
)
async def test_invalid_local_audio_is_rejected(server, service, config, headers, data):
    async def handle(request):
        return web.Response(body=data, content_type="audio/pcm", headers=headers)

    config["local"].update(base_url=await server(handle), mode="plain")
    with pytest.raises(BreezeError):
        await service.synthesize("测试")
    assert not service.store.items


async def test_voice_pagination_and_design_clone(server, service, config, wav_audio):
    async def handle(request):
        if request.path == "/v1/voices":
            assert request.query["voice_type"] == "default"
            return web.json_response(
                {
                    "voices": [{"voice_id": "voc_one", "name": "旁白"}],
                    "has_more": True,
                    "next_page_token": "next",
                }
            )
        if request.path == "/v1/voice-previews/design":
            payload = await request.json()
            assert payload["preview_count"] == 1 and payload["language_code"] == "zh"
            return web.json_response(
                {
                    "previews": [
                        {
                            "generated_voice_id": "preview_design",
                            "audio_base_64": base64.b64encode(wav_audio).decode(),
                        }
                    ]
                }
            )
        if request.path == "/v1/voice-previews/clone":
            parts = {}
            async for part in await request.multipart():
                parts[part.name] = bytes(await part.read())
            assert parts["files"] == wav_audio
            assert parts["reference_language_code"] == b"zh"
            return web.json_response(
                {"generated_voice_id": "preview_clone", "requires_verification": False}
            )
        if request.path.endswith("/stream"):
            return web.Response(body=wav_audio, content_type="audio/wav")
        assert request.path.endswith("/save")
        assert await request.json() == {"voice_name": "我的音色", "language_code": "zh"}
        return web.json_response({"voice_id": "voc_saved"})

    config["backend"] = "cloud"
    config["cloud"].update(base_url=await server(handle), api_key="test-cloud-key")
    page = await service.client.voices(config["cloud"], voice_type="default")
    assert page["next_page_token"] == "next" and page["has_more"]
    reference = service.store.upload(wav_audio, "sample.wav")
    payload = {
        "name": "我的音色",
        "language_code": "zh",
        "reference_id": reference["id"],
        "description": "温柔明亮的声音",
        "text": "这是一段试听文字。",
    }
    for operation in ("design", "clone"):
        result = await service.create_preview(operation, payload, service.settings())
        assert result["preview_id"] == "preview_" + operation
        assert not result["requires_verification"]
        assert result["audio"]["kind"] == "preview"
        assert await service.save_preview(
            {**payload, "preview_id": result["preview_id"]}
        ) == {"voice_id": "voc_saved"}


async def test_job_cancellation_and_client_cleanup(server, service, config):
    started = asyncio.Event()

    async def handle(request):
        started.set()
        await asyncio.sleep(0.2)
        return web.Response(body=b"\0\0", content_type="audio/pcm")

    config["local"].update(base_url=await server(handle), mode="plain")
    job = await service.submit("synthesize", {"text": "测试"})
    await asyncio.wait_for(started.wait(), 3)
    await service.close()
    assert service.jobs[job["id"]]["status"] == "cancelled"
    assert not service.tasks and service.client.session.closed
    assert not service.store.items


async def test_total_timeout_releases_slot(service, config, monkeypatch):
    async def wait_forever(*args):
        await asyncio.sleep(60)

    monkeypatch.setattr(service.client, "synthesize", wait_forever)
    snapshot = copy.deepcopy(dict(config))
    snapshot["local"]["mode"] = "plain"
    snapshot["behavior"]["timeout"] = 0.02
    with pytest.raises(BreezeError, match="超时"):
        await service.synthesize("测试", config=snapshot)
    assert service.active == 0 and not service.slots.locked()


async def test_cancelled_filesystem_work_finishes_before_return(service):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def write_file():
        started.set()
        release.wait(2)
        finished.set()

    task = asyncio.create_task(service.io(write_file))
    await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


async def test_close_cancels_chat_synthesis(server, service, config):
    started = asyncio.Event()

    async def handle(request):
        started.set()
        await asyncio.sleep(0.2)
        return web.Response(body=b"\0\0", content_type="audio/pcm")

    config["local"].update(base_url=await server(handle), mode="plain")
    task = asyncio.create_task(service.synthesize("测试"))
    await asyncio.wait_for(started.wait(), 3)
    await service.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not service.requests and service.active == 0
