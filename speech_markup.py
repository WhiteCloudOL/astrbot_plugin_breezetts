"""Parse transient speech markup without depending on the host runtime."""

import re

TAG = re.compile(
    r"<\s*/?\s*(?:breeze[_-]tts|tts)\b[^<>]*>|<\s*/?\s*(?:breeze[_-]tts|tts)\b", re.I
)
ENCODED_TAG = re.compile(r"&lt;(/?\s*(?:breeze[_-]tts|tts)\b[^&<>]*)&gt;", re.I)
TOKEN = re.compile(r"```|~~~|`|" + TAG.pattern, re.I)


def parse_speech(text: str) -> list[tuple[str, bool]]:
    """Remove speech markup and mark only balanced spoken blocks.

    Args:
        text: Model output, including optional speech tags.

    Returns:
        Ordered text spans and whether each is eligible for synthesis. Incomplete
        blocks retain their text, nested blocks form one span, and code stays text.
    """
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
