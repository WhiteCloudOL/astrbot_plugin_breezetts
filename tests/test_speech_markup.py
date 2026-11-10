"""Check speech markup balancing and fallback independently of AstrBot."""

import pytest
from breezetts_test.speech_markup import (
    clean_speech_bubble,
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
        ("<tts>你好</tts>！后面文字。", "你好！后面文字。", ["你好！"]),
        ("“<tts>你好</tts>。”后面文字。", "“你好。”后面文字。", ["“你好。”"]),
        ("<tts>等等</tts>……", "等等……", ["等等……"]),
        ("<tts>Wait</tts>... Next.", "Wait... Next.", ["Wait..."]),
        ("<tts>版本 1</tts>.2", "版本 1.2", ["版本 1"]),
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
        ("先等一下……后面继续说。", "先等一下……"),
        ("先等一下...后面继续说。", "先等一下..."),
        ("**先说这一句。**后面继续说。", "**先说这一句。**"),
        ("她说：“再等一下！”后面继续说。", "她说：“再等一下！”"),
        ("这是提醒（一定记住！）后面继续说。", "这是提醒（一定记住！）"),
        ("版本 1.2 已经更新。后面继续说。", "版本 1.2 已经更新。"),
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


def test_random_limit_does_not_leave_a_punctuation_only_remainder():
    assert select_spoken_span("完整的一句话。", 6) is None
    text = "完整的一句话。后面继续说。"
    span = select_spoken_span(text, 6)
    assert text[slice(*span)] == "后面继续说。"


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "\n\n而且看了一眼跟特意整理成故事能一样嘛，反正我不说。",
            "而且看了一眼跟特意整理成故事能一样嘛，反正我不说",
        ),
        ("\r\n \r\n内容很长。中间标点保留。\r\n\t\r\n", "内容很长。中间标点保留"),
        ("第一段。\n\n第二段！", "第一段。\n\n第二段！"),
        ("真的？！", "真的？！"),
        ("等等……", "等等……"),
        ("你好～", "你好～"),
        ("Hello. Version 1.2.", "Hello. Version 1.2."),
        ("她说：“你好。”", "她说：“你好。”"),
        ("`原样。`", "`原样。`"),
        ("\n```text\n原样。\n```\n", "```text\n原样。\n```"),
        ("```text\n未闭合代码。", "```text\n未闭合代码。"),
        ("    缩进代码。", "    缩进代码。"),
        ("\n\n\u200b\n。\n\n", ""),
        ("正文。\n\n。\n", "正文"),
        (" \n\u200b\n", ""),
    ],
)
def test_voice_bubble_edges_preserve_internal_text_and_expressive_punctuation(
    text, expected
):
    cleaned = clean_speech_bubble(text)
    assert cleaned == expected
    assert cleaned in text
    assert clean_speech_bubble(cleaned) == cleaned


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
