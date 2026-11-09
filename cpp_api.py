"""Implement the shared HTTP extensions of upstream and authenticated C++ servers."""

from __future__ import annotations

import asyncio
import json
import math
import re
import secrets
import uuid
from urllib.parse import quote

import aiohttp

from .settings import BreezeError, validate_identifier
from .storage import playable_audio


def voice_info(value: dict) -> dict:
    """Validate one server-owned voice without exposing arbitrary response fields.

    Args:
        value: A voice object from the C++ server.

    Returns:
        Validated public voice identity, metadata, and cache information.

    Raises:
        BreezeError: If the server returned an invalid voice object.
    """
    if not isinstance(value, dict):
        raise BreezeError("服务端音色数据格式无效。")
    identifier = validate_identifier(value.get("id"), label="服务端音色 ID", limit=64)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
        raise BreezeError("服务端音色 ID 格式无效。")
    for key in ("seconds", "frames", "encode_ms"):
        number = value.get(key, 0)
        if type(number) not in {int, float} or not math.isfinite(number) or number < 0:
            raise BreezeError("服务端音色时长或编码信息无效。")
    text, description = value.get("ref_text", ""), value.get("description", "")
    metadata = value.get("metadata", {})
    if (
        not isinstance(text, str)
        or len(text) > 16000
        or not isinstance(description, str)
        or len(description.encode("utf-8")) > 4096
        or type(value.get("saved", False)) is not bool
        or not isinstance(metadata, dict)
        or len(metadata) > 32
        or any(
            not isinstance(k, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", k)
            or not isinstance(v, str)
            or len(v.encode("utf-8")) > 1024
            for k, v in metadata.items()
        )
    ):
        raise BreezeError("服务端音色描述或元数据无效。")
    return {
        "id": identifier,
        "saved": value.get("saved", False),
        "ref_text": text,
        "description": description,
        "metadata": metadata,
        **{key: value.get(key, 0) for key in ("frames", "seconds", "encode_ms")},
    }


class CppAPI:
    """Reuse the plugin's authenticated, bounded transport for C++ HTTP features."""

    def __init__(self, client):
        self.client = client

    async def status(self, connection: dict) -> dict:
        """Read optional model and FIFO status without requiring a fork-only route.

        Args:
            connection: Validated local connection settings.

        Returns:
            Whitelisted model and queue information; missing routes return None.

        Raises:
            BreezeError: If authentication, transport, or response validation fails.
        """
        model, queue = await asyncio.gather(
            self.client.request(connection, "GET", "model", optional=True),
            self.client.request(connection, "GET", "queue", optional=True),
        )
        if model is not None and (
            not isinstance(model, dict)
            or any(
                not isinstance(model.get(k), str) or len(model[k]) > 200
                for k in ("model", "backend")
            )
            or type(model.get("sample_rate")) is not int
        ):
            raise BreezeError("服务端模型信息格式无效。")
        if queue is not None and (
            not isinstance(queue, dict)
            or any(
                type(queue.get(k)) is not int or queue[k] < 0
                for k in ("active", "waiting", "capacity")
            )
            or queue["capacity"] < 1
        ):
            raise BreezeError("服务端队列信息格式无效。")
        return {
            "model": {k: model[k] for k in ("model", "backend", "sample_rate")}
            if model
            else None,
            "queue": {k: queue[k] for k in ("active", "waiting", "capacity")}
            if queue
            else None,
        }

    async def voices(self, connection: dict, identifier: str = ""):
        """Read the server catalog or one voice, including saved and cached entries.

        Args:
            connection: Validated local connection settings.
            identifier: Optional server voice ID.

        Returns:
            A voice object, or a list of voice objects.

        Raises:
            BreezeError: If the catalog is absent, malformed, or inaccessible.
        """
        path = "voices"
        if identifier:
            if not isinstance(identifier, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,64}", identifier
            ):
                raise BreezeError("服务端音色 ID 格式无效。")
            path += "/" + quote(
                validate_identifier(identifier, label="服务端音色 ID", limit=64),
                safe="",
            )
        result = await self.client.request(connection, "GET", path)
        if identifier:
            return voice_info(result)
        if not isinstance(result, list):
            raise BreezeError("服务端音色列表格式无效。")
        return [voice_info(item) for item in result]

    async def voice(
        self,
        connection: dict,
        operation: str,
        payload: dict,
        reference: tuple[bytes, str] | None = None,
    ) -> dict:
        """Register, edit, or unload a server voice using documented metadata limits.

        Args:
            connection: Validated local connection settings.
            operation: register, edit, or remove.
            payload: Voice name/ID and optional description/metadata patch.
            reference: Normalized WAV and exact transcript for registration.

        Returns:
            Updated voice data, or an unload result with file-kept semantics.

        Raises:
            BreezeError: If input or the upstream response is invalid.
        """
        if (
            not isinstance(operation, str)
            or operation not in {"register", "edit", "remove"}
            or not isinstance(payload, dict)
        ):
            raise BreezeError("服务端音色操作无效。")
        identifier = payload.get("id", "")
        if operation != "register":
            identifier = validate_identifier(
                identifier, label="服务端音色 ID", limit=64
            )
            if not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
                raise BreezeError("服务端音色 ID 格式无效。")
        patch = {}
        if "description" in payload:
            description = payload["description"]
            if description is not None and (
                not isinstance(description, str)
                or len(description.encode("utf-8")) > 4096
            ):
                raise BreezeError("音色描述最多 4096 个 UTF-8 字节。")
            patch["description"] = description
        if "metadata" in payload:
            metadata = payload["metadata"]
            if (
                not isinstance(metadata, dict)
                or len(metadata) > 32
                or any(
                    not isinstance(k, str)
                    or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", k)
                    or k == "description"
                    or (
                        v is not None
                        and (not isinstance(v, str) or len(v.encode("utf-8")) > 1024)
                    )
                    for k, v in metadata.items()
                )
            ):
                raise BreezeError(
                    "元数据最多 32 项：名称为 1–64 个字母、数字、点、短横线或下划线，值最多 1024 个 UTF-8 字节；null 可移除字段。"
                )
            patch["metadata"] = metadata
        self.client.logger.info(
            "Breeze C++ voice operation (operation=%s, voice_id=%s).",
            operation,
            identifier,
        )
        if operation == "register":
            name = payload.get("name", "")
            if not isinstance(name, str) or (
                name and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name)
            ):
                raise BreezeError(
                    "持久音色名须为 1–64 个字母、数字、短横线或下划线；留空创建内存缓存。"
                )
            if reference is None or not reference[1].strip():
                raise BreezeError("注册音色需要参考录音和准确文字稿。")
            form = aiohttp.FormData()
            form.add_field(
                "ref_audio",
                reference[0],
                filename="reference.wav",
                content_type="audio/wav",
            )
            form.add_field("ref_text", reference[1])
            if name:
                form.add_field("name", name)
            for key, value in patch.items():
                form.add_field(
                    key,
                    json.dumps(value, ensure_ascii=False)
                    if key == "metadata"
                    else value or "",
                )
            result = await self.client.request(connection, "POST", "voices", data=form)
        elif operation == "edit":
            result = await self.client.request(
                connection, "PATCH", "voices/" + quote(identifier, safe=""), json=patch
            )
        else:
            result = await self.client.request(
                connection, "DELETE", "voices/" + quote(identifier, safe="")
            )
            if (
                not isinstance(result, dict)
                or result.get("deleted") != identifier
                or type(result.get("file_kept")) is not bool
            ):
                raise BreezeError("移除音色的响应格式无效。")
            return {"deleted": identifier, "file_kept": result["file_kept"]}
        return voice_info(result)

    async def synthesize(self, connection: dict, text: str, reference, request_id: str):
        """Use native form/PCM or OpenAI JSON formats with the same sampling controls.

        Args:
            connection: Validated C++ connection settings.
            text: Complete section text.
            reference: Optional normalized WAV and transcript.
            request_id: Correlation identifier for this synthesis.

        Returns:
            Validated playable audio bytes and extension.

        Raises:
            BreezeError: If voice selection, response format, or transport fails.
        """
        payload = {
            "text": text,
            "instruction": connection["instruction"],
            "cfg_scale": connection["cfg_scale"],
            "seed": secrets.randbits(32)
            if connection["random_seed"]
            else connection["seed"],
        }
        # The C++ API stores int32 seeds; preserve all uint32 bits without atoi overflow.
        if payload["seed"] >= 2**31:
            payload["seed"] -= 2**32
        for key in (
            "temperature",
            "top_k",
            "top_p",
            "repetition_penalty",
            "max_new_tokens",
        ):
            if connection[key]:
                payload[key] = connection[key]
        if connection["split_chars"] >= 0:
            payload["split_chars"] = connection["split_chars"]
        if connection["mode"] == "voice":
            payload["voice_id"] = validate_identifier(
                connection["voice_id"], label="服务端音色 ID", limit=64
            )
        elif connection["mode"] == "reference" and (
            reference is None or not reference[1].strip()
        ):
            raise BreezeError("参考音色模式需要录音和准确文字稿。")
        elif connection["mode"] == "design" and not connection["instruction"].strip():
            raise BreezeError("声音设计模式需要填写声音描述。")
        if connection["output_format"] == "pcm":
            form = aiohttp.FormData()
            for key, value in payload.items():
                form.add_field(key, str(value))
            if connection["mode"] == "reference":
                form.add_field(
                    "ref_audio",
                    reference[0],
                    filename="reference.wav",
                    content_type="audio/wav",
                )
                form.add_field("ref_text", reference[1])
            kwargs = {"data": form}
        else:
            if connection["mode"] == "reference":
                cached = await self.voice(connection, "register", {}, reference)
                payload["voice_id"] = cached["id"]
            payload["input"] = payload.pop("text")
            payload["instructions"] = payload.pop("instruction")
            if "voice_id" in payload:
                payload["voice"] = payload.pop("voice_id")
            payload.update(
                response_format=connection["output_format"], stream_format="audio"
            )
            kwargs = {"json": payload}
        self.client.logger.info(
            "Breeze C++ synthesis payload (request_id=%s, payload=%r, reference_text=%r, reference_bytes=%s).",
            request_id,
            payload,
            reference[1] if reference else "",
            len(reference[0]) if reference else 0,
        )
        raw, headers, _ = await self.client.request(
            connection,
            "POST",
            "audio/speech",
            raw=True,
            request_id=request_id,
            **kwargs,
        )
        content_type = headers.get("Content-Type", "").split(";")[0].lower()
        fmt = {
            "audio/pcm": "pcm",
            "application/octet-stream": "pcm",
            "audio/wav": "wav",
            "audio/x-wav": "wav",
            "audio/mpeg": "mp3",
        }.get(content_type)
        if fmt is None:
            raise BreezeError("C++ 服务未返回支持的 PCM、WAV 或 MP3 音频。")
        rate = headers.get("X-Sample-Rate", "24000")
        if (
            not re.fullmatch(r"[0-9]{4,5}", rate)
            or headers.get("X-Sample-Format", "s16le") != "s16le"
        ):
            raise BreezeError("C++ 服务的音频采样格式无效。")
        return playable_audio(raw, fmt, int(rate))

    async def convert(
        self,
        connection: dict,
        payload: dict,
        source: bytes,
        reference=None,
        request_id: str = "",
    ):
        """Convert a normalized recording using a saved voice or reference WAV.

        Args:
            connection: Validated C++ connection settings.
            payload: Target ID, optional transcript, and conversion parameters.
            source: Normalized source WAV.
            reference: Optional target WAV and exact transcript.
            request_id: Optional conversion correlation identifier.

        Returns:
            Playable converted WAV bytes and extension.

        Raises:
            BreezeError: If conversion inputs or the upstream PCM response are invalid.
        """
        fields = {
            "text": payload.get("text", ""),
            "temperature": payload.get("temperature", 0.3),
            "top_k": payload.get("top_k", 1),
            "cfg_scale": payload.get("cfg_scale", 1.0),
            "keep_acoustic": payload.get("keep_acoustic", 0),
            "seed": payload.get(
                "seed",
                secrets.randbits(32)
                if connection["random_seed"]
                else connection["seed"],
            ),
        }
        if not isinstance(fields["text"], str) or len(fields["text"]) > 16000:
            raise BreezeError("源录音文字稿最多 16000 字符，可留空。")
        for key, low, high in (
            ("temperature", 0, 5),
            ("top_k", 0, 10000),
            ("cfg_scale", 0.01, 10),
            ("keep_acoustic", 0, 15),
            ("seed", 0, 2**32 - 1),
        ):
            value = fields[key]
            expected = (
                {int} if key in {"top_k", "keep_acoustic", "seed"} else {int, float}
            )
            if (
                type(value) not in expected
                or not math.isfinite(value)
                or not low <= value <= high
            ):
                raise BreezeError(f"语音转换参数 {key} 须介于 {low} 和 {high}。")
        if payload.get("voice_id"):
            fields["voice_id"] = validate_identifier(
                payload["voice_id"], label="目标音色 ID", limit=64
            )
            if not re.fullmatch(r"[A-Za-z0-9_-]+", fields["voice_id"]):
                raise BreezeError("目标音色 ID 格式无效。")
        elif reference is None or not reference[1].strip():
            raise BreezeError("请选择服务端音色，或配有文字稿的目标参考录音。")
        if fields["seed"] >= 2**31:
            fields["seed"] -= 2**32
        form = aiohttp.FormData()
        form.add_field(
            "source", source, filename="source.wav", content_type="audio/wav"
        )
        for key, value in fields.items():
            form.add_field(key, str(value))
        if "voice_id" not in fields:
            form.add_field(
                "ref_audio",
                reference[0],
                filename="reference.wav",
                content_type="audio/wav",
            )
            form.add_field("ref_text", reference[1])
        request_id = request_id or uuid.uuid4().hex[:12]
        self.client.logger.info(
            "Breeze C++ conversion request (request_id=%s, payload=%r, source_bytes=%s, reference_bytes=%s).",
            request_id,
            fields,
            len(source),
            len(reference[0]) if reference else 0,
        )
        raw, headers, _ = await self.client.request(
            connection,
            "POST",
            "audio/convert",
            raw=True,
            data=form,
            request_id=request_id,
        )
        rate = headers.get("X-Sample-Rate", "24000")
        if (
            headers.get("Content-Type", "").split(";")[0] != "audio/pcm"
            or headers.get("X-Sample-Format", "s16le") != "s16le"
            or not re.fullmatch(r"[0-9]{4,5}", rate)
        ):
            raise BreezeError("语音转换未返回有效 PCM 音频。")
        return playable_audio(raw, "pcm", int(rate))
