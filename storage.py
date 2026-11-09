"""Persist audio and reference metadata outside the plugin source tree."""

from __future__ import annotations

import io
import json
import math
import re
import time
import uuid
import wave
from pathlib import Path

import soundfile as sf

from .settings import BreezeError

UPLOAD_LIMIT = 20 * 1024 * 1024
AUDIO_LIMIT = 64 * 1024 * 1024
MIME = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "flac": "audio/flac",
    "aac": "audio/aac",
    "opus": "audio/ogg",
}


def playable_audio(data: bytes, fmt: str, rate: int = 24000) -> tuple[bytes, str]:
    """Validate binary audio and wrap signed mono PCM in a WAV container.

    Args:
        data: Complete audio bytes, after all network chunks have arrived.
        fmt: Breeze output format identifier.
        rate: Sample rate for raw little-endian 16-bit PCM.

    Returns:
        Playable audio bytes and their file extension.

    Raises:
        BreezeError: If audio is empty, malformed, or too large.
    """
    if not data or len(data) > AUDIO_LIMIT:
        raise BreezeError("音频为空或超过 64 MiB 上限。")
    extension = fmt.split("_")[0]
    # C++ streaming WAV uses unknown sizes; fix only its documented PCM header after EOF.
    if (
        extension == "wav"
        and data[:8] == b"RIFF\xff\xff\xff\xff"
        and data[36:44] == b"data\xff\xff\xff\xff"
    ):
        if data[8:16] != b"WAVEfmt " or data[20:22] != b"\x01\x00":
            raise BreezeError("服务返回的流式 WAV 头无效。")
        stride = int.from_bytes(data[32:34], "little")
        if not stride or (len(data) - 44) % stride:
            raise BreezeError("服务返回的流式 WAV 采样不完整。")
        data = (
            data[:4]
            + (len(data) - 8).to_bytes(4, "little")
            + data[8:40]
            + (len(data) - 44).to_bytes(4, "little")
            + data[44:]
        )
    if extension == "pcm":
        if len(data) % 2 or not 8000 <= rate <= 48000:
            raise BreezeError("PCM 数据不完整或采样率无效。")
        output = io.BytesIO()
        with wave.open(output, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(rate)
            audio.writeframes(data)
        return output.getvalue(), "wav"
    signatures = {
        "wav": data[:4] == b"RIFF" and data[8:12] == b"WAVE",
        "mp3": data[:3] == b"ID3"
        or (len(data) > 1 and data[0] == 255 and data[1] & 224 == 224),
        "flac": data[:4] == b"fLaC",
        "aac": len(data) > 1 and data[0] == 255 and data[1] & 246 == 240,
        "opus": data[:4] == b"OggS" and b"OpusHead" in data[:128],
    }
    if not signatures.get(extension, False):
        raise BreezeError("服务返回的音频与所选格式不一致，请检查服务类型和输出格式。")
    if extension in {"wav", "flac", "mp3"}:
        try:
            with sf.SoundFile(io.BytesIO(data)) as audio:
                if (
                    audio.frames <= 0
                    or not 1 <= audio.channels <= 2
                    or not 8000 <= audio.samplerate <= 192000
                ):
                    raise ValueError
        except (RuntimeError, ValueError):
            raise BreezeError(
                "服务返回的音频文件损坏或缺少有效采样。", code="audio_format"
            ) from None
    return data, extension


class AudioStore:
    """Own an atomic metadata index and bounded audio files."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.index = self.root / "audio-index.json"
        self.items: dict[str, dict] = {}
        if self.index.exists():
            try:
                if self.index.stat().st_size > 2 * 1024 * 1024:
                    raise ValueError
                self.items = json.loads(self.index.read_text(encoding="utf-8"))
                if not isinstance(self.items, dict):
                    raise ValueError
                for key, item in self.items.items():
                    if not isinstance(item, dict) or item.get("id") != key:
                        raise ValueError
                    self.path(key)
                    if (
                        item.get("kind") not in {"reference", "output", "preview"}
                        or item.get("extension") not in MIME
                        or not isinstance(item.get("name"), str)
                        or not 1 <= len(item["name"]) <= 120
                        or type(item.get("size")) is not int
                        or not 1 <= item["size"] <= AUDIO_LIMIT + 44
                        or type(item.get("created")) not in {int, float}
                        or not 0 <= item["created"] <= 10**12
                        or not item["filename"].endswith("." + item["extension"])
                    ):
                        raise ValueError
                    if item["kind"] == "reference" and (
                        not isinstance(item.get("ref_text"), str)
                        or len(item["ref_text"]) > 4000
                        or type(item.get("duration")) not in {int, float}
                        or not 0.1 <= item["duration"] <= 120
                        or type(item.get("sample_rate")) is not int
                    ):
                        raise ValueError
            except (ValueError, KeyError, TypeError):
                raise BreezeError(
                    "音频索引损坏，请备份并检查 audio-index.json，原文件已保留。"
                ) from None

    def persist(self) -> None:
        """Atomically save metadata without exposing a partially written index."""
        temporary = self.index.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.items, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.index)

    def path(self, audio_id: str) -> Path:
        """Resolve an indexed identifier without accepting arbitrary paths.

        Args:
            audio_id: Opaque ID returned by the plugin.

        Returns:
            A validated path inside plugin data storage.

        Raises:
            BreezeError: If the ID or indexed filename is invalid.
        """
        if (
            not isinstance(audio_id, str)
            or not re.fullmatch(r"[a-f0-9]{32}", audio_id)
            or audio_id not in self.items
        ):
            raise BreezeError("找不到音频，请刷新列表后重试。")
        item = self.items[audio_id]
        filename = item.get("filename", "")
        if not re.fullmatch(audio_id + r"\.(wav|mp3|flac|aac|opus)", filename):
            raise BreezeError("音频文件索引无效。")
        path = (self.root / filename).resolve()
        if path.parent != self.root:
            raise BreezeError("音频文件路径无效。")
        return path

    def add(
        self, data: bytes, extension: str, kind: str, name: str, **metadata
    ) -> dict:
        """Save a generated output or a validated reference recording.

        Args:
            data: Audio bytes to persist.
            extension: Validated audio extension.
            kind: Either reference, output, or preview.
            name: User-facing display name.
            **metadata: Audio properties and reference transcript.

        Returns:
            The saved audio record.
        """
        if (
            extension not in MIME
            or kind not in {"reference", "output", "preview"}
            or not isinstance(name, str)
            or not name.strip()
        ):
            raise BreezeError("音频名称或格式无效，请重新选择文件。")
        audio_id = uuid.uuid4().hex
        filename = f"{audio_id}.{extension}"
        path = self.root / filename
        item = {
            "id": audio_id,
            "filename": filename,
            "name": name[:120],
            "kind": kind,
            "size": len(data),
            "created": time.time(),
            "extension": extension,
            **metadata,
        }
        try:
            path.write_bytes(data)
            self.items[audio_id] = item
            self.persist()
        except OSError:
            self.items.pop(audio_id, None)
            path.unlink(missing_ok=True)
            raise
        return dict(item)

    def upload(self, data: bytes, filename: str) -> dict:
        """Validate a reference recording before saving it.

        Args:
            data: Uploaded file bytes with an enforced transport size limit.
            filename: Original filename used only as a display label.

        Returns:
            A reference record with duration, sample rate, and empty transcript.

        Raises:
            BreezeError: If format, duration, size, or library capacity is invalid.
        """
        if sum(item["kind"] == "reference" for item in self.items.values()) >= 100:
            raise BreezeError("参考录音已达 100 条，请先删除不需要的录音。")
        if not data or len(data) > UPLOAD_LIMIT:
            raise BreezeError("请上传不超过 20 MiB 的 WAV、MP3、FLAC 或 OGG 音频。")
        try:
            with sf.SoundFile(io.BytesIO(data)) as audio:
                if audio.format not in {"WAV", "WAVEX", "MP3", "FLAC", "OGG"}:
                    raise BreezeError("请上传 WAV、MP3、FLAC 或 OGG 音频。")
                duration = audio.frames / audio.samplerate
                if (
                    not 0.1 <= duration <= 120
                    or not 1 <= audio.channels <= 2
                    or not 8000 <= audio.samplerate <= 192000
                ):
                    raise BreezeError("参考录音须为 0.1–120 秒的单声道或双声道音频。")
                if (
                    audio.frames * 2 + 44 > UPLOAD_LIMIT
                    or audio.frames * audio.channels * 4 > AUDIO_LIMIT
                ):
                    raise BreezeError(
                        "解码后的录音超过大小上限，请缩短录音或降低采样率。"
                    )
                # Decode the bounded recording to detect corrupt/truncated uploads.
                samples = audio.read(dtype="float32", always_2d=True)
                if len(samples) != audio.frames:
                    raise BreezeError("音频不完整，无法读取全部采样。")
                if not math.isfinite(float(samples.max())) or not math.isfinite(
                    float(samples.min())
                ):
                    raise BreezeError("录音包含无效采样，请重新导出音频。")
                normalized = io.BytesIO()
                sf.write(
                    normalized,
                    samples.mean(axis=1),
                    audio.samplerate,
                    format="WAV",
                    subtype="PCM_16",
                )
                return self.add(
                    normalized.getvalue(),
                    "wav",
                    "reference",
                    Path(filename.replace("\\", "/")).name,
                    duration=round(duration, 3),
                    sample_rate=audio.samplerate,
                    ref_text="",
                )
        except (sf.LibsndfileError, RuntimeError, ValueError) as exc:
            if isinstance(exc, BreezeError):
                raise
            raise BreezeError("音频文件损坏或无法解码，请重新导出录音。") from None

    def remove(self, audio_id: str) -> None:
        """Delete one indexed audio file and its metadata.

        Args:
            audio_id: Identifier of the audio to delete.
        """
        path = self.path(audio_id)
        staged = path.with_suffix(path.suffix + ".delete")
        if path.exists():
            path.replace(staged)
        item = self.items.pop(audio_id)
        try:
            self.persist()
            staged.unlink(missing_ok=True)
        except OSError:
            self.items[audio_id] = item
            if staged.exists():
                staged.replace(path)
            self.persist()
            raise

    def prune(
        self, limit: int, preserve: set[str] | None = None, max_bytes: int | None = None
    ) -> None:
        """Keep the newest outputs and previews while retaining all references.

        Args:
            limit: Maximum number of generated audio files to retain.
            preserve: Audio identifiers currently reserved for delivery.
            max_bytes: Maximum total size of generated files, excluding references.
        """
        generated = sorted(
            # Stable sorting keeps the most recently inserted item on timestamp ties.
            (
                item
                for item in reversed(self.items.values())
                if item["kind"] != "reference"
            ),
            key=lambda item: item["created"],
            reverse=True,
        )
        preserve = preserve or set()
        kept = sum(item["id"] in preserve for item in generated)
        size = sum(item["size"] for item in generated if item["id"] in preserve)
        for item in generated:
            if item["id"] in preserve:
                continue
            if kept >= limit or (
                max_bytes is not None and size + item["size"] > max_bytes
            ):
                self.remove(item["id"])
            else:
                kept += 1
                size += item["size"]
