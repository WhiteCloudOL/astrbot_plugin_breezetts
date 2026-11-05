"""Separate Breeze performance controls from visible dialogue."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .settings import BreezeError

# Source: https://docs.breezeblue.ai/guides/audio-tags
AUDIO_TAGS = json.loads(
    (Path(__file__).parent / "assets" / "breeze_audio_tags.json").read_text(
        encoding="utf-8"
    )
)
LOCAL_ALIASES = ("(laugh)", "(cough)", "(sigh)", "[清嗓子]")
EVENTS = {tag for values in AUDIO_TAGS.values() for tag in values} | set(LOCAL_ALIASES)
CONTROL = re.compile(
    r"(?P<protected>```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)|`[^`\n]*(?:`|$)"
    r"|\\[\[(][^\]\)\r\n]*[\]\)]|\[[^\]\r\n]*\]\((?!"
    + "|".join(re.escape(tag[1:]) for tag in EVENTS if tag.startswith("("))
    + r")[^\)\r\n]*\))"
    r"|(?P<direction>\[(?:(?:语气|style|話し方)\s*[:：][^\]\r\n]*(?:\]|(?=\r?\n)|$)"
    r"|用[^\[\]\r\n]+?语气(?:说话|说)?\]))"
    r"|(?P<event>"
    + "|".join(re.escape(tag) for tag in sorted(EVENTS, key=len, reverse=True))
    + ")",
    re.I,
)


def clean_breeze_text(text: str) -> str:
    """Hide recognized controls while preserving ordinary brackets and code.

    Args:
        text: Dialogue containing optional Breeze controls.

    Returns:
        Visible text with its original whitespace and protected literals intact.
    """
    return CONTROL.sub(lambda match: match[0] if match["protected"] else "", text)


def parse_breeze_text(text: str, *, enabled: bool) -> list[tuple[str, str]]:
    """Build bounded synthesis sections with independent performance instructions.

    Args:
        text: Speech input containing native events and free-form directions.
        enabled: Whether dynamic controls are allowed for this synthesis.

    Returns:
        Ordered pairs of synthesis text and the direction for that section.

    Raises:
        BreezeError: If an enabled direction is malformed, empty, or excessive.
    """
    if not enabled:
        plain = clean_breeze_text(text).strip()
        return [(plain, "")] if plain else []
    parts = []
    cursor, direction, count = 0, "", 0
    for match in CONTROL.finditer(text):
        if not match["direction"]:
            continue
        fragment = text[cursor : match.start()].strip()
        if fragment:
            parts.append((fragment, direction))
        elif direction:
            raise BreezeError("每个语气标记后需要填写朗读文字或音频事件。")
        token = match[0]
        if not token.endswith("]"):
            raise BreezeError("语气标记须完整，例如 [语气:温柔]。")
        direction = re.sub(
            r"^(?:语气|style|話し方)\s*[:：]", "", token[1:-1], flags=re.I
        ).strip()
        count += 1
        if (
            not 1 <= len(direction) <= 200
            or "[" in direction
            or any(ord(char) < 32 for char in direction)
        ):
            raise BreezeError("每段语气描述须为 1–200 个字符，且不能嵌套标记。")
        if count > 8:
            raise BreezeError("一次合成最多使用 8 个语气标记。")
        cursor = match.end()
    fragment = text[cursor:].strip()
    if fragment:
        parts.append((fragment, direction))
    elif direction:
        raise BreezeError("语气标记后需要填写朗读文字或音频事件。")
    return parts


def control_prompt(*, local: bool, language: str = "") -> str:
    """Describe supported controls using a compact, language-specific event table.

    Args:
        local: Whether the bilingual self-hosted model is selected.
        language: Configured cloud language, or empty for automatic detection.

    Returns:
        Model guidance without unrelated languages or account configuration.
    """
    languages = (
        [language.lower()]
        if not local and language.lower() in AUDIO_TAGS
        else ["zh", "en"]
    )
    rows = []
    for code in languages:
        tags = list(AUDIO_TAGS[code])
        if local:
            tags = [
                {
                    "[清嗓]": "[清嗓子]",
                    "(laughs)": "(laugh)",
                    "(coughs)": "(cough)",
                    "(sighs)": "(sigh)",
                }.get(tag, tag)
                for tag in tags
            ]
        rows.append(f"{code}: {' '.join(tags)}")
    return (
        "Dynamic Breeze controls are enabled. Use instruction for an overall delivery style, "
        "and [语气:描述] / [style:direction] / [話し方:説明] for local changes until the next marker; "
        "[用温柔的语气] is also supported. Directions must match the dialogue language, 1–200 characters, "
        "at most 8 markers. Native events stay inside text; free directions become separate instructions. "
        "Controls are hidden from captions. Preserve the complete dialogue. Use punctuation for timing; "
        "insert a single listed pause where necessary, never stack pauses or invent duration syntax. "
        "Only use these exact native tags, in the spoken language (local model support varies):\n"
        + "\n".join(rows)
    )
