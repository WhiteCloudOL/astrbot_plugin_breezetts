"""Select spoken spans and coordinate delivery within a single message event."""

from __future__ import annotations

import asyncio
import json
import random
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, AtAll, Plain, Record, Reply
from astrbot.core.platform.message_type import MessageType

from .settings import BreezeError
from .speech_markup import (
    ENCODED_TAG,
    TAG,
    parse_speech,
    select_spoken_span,
    strip_delivery_ack,
)
from .speech_policy import SpeechFrequency

TURN_KEY = "breezetts_speech_turn"
MAX_SPANS = 4


@dataclass
class SpeechTurn:
    """Keep selection, duplicate suppression, and file reservations event-local."""

    random_selected: bool
    autonomous: bool
    session: str = ""
    token: object = field(default_factory=object)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    attempted: set[str] = field(default_factory=set)
    source: str = ""
    audio_ids: set[str] = field(default_factory=set)
    plan: list[tuple[str, bool]] = field(default_factory=list)
    status_cleaned: bool = False


class SpeechDelivery:
    """Coordinate selective speech and owned audio at AstrBot delivery boundaries."""

    def __init__(self, service, logger, context):
        self.service = service
        self.logger = logger
        self.context = context
        self.frequency = SpeechFrequency()
        self.active = True
        self.delivery_guards = []

    def bind_delivery(self, event) -> None:
        """Guard final sends without depending on any message splitter.

        Args:
            event: Event whose direct sends may contain this plugin's audio.
        """
        platform = self.context.get_platform_inst(event.get_platform_id())
        if platform is not None:
            state = getattr(platform, "_breezetts_delivery_guard", None)
            if state is None:
                original = platform.send_by_session
                state = {"owner": self, "original": original}

                async def send_message(session, message_chain):
                    owner = state["owner"]
                    if owner is None or not owner.active:
                        return await original(session, message_chain)
                    return await owner.send_chain(
                        lambda part: original(session, part), message_chain
                    )

                state["wrapper"] = send_message
                platform._breezetts_delivery_guard = state
                platform.send_by_session = send_message
            else:
                state["owner"] = self
            if not any(saved is state for _, saved in self.delivery_guards):
                self.delivery_guards.append((platform, state))
        if event.get_extra("breezetts_delivery_guard") is self:
            return
        original_send = event.send

        async def send(message):
            if not self.active:
                return await original_send(message)
            return await self.send_chain(original_send, message)

        event.send = send
        event.set_extra("breezetts_delivery_guard", self)

    def close_delivery(self) -> None:
        """Deactivate owned wrappers and preserve any later host wrappers."""
        self.active = False
        for platform, state in self.delivery_guards:
            if state["owner"] is not self:
                continue
            state["owner"] = None
            if platform.send_by_session is state["wrapper"]:
                platform.send_by_session = state["original"]
                del platform._breezetts_delivery_guard
        self.delivery_guards.clear()

    async def send_chain(self, sender, message):
        """Send reserved Breeze audio alone while retaining surrounding content.

        Args:
            sender: Next sender in the host's existing delivery chain.
            message: Message already processed by the host and optional plugins.

        Returns:
            The final sender result, or the original result for unrelated content.
            A partially completed delivery returns True to prevent full replay.

        Raises:
            RuntimeError: A sender rejects a partitioned message.
            Exception: The platform fails or delivery is cancelled.
        """
        owned = {}
        for index, component in enumerate(getattr(message, "chain", []) or []):
            if not isinstance(component, Record) or not component.path:
                continue
            path = Path(component.path)
            if (
                path.stem not in self.service.delivery_audio
                or self.service.store is None
            ):
                continue
            if path.resolve() == self.service.store.path(path.stem).resolve():
                owned[index] = path.stem
        if not owned:
            return await sender(message)

        batches = []
        pending = []
        for index, component in enumerate(message.chain):
            if index not in owned:
                pending.append(component)
                continue
            if any(
                isinstance(part, Plain)
                and part.text.strip()
                or not isinstance(part, (Plain, Reply, At, AtAll))
                for part in pending
            ):
                batches.append(pending)
                pending = []
            batches.append([component])
        if any(
            isinstance(part, Plain)
            and part.text.strip()
            or not isinstance(part, (Plain, Reply, At, AtAll))
            for part in pending
        ):
            batches.append(pending)

        outcome = None
        delivered = False
        try:
            for batch in batches:
                outcome = await sender(message.derive(batch))
                if outcome is False:
                    raise RuntimeError(
                        "Breeze speech message was rejected by the sender"
                    )
                delivered = True
        except Exception as exc:
            if not delivered:
                raise
            self.logger.warning(
                "Breeze message was partially delivered (%s); skipping whole-message replay.",
                type(exc).__name__,
            )
            return True
        finally:
            pending_ids = set(owned.values()) & self.service.delivery_audio.keys()
            if pending_ids:
                await self.service.release_audio(pending_ids)
        return outcome

    def turn(self, event) -> SpeechTurn:
        """Make one probability decision and reuse it throughout an event.

        Args:
            event: Incoming event that owns the speech decision.

        Returns:
            The event-local state, with no cross-session response cache.
        """
        state = event.get_extra(TURN_KEY)
        if state is None:
            behavior = self.service.settings()["behavior"]
            probability = behavior["trigger_probability"]
            scope = event.get_message_type()
            override = (
                behavior["group_trigger_probability"]
                if scope == MessageType.GROUP_MESSAGE
                else behavior["private_trigger_probability"]
                if scope == MessageType.FRIEND_MESSAGE
                else -1
            )
            if override >= 0:
                probability = override
            state = SpeechTurn(
                False,
                behavior["tool_enabled"],
                session=str(event.unified_msg_origin or ""),
            )
            state.random_selected = bool(
                behavior["auto_tts"]
                and probability > 0
                and self.frequency.can_speak(
                    state.session, state.token, behavior["min_interval_seconds"]
                )
                and (probability == 100 or random.random() < probability / 100)
            )
            self.logger.debug(
                "Breeze speech decision (probability=%s, random_selected=%s, autonomous=%s).",
                probability,
                state.random_selected,
                state.autonomous,
            )
            event.set_extra(TURN_KEY, state)
        return state

    async def tool(self, event, text: str) -> str:
        """Send one model-selected span, suppressing repeated or competing calls.

        Args:
            event: Destination event and deduplication scope.
            text: Exact spoken text selected by the model.

        Returns:
            A model-facing delivery outcome, with safe errors on failure.
        """
        config = self.service.settings()
        if not config["behavior"]["tool_enabled"]:
            return "BreezeTTS tool is disabled."
        if not isinstance(text, str):
            return "Speech text must be a string."
        text = "".join(part for part, _ in parse_speech(text)).strip()
        if not text or len(text) > config["behavior"]["max_characters"]:
            return "Choose a nonempty spoken passage within the configured character limit."
        state = self.turn(event)
        if not state.autonomous:
            return "Speech is disabled for this turn. Continue with ordinary text."
        key = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).strip(
            "。.!！?？\"'“” "
        )
        async with state.lock:
            if key in state.attempted or state.source in {"tags", "random"}:
                return "This turn's voice has already been handled. Continue with ordinary text."
            if len(state.attempted) >= MAX_SPANS:
                return "The speech limit for this turn has been reached. Continue with ordinary text."
            if not self.frequency.acquire(
                state.session, state.token, config["behavior"]["min_interval_seconds"]
            ):
                return "Speech is cooling down or another reply is generating voice. Continue with ordinary text."
            state.attempted.add(key)
            state.source = "tool"
            audio_ids = set()
            delivered = False
            generated = False
            try:
                item = await self.service.synthesize(
                    text, config=config, for_delivery=True
                )
                generated = True
                audio_ids.add(item["id"])
                assert self.service.store is not None
                self.bind_delivery(event)
                chain = [
                    Record.fromFileSystem(
                        self.service.store.path(item["id"]), text=text
                    )
                ]
                await event.send(MessageChain(chain))
                delivered = True
                if config["behavior"]["keep_text"]:
                    await event.send(MessageChain([Plain(text)]))
                return json.dumps(
                    {
                        "status": "sent",
                        "caption_sent": config["behavior"]["keep_text"],
                        "next": "Continue only with new in-character dialogue. If complete, return empty text. "
                        "Omit delivery acknowledgments, bracketed status messages, and repeated spoken words.",
                    }
                )
            except BreezeError as exc:
                self.logger.warning("Breeze voice tool failed (code=%s).", exc.code)
                return f"Voice generation failed: {exc} Reply with the passage as ordinary text."
            except Exception as exc:
                self.logger.error(
                    "Breeze voice delivery failed (%s).", type(exc).__name__
                )
                if delivered:
                    return "Voice message sent; its text caption could not be delivered. Continue with ordinary text."
                return "Voice delivery failed. Reply with the passage as ordinary text."
            finally:
                self.frequency.finish(state.session, state.token, generated)
                await self.service.release_audio(audio_ids)

    async def decorate(self, event) -> None:
        """Replace selected spans in place before optional message splitting.

        Args:
            event: Event containing a completed LLM message chain.
        """
        result = event.get_result()
        if (
            result is None
            or not result.chain
            or getattr(result, "_breezetts_processed", False)
        ):
            return
        # A result may pass several decorators; never synthesize the same result twice.
        result._breezetts_processed = True
        state = self.turn(event)
        config = self.service.settings()
        raw_text = "".join(
            part.text for part in result.chain if isinstance(part, Plain)
        )
        planned_text = "".join(text for text, _ in state.plan)
        intervals = []
        offset = 0
        if state.plan and raw_text == planned_text:
            for text, voice in state.plan:
                if voice:
                    intervals.append((offset, offset + len(text)))
                offset += len(text)
        # Parse contiguous Plain components together so tags can cross their boundaries.
        parts = []
        index = offset = 0
        while index < len(result.chain):
            component = result.chain[index]
            if not isinstance(component, Plain):
                parts.append((component, False))
                index += 1
                continue
            group = ""
            original = []
            while index < len(result.chain) and isinstance(result.chain[index], Plain):
                original.append(result.chain[index])
                group += result.chain[index].text
                index += 1
            if result.is_llm_result():
                cleaned = strip_delivery_ack(group)
                if cleaned != group:
                    state.status_cleaned = True
                    parts.extend(
                        (Plain(text), voice)
                        for text, voice in parse_speech(cleaned)
                        if text
                    )
                    offset += len(group)
                    continue
            if TAG.search(group) or ENCODED_TAG.search(group):
                parts.extend(
                    (Plain(text), voice) for text, voice in parse_speech(group) if text
                )
            else:
                if not any(
                    offset <= start < end <= offset + len(group)
                    for start, end in intervals
                ):
                    parts.extend((part, False) for part in original)
                    offset += len(group)
                    continue
                cursor = 0
                for start, end in intervals:
                    if offset <= start < end <= offset + len(group):
                        if start - offset > cursor:
                            parts.append((Plain(group[cursor : start - offset]), False))
                        parts.append(
                            (Plain(group[start - offset : end - offset]), True)
                        )
                        cursor = end - offset
                if cursor < len(group):
                    parts.append((Plain(group[cursor:]), False))
            offset += len(group)
        if state.status_cleaned and not any(
            (isinstance(part, Plain) and part.text.strip())
            or not isinstance(part, (Plain, At, AtAll, Reply))
            for part, _ in parts
        ):
            parts = []
        # Commit tag-free fallback before any I/O, including cancellation paths.
        result.chain[:] = [component for component, _ in parts]
        if not result.is_llm_result():
            return
        async with state.lock:
            allowed = (state.random_selected or state.autonomous) and not state.source
            program_selected = False
            if (
                allowed
                and state.random_selected
                and not any(isinstance(part, Record) for part, _ in parts)
                and not any(
                    voice
                    and 0
                    < len(part.text.strip())
                    <= config["behavior"]["max_characters"]
                    for part, voice in parts
                )
            ):
                index = 0
                while index < len(parts):
                    if not isinstance(parts[index][0], Plain):
                        index += 1
                        continue
                    stop = index
                    while stop < len(parts) and isinstance(parts[stop][0], Plain):
                        stop += 1
                    group = "".join(part.text for part, _ in parts[index:stop])
                    span = select_spoken_span(
                        group, config["behavior"]["max_characters"]
                    )
                    if span is None:
                        index = stop
                        continue
                    start, end = span
                    replacement = []
                    cursor = 0
                    inserted = False
                    for part, _ in parts[index:stop]:
                        following = cursor + len(part.text)
                        if following <= start or cursor >= end:
                            replacement.append((part, False))
                        else:
                            if cursor < start:
                                replacement.append(
                                    (Plain(part.text[: start - cursor]), False)
                                )
                            if not inserted:
                                replacement.append((Plain(group[start:end]), True))
                                inserted = True
                            if following > end:
                                replacement.append(
                                    (Plain(part.text[end - cursor :]), False)
                                )
                        cursor = following
                    parts[index:stop] = replacement
                    program_selected = True
                    break
                if not program_selected:
                    self.logger.debug(
                        "Breeze random speech skipped: no suitable prose passage."
                    )
            spoken = 0
            chain = []
            for component, voice in parts:
                if (
                    not voice
                    or not allowed
                    or spoken
                    or any(isinstance(part, Record) for part, _ in parts)
                ):
                    chain.append(component)
                    continue
                text = component.text.strip()
                if program_selected:
                    text = (
                        text.replace("**", "")
                        .replace("__", "")
                        .replace("~~", "")
                        .strip()
                    )
                key = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).strip(
                    "。.!！?？\"'“” "
                )
                if (
                    not key
                    or key in state.attempted
                    or len(state.attempted) >= MAX_SPANS
                    or len(text) > config["behavior"]["max_characters"]
                ):
                    chain.append(component)
                    continue
                if not self.frequency.acquire(
                    state.session,
                    state.token,
                    config["behavior"]["min_interval_seconds"],
                ):
                    chain.append(component)
                    continue
                state.attempted.add(key)
                state.source = "random" if program_selected else "tags"
                spoken += 1
                generated = False
                try:
                    item = await self.service.synthesize(
                        text, config=config, for_delivery=True
                    )
                    generated = True
                    state.audio_ids.add(item["id"])
                    assert self.service.store is not None
                    self.bind_delivery(event)
                    chain.append(
                        Record.fromFileSystem(
                            self.service.store.path(item["id"]), text=text
                        )
                    )
                    if config["behavior"]["keep_text"]:
                        chain.append(component)
                except BreezeError as exc:
                    self.logger.warning(
                        "Breeze selected speech failed (code=%s); retaining text.",
                        exc.code,
                    )
                    chain.append(component)
                except Exception as exc:
                    self.logger.error(
                        "Breeze selected speech failed (%s); retaining text.",
                        type(exc).__name__,
                    )
                    chain.append(component)
                finally:
                    self.frequency.finish(state.session, state.token, generated)
            result.chain[:] = chain
