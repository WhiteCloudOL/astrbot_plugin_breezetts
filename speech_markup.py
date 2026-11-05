"""Parse transient speech markup without depending on the host runtime."""

import re

from .breeze_control import clean_breeze_text

TAG = re.compile(
    r"<\s*/?\s*(?:breeze[_-]tts|tts)\b[^<>]*>|<\s*/?\s*(?:breeze[_-]tts|tts)\b", re.I
)
ENCODED_TAG = re.compile(r"&lt;(/?\s*(?:breeze[_-]tts|tts)\b[^&<>]*)&gt;", re.I)
TOKEN = re.compile(r"```|~~~|`|" + TAG.pattern, re.I)
DELIVERY_ACK = (
    r"(?:(?:语音|音频)(?:消息)?(?:已|已经)?(?:成功)?(?:发送|发出|送达)(?:了|成功|完成|完毕)?"
    r"|(?:已|已经)(?:成功)?(?:发送|发出|送达)(?:了)?(?:语音|音频)(?:消息)?"
    r"|(?:voice(?: message)?|audio)(?: has been| was)? (?:sent|delivered)(?: successfully)?)"
)


def parse_speech(
    text: str, *, preserve_controls: bool = False
) -> list[tuple[str, bool]]:
    """Remove speech markup and mark only balanced spoken blocks.

    Args:
        text: Model output, including optional speech tags.
        preserve_controls: Keep Breeze controls for synthesis planning only.

    Returns:
        Ordered text spans and whether each is eligible for synthesis. Incomplete
        blocks retain their text, nested blocks form one span, and code stays text.
    """
    if not preserve_controls:
        text = clean_breeze_text(text)
    text = ENCODED_TAG.sub(lambda match: f"<{match[1]}>", text)
    parts: list[tuple[str, bool]] = []
    pending = ""
    depth = 0
    cursor = 0
    fence = ""
    for match in TOKEN.finditer(text):
        fragment = text[cursor : match.start()]
        if depth:
            pending += fragment
        elif fragment:
            parts.append((fragment, False))
        token = match[0]
        if token in {"`", "```", "~~~"}:
            if depth:
                pending += token
            else:
                parts.append((token, False))
            if fence == token:
                fence = ""
            elif not fence:
                fence = token
            cursor = match.end()
            continue
        closing = bool(re.match(r"<\s*/", token))
        if not fence and token.endswith(">") and not token.rstrip().endswith("/>"):
            if closing and depth:
                depth -= 1
                if not depth:
                    if pending:
                        parts.append(
                            (pending, "`" not in pending and "~~~" not in pending)
                        )
                    pending = ""
            elif not closing:
                depth += 1
        cursor = match.end()
    tail = text[cursor:]
    if depth:
        pending += tail
        if pending:
            parts.append((pending, False))
    elif tail:
        parts.append((tail, False))
    return parts


def strip_delivery_ack(text: str) -> str:
    """Remove standalone delivery acknowledgments while retaining dialogue.

    Args:
        text: Model-generated content whose delivery status should stay internal.

    Returns:
        Content with exact status lines and leading bracketed statuses removed.
    """
    lines = []
    for line in text.splitlines(keepends=True):
        if re.fullmatch(
            r"\s*[（(\[【]?\s*"
            + DELIVERY_ACK
            + r"\s*[。.!！]*\s*[）)\]】]?[。.!！]*\s*",
            line.strip().strip("*_").strip(),
            re.I,
        ):
            continue
        lines.append(
            re.sub(
                r"^\s*[（(\[【]\s*"
                + DELIVERY_ACK
                + r"\s*[。.!！]*\s*[）)\]】][。.!！]*\s*",
                "",
                line,
                flags=re.I,
            )
        )
    return "".join(lines)


def select_spoken_span(text: str, limit: int) -> tuple[int, int] | None:
    """Choose one bounded prose passage without involving the language model.

    Args:
        text: Contiguous reply text whose original offsets must be preserved.
        limit: Configured character limit, further bounded to 200 for random speech.

    Returns:
        Original start/end offsets, or None for code, links, lists, or empty content.
    """
    limit = min(limit, 200)
    protected = [
        match.span()
        for match in re.finditer(
            r"```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)"
            r"|`[^`\n]*(?:`|$)|https?://[^\s<>。！？!?]+|<[^>]+>",
            text,
        )
    ]
    for line in re.finditer(r"[^\n]+", text):
        if re.match(r"\s*(?:#{1,6}\s|[-*+]\s|\d+[.)、]\s*|>\s*|\|)", line[0]):
            continue
        for sentence in re.finditer(
            r".+?(?:[。！？!?]+[”’\"']*|(?<!\d)\.(?=\s|$)|$)",
            line[0],
        ):
            content = sentence[0].strip()
            start = (
                line.start()
                + sentence.start()
                + len(sentence[0])
                - len(sentence[0].lstrip())
            )
            end = start + len(content)
            if (
                not any(char.isalnum() for char in content)
                or not strip_delivery_ack(content).strip()
                or any(start < stop and end > begin for begin, stop in protected)
            ):
                continue
            if end - start > limit:
                end = start + limit
                boundaries = list(re.finditer(r"[\s，,；;、：:]", text[start:end]))
                if boundaries:
                    end = start + boundaries[-1].end()
                elif content.isascii():
                    continue
            while end > start and text[end - 1].isspace():
                end -= 1
            if end > start:
                return start, end
    return None
