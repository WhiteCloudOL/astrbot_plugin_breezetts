"""AstrBot entrypoint for BreezeTTS commands, tools, hooks, and views."""

from __future__ import annotations

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain, Record
from astrbot.api.star import Context, Star, StarTools

from .service import BreezeService
from .settings import BreezeError
from .web import WorkbenchAPI

PLUGIN_NAME = "astrbot_plugin_breezetts"


class BreezeTTS(Star):
    """Integrate Breeze speech synthesis into AstrBot's plugin lifecycle."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.service = BreezeService(
            config, StarTools.get_data_dir(PLUGIN_NAME), self.logger
        )
        self.web = WorkbenchAPI(self.service)
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
            item = await self.service.synthesize(parts[1])
            assert self.service.store is not None
            yield event.chain_result(
                [
                    Record.fromFileSystem(
                        self.service.store.path(item["id"]), text=parts[1]
                    )
                ]
            )
        except BreezeError as exc:
            yield event.plain_result(str(exc))

    @filter.llm_tool(name="breeze_tts")
    async def breeze_tts(self, event: AstrMessageEvent, text: str):
        """Send a spoken voice message to the current conversation when requested.

        Args:
            text(string): The exact text to speak, up to the configured character limit.
        """
        if not self.service.settings()["behavior"]["tool_enabled"]:
            return "BreezeTTS tool is disabled."
        try:
            item = await self.service.synthesize(text)
            assert self.service.store is not None
            await event.send(
                MessageChain(
                    [
                        Record.fromFileSystem(
                            self.service.store.path(item["id"]), text=text
                        )
                    ]
                )
            )
            event.set_extra("breezetts_voice_sent", True)
            return "Voice message sent. Do not repeat the spoken text."
        except BreezeError as exc:
            return str(exc)

    @filter.on_decorating_result()
    async def auto_speak(self, event: AstrMessageEvent):
        """Attach speech to completed LLM replies while retaining text on failure.

        Args:
            event: Event whose final non-streaming LLM result is being decorated.
        """
        config = self.service.settings()
        result = event.get_result()
        if (
            not config["behavior"]["auto_tts"]
            or event.get_extra("breezetts_voice_sent", False)
            or result is None
            or not result.is_llm_result()
            or any(isinstance(part, Record) for part in result.chain)
        ):
            return
        text = "\n".join(
            part.text for part in result.chain if isinstance(part, Plain)
        ).strip()
        if not text or len(text) > config["behavior"]["max_characters"]:
            return
        try:
            item = await self.service.synthesize(text, config=config)
            assert self.service.store is not None
            record = Record.fromFileSystem(
                self.service.store.path(item["id"]), text=text
            )
            if not config["behavior"]["keep_text"]:
                result.chain[:] = [
                    part for part in result.chain if not isinstance(part, Plain)
                ]
            result.chain.append(record)
        except BreezeError:
            self.logger.warning(
                "Breeze automatic speech failed; retaining the text reply."
            )
