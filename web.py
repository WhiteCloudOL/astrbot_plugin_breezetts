"""Expose workbench operations through AstrBot's authenticated view bridge."""

from __future__ import annotations

import asyncio
import base64
import time
from functools import wraps

from astrbot.api.web import error_response, file_response, json_response, request

from .service import BreezeService
from .settings import BreezeError
from .storage import MIME, UPLOAD_LIMIT


def web_errors(handler):
    """Translate errors at every Web API boundary without echoing secrets.

    Args:
        handler: Async workbench API handler.

    Returns:
        Handler with safe, consistent JSON errors.
    """

    @wraps(handler)
    async def wrapped(self):
        try:
            await self.service.start()
            return await handler(self)
        except BreezeError as exc:
            log = (
                self.service.logger.debug
                if exc.code == "validation"
                else self.service.logger.warning
            )
            log(
                "Breeze Web API rejected request (handler=%s, code=%s).",
                handler.__name__,
                exc.code,
            )
            return error_response(str(exc), status_code=400)
        except (TimeoutError, asyncio.TimeoutError):
            return error_response("请求超时，请稍后重试。", status_code=504)
        except Exception as exc:
            self.service.logger.error("Breeze Web API failed (%s).", type(exc).__name__)
            return error_response(
                "操作失败，请检查插件数据目录和服务状态。", status_code=500
            )

    return wrapped


class WorkbenchAPI:
    """Adapt authenticated Dashboard requests to the plugin application service."""

    def __init__(self, service: BreezeService):
        self.service = service

    @web_errors
    async def state(self):
        """Return redacted configuration and the audio library."""
        return json_response(await self.service.state())

    @web_errors
    async def settings(self):
        """Persist a complete settings draft and explicit credential removals."""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            raise BreezeError("配置请求须为 JSON 对象。")
        return json_response(
            await self.service.save_settings(
                payload.get("config"), payload.get("clear_keys", [])
            )
        )

    @web_errors
    async def connection(self):
        """Check the selected service using its saved settings."""
        return json_response(await self.service.connection())

    @web_errors
    async def voices(self):
        """Read one official voice-catalog page with validated filters."""
        config = self.service.settings()
        voice_type = request.query.get("voice_type", "all")
        search, token = request.query.get("search", ""), request.query.get("token", "")
        page = request.query.get("page", 1, type=int)
        if (
            voice_type not in {"all", "personal", "default"}
            or len(search) > 200
            or len(token) > 4000
            or not 1 <= page <= 10000
        ):
            raise BreezeError("音色目录筛选参数无效。")
        result = await asyncio.wait_for(
            self.service.client.voices(
                config["cloud"],
                voice_type=voice_type,
                search=search,
                token=token,
                page=page,
            ),
            30,
        )
        return json_response(result)

    @web_errors
    async def submit(self):
        """Start a synthesis, clone, or design job without blocking the bridge."""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            raise BreezeError("任务请求须为 JSON 对象。")
        return json_response(
            await self.service.submit(
                payload.get("operation"), payload.get("payload", {})
            )
        )

    @web_errors
    async def job(self):
        """Read a bounded, plugin-owned job result."""
        job = self.service.jobs.get(request.query.get("id", ""))
        if job is None:
            raise BreezeError("任务已过期或插件已重载，请刷新音频列表查看结果。")
        return json_response(dict(job))

    @web_errors
    async def save_preview(self):
        """Save an auditioned official voice preview to the account."""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            raise BreezeError("保存音色请求须为 JSON 对象。")
        return json_response(await self.service.save_preview(payload))

    @web_errors
    async def upload(self):
        """Read a bounded recording and normalize it for both Breeze services."""
        await self.service.start()
        upload = (await request.files()).get("file")
        if upload is None:
            raise BreezeError("请选择要上传的参考录音。")
        data = bytearray()
        while chunk := await upload.read(65536):
            data.extend(chunk)
            if len(data) > UPLOAD_LIMIT:
                raise BreezeError("参考录音超过 20 MiB 上限。")
        async with self.service.lock:
            assert self.service.store is not None
            item = await self.service.io(
                self.service.store.upload,
                bytes(data),
                upload.filename or "reference.wav",
            )
        return json_response(item)

    @web_errors
    async def reference(self):
        """Update a reference display name and exact transcript."""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            raise BreezeError("参考录音请求须为 JSON 对象。")
        name, text = payload.get("name"), payload.get("ref_text")
        if (
            not isinstance(name, str)
            or not 1 <= len(name.strip()) <= 120
            or not isinstance(text, str)
            or len(text) > 4000
        ):
            raise BreezeError("名称须为 1–120 字符，录音文字稿不超过 4000 字符。")
        await self.service.start()
        async with self.service.lock:
            store = self.service.store
            assert store is not None
            store.path(payload.get("id"))
            item = store.items[payload["id"]]
            if item["kind"] != "reference":
                raise BreezeError("此音频不是参考录音。")
            previous = dict(item)
            item.update(name=name.strip(), ref_text=text.strip())
            try:
                await self.service.io(store.persist)
            except OSError:
                store.items[payload["id"]] = previous
                raise
        return json_response(dict(item))

    @web_errors
    async def delete_audio(self):
        """Delete an audio record while protecting the selected local voice."""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            raise BreezeError("删除请求须为 JSON 对象。")
        await self.service.start()
        async with self.service.lock:
            if (
                isinstance(payload.get("id"), str)
                and self.service.delivery_audio.get(payload["id"], 0) > time.monotonic()
            ):
                raise BreezeError("该音频正在发送，请稍后再删除。")
            if payload.get("id") == self.service.settings()["local"]["reference_id"]:
                raise BreezeError("该录音正在作为默认音色使用，请先更换默认音色。")
            assert self.service.store is not None
            await self.service.io(self.service.store.remove, payload.get("id"))
        return json_response({"deleted": True})

    @web_errors
    async def download(self):
        """Return an indexed audio file through the authenticated bridge."""
        await self.service.start()
        async with self.service.lock:
            store = self.service.store
            assert store is not None
            audio_id = request.query.get("id", "")
            path = store.path(audio_id)
            if not path.is_file():
                raise BreezeError("音频文件不存在，请刷新列表。")
            extension = store.items[audio_id]["extension"]
            if store.items[audio_id]["kind"] != "reference":
                self.service.reading_audio[audio_id] = time.monotonic() + 60
            return file_response(
                path,
                filename=f"breezetts-{audio_id[:8]}.{extension}",
                content_type=MIME[extension],
            )

    @web_errors
    async def audio_chunk(self):
        """Send bounded base64 chunks for playback inside a restricted iframe."""
        await self.service.start()
        try:
            offset = int(request.query.get("offset", "0"))
        except (TypeError, ValueError):
            raise BreezeError("音频读取位置须为整数。") from None
        if not 0 <= offset <= 64 * 1024 * 1024:
            raise BreezeError("音频读取位置无效。")
        async with self.service.lock:
            store = self.service.store
            assert store is not None
            audio_id = request.query.get("id", "")
            path = store.path(audio_id)

            def read_chunk():
                with path.open("rb") as source:
                    source.seek(offset)
                    return source.read(512 * 1024)

            if not path.is_file():
                raise BreezeError("音频文件不存在，请刷新列表。")
            item = store.items[audio_id]
            if offset > item["size"]:
                raise BreezeError("音频读取位置超过文件大小。")
            if path.stat().st_size != item["size"]:
                raise BreezeError("音频文件与索引大小不一致，请重新上传或生成。")
            data = await self.service.io(read_chunk)
            if item["kind"] != "reference":
                self.service.reading_audio[audio_id] = time.monotonic() + 60
            return json_response(
                {
                    "audio_base64": base64.b64encode(data).decode("ascii"),
                    "next": offset + len(data),
                    "eof": offset + len(data) >= item["size"],
                    "mime": MIME[item["extension"]],
                }
            )
