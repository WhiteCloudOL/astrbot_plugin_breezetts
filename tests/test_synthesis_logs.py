"""Verify synthesis diagnostics retain dialogue and redact credentials."""

import asyncio
import logging
import re

import pytest
from aiohttp import web
from breezetts_test.client import SYNTHESIS_ID
from breezetts_test.settings import BreezeError


@pytest.mark.parametrize("backend", ["local", "cloud"])
async def test_synthesis_logs_original_payload_and_correlated_result(
    service, config, server, wav_audio, caplog, backend
):
    caplog.set_level(logging.INFO)
    config["behavior"]["dynamic_tone"] = True
    config["backend"] = backend
    config["local"].update(mode="plain", random_seed=False, seed=42)
    config["cloud"].update(api_key="must-not-log-this-key", voice_id="opaque")

    async def upstream(request):
        if backend == "local":
            fields = await request.post()
            assert fields["text"] == "尾音不要丢。" and fields["instruction"] == "轻快"
            assert fields["seed"] == "42"
            return web.Response(body=b"\xff\x1f" * 2400, content_type="audio/pcm")
        payload = await request.json()
        assert payload["text"] == "尾音不要丢。" and payload["instructions"] == "轻快"
        return web.Response(body=wav_audio, content_type="audio/wav")

    config[backend]["base_url"] = await server(upstream)
    result = await service.synthesize("[语气:轻快]尾音不要丢。")
    messages = [r.getMessage() for r in caplog.records]
    start = next(m for m in messages if m.startswith("Breeze synthesis started"))
    synthesis_id = re.search(r"synthesis_id=([0-9a-f]+)", start)[1]
    assert "[语气:轻快]尾音不要丢。" in start
    payload = next(m for m in messages if m.startswith("Breeze synthesis payload"))
    assert "尾音不要丢。" in payload and "轻快" in payload
    assert f"request_id={synthesis_id}." in payload
    assert (
        "Breeze HTTP request" in caplog.text and "Breeze HTTP response" in caplog.text
    )
    assert result["id"] in caplog.text and "Breeze synthesis completed" in caplog.text
    assert (
        "must-not-log-this-key" not in caplog.text
        and "Authorization" not in caplog.text
    )
    assert SYNTHESIS_ID.get() == ""


async def test_failed_synthesis_logs_safe_correlated_error(
    service, config, server, caplog
):
    caplog.set_level(logging.INFO)
    config["local"].update(mode="plain", api_key="secret-request-key")

    async def upstream(request):
        return web.Response(status=403, text="secret-upstream-body")

    config["local"]["base_url"] = await server(upstream)
    with pytest.raises(BreezeError):
        await service.synthesize("请记录失败的原文。")
    assert "请记录失败的原文。" in caplog.text
    assert "Breeze HTTP request failed" in caplog.text
    assert (
        "Breeze synthesis failed" in caplog.text and "upstream_http_403" in caplog.text
    )
    assert (
        "secret-request-key" not in caplog.text
        and "secret-upstream-body" not in caplog.text
    )
    assert SYNTHESIS_ID.get() == ""


async def test_concurrent_synthesis_keeps_request_ids_separate(
    service, config, server, caplog
):
    caplog.set_level(logging.INFO)
    config["local"]["mode"] = "plain"

    async def upstream(request):
        return web.Response(body=b"\xff\x1f" * 2400, content_type="audio/pcm")

    config["local"]["base_url"] = await server(upstream)
    await asyncio.gather(service.synthesize("第一条。"), service.synthesize("第二条。"))
    starts = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("Breeze synthesis started")
    ]
    parents = {re.search(r"synthesis_id=([0-9a-f]+)", m)[1] for m in starts}
    requests = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("Breeze synthesis payload")
    ]
    assert len(parents) == 2 and len(requests) == 2
    assert all(
        sum(f"request_id={parent}." in m for m in requests) == 1 for parent in parents
    )
    assert SYNTHESIS_ID.get() == ""
