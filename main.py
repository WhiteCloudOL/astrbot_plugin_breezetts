"""AstrBot entrypoint for BreezeTTS commands, tools, hooks, and views."""

from __future__ import annotations

import re
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, AtAll, Plain, Record, Reply
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import TextPart
from astrbot.core.agent.tool import ToolSet
from astrbot.core.platform.message_type import MessageType

from .breeze_control import control_prompt
from .service import BreezeService
from .settings import BreezeError
from .speech import TURN_KEY, SpeechDelivery
from .speech_markup import clean_speech_bubble, strip_delivery_ack
from .web import WorkbenchAPI

PLUGIN_NAME = "astrbot_plugin_breezetts"


class BreezeTTS(Star):
    """Integrate Breeze speech synthesis into AstrBot's plugin lifecycle."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.logger = getattr(self, "logger", logger)
        self.config = config
        self.service = BreezeService(
            config, StarTools.get_data_dir(PLUGIN_NAME), self.logger, context
        )
        self.web = WorkbenchAPI(self.service)
        self.speech = SpeechDelivery(self.service, self.logger, context)
        for endpoint, handler, methods in (
            ("state", self.web.state, ["GET"]),
            ("settings", self.web.settings, ["POST"]),
            ("connection", self.web.connection, ["POST"]),
            ("voices", self.web.voices, ["GET"]),
            ("local/status", self.web.local_status, ["GET"]),
            ("local/voices", self.web.local_voices, ["GET"]),
            ("local/voice", self.web.local_voice, ["POST"]),
            ("jobs/submit", self.web.submit, ["POST"]),
            ("jobs/status", self.web.job, ["GET"]),
            ("jobs/cancel", self.web.cancel_job, ["POST"]),
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
        self.speech.close_delivery()
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
        audio_id = None
        try:
            item = await self.service.synthesize(parts[1], for_delivery=True)
            audio_id = item["id"]
            state = self.speech.turn(event)
            state.source = "command"
            assert self.service.store is not None
            caption = clean_speech_bubble(self.service.clean_text(parts[1]).strip())
            if self.service.settings()["behavior"]["keep_text"] and caption:
                yield event.plain_result(caption)
            # Caption delivery must not release the pending voice reservation.
            state.audio_ids.add(item["id"])
            yield event.chain_result(
                [
                    Record.fromFileSystem(
                        self.service.store.path(item["id"]),
                        text=self.service.clean_text(parts[1]),
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
        finally:
            if audio_id is not None:
                await self.service.release_audio({audio_id})

    @filter.llm_tool(name="breeze_tts")
    async def breeze_tts(
        self, event: AstrMessageEvent, text: str, instruction: str = ""
    ):
        """Send selected dialogue as a real voice message using the configured service.

        Use voice proactively for fitting conversation or emotion; respect text-only
        requests. text contains dialogue and optional controls, not JSON or analysis.
        With dynamic tone enabled, consider fitting emotion or rhythm; tags and
        instructions are optional. Fixed platforms need exact allowed styles. Breeze
        accepts free delivery via instruction or [语气:description], while native
        events use current catalog spellings. Prefer one overall tone for short
        replies; change sections only at natural boundaries. Use brief steady
        directions, not multi-step scripts or repeated vocal events. Put a section
        direction before its dialogue and events, never empty or trailing. Normal
        timing uses punctuation; pause tags are for deliberate pauses.

        Dynamic controls are ignored when disabled. Captions hide controls and precede
        voice in separate messages. The result is an internal receipt: after success,
        continue only with new dialogue, or return empty text if complete. Do not
        repeat delivered words or status, or retry a failed call this turn.

        Args:
            text(string): Exact spoken dialogue within the configured character limit, with optional supported controls when dynamic tone is enabled.
            instruction(string): Optional brief overall tone, one line without tags, up to 200 characters; fixed platforms require one exact style. Omit for natural narration. Ignored when dynamic tone is disabled.
        """
        return await self.speech.tool(event, text, instruction)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=12000)
    async def prepare_speech(self, event: AstrMessageEvent) -> None:
        """Select speech before the host snapshots its streaming setting.

        Args:
            event: Incoming event whose reply may contain a spoken passage.
        """
        state = self.speech.turn(event)
        if (
            state.random_selected
            or state.autonomous
            or self.service.settings()["behavior"]["dynamic_tone"]
        ):
            event.set_extra("enable_streaming", False)

    @filter.on_llm_request(priority=-(2**63))
    async def prepare_speech_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """Add temporary tool guidance for explicit voice requests.

        Args:
            event: Incoming event with its cached probability decision.
            req: Request whose stable system prompt and history stay unchanged.
        """
        state = self.speech.turn(event)
        config = self.service.settings()
        components = getattr(event.message_obj, "message", None)
        current_text = (
            "".join(part.text for part in components if isinstance(part, Plain))
            if components
            else event.message_str
        )
        if re.search(
            r"^\s*(?:(?:请|麻烦|(?:这|本)(?:一)?(?:条|次|回|轮)(?:回复)?|今天|接下来|你|我|先)\s*)*"
            r"(?:不要|别|不用|无需|不想|停止|关闭)"
            r"(?:再|给我|用|发|听|使用){0,2}(?:语音|朗读|声音)"
            r"|(?:^|[.;?!\n])\s*(?:please\s+)?(?:don't|do not|stop).{0,12}(?:speak|voice|audio|read)\b",
            current_text,
            re.I,
        ):
            if state.random_selected:
                self.logger.info(
                    "Breeze speech skipped (reason=user_opt_out, source=random)."
                )
            state.random_selected = state.autonomous = False
        # Turn-local opt-outs keep the tool catalog stable and reject execution instead.
        if req.func_tool and not config["behavior"]["tool_enabled"]:
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
                current_text,
                re.I,
            )
        )
        # Final tool filters have run; temporary guidance matches executable tools.
        tool_available = bool(
            state.autonomous and req.func_tool and req.func_tool.get_tool("breeze_tts")
        )
        random_dynamic = state.random_selected and config["behavior"]["dynamic_tone"]
        if (tool_available or random_dynamic) and not state.source:
            instruction = (
                marker + "BreezeTTS 可通过工作台中的服务与音色配置合成并发送真实语音。"
                "当前语音能力和发送结果以 breeze_tts 工具返回为准，旧对话中关于未配置语音的说法不代表当前状态。"
                "请朗读自然的对话内容，成功发送后仅补充尚未发送的新内容。"
                "发送结果是内部回执；保持角色对话，不回复发送状态或“（语音已发送）”之类的括号提示。"
            )
            if voice_requested:
                instruction += (
                    "用户本轮明确请求语音，请调用 breeze_tts 工具发送选中的简短内容。"
                )
            elif tool_available:
                instruction += (
                    "可按聊天语境主动调用 breeze_tts；普通文字回复不添加控制标记。"
                )
            if random_dynamic:
                instruction += "本轮已抽中概率语音，回复中的一段正文会自动合成；推荐按语境考虑局部语气和音频事件，也可自然朗读、不加标签，无需专门调用工具。"
            instruction += control_prompt(
                local=config["backend"] == "local",
                language=config["cloud"]["language_code"],
                prompt=config["behavior"]["speech_prompt"],
                profile=self.service.profile(config),
                user_message=current_text,
            )
            if not config["behavior"]["dynamic_tone"]:
                instruction += "\n当前动态语调关闭：使用默认声音，不添加语气或音频事件标记，不传 instruction。"
            req.extra_user_content_parts.append(
                TextPart(text=instruction + "</breezetts_turn>").mark_as_temp()
            )
            self.logger.debug(
                "Breeze speech guidance injected (tool=%s, random=%s, dynamic=%s, characters=%s).",
                tool_available,
                random_dynamic,
                config["behavior"]["dynamic_tone"],
                len(instruction),
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
            state.model_response = state.model_response or (
                resp.role == "assistant" and bool(raw.strip())
            )
            if state.model_response:
                self.speech.bind_delivery(event, clean_text=True)
            cleaned = strip_delivery_ack(raw)
            state.status_cleaned = state.status_cleaned or cleaned != raw
            raw = cleaned
            if raw != "".join(text for text, _ in state.plan):
                state.plan = self.speech.parse_speech(raw)
                state.speech_inputs = {
                    self.service.clean_text(text): text
                    for text, voice in reversed(
                        self.speech.parse_speech(raw, preserve_controls=True)
                    )
                    if self.service.clean_text(text) != text
                }
            if resp.result_chain and any(
                not isinstance(part, Plain) for part in resp.result_chain.chain
            ):
                # The property setter moves all text before images and other media.
                resp.result_chain.chain[:] = self.speech.clean_chain(
                    resp.result_chain.chain
                )
                for part in resp.result_chain.chain:
                    if isinstance(part, Plain):
                        part.text = strip_delivery_ack(part.text)
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
        last_assistant = next(
            (
                message
                for message in reversed(run_context.messages)
                if message.role == "assistant"
            ),
            None,
        )
        for message in run_context.messages:
            if message.role != "assistant":
                continue
            if isinstance(message.content, str):
                message.content = "".join(
                    text for text, _ in self.speech.parse_speech(message.content)
                )
                if message is last_assistant:
                    message.content = strip_delivery_ack(message.content)
            elif isinstance(message.content, list):
                if (
                    message is last_assistant
                    and all(isinstance(part, TextPart) for part in message.content)
                    and message.content
                ):
                    text = strip_delivery_ack(
                        "".join(part.text for part in message.content)
                    )
                    message.content[0].text = text
                    for part in message.content[1:]:
                        part.text = ""
                for part in message.content:
                    if isinstance(part, TextPart):
                        part.text = "".join(
                            text for text, _ in self.speech.parse_speech(part.text)
                        )
                        if message is last_assistant:
                            part.text = strip_delivery_ack(part.text)

    @filter.on_decorating_result(priority=500)
    async def auto_speak(self, event: AstrMessageEvent):
        """Convert selected spans before downstream message splitters run.

        Args:
            event: Event whose completed reply is being decorated.
        """
        await self.speech.decorate(event)

    @filter.on_decorating_result(priority=499)
    async def preserve_speech_order(self, event: AstrMessageEvent) -> None:
        """Deliver owned speech segments before downstream message splitters.

        Args:
            event: Event containing the selected speech and surrounding content.
        """
        result = event.get_result()
        state = event.get_extra(TURN_KEY)
        if result is not None and state and state.model_response:
            result.chain[:] = self.speech.clean_chain(result.chain)
        if result is None or not result.chain or not state or not state.audio_ids:
            return
        if state.source in {"random", "tags"} and any(
            isinstance(part, Reply) for part in result.chain
        ):
            # A later decorator may add a quote after speech selection.
            keep_voice = self.service.settings()["behavior"]["quote_keep_text"]
            for component in result.chain[:]:
                if not (
                    isinstance(component, Record)
                    and component.path
                    and Path(component.path).stem in state.audio_ids
                ):
                    continue
                if keep_voice:
                    index = result.chain.index(component)
                    if component.text and not any(
                        isinstance(part, Plain) and component.text in part.text
                        for part in result.chain[:index]
                    ):
                        result.chain.insert(index, Plain(component.text))
                    continue
                else:
                    self.logger.info(
                        "Breeze speech skipped (reason=late_quote_text_only, source=%s, audio_id=%s).",
                        state.source,
                        Path(component.path).stem,
                    )
                index = result.chain.index(component)
                if component.text and not any(
                    isinstance(part, Plain) and component.text in part.text
                    for part in result.chain
                ):
                    result.chain[index] = Plain(component.text)
                else:
                    result.chain.pop(index)
                await self.service.release_audio({Path(component.path).stem})
        if not any(
            isinstance(part, Plain)
            and part.text.strip()
            or not isinstance(part, (Plain, Record, Reply, At, AtAll))
            for part in result.chain
        ):
            # Voice-only replies have no text or media to carry routing headers.
            result.chain[:] = [
                part
                for part in result.chain
                if not isinstance(part, (Reply, At, AtAll))
            ]
        settings = self.context.get_config(event.unified_msg_origin).get(
            "platform_settings", {}
        )
        # The host adds these only to text/image-only chains, after this hook.
        if state.source in {"random", "tags"} and any(
            isinstance(part, Plain) and part.text.strip() for part in result.chain
        ):
            if (
                settings.get("reply_with_mention")
                and event.get_message_type() != MessageType.FRIEND_MESSAGE
                and not any(isinstance(part, At) for part in result.chain)
            ):
                result.chain.insert(
                    0, At(qq=event.get_sender_id(), name=event.get_sender_name())
                )
            if settings.get("reply_with_quote") and not any(
                isinstance(part, Reply) for part in result.chain
            ):
                result.chain.insert(0, Reply(id=event.message_obj.message_id))
        if not any(
            isinstance(part, Record)
            and part.path
            and Path(part.path).stem in state.audio_ids
            for part in result.chain
        ):
            return
        try:
            # The delivery guard partitions text, pure voice, and following content.
            # Downstream decorators receive an empty result after successful delivery.
            if await event.send(result.derive(list(result.chain))) is False:
                raise RuntimeError("Breeze speech segments were rejected by the sender")
        except Exception as exc:
            self.logger.error(
                "Breeze speech segment delivery failed (%s); retaining text.",
                type(exc).__name__,
            )
            fallback = []
            visible = "".join(
                part.text for part in result.chain if isinstance(part, Plain)
            )
            for part in result.chain:
                if (
                    isinstance(part, Record)
                    and part.path
                    and Path(part.path).stem in state.audio_ids
                ):
                    if part.text and part.text not in visible:
                        fallback.append(Plain(part.text))
                else:
                    fallback.append(part)
            result.chain[:] = fallback
            await self.service.release_audio(state.audio_ids)
            return
        result.chain.clear()

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
