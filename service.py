"""Coordinate settings, bounded jobs, and persistent audio operations."""

from __future__ import annotations

import asyncio
import copy
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from .breeze_audio import join_breeze_audio
from .breeze_control import clean_breeze_text, parse_breeze_text
from .client import BreezeClient
from .expression import PRESETS, check_directions, expression_profile, provider_preset
from .provider_audio import provider_audio
from .settings import (
    DEFAULTS,
    FORMATS,
    PROMPT_VARIABLES,
    STREAM_FORMATS,
    BreezeError,
    validate_config,
    validate_identifier,
)
from .storage import AUDIO_LIMIT, AudioStore


class BreezeService:
    """Own plugin resources and serialize file/config mutations."""

    def __init__(self, config, root: Path, logger, context=None):
        self.config = config
        self.root = root
        self.logger = logger
        self.context = context
        self.client = BreezeClient()
        self.store: AudioStore | None = None
        self.lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(2)
        self.local_slot = asyncio.Lock()
        self.jobs: dict[str, dict] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.requests: set[asyncio.Task] = set()
        self.closed = False
        self.active = 0
        self.delivery_audio: dict[str, float] = {}
        self.reading_audio: dict[str, float] = {}
        self.maintenance: asyncio.Task | None = None

    async def io(self, operation, *args, **kwargs):
        """Finish filesystem work before releasing its lock on cancellation.

        Args:
            operation: Blocking filesystem operation to run in a worker thread.
            *args: Positional operation arguments.
            **kwargs: Keyword operation arguments.

        Returns:
            The operation's result.

        Raises:
            asyncio.CancelledError: After the in-flight filesystem work finishes.
        """
        task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def start(self) -> None:
        """Load persistent data and acquire the HTTP session once."""
        async with self.lock:
            if self.closed:
                raise BreezeError("插件正在关闭，请稍后重试。")
            if self.store is None:
                cloud = self.config.get("cloud", {})
                if (
                    isinstance(cloud, dict)
                    and cloud.get("delivery") == "stream"
                    and isinstance(cloud.get("output_format"), str)
                    and cloud["output_format"] in FORMATS - STREAM_FORMATS
                ):
                    # Older versions allowed this combination; keep the UI accessible.
                    validate_config(
                        dict(self.config) | {"cloud": {**cloud, "output_format": "wav"}}
                    )
                    self.config["cloud"] = {**cloud, "output_format": "wav"}
                    self.logger.warning(
                        "Legacy cloud stream format is unsupported; using WAV in memory until settings are saved."
                    )
                self.store = await self.io(AudioStore, self.root)
                await self.prune_audio(self.settings()["behavior"]["history_limit"])
            await self.client.start()
            if self.maintenance is None:
                self.maintenance = asyncio.create_task(
                    self.maintain_audio(), name="breezetts-audio-cleanup"
                )

    async def maintain_audio(self) -> None:
        """Apply retention after abandoned delivery and download leases expire."""
        failed = False
        while True:
            await asyncio.sleep(60)
            try:
                async with self.lock:
                    await self.prune_audio(self.settings()["behavior"]["history_limit"])
                failed = False
            except (OSError, BreezeError) as exc:
                if not failed:
                    self.logger.warning(
                        "Breeze scheduled audio cleanup failed (%s).",
                        type(exc).__name__,
                    )
                failed = True

    async def close(self) -> None:
        """Cancel and await owned jobs before closing the HTTP session."""
        self.closed = True
        tasks = list(set(self.tasks.values()) | self.requests)
        if self.maintenance is not None:
            tasks.append(self.maintenance)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        self.requests.clear()
        await self.client.close()

    def settings(self) -> dict:
        """Return a validated snapshot of AstrBot's injected settings.

        Returns:
            A standalone configuration snapshot for one operation.
        """
        return validate_config(dict(self.config))

    def profile(
        self, config: dict | None = None, *, require_provider: bool = False
    ) -> dict:
        """Resolve effective controls for prompts, captions, and synthesis.

        Args:
            config: Operation snapshot, or the current saved configuration.
            require_provider: Reject an unavailable provider before synthesis.

        Returns:
            Public capabilities without any provider credentials.
        """
        config = config or self.settings()
        provider = None
        if config["backend"] == "provider" and self.context is not None:
            provider = self.context.get_provider_by_id(
                config["provider"]["provider_id"]
            )
        try:
            return expression_profile(config, provider)
        except BreezeError:
            if require_provider:
                raise
            tags = {tag for preset in PRESETS.values() for tag in preset["tags"]}
            for name, custom in config["expression"].items():
                if name.endswith("_tags"):
                    tags.update(
                        line.strip() for line in custom.splitlines() if line.strip()
                    )
            return {
                "key": "unavailable",
                "name": "TTS unavailable",
                "mode": "fixed",
                "tags": tuple(sorted(tags)),
                "styles": [],
                "note": "Selected TTS provider is unavailable; select a supported model before using speech.",
            }

    def clean_text(self, text: str) -> str:
        """Hide the current provider's controls in user-visible text.

        Args:
            text: Outgoing dialogue or a speech caption.

        Returns:
            Dialogue with recognized controls removed and literals preserved.
        """
        return clean_breeze_text(text, self.profile()["tags"])

    async def state(self) -> dict:
        """Return the workbench state with all credential values removed.

        Returns:
            Redacted settings, credential presence, and persistent audio records.
        """
        await self.start()
        config = self.settings()
        credentials = {}
        for backend in ("local", "cloud"):
            credentials[backend] = bool(config[backend]["api_key"])
            config[backend]["api_key"] = ""
        async with self.lock:
            assert self.store is not None
            items = sorted(
                (dict(item) for item in self.store.items.values()),
                key=lambda item: item["created"],
                reverse=True,
            )
        providers = []
        if self.context is not None:
            for provider in self.context.get_all_providers():
                meta = provider.meta()
                providers.append({"id": meta.id, "name": meta.id})
        tts_providers = []
        if self.context is not None and hasattr(self.context, "get_all_tts_providers"):
            for provider in self.context.get_all_tts_providers():
                key = provider_preset(provider)
                if key or provider.provider_config.get("type") == "dashscope_tts":
                    provider_config = config | {
                        "backend": "provider",
                        "expression": DEFAULTS["expression"],
                        "provider": {**config["provider"], "preset": ""},
                    }
                    try:
                        preset = expression_profile(provider_config, provider)
                    except BreezeError:
                        preset = {
                            "key": "",
                            "name": "Dashscope（无可用动态语调协议）",
                            "mode": "fixed",
                            "tags": [],
                            "styles": [],
                            "note": "当前模型或音色不支持已接入的指令协议，请在 AstrBot 中更换模型或音色。",
                        }
                    tts_providers.append(
                        {
                            "id": provider.meta().id,
                            "name": provider.meta().id,
                            "type": provider.provider_config.get("type"),
                            "profile": preset,
                        }
                    )
        presets = copy.deepcopy(PRESETS)
        for backend in ("local", "cloud"):
            presets["breeze_" + backend] = expression_profile(
                config | {"backend": backend, "expression": DEFAULTS["expression"]}
            )
        return {
            "tts_providers": tts_providers,
            "expression_presets": presets,
            "config": config,
            "credentials": credentials,
            "audio": items,
            "llm_providers": providers,
            "prompt_variables": PROMPT_VARIABLES,
            "prompt_defaults": {
                name: DEFAULTS["behavior"][name]
                for name in ("speech_prompt", "tone_prompt")
            },
        }

    async def save_settings(self, draft: dict, clear_keys: list) -> dict:
        """Save a validated draft while retaining omitted or blank credentials.

        Args:
            draft: Configuration edited by the WebUI.
            clear_keys: Explicit local/cloud credentials selected for removal.

        Returns:
            The updated redacted workbench state.

        Raises:
            BreezeError: If validation or persistence fails.
        """
        if (
            not isinstance(draft, dict)
            or not isinstance(clear_keys, list)
            or any(
                not isinstance(key, str) or key not in {"local", "cloud"}
                for key in clear_keys
            )
        ):
            raise BreezeError("配置格式不正确。")
        async with self.lock:
            candidate = copy.deepcopy(draft)
            current = self.settings()
            for backend in ("local", "cloud"):
                if not isinstance(candidate.get(backend), dict):
                    raise BreezeError("请提交完整的连接配置。")
                if backend in clear_keys:
                    candidate[backend]["api_key"] = ""
                elif candidate[backend].get("api_key", "") == "":
                    candidate[backend]["api_key"] = current[backend]["api_key"]
            candidate = validate_config(candidate)
            previous = copy.deepcopy(dict(self.config))
            self.config.clear()
            self.config.update(candidate)
            try:
                await self.io(self.config.save_config)
            except Exception as exc:
                self.config.clear()
                self.config.update(previous)
                self.logger.error(
                    "Breeze configuration save failed (%s).", type(exc).__name__
                )
                raise BreezeError(
                    "配置保存失败，请检查数据目录写入权限。", code="storage"
                ) from None
            try:
                await self.prune_audio(candidate["behavior"]["history_limit"])
            except OSError as exc:
                self.logger.warning(
                    "Breeze settings saved; history cleanup failed (%s).",
                    type(exc).__name__,
                )
        return await self.state()

    async def connection(self) -> dict:
        """Check the selected connection without generating billable audio.

        Returns:
            Local health/model/queue information or official model identifiers.
        """
        config = self.settings()
        try:
            if config["backend"] == "provider":
                profile = self.profile(config, require_provider=True)
                return {
                    "backend": "provider",
                    "profile": profile["name"],
                    "status": "configured",
                }
            if config["backend"] == "local":
                health = await asyncio.wait_for(
                    self.client.request(config["local"], "GET", "../health"), 20
                )
                if not isinstance(health, dict) or health.get("status") != "ok":
                    raise BreezeError("本地服务尚未就绪，请检查服务状态。")
                return {
                    "backend": "local",
                    "health": {
                        "status": "ok",
                        "sample_rate": health.get("sample_rate"),
                    },
                }
            models = await asyncio.wait_for(
                self.client.request(config["cloud"], "GET", "models"), 20
            )
            if not isinstance(models, list) or any(
                not isinstance(item, dict) or not isinstance(item.get("model_id"), str)
                for item in models
            ):
                raise BreezeError("官方模型列表格式无效。")
            return {
                "backend": "cloud",
                "models": [{"model_id": item["model_id"]} for item in models],
            }
        except (TimeoutError, asyncio.TimeoutError):
            raise BreezeError("连接检查超时，请检查服务地址和网络。") from None

    async def synthesize(
        self,
        text: str,
        *,
        config: dict | None = None,
        for_delivery: bool = False,
        instruction: str = "",
    ) -> dict:
        """Generate and persist one audio file with bounded queueing and timeout.

        Args:
            text: Text to synthesize.
            config: Optional validated settings captured when a job was submitted.
            for_delivery: Reserve the output until platform delivery completes.
            instruction: Optional per-call direction, used only with dynamic tone enabled.

        Returns:
            The generated audio record.

        Raises:
            BreezeError: If input, reference, queue, or upstream synthesis fails.
        """
        await self.start()
        config = config or self.settings()
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > config["behavior"]["max_characters"] + 1800
        ):
            raise BreezeError(
                f"合成文字须为 1–{config['behavior']['max_characters']} 个字符。"
            )
        host_provider = None
        if config["backend"] == "provider":
            if self.context is not None:
                host_provider = self.context.get_provider_by_id(
                    config["provider"]["provider_id"]
                )
            profile = expression_profile(config, host_provider)
        else:
            profile = self.profile(config, require_provider=True)
        sections = parse_breeze_text(
            text, enabled=config["behavior"]["dynamic_tone"], tags=profile["tags"]
        )
        if (
            not sections
            or sum(len(part) for part, _ in sections)
            > config["behavior"]["max_characters"]
        ):
            raise BreezeError(
                f"合成正文须为 1–{config['behavior']['max_characters']} 个字符。"
            )
        if not config["behavior"]["dynamic_tone"]:
            instruction = ""
        if (
            not isinstance(instruction, str)
            or len(instruction) > 200
            or any(ord(char) < 32 and char != "\n" for char in instruction)
        ):
            raise BreezeError("本次语气指令须为不超过 200 字符的文本。")
        check_directions(
            profile, instruction, *(direction for _, direction in sections)
        )
        self.logger.debug(
            "Breeze synthesis planned (backend=%s, dynamic=%s, sections=%s, instruction=%s).",
            config["backend"],
            config["behavior"]["dynamic_tone"],
            len(sections),
            bool(instruction),
        )
        if self.active >= 8:
            raise BreezeError("插件已有 8 个请求正在处理或排队，请稍后重试。")
        self.active += 1
        reference = None
        try:
            async with self.lock:
                try:
                    await self.prune_audio(self.settings()["behavior"]["history_limit"])
                except OSError:
                    raise BreezeError(
                        "成品清理失败，请检查音频目录的写入与删除权限。", code="storage"
                    ) from None
            if config["backend"] == "local" and config["local"]["mode"] == "reference":
                async with self.lock:
                    assert self.store is not None
                    path = self.store.path(config["local"]["reference_id"])
                    item = self.store.items[config["local"]["reference_id"]]
                    if item["kind"] != "reference":
                        raise BreezeError("请在音色库中选择参考录音。")
                    reference = (
                        await self.io(path.read_bytes),
                        item.get("ref_text", ""),
                    )

            async def generate():
                async with self.slots:
                    outputs = []
                    size = 0
                    for index, (fragment, direction) in enumerate(sections):
                        section_config = copy.deepcopy(config)
                        backend = section_config["backend"]
                        if backend == "provider":
                            combined = (
                                direction or instruction
                                if profile["mode"] == "fixed"
                                else "\n".join(filter(None, (instruction, direction)))
                            )
                            output = await provider_audio(
                                host_provider, fragment, combined, profile
                            )
                            outputs.append(output)
                            size += len(output[0])
                            if size > AUDIO_LIMIT:
                                raise BreezeError("动态语调合成音频超过大小上限。")
                            continue
                        key = "instruction" if backend == "local" else "instructions"
                        section_config[backend][key] = "\n".join(
                            value.strip()
                            for value in (config[backend][key], instruction, direction)
                            if value.strip()
                        )
                        if len(section_config[backend][key]) > 4000:
                            raise BreezeError(
                                "默认描述与动态语气合并后超过 4000 字符。"
                            )
                        if backend == "cloud" and len(sections) > 1:
                            section_config["cloud"]["output_format"] = "wav"
                        self.logger.debug(
                            "Breeze section synthesis (index=%s, total=%s, characters=%s, directed=%s).",
                            index + 1,
                            len(sections),
                            len(fragment),
                            bool(section_config[backend][key]),
                        )
                        if backend == "local":
                            async with self.local_slot:
                                output = await self.client.synthesize(
                                    section_config, fragment, reference
                                )
                        else:
                            output = await self.client.synthesize(
                                section_config, fragment
                            )
                        outputs.append(output)
                        size += len(output[0])
                        if size > AUDIO_LIMIT:
                            raise BreezeError("动态语调合成音频超过大小上限。")
                    if len(outputs) == 1:
                        return outputs[0]
                    if any(extension != "wav" for _, extension in outputs):
                        raise BreezeError("动态语调分段必须返回 WAV 音频。")
                    return await asyncio.to_thread(
                        join_breeze_audio,
                        [raw for raw, _ in outputs],
                        [part for part, _ in sections],
                        tags=profile["tags"],
                    ), "wav"

            if self.closed:
                raise BreezeError("插件正在关闭，请稍后重试。")
            generation = asyncio.create_task(generate())
            self.requests.add(generation)
            try:
                audio, extension = await asyncio.wait_for(
                    generation, config["behavior"]["timeout"]
                )
            except (TimeoutError, asyncio.TimeoutError):
                raise BreezeError(
                    "合成超时，可在连接设置中增加请求超时。官方后台任务可能仍在运行，请先在控制台检查。"
                ) from None
            finally:
                self.requests.discard(generation)
            async with self.lock:
                assert self.store is not None
                capacity = self.settings()["behavior"]["history_max_mb"] * 1024 * 1024
                reserved_size = sum(
                    item["size"]
                    for key, item in self.store.items.items()
                    if max(
                        self.delivery_audio.get(key, 0), self.reading_audio.get(key, 0)
                    )
                    > time.monotonic()
                )
                if len(audio) + reserved_size > capacity:
                    raise BreezeError(
                        "待发送音频已占满成品容量，请等待发送完成或提高容量上限。"
                    )
                await self.prune_audio(
                    max(self.settings()["behavior"]["history_limit"] - 1, 0),
                    reserve_bytes=len(audio),
                )
                result = await self.io(
                    self.store.add,
                    audio,
                    extension,
                    "output",
                    clean_breeze_text(text, profile["tags"]).strip()[:60]
                    or "Breeze vocal event",
                    backend=config["backend"],
                )
                if for_delivery:
                    self.delivery_audio[result["id"]] = time.monotonic() + 3600
                try:
                    await self.prune_audio(
                        self.settings()["behavior"]["history_limit"], {result["id"]}
                    )
                except OSError as exc:
                    self.logger.warning(
                        "Breeze output saved; history cleanup failed (%s).",
                        type(exc).__name__,
                    )
                return result
        except OSError as exc:
            self.logger.error("Breeze audio storage failed (%s).", type(exc).__name__)
            raise BreezeError(
                "音频文件操作失败，请检查数据目录权限与可用空间。", code="storage"
            ) from None
        finally:
            self.active -= 1

    async def prune_audio(
        self, limit: int, preserve: set[str] | None = None, reserve_bytes: int = 0
    ) -> None:
        """Prune history under the caller's storage lock, retaining active sends.

        Args:
            limit: Number of recent generated files to retain.
            preserve: Newly created files that must survive this cleanup.
            reserve_bytes: Capacity to free before writing newly downloaded audio.
        """
        self.delivery_audio = {
            key: expiry
            for key, expiry in self.delivery_audio.items()
            if expiry > time.monotonic()
        }
        self.reading_audio = {
            key: expiry
            for key, expiry in self.reading_audio.items()
            if expiry > time.monotonic()
        }
        assert self.store is not None
        await self.io(
            self.store.prune,
            limit,
            set(self.delivery_audio) | set(self.reading_audio) | (preserve or set()),
            self.settings()["behavior"]["history_max_mb"] * 1024 * 1024 - reserve_bytes,
        )

    async def release_audio(self, identifiers: set[str]) -> None:
        """Release delivered files and apply the configured history limit.

        Args:
            identifiers: Files reserved by the completed platform send.
        """
        async with self.lock:
            for audio_id in identifiers:
                self.delivery_audio.pop(audio_id, None)
            if self.store is not None:
                try:
                    await self.prune_audio(self.settings()["behavior"]["history_limit"])
                except (OSError, BreezeError) as exc:
                    self.logger.warning(
                        "Breeze delivery cleanup failed (%s).", type(exc).__name__
                    )

    async def submit(self, operation: str, payload: dict) -> dict:
        """Start a tracked WebUI job so long requests survive bridge timeouts.

        Args:
            operation: synthesize, clone, or design.
            payload: User input for the chosen operation.

        Returns:
            A short-lived job identifier to poll through the Dashboard bridge.

        Raises:
            BreezeError: If the queue is full or the requested action is invalid.
        """
        await self.start()
        if (
            not isinstance(operation, str)
            or operation not in {"synthesize", "clone", "design"}
            or not isinstance(payload, dict)
        ):
            raise BreezeError("任务类型或参数无效。")
        if len(self.tasks) >= 8:
            raise BreezeError("任务队列已满，请等待当前任务完成。")
        config = self.settings()
        job_id = uuid.uuid4().hex
        for old_id in list(self.jobs):
            if old_id not in self.tasks and (
                len(self.jobs) >= 30
                or time.time() - self.jobs[old_id]["created"] > 3600
            ):
                del self.jobs[old_id]
        self.jobs[job_id] = {"id": job_id, "status": "running", "created": time.time()}

        async def run():
            try:
                if operation == "synthesize":
                    result = await self.synthesize(payload.get("text"), config=config)
                else:
                    result = await asyncio.wait_for(
                        self.create_preview(operation, payload, config),
                        config["behavior"]["timeout"],
                    )
                self.jobs[job_id].update(status="ready", result=result)
            except asyncio.CancelledError:
                self.jobs[job_id].update(status="cancelled", error="任务已取消。")
                raise
            except BreezeError as exc:
                self.logger.warning(
                    "Breeze job failed (operation=%s, code=%s).", operation, exc.code
                )
                self.jobs[job_id].update(status="failed", error=str(exc))
            except (TimeoutError, asyncio.TimeoutError):
                self.jobs[job_id].update(
                    status="failed",
                    error="任务超时，请检查官方控制台后再决定是否重新提交。",
                )
            except Exception as exc:
                self.logger.error("Breeze job failed (%s).", type(exc).__name__)
                self.jobs[job_id].update(
                    status="failed", error="任务失败，请检查插件数据目录和服务状态。"
                )
            finally:
                self.tasks.pop(job_id, None)

        self.tasks[job_id] = asyncio.create_task(
            run(), name=f"breezetts-{operation}-{job_id}"
        )
        return {"id": job_id}

    async def create_preview(self, operation: str, payload: dict, config: dict) -> dict:
        """Create and retain an official preview for explicit audition and saving.

        Args:
            operation: clone or design.
            payload: Preview description, language, name, and optional reference ID.
            config: Settings captured at job submission.

        Returns:
            Preview identity, verification state, and optional saved audition audio.

        Raises:
            BreezeError: If preview inputs do not satisfy the official API.
        """
        await self.start()
        if not isinstance(operation, str) or operation not in {"clone", "design"}:
            raise BreezeError("音色预览类型无效。")
        if config["backend"] != "cloud" or not config["cloud"]["api_key"]:
            raise BreezeError("请先切换并配置 BreezeBlue 官方 API。")
        for key, maximum in (("name", 80), ("language_code", 2)):
            if (
                not isinstance(payload.get(key), str)
                or not payload[key].strip()
                or not 1 <= len(payload[key]) <= maximum
            ):
                raise BreezeError("请填写音色名称和两位语言代码。")
        language = payload["language_code"]
        if len(language) != 2 or not language.isascii() or not language.isalpha():
            raise BreezeError("请填写 zh、en 等两位语言代码。")
        recording = None
        if operation == "clone":
            if type(payload.get("remove_noise", False)) is not bool:
                raise BreezeError("降噪选项须为布尔值。")
            async with self.lock:
                assert self.store is not None
                path = self.store.path(payload.get("reference_id"))
                item = self.store.items[payload["reference_id"]]
                if (
                    item["kind"] != "reference"
                    or item.get("duration", 0) < 3
                    or item["size"] > 5_000_000
                ):
                    raise BreezeError(
                        "官方克隆需要至少 3 秒、规范化后不超过 5 MB 的参考录音。"
                    )
                recording = await self.io(path.read_bytes)
        else:
            for key in ("description", "text"):
                if (
                    not isinstance(payload.get(key), str)
                    or not 3 <= len(payload[key].strip()) <= 500
                ):
                    raise BreezeError("声音描述和试听文字须为 3–500 个字符。")
        async with self.slots:
            preview_id, verification, audio = await self.client.preview(
                config["cloud"], payload, recording
            )
        result = {
            "preview_id": preview_id,
            "requires_verification": verification,
            "name": payload["name"],
            "language_code": language,
            "audio_unavailable": not verification and audio is None,
        }
        if result["audio_unavailable"]:
            self.logger.warning(
                "Breeze preview created; audition audio is unavailable."
            )
        if audio:
            async with self.lock:
                assert self.store is not None
                capacity = self.settings()["behavior"]["history_max_mb"] * 1024 * 1024
                reserved_size = sum(
                    item["size"]
                    for key, item in self.store.items.items()
                    if max(
                        self.delivery_audio.get(key, 0), self.reading_audio.get(key, 0)
                    )
                    > time.monotonic()
                )
                if len(audio[0]) + reserved_size > capacity:
                    result["audio_unavailable"] = True
                    self.logger.warning(
                        "Breeze preview created; local audition capacity is occupied."
                    )
                    return result
                try:
                    await self.prune_audio(
                        max(self.settings()["behavior"]["history_limit"] - 1, 0),
                        reserve_bytes=len(audio[0]),
                    )
                    result["audio"] = await self.io(
                        self.store.add, *audio, "preview", payload["name"]
                    )
                    await self.prune_audio(
                        self.settings()["behavior"]["history_limit"],
                        {result["audio"]["id"]},
                    )
                except OSError as exc:
                    result["audio_unavailable"] = "audio" not in result
                    self.logger.warning(
                        "Breeze preview saved; history cleanup failed (%s).",
                        type(exc).__name__,
                    )
        return result

    async def save_preview(self, payload: dict) -> dict:
        """Save an auditioned official preview as a reusable account voice.

        Args:
            payload: Preview ID, display name, and required language code.

        Returns:
            The saved official voice ID.

        Raises:
            BreezeError: If the save request or upstream response is invalid.
        """
        await self.start()
        config = self.settings()
        if not config["cloud"]["api_key"]:
            raise BreezeError("请先填写并保存官方 API Key。")
        preview_id = validate_identifier(payload.get("preview_id"), "音色预览 ID", 200)
        name, language = payload.get("name"), payload.get("language_code")
        if (
            not isinstance(preview_id, str)
            or not 1 <= len(preview_id) <= 200
            or not isinstance(name, str)
            or not 1 <= len(name) <= 80
            or not isinstance(language, str)
            or len(language) != 2
            or not language.isascii()
            or not language.isalpha()
        ):
            raise BreezeError("请提供有效的预览 ID、名称和语言代码。")
        try:
            result = await asyncio.wait_for(
                self.client.request(
                    config["cloud"],
                    "POST",
                    "voice-previews/" + quote(preview_id, safe="") + "/save",
                    json={"voice_name": name, "language_code": language},
                ),
                30,
            )
        except (TimeoutError, asyncio.TimeoutError):
            raise BreezeError(
                "保存音色超时，请先同步官方音色，确认是否已保存。"
            ) from None
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("voice_id"), str)
            or not 1 <= len(result["voice_id"]) <= 500
            or result["voice_id"] in {".", ".."}
        ):
            raise BreezeError("官方保存音色响应无效，请同步目录核对结果。")
        validate_identifier(result["voice_id"], "官方保存音色响应中的音色 ID")
        return {"voice_id": result["voice_id"]}
