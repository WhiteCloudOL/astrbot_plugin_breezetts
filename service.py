"""Coordinate settings, bounded jobs, and persistent audio operations."""

from __future__ import annotations

import asyncio
import copy
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from .client import BreezeClient
from .settings import BreezeError, validate_config
from .storage import AudioStore


class BreezeService:
    """Own plugin resources and serialize file/config mutations."""

    def __init__(self, config, root: Path, logger):
        self.config = config
        self.root = root
        self.logger = logger
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
                self.store = await self.io(AudioStore, self.root)
            await self.client.start()

    async def close(self) -> None:
        """Cancel and await owned jobs before closing the HTTP session."""
        self.closed = True
        tasks = list(set(self.tasks.values()) | self.requests)
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
        return {"config": config, "credentials": credentials, "audio": items}

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
            or any(key not in {"local", "cloud"} for key in clear_keys)
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
                elif not candidate[backend].get("api_key"):
                    candidate[backend]["api_key"] = current[backend]["api_key"]
            candidate = validate_config(candidate)
            previous = copy.deepcopy(dict(self.config))
            self.config.clear()
            self.config.update(candidate)
            try:
                await self.io(self.config.save_config)
            except Exception:
                self.config.clear()
                self.config.update(previous)
                raise BreezeError("配置保存失败，请检查数据目录写入权限。") from None
        return await self.state()

    async def connection(self) -> dict:
        """Check the selected connection without generating billable audio.

        Returns:
            Local health/model/queue information or official model identifiers.
        """
        config = self.settings()
        try:
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

    async def synthesize(self, text: str, *, config: dict | None = None) -> dict:
        """Generate and persist one audio file with bounded queueing and timeout.

        Args:
            text: Text to synthesize.
            config: Optional validated settings captured when a job was submitted.

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
            or len(text) > config["behavior"]["max_characters"]
        ):
            raise BreezeError(
                f"合成文字须为 1–{config['behavior']['max_characters']} 个字符。"
            )
        if self.active >= 8:
            raise BreezeError("插件已有 8 个请求正在处理或排队，请稍后重试。")
        self.active += 1
        reference = None
        try:
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
                    if config["backend"] == "local":
                        async with self.local_slot:
                            return await self.client.synthesize(
                                config, text.strip(), reference
                            )
                    return await self.client.synthesize(config, text.strip())

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
                result = await self.io(
                    self.store.add,
                    audio,
                    extension,
                    "output",
                    text.strip()[:60],
                    backend=config["backend"],
                )
                await self.io(self.store.prune, config["behavior"]["history_limit"])
                return result
        finally:
            self.active -= 1

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
        if config["backend"] != "cloud" or not config["cloud"]["api_key"]:
            raise BreezeError("请先切换并配置 BreezeBlue 官方 API。")
        for key, maximum in (("name", 80), ("language_code", 2)):
            if (
                not isinstance(payload.get(key), str)
                or not 1 <= len(payload[key].strip()) <= maximum
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
        }
        if audio:
            async with self.lock:
                assert self.store is not None
                result["audio"] = await self.io(
                    self.store.add, *audio, "preview", payload["name"]
                )
                await self.io(self.store.prune, config["behavior"]["history_limit"])
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
        config = self.settings()
        preview_id = payload.get("preview_id")
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
        if not isinstance(result, dict) or not isinstance(result.get("voice_id"), str):
            raise BreezeError("官方保存音色响应无效，请同步目录核对结果。")
        return {"voice_id": result["voice_id"]}
