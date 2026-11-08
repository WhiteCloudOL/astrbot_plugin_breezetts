"""Join bounded PCM WAV sections without an external audio process."""

from __future__ import annotations

import wave
from io import BytesIO

from .breeze_control import AUDIO_TAGS, clean_breeze_text
from .settings import BreezeError
from .storage import AUDIO_LIMIT


def join_breeze_audio(
    audio: list[bytes],
    texts: list[str],
    *,
    tags: tuple[str, ...] | None = None,
    minimum_tail_ms: int = 0,
    whole_seconds: bool = False,
) -> bytes:
    """Join matching WAV sections and preserve natural pauses between directions.

    Args:
        audio: Complete PCM WAV sections from one synthesis request.
        texts: Matching sections used to determine punctuation and explicit pauses.
        tags: Selected provider's native controls, or Breeze defaults when omitted.
        minimum_tail_ms: Minimum quiet tail for platform transcoding and playback.
        whole_seconds: Pad total duration upward to whole seconds after adding
            the quiet tail, protecting against rounded platform metadata.

    Returns:
        A single WAV file, with existing quiet edges counted toward each pause.

    Raises:
        BreezeError: If WAV data is invalid, incompatible, empty, or oversized.
    """
    if not audio or len(audio) != len(texts):
        raise BreezeError("动态语调音频分段数量不正确。")
    output = BytesIO()
    pauses = tuple({values[19] for values in AUDIO_TAGS.values()})
    parameters = None
    previous_tail = 0
    total = 44
    try:
        with wave.open(output, "wb") as target:
            for index, raw in enumerate(audio):
                with wave.open(BytesIO(raw), "rb") as source:
                    current = (
                        source.getnchannels(),
                        source.getsampwidth(),
                        source.getframerate(),
                        source.getcomptype(),
                    )
                    if (
                        current[3] != "NONE"
                        or current[1] not in {1, 2, 3, 4}
                        or not 1 <= current[0] <= 2
                        or not 8000 <= current[2] <= 192000
                    ):
                        raise BreezeError("动态语调需要有效的 PCM WAV 音频。")
                    if parameters is None:
                        parameters = current
                        target.setnchannels(current[0])
                        target.setsampwidth(current[1])
                        target.setframerate(current[2])
                    elif current != parameters:
                        raise BreezeError(
                            "动态语调分段音频的采样率、声道或采样宽度不一致。"
                        )
                    frames = source.readframes(source.getnframes())
                    stride = current[0] * current[1]
                    if not frames or len(frames) != source.getnframes() * stride:
                        raise BreezeError("动态语调分段音频为空或不完整。")
                quiet = []
                # Count quiet edges only; never trim or overwrite original samples.
                threshold = max(1, (1 << (current[1] * 8 - 1)) // 1024)
                for reverse in (False, True):
                    count = 0
                    for position in range(min(len(frames) // stride, current[2])):
                        offset = (
                            len(frames) - (position + 1) * stride
                            if reverse
                            else position * stride
                        )
                        values = (
                            [
                                frames[offset + channel] - 128
                                for channel in range(current[0])
                            ]
                            if current[1] == 1
                            else [
                                int.from_bytes(
                                    frames[
                                        offset + channel * current[1] : offset
                                        + (channel + 1) * current[1]
                                    ],
                                    "little",
                                    signed=True,
                                )
                                for channel in range(current[0])
                            ]
                        )
                        if any(abs(value) > threshold for value in values):
                            break
                        count += 1
                    quiet.append(count)
                silence = b""
                if index:
                    left, right = texts[index - 1].rstrip(), texts[index].lstrip()
                    ending = clean_breeze_text(left, tags).rstrip(
                        " \t\r\n\"'”’»」』】）》）)]"
                    )
                    milliseconds = (
                        300
                        if left.endswith(pauses)
                        or right.startswith(pauses)
                        or ending.endswith(("。", "！", "？", ".", "!", "?", "…"))
                        else 150
                        if ending.endswith(("，", ",", "；", ";", "：", ":"))
                        else 100
                    )
                    missing = max(
                        0, current[2] * milliseconds // 1000 - previous_tail - quiet[0]
                    )
                    silence = (b"\x80" if current[1] == 1 else b"\x00") * (
                        missing * stride
                    )
                total += len(silence) + len(frames)
                if total > AUDIO_LIMIT:
                    raise BreezeError("动态语调合并音频超过大小上限。")
                target.writeframesraw(silence)
                target.writeframesraw(frames)
                previous_tail = quiet[1]
            # Keep speech ahead of encoder delay and integer-second platform metadata.
            missing = max(0, parameters[2] * minimum_tail_ms // 1000 - previous_tail)
            if whole_seconds:
                missing += (-target.getnframes() - missing) % parameters[2]
            tail = (b"\x80" if parameters[1] == 1 else b"\x00") * (
                missing * parameters[0] * parameters[1]
            )
            if total + len(tail) > AUDIO_LIMIT:
                raise BreezeError("动态语调合并音频超过大小上限。")
            target.writeframesraw(tail)
    except BreezeError:
        raise
    except (wave.Error, EOFError, ValueError, OverflowError):
        raise BreezeError("动态语调分段未返回完整的 PCM WAV 音频。") from None
    return output.getvalue()
