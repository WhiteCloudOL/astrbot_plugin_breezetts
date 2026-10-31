"""Select spoken spans and coordinate delivery within a single message event."""

from __future__ import annotations

import asyncio
import random
import re
import unicodedata
from dataclasses import dataclass, field

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain, Record

from .settings import BreezeError
from .speech_markup import ENCODED_TAG, TAG, parse_speech

TURN_KEY = "breezetts_speech_turn"
MAX_SPANS = 4


@dataclass
class SpeechTurn:
    """Keep selection, duplicate suppression, and file reservations event-local."""

    random_selected: bool
    autonomous: bool
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    attempted: set[str] = field(default_factory=set)
    source: str = ""
    audio_ids: set[str] = field(default_factory=set)
    plan: list[tuple[str, bool]] = field(default_factory=list)


class SpeechDelivery:
    """Adapt selective speech to AstrBot messages without changing host services."""

    def __init__(self, service, logger):
        self.service = service
        self.logger = logger

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
            state = SpeechTurn(
                bool(
                    behavior["auto_tts"]
                    and random.random() < behavior["trigger_probability"] / 100
                ),
                behavior["tool_enabled"],
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
        key = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).strip(
            "。.!！?？\"'“” "
        )
        async with state.lock:
            if key in state.attempted or state.source == "tags":
                return "This turn's voice has already been handled. Continue with ordinary text."
            if len(state.attempted) >= MAX_SPANS:
                return "The speech limit for this turn has been reached. Continue with ordinary text."
            state.attempted.add(key)
            state.source = "tool"
            audio_ids = set()
            delivered = False
            try:
                item = await self.service.synthesize(
                    text, config=config, for_delivery=True
                )
                audio_ids.add(item["id"])
                assert self.service.store is not None
                chain = [
                    Record.fromFileSystem(
                        self.service.store.path(item["id"]), text=text
                    )
                ]
                await event.send(MessageChain(chain))
                delivered = True
                if config["behavior"]["keep_text"]:
                    await event.send(MessageChain([Plain(text)]))
                return "Voice message sent. Continue with any remaining ordinary text; the spoken passage is already delivered."
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
        # Commit tag-free fallback before any I/O, including cancellation paths.
        result.chain[:] = [component for component, _ in parts]
        if not result.is_llm_result():
            return
        async with state.lock:
            allowed = (state.random_selected or state.autonomous) and not state.source
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
                state.attempted.add(key)
                state.source = "tags"
                spoken += 1
                try:
                    item = await self.service.synthesize(
                        text, config=config, for_delivery=True
                    )
                    state.audio_ids.add(item["id"])
                    assert self.service.store is not None
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
            result.chain[:] = chain
