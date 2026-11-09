"""Verify real AstrBot loading, reloads, commands, and authenticated Web APIs.

Run with an AstrBot checkout on PYTHONPATH. Protocol unit tests do not require it.
"""

import asyncio
import base64
import copy
import json
import os
import shutil
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("astrbot", reason="Set PYTHONPATH to an AstrBot 4.26.0+ checkout")


@pytest.fixture
async def loaded_plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
    monkeypatch.syspath_prepend(str(tmp_path))
    from astrbot.core.config.astrbot_config import AstrBotConfig
    from astrbot.core.star import star_manager
    from astrbot.core.star.context import Context
    from astrbot.core.star.star import star_map

    source = Path(__file__).resolve().parents[1]
    target = tmp_path / "data" / "plugins" / source.name
    shutil.copytree(
        source,
        target,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
            "tests",
            "node_modules",
        ),
    )
    config_dir = tmp_path / "data" / "config"
    config_dir.mkdir(parents=True)
    config = AstrBotConfig(str(tmp_path / "host.json"), default_config={})
    context = Context(asyncio.Queue(), config, *[MagicMock() for _ in range(9)])
    context.astrbot_config_mgr.get_conf.return_value = config
    context.registered_web_apis = []
    preferences = SimpleNamespace(
        global_get=AsyncMock(
            side_effect=lambda key, default=None: copy.deepcopy(default)
        ),
        global_put=AsyncMock(),
    )
    monkeypatch.setattr(star_manager, "sp", preferences)
    monkeypatch.setattr(star_manager, "sync_command_configs", AsyncMock())
    manager = star_manager.PluginManager(context, config)
    manager.reserved_plugin_path = str(tmp_path / "no-builtins")
    success, error = await manager.load(specified_dir_name=source.name)
    assert success, error
    metadata = next(item for item in star_map.values() if item.name == source.name)
    yield manager, context, metadata
    current = context.get_registered_star(source.name)
    if current and current.star_cls:
        await current.star_cls.terminate()
    await manager._unbind_plugin(source.name, metadata.module_path)


async def test_load_reload_and_resources(loaded_plugin):
    from astrbot.core.provider.register import llm_tools
    from astrbot.dashboard.services.plugin_page_service import PluginPageService

    manager, context, metadata = loaded_plugin
    assert metadata.author == "清蒸云鸭"
    pages = await PluginPageService(manager).discover_plugin_pages(metadata)
    assert [page.name for page in pages] == ["studio"]
    tools = [tool for tool in llm_tools.func_list if tool.name == "breeze_tts"]
    assert (
        len(tools) == 1
        and tools[0].parameters["properties"]["text"]["type"] == "string"
    )
    assert tools[0].parameters["properties"]["instruction"]["type"] == "string"
    assert "instruction" not in tools[0].parameters.get("required", [])
    first = metadata.star_cls
    session = first.service.client.session
    ok, error = await manager.reload(metadata.name)
    assert ok, error
    current = context.get_registered_star(metadata.name)
    assert current.star_cls is not first
    assert session.closed
    assert len(context.registered_web_apis) == 16
    assert not current.star_cls.service.client.session.closed
    from astrbot.core.star.star_handler import star_handlers_registry

    handlers = star_handlers_registry.get_handlers_by_module_name(current.module_path)
    assert len(handlers) == 9


async def test_reload_upgrades_saved_behavior_settings(loaded_plugin):
    manager, context, metadata = loaded_plugin
    first = metadata.star_cls
    legacy = copy.deepcopy(dict(first.config))
    legacy["local"]["api_key"] = "upgrade-test-local-key"
    legacy["behavior"].update(auto_tts=True, trigger_probability=25)
    for key in (
        "private_trigger_probability",
        "group_trigger_probability",
        "min_interval_seconds",
        "quote_keep_text",
        "integer_second_audio",
    ):
        legacy["behavior"].pop(key)
    config_path = Path(first.config.config_path)
    config_path.write_text(json.dumps(legacy), encoding="utf-8-sig")

    ok, error = await manager.reload(metadata.name)
    assert ok, error
    plugin = context.get_registered_star(metadata.name).star_cls
    snapshot = await plugin.service.state()
    behavior = snapshot["config"]["behavior"]
    assert behavior["auto_tts"] and behavior["trigger_probability"] == 25
    assert behavior["quote_keep_text"] is False
    assert behavior["integer_second_audio"] is True
    behavior["integer_second_audio"] = False
    assert behavior["private_trigger_probability"] == -1
    assert behavior["group_trigger_probability"] == -1
    assert behavior["min_interval_seconds"] == 90
    behavior["group_trigger_probability"] = 10
    await plugin.service.save_settings(snapshot["config"], [])
    saved = json.loads(config_path.read_text(encoding="utf-8-sig"))
    assert saved["behavior"]["group_trigger_probability"] == 10
    assert saved["local"]["api_key"] == "upgrade-test-local-key"
    assert len(context.registered_web_apis) == 16

    ok, error = await manager.reload(metadata.name)
    assert ok, error
    current = context.get_registered_star(metadata.name).star_cls
    assert current.service.settings()["behavior"]["integer_second_audio"] is False


@pytest.fixture
async def host_http(loaded_plugin):
    import httpx
    import jwt
    from astrbot.dashboard.api.plugins import router
    from astrbot.dashboard.responses import ApiError
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    _, context, metadata = loaded_plugin
    app = FastAPI()
    app.state.jwt_secret = "isolated-test-secret-that-is-at-least-32-bytes"
    app.state.core_lifecycle = SimpleNamespace(star_context=context)

    @app.exception_handler(ApiError)
    async def api_error(_request, error):
        return JSONResponse({"error": str(error)}, status_code=error.status_code)

    app.include_router(router, prefix="/api/v1")
    token = jwt.encode(
        {"username": "test-admin"}, app.state.jwt_secret, algorithm="HS256"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        yield client, {"Authorization": "Bearer " + token}, metadata.star_cls, app


async def test_authenticated_upload_edit_play_download_delete(host_http, wav_audio):
    client, auth, plugin, _ = host_http
    root = "/api/v1/plugins/extensions/astrbot_plugin_breezetts/"
    assert (await client.get(root + "state")).status_code == 401
    assert (
        await client.post(
            root + "audio/upload",
            files={"file": ("sample.wav", wav_audio, "audio/wav")},
        )
    ).status_code == 401
    response = await client.post(
        root + "audio/upload",
        headers=auth,
        files={"file": ("../../sample.wav", wav_audio, "audio/wav")},
    )
    assert response.status_code == 200, response.text
    item = response.json()
    edited = await client.post(
        root + "audio/reference",
        headers=auth,
        json={"id": item["id"], "name": "测试音色", "ref_text": "准确的录音文字"},
    )
    assert edited.status_code == 200 and edited.json()["ref_text"] == "准确的录音文字"
    chunk = (
        await client.get(root + "audio/chunk", headers=auth, params={"id": item["id"]})
    ).json()
    assert "data" not in chunk
    assert base64.b64decode(chunk["audio_base64"]).startswith(b"RIFF") and chunk["eof"]
    downloaded = await client.get(
        root + "audio/download", headers=auth, params={"id": item["id"]}
    )
    assert downloaded.content == plugin.service.store.path(item["id"]).read_bytes()
    assert "attachment" in downloaded.headers["Content-Disposition"]
    invalid = await client.get(
        root + "audio/download", headers=auth, params={"id": "../../host.json"}
    )
    assert invalid.status_code == 400
    snapshot = (await client.get(root + "state", headers=auth)).json()["config"]
    snapshot["local"]["reference_id"] = item["id"]
    assert (
        await client.post(root + "settings", headers=auth, json={"config": snapshot})
    ).status_code == 200
    assert (
        await client.post(root + "audio/delete", headers=auth, json={"id": item["id"]})
    ).status_code == 400
    snapshot["local"]["reference_id"] = ""
    await client.post(root + "settings", headers=auth, json={"config": snapshot})
    assert (
        await client.post(root + "audio/delete", headers=auth, json={"id": item["id"]})
    ).status_code == 200
    assert not plugin.service.store.items


async def test_invalid_web_inputs_and_audio_boundaries(host_http, wav_audio, caplog):
    client, auth, plugin, _ = host_http
    root = "/api/v1/plugins/extensions/astrbot_plugin_breezetts/"
    cases = [
        ("settings", []),
        ("settings", {"config": {"local": {}, "cloud": {}}, "clear_keys": [{}]}),
        ("jobs/submit", {"operation": []}),
        ("jobs/submit", {"operation": "synthesize", "payload": "text"}),
        ("voices/save-preview", {"preview_id": ".."}),
        ("audio/reference", []),
        ("audio/reference", {"id": [], "name": "Name", "ref_text": "Text"}),
        ("audio/delete", {"id": "../private"}),
    ]
    for endpoint, payload in cases:
        response = await client.post(root + endpoint, headers=auth, json=payload)
        assert response.status_code == 400, (endpoint, response.text)
    assert (await client.post(root + "audio/upload", headers=auth)).status_code == 400
    assert (
        await client.post(
            root + "audio/upload",
            headers=auth,
            files={"file": ("bad.wav", b"private-key-test", "audio/wav")},
        )
    ).status_code == 400
    item = (
        await client.post(
            root + "audio/upload",
            headers=auth,
            files={"file": ("voice.wav", wav_audio, "audio/wav")},
        )
    ).json()
    for offset in ("bad", "-1", str(item["size"] + 1)):
        assert (
            await client.get(
                root + "audio/chunk",
                headers=auth,
                params={"id": item["id"], "offset": offset},
            )
        ).status_code == 400
    plugin.service.store.path(item["id"]).write_bytes(b"shortened")
    assert (
        await client.get(root + "audio/chunk", headers=auth, params={"id": item["id"]})
    ).status_code == 400
    assert "private-key-test" not in caplog.text


async def test_chunk_reads_protect_audio_during_history_cleanup(host_http):
    import io
    import wave

    client, auth, plugin, _ = host_http
    target = io.BytesIO()
    with wave.open(target, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\x01\x00" * 24000 * 20)
    store = plugin.service.store
    first = store.add(target.getvalue(), "wav", "output", "Playing")
    root = "/api/v1/plugins/extensions/astrbot_plugin_breezetts/"
    response = await client.get(
        root + "audio/chunk", headers=auth, params={"id": first["id"]}
    )
    assert response.status_code == 200
    assert first["id"] in plugin.service.reading_audio
    second = store.add(target.getvalue(), "wav", "output", "New")
    async with plugin.service.lock:
        await plugin.service.prune_audio(1, {second["id"]})
    assert store.path(first["id"]).exists() and store.path(second["id"]).exists()
    # Finishing one reader keeps the grace lease for another concurrent reader.
    response = await client.get(
        root + "audio/chunk", headers=auth, params={"id": first["id"], "offset": 524288}
    )
    assert response.status_code == 200 and first["id"] in plugin.service.reading_audio
    plugin.service.reading_audio.clear()
    async with plugin.service.lock:
        await plugin.service.prune_audio(1)
    assert set(store.items) == {second["id"]}


async def test_real_message_command_and_auto_reply(loaded_plugin, server, caplog):
    from aiohttp import web
    from astrbot.api.event import MessageEventResult, ResultContentType
    from astrbot.api.message_components import Plain, Record, Reply
    from astrbot.api.provider import LLMResponse
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata

    class CapturedEvent(AstrMessageEvent):
        async def send(self, message):
            self.sent = message

    async def handle(request):
        assert (await request.post())["text"] == "你好 hello world"
        return web.Response(body=b"\0\0" * 100, content_type="audio/pcm")

    _, _, metadata = loaded_plugin
    plugin = metadata.star_cls
    plugin.config["local"].update(base_url=await server(handle), mode="plain")
    plugin.config["behavior"]["min_interval_seconds"] = 0
    message = AstrBotMessage()
    message.type = MessageType.FRIEND_MESSAGE
    message.sender = MessageMember("user", "User")
    message.message = [Plain("breezetts 你好 hello world")]
    message.message_str = "breezetts 你好 hello world"
    message.message_id = "test-message"
    message.self_id = "test-bot"
    event = CapturedEvent(
        "breezetts 你好 hello world",
        message,
        PlatformMetadata("test", "test", "test"),
        "test-session",
    )
    results = [result async for result in plugin.speak(event)]
    assert len(results) == 2 and isinstance(results.pop(0).chain[0], Plain)
    assert isinstance(results[0].chain[0], Record)
    assert results[0].chain[0].text == "你好 hello world"
    results[0].chain.insert(0, Reply(id="source", sender_id="user"))
    event.set_result(results[0])
    await plugin.preserve_speech_order(event)
    assert not results[0].chain
    assert len(event.sent.chain) == 1 and isinstance(event.sent.chain[0], Record)
    await plugin.release_speech_audio(event)
    event.set_extra("breezetts_speech_turn", None)
    plugin.config["behavior"].update(auto_tts=True, keep_text=False)
    event.set_result(
        MessageEventResult(
            chain=[Plain("<breeze_tts>你好 hello world</breeze_tts>")],
            result_content_type=ResultContentType.LLM_RESULT,
        )
    )
    await plugin.clean_speech_response(
        event,
        LLMResponse(
            role="assistant", completion_text=event.get_result().get_plain_text()
        ),
    )
    await plugin.auto_speak(event)
    assert len(event.get_result().chain) == 1 and isinstance(
        event.get_result().chain[0], Record
    )
    event.send = AsyncMock(side_effect=RuntimeError("private-platform-message"))
    event.set_extra("breezetts_speech_turn", None)
    outcome = await plugin.breeze_tts(event, "你好 hello world")
    assert "delivery failed" in outcome
    count = len(plugin.service.store.items)
    event.set_result(
        MessageEventResult(
            chain=[Plain("你好 hello world")],
            result_content_type=ResultContentType.LLM_RESULT,
        )
    )
    await plugin.clean_speech_response(
        event,
        LLMResponse(
            role="assistant", completion_text=event.get_result().get_plain_text()
        ),
    )
    await plugin.auto_speak(event)
    assert len(plugin.service.store.items) == count
    assert isinstance(event.get_result().chain[0], Plain)
    assert "private-platform-message" not in caplog.text
    event.set_extra("breezetts_speech_turn", None)
    plugin.config["local"]["base_url"] = "http://127.0.0.1:1"
    event.set_result(
        MessageEventResult(
            chain=[Plain("<tts>你好 hello world</tts>")],
            result_content_type=ResultContentType.LLM_RESULT,
        )
    )
    await plugin.clean_speech_response(
        event,
        LLMResponse(
            role="assistant", completion_text=event.get_result().get_plain_text()
        ),
    )
    await plugin.auto_speak(event)
    assert isinstance(event.get_result().chain[0], Plain)


@pytest.mark.skipif(
    os.environ.get("BREEZE_BROWSER_TEST") != "1",
    reason="Set BREEZE_BROWSER_TEST=1 to run Chromium interaction tests",
)
async def test_browser_workbench(host_http, loaded_plugin, server, wav_audio, tmp_path):
    import uvicorn
    from aiohttp import web
    from astrbot.dashboard.services.plugin_page_service import PluginPageService
    from fastapi.responses import HTMLResponse, Response
    from playwright.async_api import async_playwright, expect

    _, auth, plugin, app = host_http
    manager, _, metadata = loaded_plugin
    page_service = PluginPageService(manager)
    root = page_service.get_plugin_root_dir(metadata) / "pages" / "studio"
    saved_previews = []
    created_previews = []
    manual_tts_calls = []
    voice_catalog_calls = []
    cpp_forms = []
    cpp_voices = {
        name: {
            "id": name,
            "frames": 50,
            "seconds": 4,
            "encode_ms": 12,
            "saved": saved,
            "ref_text": "参考文字。",
            "description": "服务端旁白",
            "metadata": {"accent": "zh"},
        }
        for name, saved in (("cpp_saved", True), ("cpp_cached", False))
    }

    async def upstream(request):
        if request.path.startswith("/cpp/"):
            assert request.headers.get("Authorization") == "Bearer cpp-ui-key"
            if request.path.endswith("/health"):
                return web.json_response({"status": "ok", "sample_rate": 24000})
            if request.path.endswith("/model"):
                return web.json_response(
                    {"model": "breeze-tts-2", "backend": "CUDA0", "sample_rate": 24000}
                )
            if request.path.endswith("/queue"):
                return web.json_response({"active": 1, "waiting": 2, "capacity": 1})
            if request.path.endswith("/voices"):
                if request.method == "GET":
                    return web.json_response(list(cpp_voices.values()))
                fields = await request.post()
                name = fields.get("name", "cpp_registered")
                cpp_voices[name] = {
                    "id": name,
                    "frames": 50,
                    "seconds": 4,
                    "encode_ms": 5,
                    "saved": bool(fields.get("name")),
                    "ref_text": fields["ref_text"],
                    "description": fields.get("description", ""),
                    "metadata": json.loads(fields.get("metadata", "{}")),
                }
                return web.json_response(cpp_voices[name])
            if "/voices/" in request.path:
                name = request.path.rsplit("/", 1)[1]
                if request.method == "DELETE":
                    removed = cpp_voices.pop(name)
                    return web.json_response(
                        {"deleted": name, "file_kept": removed["saved"]}
                    )
                fields = await request.json()
                cpp_voices[name]["description"] = fields["description"]
                for key, value in fields["metadata"].items():
                    if value is None:
                        cpp_voices[name]["metadata"].pop(key, None)
                    else:
                        cpp_voices[name]["metadata"][key] = value
                return web.json_response(cpp_voices[name])
            if request.content_type == "application/json":
                cpp_forms.append(await request.json())
                streamed = (
                    wav_audio[:4]
                    + b"\xff" * 4
                    + wav_audio[8:40]
                    + b"\xff" * 4
                    + wav_audio[44:]
                )
                return web.Response(body=streamed, content_type="audio/wav")
            cpp_forms.append(dict(await request.post()))
            return web.Response(body=wav_audio[44:], content_type="audio/pcm")
        if request.path.endswith("/models"):
            return web.json_response([{"model_id": "breeze-tts-2"}])
        if request.path.endswith("/voices"):
            voice_catalog_calls.append(dict(request.query))
            second = request.query.get("next_page_token") == "next"
            return web.json_response(
                {
                    "voices": [
                        {
                            "voice_id": "voc_second" if second else "voc_first",
                            "name": "第二个音色" if second else "公共旁白",
                            "origin": "designed",
                        }
                    ],
                    "has_more": not second,
                    "next_page_token": "" if second else "next",
                }
            )
        if request.path.endswith("/clone"):
            created_previews.append(len(created_previews) + 1)
            return web.json_response(
                {
                    "generated_voice_id": f"preview_ui_{len(created_previews)}",
                    "requires_verification": False,
                }
            )
        if request.path.endswith("/save"):
            saved_previews.append(await request.json())
            return web.json_response(
                {
                    "voice_id": "voc_ui_saved"
                    if "/preview_ui_1/" in request.path
                    else f"voc_ui_saved_{len(saved_previews)}"
                }
            )
        if "/text-to-speech/" in request.path:
            manual_tts_calls.append((request.path, await request.json()))
            return web.Response(body=wav_audio, content_type="audio/wav")
        if request.path.endswith("/stream"):
            return web.Response(body=wav_audio, content_type="audio/wav")
        if request.path.endswith("/health"):
            return web.json_response({"status": "ok", "sample_rate": 24000})
        return web.Response(body=wav_audio[44:] * 4, content_type="audio/pcm")

    base_url = await server(upstream)
    plugin.config["local"].update(base_url=base_url, mode="plain")
    plugin.context.get_all_providers = lambda: [
        SimpleNamespace(meta=lambda: SimpleNamespace(id="tone-ui-model"))
    ]
    ui_tts = SimpleNamespace(
        provider_config={"type": "minimax_tts_api", "id": "expressive-voice"},
        model_name="speech-2.8-hd",
        meta=lambda: SimpleNamespace(id="expressive-voice"),
    )
    ui_cosy = SimpleNamespace(
        provider_config={"type": "dashscope_tts", "id": "cosy-free"},
        model_name="cosyvoice-v3-flash",
        voice="cosyvoice-custom",
        meta=lambda: SimpleNamespace(id="cosy-free"),
    )
    ui_unsupported = SimpleNamespace(
        provider_config={"type": "dashscope_tts", "id": "cosy-unsupported"},
        model_name="cosyvoice-v3-plus",
        voice="cosyvoice-custom",
        meta=lambda: SimpleNamespace(id="cosy-unsupported"),
    )
    plugin.context.get_all_tts_providers = lambda: [ui_tts, ui_cosy, ui_unsupported]
    plugin.context.get_provider_by_id = lambda provider_id: (
        ui_tts if provider_id == "expressive-voice" else None
    )

    @app.get("/")
    async def parent():
        html = (
            Path(__file__).with_name("browser_parent.html").read_text(encoding="utf-8")
        )
        return HTMLResponse(
            html.replace("AUTH_PLACEHOLDER", json.dumps(auth["Authorization"]))
        )

    @app.get("/view/{asset_path:path}")
    async def asset(asset_path: str):
        path = (root / asset_path).resolve()
        if path.parent != root:
            return Response(status_code=404)
        if path.suffix == ".html":
            return HTMLResponse(
                page_service.apply_theme_to_html(
                    path.read_text(encoding="utf-8"), "light"
                ).replace(
                    "</body>",
                    '<script src="/api/plugin/page/bridge-sdk.js"></script></body>',
                )
            )
        return Response(
            path.read_bytes(),
            headers={"Access-Control-Allow-Origin": "*"},
            media_type=page_service.guess_plugin_page_mime_type(path),
        )

    @app.get("/api/plugin/page/bridge-sdk.js")
    async def sdk():
        return Response(
            page_service.bridge_file.read_bytes(), media_type="application/javascript"
        )

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    web_server = uvicorn.Server(
        uvicorn.Config(
            app, log_level="error", access_log=False, timeout_graceful_shutdown=2
        )
    )
    serving = asyncio.create_task(web_server.serve(sockets=[listener]))
    try:
        for _ in range(300):
            if web_server.started:
                break
            await asyncio.sleep(0.01)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page(
                viewport={"width": 1440, "height": 1040}, accept_downloads=True
            )
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            async def legacy_state(route):
                response = await route.fetch()
                payload = await response.json()
                payload["config"]["behavior"].pop("min_interval_seconds")
                await route.fulfill(response=response, json=payload)

            await page.route("**/state", legacy_state)
            await page.goto(f"http://127.0.0.1:{listener.getsockname()[1]}")
            frame = page.frame_locator("iframe")
            await expect(frame.locator("#notice")).to_contain_text(
                "在插件管理中重载 BreezeTTS"
            )
            await expect(frame.locator("#save")).to_be_disabled()
            await expect(frame.locator("#synthesize")).to_be_disabled()
            await expect(frame.locator("#test-connection")).to_be_disabled()
            assert plugin.config["behavior"]["min_interval_seconds"] == 90
            await page.unroute("**/state", legacy_state)
            await page.reload()
            await expect(frame.locator("#save-state")).to_have_text("配置已保存")
            assert await frame.locator(".brand-icon").evaluate(
                "image => image.naturalWidth > 0"
            )
            skip = frame.get_by_role("link", name="跳到主要内容")
            await skip.focus()
            await skip.press("Enter")
            await expect(frame.locator("#main-content")).to_be_focused()

            await frame.get_by_role("button", name="音色与录音").click()
            await frame.locator("#upload").set_input_files(
                {"name": "试听录音.wav", "mimeType": "audio/wav", "buffer": wav_audio}
            )
            await expect(frame.locator("#reference-dialog")).to_be_visible()
            await frame.locator("#reference-text").fill(
                "你好，这是参考录音的完整文字稿。"
            )
            await frame.get_by_role("button", name="保存录音信息").click()
            await expect(frame.locator("#cloud-voices-panel")).to_be_hidden()
            await frame.get_by_role("button", name="设为当前音色").click()
            await expect(frame.get_by_role("button", name="当前音色 ✓")).to_be_visible()
            # A remembered reference becomes active only in local/reference mode.
            for mode in ("plain", "design"):
                await frame.get_by_role("button", name="连接与设置").click()
                await frame.locator("#local-mode").select_option(mode)
                if mode == "design":
                    await frame.locator('[data-config="local.instruction"]').fill(
                        "清晰的女声"
                    )
                await frame.locator("#save").click()
                await expect(frame.locator("#save-state")).to_have_text("配置已保存")
                await frame.get_by_role("button", name="音色与录音").click()
                await expect(
                    frame.get_by_role("button", name="当前音色 ✓")
                ).to_have_count(0)
                await expect(
                    frame.get_by_role("button", name="设为当前音色")
                ).to_be_enabled()
                await frame.get_by_role("button", name="设为当前音色").click()
                await expect(
                    frame.get_by_role("button", name="当前音色 ✓")
                ).to_be_visible()
                assert plugin.config["backend"] == "local"
                assert plugin.config["local"]["mode"] == "reference"
            await frame.get_by_role("button", name="连接与设置").click()
            await frame.locator('[data-config="local.instruction"]').fill("")
            await frame.locator("#save").click()
            await expect(frame.locator("#save-state")).to_have_text("配置已保存")
            await frame.get_by_role("button", name="合成试听", exact=True).click()
            await frame.locator("#synthesis-text").fill("🎧")
            await expect(frame.locator("#text-count")).to_have_text("1 / 1000")
            await frame.locator("#synthesis-text").fill(
                "你好，愿每一段文字，都能找到属于自己的声音。"
            )
            await frame.locator("#synthesize").click()
            await expect(frame.locator(".audio-row")).to_have_count(1)
            await expect(frame.locator("#download-current")).to_be_enabled()
            await expect(frame.locator("#player")).to_be_visible()
            await expect(frame.locator("#player-state")).to_have_text("16.0 秒")
            assert (
                await page.frames[1]
                .locator("#player")
                .evaluate("audio => audio.duration")
                == 16
            )
            await page.reload()
            await expect(frame.locator("#player")).to_be_visible()
            await expect(frame.locator("#player-state")).to_have_text("16.0 秒")
            assert (
                await page.frames[1]
                .locator("#player")
                .evaluate("audio => audio.paused")
            )

            async def fail_audio(route):
                await route.fulfill(
                    status=503,
                    json={
                        "status": "error",
                        "message": "Audio temporarily unavailable",
                    },
                )

            await page.route("**/audio/chunk**", fail_audio)
            await (
                frame.locator(".audio-row")
                .first.get_by_role("button", name="试听", exact=True)
                .click()
            )
            await expect(frame.locator("#player-state")).to_have_text("载入失败")
            await expect(frame.locator("#download-current")).to_be_enabled()
            await page.unroute("**/audio/chunk**", fail_audio)
            await frame.locator("#retry-playback").click()
            await expect(frame.locator("#player-state")).to_have_text("16.0 秒")
            await expect(frame.locator("#player")).to_have_js_property("paused", False)
            await expect(frame.locator("#player")).to_have_js_property("error", None)
            assert await frame.locator("#player").evaluate(
                "audio => audio.readyState >= 2"
            )
            async with page.expect_download() as download_info:
                await frame.locator("#download-current").click()
            downloaded = await download_info.value
            assert Path(await downloaded.path()).read_bytes().startswith(b"RIFF")
            screenshot_dir = Path(
                os.environ.get("BREEZE_SCREENSHOT_DIR", tmp_path / "screenshots")
            )
            screenshot_dir.mkdir(parents=True, exist_ok=True)

            async def capture_view(filename, selector=None):
                height = await page.frames[1].evaluate(
                    "document.documentElement.scrollHeight"
                )
                await page.evaluate(
                    "height => document.querySelector('iframe').style.height = height + 'px'",
                    height,
                )
                try:
                    if selector:
                        await frame.locator(selector).screenshot(
                            path=str(screenshot_dir / filename)
                        )
                    else:
                        await page.screenshot(
                            path=str(screenshot_dir / filename), full_page=True
                        )
                finally:
                    await page.evaluate(
                        "document.querySelector('iframe').style.height = '100vh'"
                    )

            await page.screenshot(
                path=str(screenshot_dir / "studio-desktop.png"), full_page=True
            )
            await frame.get_by_role("button", name="连接与设置").click()
            await expect(frame.locator("#random-tone-adaptive")).to_be_enabled()
            await expect(frame.locator("#tone-provider")).to_be_enabled()
            await expect(frame.locator("#random-tone-hint")).to_contain_text("随机语音")
            await expect(frame.locator("#random-tone-hint")).to_contain_text("动态语调")
            await frame.locator("#random-tone-adaptive").check()
            await expect(frame.locator("#enable-tone-dialog")).to_be_visible()
            await frame.locator("#enable-tone-dialog").screenshot(
                path=str(screenshot_dir / "adaptive-tone-dialog.png")
            )
            await frame.locator("#cancel-enable-tone").click()
            await expect(frame.locator("#random-tone-adaptive")).not_to_be_checked()
            await expect(frame.locator("#dynamic-tone")).not_to_be_checked()
            await expect(
                frame.locator('[data-config="behavior.auto_tts"]')
            ).not_to_be_checked()
            await frame.locator("#random-tone-adaptive").check()
            await page.keyboard.press("Escape")
            await expect(frame.locator("#enable-tone-dialog")).not_to_be_visible()
            await expect(frame.locator("#random-tone-adaptive")).not_to_be_checked()
            await frame.locator("#random-tone-adaptive").check()
            await frame.locator("#confirm-enable-tone").click()
            await expect(frame.locator("#dynamic-tone")).to_be_checked()
            await expect(
                frame.locator('[data-config="behavior.auto_tts"]')
            ).to_be_checked()
            await frame.locator("#tone-provider").select_option("tone-ui-model")
            await frame.locator("#save").click()
            await expect(frame.locator("#save-state")).to_have_text("配置已保存")
            assert plugin.config["behavior"]["random_tone_adaptive"] is True
            assert plugin.config["behavior"]["tone_provider_id"] == "tone-ui-model"
            assert plugin.config["behavior"]["dynamic_tone"] is True
            assert plugin.config["behavior"]["auto_tts"] is True
            await page.reload()
            await frame.get_by_role("button", name="连接与设置").click()
            await expect(frame.locator("#random-tone-adaptive")).to_be_checked()
            await expect(frame.locator("#tone-provider")).to_have_value("tone-ui-model")
            await expect(frame.locator("#dynamic-tone")).to_be_checked()
            await expect(frame.locator("#random-tone-hint")).to_contain_text("已启用")
            await frame.locator("#dynamic-tone").uncheck()
            await expect(frame.locator("#random-tone-hint")).not_to_contain_text(
                "随机语音"
            )
            await expect(frame.locator("#random-tone-hint")).to_contain_text("动态语调")
            await frame.locator("#random-tone-adaptive").uncheck()
            await frame.locator("#random-tone-adaptive").check()
            await frame.locator("#cancel-enable-tone").click()
            await expect(frame.locator("#random-tone-adaptive")).not_to_be_checked()
            await expect(frame.locator("#dynamic-tone")).not_to_be_checked()
            await expect(
                frame.locator('[data-config="behavior.auto_tts"]')
            ).to_be_checked()
            await frame.locator('[data-config="behavior.auto_tts"]').uncheck()
            await expect(frame.locator("#tone-provider")).to_be_enabled()
            await frame.locator("#save").click()
            await expect(frame.locator("#save-state")).to_have_text("配置已保存")
            await frame.locator('[data-edit-prompt="speech_prompt"]').click()
            await expect(frame.locator("#speech-prompt-dialog")).to_be_visible()
            await expect(frame.locator("#speech-prompt-description")).to_contain_text(
                "可全部省略"
            )
            await expect(frame.locator("#prompt-variable-list")).to_contain_text(
                "原生标签表"
            )
            await expect(
                frame.locator('[data-insert-variable="output_protocol"]')
            ).to_have_count(0)
            await frame.locator("#prompt-editor").fill("开头|结尾")
            await frame.locator("#prompt-editor").evaluate(
                "editor => editor.setSelectionRange(2, 3)"
            )
            await frame.locator('[data-insert-variable="audio_tags"]').click()
            await expect(frame.locator("#prompt-editor")).to_have_value(
                "开头{audio_tags}结尾"
            )
            await frame.locator("#restore-prompt").click()
            await expect(frame.locator("#prompt-editor")).to_have_value(
                plugin.service.settings()["behavior"]["speech_prompt"]
            )
            await page.screenshot(
                path=str(screenshot_dir / "prompt-editor-desktop.png")
            )
            await page.set_viewport_size({"width": 390, "height": 844})
            await expect(
                frame.locator('[data-insert-variable="audio_tags"]')
            ).to_be_visible()
            assert await frame.locator("#speech-prompt-dialog").evaluate(
                "dialog => dialog.scrollWidth <= dialog.clientWidth"
            )
            await page.screenshot(path=str(screenshot_dir / "prompt-editor-mobile.png"))
            await page.set_viewport_size({"width": 1440, "height": 1040})
            await frame.locator("#prompt-editor").fill("{output_protocol}")
            await frame.locator("#apply-prompt").click()
            await expect(frame.locator("#prompt-editor-error")).to_contain_text(
                "不支持的变量"
            )
            await expect(frame.locator("#speech-prompt-dialog")).to_be_visible()
            await frame.locator("#prompt-editor").fill("取消这次编辑")
            await frame.locator("#cancel-prompt").click()
            await expect(frame.locator("#save-state")).to_have_text("配置已保存")
            await frame.locator('[data-edit-prompt="speech_prompt"]').click()
            await expect(frame.locator("#prompt-editor")).not_to_have_value(
                "取消这次编辑"
            )
            await frame.locator("#prompt-editor").fill("Escape 不应应用")
            await page.keyboard.press("Escape")
            await expect(frame.locator("#speech-prompt-dialog")).not_to_be_visible()
            await expect(frame.locator("#save-state")).to_have_text("配置已保存")
            speech_template = "推荐按语境表达惊喜，不必添加标签。\n{platform_rules}\n{audio_tags}\n{control_syntax}"
            tone_template = "保留所有台词，允许自然朗读。\n{platform_rules}\n{audio_tags}\n{output_protocol}"
            for name, template in (
                ("speech_prompt", speech_template),
                ("tone_prompt", tone_template),
            ):
                await frame.locator(f'[data-edit-prompt="{name}"]').click()
                await frame.locator("#prompt-editor").fill(template)
                if name == "tone_prompt":
                    await expect(
                        frame.locator("#adaptive-output-hint")
                    ).to_contain_text("需要返回 JSON")
                    await expect(
                        frame.locator('[data-insert-variable="output_protocol"]')
                    ).to_be_visible()
                    await expect(
                        frame.locator('[data-insert-variable="passage"]')
                    ).to_be_visible()
                await frame.locator("#apply-prompt").click()
            await frame.locator("#save").click()
            await expect(frame.locator("#save-state")).to_have_text("配置已保存")
            await page.reload()
            await frame.get_by_role("button", name="连接与设置").click()
            for name, template in (
                ("speech_prompt", speech_template),
                ("tone_prompt", tone_template),
            ):
                await frame.locator(f'[data-edit-prompt="{name}"]').click()
                await expect(frame.locator("#prompt-editor")).to_have_value(template)
                await frame.locator("#restore-prompt").click()
                await frame.locator("#apply-prompt").click()
            await frame.locator("#save").click()
            await expect(frame.locator("#save-state")).to_have_text("配置已保存")
            await frame.locator("#edit-expression").click()
            await expect(frame.locator("#expression-dialog")).to_be_visible()
            await expect(frame.locator("#expression-description")).to_contain_text(
                "并非穷举白名单"
            )
            await expect(frame.locator("#custom-styles-field")).to_be_hidden()
            await page.screenshot(
                path=str(screenshot_dir / "expression-dialog-desktop.png")
            )
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await frame.locator("#expression-dialog").evaluate(
                "dialog => dialog.scrollWidth <= dialog.clientWidth"
            )
            await page.screenshot(
                path=str(screenshot_dir / "expression-dialog-mobile.png")
            )
            await page.set_viewport_size({"width": 1440, "height": 1040})
            await frame.locator("#custom-tags").fill("[语气:新描述]")
            await frame.locator("#apply-expression").click()
            await expect(frame.locator("#expression-error")).to_contain_text(
                "无需填入标签表"
            )
            await expect(frame.locator("#expression-dialog")).to_be_visible()
            await frame.locator("#custom-tags").fill("[取消的标签]")
            await frame.locator("#cancel-expression").click()
            await expect(frame.locator("#save-state")).to_have_text("配置已保存")
            await frame.locator("#edit-expression").click()
            await expect(frame.locator("#custom-tags")).to_have_value("")
            await frame.locator("#custom-tags").fill("[Esc 丢弃]")
            await page.keyboard.press("Escape")
            await expect(frame.locator("#expression-dialog")).not_to_be_visible()
            await expect(frame.locator("#save-state")).to_have_text("配置已保存")
            await frame.locator("#edit-expression").click()
            await frame.locator("#custom-tags").fill("[咳嗽]")
            await frame.locator("#apply-expression").click()
            await frame.locator("#save").click()
            await expect(frame.locator("#save-state")).to_have_text("配置已保存")
            await page.reload()
            await frame.get_by_role("button", name="连接与设置").click()
            await frame.locator("#edit-expression").click()
            await expect(frame.locator("#custom-tags")).to_have_value("[咳嗽]")
            await frame.locator("#reset-expression").click()
            assert plugin.config["expression"]["breeze_local_tags"] == "[咳嗽]"
            await frame.locator("#apply-expression").click()
            await frame.locator("#backend").select_option("provider")
            await frame.locator("#tts-provider").select_option("cosy-free")
            await expect(frame.locator("#expression-policy")).to_contain_text(
                "100 加权字符"
            )
            await frame.locator("#tts-preset").select_option("cosyvoice")
            await expect(
                frame.locator('#tts-preset option[value="cosyvoice"]')
            ).to_be_enabled()
            await expect(
                frame.locator('#tts-preset option[value="cosyvoice_fixed"]')
            ).to_be_disabled()
            await frame.locator("#tts-provider").select_option("cosy-unsupported")
            await expect(frame.locator("#tts-preset")).to_have_value("")
            await expect(frame.locator("#notice")).to_contain_text("恢复自动识别")
            await expect(frame.locator("#expression-controls")).to_be_hidden()
            await expect(
                frame.locator('#tts-preset option[value="cosyvoice"]')
            ).to_be_disabled()
            await frame.locator("#tts-provider").select_option("expressive-voice")
            await expect(frame.locator("#local-settings")).to_be_hidden()
            await expect(frame.locator("#expression-policy")).to_contain_text(
                "严格固定值"
            )
            await frame.locator("#edit-expression").click()
            await expect(frame.locator("#custom-styles-field")).to_be_visible()
            await frame.locator("#custom-tags").fill("(laughs)\n(custom event)")
            await frame.locator("#custom-styles").fill("happy\nsad")
            await page.screenshot(
                path=str(screenshot_dir / "expression-dialog-fixed.png")
            )
            await frame.locator("#apply-expression").click()
            await frame.locator("#save").click()
            await expect(frame.locator("#save-state")).to_have_text("配置已保存")
            assert plugin.config["expression"]["minimax_styles"] == "happy\nsad"
            await page.reload()
            await frame.get_by_role("button", name="连接与设置").click()
            await frame.locator("#edit-expression").click()
            await expect(frame.locator("#custom-tags")).to_have_value(
                "(laughs)\n(custom event)"
            )
            await expect(frame.locator("#custom-styles")).to_have_value("happy\nsad")
            await frame.locator("#reset-expression").click()
            await expect(frame.locator("#custom-styles")).to_have_value("")
            await frame.locator("#apply-expression").click()
            await frame.locator("#test-connection").click()
            await expect(frame.locator("#connection-result")).to_contain_text(
                "尚未合成"
            )
            await frame.locator("#backend").select_option("local")
            await frame.locator("#save").click()
            await expect(frame.locator("#save-state")).to_have_text("配置已保存")
            await expect(frame.locator("#random-seed")).to_be_checked()
            await expect(frame.locator("#fixed-seed-field")).to_be_hidden()
            await frame.locator("#random-seed").uncheck()
            await expect(frame.locator("#seed")).to_be_visible()
            await frame.locator("#seed").fill("123")
            await frame.locator("#save").click()
            await expect(frame.locator("#save-state")).to_have_text("配置已保存")
            assert plugin.config["local"]["random_seed"] is False
            assert plugin.config["local"]["seed"] == 123
            await frame.locator("#random-seed").check()
            await expect(frame.locator("#fixed-seed-field")).to_be_hidden()
            await frame.locator("#save").click()
            await expect(frame.locator("#save-state")).to_have_text("配置已保存")
            assert plugin.config["local"]["random_seed"] is True
            await frame.locator("#backend").select_option("cloud")
            await frame.locator("#cloud-url").fill(base_url)
            await frame.locator("#cloud-key").fill("browser-test-key")
            manual_voice = "a2b76355-59c5-4ec4-98cf-6c523ce937fb"
            await frame.locator("#cloud-voice").fill(
                "\ufeff \u200b" + manual_voice + " "
            )
            await frame.locator("#save").click()
            await expect(frame.locator("#save-state")).to_have_text("配置已保存")
            await expect(frame.locator("#cloud-voice")).to_have_value(manual_voice)
            await page.reload()
            await expect(frame.locator("#cloud-voice")).to_have_value(manual_voice)
            await frame.get_by_role("button", name="合成试听", exact=True).click()
            await frame.locator("#synthesis-text").fill("手动音色测试。")
            await frame.locator("#synthesize").click()
            await expect(frame.locator("#player-state")).to_have_text(
                "4.0 秒", timeout=15000
            )
            assert plugin.config["cloud"]["voice_id"] == manual_voice
            assert manual_tts_calls[0][0] == "/v1/text-to-speech/" + manual_voice
            assert not voice_catalog_calls
            await frame.get_by_role("button", name="连接与设置").click()
            for output_format in ("flac", "wav_24000"):
                await frame.locator("#cloud-format").select_option(output_format)
                await frame.locator("#cloud-delivery").select_option("stream")
                await expect(frame.locator("#cloud-format")).to_have_value("wav")
                await expect(
                    frame.locator("#cloud-format option:not([hidden])")
                ).to_have_count(3)
                await expect(frame.locator("#notice")).to_contain_text(
                    "已将输出格式改为 wav"
                )
                for allowed in ("pcm", "mp3", "wav"):
                    await frame.locator("#cloud-format").select_option(allowed)
                await frame.locator("#cloud-delivery").select_option("sync")
                await expect(
                    frame.locator("#cloud-format option:not([hidden])")
                ).to_have_count(14)
            await frame.locator("#cloud-delivery").select_option("stream")
            await capture_view("settings-cloud-formats.png")
            await frame.locator("#cloud-delivery").select_option("sync")
            await frame.locator("#test-connection").click()
            await expect(frame.locator("#connection-result")).to_have_text("连接成功")
            await expect(frame.locator("#cloud-key")).to_have_value("")
            await frame.locator('[data-config="behavior.auto_tts"]').check()
            await expect(
                frame.locator('[data-config="behavior.auto_tts"]')
            ).to_be_checked()
            await expect(frame.locator("#trigger-probability")).to_be_enabled()
            await frame.locator("#trigger-probability").fill("35")
            await frame.locator("#private-probability-mode").select_option("custom")
            await expect(frame.locator("#private-probability-field")).to_be_visible()
            await frame.locator("#private-trigger-probability").fill("30")
            await frame.locator("#group-probability-mode").select_option("custom")
            await frame.locator("#group-trigger-probability").fill("10")
            await frame.locator("#speech-min-interval").fill("")
            await frame.locator("#save").click()
            await expect(frame.locator("#notice")).to_contain_text("取值范围")
            assert plugin.config["behavior"]["min_interval_seconds"] == 90
            await frame.locator("#speech-min-interval").fill("120")
            await frame.locator("#history-max-mb").fill("128")
            await expect(
                frame.locator('[data-config="behavior.quote_keep_text"]')
            ).not_to_be_checked()
            await frame.locator('[data-config="behavior.quote_keep_text"]').check()
            await expect(frame.locator("#integer-second-audio")).to_be_checked()
            await expect(frame.locator("#integer-second-hint")).to_contain_text(
                "SnowLuma"
            )
            await frame.locator("#integer-second-audio").uncheck()
            await frame.locator('[data-config="behavior.auto_tts"]').uncheck()
            await expect(frame.locator("#trigger-probability")).to_be_disabled()
            await frame.locator("#clear-cloud-key").click()
            await expect(frame.locator("#cloud-key-state")).to_have_text(
                "密钥已清空 · 保存后生效"
            )
            await frame.locator("#save").click()
            await expect(frame.locator("#save-state")).to_have_text("配置已保存")
            assert plugin.config["cloud"]["api_key"] == ""
            assert plugin.config["behavior"]["trigger_probability"] == 35
            assert plugin.config["behavior"]["private_trigger_probability"] == 30
            assert plugin.config["behavior"]["group_trigger_probability"] == 10
            assert plugin.config["behavior"]["min_interval_seconds"] == 120
            await expect(frame.locator("#group-probability-mode")).to_have_value(
                "custom"
            )
            assert plugin.config["behavior"]["history_max_mb"] == 128
            assert plugin.config["behavior"]["quote_keep_text"] is True
            assert plugin.config["behavior"]["integer_second_audio"] is False
            await page.reload()
            await frame.get_by_role("button", name="连接与设置").click()
            await expect(frame.locator("#integer-second-audio")).not_to_be_checked()
            await frame.locator("#integer-second-audio").check()
            await frame.locator("#save").click()
            await expect(frame.locator("#save-state")).to_have_text("配置已保存")
            assert plugin.config["behavior"]["integer_second_audio"] is True
            viewport = page.viewport_size
            for width, height, suffix in [
                (1440, 1040, "desktop"),
                (390, 844, "mobile"),
            ]:
                await page.set_viewport_size({"width": width, "height": height})
                await frame.locator(
                    "#integer-second-audio"
                ).scroll_into_view_if_needed()
                await page.screenshot(
                    path=str(screenshot_dir / f"integer-second-audio-{suffix}.png")
                )
            await page.set_viewport_size(viewport)
            await expect(frame.locator("#cache-usage")).to_contain_text("MiB")
            await frame.locator("#cloud-key").fill("browser-test-key")
            await frame.locator('[data-config="behavior.auto_tts"]').check()
            await frame.locator("#group-probability-mode").select_option("inherit")
            await expect(frame.locator("#group-probability-field")).to_be_hidden()
            await frame.locator('[data-config="behavior.auto_tts"]').uncheck()
            await frame.locator("#save").click()
            await expect(frame.locator("#save-state")).to_have_text("配置已保存")
            assert plugin.config["behavior"]["group_trigger_probability"] == -1

            await expect(frame.locator("#cloud-key")).to_have_attribute(
                "placeholder", "已配置密钥；留空保留，输入新值替换"
            )

            # Explicit barriers cover edits during direct/indirect saves, both outcomes.
            for index, (trigger, success) in enumerate(
                [
                    ("save", True),
                    ("save", False),
                    ("test-connection", True),
                    ("test-connection", False),
                ]
            ):
                save_started = asyncio.Event()
                release_save = asyncio.Event()

                async def delayed_settings(route):
                    save_started.set()
                    await release_save.wait()
                    if success:
                        await route.continue_()
                    else:
                        await route.fulfill(
                            status=500,
                            json={
                                "status": "error",
                                "message": "Temporary save failure",
                            },
                        )

                await page.route("**/settings", delayed_settings)
                await frame.locator("#max-characters").fill(str(1100 + index))
                await frame.locator(f"#{trigger}").click()
                await asyncio.wait_for(save_started.wait(), 5)
                try:
                    await frame.locator("#timeout").fill(str(701 + index))
                    await expect(frame.locator("#save")).to_be_disabled()
                finally:
                    release_save.set()
                await expect(frame.locator("#save")).to_be_enabled()
                await expect(frame.locator("#timeout")).to_have_value(str(701 + index))
                await expect(frame.locator(f"#{trigger}")).not_to_have_attribute(
                    "aria-busy", "true"
                )
                await page.unroute("**/settings", delayed_settings)
                await frame.locator("#save").click()
                await expect(frame.locator("#save-state")).to_have_text("配置已保存")
                assert plugin.config["behavior"]["timeout"] == 701 + index
            await frame.get_by_role("button", name="音色与录音").click()
            await expect(frame.locator("#local-voices-panel")).to_be_hidden()
            await expect(frame.locator("#cloud-voices-panel")).to_be_visible()
            await frame.locator("#voice-type").select_option("default")
            await frame.locator("#sync-voices").click()
            await expect(frame.locator(".voice-card")).to_have_count(1)
            await frame.locator("#more-voices").click()
            await expect(frame.locator(".voice-card")).to_have_count(2)
            await frame.locator("#preview-name").fill("工作台音色")
            await frame.locator("#clone-reference").select_option(index=1)
            await frame.locator("#create-preview").click()
            await expect(frame.locator("#save-preview")).to_be_visible(timeout=15000)
            assert not saved_previews

            async def fail_settings(route):
                await route.fulfill(
                    status=500,
                    json={"status": "error", "message": "Temporary save failure"},
                )

            await page.route("**/settings", fail_settings)
            await frame.locator("#save-preview").click()
            await expect(frame.locator("#notice")).to_contain_text(
                "Temporary save failure"
            )
            await expect(frame.locator("#save-preview")).to_be_enabled()
            await expect(frame.locator("#save-preview")).to_have_text("设为当前音色")
            await page.unroute("**/settings", fail_settings)
            await frame.locator("#save-preview").click()
            await expect(frame.locator("#preview-result")).to_be_hidden()
            assert saved_previews == [
                {"voice_name": "工作台音色", "language_code": "zh"}
            ]
            # Save A finishes after B is displayed: only A may receive A's voice ID.
            await frame.locator("#preview-name").fill("预览 A")
            await frame.locator("#create-preview").click()
            await expect(frame.locator("#preview-message")).to_contain_text(
                "预览 A", timeout=15000
            )
            save_started = asyncio.Event()
            release_save = asyncio.Event()
            pending_identity = []

            async def delayed_preview_save(route):
                pending_identity.append(route.request.post_data_json)
                save_started.set()
                await release_save.wait()
                await route.continue_()

            baseline_voice = plugin.config["cloud"]["voice_id"]
            await page.route("**/voices/save-preview", delayed_preview_save)
            await frame.locator("#save-preview").click()
            await asyncio.wait_for(save_started.wait(), 5)
            try:
                await frame.locator("#preview-name").fill("预览 B")
                await frame.locator("#create-preview").click()
                await expect(frame.locator("#preview-message")).to_contain_text(
                    "预览 B", timeout=15000
                )
            finally:
                release_save.set()
            await expect(frame.locator("#save-preview")).to_be_enabled()
            await expect(frame.locator("#preview-result")).to_be_visible()
            await expect(frame.locator("#preview-message")).to_contain_text("预览 B")
            assert pending_identity[0]["preview_id"] == "preview_ui_2"
            assert plugin.config["cloud"]["voice_id"] == baseline_voice
            await page.unroute("**/voices/save-preview", delayed_preview_save)
            await frame.locator("#save-preview").click()
            await expect(frame.locator("#preview-result")).to_be_hidden()
            assert saved_previews[-1]["voice_name"] == "预览 B"
            assert len(saved_previews) == 3
            assert plugin.config["cloud"]["voice_id"] == "voc_ui_saved_3"
            actual_frame = page.frames[1]
            for width, height in [
                (1440, 1040),
                (1024, 900),
                (768, 1024),
                (320, 780),
                (390, 844),
            ]:
                await page.set_viewport_size({"width": width, "height": height})
                for nav in ("studio", "voices", "settings"):
                    await frame.locator(f'[data-page="{nav}"]').click()
                    overflow = await actual_frame.evaluate(
                        "Array.from(document.querySelectorAll('body *')).filter(el => el.getBoundingClientRect().right > innerWidth + 1 && el.getBoundingClientRect().width > 0).map(el => ({tag:el.tagName,id:el.id,cls:el.className})).slice(0,8)"
                    )
                    assert await actual_frame.evaluate(
                        "document.documentElement.scrollWidth <= window.innerWidth"
                    ), f"Overflow on {nav} at {width}px: {overflow}"
                await frame.locator('[data-page="studio"]').click()
            await capture_view("studio-mobile.png")
            await frame.locator('[data-page="settings"]').click()
            await capture_view("settings-mobile.png")
            await frame.locator('[data-page="voices"]').click()
            await capture_view("voices-official-mobile.png")
            await frame.locator('[data-page="settings"]').click()
            await frame.locator("#backend").select_option("local")
            await expect(frame.locator("#random-seed")).to_be_checked()
            await frame.locator("#random-seed").uncheck()
            await expect(frame.locator("#seed")).to_be_visible()
            assert await actual_frame.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth"
            )
            await capture_view("settings-local-seed-mobile.png")
            await page.set_viewport_size({"width": 1440, "height": 1040})
            await capture_view("settings-local-seed-desktop.png")
            await page.set_viewport_size({"width": 390, "height": 844})
            await frame.locator("#random-seed").check()
            await frame.locator("#save").click()
            await frame.locator('[data-page="voices"]').click()
            await capture_view("voices-local-mobile.png")
            await page.set_viewport_size({"width": 1440, "height": 1040})
            await capture_view("voices-local-desktop.png")
            await frame.locator('[data-page="studio"]').click()
            await page.set_viewport_size({"width": 1440, "height": 1040})
            reference = await plugin.service.io(
                plugin.service.store.upload, wav_audio, "cpp-reference.wav"
            )
            plugin.service.store.items[reference["id"]]["ref_text"] = "参考文字。"
            await plugin.service.io(plugin.service.store.persist)
            await page.reload()
            await frame.locator('[data-page="settings"]').click()
            await frame.locator("#backend").select_option("local")
            await frame.locator("#local-implementation").select_option("cpp")
            await frame.locator("#local-url").fill(base_url + "/cpp/v1")
            await frame.locator("#local-key").fill("cpp-ui-key")
            await frame.locator("#local-reference").select_option(reference["id"])
            await frame.locator("#local-temperature").fill("0.65")
            await frame.locator("#local-format").select_option("mp3")
            await frame.locator("#save").click()
            await expect(frame.locator("#save-state")).to_have_text("配置已保存")
            await frame.locator("#refresh-local-status").click()
            await expect(frame.locator("#local-server-status")).to_contain_text("CUDA0")
            await expect(frame.locator("#local-server-status")).to_contain_text(
                "等待 2 条"
            )
            await capture_view("settings-cpp-desktop.png")
            await frame.locator('[data-page="voices"]').click()
            await frame.locator("#refresh-server-voices").click()
            saved_card = frame.locator('[data-server-voice-id="cpp_saved"]')
            await saved_card.get_by_role(
                "button", name="使用此音色", exact=True
            ).click()
            await expect(
                saved_card.get_by_role("button", name="当前音色 ✓")
            ).to_be_disabled()
            await saved_card.get_by_role("button", name="编辑描述与元数据").click()
            await frame.locator("#server-voice-description").fill(
                "<img src=x onerror=alert(1)>"
            )
            await frame.locator("#server-voice-metadata").fill('{"source":"test"}')
            await frame.locator("#save-server-voice").click()
            await expect(frame.locator("#server-voice-dialog")).not_to_be_visible()
            assert cpp_voices["cpp_saved"]["metadata"] == {"source": "test"}
            await expect(saved_card.locator("p")).to_have_text(
                "<img src=x onerror=alert(1)>"
            )
            await (
                frame.locator('[data-server-voice-id="cpp_cached"]')
                .get_by_role("button", name="使用此音色", exact=True)
                .click()
            )
            await saved_card.get_by_role("button", name="移出内存").click()
            await expect(frame.locator("#server-delete-message")).to_contain_text(
                "文件会保留"
            )
            await frame.locator("#confirm-server-delete").click()
            await expect(saved_card).to_have_count(0)
            await frame.locator("#register-server-voice").click()
            await frame.locator("#server-register-reference").select_option(
                reference["id"]
            )
            await frame.locator("#server-register-name").fill("new_cpp_voice")
            await frame.locator("#server-voice-description").fill("新注册的测试音色")
            await frame.locator("#server-voice-dialog").screenshot(
                path=str(screenshot_dir / "server-voice-dialog-desktop.png")
            )
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await frame.locator("#server-voice-dialog").evaluate(
                "dialog => dialog.scrollWidth <= dialog.clientWidth"
            )
            await frame.locator("#server-voice-dialog").screenshot(
                path=str(screenshot_dir / "server-voice-dialog-mobile.png")
            )
            await page.set_viewport_size({"width": 1440, "height": 1040})
            await frame.locator("#save-server-voice").click()
            await expect(frame.locator("#server-voice-dialog")).not_to_be_visible()
            await expect(
                frame.locator('[data-server-voice-id="new_cpp_voice"]')
            ).to_have_count(1)
            await capture_view("voices-cpp-desktop.png")
            await page.set_viewport_size({"width": 390, "height": 844})
            await capture_view("voices-cpp-mobile.png")
            await page.set_viewport_size({"width": 1440, "height": 1040})
            await frame.locator('[data-page="studio"]').click()
            await frame.locator("#synthesis-text").fill("C++ 工作台合成测试。")
            await frame.locator("#synthesize").click()
            await expect(frame.locator("#history .audio-row").first).to_contain_text(
                "C++ 工作台合成测试。"
            )
            await expect(frame.locator("#player-state")).to_have_text("4.0 秒")
            assert (
                cpp_forms[-1]["voice"] == "cpp_cached"
                and cpp_forms[-1]["temperature"] == 0.65
            )
            await frame.locator("#conversion-source").select_option(reference["id"])
            await frame.locator("#conversion-voice").fill("new_cpp_voice")
            await frame.locator("#convert-audio").click()
            await expect(frame.locator("#conversion-status")).to_have_text(
                "转换完成，音频已保存。"
            )
            assert cpp_forms[-1]["voice_id"] == "new_cpp_voice"
            await capture_view("conversion-cpp-desktop.png", "#conversion-panel")
            await page.set_viewport_size({"width": 390, "height": 844})
            await capture_view("conversion-cpp-mobile.png", "#conversion-panel")
            await page.set_viewport_size({"width": 1440, "height": 1040})
            await page.evaluate(
                "document.querySelector('iframe').contentWindow.postMessage({channel:'astrbot-plugin-page',kind:'context',context:{isDark:true}}, '*')"
            )
            await expect(frame.locator("html")).to_have_attribute("data-theme", "dark")
            await page.wait_for_timeout(250)
            await page.screenshot(
                path=str(screenshot_dir / "studio-dark.png"), full_page=True
            )
            assert not errors, errors
            await browser.close()
    finally:
        web_server.should_exit = True
        await asyncio.wait_for(serving, 5)
        listener.close()


async def test_reload_legacy_stream_format_keeps_workbench_accessible(loaded_plugin):
    manager, context, metadata = loaded_plugin
    plugin = metadata.star_cls
    legacy = copy.deepcopy(dict(plugin.config))
    legacy["cloud"].update(delivery="stream", output_format="flac")
    path = Path(plugin.config.config_path)
    path.write_text(json.dumps(legacy), encoding="utf-8-sig")
    ok, error = await manager.reload(metadata.name)
    assert ok, error
    current = context.get_registered_star(metadata.name).star_cls
    assert (await current.service.state())["config"]["cloud"]["output_format"] == "wav"
    assert (
        json.loads(path.read_text(encoding="utf-8-sig"))["cloud"]["output_format"]
        == "flac"
    )


async def test_manual_voice_id_survives_actual_plugin_reload(
    loaded_plugin, server, wav_audio
):
    from aiohttp import web

    manager, context, metadata = loaded_plugin
    plugin = metadata.star_cls
    voice_id = "a2b76355-59c5-4ec4-98cf-6c523ce937fb"
    calls = []

    async def upstream(request):
        calls.append(request.path)
        assert request.path == "/v1/text-to-speech/" + voice_id
        return web.Response(body=wav_audio, content_type="audio/wav")

    draft = (await plugin.service.state())["config"]
    draft["backend"] = "cloud"
    draft["cloud"].update(
        base_url=await server(upstream),
        api_key="manual-test-key",
        voice_id=" \ufeff" + voice_id + "\u200b ",
    )
    await plugin.service.save_settings(draft, [])
    ok, error = await manager.reload(metadata.name)
    assert ok, error
    current = context.get_registered_star(metadata.name).star_cls
    assert current.service.settings()["cloud"]["voice_id"] == voice_id
    await current.service.synthesize("测试。")
    assert calls == ["/v1/text-to-speech/" + voice_id]
