"""Check speech markup balancing and fallback independently of AstrBot."""

import pytest
from breezetts_test.speech_markup import (
    parse_speech,
    select_spoken_span,
    strip_delivery_ack,
)


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
        ("`[笑]` [语气:温柔]你好。", "`[笑]` 你好。", []),
        ("```text\n[style:literal][笑]\n```", "```text\n[style:literal][笑]\n```", []),
    ],
)
def test_markup_balancing_and_code(raw, clean, spoken):
    parts = parse_speech(raw)
    assert "".join(text for text, _ in parts) == clean
    assert [text for text, voice in parts if voice] == spoken


@pytest.mark.parametrize(
    "text,expected",
    [
        ("你好呀。后面继续文字。", "你好呀。"),
        ("\n  你好！\n下一句", "你好！"),
        ("Hello there. More text follows.", "Hello there."),
        ("```python\nprint('code')\n```\n这是解释。", "这是解释。"),
        ("访问 https://example.com。然后继续聊天。", "然后继续聊天。"),
        ("- 清单条目\n1. 另一条\n自然聊天内容。", "自然聊天内容。"),
        ("（语音已发送）\n你好。", "你好。"),
        ("```only code```", None),
        ("`only code`", None),
        ("https://example.com", None),
        ("# 标题\n- 列表", None),
        ("🎧✨", None),
    ],
)
def test_program_selects_prose_and_preserves_original_offsets(text, expected):
    span = select_spoken_span(text, 1000)
    assert (text[slice(*span)] if span else None) == expected


def test_random_passage_is_bounded_without_merging_the_full_reply():
    text = "自然内容" * 100
    start, end = select_spoken_span(text, 1000)
    assert (start, end) == (0, 200)
    assert select_spoken_span(text, 12) == (0, 12)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("（语音已发送）", ""),
        ("（语音已发送。）", ""),
        ("（已发送语音）", ""),
        ("**语音已发送**", ""),
        ("(语音已发送)", ""),
        ("语音已发送。", ""),
        ("Voice message sent successfully.", ""),
        ("（语音已发送）你听到了吗？", "你听到了吗？"),
        ("（语音已发送）\n你听到了吗？", "你听到了吗？"),
        ("你说的‘语音已发送’是什么意思？", "你说的‘语音已发送’是什么意思？"),
        ("语音已发送了吗？", "语音已发送了吗？"),
        ("今晚聊点什么？", "今晚聊点什么？"),
    ],
)
def test_strip_status_preserves_real_dialogue(text, expected):
    assert strip_delivery_ack(text) == expected
