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

pytest.importorskip("astrbot", reason="Set PYTHONPATH to an AstrBot 4.29.3+ checkout")


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
    first = metadata.star_cls
    session = first.service.client.session
    ok, error = await manager.reload(metadata.name)
    assert ok, error
    current = context.get_registered_star(metadata.name)
    assert current.star_cls is not first
    assert session.closed
    assert len(context.registered_web_apis) == 12
    assert not current.star_cls.service.client.session.closed
    from astrbot.core.star.star_handler import star_handlers_registry

    handlers = star_handlers_registry.get_handlers_by_module_name(current.module_path)
    assert len(handlers) == 3


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
    assert base64.b64decode(chunk["data"]).startswith(b"RIFF") and chunk["eof"]
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


async def test_real_message_command_and_auto_reply(loaded_plugin, server):
    from aiohttp import web
    from astrbot.api.event import MessageEventResult, ResultContentType
    from astrbot.api.message_components import Plain, Record
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
    assert len(results) == 1 and isinstance(results[0].chain[0], Record)
    assert results[0].chain[0].text == "你好 hello world"
    plugin.config["behavior"].update(auto_tts=True, keep_text=False)
    event.set_result(
        MessageEventResult(
            chain=[Plain("你好 hello world")],
            result_content_type=ResultContentType.LLM_RESULT,
        )
    )
    await plugin.auto_speak(event)
    assert len(event.get_result().chain) == 1 and isinstance(
        event.get_result().chain[0], Record
    )
    plugin.config["local"]["base_url"] = "http://127.0.0.1:1"
    event.set_result(
        MessageEventResult(
            chain=[Plain("你好 hello world")],
            result_content_type=ResultContentType.LLM_RESULT,
        )
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
    root = page_service.get_plugin_root_dir(metadata) / "views" / "studio"
    saved_previews = []

    async def upstream(request):
        if request.path.endswith("/models"):
            return web.json_response([{"model_id": "breeze-tts-2"}])
        if request.path.endswith("/voices"):
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
            return web.json_response(
                {"generated_voice_id": "preview_ui", "requires_verification": False}
            )
        if request.path.endswith("/save"):
            saved_previews.append(await request.json())
            return web.json_response({"voice_id": "voc_ui_saved"})
        if request.path.endswith("/stream"):
            return web.Response(body=wav_audio, content_type="audio/wav")
        if request.path.endswith("/health"):
            return web.json_response({"status": "ok", "sample_rate": 24000})
        return web.Response(body=wav_audio[44:], content_type="audio/pcm")

    base_url = await server(upstream)
    plugin.config["local"].update(base_url=base_url, mode="plain")

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
                page_service.process_plugin_page_html(
                    path.read_text(encoding="utf-8"), theme="light"
                )
            )
        return Response(
            path.read_bytes(),
            headers={"Access-Control-Allow-Origin": "*"},
            media_type="text/css"
            if path.suffix == ".css"
            else "application/javascript",
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
            await page.goto(f"http://127.0.0.1:{listener.getsockname()[1]}")
            frame = page.frame_locator("iframe")
            await expect(frame.locator("#save-state")).to_have_text("配置已保存")
            await frame.get_by_role("button", name="音色与录音").click()
            await frame.locator("#upload").set_input_files(
                {"name": "试听录音.wav", "mimeType": "audio/wav", "buffer": wav_audio}
            )
            await expect(frame.locator("#reference-dialog")).to_be_visible()
            await frame.locator("#reference-text").fill(
                "你好，这是参考录音的完整文字稿。"
            )
            await frame.get_by_role("button", name="保存录音信息").click()
            await frame.get_by_role("button", name="设为本地音色").click()
            await expect(frame.get_by_role("button", name="默认音色 ✓")).to_be_visible()
            await frame.get_by_role("button", name="合成试听", exact=True).click()
            await frame.locator("#synthesis-text").fill(
                "你好，愿每一段文字，都能找到属于自己的声音。"
            )
            await frame.locator("#synthesize").click()
            await expect(frame.locator(".audio-row")).to_have_count(1)
            await expect(frame.locator("#download-current")).to_be_enabled()
            async with page.expect_download() as download_info:
                await frame.locator("#download-current").click()
            downloaded = await download_info.value
            assert Path(await downloaded.path()).read_bytes().startswith(b"RIFF")
            screenshot_dir = Path(
                os.environ.get("BREEZE_SCREENSHOT_DIR", tmp_path / "screenshots")
            )
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            await page.screenshot(
                path=str(screenshot_dir / "studio-desktop.png"), full_page=True
            )
            await frame.get_by_role("button", name="连接与设置").click()
            await frame.locator("#backend").select_option("cloud")
            await frame.locator("#cloud-url").fill(base_url)
            await frame.locator("#cloud-key").fill("browser-test-key")
            await frame.locator("#test-connection").click()
            await expect(frame.locator("#connection-result")).to_have_text("连接成功")
            await expect(frame.locator("#cloud-key")).to_have_value("")
            await expect(frame.locator("#cloud-key")).to_have_attribute(
                "placeholder", "已配置密钥；留空保留，输入新值替换"
            )
            await frame.get_by_role("button", name="音色与录音").click()
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
            await frame.locator("#save-preview").click()
            await expect(frame.locator("#preview-result")).to_be_hidden()
            assert saved_previews == [
                {"voice_name": "工作台音色", "language_code": "zh"}
            ]
            actual_frame = page.frames[1]
            for width, height in [(1440, 1040), (390, 844)]:
                await page.set_viewport_size({"width": width, "height": height})
                for nav in ("studio", "voices", "settings"):
                    await frame.locator(f'[data-page="{nav}"]').click()
                    assert await actual_frame.evaluate(
                        "document.documentElement.scrollWidth <= window.innerWidth"
                    ), f"Overflow on {nav} at {width}px"
                await frame.locator('[data-page="studio"]').click()
            await page.screenshot(
                path=str(screenshot_dir / "studio-mobile.png"), full_page=True
            )
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
