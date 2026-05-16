"""AstrBot GPT web-auth image research plugin.

The AstrBot plugin is intentionally thin: it handles QQ commands, queueing,
image extraction, and message sending. GPT web-login state stays behind the
local broker process.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import httpx

try:  # pragma: no cover - exercised only inside AstrBot.
    from astrbot.api import AstrBotConfig, logger
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api.star import Context, Star, register
    from astrbot.core.message.components import At, Image, Plain
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:  # pragma: no cover - import fallback for local tests.
    AstrBotConfig = dict

    class _Logger:
        def warning(self, message: str) -> None:
            del message

        def info(self, message: str) -> None:
            del message

    logger = _Logger()

    class _Filter:
        @staticmethod
        def command(_name: str):
            def decorator(func):
                return func

            return decorator

    filter = _Filter()

    class AstrMessageEvent:
        pass

    class Context:
        pass

    class Star:
        def __init__(self, context: Context | None = None) -> None:
            self.context = context
            self.name = "astrbot_plugin_gpt_image_web"

    def register(*_args, **_kwargs):
        def decorator(cls):
            return cls

        return decorator

    class At:
        def __init__(self, qq: str = "") -> None:
            self.qq = qq

    class Plain:
        def __init__(self, text: str = "") -> None:
            self.text = text

    class Image:
        @classmethod
        def fromFileSystem(cls, path: str):
            return path

    def get_astrbot_data_path() -> str:
        return str(Path.home() / ".openclaw" / "astrbot-data")


BROKER_DEFAULT_URL = "http://127.0.0.1:18791"
PROVIDER_GPT_BROWSER = "gpt_browser"
PROVIDER_AI_STUDIO_BROWSER = "ai_studio_browser"
VALID_PROVIDERS = {PROVIDER_GPT_BROWSER, PROVIDER_AI_STUDIO_BROWSER}
DEFAULT_COMMAND_PROVIDERS = {
    "gptimg": PROVIDER_GPT_BROWSER,
    "gptedit": PROVIDER_GPT_BROWSER,
    "banana": PROVIDER_AI_STUDIO_BROWSER,
    "bananaedit": PROVIDER_AI_STUDIO_BROWSER,
}
MAX_REFERENCE_IMAGE_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_IMAGE_BYTES = 16 * 1024 * 1024
OUTPUT_RETENTION_SECONDS = 24 * 60 * 60
SUPPORTED_REFERENCE_MIMES = {"image/jpeg", "image/png", "image/webp"}


def _string_or_empty(value: object) -> str:
    return "" if value is None else str(value).strip()


def _normalize_group_ids(value: object) -> set[str]:
    if value in (None, ""):
        return set()
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    return {group for item in value if (group := _string_or_empty(item))}


def _read_astrbot_config(config: AstrBotConfig | dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(config, dict):
        return config
    if not hasattr(config, "get"):
        return {}
    values: dict[str, Any] = {}
    for key in (
        "enabled_groups",
        "broker_url",
        "broker_token",
        "timeout_seconds",
        "queue_max_size_per_group",
        "output_dir",
        "admin_user_ids",
        "command_providers",
    ):
        value = config.get(key, None)
        if value is not None:
            values[key] = value
    return values


def _normalize_provider(value: object, default: str) -> str:
    provider = _string_or_empty(value).lower()
    if provider == "browser":
        provider = PROVIDER_GPT_BROWSER
    return provider if provider in VALID_PROVIDERS else default


def _normalize_command_providers(value: object) -> dict[str, str]:
    result = dict(DEFAULT_COMMAND_PROVIDERS)
    if isinstance(value, dict):
        for command, provider in value.items():
            key = _string_or_empty(command).lower().lstrip("/")
            if key in result:
                result[key] = _normalize_provider(provider, result[key])
    return result


@dataclass(frozen=True)
class GptImageWebConfig:
    enabled_groups: set[str]
    broker_url: str
    broker_token: str
    timeout_seconds: float
    queue_max_size_per_group: int
    output_dir: str
    admin_user_ids: set[str]
    command_providers: dict[str, str]

    @classmethod
    def from_raw(cls, raw: AstrBotConfig | dict[str, Any] | None) -> "GptImageWebConfig":
        payload = _read_astrbot_config(raw)
        return cls(
            enabled_groups=_normalize_group_ids(payload.get("enabled_groups")),
            broker_url=_string_or_empty(payload.get("broker_url")) or BROKER_DEFAULT_URL,
            broker_token=_string_or_empty(payload.get("broker_token")),
            timeout_seconds=float(payload.get("timeout_seconds") or 300),
            queue_max_size_per_group=max(1, int(payload.get("queue_max_size_per_group") or 3)),
            output_dir=_string_or_empty(payload.get("output_dir")),
            admin_user_ids=_normalize_group_ids(payload.get("admin_user_ids")),
            command_providers=_normalize_command_providers(payload.get("command_providers")),
        )


@dataclass(frozen=True)
class PromptPolicyResult:
    allowed: bool
    reason: str = ""


@dataclass(frozen=True)
class GptImageJob:
    group_id: str
    user_id: str
    prompt: str
    mode: str
    provider: str = PROVIDER_GPT_BROWSER
    image_base64: str = ""
    mime_type: str = ""


@dataclass(frozen=True)
class GptImageResult:
    path: Path
    backend: str
    request_id: str
    mime_type: str


@dataclass(frozen=True)
class ReferenceImageResult:
    image_url: str = ""
    file_path: str = ""
    mime_type: str = ""
    error_code: str = ""


class GroupQueueFull(RuntimeError):
    pass


class BrokerClientError(RuntimeError):
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        super().__init__(safe_group_error(payload))


def _parse_command(text: str, command: str) -> str | None:
    value = _string_or_empty(text)
    lowered = value.lower()
    for prefix in (f"/{command}", command):
        if lowered == prefix:
            return None
        if lowered.startswith(prefix) and value[len(prefix) : len(prefix) + 1].isspace():
            return value[len(prefix) :].strip() or None
    return None


def parse_gptimg_command(text: str) -> str | None:
    return _parse_command(text, "gptimg")


def parse_gptedit_command(text: str) -> str | None:
    return _parse_command(text, "gptedit")


def parse_banana_command(text: str) -> str | None:
    return _parse_command(text, "banana")


def parse_bananaedit_command(text: str) -> str | None:
    return _parse_command(text, "bananaedit")


SEXUAL_TERMS = ("nsfw", "nude", "naked", "sex", "pussy", "breast", "成人", "裸体", "裸", "色情")
MINOR_TERMS = ("loli", "shota", "child", "children", "kid", "幼女", "幼", "未成年", "小学生")
REAL_PERSON_TERMS = ("real person", "celebrity", "明星", "真人")
ILLEGAL_TERMS = ("炸药", "爆炸物", "恐怖袭击", "制毒", "毒品制作")


def validate_prompt(prompt: str) -> PromptPolicyResult:
    value = _string_or_empty(prompt).lower()
    has_sexual = any(term in value for term in SEXUAL_TERMS)
    has_minor = any(term in value for term in MINOR_TERMS)
    has_real_person = any(term in value for term in REAL_PERSON_TERMS)
    if has_sexual and has_minor:
        return PromptPolicyResult(False, "拒绝未成年或疑似未成年色情内容。")
    if has_sexual and has_real_person:
        return PromptPolicyResult(False, "拒绝真实人物色情内容。")
    if any(term in value for term in ILLEGAL_TERMS):
        return PromptPolicyResult(False, "拒绝明显违法高风险内容。")
    return PromptPolicyResult(True)


class GroupBoundedSerialQueue:
    """Run jobs serially per group and reject requests beyond a bounded backlog."""

    def __init__(self, worker: Callable[[GptImageJob], Awaitable[Any]], *, max_size_per_group: int) -> None:
        self._worker = worker
        self._max_size_per_group = max(1, max_size_per_group)
        self._locks: dict[str, asyncio.Lock] = {}
        self._counts: dict[str, int] = {}
        self._counts_lock = asyncio.Lock()

    async def enqueue(self, job: GptImageJob) -> Any:
        async with self._counts_lock:
            current = self._counts.get(job.group_id, 0)
            if current >= self._max_size_per_group:
                raise GroupQueueFull("group queue is full")
            self._counts[job.group_id] = current + 1
            lock = self._locks.setdefault(job.group_id, asyncio.Lock())
        try:
            async with lock:
                return await self._worker(job)
        finally:
            async with self._counts_lock:
                next_count = self._counts.get(job.group_id, 1) - 1
                if next_count <= 0:
                    self._counts.pop(job.group_id, None)
                else:
                    self._counts[job.group_id] = next_count


_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(token['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+"),
    re.compile(r"(?i)(cookie['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+"),
    re.compile(r"sk-[A-Za-z0-9_-]+"),
)


def redact_sensitive_text(text: str) -> str:
    sanitized = _string_or_empty(text)
    for pattern in _SENSITIVE_PATTERNS:
        if pattern.pattern.startswith("sk-"):
            sanitized = pattern.sub("sk-<redacted>", sanitized)
        else:
            sanitized = pattern.sub(r"\1[REDACTED]", sanitized)
    return sanitized


def _safe_fix_text(value: object) -> str:
    text = redact_sensitive_text(_string_or_empty(value))
    text = re.sub(r"(?i)/users/[^\s,，。)）]+", "<path-redacted>", text)
    text = re.sub(r"(?i)(token=)[^\s,，。)）]+", r"\1[REDACTED]", text)
    return text


def safe_health_summary(payload: dict[str, Any]) -> str:
    active = _string_or_empty(payload.get("active_provider") or payload.get("active_backend") or "unknown")
    request_id = _string_or_empty(payload.get("request_id"))
    lines = [f"图片 broker 状态：active={active}"]
    checks = payload.get("checks") if isinstance(payload.get("checks"), list) else []
    for item in checks:
        if not isinstance(item, dict):
            continue
        provider = _string_or_empty(item.get("provider") or item.get("backend") or "unknown")
        state = "ok" if item.get("available") else "fail"
        code = _string_or_empty(item.get("code"))
        fix = _safe_fix_text(item.get("fix"))
        suffix = f"，{code}" if code else ""
        if fix:
            suffix += f"，fix={fix}"
        lines.append(f"- {provider}: {state}{suffix}")
    if request_id:
        lines.append(f"request_id: {request_id}")
    return "\n".join(lines)


def safe_group_error(payload: dict[str, Any] | BaseException) -> str:
    if isinstance(payload, BaseException):
        return f"图片生成失败：{redact_sensitive_text(str(payload))}"
    code = redact_sensitive_text(_string_or_empty(payload.get("code")) or "broker_error")
    problem = redact_sensitive_text(_string_or_empty(payload.get("problem") or payload.get("message")))
    request_id = redact_sensitive_text(_string_or_empty(payload.get("request_id")))
    suffix = f"（request_id: {request_id}）" if request_id else ""
    if problem:
        return f"图片生成失败：{code}：{problem}{suffix}"
    return f"图片生成失败：{code}{suffix}"


def _mime_extension(mime_type: str) -> str:
    value = mime_type.lower()
    if "jpeg" in value or "jpg" in value:
        return ".jpg"
    if "webp" in value:
        return ".webp"
    return ".png"


def cleanup_expired_outputs(
    output_dir: Path,
    *,
    now: float | None = None,
    retention_seconds: int = OUTPUT_RETENTION_SECONDS,
    max_deletions: int = 1,
) -> None:
    if not output_dir.is_dir():
        return
    cutoff = (time.time() if now is None else now) - retention_seconds
    expired = sorted(
        (
            path
            for path in output_dir.glob("gpt-image-*")
            if path.is_file() and path.stat().st_mtime < cutoff
        ),
        key=lambda path: path.stat().st_mtime,
    )
    for path in expired[:max(0, max_deletions)]:
        try:
            path.unlink()
        except OSError:
            logger.warning(f"[gpt-image-web] failed to remove expired output: {path.name}")


def write_broker_image(
    payload: dict[str, Any],
    output_dir: Path,
    *,
    max_output_bytes: int = MAX_OUTPUT_IMAGE_BYTES,
    now: float | None = None,
) -> Path:
    encoded = _string_or_empty(payload.get("image_base64"))
    mime_type = _string_or_empty(payload.get("mime_type")) or "image/png"
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError("broker returned invalid base64 image") from exc
    if len(image_bytes) > max_output_bytes:
        raise RuntimeError("broker returned image larger than 16 MiB")
    output_dir.mkdir(parents=True, exist_ok=True)
    cleanup_expired_outputs(output_dir, now=now)
    path = output_dir / f"gpt-image-{uuid.uuid4().hex}{_mime_extension(mime_type)}"
    path.write_bytes(image_bytes)
    return path


def _walk_components(value: Any, seen: set[int] | None = None) -> list[Any]:
    if seen is None:
        seen = set()
    if id(value) in seen:
        return []
    seen.add(id(value))
    if isinstance(value, (list, tuple, set)):
        items: list[Any] = []
        for item in value:
            items.extend(_walk_components(item, seen))
        return items
    items = [value]
    for attr in ("reply", "quote", "message", "messages", "components", "chain"):
        child = getattr(value, attr, None)
        if child is not None:
            items.extend(_walk_components(child, seen))
    return items


def extract_single_reference_image(components: list[Any]) -> ReferenceImageResult:
    images = [component for component in _walk_components(components) if isinstance(component, Image)]
    if not images:
        return ReferenceImageResult(error_code="edit_image_missing")
    if len(images) > 1:
        return ReferenceImageResult(error_code="edit_image_multiple")
    image = images[0]
    image_url = _string_or_empty(getattr(image, "url", ""))
    file_path = _string_or_empty(getattr(image, "file", ""))
    mime_type = _string_or_empty(getattr(image, "mime_type", "")) or _string_or_empty(
        getattr(image, "content_type", "")
    )
    if not image_url and not file_path:
        return ReferenceImageResult(error_code="edit_image_missing")
    return ReferenceImageResult(image_url=image_url, file_path=file_path, mime_type=mime_type or "image/png")


def _validate_reference_image(mime_type: str, image_bytes: bytes) -> None:
    if mime_type not in SUPPORTED_REFERENCE_MIMES:
        raise RuntimeError("引用图只支持 JPEG、PNG 或 WebP。")
    if len(image_bytes) > MAX_REFERENCE_IMAGE_BYTES:
        raise RuntimeError("引用图超过 8 MiB，请换一张更小的图片。")


def reference_image_error_message(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPError):
        return "引用图下载失败，请重新回复图片。"
    return f"引用图读取失败：{redact_sensitive_text(str(exc))}"


async def prepare_reference_image(reference: ReferenceImageResult, *, timeout_seconds: float) -> tuple[str, str]:
    if reference.image_url:
        async with httpx.AsyncClient(timeout=min(timeout_seconds, 30)) as client:
            async with client.stream("GET", reference.image_url) as response:
                response.raise_for_status()
                mime_type = response.headers.get("content-type", reference.mime_type).split(";", 1)[0].strip()
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_REFERENCE_IMAGE_BYTES:
                        raise RuntimeError("引用图超过 8 MiB，请换一张更小的图片。")
                image_bytes = bytes(chunks)
    elif reference.file_path:
        path = Path(reference.file_path).expanduser()
        image_bytes = path.read_bytes()
        mime_type = reference.mime_type
    else:
        raise RuntimeError("没有找到可编辑的引用图片。")
    mime_type = mime_type or "image/png"
    _validate_reference_image(mime_type, image_bytes)
    return base64.b64encode(image_bytes).decode("ascii"), mime_type


class BrokerClient:
    def __init__(self, config: GptImageWebConfig) -> None:
        self.config = config

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.config.broker_token}",
            "Content-Type": "application/json",
        }
        url = self.config.broker_url.rstrip("/") + path
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            response = await client.post(url, json=payload, headers=headers)
        try:
            body = response.json()
        except Exception:
            body = {"ok": False, "code": "broker_invalid_response", "problem": response.text[:200]}
        if response.status_code >= 400 or not body.get("ok", False):
            if "request_id" not in body:
                body["request_id"] = response.headers.get("x-request-id", "")
            raise BrokerClientError(body)
        return body

    async def generate(self, prompt: str, *, provider: str = PROVIDER_GPT_BROWSER) -> dict[str, Any]:
        return await self._post("/generate", {"prompt": prompt, "provider": provider})

    async def edit(
        self,
        prompt: str,
        *,
        image_base64: str,
        mime_type: str,
        provider: str = PROVIDER_GPT_BROWSER,
    ) -> dict[str, Any]:
        return await self._post(
            "/edit",
            {"prompt": prompt, "image_base64": image_base64, "mime_type": mime_type, "provider": provider},
        )

    async def health(self) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.config.broker_token}"}
        url = self.config.broker_url.rstrip("/") + "/health"
        async with httpx.AsyncClient(timeout=min(self.config.timeout_seconds, 30)) as client:
            response = await client.get(url, headers=headers)
        try:
            body = response.json()
        except Exception:
            body = {"ok": False, "code": "broker_invalid_response", "problem": response.text[:200]}
        if response.status_code >= 400 or not body.get("ok", False):
            if "request_id" not in body:
                body["request_id"] = response.headers.get("x-request-id", "")
            raise BrokerClientError(body)
        return body


def _default_output_dir(config: GptImageWebConfig) -> Path:
    if config.output_dir:
        return Path(config.output_dir).expanduser()
    return Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_gpt_image_web" / "images"


def _group_id(event: AstrMessageEvent) -> str:
    return _string_or_empty(event.get_group_id() or event.get_session_id())


def _stop_event(event: AstrMessageEvent) -> None:
    stopper = getattr(event, "stop_event", None)
    if callable(stopper):
        stopper()


def stop_result(result: Any) -> Any:
    stopper = getattr(result, "stop_event", None)
    if callable(stopper):
        return stopper()
    return result


def _broker_url_is_local(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


@register(
    "astrbot_plugin_gpt_image_web",
    "Codex",
    "GPT web-auth image research plugin via a local broker.",
    "0.1.0",
    "https://github.com/yurisachan16/openclaw",
)
class GptImageWebPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config_model = GptImageWebConfig.from_raw(config)
        self.client = BrokerClient(self.config_model)
        self.queue = GroupBoundedSerialQueue(
            self._run_job,
            max_size_per_group=self.config_model.queue_max_size_per_group,
        )
        if not _broker_url_is_local(self.config_model.broker_url):
            logger.warning("[gpt-image-web] broker_url should point at localhost only")

    def _enabled(self, group_id: str) -> bool:
        return _string_or_empty(group_id) in self.config_model.enabled_groups

    def _preflight_message(self) -> str | None:
        if not self.config_model.broker_token:
            return "broker token 未配置：请先在插件配置中填写 broker_token。"
        if not _broker_url_is_local(self.config_model.broker_url):
            return "broker_url 必须指向本机 127.0.0.1 或 localhost。"
        return None

    async def _run_job(self, job: GptImageJob) -> GptImageResult:
        if job.mode == "edit":
            payload = await self.client.edit(
                job.prompt,
                image_base64=job.image_base64,
                mime_type=job.mime_type,
                provider=job.provider,
            )
        else:
            payload = await self.client.generate(job.prompt, provider=job.provider)
        image_path = write_broker_image(payload, _default_output_dir(self.config_model))
        return GptImageResult(
            path=image_path,
            backend=_string_or_empty(payload.get("backend")) or "unknown",
            request_id=_string_or_empty(payload.get("request_id")),
            mime_type=_string_or_empty(payload.get("mime_type")) or "image/png",
        )

    async def _send_job(self, event: AstrMessageEvent, job: GptImageJob, *, queued_text: str):
        preflight = self._preflight_message()
        if preflight:
            yield stop_result(event.plain_result(preflight))
            return
        policy = validate_prompt(job.prompt)
        if not policy.allowed:
            yield stop_result(event.plain_result(policy.reason))
            return
        user_id = _string_or_empty(event.get_sender_id())
        yield event.plain_result(queued_text)
        _stop_event(event)
        try:
            result = await self.queue.enqueue(job)
            text = f" 图片生成完成！后端: {result.backend}，request_id: {result.request_id}"
            yield stop_result(event.chain_result([At(qq=user_id), Plain(text), Image.fromFileSystem(str(result.path))]))
        except GroupQueueFull:
            yield stop_result(event.plain_result("这个群的 GPT 生图队列已满，请稍后再试。"))
        except BrokerClientError as exc:
            logger.warning(f"[gpt-image-web] broker failed: {redact_sensitive_text(str(exc))}")
            yield stop_result(event.plain_result(safe_group_error(exc.payload)))
        except Exception as exc:
            logger.warning(f"[gpt-image-web] generation failed: {redact_sensitive_text(str(exc))}")
            yield stop_result(event.plain_result(safe_group_error(exc)))

    @filter.command("gptimg")
    async def gptimg_command(self, event: AstrMessageEvent):
        group_id = _group_id(event)
        if not self._enabled(group_id):
            return
        prompt = parse_gptimg_command(event.get_message_str())
        if not prompt:
            yield stop_result(event.plain_result("用法：/gptimg 提示词"))
            return
        user_id = _string_or_empty(event.get_sender_id())
        job = GptImageJob(
            group_id=group_id,
            user_id=user_id,
            prompt=prompt,
            mode="generate",
            provider=self.config_model.command_providers["gptimg"],
        )
        async for result in self._send_job(event, job, queued_text="已加入 GPT 生图队列，请稍候~"):
            yield result

    @filter.command("gptedit")
    async def gptedit_command(self, event: AstrMessageEvent):
        group_id = _group_id(event)
        if not self._enabled(group_id):
            return
        prompt = parse_gptedit_command(event.get_message_str())
        if not prompt:
            yield stop_result(event.plain_result("用法：回复一张图片并发送 /gptedit 改图要求"))
            return
        reference_components = [event, getattr(event, "message_obj", None), *list(event.get_messages())]
        reference = extract_single_reference_image(reference_components)
        if reference.error_code == "edit_image_missing":
            yield stop_result(event.plain_result("请回复一张图片再使用 /gptedit 改图。"))
            return
        if reference.error_code == "edit_image_multiple":
            yield stop_result(event.plain_result("/gptedit 一次只支持一张引用图。"))
            return
        try:
            image_base64, mime_type = await prepare_reference_image(
                reference,
                timeout_seconds=self.config_model.timeout_seconds,
            )
        except Exception as exc:
            yield stop_result(event.plain_result(reference_image_error_message(exc)))
            return
        user_id = _string_or_empty(event.get_sender_id())
        job = GptImageJob(
            group_id=group_id,
            user_id=user_id,
            prompt=prompt,
            mode="edit",
            provider=self.config_model.command_providers["gptedit"],
            image_base64=image_base64,
            mime_type=mime_type,
        )
        async for result in self._send_job(event, job, queued_text="已加入 GPT 改图队列，请稍候~"):
            yield result

    @filter.command("banana")
    async def banana_command(self, event: AstrMessageEvent):
        group_id = _group_id(event)
        if not self._enabled(group_id):
            return
        prompt = parse_banana_command(event.get_message_str())
        if not prompt:
            yield stop_result(event.plain_result("用法：/banana 提示词"))
            return
        user_id = _string_or_empty(event.get_sender_id())
        job = GptImageJob(
            group_id=group_id,
            user_id=user_id,
            prompt=prompt,
            mode="generate",
            provider=self.config_model.command_providers["banana"],
        )
        async for result in self._send_job(event, job, queued_text="已加入 banana 生图队列，请稍候~"):
            yield result

    @filter.command("bananaedit")
    async def bananaedit_command(self, event: AstrMessageEvent):
        group_id = _group_id(event)
        if not self._enabled(group_id):
            return
        prompt = parse_bananaedit_command(event.get_message_str())
        if not prompt:
            yield stop_result(event.plain_result("用法：回复一张图片并发送 /bananaedit 改图要求"))
            return
        reference_components = [event, getattr(event, "message_obj", None), *list(event.get_messages())]
        reference = extract_single_reference_image(reference_components)
        if reference.error_code == "edit_image_missing":
            yield stop_result(event.plain_result("请回复一张图片再使用 /bananaedit 改图。"))
            return
        if reference.error_code == "edit_image_multiple":
            yield stop_result(event.plain_result("/bananaedit 一次只支持一张引用图。"))
            return
        try:
            image_base64, mime_type = await prepare_reference_image(
                reference,
                timeout_seconds=self.config_model.timeout_seconds,
            )
        except Exception as exc:
            yield stop_result(event.plain_result(reference_image_error_message(exc)))
            return
        user_id = _string_or_empty(event.get_sender_id())
        job = GptImageJob(
            group_id=group_id,
            user_id=user_id,
            prompt=prompt,
            mode="edit",
            provider=self.config_model.command_providers["bananaedit"],
            image_base64=image_base64,
            mime_type=mime_type,
        )
        async for result in self._send_job(event, job, queued_text="已加入 banana 改图队列，请稍候~"):
            yield result

    @filter.command("imgstatus")
    async def imgstatus_command(self, event: AstrMessageEvent):
        group_id = _group_id(event)
        if not self._enabled(group_id):
            return
        user_id = _string_or_empty(event.get_sender_id())
        if user_id not in self.config_model.admin_user_ids:
            yield stop_result(event.plain_result("只有配置的维护者可以查看图片 broker 状态。"))
            return
        preflight = self._preflight_message()
        if preflight:
            yield stop_result(event.plain_result(preflight))
            return
        try:
            payload = await self.client.health()
            yield stop_result(event.plain_result(safe_health_summary(payload)))
        except BrokerClientError as exc:
            yield stop_result(event.plain_result(safe_group_error(exc.payload)))
        except Exception as exc:
            logger.warning(f"[gpt-image-web] health failed: {redact_sensitive_text(str(exc))}")
            yield stop_result(event.plain_result(safe_group_error(exc)))
