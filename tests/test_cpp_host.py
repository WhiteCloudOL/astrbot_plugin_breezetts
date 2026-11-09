"""Verify the C++ workbench features through AstrBot's authenticated API bridge."""

# ruff: noqa: F811

import asyncio

import pytest
from aiohttp import web

pytest.importorskip("astrbot")

from test_host import host_http, loaded_plugin  # noqa: E402,F401


async def test_cpp_voice_and_conversion_jobs_through_authenticated_bridge(
    host_http, server, wav_audio
):
    client, auth, plugin, _ = host_http
    root = "/api/v1/plugins/extensions/astrbot_plugin_breezetts/"
    voice = {
        "id": "bridge_voice",
        "frames": 50,
        "seconds": 4,
        "encode_ms": 12,
        "saved": False,
        "ref_text": "参考文字。",
        "description": "",
        "metadata": {},
    }

    async def upstream(request):
        assert request.headers["Authorization"] == "Bearer cpp-bridge-key"
        if request.path.endswith("/health"):
            return web.json_response({"status": "ok", "sample_rate": 24000})
        if request.path.endswith("/model"):
            return web.json_response(
                {"model": "breeze-tts-2", "backend": "CPU", "sample_rate": 24000}
            )
        if request.path.endswith("/queue"):
            return web.json_response({"active": 0, "waiting": 0, "capacity": 1})
        if "/voices" in request.path:
            if request.method == "POST":
                fields = await request.post()
                assert fields["ref_audio"].file.read() == wav_audio
            if request.method == "PATCH":
                voice.update(await request.json())
            if request.method == "DELETE":
                return web.json_response({"deleted": voice["id"], "file_kept": False})
            return web.json_response(
                [voice]
                if request.method == "GET" and request.path.endswith("/voices")
                else voice
            )
        assert request.path.endswith("/audio/convert")
        assert (await request.post())["voice_id"] == "bridge_voice"
        return web.Response(body=wav_audio[44:], content_type="audio/pcm")

    plugin.config["local"].update(
        implementation="cpp",
        mode="plain",
        base_url=await server(upstream),
        api_key="cpp-bridge-key",
    )
    assert (await client.get(root + "local/status")).status_code == 401
    status = await client.get(root + "local/status", headers=auth)
    assert status.status_code == 200 and status.json()["model"]["backend"] == "CPU"
    upload = await client.post(
        root + "audio/upload",
        headers=auth,
        files={"file": ("reference.wav", wav_audio, "audio/wav")},
    )
    identifier = upload.json()["id"]
    assert (
        await client.post(
            root + "audio/reference",
            headers=auth,
            json={"id": identifier, "name": "参考", "ref_text": "参考文字。"},
        )
    ).status_code == 200
    for operation, payload in [
        ("local_voice", {"reference_id": identifier}),
        ("convert", {"source_id": identifier, "voice_id": "bridge_voice"}),
    ]:
        submitted = await client.post(
            root + "jobs/submit",
            headers=auth,
            json={"operation": operation, "payload": payload},
        )
        assert submitted.status_code == 200
        job_id = submitted.json()["id"]
        for _ in range(200):
            result = (
                await client.get(
                    root + "jobs/status", headers=auth, params={"id": job_id}
                )
            ).json()
            if result["status"] != "running":
                break
            await asyncio.sleep(0.01)
        assert result["status"] == "ready", result
    assert result["result"]["conversion"] is True
    catalog = await client.get(root + "local/voices", headers=auth)
    assert catalog.status_code == 200 and catalog.json()[0]["id"] == "bridge_voice"
    one = await client.get(
        root + "local/voices", headers=auth, params={"id": "bridge_voice"}
    )
    assert one.json()["id"] == "bridge_voice"
    edited = await client.post(
        root + "local/voice",
        headers=auth,
        json={"operation": "edit", "id": "bridge_voice", "description": "新的描述"},
    )
    assert edited.status_code == 200 and voice["description"] == "新的描述"
    removed = await client.post(
        root + "local/voice",
        headers=auth,
        json={"operation": "remove", "id": "bridge_voice"},
    )
    assert removed.status_code == 200 and removed.json()["file_kept"] is False
    assert (
        await client.post(root + "local/voice", headers=auth, json={"operation": []})
    ).status_code == 400


async def test_cpp_cancel_job_and_python_capability_guard(host_http, server):
    client, auth, plugin, _ = host_http
    root = "/api/v1/plugins/extensions/astrbot_plugin_breezetts/"
    assert (await client.get(root + "local/status", headers=auth)).status_code == 400
    entered, release = asyncio.Event(), asyncio.Event()

    async def upstream(_request):
        entered.set()
        await release.wait()
        return web.Response(body=b"\x01\x00", content_type="audio/pcm")

    plugin.config["local"].update(
        implementation="cpp", mode="plain", base_url=await server(upstream)
    )
    try:
        submitted = await client.post(
            root + "jobs/submit",
            headers=auth,
            json={"operation": "synthesize", "payload": {"text": "等待取消。"}},
        )
        identifier = submitted.json()["id"]
        await asyncio.wait_for(entered.wait(), 3)
        result = await client.post(
            root + "jobs/cancel", headers=auth, json={"id": identifier}
        )
        assert result.status_code == 200 and result.json()["status"] == "cancelled"
        assert identifier not in plugin.service.tasks
        assert not plugin.service.requests and not plugin.service.local_slot.locked()
    finally:
        release.set()
