"""Implement the distinct BreezeBlue and self-hosted HTTP protocols."""

from __future__ import annotations

import asyncio
import base64
import json
import re
from urllib.parse import quote

import aiohttp

from .settings import BreezeError, api_root, validate_identifier
from .storage import AUDIO_LIMIT, playable_audio


class BreezeClient:
    """Own a reusable HTTP session and never forward keys across redirects."""

    def __init__(self):
        self.session: aiohttp.ClientSession | None = None
        self.closed = False

    async def start(self) -> None:
        """Acquire the plugin-owned HTTP session in the running event loop."""
        if self.closed:
            raise BreezeError("插件连接已关闭，请重载插件后重试。", code="closed")
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=1800, connect=15),
                trust_env=False,
            )

    async def close(self) -> None:
        """Close the session; repeated or partial shutdown is safe."""
        self.closed = True
        if self.session is not None:
            await self.session.close()

    async def request(
        self,
        config: dict,
        method: str,
        path: str,
        *,
        raw: bool = False,
        max_json_bytes: int = 2 * 1024 * 1024,
        **kwargs,
    ):
        """Perform one bounded request with secret-safe error translation.

        Args:
            config: Validated connection settings with an optional API key.
            method: HTTP method.
            path: Plugin-controlled path relative to the version root.
            raw: Return audio bytes and response headers instead of JSON.
            max_json_bytes: Size limit for JSON, including embedded preview audio.
            **kwargs: JSON, multipart data, and query parameters.

        Returns:
            Decoded JSON, or a tuple of bytes, headers, and HTTP status.

        Raises:
            BreezeError: On transport errors, non-success statuses, or invalid data.
        """
        await self.start()
        assert self.session is not None
        headers = (
            {"Authorization": "Bearer " + config["api_key"]}
            if config["api_key"]
            else {}
        )
        try:
            async with self.session.request(
                method,
                api_root(config["base_url"]) + "/" + path,
                headers=headers,
                allow_redirects=False,
                **kwargs,
            ) as response:
                if not 200 <= response.status < 300:
                    messages = {
                        401: "鉴权失败，请检查 API Key；本地无鉴权服务可留空。",
                        402: "官方账户余额或 API Key 额度不足，请在官方控制台检查。",
                        403: "当前账号无权访问此音色或操作。",
                        404: "接口或音色不存在，请检查服务类型、服务地址和音色 ID。",
                        409: "服务繁忙或操作冲突，请稍后重试。",
                        422: "服务拒绝了请求参数，请检查模型、语言、参考录音和文字稿。",
                        429: "服务队列或并发额度已满，请稍后重试。",
                    }
                    message = messages.get(
                        response.status, "服务未成功处理请求，请检查参数或服务端日志。"
                    )
                    raise BreezeError(
                        f"HTTP {response.status}：{message}",
                        code=f"upstream_http_{response.status}",
                    )
                limit = AUDIO_LIMIT if raw else max_json_bytes
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    data.extend(chunk)
                    if len(data) > limit:
                        raise BreezeError("服务响应超过大小上限。")
                if raw:
                    return bytes(data), response.headers, response.status
                try:
                    return json.loads(data)
                except (ValueError, UnicodeDecodeError):
                    raise BreezeError("服务未返回有效 JSON，请检查服务地址。") from None
        except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError):
            # Never include upstream exception strings: they can contain URLs or keys.
            raise BreezeError(
                "连接失败、超时或音频流中断，请检查服务是否启动及网络连接。",
                code="transport",
            ) from None

    async def synthesize(
        self, config: dict, text: str, reference: tuple[bytes, str] | None = None
    ) -> tuple[bytes, str]:
        """Synthesize with the selected protocol, including asynchronous cloud jobs.

        Args:
            config: Complete validated plugin settings.
            text: Bounded nonempty input text.
            reference: Local recording bytes and its exact transcript.

        Returns:
            Playable audio bytes and a validated file extension.

        Raises:
            BreezeError: If required voice data or upstream responses are invalid.
        """
        if config["backend"] == "local":
            connection = config["local"]
            if not connection["instruction"].strip() and connection["cfg_scale"] != 1:
                raise BreezeError(
                    "普通朗读与纯音色复刻请将 CFG 设为 1；使用更高 CFG 时请填写声音或语气描述。"
                )
            form = aiohttp.FormData()
            for key, value in {
                "text": text,
                "instruction": connection["instruction"],
                "cfg_scale": connection["cfg_scale"],
                "seed": connection["seed"],
            }.items():
                form.add_field(key, str(value))
            if connection["mode"] == "reference":
                if reference is None or not reference[1].strip():
                    raise BreezeError("请上传参考录音、填写准确文字稿并设为默认音色。")
                form.add_field(
                    "ref_audio",
                    reference[0],
                    filename="reference.wav",
                    content_type="audio/wav",
                )
                form.add_field("ref_text", reference[1])
            elif (
                connection["mode"] == "design" and not connection["instruction"].strip()
            ):
                raise BreezeError("声音设计模式需要填写声音描述。")
            raw, headers, _ = await self.request(
                connection, "POST", "audio/speech", raw=True, data=form
            )
            if headers.get("Content-Type", "").split(";")[0] not in {
                "audio/pcm",
                "application/octet-stream",
                "binary/octet-stream",
            }:
                raise BreezeError("本地服务应返回 PCM，请检查是否误选了官方接口。")
            rate = headers.get("X-Sample-Rate", "24000")
            if headers.get("X-Sample-Format", "s16le") != "s16le" or not re.fullmatch(
                r"[0-9]{4,5}", rate
            ):
                raise BreezeError("本地服务返回了无效的 PCM 格式或采样率。")
            return playable_audio(raw, "pcm", int(rate))
        connection = config["cloud"]
        if not connection["api_key"] or not connection["voice_id"]:
            raise BreezeError("请填写官方 API Key，并在音色库选择音色或填写音色 ID。")
        body = {
            "text": text,
            "voice_settings": {
                "guidance_scale": connection["guidance_scale"],
                "speed": connection["speed"],
                "volume": connection["volume"],
                "use_speaker_boost": connection["speaker_boost"],
            },
        }
        for key in ("model_id", "language_code", "instructions"):
            if connection[key]:
                body[key] = connection[key]
        path = "text-to-speech/" + quote(connection["voice_id"], safe="")
        params = {"output_format": connection["output_format"]}
        if connection["delivery"] == "stream":
            path += "/stream"
        else:
            params["delivery"] = connection["delivery"]
        raw, headers, status = await self.request(
            connection, "POST", path, raw=True, params=params, json=body
        )
        if status == 202:
            try:
                job_id = json.loads(raw)["generation_job_id"]
                if (
                    not isinstance(job_id, str)
                    or not 1 <= len(job_id) <= 200
                    or job_id in {".", ".."}
                ):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                raise BreezeError("官方服务未返回有效的后台任务 ID。") from None
            validate_identifier(job_id, "后台任务 ID", 200)
            path = "generation-jobs/" + quote(job_id, safe="")
            while True:
                job = await self.request(connection, "GET", path)
                if not isinstance(job, dict) or job.get("generation_job_id") != job_id:
                    raise BreezeError("官方后台任务响应无效。")
                state = job.get("status")
                if state == "ready":
                    raw, headers, _ = await self.request(
                        connection,
                        "GET",
                        path + "/audio",
                        raw=True,
                        params={"output_format": connection["output_format"]},
                    )
                    break
                if not isinstance(state, str) or state not in {
                    "queued",
                    "pending",
                    "running",
                    "processing",
                }:
                    raise BreezeError(
                        "官方后台任务未成功完成，请在官方控制台查看详情。"
                    )
                await asyncio.sleep(0.8)
        if not headers.get("Content-Type", "").startswith(
            ("audio/", "application/octet-stream")
        ):
            raise BreezeError("官方服务未返回音频。")
        return playable_audio(raw, connection["output_format"])

    async def voices(
        self,
        connection: dict,
        *,
        voice_type: str = "all",
        search: str = "",
        token: str = "",
        page: int = 1,
    ) -> dict:
        """Read one validated page of the official voice catalog.

        Args:
            connection: Official connection settings.
            voice_type: all, personal, or default (the public catalog).
            search: Optional server-side search term.
            token: Opaque next-page token from a previous response.
            page: Page number used when the server supplies no token.

        Returns:
            Voice records and pagination state.

        Raises:
            BreezeError: If the page shape is invalid.
        """
        params = {"voice_type": voice_type, "page_size": 50, "search": search}
        params["next_page_token" if token else "page"] = token if token else page
        data = await self.request(connection, "GET", "voices", params=params)
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("voices"), list)
            or len(data["voices"]) > 50
            or type(data.get("has_more")) is not bool
        ):
            raise BreezeError("官方音色目录响应格式无效。")
        voices = []
        for item in data["voices"]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("voice_id"), str)
                or not isinstance(item.get("name"), str)
            ):
                raise BreezeError("官方音色缺少有效名称或 ID。")
            validate_identifier(item["voice_id"], "音色 ID")
            if not 1 <= len(item["name"]) <= 120:
                raise BreezeError("官方音色名称无效。")
            voices.append(
                {
                    "voice_id": item["voice_id"],
                    "name": item["name"],
                    "origin": item.get("origin", ""),
                    "language_code": item.get("language_code", ""),
                }
            )
        next_token = data.get("next_page_token") or ""
        if (
            not isinstance(next_token, str)
            or len(next_token) > 4000
            or (token and next_token == token and data["has_more"])
        ):
            raise BreezeError("官方音色分页标记无效。")
        return {
            "voices": voices,
            "has_more": data["has_more"],
            "next_page_token": next_token,
            "page": page,
        }

    async def preview(
        self, connection: dict, payload: dict, recording: bytes | None = None
    ) -> tuple[str, bool, tuple[bytes, str] | None]:
        """Create an official clone or design preview without saving it silently.

        Args:
            connection: Official API connection settings.
            payload: Validated clone/design request fields.
            recording: Uploaded normalized reference WAV for clone requests.

        Returns:
            Preview ID, verification requirement, and optional playable audio.

        Raises:
            BreezeError: If a preview or its audio cannot be decoded.
        """
        if recording is not None:
            form = aiohttp.FormData()
            form.add_field("name", payload["name"])
            form.add_field(
                "files", recording, filename="reference.wav", content_type="audio/wav"
            )
            form.add_field(
                "remove_background_noise",
                str(payload.get("remove_noise", False)).lower(),
            )
            if payload["language_code"] == "zh":
                form.add_field("reference_language_code", "zh")
            result = await self.request(
                connection, "POST", "voice-previews/clone", data=form
            )
            if not isinstance(result, dict):
                raise BreezeError("官方克隆预览响应无效。")
            preview_id = result.get("generated_voice_id")
            verification = result.get("requires_verification", False)
            audio = None
        else:
            body = {
                "voice_description": payload["description"],
                "text": payload["text"],
                "guidance_scale": connection["guidance_scale"],
                "preview_count": 1,
                "language_code": payload["language_code"],
            }
            if connection["model_id"]:
                body["model_id"] = connection["model_id"]
            result = await self.request(
                connection,
                "POST",
                "voice-previews/design",
                max_json_bytes=AUDIO_LIMIT * 2,
                params={"output_format": "wav"},
                json=body,
            )
            try:
                item = result["previews"][0]
                preview_id = item["generated_voice_id"]
            except (KeyError, IndexError, TypeError):
                raise BreezeError("官方声音设计预览响应无效。") from None
            try:
                encoded = item["audio_base_64"]
                if (
                    not isinstance(encoded, str)
                    or len(encoded) > AUDIO_LIMIT * 4 // 3 + 4
                ):
                    raise ValueError
                audio = playable_audio(base64.b64decode(encoded, validate=True), "wav")
            except (ValueError, KeyError, TypeError):
                audio = None
            verification = False
        if (
            not isinstance(preview_id, str)
            or not 1 <= len(preview_id) <= 200
            or type(verification) is not bool
        ):
            raise BreezeError("官方预览 ID 或验证状态无效。")
        validate_identifier(preview_id, "音色预览 ID", 200)
        if recording is not None and not verification:
            try:
                raw, _, _ = await self.request(
                    connection,
                    "GET",
                    "voice-previews/" + quote(preview_id, safe="") + "/stream",
                    raw=True,
                    params={"output_format": "wav"},
                )
                audio = playable_audio(raw, "wav")
            except BreezeError:
                # Preserve the paid preview identity when only audition fails.
                audio = None
        return preview_id, verification, audio
