#!/usr/bin/env python3
"""Local GPT web-auth image broker for AstrBot.

The broker owns experimental web-auth backends. AstrBot plugins call this
localhost-only API with a separate broker token and never read web login state.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable, Protocol
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DOCS_URL = "https://github.com/yurisachan16/openclaw/tree/main/deploy/astrbot/plugins/astrbot_plugin_gpt_image_web"
PROVIDER_GPT_BROWSER = "gpt_browser"
PROVIDER_AI_STUDIO_BROWSER = "ai_studio_browser"
PROVIDER_OAUTH = "oauth"
PROVIDER_AUTO = "auto"
PROVIDER_ALIASES = {
    "": PROVIDER_AUTO,
    PROVIDER_AUTO: PROVIDER_AUTO,
    "browser": PROVIDER_GPT_BROWSER,
    PROVIDER_GPT_BROWSER: PROVIDER_GPT_BROWSER,
    PROVIDER_AI_STUDIO_BROWSER: PROVIDER_AI_STUDIO_BROWSER,
    PROVIDER_OAUTH: PROVIDER_OAUTH,
}
PROVIDER_CHOICES = (PROVIDER_AUTO, PROVIDER_GPT_BROWSER, PROVIDER_AI_STUDIO_BROWSER, PROVIDER_OAUTH, "browser")
MAX_INPUT_IMAGE_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_IMAGE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_EDGE = 2048
MAX_JSON_BODY_BYTES = 12 * 1024 * 1024
SUPPORTED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp"}
SERVICE_UNAVAILABLE_CODES = {
    "backend_unavailable",
    "oauth_auth_missing",
    "oauth_image_unsupported",
    "browser_profile_missing",
    "browser_playwright_missing",
    "browser_cdp_unreachable",
    "browser_cdp_no_page",
    "browser_cdp_connection_failed",
    "browser_login_required",
    "browser_profile_locked",
    "browser_composer_missing",
    "browser_edit_unchanged",
    "browser_submit_unavailable",
    "browser_upload_missing",
    "browser_upload_timeout",
    "browser_image_not_found",
    "browser_generation_failed",
    "browser_timeout",
    "browser_automation_failed",
    "browser_automation_unimplemented",
    "provider_unavailable",
    "ai_studio_login_required",
    "ai_studio_model_missing",
    "ai_studio_composer_missing",
    "ai_studio_output_not_found",
    "ai_studio_generation_failed",
    "ai_studio_timeout",
    "ai_studio_automation_failed",
}
DEFAULT_PROFILE_PATH = Path.home() / ".local" / "share" / "openclaw-gpt-image-web" / "browser-profile"
DEFAULT_OAUTH_AUTH_HOME = Path.home() / ".local" / "share" / "openclaw-gpt-image-web" / "oauth-home"
DEFAULT_BROWSER_CDP_URL = "http://127.0.0.1:18792"
DEFAULT_AI_STUDIO_PROFILE_PATH = (
    Path.home() / ".local" / "share" / "openclaw-image-web" / "ai-studio-browser-profile"
)
DEFAULT_AI_STUDIO_CDP_URL = "http://127.0.0.1:18793"
GPT_START_URL = "https://chatgpt.com/"
AI_STUDIO_START_URL = "https://aistudio.google.com/"
LOCAL_CDP_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _env_value(values: dict[str, str], new_key: str, old_key: str = "", default: str = "") -> str:
    value = values.get(new_key)
    if value not in (None, ""):
        return str(value)
    if old_key:
        value = values.get(old_key)
        if value not in (None, ""):
            return str(value)
    return default


def normalize_provider(provider: str | None) -> str:
    value = str(provider or "").strip().lower()
    return PROVIDER_ALIASES.get(value, value)


@dataclass(frozen=True)
class ProviderBrowserConfig:
    provider: str
    profile_path: Path
    connection_mode: str
    cdp_url: str
    start_url: str
    headless: bool


@dataclass(frozen=True)
class BrokerConfig:
    host: str
    port: int
    broker_token: str
    backend_mode: str
    oauth_auth_home: Path
    browser_profile_path: Path
    browser_connection_mode: str
    browser_cdp_url: str
    timeout_seconds: float
    provider_mode: str = PROVIDER_AUTO
    provider_browsers: dict[str, ProviderBrowserConfig] | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "BrokerConfig":
        values = env if env is not None else os.environ
        provider_mode = normalize_provider(
            _env_value(values, "IMAGE_WEB_PROVIDER_MODE", "GPT_IMAGE_WEB_BACKEND_MODE", PROVIDER_AUTO)
        )
        connection_mode = _env_value(
            values,
            "IMAGE_WEB_BROWSER_CONNECTION_MODE",
            "GPT_IMAGE_WEB_BROWSER_CONNECTION_MODE",
            "cdp",
        ).strip().lower()
        gpt_profile_path = Path(
            _env_value(
                values,
                "IMAGE_WEB_GPT_BROWSER_PROFILE_PATH",
                "GPT_IMAGE_WEB_BROWSER_PROFILE_PATH",
                str(DEFAULT_PROFILE_PATH),
            )
        ).expanduser()
        gpt_cdp_url = _env_value(
            values,
            "IMAGE_WEB_GPT_BROWSER_CDP_URL",
            "GPT_IMAGE_WEB_BROWSER_CDP_URL",
            DEFAULT_BROWSER_CDP_URL,
        ).strip()
        ai_studio_profile_path = Path(
            _env_value(
                values,
                "IMAGE_WEB_AI_STUDIO_BROWSER_PROFILE_PATH",
                "",
                str(DEFAULT_AI_STUDIO_PROFILE_PATH),
            )
        ).expanduser()
        ai_studio_cdp_url = _env_value(
            values,
            "IMAGE_WEB_AI_STUDIO_BROWSER_CDP_URL",
            "",
            DEFAULT_AI_STUDIO_CDP_URL,
        ).strip()
        headless = _env_value(values, "IMAGE_WEB_BROWSER_HEADLESS", "GPT_IMAGE_WEB_BROWSER_HEADLESS", "").lower() in {
            "1",
            "true",
            "yes",
        }
        provider_browsers = {
            PROVIDER_GPT_BROWSER: ProviderBrowserConfig(
                PROVIDER_GPT_BROWSER,
                gpt_profile_path,
                connection_mode,
                gpt_cdp_url,
                GPT_START_URL,
                headless,
            ),
            PROVIDER_AI_STUDIO_BROWSER: ProviderBrowserConfig(
                PROVIDER_AI_STUDIO_BROWSER,
                ai_studio_profile_path,
                connection_mode,
                ai_studio_cdp_url,
                AI_STUDIO_START_URL,
                headless,
            ),
        }
        return cls(
            host=_env_value(values, "IMAGE_WEB_HOST", "GPT_IMAGE_WEB_HOST", "127.0.0.1").strip(),
            port=int(_env_value(values, "IMAGE_WEB_PORT", "GPT_IMAGE_WEB_PORT", "18791")),
            broker_token=_env_value(values, "IMAGE_WEB_BROKER_TOKEN", "GPT_IMAGE_WEB_BROKER_TOKEN", "").strip(),
            backend_mode=provider_mode,
            oauth_auth_home=Path(
                _env_value(values, "IMAGE_WEB_OAUTH_AUTH_HOME", "GPT_IMAGE_WEB_OAUTH_AUTH_HOME", str(DEFAULT_OAUTH_AUTH_HOME))
            ).expanduser(),
            browser_profile_path=gpt_profile_path,
            browser_connection_mode=connection_mode,
            browser_cdp_url=gpt_cdp_url,
            timeout_seconds=float(_env_value(values, "IMAGE_WEB_TIMEOUT_SECONDS", "GPT_IMAGE_WEB_TIMEOUT_SECONDS", "300")),
            provider_mode=provider_mode,
            provider_browsers=provider_browsers,
        )

    def provider_browser(self, provider: str) -> ProviderBrowserConfig:
        normalized = normalize_provider(provider)
        browsers = self.provider_browsers or {}
        if normalized in browsers:
            return browsers[normalized]
        return browsers[PROVIDER_GPT_BROWSER]


@dataclass(frozen=True)
class BackendStatus:
    backend: str
    available: bool
    code: str = ""
    fix: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "provider": self.backend,
            "available": self.available,
            "code": self.code,
            "fix": self.fix,
        }


@dataclass(frozen=True)
class ImageResult:
    mime_type: str
    image_base64: str
    backend: str


class ImageBackend(Protocol):
    name: str

    def preflight(self) -> BackendStatus:
        ...

    def generate(self, prompt: str) -> ImageResult:
        ...

    def edit(self, prompt: str, *, image_base64: str, mime_type: str) -> ImageResult:
        ...


class BrowserDriver(Protocol):
    def generate(self, prompt: str) -> ImageResult:
        ...

    def edit(self, prompt: str, *, image_base64: str, mime_type: str) -> ImageResult:
        ...


@dataclass(frozen=True)
class BrowserPageLease:
    page: Any
    cleanup: Callable[[], None]


class BrokerError(RuntimeError):
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        super().__init__(payload.get("code", "broker_error"))


def request_id() -> str:
    return f"req_{uuid.uuid4().hex[:16]}"


def error_payload(
    code: str,
    *,
    problem: str,
    cause: str,
    fix: str,
    docs_url: str = DOCS_URL,
    request_id: str | None = None,
    retryable: bool = False,
) -> dict[str, Any]:
    return {
        "ok": False,
        "code": code,
        "problem": problem,
        "cause": cause,
        "fix": fix,
        "docs_url": docs_url,
        "request_id": request_id or globals()["request_id"](),
        "retryable": bool(retryable),
    }


_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(token['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+"),
    re.compile(r"(?i)(cookie['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+"),
    re.compile(r"sk-[A-Za-z0-9_-]+"),
)


def redact(text: str) -> str:
    value = "" if text is None else str(text)
    for pattern in _SENSITIVE_PATTERNS:
        if pattern.pattern.startswith("sk-"):
            value = pattern.sub("sk-<redacted>", value)
        else:
            value = pattern.sub(r"\1[REDACTED]", value)
    return value


def _playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except Exception:
        return False
    return True


def _cdp_url_for_path(base_url: str, path: str) -> str:
    return f"{str(base_url).rstrip('/')}/{path.lstrip('/')}"


def _cdp_url_parts(cdp_url: str):
    return urlparse(str(cdp_url or "").strip())


def _cdp_host(cdp_url: str) -> str:
    return (_cdp_url_parts(cdp_url).hostname or "").lower()


def _cdp_port(cdp_url: str) -> int:
    parts = _cdp_url_parts(cdp_url)
    if parts.port:
        return parts.port
    return 443 if parts.scheme == "https" else 80


def _is_remote_cdp_url(cdp_url: str) -> bool:
    host = _cdp_host(cdp_url)
    return bool(host and host not in LOCAL_CDP_HOSTS)


def browser_cdp_remote_warning(config: BrokerConfig) -> dict[str, Any] | None:
    if not _is_remote_cdp_url(config.browser_cdp_url):
        return None
    return error_payload(
        "browser_cdp_remote_warning",
        problem="browser CDP URL points to a remote host",
        cause=f"host={_cdp_host(config.browser_cdp_url)}",
        fix="only use remote CDP when you intentionally want this broker to control that browser",
        retryable=False,
    )


def _is_chatgpt_url(url: str) -> bool:
    host = (urlparse(str(url or "")).hostname or "").lower()
    return host == "chatgpt.com" or host.endswith(".chatgpt.com") or host == "chat.openai.com"


def _is_ai_studio_url(url: str) -> bool:
    host = (urlparse(str(url or "")).hostname or "").lower()
    return host == "aistudio.google.com" or host.endswith(".aistudio.google.com")


def _is_probable_uploaded_reference_src(src: str) -> bool:
    value = str(src or "")
    if "/backend-api/estuary/content" not in value:
        return False
    return "id=file-" in value


def _browser_prompt_text(prompt: str, *, has_reference_image: bool) -> str:
    clean_prompt = str(prompt or "").strip()
    if has_reference_image:
        return (
            "Edit the uploaded reference image into a new image. "
            f"Apply this change strongly and visibly: {clean_prompt}. "
            "Do not return the uploaded original, an unchanged copy, or a re-encoded copy. "
            "The final image must visibly reflect the requested edit."
        )
    return f"Create an image: {clean_prompt}"


def fetch_cdp_json(base_url: str, path: str) -> Any:
    request = Request(_cdp_url_for_path(base_url, path), headers={"Accept": "application/json"})
    with urlopen(request, timeout=2) as response:
        raw = response.read(1024 * 1024)
    return json.loads(raw.decode("utf-8"))


def _cdp_targets(base_url: str) -> list[dict[str, Any]]:
    targets = fetch_cdp_json(base_url, "/json/list")
    if not isinstance(targets, list):
        return []
    return [item for item in targets if isinstance(item, dict)]


def classify_browser_playwright_error(message: str) -> tuple[str, str]:
    normalized = str(message or "").lower()
    if (
        "user data directory is already in use" in normalized
        or "process singleton" in normalized
        or "processsingleton" in normalized
        or "profile directory" in normalized
    ):
        return "browser_profile_locked", "close the dedicated Chrome profile window, then retry"
    return "browser_automation_failed", "check the dedicated ChatGPT profile and retry"


def classify_browser_cdp_playwright_error(message: str, config: BrokerConfig) -> tuple[str, str]:
    normalized = str(message or "").lower()
    if "econnrefused" in normalized or "connection refused" in normalized or "failed to connect" in normalized:
        return (
            "browser_cdp_connection_failed",
            f"run launch-browser or check GPT_IMAGE_WEB_BROWSER_CDP_URL={config.browser_cdp_url}",
        )
    return "browser_automation_failed", "check the dedicated ChatGPT browser window and retry"


def validate_runtime_config(config: BrokerConfig) -> dict[str, Any] | None:
    if config.host not in {"127.0.0.1", "localhost", "::1"}:
        return error_payload(
            "broker_host_not_local",
            problem="broker must bind localhost only",
            cause=f"configured host is {config.host}",
            fix="set GPT_IMAGE_WEB_HOST=127.0.0.1",
            retryable=False,
        )
    if not config.broker_token:
        return error_payload(
            "broker_token_missing",
            problem="broker token is missing",
            cause="GPT_IMAGE_WEB_BROKER_TOKEN is empty",
            fix="set GPT_IMAGE_WEB_BROKER_TOKEN before starting the broker",
            retryable=False,
        )
    if normalize_provider(config.provider_mode) not in {
        PROVIDER_AUTO,
        PROVIDER_OAUTH,
        PROVIDER_GPT_BROWSER,
        PROVIDER_AI_STUDIO_BROWSER,
    }:
        return error_payload(
            "provider_mode_invalid",
            problem="provider mode is invalid",
            cause=f"provider_mode={config.provider_mode}",
            fix="set IMAGE_WEB_PROVIDER_MODE to auto, gpt_browser, or ai_studio_browser",
            retryable=False,
        )
    if config.browser_connection_mode not in {"cdp", "persistent"}:
        return error_payload(
            "browser_connection_mode_invalid",
            problem="browser connection mode is invalid",
            cause=f"browser_connection_mode={config.browser_connection_mode}",
            fix="set IMAGE_WEB_BROWSER_CONNECTION_MODE to cdp or persistent",
            retryable=False,
        )
    for provider, browser in (config.provider_browsers or {}).items():
        parts = _cdp_url_parts(browser.cdp_url)
        if browser.connection_mode == "cdp" and parts.scheme not in {"http", "https"}:
            return error_payload(
                "browser_cdp_url_invalid",
                problem="browser CDP URL is invalid",
                cause=f"{provider} cdp_url={browser.cdp_url or 'missing'}",
                fix="set provider CDP URL to an http:// or https:// DevTools endpoint",
                retryable=False,
            )
    return None


def authorize(headers: dict[str, str], config: BrokerConfig) -> dict[str, Any] | None:
    authorization = ""
    for key, value in headers.items():
        if key.lower() == "authorization":
            authorization = value
            break
    expected = f"Bearer {config.broker_token}"
    if authorization != expected:
        return error_payload(
            "broker_auth_failed",
            problem="broker authorization failed",
            cause="missing or invalid bearer token",
            fix="send Authorization: Bearer <broker_token>",
            retryable=False,
        )
    return None


def _decode_base64_image(image_base64: str) -> bytes | dict[str, Any]:
    try:
        return base64.b64decode(str(image_base64), validate=True)
    except (binascii.Error, ValueError):
        return error_payload(
            "image_base64_invalid",
            problem="image_base64 is not valid base64",
            cause="request body contains malformed image data",
            fix="send raw image bytes base64-encoded without data URL prefix",
            retryable=False,
        )


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if not data.startswith(b"\xff\xd8"):
        return None
    idx = 2
    while idx + 9 < len(data):
        if data[idx] != 0xFF:
            idx += 1
            continue
        marker = data[idx + 1]
        idx += 2
        if marker in {0xD8, 0xD9}:
            continue
        if idx + 2 > len(data):
            return None
        length = int.from_bytes(data[idx : idx + 2], "big")
        if length < 2 or idx + length > len(data):
            return None
        if marker in {0xC0, 0xC1, 0xC2, 0xC3} and length >= 7:
            height = int.from_bytes(data[idx + 3 : idx + 5], "big")
            width = int.from_bytes(data[idx + 5 : idx + 7], "big")
            return width, height
        idx += length
    return None


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 20 or not data.startswith(b"RIFF") or data[8:12] != b"WEBP":
        return None
    idx = 12
    while idx + 8 <= len(data):
        chunk_type = data[idx : idx + 4]
        chunk_size = int.from_bytes(data[idx + 4 : idx + 8], "little")
        payload_start = idx + 8
        payload_end = payload_start + chunk_size
        if payload_end > len(data):
            return None
        payload = data[payload_start:payload_end]
        if chunk_type == b"VP8X" and len(payload) >= 10:
            width = int.from_bytes(payload[4:7], "little") + 1
            height = int.from_bytes(payload[7:10], "little") + 1
            return width, height
        if chunk_type == b"VP8L" and len(payload) >= 5 and payload[0] == 0x2F:
            bits = int.from_bytes(payload[1:5], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
            return width, height
        if chunk_type == b"VP8 " and len(payload) >= 10 and payload[3:6] == b"\x9d\x01\x2a":
            width = int.from_bytes(payload[6:8], "little") & 0x3FFF
            height = int.from_bytes(payload[8:10], "little") & 0x3FFF
            return width, height
        idx = payload_end + (chunk_size % 2)
    return None


def _image_dimensions(mime_type: str, data: bytes) -> tuple[int, int] | None:
    if mime_type == "image/png":
        return _png_dimensions(data)
    if mime_type == "image/jpeg":
        return _jpeg_dimensions(data)
    if mime_type == "image/webp":
        return _webp_dimensions(data)
    return None


def validate_input_image(mime_type: str, image_base64: str) -> dict[str, Any] | None:
    normalized_mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    if normalized_mime not in SUPPORTED_IMAGE_MIMES:
        return error_payload(
            "image_mime_unsupported",
            problem="unsupported image type",
            cause=f"mime_type={normalized_mime or 'missing'}",
            fix="send one JPEG, PNG, or WebP image",
            retryable=False,
        )
    decoded = _decode_base64_image(image_base64)
    if isinstance(decoded, dict):
        return decoded
    if len(decoded) > MAX_INPUT_IMAGE_BYTES:
        return error_payload(
            "image_too_large",
            problem="input image is larger than 8 MiB",
            cause=f"decoded size is {len(decoded)} bytes",
            fix="resize or compress the image and retry",
            retryable=False,
        )
    dimensions = _image_dimensions(normalized_mime, decoded)
    if dimensions and max(dimensions) > MAX_IMAGE_EDGE:
        return error_payload(
            "image_edge_too_large",
            problem="input image is larger than 2048px on one edge",
            cause=f"dimensions are {dimensions[0]}x{dimensions[1]}",
            fix="resize the image so its longest edge is at most 2048px",
            retryable=False,
        )
    return None


SEXUAL_TERMS = ("nsfw", "nude", "naked", "sex", "pussy", "breast", "成人", "裸体", "裸", "色情")
MINOR_TERMS = ("loli", "shota", "child", "children", "kid", "幼女", "幼", "未成年", "小学生")
REAL_PERSON_TERMS = ("real person", "celebrity", "明星", "真人")
ILLEGAL_TERMS = ("炸药", "爆炸物", "恐怖袭击", "制毒", "毒品制作")


def validate_prompt(prompt: str) -> dict[str, Any] | None:
    value = str(prompt or "").strip()
    lowered = value.lower()
    if not value:
        return error_payload(
            "prompt_missing",
            problem="prompt is missing",
            cause="request prompt is empty",
            fix="send a non-empty prompt",
            retryable=False,
        )
    if len(value) > 2000:
        return error_payload(
            "prompt_too_long",
            problem="prompt is too long",
            cause=f"prompt length is {len(value)}",
            fix="keep prompts under 2000 characters",
            retryable=False,
        )
    has_sexual = any(term in lowered for term in SEXUAL_TERMS)
    has_minor = any(term in lowered for term in MINOR_TERMS)
    has_real_person = any(term in lowered for term in REAL_PERSON_TERMS)
    if has_sexual and has_minor:
        return error_payload(
            "content_rejected",
            problem="minor sexual content is rejected",
            cause="prompt matched local high-risk policy",
            fix="remove sexual content involving minors or ambiguous minors",
            retryable=False,
        )
    if has_sexual and has_real_person:
        return error_payload(
            "content_rejected",
            problem="real-person sexual content is rejected",
            cause="prompt matched local high-risk policy",
            fix="remove sexual real-person content",
            retryable=False,
        )
    if any(term in lowered for term in ILLEGAL_TERMS):
        return error_payload(
            "content_rejected",
            problem="illegal high-risk content is rejected",
            cause="prompt matched local high-risk policy",
            fix="remove illegal instructions or harmful content",
            retryable=False,
        )
    return None


class OAuthBackend:
    name = "oauth"

    def __init__(self, config: BrokerConfig) -> None:
        self.config = config

    def preflight(self) -> BackendStatus:
        auth_path = self.config.oauth_auth_home / "auth.json"
        if not auth_path.is_file():
            return BackendStatus(
                self.name,
                False,
                "oauth_auth_missing",
                "run an isolated OAuth login and set GPT_IMAGE_WEB_OAUTH_AUTH_HOME",
            )
        return BackendStatus(
            self.name,
            False,
            "oauth_image_unsupported",
            "OAuth image endpoint is not configured or proven for this broker",
        )

    def _unsupported(self) -> BrokerError:
        return BrokerError(
            error_payload(
                "oauth_image_unsupported",
                problem="OAuth backend has not proven image support",
                cause="this broker does not know a stable web-auth image endpoint",
                fix="run the research spike or use backend_mode=browser",
                retryable=False,
            )
        )

    def generate(self, prompt: str) -> ImageResult:
        del prompt
        raise self._unsupported()

    def edit(self, prompt: str, *, image_base64: str, mime_type: str) -> ImageResult:
        del prompt, image_base64, mime_type
        raise self._unsupported()


class BrowserBackend:
    def __init__(
        self,
        config: BrokerConfig,
        driver: BrowserDriver | None = None,
        *,
        provider_name: str = "browser",
    ) -> None:
        self.name = normalize_provider(provider_name)
        if self.name == PROVIDER_AUTO:
            self.name = PROVIDER_GPT_BROWSER
        self.config = config
        if driver is not None:
            self.driver = driver
        elif self.name == PROVIDER_AI_STUDIO_BROWSER:
            self.driver = AiStudioBrowserDriver(config)
        else:
            self.driver = ChatGptBrowserDriver(config)

    def preflight(self) -> BackendStatus:
        browser = self.config.provider_browser(self.name)
        if not _playwright_available():
            return BackendStatus(
                self.name,
                False,
                "browser_playwright_missing",
                "install the browser extra with Playwright before using browser mode",
            )
        if browser.connection_mode == "persistent":
            if not browser.profile_path.exists():
                return BackendStatus(
                    self.name,
                    False,
                    "browser_profile_missing",
                    f"create and log in with dedicated profile for {self.name}",
                )
            return BackendStatus(self.name, True)
        try:
            fetch_cdp_json(browser.cdp_url, "/json/version")
            targets = _cdp_targets(browser.cdp_url)
        except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            return BackendStatus(
                self.name,
                False,
                "browser_cdp_unreachable",
                f"run launch-browser --provider {self.name} ({type(exc).__name__})",
            )
        if self.name == PROVIDER_AI_STUDIO_BROWSER:
            if any(_is_ai_studio_url(str(target.get("url") or "")) for target in targets):
                return BackendStatus(self.name, True)
            return BackendStatus(
                self.name,
                True,
                "browser_cdp_no_page",
                "open an AI Studio tab or let the broker create one on the next request",
            )
        if any(_is_chatgpt_url(str(target.get("url") or "")) for target in targets):
            return BackendStatus(self.name, True)
        return BackendStatus(
            self.name,
            True,
            "browser_cdp_no_page",
            "open a ChatGPT tab or let the broker create one on the next request",
        )

    def generate(self, prompt: str) -> ImageResult:
        return self.driver.generate(prompt)

    def edit(self, prompt: str, *, image_base64: str, mime_type: str) -> ImageResult:
        return self.driver.edit(prompt, image_base64=image_base64, mime_type=mime_type)


class ChatGptBrowserDriver:
    name = PROVIDER_GPT_BROWSER

    def __init__(self, config: BrokerConfig) -> None:
        self.config = config
        self.browser_config = config.provider_browser(self.name)

    def generate(self, prompt: str) -> ImageResult:
        return self._run(prompt)

    def edit(self, prompt: str, *, image_base64: str, mime_type: str) -> ImageResult:
        suffix = ".png"
        if mime_type == "image/jpeg":
            suffix = ".jpg"
        elif mime_type == "image/webp":
            suffix = ".webp"
        decoded = _decode_base64_image(image_base64)
        if isinstance(decoded, dict):
            raise BrokerError(decoded)
        with NamedTemporaryFile(prefix="gpt-image-edit-", suffix=suffix, delete=False) as handle:
            handle.write(decoded)
            input_path = Path(handle.name)
        try:
            return self._run(prompt, input_path=input_path, input_mime_type=mime_type)
        finally:
            try:
                input_path.unlink()
            except OSError:
                pass

    @staticmethod
    def _safe_close_page(page: Any) -> None:
        try:
            page.close()
        except Exception:
            pass

    def _run(self, prompt: str, input_path: Path | None = None, input_mime_type: str = "image/png") -> ImageResult:
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise BrokerError(
                error_payload(
                    "browser_playwright_missing",
                    problem="Playwright is not available",
                    cause=type(exc).__name__,
                    fix="install project browser dependencies, then retry browser mode",
                    retryable=False,
                )
            ) from exc

        timeout_ms = max(10_000, int(self.config.timeout_seconds * 1000))
        try:
            with sync_playwright() as playwright:
                lease = self._acquire_page_lease(playwright, timeout_ms=timeout_ms)
                try:
                    page = lease.page
                    page.set_default_timeout(min(timeout_ms, 60_000))
                    self._ensure_logged_in(page)
                    self._reset_composer(page)
                    before = self._image_fingerprints(page)
                    skip_image_sha256: set[str] = set()
                    skip_image_pixel_signatures: set[str] = set()
                    if input_path is not None:
                        self._upload_reference_image(page, input_path, timeout_ms=timeout_ms)
                        input_bytes = input_path.read_bytes()
                        skip_image_sha256.add(hashlib.sha256(input_bytes).hexdigest())
                        input_signature = self._image_pixel_signature_from_bytes(page, input_bytes, input_mime_type)
                        if input_signature:
                            skip_image_pixel_signatures.add(input_signature)
                        skip_image_pixel_signatures.update(self._new_image_pixel_signatures(page, before))
                        before = self._image_fingerprints(page)
                    self._submit_prompt(
                        page,
                        prompt,
                        preserve_attachments=input_path is not None,
                        timeout_ms=timeout_ms,
                    )
                    return self._wait_for_new_image(
                        page,
                        before,
                        timeout_ms=timeout_ms,
                        allow_blob_results=input_path is None,
                        skip_image_sha256=skip_image_sha256,
                        skip_image_pixel_signatures=skip_image_pixel_signatures,
                    )
                finally:
                    lease.cleanup()
        except BrokerError:
            raise
        except PlaywrightTimeoutError as exc:
            raise BrokerError(
                error_payload(
                    "browser_timeout",
                    problem="browser image generation timed out",
                    cause=type(exc).__name__,
                    fix="check the dedicated ChatGPT profile window and retry",
                    retryable=True,
                )
            ) from exc
        except PlaywrightError as exc:
            if self.config.browser_connection_mode == "cdp":
                code, fix = classify_browser_cdp_playwright_error(str(exc), self.config)
            else:
                code, fix = classify_browser_playwright_error(str(exc))
            raise BrokerError(
                error_payload(
                    code,
                    problem="browser automation failed",
                    cause=type(exc).__name__,
                    fix=fix,
                    retryable=True,
                )
            ) from exc
        except Exception as exc:
            raise BrokerError(
                error_payload(
                    "browser_automation_failed",
                    problem="browser automation failed",
                    cause=type(exc).__name__,
                    fix="check the broker logs using the request_id",
                    retryable=True,
                )
            ) from exc

    def _acquire_page_lease(self, playwright: Any, *, timeout_ms: int) -> BrowserPageLease:
        if self.browser_config.connection_mode == "persistent":
            return self._acquire_persistent_page_lease(playwright, timeout_ms=timeout_ms)
        return self._acquire_cdp_page_lease(playwright, timeout_ms=timeout_ms)

    def _acquire_persistent_page_lease(self, playwright: Any, *, timeout_ms: int) -> BrowserPageLease:
        context = playwright.chromium.launch_persistent_context(
            str(self.browser_config.profile_path),
            channel="chrome",
            headless=self.browser_config.headless,
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(self.browser_config.start_url, wait_until="domcontentloaded", timeout=min(timeout_ms, 60_000))
        return BrowserPageLease(page=page, cleanup=context.close)

    def _acquire_cdp_page_lease(self, playwright: Any, *, timeout_ms: int) -> BrowserPageLease:
        browser = playwright.chromium.connect_over_cdp(
            self.browser_config.cdp_url,
            timeout=min(timeout_ms, 60_000),
        )
        contexts = list(getattr(browser, "contexts", []) or [])
        if contexts:
            context = contexts[0]
        else:
            context = browser.new_context()
        page = context.new_page()
        if hasattr(page, "bring_to_front"):
            page.bring_to_front()
        page.goto(self.browser_config.start_url, wait_until="domcontentloaded", timeout=min(timeout_ms, 60_000))
        return BrowserPageLease(page=page, cleanup=lambda: self._safe_close_page(page))

    def _ensure_logged_in(self, page: Any) -> None:
        body_text = (page.locator("body").inner_text(timeout=10_000) or "").lower()
        logged_out_markers = (
            ("log in" in body_text and "sign up" in body_text),
            ("登录" in body_text and "免费注册" in body_text),
            "登录以获取" in body_text,
        )
        if any(logged_out_markers) and "message chatgpt" not in body_text:
            raise BrokerError(
                error_payload(
                    "browser_login_required",
                    problem="dedicated browser profile is not logged in",
                    cause="ChatGPT showed the logged-out landing page",
                    fix="open the dedicated browser profile and log in to ChatGPT",
                    retryable=False,
                )
            )

    def _composer_locator(self, page: Any, *, timeout_ms: int = 30_000) -> Any:
        selectors = (
            "#prompt-textarea",
            "textarea[placeholder*='有问题']",
            "textarea[placeholder*='尽管问']",
            "textarea[placeholder*='Message']",
            "textarea[placeholder*='Ask']",
            "textarea:visible",
            "[contenteditable='true'][data-placeholder*='Message']",
            "[contenteditable='true'][data-placeholder*='Ask']",
            "[contenteditable='true'][data-placeholder*='有问题']",
            "[contenteditable='true'][role='textbox']",
            "[contenteditable='true']",
        )
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            for selector in selectors:
                locator = page.locator(selector).last
                try:
                    if locator.count() > 0 and locator.is_visible(timeout=1_000):
                        return locator
                except Exception:
                    continue
            page.wait_for_timeout(500)
        raise BrokerError(
            error_payload(
                "browser_composer_missing",
                problem="ChatGPT message composer was not found",
                cause="known composer selectors did not match",
                fix="open the dedicated profile, verify ChatGPT loaded normally, then retry",
                retryable=True,
            )
        )

    def _composer_text(self, composer: Any) -> str:
        try:
            return str(
                composer.evaluate(
                    """el => {
                        if ("value" in el) return el.value || "";
                        return el.innerText || el.textContent || "";
                    }"""
                )
                or ""
            )
        except Exception:
            return ""

    def _set_composer_text(self, page: Any, composer: Any, text: str) -> None:
        try:
            composer.fill(text, timeout=5_000)
            return
        except Exception:
            pass
        composer.click()
        page.keyboard.press("ControlOrMeta+A")
        page.keyboard.press("Backspace")
        if text:
            page.keyboard.insert_text(text)

    def _reset_composer(self, page: Any) -> None:
        deadline = time.monotonic() + 15
        observe_until = time.monotonic() + 2.5
        stable_since: float | None = None
        while time.monotonic() < deadline:
            composer = self._composer_locator(page, timeout_ms=3_000)
            text = self._composer_text(composer).strip()
            if text:
                self._set_composer_text(page, composer, "")
                stable_since = None
            else:
                now = time.monotonic()
                if stable_since is None:
                    stable_since = now
                if now >= observe_until and now - stable_since >= 1:
                    return
            page.wait_for_timeout(500)

    def _click_send_button(self, page: Any, *, timeout_ms: int) -> None:
        selectors = (
            "button[data-testid='send-button']",
            "button[aria-label*='发送']",
            "button[aria-label*='Send']",
            "button[aria-label*='send']",
        )
        deadline = time.monotonic() + min(timeout_ms / 1000, 120)
        saw_button = False
        while time.monotonic() < deadline:
            for selector in selectors:
                button = page.locator(selector).last
                try:
                    if button.count() <= 0 or not button.is_visible(timeout=1_000):
                        continue
                    saw_button = True
                    if button.is_enabled(timeout=1_000):
                        button.click()
                        return
                except Exception:
                    continue
            page.wait_for_timeout(500)
        if not saw_button:
            try:
                page.keyboard.press("Enter")
                return
            except Exception:
                pass
        raise BrokerError(
            error_payload(
                "browser_submit_unavailable",
                problem="ChatGPT send button did not become available",
                cause="send button stayed disabled after prompt and upload",
                fix="wait for the reference image upload to finish, then retry /gptedit",
                retryable=True,
            )
        )

    def _submit_prompt(
        self,
        page: Any,
        prompt: str,
        *,
        preserve_attachments: bool = False,
        timeout_ms: int = 300_000,
    ) -> None:
        composer = self._composer_locator(page)
        text = _browser_prompt_text(prompt, has_reference_image=preserve_attachments)
        if preserve_attachments:
            composer.click()
            page.keyboard.insert_text(text)
        else:
            self._set_composer_text(page, composer, text)
        self._click_send_button(page, timeout_ms=timeout_ms)

    def _upload_reference_image(self, page: Any, input_path: Path, *, timeout_ms: int) -> None:
        before = self._image_fingerprints(page)
        file_inputs = page.locator("input[type='file']")
        try:
            if file_inputs.count() > 0:
                file_inputs.first.set_input_files(str(input_path))
                self._wait_for_reference_upload(page, before, timeout_ms=timeout_ms)
                return
        except BrokerError:
            raise
        except Exception:
            pass
        buttons = page.locator("button")
        for label in ("Attach", "Upload", "Add photos", "添加", "上传"):
            try:
                button = buttons.filter(has_text=label).first
                if button.count() > 0 and button.is_visible(timeout=1_000):
                    button.click()
                    page.locator("input[type='file']").first.set_input_files(str(input_path))
                    self._wait_for_reference_upload(page, before, timeout_ms=timeout_ms)
                    return
            except BrokerError:
                raise
            except Exception:
                continue
        raise BrokerError(
            error_payload(
                "browser_upload_missing",
                problem="ChatGPT image upload control was not found",
                cause="known upload selectors did not match",
                fix="open ChatGPT in the dedicated profile and verify image upload is available",
                retryable=True,
            )
        )

    def _wait_for_reference_upload(self, page: Any, before: set[str], *, timeout_ms: int) -> None:
        deadline = time.monotonic() + min(timeout_ms / 1000, 60)
        stable_since: float | None = None
        while time.monotonic() < deadline:
            previews = page.eval_on_selector_all(
                "img",
                """imgs => imgs.map(img => ({
                    src: img.currentSrc || img.src || "",
                    width: img.naturalWidth || img.width || 0,
                    height: img.naturalHeight || img.height || 0
                })).filter(item => item.src && item.width >= 32 && item.height >= 32)""",
            )
            has_new_preview = any(str(item.get("src") or "") not in before for item in previews)
            body_text = (page.locator("body").inner_text(timeout=2_000) or "").lower()
            upload_busy = any(
                marker in body_text
                for marker in (
                    "uploading",
                    "processing",
                    "正在上传",
                    "上传中",
                    "正在处理",
                    "处理中",
                )
            )
            if has_new_preview and not upload_busy:
                if stable_since is None:
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= 1:
                    return
            else:
                stable_since = None
            page.wait_for_timeout(500)
        raise BrokerError(
            error_payload(
                "browser_upload_timeout",
                problem="reference image upload did not finish",
                cause="upload preview was not ready before timeout",
                fix="retry /gptedit after ChatGPT image upload becomes available",
                retryable=True,
            )
        )

    def _image_fingerprints(self, page: Any) -> set[str]:
        return set(
            page.eval_on_selector_all(
                "img",
                """imgs => imgs
                    .map(img => img.currentSrc || img.src || "")
                    .filter(Boolean)""",
            )
        )

    def _new_image_pixel_signatures(self, page: Any, before: set[str]) -> set[str]:
        signatures: set[str] = set()
        candidates = page.eval_on_selector_all(
            "img",
            """imgs => imgs.map(img => ({
                src: img.currentSrc || img.src || "",
                width: img.naturalWidth || img.width || 0,
                height: img.naturalHeight || img.height || 0
            })).filter(item => item.src && item.width >= 256 && item.height >= 256)""",
        )
        for item in candidates:
            src = str(item.get("src") or "")
            if not src or src in before:
                continue
            signature = self._image_pixel_signature_from_src(page, src)
            if signature:
                signatures.add(signature)
        return signatures

    def _image_pixel_signature_from_src(self, page: Any, src: str) -> str:
        try:
            return str(
                page.evaluate(
                    """async (src) => {
                        const response = await fetch(src);
                        const blob = await response.blob();
                        const bitmap = await createImageBitmap(blob);
                        const size = 128;
                        const canvas = document.createElement("canvas");
                        canvas.width = size;
                        canvas.height = size;
                        const ctx = canvas.getContext("2d", { willReadFrequently: true });
                        ctx.fillStyle = "white";
                        ctx.fillRect(0, 0, size, size);
                        ctx.drawImage(bitmap, 0, 0, size, size);
                        if (bitmap.close) bitmap.close();
                        const data = ctx.getImageData(0, 0, size, size).data;
                        let hash = 2166136261;
                        for (let i = 0; i < data.length; i += 1) {
                            hash ^= data[i];
                            hash = Math.imul(hash, 16777619) >>> 0;
                        }
                        return `${size}x${size}:${hash.toString(16).padStart(8, "0")}`;
                    }""",
                    src,
                )
                or ""
            )
        except Exception:
            return ""

    def _image_pixel_signature_from_bytes(self, page: Any, image_bytes: bytes, mime_type: str) -> str:
        try:
            from io import BytesIO

            from PIL import Image

            with Image.open(BytesIO(image_bytes)) as image:
                resized = image.convert("RGBA").resize((128, 128))
                data = resized.tobytes()
            hash_value = 2166136261
            for byte in data:
                hash_value ^= byte
                hash_value = (hash_value * 16777619) & 0xFFFFFFFF
            return f"128x128:{hash_value:08x}"
        except Exception:
            pass
        normalized_mime = str(mime_type or "image/png").split(";", 1)[0].strip().lower() or "image/png"
        if normalized_mime not in SUPPORTED_IMAGE_MIMES:
            normalized_mime = "image/png"
        encoded = base64.b64encode(image_bytes).decode("ascii")
        return self._image_pixel_signature_from_src(page, f"data:{normalized_mime};base64,{encoded}")

    def _wait_for_new_image(
        self,
        page: Any,
        before: set[str],
        *,
        timeout_ms: int,
        allow_blob_results: bool = True,
        skip_image_sha256: set[str] | None = None,
        skip_image_pixel_signatures: set[str] | None = None,
    ) -> ImageResult:
        deadline = time.monotonic() + timeout_ms / 1000
        last_src = ""
        stable_src = ""
        stable_since: float | None = None
        unchanged_seen = False
        while time.monotonic() < deadline:
            candidates = page.eval_on_selector_all(
                "img",
                """imgs => imgs.map(img => ({
                    src: img.currentSrc || img.src || "",
                    width: img.naturalWidth || img.width || 0,
                    height: img.naturalHeight || img.height || 0,
                    alt: img.alt || ""
                })).filter(item => item.src && item.width >= 256 && item.height >= 256)""",
            )
            for item in reversed(candidates):
                src = str(item.get("src") or "")
                if not src or src in before or _is_probable_uploaded_reference_src(src):
                    continue
                if not allow_blob_results and src.startswith("blob:"):
                    continue
                last_src = src
                if src != stable_src:
                    stable_src = src
                    stable_since = time.monotonic()
                elif stable_since is not None and time.monotonic() - stable_since >= 1:
                    if skip_image_pixel_signatures:
                        signature = self._image_pixel_signature_from_src(page, src)
                        if signature and signature in skip_image_pixel_signatures:
                            unchanged_seen = True
                            before.add(src)
                            stable_src = ""
                            stable_since = None
                            continue
                    try:
                        result = self._image_result_from_src(page, src)
                    except Exception:
                        before.add(src)
                        stable_src = ""
                        stable_since = None
                        continue
                    decoded = b""
                    if skip_image_sha256 or skip_image_pixel_signatures:
                        decoded = base64.b64decode(result.image_base64)
                    if skip_image_sha256:
                        if hashlib.sha256(decoded).hexdigest() in skip_image_sha256:
                            unchanged_seen = True
                            before.add(src)
                            stable_src = ""
                            stable_since = None
                            continue
                    if skip_image_pixel_signatures:
                        result_signature = self._image_pixel_signature_from_bytes(page, decoded, result.mime_type)
                        if result_signature and result_signature in skip_image_pixel_signatures:
                            unchanged_seen = True
                            before.add(src)
                            stable_src = ""
                            stable_since = None
                            continue
                    return result
                break
            error_text = (page.locator("body").inner_text(timeout=2_000) or "").lower()
            if "something went wrong" in error_text or "try again later" in error_text:
                raise BrokerError(
                    error_payload(
                        "browser_generation_failed",
                        problem="ChatGPT reported a generation error",
                        cause="generation error text appeared in the page",
                        fix="retry later or check the dedicated ChatGPT session",
                        retryable=True,
                    )
                )
            page.wait_for_timeout(2_000)
        if unchanged_seen:
            raise BrokerError(
                error_payload(
                    "browser_edit_unchanged",
                    problem="ChatGPT returned an unchanged copy of the reference image",
                    cause="candidate image matched the uploaded reference pixels",
                    fix="retry /gptedit with a stronger edit request or check the dedicated ChatGPT tab",
                    retryable=True,
                )
            )
        raise BrokerError(
            error_payload(
                "browser_image_not_found",
                problem="browser backend did not find a generated image",
                cause=f"last image source seen: {'present' if last_src else 'none'}",
                fix="check whether ChatGPT generated an image or returned a text-only response",
                retryable=True,
            )
        )

    def _image_result_from_src(self, page: Any, src: str) -> ImageResult:
        payload = page.evaluate(
            """async (src) => {
                const response = await fetch(src);
                const blob = await response.blob();
                const buffer = await blob.arrayBuffer();
                const bytes = Array.from(new Uint8Array(buffer));
                return { mimeType: blob.type || response.headers.get("content-type") || "image/png", bytes };
            }""",
            src,
        )
        image_bytes = bytes(payload.get("bytes") or [])
        mime_type = str(payload.get("mimeType") or "image/png").split(";", 1)[0].strip() or "image/png"
        if mime_type not in SUPPORTED_IMAGE_MIMES:
            mime_type = "image/png"
        return ImageResult(mime_type, base64.b64encode(image_bytes).decode("ascii"), self.name)


class AiStudioBrowserDriver:
    name = PROVIDER_AI_STUDIO_BROWSER

    def __init__(self, config: BrokerConfig) -> None:
        self.config = config
        self.browser_config = config.provider_browser(self.name)

    def generate(self, prompt: str) -> ImageResult:
        return self._run(prompt)

    def edit(self, prompt: str, *, image_base64: str, mime_type: str) -> ImageResult:
        del prompt, image_base64, mime_type
        raise BrokerError(
            error_payload(
                "ai_studio_edit_unsupported",
                problem="AI Studio image edit is not enabled yet",
                cause="the AI Studio v1 driver only supports text-to-image generation",
                fix="use /banana for generation or /gptedit for reference image edits",
                retryable=False,
            )
        )

    def _run(self, prompt: str) -> ImageResult:
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise BrokerError(
                error_payload(
                    "browser_playwright_missing",
                    problem="Playwright is not available",
                    cause=type(exc).__name__,
                    fix="install project browser dependencies, then retry AI Studio browser mode",
                    retryable=False,
                )
            ) from exc

        timeout_ms = max(10_000, int(self.config.timeout_seconds * 1000))
        try:
            with sync_playwright() as playwright:
                lease = self._acquire_page_lease(playwright, timeout_ms=timeout_ms)
                try:
                    page = lease.page
                    page.set_default_timeout(min(timeout_ms, 60_000))
                    self._ensure_logged_in(page)
                    self._ensure_model_available(page)
                    before = self._image_fingerprints(page)
                    self._submit_prompt(page, prompt, timeout_ms=timeout_ms)
                    return self._wait_for_new_image(page, before, timeout_ms=timeout_ms)
                finally:
                    lease.cleanup()
        except BrokerError:
            raise
        except PlaywrightTimeoutError as exc:
            raise BrokerError(
                error_payload(
                    "ai_studio_timeout",
                    problem="AI Studio image generation timed out",
                    cause=type(exc).__name__,
                    fix="check the dedicated AI Studio profile and retry",
                    retryable=True,
                )
            ) from exc
        except PlaywrightError as exc:
            raise BrokerError(
                error_payload(
                    "ai_studio_automation_failed",
                    problem="AI Studio browser automation failed",
                    cause=type(exc).__name__,
                    fix="run doctor-browser --provider ai_studio_browser",
                    retryable=True,
                )
            ) from exc

    @staticmethod
    def _safe_close_page(page: Any) -> None:
        try:
            page.close()
        except Exception:
            pass

    def _acquire_page_lease(self, playwright: Any, *, timeout_ms: int) -> BrowserPageLease:
        if self.browser_config.connection_mode == "persistent":
            context = playwright.chromium.launch_persistent_context(
                str(self.browser_config.profile_path),
                channel="chrome",
                headless=self.browser_config.headless,
                accept_downloads=True,
                viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(self.browser_config.start_url, wait_until="domcontentloaded", timeout=min(timeout_ms, 60_000))
            return BrowserPageLease(page=page, cleanup=context.close)
        browser = playwright.chromium.connect_over_cdp(
            self.browser_config.cdp_url,
            timeout=min(timeout_ms, 60_000),
        )
        contexts = list(getattr(browser, "contexts", []) or [])
        context = contexts[0] if contexts else browser.new_context()
        page = context.new_page()
        if hasattr(page, "bring_to_front"):
            page.bring_to_front()
        page.goto(self.browser_config.start_url, wait_until="domcontentloaded", timeout=min(timeout_ms, 60_000))
        return BrowserPageLease(page=page, cleanup=lambda: self._safe_close_page(page))

    def _ensure_logged_in(self, page: Any) -> None:
        body_text = (page.locator("body").inner_text(timeout=10_000) or "").lower()
        logged_out = (
            "sign in" in body_text
            or "log in" in body_text
            or "登录" in body_text
            or "google account" in body_text
        )
        if logged_out and "prompt" not in body_text and "generate" not in body_text:
            raise BrokerError(
                error_payload(
                    "ai_studio_login_required",
                    problem="dedicated AI Studio profile is not logged in",
                    cause="AI Studio showed a logged-out page",
                    fix="run launch-browser --provider ai_studio_browser and log in",
                    retryable=False,
                )
            )

    def _ensure_model_available(self, page: Any) -> None:
        body_text = (page.locator("body").inner_text(timeout=10_000) or "").lower()
        model_markers = ("nano banana", "banana", "gemini-3.1-flash-image", "image generation")
        if not any(marker in body_text for marker in model_markers):
            raise BrokerError(
                error_payload(
                    "ai_studio_model_missing",
                    problem="Nano Banana image model was not found in AI Studio",
                    cause="known model markers did not appear on the page",
                    fix="open AI Studio and select the Nano Banana image model",
                    retryable=True,
                )
            )

    def _composer_locator(self, page: Any, *, timeout_ms: int = 30_000) -> Any:
        selectors = (
            "textarea[placeholder*='prompt']",
            "textarea[aria-label*='prompt']",
            "textarea:visible",
            "[contenteditable='true'][role='textbox']",
            "[contenteditable='true']",
        )
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            for selector in selectors:
                locator = page.locator(selector).last
                try:
                    if locator.count() > 0 and locator.is_visible(timeout=1_000):
                        return locator
                except Exception:
                    continue
            page.wait_for_timeout(500)
        raise BrokerError(
            error_payload(
                "ai_studio_composer_missing",
                problem="AI Studio prompt composer was not found",
                cause="known composer selectors did not match",
                fix="open the dedicated AI Studio profile and verify the model page loaded",
                retryable=True,
            )
        )

    def _submit_prompt(self, page: Any, prompt: str, *, timeout_ms: int) -> None:
        composer = self._composer_locator(page)
        try:
            composer.fill(prompt, timeout=5_000)
        except Exception:
            composer.click()
            page.keyboard.press("ControlOrMeta+A")
            page.keyboard.press("Backspace")
            page.keyboard.insert_text(prompt)
        deadline = time.monotonic() + min(timeout_ms / 1000, 120)
        selectors = (
            "button[aria-label*='Run']",
            "button[aria-label*='Generate']",
            "button:has-text('Run')",
            "button:has-text('Generate')",
            "button:has-text('运行')",
            "button:has-text('生成')",
        )
        while time.monotonic() < deadline:
            for selector in selectors:
                button = page.locator(selector).last
                try:
                    if button.count() > 0 and button.is_visible(timeout=1_000) and button.is_enabled(timeout=1_000):
                        button.click()
                        return
                except Exception:
                    continue
            page.wait_for_timeout(500)
        raise BrokerError(
            error_payload(
                "ai_studio_submit_unavailable",
                problem="AI Studio generate button did not become available",
                cause="known submit selectors did not match or stayed disabled",
                fix="check the dedicated AI Studio tab and retry",
                retryable=True,
            )
        )

    def _image_fingerprints(self, page: Any) -> set[str]:
        return set(
            page.eval_on_selector_all(
                "img",
                """imgs => imgs
                    .map(img => img.currentSrc || img.src || "")
                    .filter(Boolean)""",
            )
        )

    def _wait_for_new_image(self, page: Any, before: set[str], *, timeout_ms: int) -> ImageResult:
        deadline = time.monotonic() + timeout_ms / 1000
        stable_src = ""
        stable_since: float | None = None
        while time.monotonic() < deadline:
            candidates = page.eval_on_selector_all(
                "img",
                """imgs => imgs.map(img => ({
                    src: img.currentSrc || img.src || "",
                    width: img.naturalWidth || img.width || 0,
                    height: img.naturalHeight || img.height || 0
                })).filter(item => item.src && item.width >= 256 && item.height >= 256)""",
            )
            for item in reversed(candidates):
                src = str(item.get("src") or "")
                if not src or src in before:
                    continue
                if src != stable_src:
                    stable_src = src
                    stable_since = time.monotonic()
                elif stable_since is not None and time.monotonic() - stable_since >= 1:
                    return self._image_result_from_src(page, src)
                break
            body_text = (page.locator("body").inner_text(timeout=2_000) or "").lower()
            if any(marker in body_text for marker in ("quota", "rate limit", "try again later", "blocked", "refused")):
                raise BrokerError(
                    error_payload(
                        "ai_studio_generation_failed",
                        problem="AI Studio reported a generation failure",
                        cause="quota, refusal, or retry text appeared in the page",
                        fix="check the AI Studio tab, quota, and prompt policy",
                        retryable=True,
                    )
                )
            page.wait_for_timeout(1_000)
        raise BrokerError(
            error_payload(
                "ai_studio_output_not_found",
                problem="AI Studio did not expose a generated image",
                cause="no new image candidate appeared before timeout",
                fix="check whether AI Studio generated an image or returned an error",
                retryable=True,
            )
        )

    def _image_result_from_src(self, page: Any, src: str) -> ImageResult:
        payload = page.evaluate(
            """async (src) => {
                const response = await fetch(src);
                const blob = await response.blob();
                const buffer = await blob.arrayBuffer();
                const bytes = Array.from(new Uint8Array(buffer));
                return { mimeType: blob.type || response.headers.get("content-type") || "image/png", bytes };
            }""",
            src,
        )
        image_bytes = bytes(payload.get("bytes") or [])
        mime_type = str(payload.get("mimeType") or "image/png").split(";", 1)[0].strip() or "image/png"
        if mime_type not in SUPPORTED_IMAGE_MIMES:
            mime_type = "image/png"
        return ImageResult(mime_type, base64.b64encode(image_bytes).decode("ascii"), self.name)


class ImageBroker:
    def __init__(self, config: BrokerConfig, backends: list[ImageBackend] | None = None) -> None:
        self.config = config
        self.backends = backends or [
            OAuthBackend(config),
            BrowserBackend(config, provider_name=PROVIDER_GPT_BROWSER),
            BrowserBackend(config, provider_name=PROVIDER_AI_STUDIO_BROWSER),
        ]
        self._provider_locks: dict[str, threading.Lock] = {}

    def checks(self) -> list[BackendStatus]:
        return [backend.preflight() for backend in self.backends]

    def active_backend_name(self) -> str:
        for status in self.checks():
            if status.available:
                return status.backend
        return ""

    def _candidate_backends(self, provider: str | None = None) -> list[ImageBackend]:
        normalized = normalize_provider(provider) if provider else normalize_provider(self.config.provider_mode)
        if normalized not in {PROVIDER_AUTO, PROVIDER_OAUTH, PROVIDER_GPT_BROWSER, PROVIDER_AI_STUDIO_BROWSER}:
            raise BrokerError(
                error_payload(
                    "provider_invalid",
                    problem="image provider is invalid",
                    cause=f"provider={provider}",
                    fix="use gpt_browser, ai_studio_browser, or auto",
                    retryable=False,
                )
            )
        if normalized == PROVIDER_AUTO:
            return self.backends
        return [backend for backend in self.backends if normalize_provider(backend.name) == normalized]

    def _available_candidates(self, provider: str | None = None) -> list[ImageBackend]:
        candidates = []
        for backend in self._candidate_backends(provider):
            if backend.preflight().available:
                candidates.append(backend)
        return candidates

    def _no_backend_error(self, provider: str | None = None) -> BrokerError:
        checks = [status.as_dict() for status in self.checks()]
        normalized = normalize_provider(provider) if provider else normalize_provider(self.config.provider_mode)
        if normalized != PROVIDER_AUTO:
            status = next((item for item in self.checks() if normalize_provider(item.backend) == normalized), None)
            if status is not None:
                raise BrokerError(
                    error_payload(
                        status.code or f"{status.backend}_unavailable",
                        problem=f"{status.backend} backend is unavailable",
                        cause=status.code or "backend preflight failed",
                        fix=status.fix or "run /health and configure the backend",
                        retryable=True,
                    )
                )
        return BrokerError(
            error_payload(
                "provider_unavailable",
                problem="no image provider is available",
                cause=json.dumps(checks, ensure_ascii=False),
                fix="run /health, configure a dedicated browser profile, then retry",
                retryable=True,
            )
        )

    def _generate_with_backend(self, backend: ImageBackend, prompt: str) -> ImageResult:
        if normalize_provider(backend.name) in {PROVIDER_GPT_BROWSER, PROVIDER_AI_STUDIO_BROWSER}:
            lock = self._provider_locks.setdefault(normalize_provider(backend.name), threading.Lock())
            with lock:
                return backend.generate(prompt)
        return backend.generate(prompt)

    def _edit_with_backend(self, backend: ImageBackend, prompt: str, *, image_base64: str, mime_type: str) -> ImageResult:
        if normalize_provider(backend.name) in {PROVIDER_GPT_BROWSER, PROVIDER_AI_STUDIO_BROWSER}:
            lock = self._provider_locks.setdefault(normalize_provider(backend.name), threading.Lock())
            with lock:
                return backend.edit(prompt, image_base64=image_base64, mime_type=mime_type)
        return backend.edit(prompt, image_base64=image_base64, mime_type=mime_type)

    def generate(self, prompt: str, *, provider: str | None = None) -> ImageResult:
        prompt_error = validate_prompt(prompt)
        if prompt_error:
            raise BrokerError(prompt_error)
        for backend in self._available_candidates(provider):
            return self._generate_with_backend(backend, prompt)
        raise self._no_backend_error(provider)

    def edit(self, prompt: str, *, image_base64: str, mime_type: str, provider: str | None = None) -> ImageResult:
        prompt_error = validate_prompt(prompt)
        if prompt_error:
            raise BrokerError(prompt_error)
        image_error = validate_input_image(mime_type, image_base64)
        if image_error:
            raise BrokerError(image_error)
        for backend in self._available_candidates(provider):
            return self._edit_with_backend(backend, prompt, image_base64=image_base64, mime_type=mime_type)
        raise self._no_backend_error(provider)


def success_payload(result: ImageResult, *, request_id: str | None = None) -> dict[str, Any]:
    decoded = _decode_base64_image(result.image_base64)
    if isinstance(decoded, dict):
        raise BrokerError(decoded)
    if len(decoded) > MAX_OUTPUT_IMAGE_BYTES:
        raise BrokerError(
            error_payload(
                "output_image_too_large",
                problem="backend returned image larger than 16 MiB",
                cause=f"decoded size is {len(decoded)} bytes",
                fix="lower output quality or image size and retry",
                retryable=False,
            )
        )
    return {
        "ok": True,
        "image_base64": result.image_base64,
        "mime_type": result.mime_type,
        "backend": result.backend,
        "request_id": request_id or globals()["request_id"](),
    }


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    if payload.get("request_id"):
        handler.send_header("X-Request-ID", str(payload["request_id"]))
    handler.end_headers()
    handler.wfile.write(body)


def http_status_for_error(payload: dict[str, Any]) -> int:
    code = str(payload.get("code") or "")
    if code in SERVICE_UNAVAILABLE_CODES or payload.get("retryable") is True:
        return 503
    return 400


def make_handler(config: BrokerConfig):
    broker = ImageBroker(config)

    class Handler(BaseHTTPRequestHandler):
        server_version = "GptImageWebBroker/0.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
            sys.stderr.write("[gpt-image-web-broker] " + redact(format % args) + "\n")

        def _auth_error(self) -> dict[str, Any] | None:
            return authorize(dict(self.headers), config)

        def _read_json(self) -> dict[str, Any] | dict[str, Any]:
            length = int(self.headers.get("content-length") or 0)
            if length > MAX_JSON_BODY_BYTES:
                raise BrokerError(
                    error_payload(
                        "request_body_too_large",
                        problem="request body is too large",
                        cause=f"content-length is {length} bytes",
                        fix="send a JSON body under 12 MiB",
                        retryable=False,
                    )
                )
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                raise BrokerError(
                    error_payload(
                        "json_invalid",
                        problem="request body is not valid JSON",
                        cause="JSON parser failed",
                        fix="send Content-Type application/json with a JSON object",
                        retryable=False,
                    )
                )
            if not isinstance(payload, dict):
                raise BrokerError(
                    error_payload(
                        "json_invalid",
                        problem="request body must be a JSON object",
                        cause="top-level JSON value was not an object",
                        fix="send a JSON object",
                        retryable=False,
                    )
                )
            return payload

        def do_GET(self) -> None:
            auth_error = self._auth_error()
            if auth_error:
                _json_response(self, 401, auth_error)
                return
            if self.path.rstrip("/") != "/health":
                _json_response(
                    self,
                    404,
                    error_payload(
                        "not_found",
                        problem="unknown endpoint",
                        cause=f"path={self.path}",
                        fix="use /health, /generate, or /edit",
                        retryable=False,
                    ),
                )
                return
            checks = [status.as_dict() for status in broker.checks()]
            _json_response(
                self,
                200,
                {
                    "ok": True,
                    "active_backend": broker.active_backend_name(),
                    "active_provider": broker.active_backend_name(),
                    "checks": checks,
                    "request_id": request_id(),
                },
            )

        def do_POST(self) -> None:
            auth_error = self._auth_error()
            if auth_error:
                _json_response(self, 401, auth_error)
                return
            try:
                payload = self._read_json()
                rid = request_id()
                if self.path.rstrip("/") == "/generate":
                    result = broker.generate(
                        str(payload.get("prompt") or ""),
                        provider=str(payload.get("provider") or ""),
                    )
                    _json_response(self, 200, success_payload(result, request_id=rid))
                elif self.path.rstrip("/") == "/edit":
                    result = broker.edit(
                        str(payload.get("prompt") or ""),
                        image_base64=str(payload.get("image_base64") or ""),
                        mime_type=str(payload.get("mime_type") or ""),
                        provider=str(payload.get("provider") or ""),
                    )
                    _json_response(self, 200, success_payload(result, request_id=rid))
                else:
                    _json_response(
                        self,
                        404,
                        error_payload(
                            "not_found",
                            problem="unknown endpoint",
                            cause=f"path={self.path}",
                            fix="use /health, /generate, or /edit",
                            retryable=False,
                        ),
                    )
            except BrokerError as exc:
                _json_response(self, http_status_for_error(exc.payload), exc.payload)
            except Exception as exc:
                _json_response(
                    self,
                    500,
                    error_payload(
                        "broker_internal_error",
                        problem="broker hit an unexpected error",
                        cause=redact(str(exc))[:160],
                        fix="check broker logs using the request_id",
                        retryable=True,
                    ),
                )

    return Handler


def run_serve(args: argparse.Namespace) -> int:
    config = BrokerConfig.from_env()
    if args.host:
        config = replace(config, host=args.host)
    if args.port:
        config = replace(config, port=args.port)
    config_error = validate_runtime_config(config)
    if config_error:
        print(json.dumps(config_error, ensure_ascii=False))
        return 2
    server = ThreadingHTTPServer((config.host, config.port), make_handler(config))
    print(json.dumps({"ok": True, "url": f"http://{config.host}:{config.port}", "request_id": request_id()}))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def run_health(_args: argparse.Namespace) -> int:
    config = BrokerConfig.from_env()
    config_error = validate_runtime_config(config)
    if config_error:
        _print_json(config_error)
        return 2
    broker = ImageBroker(config)
    _print_json(
        {
            "ok": True,
            "active_backend": broker.active_backend_name(),
            "active_provider": broker.active_backend_name(),
            "checks": [status.as_dict() for status in broker.checks()],
            "request_id": request_id(),
        }
    )
    return 0


def run_test_generate(args: argparse.Namespace) -> int:
    env_config = BrokerConfig.from_env()
    provider = normalize_provider(getattr(args, "provider", "") or getattr(args, "backend", "") or PROVIDER_AUTO)
    config = replace(env_config, backend_mode=provider, provider_mode=provider)
    config_error = validate_runtime_config(config)
    if config_error:
        _print_json(config_error)
        return 2
    broker = ImageBroker(config)
    started = time.monotonic()
    try:
        result = broker.generate(args.prompt, provider=provider)
        _print_json(success_payload(result))
        return 0
    except BrokerError as exc:
        payload = dict(exc.payload)
        payload["elapsed_seconds"] = round(time.monotonic() - started, 3)
        _print_json(payload)
        return 1


def build_launch_browser_command(config: BrokerConfig, *, provider: str = PROVIDER_GPT_BROWSER) -> list[str]:
    browser = config.provider_browser(provider)
    port = _cdp_port(browser.cdp_url)
    command = [
        "open",
        "-na",
        "Google Chrome",
        "--args",
        f"--remote-debugging-port={port}",
    ]
    if not _is_remote_cdp_url(browser.cdp_url):
        command.append("--remote-debugging-address=127.0.0.1")
    command.extend(
        [
            f"--user-data-dir={browser.profile_path}",
            browser.start_url,
        ]
    )
    return command


def wait_for_cdp(config: BrokerConfig, *, provider: str = PROVIDER_GPT_BROWSER, timeout_seconds: float = 8.0) -> bool:
    browser = config.provider_browser(provider)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            fetch_cdp_json(browser.cdp_url, "/json/version")
            return True
        except Exception:
            time.sleep(0.25)
    return False


def run_launch_browser(args: argparse.Namespace) -> int:
    config = BrokerConfig.from_env()
    provider = normalize_provider(getattr(args, "provider", "") or PROVIDER_GPT_BROWSER)
    browser = config.provider_browser(provider)
    warning = browser_cdp_remote_warning(replace(config, browser_cdp_url=browser.cdp_url))
    if browser.connection_mode not in {"cdp", "persistent"}:
        _print_json(
            error_payload(
                "browser_connection_mode_invalid",
                problem="browser connection mode is invalid",
                cause=f"browser_connection_mode={browser.connection_mode}",
                fix="set IMAGE_WEB_BROWSER_CONNECTION_MODE to cdp or persistent",
                retryable=False,
            )
        )
        return 2
    if browser.cdp_url and _cdp_url_parts(browser.cdp_url).scheme not in {"http", "https"}:
        _print_json(
            error_payload(
                "browser_cdp_url_invalid",
                problem="browser CDP URL is invalid",
                cause=f"{provider} cdp_url={browser.cdp_url}",
                fix="set provider CDP URL to an http:// or https:// DevTools endpoint",
                retryable=False,
            )
        )
        return 2
    already_running = wait_for_cdp(config, provider=provider, timeout_seconds=0.5)
    if already_running:
        _print_json(
            {
                "ok": True,
                "already_running": True,
                "provider": provider,
                "cdp_url": browser.cdp_url,
                "profile_path": str(browser.profile_path),
                "warning": warning,
                "request_id": request_id(),
            }
        )
        return 0
    if _is_remote_cdp_url(browser.cdp_url):
        _print_json(
            error_payload(
                "browser_cdp_unreachable",
                problem="remote Chrome CDP endpoint is unreachable",
                cause=f"{provider} cdp_url={browser.cdp_url}",
                fix="start Chrome with remote debugging on the remote host, or use the default local CDP URL",
                retryable=True,
            )
        )
        return 1
    browser.profile_path.mkdir(parents=True, exist_ok=True)
    command = build_launch_browser_command(config, provider=provider)
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not wait_for_cdp(config, provider=provider):
        _print_json(
            error_payload(
                "browser_cdp_unreachable",
                problem="Chrome did not expose the configured CDP endpoint after launch",
                cause=f"{provider} cdp_url={browser.cdp_url}",
                fix="if the dedicated Chrome profile is already open without CDP, close that one window once and rerun launch-browser",
                retryable=True,
            )
        )
        return 1
    _print_json(
            {
                "ok": True,
                "already_running": False,
                "provider": provider,
                "cdp_url": browser.cdp_url,
                "profile_path": str(browser.profile_path),
                "warning": warning,
                "request_id": request_id(),
            }
    )
    return 0


def run_doctor_browser(args: argparse.Namespace) -> int:
    config = BrokerConfig.from_env()
    provider = normalize_provider(getattr(args, "provider", "") or PROVIDER_GPT_BROWSER)
    browser = config.provider_browser(provider)
    checks: list[dict[str, Any]] = []
    warning = browser_cdp_remote_warning(replace(config, browser_cdp_url=browser.cdp_url))
    if warning:
        checks.append({"ok": True, "code": "browser_cdp_remote_warning", "fix": warning["fix"]})
    if not _playwright_available():
        payload = error_payload(
            "browser_playwright_missing",
            problem="Playwright is not available",
            cause="playwright.sync_api import failed",
            fix="install project browser dependencies, then retry browser mode",
            retryable=False,
        )
        payload["checks"] = checks
        _print_json(payload)
        return 1
    try:
        fetch_cdp_json(browser.cdp_url, "/json/version")
    except Exception as exc:
        payload = error_payload(
            "browser_cdp_unreachable",
            problem="browser CDP endpoint is unreachable",
            cause=type(exc).__name__,
            fix=f"run launch-browser --provider {provider}",
            retryable=True,
        )
        payload["checks"] = checks
        _print_json(payload)
        return 1
    try:
        from playwright.sync_api import sync_playwright

        driver = AiStudioBrowserDriver(config) if provider == PROVIDER_AI_STUDIO_BROWSER else ChatGptBrowserDriver(config)
        with sync_playwright() as playwright:
            lease = driver._acquire_page_lease(playwright, timeout_ms=max(10_000, int(config.timeout_seconds * 1000)))
            try:
                page = lease.page
                page.set_default_timeout(min(max(10_000, int(config.timeout_seconds * 1000)), 60_000))
                driver._ensure_logged_in(page)
                if provider == PROVIDER_AI_STUDIO_BROWSER:
                    driver._ensure_model_available(page)
                driver._composer_locator(page)
            finally:
                lease.cleanup()
    except BrokerError as exc:
        payload = dict(exc.payload)
        payload["checks"] = checks
        _print_json(payload)
        return 1
    except Exception as exc:
        payload = error_payload(
            "browser_cdp_connection_failed",
            problem="browser doctor failed to attach to ChatGPT",
            cause=type(exc).__name__,
            fix=f"run launch-browser --provider {provider}, keep Chrome open, then retry doctor-browser",
            retryable=True,
        )
        payload["checks"] = checks
        _print_json(payload)
        return 1
    _print_json(
        {
            "ok": True,
            "code": "browser_ready",
            "backend": provider,
            "provider": provider,
            "checks": checks,
            "request_id": request_id(),
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AstrBot GPT web-auth image broker")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="")
    serve.add_argument("--port", type=int, default=0)

    sub.add_parser("health")

    launch_browser = sub.add_parser("launch-browser")
    launch_browser.add_argument("--provider", choices=PROVIDER_CHOICES, default=PROVIDER_GPT_BROWSER)
    doctor_browser = sub.add_parser("doctor-browser")
    doctor_browser.add_argument("--provider", choices=PROVIDER_CHOICES, default=PROVIDER_GPT_BROWSER)

    test_generate = sub.add_parser("test-generate")
    test_generate.add_argument("--backend", choices=["auto", "oauth", "browser"], default="auto")
    test_generate.add_argument("--provider", choices=PROVIDER_CHOICES, default="")
    test_generate.add_argument("--prompt", default="a cat in watercolor")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        return run_serve(args)
    if args.command == "health":
        return run_health(args)
    if args.command == "launch-browser":
        return run_launch_browser(args)
    if args.command == "doctor-browser":
        return run_doctor_browser(args)
    if args.command == "test-generate":
        return run_test_generate(args)
    raise SystemExit(f"unknown command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
