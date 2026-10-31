"""AstrBot entrypoint for BreezeTTS commands, tools, hooks, and views."""

from __future__ import annotations

import re
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, Record
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import TextPart
from astrbot.core.agent.tool import ToolSet

from .service import BreezeService
from .settings import BreezeError
from .speech import TURN_KEY, SpeechDelivery
from .speech_markup import parse_speech
from .web import WorkbenchAPI

PLUGIN_NAME = "astrbot_plugin_breezetts"


class BreezeTTS(Star):
    """Integrate Breeze speech synthesis into AstrBot's plugin lifecycle."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.logger = getattr(self, "logger", logger)
        self.config = config
        self.service = BreezeService(
            config, StarTools.get_data_dir(PLUGIN_NAME), self.logger
        )
        self.web = WorkbenchAPI(self.service)
        self.speech = SpeechDelivery(self.service, self.logger)
        for endpoint, handler, methods in (
            ("state", self.web.state, ["GET"]),
            ("settings", self.web.settings, ["POST"]),
            ("connection", self.web.connection, ["POST"]),
            ("voices", self.web.voices, ["GET"]),
            ("jobs/submit", self.web.submit, ["POST"]),
            ("jobs/status", self.web.job, ["GET"]),
            ("voices/save-preview", self.web.save_preview, ["POST"]),
            ("audio/upload", self.web.upload, ["POST"]),
            ("audio/reference", self.web.reference, ["POST"]),
            ("audio/delete", self.web.delete_audio, ["POST"]),
            ("audio/download", self.web.download, ["GET"]),
            ("audio/chunk", self.web.audio_chunk, ["GET"]),
        ):
            context.register_web_api(
                f"/{PLUGIN_NAME}/{endpoint}", handler, methods, "BreezeTTS workbench"
            )

    async def initialize(self) -> None:
        """Acquire plugin resources after AstrBot has initialized the plugin."""
        await self.service.start()

    async def terminate(self) -> None:
        """Release plugin-owned jobs, HTTP connections, and route registrations."""
        await self.service.close()
        # Remove only this instance's handlers, preserving a replacement on reload.
        self.context.registered_web_apis[:] = [
            api
            for api in self.context.registered_web_apis
            if getattr(api[1], "__self__", None) is not self.web
        ]

    @filter.command("breezetts", alias={"btts", "语音合成"})
    async def speak(self, event: AstrMessageEvent):
        """Synthesize all text following the command, preserving spaces.

        Args:
            event: Incoming message containing the command and its full text.
        """
        parts = event.message_str.strip().split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result(
                "用法：/breezetts 想说的话\n请先在插件的 BreezeTTS 工作台配置服务和音色。"
            )
            return
        try:
            item = await self.service.synthesize(parts[1], for_delivery=True)
            self.speech.turn(event).audio_ids.add(item["id"])
            assert self.service.store is not None
            yield event.chain_result(
                [
                    Record.fromFileSystem(
                        self.service.store.path(item["id"]), text=parts[1]
                    )
                ]
            )
        except BreezeError as exc:
            self.logger.warning("Breeze command failed (code=%s).", exc.code)
            yield event.plain_result(str(exc))
        except Exception as exc:
            self.logger.error(
                "Breeze command failed unexpectedly (%s).", type(exc).__name__
            )
            yield event.plain_result("语音发送失败，请检查平台连接及音频格式。")

    @filter.llm_tool(name="breeze_tts")
    async def breeze_tts(self, event: AstrMessageEvent, text: str):
        """Synthesize and send a real voice message using BreezeTTS workbench settings.

        Choose only the passage to speak. Other content can remain ordinary text.
        This tool uses its own service and voice configuration. Its result reports
        actual delivery, including any configured text caption. Choose natural
        conversational content and describe speech availability from the tool result.
        After success, continue only with new content that has not been delivered.

        Args:
            text(string): The exact text to speak, up to the configured character limit.
        """
        return await self.speech.tool(event, text)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=12000)
    async def prepare_speech(self, event: AstrMessageEvent) -> None:
        """Select speech before the host snapshots its streaming setting.

        Args:
            event: Incoming event whose reply may contain a spoken passage.
        """
        state = self.speech.turn(event)
        if state.random_selected or state.autonomous:
            event.set_extra("enable_streaming", False)

    @filter.on_llm_request(priority=-100000)
    async def prepare_speech_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """Describe speech only for random selection or an explicit voice request.

        Args:
            event: Incoming event with its cached probability decision.
            req: Request whose stable system prompt and history stay unchanged.
        """
        state = self.speech.turn(event)
        if re.search(
            r"^\s*(?:请|这次|本次|这回|今天)?\s*(?:不要|别|不用|无需|不想|停止|关闭)"
            r"(?:再|给我|用|发|听|使用){0,2}(?:语音|朗读|声音)"
            r"|^\s*(?:please\s+)?(?:don't|do not|stop).{0,12}(?:speak|voice|audio|read)\b",
            event.message_str,
            re.I,
        ):
            state.random_selected = state.autonomous = False
        # Turn-local opt-outs keep the tool catalog stable and reject execution instead.
        if req.func_tool and not self.service.settings()["behavior"]["tool_enabled"]:
            req.func_tool = ToolSet(
                tools=[
                    tool for tool in req.func_tool.tools if tool.name != "breeze_tts"
                ]
            )
        marker = "<breezetts_turn>"
        req.extra_user_content_parts = [
            part
            for part in req.extra_user_content_parts
            if not (isinstance(part, TextPart) and part.text.startswith(marker))
        ]
        voice_requested = bool(
            state.autonomous
            and req.func_tool
            and req.func_tool.get_tool("breeze_tts")
            and re.search(
                r"(?:发|来)(?:个|条|段|一条|一段|一下)?语音|用语音|语音(?:回复|说|读)"
                r"|(?:想听|听听).{0,12}(?:声音|说话)|朗读|念给我听|读给我听"
                r"|\b(?:speak|read.{0,20}aloud|say.{0,20}aloud|send.{0,12}(?:voice|audio))\b",
                event.message_str,
                re.I,
            )
        )
        if (state.random_selected or voice_requested) and not state.source:
            instruction = (
                marker + "BreezeTTS 可通过工作台中的服务与音色配置合成并发送真实语音。"
                "当前语音能力和发送结果以 breeze_tts 工具返回为准，旧对话中关于未配置语音的说法不代表当前状态。"
                "请朗读自然的对话内容，成功发送后仅补充尚未发送的新内容。"
            )
            if voice_requested:
                instruction += (
                    "用户本轮明确请求语音，请调用 breeze_tts 工具发送选中的简短内容。"
                )
            else:
                instruction += (
                    "本轮可以用语音表达一小段适合朗读的内容。"
                    "请自主选择最多一段简短、自然的口语，用 <breeze_tts>正文</breeze_tts> 包裹，"
                    "其余内容保持普通文字；代码、网址和长篇列表保持文字。"
                    "若本轮无需语音，直接使用普通文字。"
                    "如果已调用 breeze_tts 工具发送语音，后续回复使用普通文字。"
                )
            req.extra_user_content_parts.append(
                TextPart(text=instruction + "</breezetts_turn>").mark_as_temp()
            )

    @filter.on_llm_response(priority=1100000)
    async def clean_speech_response(
        self, event: AstrMessageEvent, resp: LLMResponse
    ) -> None:
        """Retain spoken boundaries while presenting tag-free model text.

        Args:
            event: Event that carries the transient spoken-span plan.
            resp: Completed model response prior to history persistence.
        """
        if resp is not None and isinstance(resp.completion_text, str):
            state = self.speech.turn(event)
            raw = resp.completion_text
            if resp.result_chain and all(
                isinstance(part, Plain) for part in resp.result_chain.chain
            ):
                raw = "".join(part.text for part in resp.result_chain.chain)
            if raw != "".join(text for text, _ in state.plan):
                state.plan = parse_speech(raw)
            if resp.result_chain and any(
                not isinstance(part, Plain) for part in resp.result_chain.chain
            ):
                # The property setter moves all text before images and other media.
                for part in resp.result_chain.chain:
                    if isinstance(part, Plain):
                        part.text = "".join(text for text, _ in parse_speech(part.text))
            else:
                resp.completion_text = "".join(text for text, _ in state.plan)

    @filter.on_agent_done(priority=-100000)
    async def clean_speech_history(self, event, run_context, resp) -> None:
        """Clean the runner's copied assistant content before saving history.

        Args:
            event: Event owning the speech turn.
            run_context: Runner context containing assistant messages.
            resp: Final model response, already cleaned by the response hook.
        """
        for message in run_context.messages:
            if message.role != "assistant":
                continue
            if isinstance(message.content, str):
                message.content = "".join(
                    text for text, _ in parse_speech(message.content)
                )
            elif isinstance(message.content, list):
                for part in message.content:
                    if isinstance(part, TextPart):
                        part.text = "".join(text for text, _ in parse_speech(part.text))

    @filter.on_decorating_result(priority=500)
    async def auto_speak(self, event: AstrMessageEvent):
        """Convert selected spans before downstream message splitters run.

        Args:
            event: Event whose completed reply is being decorated.
        """
        await self.speech.decorate(event)

    @filter.on_decorating_result(priority=-(2**63))
    async def preserve_speech_order(self, event: AstrMessageEvent) -> None:
        """Send leading text before the host extracts standalone voice messages.

        Args:
            event: Decorated event after optional third-party splitting.
        """
        result = event.get_result()
        state = event.get_extra(TURN_KEY)
        if result is None or not result.chain or not state or not state.audio_ids:
            return
        segmented = (
            self.context.get_config(event.unified_msg_origin)
            .get("platform_settings", {})
            .get("segmented_reply", {})
        )
        if segmented.get("enable") and event.get_platform_name() not in {
            "qq_official_webhook",
            "weixin_official_account",
            "dingtalk",
        }:
            return
        for index, component in enumerate(result.chain):
            if (
                isinstance(component, Record)
                and component.path
                and Path(component.path).stem in state.audio_ids
            ):
                if index:
                    prefix = result.chain[:index]
                    try:
                        await event.send(result.derive(prefix))
                        del result.chain[:index]
                    except Exception as exc:
                        self.logger.error(
                            "Breeze leading text delivery failed (%s).",
                            type(exc).__name__,
                        )
                break

    @filter.after_message_sent()
    async def release_speech_audio(self, event: AstrMessageEvent) -> None:
        """Release reserved audio after the platform and splitter finish sending.

        Args:
            event: Event whose current message has finished delivery.
        """
        state = event.get_extra(TURN_KEY)
        if state and state.audio_ids:
            identifiers, state.audio_ids = state.audio_ids, set()
            await self.service.release_audio(identifiers)
