"""Select spoken spans and coordinate delivery within a single message event."""

from __future__ import annotations

import asyncio
import json
import random
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, AtAll, Node, Nodes, Plain, Record, Reply
from astrbot.core.platform.message_type import MessageType

from .breeze_control import (
    clean_breeze_text,
    control_pattern,
    control_prompt,
    parse_breeze_text,
)
from .expression import check_directions
from .settings import DEFAULTS, BreezeError
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
    speech_inputs: dict[str, str] = field(default_factory=dict)
    status_cleaned: bool = False
    model_response: bool = False


class SpeechDelivery:
    """Coordinate selective speech and owned audio at AstrBot delivery boundaries."""

    def parse_speech(self, text: str, *, preserve_controls: bool = False):
        """Parse speech wrappers with the selected native token catalog.

        Args:
            text: Candidate model dialogue.
            preserve_controls: Retain controls for synthesis input.

        Returns:
            Ordered dialogue and speech-eligibility pairs.
        """
        return parse_speech(
            text,
            preserve_controls=preserve_controls,
            tags=self.service.profile()["tags"],
        )

    def __init__(self, service, logger, context):
        self.service = service
        self.logger = logger
        self.context = context
        self.frequency = SpeechFrequency()
        self.active = True
        self.delivery_guards = []
        self.tone_tasks: set[asyncio.Task] = set()

    def bind_delivery(self, event, *, clean_text: bool = False) -> None:
        """Guard final sends without depending on any message splitter.

        Args:
            event: Event whose direct sends may contain this plugin's audio.
            clean_text: Hide controls in this model event's final outgoing text.
        """
        if clean_text:
            event.set_extra("breezetts_clean_text", True)
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
            return await self.send_chain(
                original_send,
                message,
                hide_controls=event.get_extra("breezetts_clean_text", False),
            )

        event.send = send
        event.set_extra("breezetts_delivery_guard", self)

    def close_delivery(self) -> None:
        """Deactivate owned wrappers and preserve any later host wrappers."""
        self.active = False
        for task in self.tone_tasks:
            task.cancel()
        for platform, state in self.delivery_guards:
            if state["owner"] is not self:
                continue
            state["owner"] = None
            if platform.send_by_session is state["wrapper"]:
                platform.send_by_session = state["original"]
                del platform._breezetts_delivery_guard
        self.delivery_guards.clear()

    def clean_chain(self, chain: list) -> list:
        """Hide speech controls in text, transcripts, and nested forward messages.

        Args:
            chain: Outgoing components, possibly containing fragmented controls.

        Returns:
            Clean components in their original order, reusing unchanged objects.
            Contiguous text is parsed together to preserve code and Markdown links.
        """
        cleaned = []
        index = 0
        while index < len(chain):
            component = chain[index]
            if isinstance(component, Plain):
                start = index
                while index < len(chain) and isinstance(chain[index], Plain):
                    index += 1
                original = "".join(part.text for part in chain[start:index])
                text = "".join(part for part, _ in self.parse_speech(original))
                if text == original:
                    cleaned.extend(chain[start:index])
                elif text.strip():
                    cleaned.append(Plain(text))
                continue
            if isinstance(component, (Node, Nodes)):
                key = "content" if isinstance(component, Node) else "nodes"
                original = getattr(component, key)
                children = self.clean_chain(original)
                if children != original:
                    if not children:
                        index += 1
                        continue
                    component = component.copy()
                    setattr(component, key, children)
            elif isinstance(component, Record) and component.text:
                text = "".join(part for part, _ in self.parse_speech(component.text))
                if text != component.text:
                    component = component.copy()
                    component.text = text
            cleaned.append(component)
            index += 1
        return cleaned

    async def send_chain(self, sender, message, *, hide_controls: bool = False):
        """Send reserved Breeze audio alone while retaining surrounding content.

        Args:
            sender: Next sender in the host's existing delivery chain.
            message: Message already processed by the host and optional plugins.
            hide_controls: Clean this model event's visible text before dispatch.

        Returns:
            The final sender result, or the original result for unrelated content.
            A partially completed delivery returns True to prevent full replay.

        Raises:
            RuntimeError: A sender rejects a partitioned message.
            Exception: The platform fails or delivery is cancelled.
        """
        if hide_controls and getattr(message, "chain", None):
            cleaned = self.clean_chain(message.chain)
            if cleaned != message.chain:
                if not any(
                    isinstance(part, Plain)
                    and part.text.strip()
                    or not isinstance(part, (Plain, Reply, At, AtAll))
                    for part in cleaned
                ):
                    return True
                message = message.derive(cleaned)
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
                for part in batch:
                    if (
                        isinstance(part, Record)
                        and part.path
                        and Path(part.path).stem in owned.values()
                        and Path(part.path).stem in self.service.delivery_audio
                    ):
                        self.logger.info(
                            "Breeze audio submitted (audio_id=%s).",
                            Path(part.path).stem,
                        )
        except Exception as exc:
            if not delivered:
                self.logger.warning(
                    "Breeze audio submission failed (audio_ids=%s, error=%s).",
                    ",".join(owned.values()),
                    type(exc).__name__,
                )
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
                "Breeze speech decision (probability=%s, random_selected=%s, autonomous=%s, stage=candidate).",
                probability,
                state.random_selected,
                state.autonomous,
            )
            event.set_extra(TURN_KEY, state)
        return state

    async def tool(self, event, text: str, instruction: str = "") -> str:
        """Send one model-selected span, suppressing repeated or competing calls.

        Args:
            event: Destination event and deduplication scope.
            text: Exact spoken text selected by the model.
            instruction: Optional per-call performance direction.

        Returns:
            A model-facing delivery outcome, with safe errors on failure.
        """
        config = self.service.settings()
        if not config["behavior"]["tool_enabled"]:
            return "BreezeTTS tool is disabled."
        if not isinstance(text, str):
            return "Speech text must be a string."
        text = "".join(
            part for part, _ in self.parse_speech(text, preserve_controls=True)
        ).strip()
        caption = self.service.clean_text(text).strip()
        if not text or len(text) > config["behavior"]["max_characters"] + 1800:
            return "Choose a nonempty spoken passage within the configured character limit."
        state = self.turn(event)
        if not state.autonomous:
            return "Speech is disabled for this turn. Continue with ordinary text."
        key = re.sub(r"\s+", "", unicodedata.normalize("NFKC", caption or text)).strip(
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
            caption_sent = False
            generated = False
            try:
                item = await self.service.synthesize(
                    text, config=config, for_delivery=True, instruction=instruction
                )
                generated = True
                audio_ids.add(item["id"])
                assert self.service.store is not None
                self.bind_delivery(event)
                chain = [
                    Record.fromFileSystem(
                        self.service.store.path(item["id"]), text=caption
                    )
                ]
                if config["behavior"]["keep_text"] and caption:
                    if await event.send(MessageChain([Plain(caption)])) is False:
                        raise RuntimeError("Breeze caption was rejected by the sender")
                    caption_sent = True
                if await event.send(MessageChain(chain)) is False:
                    raise RuntimeError("Breeze voice was rejected by the sender")
                delivered = True
                return json.dumps(
                    {
                        "status": "sent",
                        "caption_sent": caption_sent,
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
                if caption_sent:
                    return json.dumps(
                        {
                            "status": "voice_failed",
                            "caption_sent": True,
                            "next": "The text was delivered. Do not repeat it or retry voice this turn; continue only with new dialogue.",
                        }
                    )
                return "Voice delivery failed. Reply with the passage as ordinary text."
            finally:
                self.frequency.finish(state.session, state.token, generated)
                await self.service.release_audio(audio_ids)

    async def adapt_random_tone(
        self, event, text: str, config: dict
    ) -> tuple[str, str]:
        """Add performance controls while verifying every spoken character is retained.

        Args:
            event: Current event supplying bounded conversational context.
            text: The already selected spoken passage, kept unchanged.
            config: Validated settings snapshot for this synthesis.

        Returns:
            Controlled speech text and an overall direction. Failures retain the
            original speech input and use the configured default direction.
        """
        behavior = config["behavior"]
        if not behavior["dynamic_tone"] or not behavior["random_tone_adaptive"]:
            return text, ""
        try:
            profile = self.service.profile(config)

            async def choose():
                """Resolve the provider and generate within one shared deadline.

                Returns:
                    The selected provider's raw language-model response.
                """
                provider_id = behavior[
                    "tone_provider_id"
                ] or await self.context.get_current_chat_provider_id(
                    event.unified_msg_origin
                )
                return await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    system_prompt=control_prompt(
                        local=config["backend"] == "local",
                        language=config["cloud"]["language_code"],
                        profile=profile,
                        prompt=behavior["tone_prompt"].strip()
                        or DEFAULTS["behavior"]["tone_prompt"],
                        user_message=event.message_str,
                        passage=text,
                    ),
                    prompt=json.dumps(
                        {"user_message": event.message_str[:1000], "passage": text},
                        ensure_ascii=False,
                    ),
                )

            task = asyncio.create_task(choose())
            self.tone_tasks.add(task)
            try:
                response = await asyncio.wait_for(task, timeout=20)
            finally:
                self.tone_tasks.discard(task)
            raw = response.completion_text
            if not isinstance(raw, str) or len(raw) > 32000:
                raise ValueError("Invalid tone response size")
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I)
            data = json.loads(raw)
            direction = data.get("instruction", "") if isinstance(data, dict) else None
            if (
                not isinstance(direction, str)
                or len(direction) > 200
                or any(ord(char) < 32 for char in direction)
                or any(char in direction for char in "[]<>")
                or clean_breeze_text(direction, profile["tags"]) != direction
            ):
                raise ValueError("Invalid tone direction")
            marked_text = data.get("text", text)
            if (
                not isinstance(marked_text, str)
                or len(marked_text) > behavior["max_characters"] + 1800
            ):
                raise ValueError("Invalid controlled passage")
            if clean_breeze_text(marked_text, profile["tags"]) != clean_breeze_text(
                text, profile["tags"]
            ):
                raise ValueError("Tone adaptation rewrote spoken dialogue")
            sections = parse_breeze_text(
                marked_text, enabled=True, tags=profile["tags"]
            )
            if (
                not sections
                or sum(len(fragment) for fragment, _ in sections)
                > behavior["max_characters"]
            ):
                raise ValueError("Controlled passage exceeds synthesis limits")
            check_directions(
                profile,
                direction,
                *(style for _, style in sections),
            )
            markers = []
            for value in (text, marked_text):
                controls = Counter()
                cursor = visible = 0
                for match in control_pattern(profile["tags"]).finditer(value):
                    if match["protected"]:
                        continue
                    visible += len(value[cursor : match.start()])
                    controls[(visible, match[0])] += 1
                    cursor = match.end()
                markers.append(controls)
            if markers[0] - markers[1]:
                raise ValueError("Tone adaptation changed existing controls")
            self.logger.info(
                "Breeze random tone adapted (provider=%s, directed=%s, added_controls=%s, sections=%s).",
                "configured" if behavior["tone_provider_id"] else "session",
                bool(direction.strip()),
                sum((markers[1] - markers[0]).values()),
                len(sections),
            )
            return marked_text, direction.strip()
        except Exception as exc:
            self.logger.warning(
                "Breeze random tone adaptation failed (error=%s); using configured tone.",
                type(exc).__name__,
            )
            return text, ""

    async def decorate(self, event) -> None:
        """Replace selected spans in place before optional message splitting.

        Args:
            event: Event containing a completed LLM message chain.
        """
        result = event.get_result()
        state = event.get_extra(TURN_KEY)
        if (
            result is None
            or not result.chain
            or getattr(result, "_breezetts_processed", False)
            or not result.is_llm_result()
            or state is None
            or not state.model_response
        ):
            if (
                state
                and state.random_selected
                and not getattr(result, "_breezetts_processed", False)
            ):
                self.logger.debug(
                    "Breeze speech skipped (reason=no_model_reply, source=random)."
                )
            return
        # A result may pass several decorators; never synthesize the same result twice.
        result._breezetts_processed = True
        config = self.service.settings()
        quoted = any(isinstance(part, Reply) for part in result.chain) or bool(
            self.context.get_config(event.unified_msg_origin)
            .get("platform_settings", {})
            .get("reply_with_quote", False)
        )
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
                        for text, voice in self.parse_speech(cleaned)
                        if text
                    )
                    offset += len(group)
                    continue
            if (
                TAG.search(group)
                or ENCODED_TAG.search(group)
                or self.service.clean_text(group) != group
            ):
                if self.service.clean_text(group) != group:
                    state.speech_inputs.setdefault(
                        self.service.clean_text(group), group
                    )
                parts.extend(
                    (Plain(text), voice)
                    for text, voice in self.parse_speech(group)
                    if text
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
            allowed = allowed and (not quoted or config["behavior"]["quote_keep_text"])
            if state.random_selected:
                reason = (
                    "handled_by_" + state.source
                    if state.source
                    else "quoted_reply_text_only"
                    if quoted and not config["behavior"]["quote_keep_text"]
                    else "existing_audio"
                    if any(isinstance(part, Record) for part, _ in parts)
                    else ""
                )
                if reason:
                    self.logger.info(
                        "Breeze speech skipped (reason=%s, source=random).", reason
                    )
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
                    self.logger.info(
                        "Breeze speech skipped (reason=no_suitable_prose, source=random)."
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
                synthesis_text = state.speech_inputs.get(component.text, text)
                if program_selected:
                    text = (
                        text.replace("**", "")
                        .replace("__", "")
                        .replace("~~", "")
                        .strip()
                    )
                    synthesis_text = text
                    # Recover only controls inside this selected passage. The last
                    # preceding direction still governs its first spoken word.
                    for display, original in state.speech_inputs.items():
                        start = display.find(component.text)
                        if start < 0:
                            continue
                        stop = start + len(component.text)
                        tokens = []
                        cursor = visible = 0
                        direction = ""
                        for match in control_pattern(
                            self.service.profile()["tags"]
                        ).finditer(original):
                            if match["protected"]:
                                continue
                            fragment = original[cursor : match.start()]
                            end = visible + len(fragment)
                            tokens.append(
                                fragment[
                                    max(0, start - visible) : max(
                                        0, min(len(fragment), stop - visible)
                                    )
                                ]
                            )
                            visible = end
                            if match["direction"] and visible < start:
                                direction = match[0]
                            elif start <= visible < stop:
                                tokens.append(match[0])
                            cursor = match.end()
                        fragment = original[cursor:]
                        tokens.append(
                            fragment[
                                max(0, start - visible) : max(
                                    0, min(len(fragment), stop - visible)
                                )
                            ]
                        )
                        synthesis_text = direction + "".join(tokens)
                        break
                    synthesis_text = re.sub(r"\*\*|__|~~", "", synthesis_text).strip()
                key = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).strip(
                    "。.!！?？\"'“” "
                )
                if (
                    not key
                    or key in state.attempted
                    or len(state.attempted) >= MAX_SPANS
                    or len(text) > config["behavior"]["max_characters"]
                ):
                    self.logger.info(
                        "Breeze speech skipped (reason=invalid_or_repeated_span, source=%s).",
                        "random" if program_selected else "tags",
                    )
                    chain.append(component)
                    continue
                if not self.frequency.acquire(
                    state.session,
                    state.token,
                    config["behavior"]["min_interval_seconds"],
                ):
                    self.logger.info(
                        "Breeze speech skipped (reason=cooldown_or_inflight, source=%s).",
                        "random" if program_selected else "tags",
                    )
                    chain.append(component)
                    continue
                state.attempted.add(key)
                state.source = "random" if program_selected else "tags"
                spoken += 1
                generated = False
                try:
                    instruction = ""
                    if program_selected:
                        synthesis_text, instruction = await self.adapt_random_tone(
                            event, synthesis_text, config
                        )
                    item = await self.service.synthesize(
                        synthesis_text,
                        config=config,
                        for_delivery=True,
                        instruction=instruction,
                    )
                    generated = True
                    state.audio_ids.add(item["id"])
                    assert self.service.store is not None
                    self.bind_delivery(event)
                    record = Record.fromFileSystem(
                        self.service.store.path(item["id"]), text=text
                    )
                    self.logger.info(
                        "Breeze speech prepared (source=%s, audio_id=%s, characters=%s).",
                        state.source,
                        item["id"],
                        len(text),
                    )
                    if quoted or config["behavior"]["keep_text"]:
                        chain.append(component)
                    chain.append(record)
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
