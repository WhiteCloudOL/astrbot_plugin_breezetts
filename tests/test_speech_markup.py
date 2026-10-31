"""Check speech markup balancing and fallback independently of AstrBot."""

import pytest
from breezetts_test.speech_markup import parse_speech


@pytest.mark.parametrize(
    "raw,clean,spoken",
    [
        ("前<tts>你好</tts>后", "前你好后", ["你好"]),
        ("<BREEZE_TTS >你好</BREEZE_TTS>", "你好", ["你好"]),
        ("<tts>外<tts>内</tts>层</tts>", "外内层", ["外内层"]),
        ("<tts>未闭合", "未闭合", []),
        ("尾部</tts>", "尾部", []),
        ("文本<tts", "文本", []),
        ("<tts/>普通", "普通", []),
        ("```xml\n<tts>代码</tts>\n```", "```xml\n代码\n```", []),
        ("`<tts>代码</tts>`", "`代码`", []),
        ("<tts>含`代码`</tts>", "含`代码`", []),
        ("&lt;tts&gt;你好&lt;/tts&gt;", "你好", ["你好"]),
        ("<tts>甲</tts>和<breeze_tts>乙</breeze_tts>", "甲和乙", ["甲", "乙"]),
    ],
)
def test_markup_balancing_and_code(raw, clean, spoken):
    parts = parse_speech(raw)
    assert "".join(text for text, _ in parts) == clean
    assert [text for text, voice in parts if voice] == spoken
