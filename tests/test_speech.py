"""Exercise selective speech with real AstrBot events and send stages."""

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

pytest.importorskip("astrbot")

from astrbot.api.event import MessageEventResult, ResultContentType
from astrbot.api.message_components import At, Image, Plain, Record, Reply
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core.agent.message import Message, TextPart, dump_messages_with_checkpoints
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from test_host import loaded_plugin  # noqa: F401


class CapturedEvent(AstrMessageEvent):
    """Capture actual AstrBot message chains at the platform boundary."""

    async def send(self, message):
        for part in message.chain:
            if isinstance(part, Record) and part.path:
                assert Path(part.path).is_file()
        self.sent.append(copy.deepcopy(message.chain))


@pytest.fixture
def event_factory():
    def make(
        text="Say hello", message_type=MessageType.FRIEND_MESSAGE, session="session"
    ):
        message = AstrBotMessage()
        message.type = message_type
        message.sender = MessageMember("user", "User")
        message.message = [Plain(text)]
        message.message_str = text
        message.message_id = "test-message"
        message.self_id = "bot"
        event = CapturedEvent(
            text, message, PlatformMetadata("test", "test", "test"), session
        )
        event.sent = []
        event.plugins_name = ["astrbot_plugin_breezetts"]
        return event

    return make


@pytest.fixture
async def speech_plugin(loaded_plugin, server):  # noqa: F811
    from aiohttp import web

    calls = []

    async def upstream(request):
        calls.append((await request.post())["text"])
        return web.Response(body=b"\x01\x00" * 2400, content_type="audio/pcm")

    _, context, metadata = loaded_plugin
    plugin = metadata.star_cls
    plugin.config["local"].update(base_url=await server(upstream), mode="plain")
    plugin.config["behavior"].update(
        auto_tts=True, trigger_probability=100, keep_text=False, min_interval_seconds=0
    )
    host = copy.deepcopy(DEFAULT_CONFIG)
    host.update(t2i=False)
    host["platform_settings"].update(
        reply_prefix="", reply_with_mention=False, reply_with_quote=False
    )
    host["provider_tts_settings"]["enable"] = False
    context.get_config = lambda *args, **kwargs: host
    context.get_using_tts_provider_async = AsyncMock(return_value=None)
    context.get_using_tts_provider = Mock(return_value=None)
    context.send_message = AsyncMock()
    return plugin, calls, host


@pytest.fixture
def run_model(speech_plugin):
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    from astrbot.core.astr_agent_context import AgentContextWrapper, AstrAgentContext
    from astrbot.core.astr_agent_hooks import MAIN_AGENT_HOOKS
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
    from astrbot.core.provider.register import llm_tools

    plugin, _, _ = speech_plugin

    async def run(event, responses):
        tool = next(tool for tool in llm_tools.func_list if tool.name == "breeze_tts")
        request = ProviderRequest(
            prompt=event.message_str,
            system_prompt="Stable persona",
            func_tool=ToolSet(tools=[tool]),
        )
        await plugin.prepare_speech(event)
        await plugin.prepare_speech_request(event, request)
        provider = SimpleNamespace(
            provider_config={"id": "test-provider"},
            text_chat=AsyncMock(side_effect=responses),
        )
        runner = ToolLoopAgentRunner()
        await runner.reset(
            provider,
            request,
            AgentContextWrapper(
                context=AstrAgentContext(context=plugin.context, event=event)
            ),
            FunctionToolExecutor(),
            MAIN_AGENT_HOOKS,
            streaming=False,
        )
        for _ in range(5):
            async for _ in runner.step():
                pass
            if runner.done():
                break
        assert runner.done()
        return runner, provider

    return run


async def test_probability_prompt_cache_and_tool_visibility(
    speech_plugin, event_factory
):
    plugin, _, _ = speech_plugin
    plugin.config["behavior"].update(trigger_probability=20, tool_enabled=False)
    event = event_factory()
    tool = FunctionTool(name="breeze_tts", description="Speech", parameters={})
    tools = ToolSet(tools=[tool])
    req = ProviderRequest(
        prompt="Hello",
        system_prompt="Stable persona",
        contexts=[{"role": "assistant", "content": "History"}],
        func_tool=tools,
    )
    with patch("random.random", return_value=0.1) as draw:
        await plugin.prepare_speech(event)
        await plugin.prepare_speech_request(event, req)
        await plugin.prepare_speech_request(event, req)
        assert draw.call_count == 1
    assert event.get_extra("enable_streaming") is False
    assert req.system_prompt == "Stable persona" and req.prompt == "Hello"
    assert req.contexts == [{"role": "assistant", "content": "History"}]
    assert req.extra_user_content_parts == []
    assert req.func_tool.get_tool("breeze_tts") is None
    assert tools.get_tool("breeze_tts") is tool
    dumped = dump_messages_with_checkpoints(
        [
            Message(
                role="user",
                content=[TextPart(text="Hello"), *req.extra_user_content_parts],
            )
        ]
    )
    assert dumped[0]["content"] == [{"type": "text", "text": "Hello"}]
    # Reusing a request with a different event drops its previous temporary hint.
    with patch("random.random", return_value=0.9):
        missed = event_factory()
        await plugin.prepare_speech(missed)
        await plugin.prepare_speech_request(missed, req)
    assert missed.get_extra("enable_streaming") is None
    assert req.extra_user_content_parts == []
    plugin.config["behavior"].update(auto_tts=False, tool_enabled=True)
    req = ProviderRequest(func_tool=tools, system_prompt="Stable persona")
    await plugin.prepare_speech_request(event_factory(), req)
    assert req.func_tool is tools and req.extra_user_content_parts == []


async def test_clean_history_selective_chain_and_failure(
    speech_plugin, event_factory, monkeypatch, caplog
):
    plugin, calls, _ = speech_plugin
    event = event_factory()
    raw = "前面文字。<breeze_tts>只朗读这一句</breeze_tts>后面文字。"
    resp = LLMResponse(role="assistant", completion_text=raw)
    await plugin.clean_speech_response(event, resp)
    await plugin.clean_speech_response(event, resp)
    messages = [Message(role="assistant", content=[TextPart(text=raw)])]
    await plugin.clean_speech_history(event, SimpleNamespace(messages=messages), resp)
    assert (
        messages[0].content[0].text
        == resp.completion_text
        == "前面文字。只朗读这一句后面文字。"
    )
    event.set_result(
        MessageEventResult(
            chain=[Plain(resp.completion_text)],
            result_content_type=ResultContentType.LLM_RESULT,
        )
    )
    await plugin.auto_speak(event)
    await plugin.auto_speak(event)
    chain = event.get_result().chain
    assert [type(part) for part in chain] == [Plain, Record, Plain]
    assert [part.text for part in chain] == ["前面文字。", "只朗读这一句", "后面文字。"]
    assert calls == ["只朗读这一句"]
    await plugin.release_speech_audio(event)
    # Failed synthesis keeps the chosen words at their original position.
    monkeypatch.setattr(
        plugin.service.client,
        "synthesize",
        AsyncMock(side_effect=RuntimeError("private-key-test")),
    )
    event = event_factory()
    event.set_result(
        MessageEventResult(
            chain=[Plain(raw)], result_content_type=ResultContentType.LLM_RESULT
        )
    )
    await plugin.auto_speak(event)
    assert all(isinstance(part, Plain) for part in event.get_result().chain)
    assert (
        "".join(part.text for part in event.get_result().chain)
        == "前面文字。只朗读这一句后面文字。"
    )
    assert "private-key-test" not in caplog.text


async def test_tool_and_random_coexist_deduplicate_and_isolate(
    speech_plugin, event_factory
):
    plugin, calls, _ = speech_plugin
    event = event_factory()
    outcomes = await asyncio.gather(
        plugin.breeze_tts(event, "你好"), plugin.breeze_tts(event, "<tts>你好。</tts>")
    )
    assert len(event.sent) == 1 and calls == ["你好"]
    assert any("already" in outcome for outcome in outcomes)
    event.set_result(
        MessageEventResult(
            chain=[Plain("<breeze_tts>你好</breeze_tts> 其余文字")],
            result_content_type=ResultContentType.LLM_RESULT,
        )
    )
    await plugin.auto_speak(event)
    assert calls == ["你好"]
    assert "<" not in event.get_result().get_plain_text()
    other = event_factory()
    await plugin.breeze_tts(other, "你好")
    assert len(other.sent) == 1 and calls == ["你好", "你好"]


@pytest.mark.parametrize("mode", ["none", "native", "splitter"])
@pytest.mark.parametrize("keep_text", [False, True])
@pytest.mark.parametrize("tagged", [False, True])
async def test_real_decorate_and_send_pipeline(
    speech_plugin, event_factory, run_model, mode, keep_text, tagged
):
    from astrbot.core.pipeline.respond.stage import RespondStage
    from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage

    plugin, calls, host = speech_plugin
    plugin.config["behavior"]["keep_text"] = keep_text
    event = event_factory()
    segmented = host["platform_settings"]["segmented_reply"]
    segmented.update(
        enable=mode == "native",
        interval="0,0",
        interval_method="random",
        split_mode="regex",
        regex=r".*?[。？！]+|.+$",
        content_cleanup_rule="",
    )
    host["platform_settings"]["forward_threshold"] = 0
    body = "开头第一句。第二句。<breeze_tts>中间语音</breeze_tts>结尾第一句。第二句。"
    if not tagged:
        body = body.replace("<breeze_tts>", "").replace("</breeze_tts>", "")
    response = LLMResponse(
        role="assistant",
        completion_text=body,
    )
    runner, provider = await run_model(event, [response])
    assert provider.text_chat.await_count == 1
    saved = str(dump_messages_with_checkpoints(runner.run_context.messages))
    assert "breezetts_turn" not in saved and "<breeze_tts>" not in saved
    event.set_result(
        MessageEventResult(
            chain=[Plain(response.completion_text)],
            result_content_type=ResultContentType.LLM_RESULT,
        )
    )
    if mode == "splitter":
        module = pytest.importorskip("data.plugins.astrbot_plugin_splitter.main")
        splitter = object.__new__(module.MessageSplitterPlugin)
        splitter.context = plugin.context
        splitter._is_simple_mode = splitter._is_advanced_mode = False
        splitter._is_pro_mode = True
        splitter._processing_locks = {}
        settings = {
            "split_regex": r"[。\n]+",
            "enable_reply": False,
            "enable_smart_reply": False,
            "enable_smart_split": False,
            "delay_strategy": "fixed",
            "fixed_delay": 0,
            "other_media_strategy": "单独",
        }
        splitter._get_cfg = lambda key, default=None: settings.get(key, default)

        async def send_split(_event, chain, *_args):
            await event.send(MessageEventResult(chain=chain))

        splitter._send_proactive_segment = AsyncMock(side_effect=send_split)
        await plugin.auto_speak(event)
        await splitter.on_decorating_result(event)
    pipeline = SimpleNamespace(
        astrbot_config=host, plugin_manager=SimpleNamespace(context=plugin.context)
    )
    decorate, respond = ResultDecorateStage(), RespondStage()
    await decorate.initialize(pipeline)
    await respond.initialize(pipeline)
    async for _ in decorate.process(event):
        pass
    await respond.process(event)
    delivered = [part for chain in event.sent for part in chain]
    assert sum(isinstance(part, Record) for part in delivered) == 1
    voice_index = next(
        index for index, part in enumerate(delivered) if isinstance(part, Record)
    )
    before = "".join(
        part.text for part in delivered[:voice_index] if isinstance(part, Plain)
    )
    after = "".join(
        part.text for part in delivered[voice_index + 1 :] if isinstance(part, Plain)
    )
    expected_before = "开头第一句。第二句。" if tagged else ""
    expected_after = (
        ("中间语音" if keep_text else "") + "结尾第一句。第二句。"
        if tagged
        else ("开头第一句。" if keep_text else "")
        + "第二句。中间语音结尾第一句。第二句。"
    )
    if mode == "splitter":
        expected_before = expected_before.replace("。", "")
        expected_after = expected_after.replace("。", "")
    assert before == expected_before
    assert after == expected_after
    assert calls == ["中间语音" if tagged else "开头第一句。"]
    assert not plugin.service.delivery_audio


async def test_agent_tool_execution_and_tag_followup_dedup(
    speech_plugin, event_factory, run_model
):
    plugin, calls, _ = speech_plugin
    event = event_factory()
    runner, provider = await run_model(
        event,
        [
            LLMResponse(
                role="assistant",
                tools_call_name=["breeze_tts", "breeze_tts"],
                tools_call_args=[{"text": "你好"}, {"text": "你好。"}],
                tools_call_ids=["call-1", "call-2"],
            ),
            LLMResponse(
                role="assistant",
                completion_text="<breeze_tts>你好</breeze_tts>其余内容保持文字",
            ),
        ],
    )
    assert provider.text_chat.await_count == 2
    assert calls == ["你好"] and len(event.sent) == 1
    final = runner.get_final_llm_resp()
    assert final.completion_text == "你好其余内容保持文字"
    event.set_result(
        MessageEventResult(
            chain=[Plain(final.completion_text)],
            result_content_type=ResultContentType.LLM_RESULT,
        )
    )
    await plugin.auto_speak(event)
    assert all(isinstance(part, Plain) for part in event.get_result().chain)
    assert calls == ["你好"]
    history = str(dump_messages_with_checkpoints(runner.run_context.messages))
    assert "<breeze_tts>" not in history and "breezetts_turn" not in history


async def test_tool_voice_and_caption_are_separate_messages(
    speech_plugin, event_factory
):
    plugin, calls, _ = speech_plugin
    plugin.config["behavior"]["keep_text"] = True
    event = event_factory()
    outcome = await plugin.breeze_tts(event, "单独语音")
    assert "sent" in outcome and calls == ["单独语音"]
    assert len(event.sent) == 2
    assert len(event.sent[0]) == 1 and isinstance(event.sent[0][0], Record)
    assert len(event.sent[1]) == 1 and isinstance(event.sent[1][0], Plain)


async def test_agent_plain_turn_preserves_component_boundaries(
    speech_plugin, event_factory, run_model
):
    plugin, calls, _ = speech_plugin
    plugin.config["behavior"].update(auto_tts=False, tool_enabled=False)
    event = event_factory()
    runner, _ = await run_model(
        event, [LLMResponse(role="assistant", completion_text="纯文字回复")]
    )
    assert runner.get_final_llm_resp().completion_text == "纯文字回复"
    first, second = Plain("第一段"), Plain("第二段")
    event.set_result(
        MessageEventResult(
            chain=[first, second], result_content_type=ResultContentType.LLM_RESULT
        )
    )
    await plugin.auto_speak(event)
    assert event.get_result().chain == [first, second] and calls == []


async def test_boundaries_components_and_one_tag_per_turn(speech_plugin, event_factory):
    plugin, calls, _ = speech_plugin
    event = event_factory()
    image = Image.fromURL("https://example.test/picture.png")
    event.set_result(
        MessageEventResult(
            chain=[
                Plain("前<tts>第一"),
                Plain("句</tts>"),
                image,
                Plain("<tts>第二句</tts>后"),
            ],
            result_content_type=ResultContentType.LLM_RESULT,
        )
    )
    await plugin.auto_speak(event)
    chain = event.get_result().chain
    assert calls == ["第一句"] and chain[2] is image
    assert chain[3].text == "第二句" and chain[4].text == "后"
    await plugin.breeze_tts(event, "另一次调用")
    assert calls == ["第一句"]


async def test_disabled_and_over_limit_tags_are_clean_text(
    speech_plugin, event_factory
):
    plugin, calls, _ = speech_plugin
    plugin.config["behavior"].update(auto_tts=False, tool_enabled=False)
    event = event_factory()
    event.set_result(
        MessageEventResult(
            chain=[Plain("<tts>文字</tts><tts>未闭合")],
            result_content_type=ResultContentType.LLM_RESULT,
        )
    )
    await plugin.auto_speak(event)
    assert (
        "".join(part.text for part in event.get_result().chain) == "文字未闭合"
        and calls == []
    )
    plugin.config["behavior"].update(
        auto_tts=False, tool_enabled=True, max_characters=1
    )
    event = event_factory()
    event.set_result(
        MessageEventResult(
            chain=[Plain("<tts>超过上限</tts>")],
            result_content_type=ResultContentType.LLM_RESULT,
        )
    )
    await plugin.auto_speak(event)
    assert event.get_result().get_plain_text() == "超过上限" and calls == []


@pytest.mark.parametrize(
    "message_type,expected",
    [
        (MessageType.FRIEND_MESSAGE, True),
        (MessageType.GROUP_MESSAGE, False),
        (MessageType.OTHER_MESSAGE, False),
    ],
)
async def test_private_group_probability_and_default(
    speech_plugin, event_factory, message_type, expected
):
    plugin, _, _ = speech_plugin
    plugin.config["behavior"].update(
        trigger_probability=20,
        private_trigger_probability=70,
        group_trigger_probability=5,
    )
    event = event_factory(message_type=message_type)
    with patch("random.random", return_value=0.4) as draw:
        await plugin.prepare_speech(event)
        request = ProviderRequest()
        await plugin.prepare_speech_request(event, request)
        await plugin.prepare_speech_request(event, request)
    assert draw.call_count == 1
    assert plugin.speech.turn(event).random_selected is expected
    assert request.extra_user_content_parts == []


@pytest.mark.parametrize("probability,selected", [(0, False), (100, True)])
async def test_scope_probability_boundaries_do_not_draw(
    speech_plugin, event_factory, probability, selected
):
    plugin, _, _ = speech_plugin
    plugin.config["behavior"]["private_trigger_probability"] = probability
    with patch("random.random") as draw:
        state = plugin.speech.turn(event_factory())
    assert state.random_selected is selected
    draw.assert_not_called()


async def test_cooldown_shared_by_tool_and_random_preserves_text(
    speech_plugin, event_factory
):
    plugin, calls, _ = speech_plugin
    plugin.config["behavior"]["min_interval_seconds"] = 90
    first = event_factory()
    assert "sent" in await plugin.breeze_tts(first, "第一句")
    # Distinct passages in the same turn remain available to the model.
    assert "sent" in await plugin.breeze_tts(first, "第二句")
    second = event_factory()
    request = ProviderRequest(system_prompt="Stable persona")
    await plugin.prepare_speech_request(second, request)
    assert (
        request.extra_user_content_parts == []
        and request.system_prompt == "Stable persona"
    )
    assert "cooling down" in await plugin.breeze_tts(second, "第三句")
    second.set_result(
        MessageEventResult(
            chain=[Plain("前<tts>第三句</tts>后")],
            result_content_type=ResultContentType.LLM_RESULT,
        )
    )
    await plugin.auto_speak(second)
    assert all(isinstance(part, Plain) for part in second.get_result().chain)
    assert "".join(part.text for part in second.get_result().chain) == "前第三句后"
    assert calls == ["第一句", "第二句"]
    other = event_factory(session="other-session")
    assert "sent" in await plugin.breeze_tts(other, "独立会话")
    assert calls[-1] == "独立会话"


async def test_pending_turn_blocks_concurrent_generation_and_cancellation_releases(
    speech_plugin, event_factory, monkeypatch
):
    plugin, calls, _ = speech_plugin
    plugin.config["behavior"]["min_interval_seconds"] = 90
    original = plugin.service.synthesize
    entered = asyncio.Event()

    async def waiting(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(plugin.service, "synthesize", waiting)
    task = asyncio.create_task(plugin.breeze_tts(event_factory(), "处理中"))
    await entered.wait()
    assert "cooling down" in await plugin.breeze_tts(event_factory(), "另一轮")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert plugin.speech.frequency.sessions == {} and calls == []
    monkeypatch.setattr(plugin.service, "synthesize", original)
    assert "sent" in await plugin.breeze_tts(event_factory(), "恢复发送")
    assert calls == ["恢复发送"]


async def test_failed_synthesis_allows_next_turn(
    speech_plugin, event_factory, monkeypatch
):
    plugin, calls, _ = speech_plugin
    plugin.config["behavior"]["min_interval_seconds"] = 90
    original = plugin.service.synthesize
    monkeypatch.setattr(
        plugin.service,
        "synthesize",
        AsyncMock(side_effect=RuntimeError("isolated failure")),
    )
    assert "failed" in await plugin.breeze_tts(event_factory(), "失败")
    assert plugin.speech.frequency.sessions == {}
    monkeypatch.setattr(plugin.service, "synthesize", original)
    assert "sent" in await plugin.breeze_tts(event_factory(), "恢复")
    assert calls == ["恢复"]


async def test_random_generation_cools_down_tool_calls(speech_plugin, event_factory):
    plugin, calls, _ = speech_plugin
    plugin.config["behavior"]["min_interval_seconds"] = 90
    event = event_factory()
    event.set_result(
        MessageEventResult(
            chain=[Plain("前<breeze_tts>随机片段</breeze_tts>后")],
            result_content_type=ResultContentType.LLM_RESULT,
        )
    )
    await plugin.auto_speak(event)
    assert sum(isinstance(part, Record) for part in event.get_result().chain) == 1
    assert "cooling down" in await plugin.breeze_tts(event_factory(), "工具片段")
    assert calls == ["随机片段"]
    await plugin.release_speech_audio(event)


async def test_platform_failure_keeps_generated_audio_cooldown(
    speech_plugin, event_factory
):
    plugin, calls, _ = speech_plugin
    plugin.config["behavior"]["min_interval_seconds"] = 90
    event = event_factory()
    event.send = AsyncMock(side_effect=RuntimeError("isolated platform failure"))
    assert "delivery failed" in await plugin.breeze_tts(event, "已生成")
    assert "cooling down" in await plugin.breeze_tts(event_factory(), "立即重试")
    assert calls == ["已生成"]


async def test_explicit_command_can_speak_during_cooldown(speech_plugin, event_factory):
    plugin, calls, _ = speech_plugin
    plugin.config["behavior"]["min_interval_seconds"] = 90
    await plugin.breeze_tts(event_factory(), "自动语音")
    event = event_factory("breezetts 手动请求")
    results = [result async for result in plugin.speak(event)]
    assert isinstance(results[0].chain[0], Record)
    assert calls == ["自动语音", "手动请求"]
    await plugin.release_speech_audio(event)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("发个语音来", True),
        ("用语音回复我", True),
        ("想听听你的声音", True),
        ("Please send a voice message", True),
        ("Please read this aloud", True),
        ("请用语音读这句：不要发语音", True),
        ("不要发语音", False),
        ("请不要发语音", False),
        ("Don't speak this time", False),
        ("语音识别怎么设置", False),
        ("今天心情不错", False),
    ],
)
async def test_explicit_voice_hint_is_temporary_and_request_scoped(
    speech_plugin, event_factory, text, expected
):
    plugin, _, _ = speech_plugin
    plugin.config["behavior"]["auto_tts"] = False
    tool = FunctionTool(name="breeze_tts", description="Voice", parameters={})
    request = ProviderRequest(
        prompt=text,
        system_prompt="Stable persona",
        contexts=[{"role": "assistant", "content": "History"}],
        func_tool=ToolSet(tools=[tool]),
    )
    event = event_factory(text)
    await plugin.prepare_speech_request(event, request)
    await plugin.prepare_speech_request(event, request)
    assert len(request.extra_user_content_parts) == int(expected)
    assert request.system_prompt == "Stable persona"
    assert request.contexts == [{"role": "assistant", "content": "History"}]
    if expected:
        assert request.extra_user_content_parts[0]._no_save
    dumped = dump_messages_with_checkpoints(
        [
            Message(
                role="user",
                content=[TextPart(text=text), *request.extra_user_content_parts],
            )
        ]
    )
    assert dumped[0]["content"] == [{"type": "text", "text": text}]
    await plugin.prepare_speech_request(event_factory("普通文字"), request)
    assert request.extra_user_content_parts == []


@pytest.mark.parametrize(
    "text",
    [
        "不要发语音，文字回复就行",
        "这一条请不要用语音回复，文字就好",
        "Reply normally; do not use voice",
    ],
)
async def test_voice_opt_out_suppresses_random_and_tool_for_one_turn(
    speech_plugin, event_factory, text
):
    plugin, calls, _ = speech_plugin
    event = event_factory(text)
    tool = FunctionTool(name="breeze_tts", description="Voice", parameters={})
    tools = ToolSet(tools=[tool])
    request = ProviderRequest(func_tool=tools)
    await plugin.prepare_speech_request(event, request)
    assert request.extra_user_content_parts == []
    assert request.func_tool is tools
    assert tools.get_tool("breeze_tts") is tool
    event.set_result(
        MessageEventResult(
            chain=[Plain("<tts>保留文字</tts>")],
            result_content_type=ResultContentType.LLM_RESULT,
        )
    )
    await plugin.auto_speak(event)
    assert calls == [] and event.get_result().get_plain_text() == "保留文字"
    assert "disabled" in await plugin.breeze_tts(event, "旧工具调用")
    assert calls == []
    assert plugin.speech.turn(event_factory("发个语音")).autonomous


@pytest.mark.parametrize("keep_text", [False, True])
async def test_explicit_voice_agent_reports_delivery_and_finishes_once(
    speech_plugin, event_factory, run_model, keep_text
):
    plugin, calls, _ = speech_plugin
    plugin.config["behavior"].update(auto_tts=False, keep_text=keep_text)
    event = event_factory("发个语音来")
    runner, _ = await run_model(
        event,
        [
            LLMResponse(
                role="assistant",
                tools_call_name=["breeze_tts"],
                tools_call_args=[{"text": "听得到我的声音吗"}],
                tools_call_ids=["voice-1"],
            ),
            LLMResponse(role="assistant", completion_text=""),
        ],
    )
    assert calls == ["听得到我的声音吗"]
    assert len(event.sent) == 1 + int(keep_text)
    assert isinstance(event.sent[0][0], Record)
    history = dump_messages_with_checkpoints(runner.run_context.messages)
    result = next(
        message["content"] for message in history if message["role"] == "tool"
    )
    receipt = json.loads(result)
    assert receipt["status"] == "sent"
    assert receipt["caption_sent"] is keep_text
    assert "breezetts_turn" not in str(history)


async def test_explicit_hint_requires_an_exposed_speech_tool(
    speech_plugin, event_factory
):
    plugin, _, _ = speech_plugin
    plugin.config["behavior"]["auto_tts"] = False
    request = ProviderRequest(func_tool=ToolSet(tools=[]))
    await plugin.prepare_speech_request(event_factory("发个语音来"), request)
    assert request.extra_user_content_parts == []


async def test_program_probability_converts_plain_replies_without_model_markers(
    speech_plugin, event_factory
):
    plugin, calls, _ = speech_plugin
    plugin.config["behavior"].update(trigger_probability=25, tool_enabled=False)
    with patch("random.random", side_effect=[0.01, 0.24, 0.25, 0.99]) as draw:
        for index in range(4):
            event = event_factory()
            request = ProviderRequest(system_prompt="Stable persona")
            await plugin.prepare_speech(event)
            await plugin.prepare_speech_request(event, request)
            assert request.extra_user_content_parts == []
            event.set_result(
                MessageEventResult(
                    chain=[Plain(f"第{index}轮普通回复。其余内容。")],
                    result_content_type=ResultContentType.LLM_RESULT,
                )
            )
            await plugin.auto_speak(event)
            await plugin.auto_speak(event)
            assert sum(
                isinstance(part, Record) for part in event.get_result().chain
            ) == int(index < 2)
            await plugin.release_speech_audio(event)
    assert draw.call_count == 4
    assert calls == ["第0轮普通回复。", "第1轮普通回复。"]


async def test_random_selection_spans_plain_components_and_preserves_media(
    speech_plugin, event_factory
):
    plugin, calls, _ = speech_plugin
    event = event_factory()
    image = Image.fromURL("https://example.com/image.png")
    tail = Plain("后面的另一段文字。")
    event.set_result(
        MessageEventResult(
            chain=[Plain("你"), Plain("好呀。其余文字。"), image, tail],
            result_content_type=ResultContentType.LLM_RESULT,
        )
    )
    await plugin.auto_speak(event)
    chain = event.get_result().chain
    assert calls == ["你好呀。"]
    assert isinstance(chain[0], Record) and chain[1].text == "其余文字。"
    assert chain[2] is image and chain[3] is tail
    assert "already" in await plugin.breeze_tts(event, "再次调用")
    assert calls == ["你好呀。"]
    await plugin.release_speech_audio(event)


@pytest.mark.parametrize("tool_first", [True, False])
async def test_tool_and_program_random_share_one_voice_path(
    speech_plugin, event_factory, tool_first
):
    plugin, calls, _ = speech_plugin
    event = event_factory()
    event.set_result(
        MessageEventResult(
            chain=[Plain("随机片段。剩下文字。")],
            result_content_type=ResultContentType.LLM_RESULT,
        )
    )
    if tool_first:
        await plugin.breeze_tts(event, "自主片段")
        await plugin.auto_speak(event)
        assert calls == ["自主片段"]
    else:
        await plugin.auto_speak(event)
        assert "already" in await plugin.breeze_tts(event, "自主片段")
        assert calls == ["随机片段。"]
    await plugin.release_speech_audio(event)


async def test_probability_miss_still_allows_autonomous_tool(
    speech_plugin, event_factory
):
    plugin, calls, _ = speech_plugin
    plugin.config["behavior"]["trigger_probability"] = 0
    event = event_factory()
    assert not plugin.speech.turn(event).random_selected
    assert json.loads(await plugin.breeze_tts(event, "自主语音"))["status"] == "sent"
    assert calls == ["自主语音"]


@pytest.mark.parametrize("segmented", [False, True])
@pytest.mark.parametrize("status", ["（语音已发送）", "Voice message sent."])
async def test_delivered_voice_removes_status_and_empty_quote_bubbles(
    speech_plugin, event_factory, run_model, segmented, status
):
    from astrbot.core.pipeline.respond.stage import RespondStage
    from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage

    plugin, calls, host = speech_plugin
    event = event_factory("发个语音来")
    runner, _ = await run_model(
        event,
        [
            LLMResponse(
                role="assistant",
                tools_call_name=["breeze_tts"],
                tools_call_args=[{"text": "你好呀"}],
                tools_call_ids=["voice-status"],
            ),
            LLMResponse(role="assistant", completion_text=status),
        ],
    )
    assert runner.get_final_llm_resp().completion_text == ""
    history = str(dump_messages_with_checkpoints(runner.run_context.messages))
    assert status not in history
    assert calls == ["你好呀"] and len(event.sent) == 1
    # A downstream decorator may reintroduce the model's original status text.
    event.set_result(
        MessageEventResult(
            chain=[Reply(id="source-message"), At(qq="user"), Plain(status)],
            result_content_type=ResultContentType.LLM_RESULT,
        )
    )
    host["platform_settings"].update(reply_with_quote=True, reply_with_mention=True)
    host["platform_settings"]["segmented_reply"].update(
        enable=segmented, interval="0,0"
    )
    pipeline = SimpleNamespace(
        astrbot_config=host, plugin_manager=SimpleNamespace(context=plugin.context)
    )
    decorate, respond = ResultDecorateStage(), RespondStage()
    await decorate.initialize(pipeline)
    await respond.initialize(pipeline)
    async for _ in decorate.process(event):
        pass
    await respond.process(event)
    assert len(event.sent) == 1


async def test_delivery_status_cleanup_preserves_new_dialogue_and_prior_history(
    speech_plugin, event_factory
):
    plugin, _, _ = speech_plugin
    event = event_factory()
    await plugin.breeze_tts(event, "你好")
    response = LLMResponse(
        role="assistant", completion_text="（语音已发送）今晚想聊什么？"
    )
    await plugin.clean_speech_response(event, response)
    assert response.completion_text == "今晚想聊什么？"
    previous = Message(role="assistant", content="语音已发送")
    current = Message(
        role="assistant", content=[TextPart(text="（语音已"), TextPart(text="发送）")]
    )
    await plugin.clean_speech_history(
        event, SimpleNamespace(messages=[previous, current]), response
    )
    assert previous.content == "语音已发送"
    assert not "".join(part.text for part in current.content).strip()


async def test_model_status_is_removed_without_voice_delivery(
    speech_plugin, event_factory
):
    plugin, _, _ = speech_plugin
    event = event_factory()
    response = LLMResponse(role="assistant", completion_text="（语音已发送）")
    await plugin.clean_speech_response(event, response)
    assert response.completion_text == ""


@pytest.mark.parametrize("mode", ["off", "native", "splitter"])
@pytest.mark.parametrize("keep_text", [False, True])
@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("forward_threshold", [100000, 10])
async def test_onebot_random_voice_is_standalone_with_live_splitter_defaults(
    speech_plugin, mode, keep_text, group, forward_threshold
):
    """Check serialized adapter payloads rather than flattened component counts."""
    from astrbot.core.pipeline.respond.stage import RespondStage
    from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
        AiocqhttpMessageEvent,
    )

    plugin, calls, host = speech_plugin
    plugin.config["behavior"]["keep_text"] = keep_text
    host["platform_settings"]["segmented_reply"].update(
        enable=mode == "native",
        interval="0,0",
        interval_method="random",
        split_mode="regex",
        regex=r".*?[。？！]+|.+$",
        content_cleanup_rule="",
    )
    host["platform_settings"]["forward_threshold"] = forward_threshold
    payloads = []

    async def capture(**kwargs):
        payloads.append(kwargs["message"])
        return {"message_id": len(payloads)}

    async def capture_forward(action, **kwargs):
        assert action in {"send_group_forward_msg", "send_private_forward_msg"}
        for node in kwargs["messages"]:
            content = node["data"]["content"]
            assert not any(c["type"] == "record" for c in content)
            payloads.append(content)
        return {"message_id": len(payloads)}

    bot = SimpleNamespace(
        send_private_msg=AsyncMock(side_effect=capture),
        send_group_msg=AsyncMock(side_effect=capture),
        call_action=AsyncMock(side_effect=capture_forward),
    )
    incoming = AstrBotMessage()
    incoming.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
    incoming.sender = MessageMember("123", "User")
    incoming.group_id = "456" if group else ""
    incoming.message_id = "789"
    incoming.message = [Plain("何意味")]
    incoming.message_str = "何意味"
    incoming.self_id = "999"
    incoming.raw_message = {}
    target = "456" if group else "123"
    event = AiocqhttpMessageEvent(
        incoming.message_str,
        incoming,
        PlatformMetadata("aiocqhttp", "test", "test"),
        target,
        bot,
    )
    event.plugins_name = ["astrbot_plugin_breezetts"]
    first = "今天的午饭吃得怎么样呀？"
    rest = "我们可以慢慢聊，你想从哪件事说起呢～"
    event.set_result(
        MessageEventResult(
            chain=[Plain(first + rest)],
            result_content_type=ResultContentType.LLM_RESULT,
        )
    )

    if mode == "splitter":
        module = pytest.importorskip("data.plugins.astrbot_plugin_splitter.main")

        async def send_to_adapter(_session, chain):
            await AiocqhttpMessageEvent.send_message(bot, chain, None, group, target)
            return True

        platform = SimpleNamespace(send_by_session=send_to_adapter)
        plugin.context.get_platform_inst = lambda _id: platform

        async def send_to_context(session, chain):
            return await platform.send_by_session(session, chain)

        plugin.context.send_message = send_to_context
        splitter = module.MessageSplitterPlugin(
            plugin.context,
            {"basic_settings": {"enable_tts_for_segments": False}},
        )
        splitter._simple_overrides.update(delay_strategy="fixed", fixed_delay=0)
        await plugin.auto_speak(event)
        await splitter.on_decorating_result(event)

    pipeline = SimpleNamespace(
        astrbot_config=host, plugin_manager=SimpleNamespace(context=plugin.context)
    )
    decorate, respond = ResultDecorateStage(), RespondStage()
    await decorate.initialize(pipeline)
    await respond.initialize(pipeline)
    async for _ in decorate.process(event):
        pass
    await respond.process(event)

    voices = [p for p in payloads if any(c["type"] == "record" for c in p)]
    assert len(voices) == 1
    assert len(voices[0]) == 1
    assert voices[0][0]["data"]["file"].startswith("base64://UklGR")
    captions = "".join(
        c["data"]["text"] for p in payloads for c in p if c["type"] == "text"
    )
    assert captions == (first if keep_text else "") + rest
    assert "[语音]" not in captions
    assert calls == [first]
    assert not plugin.service.delivery_audio


@pytest.mark.parametrize(
    "failure", [RuntimeError("send failed"), asyncio.CancelledError(), False]
)
async def test_delivery_guard_releases_audio_on_failure_without_replay(
    speech_plugin, event_factory, failure
):
    plugin, _, _ = speech_plugin
    event = event_factory()
    event.set_result(
        MessageEventResult(
            chain=[Plain("第一句说给你听。后面是普通文字。")],
            result_content_type=ResultContentType.LLM_RESULT,
        )
    )
    await plugin.auto_speak(event)
    chain = event.get_result()
    original = list(chain.chain)
    sender = (
        AsyncMock(side_effect=failure)
        if isinstance(failure, BaseException)
        else AsyncMock(return_value=failure)
    )
    expected = type(failure) if isinstance(failure, BaseException) else RuntimeError
    with pytest.raises(expected):
        await plugin.speech.send_chain(sender, chain)
    assert sender.await_count == 1
    assert chain.chain == original
    assert not plugin.service.delivery_audio


async def test_platform_guard_covers_cached_context_senders_and_ignores_foreign_audio(
    speech_plugin, event_factory
):
    plugin, _, _ = speech_plugin
    original = AsyncMock(return_value=True)
    platform = SimpleNamespace(send_by_session=original)
    plugin.context.get_platform_inst = lambda _id: platform

    async def cached_sender(chain):
        return await platform.send_by_session("test:FriendMessage:123", chain)

    event = event_factory()
    event.set_result(
        MessageEventResult(
            chain=[Plain("第一句话有一点长，拿来朗读。后面还是普通文字。")],
            result_content_type=ResultContentType.LLM_RESULT,
        )
    )
    await plugin.auto_speak(event)
    record = next(c for c in event.get_result().chain if isinstance(c, Record))
    mixed = MessageEventResult(
        chain=[Reply(id="incoming"), At(qq="123"), record, Plain("保留的文字")],
        use_markdown_=False,
    )
    await cached_sender(mixed)
    delivered = [call.args[1] for call in original.await_args_list]
    assert len(delivered) == 2
    assert delivered[0].chain == [record]
    assert [type(c) for c in delivered[1].chain] == [Reply, At, Plain]
    assert all(chain.use_markdown_ is False for chain in delivered)
    assert not plugin.service.delivery_audio

    foreign = MessageEventResult(
        chain=[Record.fromFileSystem("another-plugin.wav"), Plain("another plugin")]
    )
    await cached_sender(foreign)
    assert original.await_args.args[1] is foreign
    plugin.speech.close_delivery()
    assert platform.send_by_session is original


async def test_delivery_guard_reload_preserves_outer_wrapper_and_reuses_owner(
    speech_plugin, event_factory
):
    plugin, _, _ = speech_plugin
    original = AsyncMock(return_value=True)
    platform = SimpleNamespace(send_by_session=original)
    plugin.context.get_platform_inst = lambda _id: platform
    event = event_factory()
    plugin.speech.bind_delivery(event)
    guard = platform.send_by_session

    async def outer(session, chain):
        return await guard(session, chain)

    platform.send_by_session = outer
    previous = plugin.speech
    previous.close_delivery()
    assert platform.send_by_session is outer
    replacement = type(previous)(plugin.service, plugin.logger, plugin.context)
    replacement.bind_delivery(event_factory())
    assert platform.send_by_session is outer
    assert platform._breezetts_delivery_guard["owner"] is replacement
    replacement.close_delivery()
    await platform.send_by_session(
        "test:FriendMessage:123", MessageEventResult(chain=[Plain("unchanged")])
    )
    original.assert_awaited_once()


async def test_caption_failure_after_voice_does_not_request_whole_message_retry(
    speech_plugin, event_factory
):
    plugin, _, _ = speech_plugin
    plugin.config["behavior"]["keep_text"] = True
    event = event_factory()
    event.set_result(
        MessageEventResult(
            chain=[Plain("这是一句完整的朗读内容。后面还有普通文字。")],
            result_content_type=ResultContentType.LLM_RESULT,
        )
    )
    await plugin.auto_speak(event)
    sender = AsyncMock(side_effect=[None, RuntimeError("caption rejected")])
    assert await plugin.speech.send_chain(sender, event.get_result()) is True
    assert sender.await_count == 2
    assert len(sender.await_args_list[0].args[0].chain) == 1
    assert isinstance(sender.await_args_list[0].args[0].chain[0], Record)
    assert not plugin.service.delivery_audio


async def test_quoted_voice_request_does_not_inject_current_turn_hint(
    speech_plugin, event_factory
):
    plugin, _, _ = speech_plugin
    event = event_factory("这句话你怎么看")
    event.message_str = "[引用消息：发条语音给我听] 这句话你怎么看"
    tool = FunctionTool(name="breeze_tts", description="Voice", parameters={})
    req = ProviderRequest(
        system_prompt="Stable persona", func_tool=ToolSet(tools=[tool])
    )
    await plugin.prepare_speech_request(event, req)
    assert not req.extra_user_content_parts
    assert req.system_prompt == "Stable persona"


@pytest.mark.parametrize("stale_hint", [False, True])
async def test_guidance_runs_after_late_tool_scope_filter(
    speech_plugin, event_factory, monkeypatch, stale_hint
):
    from astrbot.core.pipeline import context_utils
    from astrbot.core.star.star_handler import (
        EventType,
        StarHandlerMetadata,
        StarHandlerRegistry,
        star_handlers_registry,
    )

    plugin, _, _ = speech_plugin
    event = event_factory("发条语音给我听")
    tool = FunctionTool(name="breeze_tts", description="Voice", parameters={})
    original_tools = ToolSet(tools=[tool])
    req = ProviderRequest(
        prompt="Original user text",
        system_prompt="Stable persona",
        contexts=[{"role": "assistant", "content": "History"}],
        func_tool=original_tools,
    )
    if stale_hint:
        await plugin.prepare_speech_request(event, req)
        assert len(req.extra_user_content_parts) == 1

    registered = next(
        handler
        for handler in star_handlers_registry.get_handlers_by_event_type(
            EventType.OnLLMRequestEvent
        )
        if handler.handler_name == "prepare_speech_request"
    )

    async def filter_tools(_event, request):
        request.func_tool = ToolSet(
            tools=[
                item for item in request.func_tool.tools if item.name != "breeze_tts"
            ]
        )

    registry = StarHandlerRegistry()
    registry.append(registered)
    registry.append(
        StarHandlerMetadata(
            event_type=EventType.OnLLMRequestEvent,
            handler_full_name="test_late_tool_scope",
            handler_name="late_tool_scope",
            handler_module_path=registered.handler_module_path,
            handler=filter_tools,
            event_filters=[],
            extras_configs={"priority": -250000},
        )
    )
    monkeypatch.setattr(context_utils, "star_handlers_registry", registry)
    await context_utils.call_event_hook(event, EventType.OnLLMRequestEvent, req)
    assert req.func_tool.get_tool("breeze_tts") is None
    assert not req.extra_user_content_parts
    assert req.prompt == "Original user text"
    assert req.system_prompt == "Stable persona"
    assert req.contexts == [{"role": "assistant", "content": "History"}]
    assert original_tools.get_tool("breeze_tts") is tool
