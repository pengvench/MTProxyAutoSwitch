from __future__ import annotations

import asyncio
import contextlib
import ctypes
import datetime
import io
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telethon import TelegramClient, errors, types
from telethon.network.connection.tcpmtproxy import ConnectionTcpMTProxyRandomizedIntermediate
from telethon.sessions import StringSession
from cryptography.fernet import Fernet

try:
    import win32crypt
except ImportError:  # pragma: no cover
    win32crypt = None

from mtproxy_collector import ProxyRecord, parse_proxy_link

THREAD_URL_RE = re.compile(r"^https?://t\.me/(?P<username>[A-Za-z0-9_]+)/(?P<thread_id>\d+)$", re.IGNORECASE)
TELEGRAM_SOURCE_URL_RE = re.compile(
    r"^https?://t\.me/(?:(?:s/)?)(?P<username>[A-Za-z0-9_]+)(?:/(?P<message_id>\d+))?/?$",
    re.IGNORECASE,
)
PROXY_URL_RE = re.compile(r"(?:https://t\.me/proxy\?|tg://proxy\?)[^\s<>'\"]+", re.IGNORECASE)

DEFAULT_MEDIA_CHANNELS = ["telegram", "durov", "TelegramTips"]
DEFAULT_TELEGRAM_SOURCE_URLS = [
    "https://t.me/strbypass/237103",
    "https://t.me/telemtrs/16160",
    "https://t.me/telemtfreeproxy",
    "https://t.me/mtpro_xyz",
    "https://t.me/ProxyFree_Ru",
]
DEFAULT_AUTH_TIMEOUT = 20.0
DEFAULT_THREAD_TOTAL_TIMEOUT = 90.0
DEFAULT_THREAD_REQUEST_TIMEOUT = 12.0
DEFAULT_THREAD_MAX_MESSAGES = 350
DEFAULT_SOURCE_MAX_MESSAGES = DEFAULT_THREAD_MAX_MESSAGES
DEFAULT_SOURCE_MAX_AGE_DAYS = 5
DEFAULT_SOURCE_MAX_PROXIES = 80
THREAD_PROGRESS_EVERY = 50
DEFAULT_QR_TOTAL_TIMEOUT = 90.0
SESSION_KEY_FILE_NAME = "session_key.bin"
DPI_WINDOW_MIN_BYTES = 14 * 1024
DPI_WINDOW_MAX_BYTES = 22 * 1024
DEEP_MEDIA_DOWNLOAD_SAMPLE_BYTES = 2 * 1024 * 1024
DEEP_MEDIA_UPLOAD_SAMPLE_BYTES = 768 * 1024
LIGHT_MEDIA_DOWNLOAD_SAMPLE_BYTES = 1024 * 1024
LIGHT_MEDIA_UPLOAD_SAMPLE_BYTES = 384 * 1024
DEEP_MEDIA_TARGET_VIDEOS = 2
LIGHT_MEDIA_TARGET_VIDEOS = 1
MEDIA_PROBE_AUX_TARGET = 1
DOWNLOAD_PROBE_WARMUP_BYTES = 128 * 1024


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("refresh_cancelled")


@dataclass
class TelegramAuthConfig:
    api_id: int
    api_hash: str
    session_path: Path
    phone: str = ""


@dataclass
class MediaProbeResult:
    proxy_key: tuple[str, int, str]
    score: float | None
    note: str
    elapsed_ms: float | None
    upload_kbps: float | None = None
    download_kbps: float | None = None
    aux_kbps: float | None = None


@dataclass(frozen=True)
class TelegramSourceSpec:
    username: str
    message_id: int | None
    normalized_url: str


def parse_thread_url(thread_url: str) -> tuple[str, int]:
    normalized = thread_url.strip()
    match = THREAD_URL_RE.fullmatch(normalized)
    if match is None:
        raise ValueError(f"Unsupported Telegram thread source: {thread_url}")
    return match.group("username"), int(match.group("thread_id"))


def parse_telegram_source_url(source_url: str) -> TelegramSourceSpec:
    normalized = source_url.strip().rstrip("/")
    match = TELEGRAM_SOURCE_URL_RE.fullmatch(normalized)
    if match is None:
        raise ValueError(f"Unsupported Telegram source: {source_url}")
    username = match.group("username")
    if username.lower() in {"proxy", "s"}:
        raise ValueError(f"Unsupported Telegram source: {source_url}")
    message_id = match.group("message_id")
    return TelegramSourceSpec(
        username=username,
        message_id=int(message_id) if message_id else None,
        normalized_url=normalized,
    )


def auth_is_configured(config: TelegramAuthConfig) -> bool:
    return bool(config.api_id and config.api_hash.strip())


def normalize_telegram_phone(phone: str) -> str:
    raw_value = str(phone or "").strip()
    if not raw_value:
        return ""

    digits = "".join(ch for ch in raw_value if ch.isdigit())
    if not digits:
        return raw_value

    if len(digits) == 11 and digits[0] in {"7", "8"}:
        return f"+7{digits[1:]}"
    if len(digits) == 10 and digits[0] == "9":
        return f"+7{digits}"
    if raw_value.startswith("+"):
        return f"+{digits}"
    if len(digits) >= 10:
        return f"+{digits}"
    return digits


def build_client(
    config: TelegramAuthConfig,
    *,
    upstream_proxy: ProxyRecord | None = None,
    timeout: float = 10.0,
    receive_updates: bool = False,
) -> TelegramClient:
    proxy_tuple: tuple[str, int, str] | None = None
    connection = None

    if upstream_proxy is not None:
        connection = ConnectionTcpMTProxyRandomizedIntermediate
        proxy_tuple = (upstream_proxy.host, upstream_proxy.port, upstream_proxy.secret)

    kwargs: dict[str, Any] = {
        "receive_updates": receive_updates,
        "connection_retries": 0,
        "request_retries": 0,
        "timeout": max(3, int(timeout)),
        "auto_reconnect": False,
    }
    if connection is not None:
        kwargs["connection"] = connection
        kwargs["proxy"] = proxy_tuple

    session = _load_session(config.session_path)
    return TelegramClient(session, config.api_id, config.api_hash, **kwargs)


async def get_auth_status(
    config: TelegramAuthConfig,
    *,
    upstream_proxy: ProxyRecord | None = None,
) -> dict[str, Any]:
    _ensure_auth_config(config)
    client = build_client(config, upstream_proxy=upstream_proxy, timeout=DEFAULT_AUTH_TIMEOUT)
    try:
        await _await_timeout(client.connect(), DEFAULT_AUTH_TIMEOUT, "connect")
        authorized = await _await_timeout(client.is_user_authorized(), DEFAULT_AUTH_TIMEOUT, "auth_status")
        me = await _await_timeout(client.get_me(), DEFAULT_AUTH_TIMEOUT, "get_me") if authorized else None
        return {
            "authorized": authorized,
            "display": getattr(me, "first_name", "") or getattr(me, "username", "") or "",
            "phone": getattr(me, "phone", "") or "",
            "session_exists": config.session_path.exists(),
        }
    finally:
        _save_session(config.session_path, client)
        await _disconnect_quietly(client)


async def request_login_code(
    config: TelegramAuthConfig,
    *,
    phone: str,
    upstream_proxy: ProxyRecord | None = None,
) -> dict[str, Any]:
    _ensure_auth_config(config)
    normalized_phone = normalize_telegram_phone(phone)
    client = build_client(config, upstream_proxy=upstream_proxy, timeout=DEFAULT_AUTH_TIMEOUT)
    try:
        await _await_timeout(client.connect(), DEFAULT_AUTH_TIMEOUT, "connect")
        sent = await _await_timeout(client.send_code_request(normalized_phone), DEFAULT_AUTH_TIMEOUT, "send_code")
        return {
            "phone_code_hash": sent.phone_code_hash,
            "type": type(sent.type).__name__ if sent.type is not None else "",
            "phone": normalized_phone,
        }
    finally:
        _save_session(config.session_path, client)
        await _disconnect_quietly(client)


async def complete_login(
    config: TelegramAuthConfig,
    *,
    phone: str,
    code: str,
    phone_code_hash: str,
    password: str = "",
    upstream_proxy: ProxyRecord | None = None,
) -> dict[str, Any]:
    _ensure_auth_config(config)
    normalized_phone = normalize_telegram_phone(phone)
    client = build_client(config, upstream_proxy=upstream_proxy, timeout=DEFAULT_AUTH_TIMEOUT)
    try:
        await _await_timeout(client.connect(), DEFAULT_AUTH_TIMEOUT, "connect")
        try:
            await _await_timeout(
                client.sign_in(phone=normalized_phone, code=code, phone_code_hash=phone_code_hash),
                DEFAULT_AUTH_TIMEOUT,
                "sign_in",
            )
        except errors.SessionPasswordNeededError:
            if not password.strip():
                return {"authorized": False, "password_required": True}
            await _await_timeout(client.sign_in(password=password), DEFAULT_AUTH_TIMEOUT, "password_sign_in")

        me = await _await_timeout(client.get_me(), DEFAULT_AUTH_TIMEOUT, "get_me")
        return {
            "authorized": True,
            "display": getattr(me, "first_name", "") or getattr(me, "username", "") or "",
            "phone": getattr(me, "phone", "") or "",
        }
    finally:
        _save_session(config.session_path, client)
        await _disconnect_quietly(client)


async def logout(
    config: TelegramAuthConfig,
    *,
    upstream_proxy: ProxyRecord | None = None,
) -> None:
    _ensure_auth_config(config)
    client = build_client(config, upstream_proxy=upstream_proxy, timeout=DEFAULT_AUTH_TIMEOUT)
    try:
        await _await_timeout(client.connect(), DEFAULT_AUTH_TIMEOUT, "connect")
        try:
            await _await_timeout(client.log_out(), DEFAULT_AUTH_TIMEOUT, "logout")
        except Exception:
            pass
    finally:
        try:
            await _disconnect_quietly(client)
        finally:
            _delete_session(config.session_path)


async def qr_login_flow(
    config: TelegramAuthConfig,
    *,
    upstream_proxy: ProxyRecord | None = None,
    password: str = "",
    qr_ready: Any | None = None,
    total_timeout: float = DEFAULT_QR_TOTAL_TIMEOUT,
) -> dict[str, Any]:
    _ensure_auth_config(config)
    client = build_client(
        config,
        upstream_proxy=upstream_proxy,
        timeout=DEFAULT_AUTH_TIMEOUT,
        receive_updates=True,
    )
    deadline = time.perf_counter() + max(15.0, float(total_timeout))

    try:
        await _await_timeout(client.connect(), _remaining(deadline, DEFAULT_AUTH_TIMEOUT), "connect")
        if await _await_timeout(client.is_user_authorized(), _remaining(deadline, DEFAULT_AUTH_TIMEOUT), "auth_status"):
            me = await _await_timeout(client.get_me(), _remaining(deadline, DEFAULT_AUTH_TIMEOUT), "get_me")
            return {
                "authorized": True,
                "display": getattr(me, "first_name", "") or getattr(me, "username", "") or "",
                "phone": getattr(me, "phone", "") or "",
                "already_authorized": True,
            }

        qr_login = await _await_timeout(client.qr_login(), _remaining(deadline, DEFAULT_AUTH_TIMEOUT), "qr_login")
        expires_at = qr_login.expires.isoformat()
        qr_payload = {
            "url": qr_login.url,
            "expires_at": expires_at,
        }
        if callable(qr_ready):
            qr_ready(qr_payload)

        wait_timeout = min(
            _remaining(deadline, DEFAULT_QR_TOTAL_TIMEOUT),
            max(5.0, (qr_login.expires - datetime.datetime.now(tz=datetime.timezone.utc)).total_seconds()),
        )
        try:
            await _await_timeout(qr_login.wait(timeout=wait_timeout), wait_timeout + 3.0, "qr_wait")
        except errors.SessionPasswordNeededError:
            if not password.strip():
                return {
                    "authorized": False,
                    "password_required": True,
                    "qr": True,
                    "expires_at": expires_at,
                }
            await _await_timeout(client.sign_in(password=password.strip()), _remaining(deadline, DEFAULT_AUTH_TIMEOUT), "qr_password")
        except RuntimeError as exc:
            if str(exc) == "qr_wait_timeout":
                return {
                    "authorized": False,
                    "timeout": True,
                    "qr": True,
                    "expires_at": expires_at,
                }
            raise

        me = await _await_timeout(client.get_me(), _remaining(deadline, DEFAULT_AUTH_TIMEOUT), "get_me")
        return {
            "authorized": True,
            "display": getattr(me, "first_name", "") or getattr(me, "username", "") or "",
            "phone": getattr(me, "phone", "") or "",
            "qr": True,
            "expires_at": expires_at,
        }
    finally:
        _save_session(config.session_path, client)
        await _disconnect_quietly(client)


async def collect_thread_proxies(
    thread_url: str,
    config: TelegramAuthConfig,
    *,
    upstream_proxy: ProxyRecord | None = None,
    log_sink: Any | None = None,
    total_timeout: float = DEFAULT_THREAD_TOTAL_TIMEOUT,
    request_timeout: float = DEFAULT_THREAD_REQUEST_TIMEOUT,
    max_messages: int = DEFAULT_THREAD_MAX_MESSAGES,
    max_proxies: int = DEFAULT_SOURCE_MAX_PROXIES,
    max_age_days: int = DEFAULT_SOURCE_MAX_AGE_DAYS,
    event_sink: Any | None = None,
    cancel_event: threading.Event | None = None,
) -> list[ProxyRecord]:
    _ensure_auth_config(config)
    username, thread_id = parse_thread_url(thread_url)
    source_url = thread_url.strip()
    client = build_client(config, upstream_proxy=upstream_proxy, timeout=request_timeout)
    registry: dict[tuple[str, int, str], ProxyRecord] = {}
    deadline = time.perf_counter() + max(5.0, float(total_timeout))
    cutoff_dt = _source_cutoff_datetime(max_age_days)
    scanned_messages = 0
    timed_out = False
    hit_limit = False
    hit_age_limit = False
    hit_proxy_limit = False

    _emit_progress(
        event_sink,
        "telegram_source_started",
        source=source_url,
        index=1,
        total=1,
        max_age_days=max_age_days,
    )

    try:
        await _await_timeout(client.connect(), _remaining(deadline, request_timeout), "connect")
        if not await _await_timeout(client.is_user_authorized(), _remaining(deadline, request_timeout), "auth_status"):
            raise RuntimeError("telegram_session_not_authorized")

        entity = await _await_timeout(client.get_entity(username), _remaining(deadline, request_timeout), "get_entity")
        root_message = await _await_timeout(
            client.get_messages(entity, ids=thread_id),
            _remaining(deadline, request_timeout),
            "get_root_message",
        )
        if root_message is not None and not _message_is_older_than(root_message, cutoff_dt):
            hit_proxy_limit = _register_proxies_from_message(registry, root_message, source_url, max_proxies=max_proxies)

        iterator = client.iter_messages(entity, reply_to=thread_id, limit=None)
        while True:
            _raise_if_cancelled(cancel_event)
            if time.perf_counter() >= deadline:
                timed_out = True
                break
            if scanned_messages >= max_messages:
                hit_limit = True
                break
            if max_proxies > 0 and len(registry) >= max_proxies:
                hit_proxy_limit = True
                break
            try:
                message = await _await_timeout(
                    iterator.__anext__(),
                    _remaining(deadline, request_timeout),
                    "iter_messages",
                )
            except StopAsyncIteration:
                break
            if _message_is_older_than(message, cutoff_dt):
                hit_age_limit = True
                break
            scanned_messages += 1
            if _register_proxies_from_message(registry, message, source_url, max_proxies=max_proxies):
                hit_proxy_limit = True
                break
            if log_sink is not None and scanned_messages % THREAD_PROGRESS_EVERY == 0:
                log_sink(
                    f"[thread] scanned={scanned_messages} proxies={len(registry)} "
                    f"source={thread_url}"
                )
            if scanned_messages % THREAD_PROGRESS_EVERY == 0:
                _emit_progress(
                    event_sink,
                    "telegram_source_progress",
                    source=source_url,
                    index=1,
                    total=1,
                    scanned_messages=scanned_messages,
                    proxy_count=len(registry),
                    max_age_days=max_age_days,
                )

        if log_sink is not None:
            suffix = ""
            if timed_out:
                suffix = f" partial_timeout_after={scanned_messages}"
            elif hit_limit:
                suffix = f" partial_limit={max_messages}"
            elif hit_proxy_limit:
                suffix = f" proxy_limit={max_proxies}"
            elif hit_age_limit:
                suffix = f" age_limit={max_age_days}d"
            log_sink(f"[thread] {thread_url} -> {len(registry)} proxies{suffix}")
        _emit_progress(
            event_sink,
            "telegram_source_finished",
            source=source_url,
            index=1,
            total=1,
            scanned_messages=scanned_messages,
            proxy_count=len(registry),
            timed_out=timed_out,
            hit_limit=hit_limit,
            hit_proxy_limit=hit_proxy_limit,
            hit_age_limit=hit_age_limit,
            max_age_days=max_age_days,
        )
        return sorted(registry.values(), key=lambda item: item.url)
    finally:
        _save_session(config.session_path, client)
        await _disconnect_quietly(client)


async def collect_telegram_source_proxies(
    source_url: str,
    config: TelegramAuthConfig,
    *,
    upstream_proxy: ProxyRecord | None = None,
    log_sink: Any | None = None,
    total_timeout: float = DEFAULT_THREAD_TOTAL_TIMEOUT,
    request_timeout: float = DEFAULT_THREAD_REQUEST_TIMEOUT,
    max_messages: int = DEFAULT_THREAD_MAX_MESSAGES,
    max_proxies: int = DEFAULT_SOURCE_MAX_PROXIES,
    max_age_days: int = DEFAULT_SOURCE_MAX_AGE_DAYS,
    event_sink: Any | None = None,
    source_index: int = 1,
    total_sources: int = 1,
    cancel_event: threading.Event | None = None,
) -> list[ProxyRecord]:
    _ensure_auth_config(config)
    spec = parse_telegram_source_url(source_url)
    client = build_client(config, upstream_proxy=upstream_proxy, timeout=request_timeout)
    registry: dict[tuple[str, int, str], ProxyRecord] = {}
    deadline = time.perf_counter() + max(5.0, float(total_timeout))
    cutoff_dt = _source_cutoff_datetime(max_age_days)
    scanned_messages = 0
    timed_out = False
    hit_limit = False
    hit_age_limit = False
    hit_proxy_limit = False

    _emit_progress(
        event_sink,
        "telegram_source_started",
        source=spec.normalized_url,
        index=source_index,
        total=total_sources,
        max_age_days=max_age_days,
    )

    try:
        await _await_timeout(client.connect(), _remaining(deadline, request_timeout), "connect")
        if not await _await_timeout(client.is_user_authorized(), _remaining(deadline, request_timeout), "auth_status"):
            raise RuntimeError("telegram_session_not_authorized")

        entity = await _await_timeout(client.get_entity(spec.username), _remaining(deadline, request_timeout), "get_entity")

        if spec.message_id is not None:
            root_message = await _await_timeout(
                client.get_messages(entity, ids=spec.message_id),
                _remaining(deadline, request_timeout),
                "get_root_message",
            )
            if root_message is not None and not _message_is_older_than(root_message, cutoff_dt):
                scanned_messages += 1
                hit_proxy_limit = _register_proxies_from_message(
                    registry,
                    root_message,
                    spec.normalized_url,
                    max_proxies=max_proxies,
                )

            with contextlib.suppress(Exception):
                iterator = client.iter_messages(entity, reply_to=spec.message_id, limit=None)
                while True:
                    _raise_if_cancelled(cancel_event)
                    if time.perf_counter() >= deadline:
                        timed_out = True
                        break
                    if scanned_messages >= max_messages:
                        hit_limit = True
                        break
                    if max_proxies > 0 and len(registry) >= max_proxies:
                        hit_proxy_limit = True
                        break
                    try:
                        message = await _await_timeout(
                            iterator.__anext__(),
                            _remaining(deadline, request_timeout),
                            "iter_messages",
                        )
                    except StopAsyncIteration:
                        break
                    if _message_is_older_than(message, cutoff_dt):
                        hit_age_limit = True
                        break
                    scanned_messages += 1
                    if _register_proxies_from_message(
                        registry,
                        message,
                        spec.normalized_url,
                        max_proxies=max_proxies,
                    ):
                        hit_proxy_limit = True
                        break
                    if log_sink is not None and scanned_messages % THREAD_PROGRESS_EVERY == 0:
                        log_sink(
                            f"[telegram] scanned={scanned_messages} proxies={len(registry)} "
                            f"source={spec.normalized_url}"
                        )
                    if scanned_messages % THREAD_PROGRESS_EVERY == 0:
                        _emit_progress(
                            event_sink,
                            "telegram_source_progress",
                            source=spec.normalized_url,
                            index=source_index,
                            total=total_sources,
                            scanned_messages=scanned_messages,
                            proxy_count=len(registry),
                            max_age_days=max_age_days,
                        )
        else:
            iterator = client.iter_messages(entity, limit=None)
            while True:
                _raise_if_cancelled(cancel_event)
                if time.perf_counter() >= deadline:
                    timed_out = True
                    break
                if scanned_messages >= max_messages:
                    hit_limit = True
                    break
                if max_proxies > 0 and len(registry) >= max_proxies:
                    hit_proxy_limit = True
                    break
                try:
                    message = await _await_timeout(
                        iterator.__anext__(),
                        _remaining(deadline, request_timeout),
                        "iter_messages",
                    )
                except StopAsyncIteration:
                    break
                if _message_is_older_than(message, cutoff_dt):
                    hit_age_limit = True
                    break
                scanned_messages += 1
                if _register_proxies_from_message(
                    registry,
                    message,
                    spec.normalized_url,
                    max_proxies=max_proxies,
                ):
                    hit_proxy_limit = True
                    break
                if log_sink is not None and scanned_messages % THREAD_PROGRESS_EVERY == 0:
                    log_sink(
                        f"[telegram] scanned={scanned_messages} proxies={len(registry)} "
                        f"source={spec.normalized_url}"
                    )
                if scanned_messages % THREAD_PROGRESS_EVERY == 0:
                    _emit_progress(
                        event_sink,
                        "telegram_source_progress",
                        source=spec.normalized_url,
                        index=source_index,
                        total=total_sources,
                        scanned_messages=scanned_messages,
                        proxy_count=len(registry),
                        max_age_days=max_age_days,
                    )

        if log_sink is not None:
            suffix = ""
            if timed_out:
                suffix = f" partial_timeout_after={scanned_messages}"
            elif hit_limit:
                suffix = f" partial_limit={max_messages}"
            elif hit_proxy_limit:
                suffix = f" proxy_limit={max_proxies}"
            elif hit_age_limit:
                suffix = f" age_limit={max_age_days}d"
            log_sink(f"[telegram] {spec.normalized_url} -> {len(registry)} proxies{suffix}")
        _emit_progress(
            event_sink,
            "telegram_source_finished",
            source=spec.normalized_url,
            index=source_index,
            total=total_sources,
            scanned_messages=scanned_messages,
            proxy_count=len(registry),
            timed_out=timed_out,
            hit_limit=hit_limit,
            hit_proxy_limit=hit_proxy_limit,
            hit_age_limit=hit_age_limit,
            max_age_days=max_age_days,
        )
        return sorted(registry.values(), key=lambda item: item.url)
    finally:
        _save_session(config.session_path, client)
        await _disconnect_quietly(client)


async def collect_telegram_sources_proxies(
    source_urls: list[str],
    config: TelegramAuthConfig,
    *,
    upstream_proxy: ProxyRecord | None = None,
    log_sink: Any | None = None,
    total_timeout: float = DEFAULT_THREAD_TOTAL_TIMEOUT,
    request_timeout: float = DEFAULT_THREAD_REQUEST_TIMEOUT,
    max_messages: int = DEFAULT_THREAD_MAX_MESSAGES,
    max_proxies: int = DEFAULT_SOURCE_MAX_PROXIES,
    max_age_days: int = DEFAULT_SOURCE_MAX_AGE_DAYS,
    event_sink: Any | None = None,
    cancel_event: threading.Event | None = None,
) -> list[ProxyRecord]:
    unique_urls: list[str] = []
    seen_urls: set[str] = set()
    for raw_url in source_urls:
        _raise_if_cancelled(cancel_event)
        url = str(raw_url).strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        unique_urls.append(url)

    registry: dict[tuple[str, int, str], ProxyRecord] = {}
    _emit_progress(
        event_sink,
        "telegram_sources_started",
        total_sources=len(unique_urls),
        max_age_days=max_age_days,
    )
    for index, source_url in enumerate(unique_urls, start=1):
        _raise_if_cancelled(cancel_event)
        proxies = await collect_telegram_source_proxies(
            source_url,
            config,
            upstream_proxy=upstream_proxy,
            log_sink=log_sink,
            total_timeout=total_timeout,
            request_timeout=request_timeout,
            max_messages=max_messages,
            max_proxies=max_proxies,
            max_age_days=max_age_days,
            event_sink=event_sink,
            source_index=index,
            total_sources=len(unique_urls),
            cancel_event=cancel_event,
        )
        for proxy in proxies:
            registry[proxy.key] = proxy
    _raise_if_cancelled(cancel_event)
    _emit_progress(
        event_sink,
        "telegram_sources_finished",
        total_sources=len(unique_urls),
        proxy_count=len(registry),
        max_age_days=max_age_days,
    )
    return sorted(registry.values(), key=lambda item: item.url)


async def deep_media_probe(
    proxy: ProxyRecord,
    config: TelegramAuthConfig,
    *,
    channels: list[str] | None = None,
) -> MediaProbeResult:
    _ensure_auth_config(config)
    started_at = time.perf_counter()
    client = build_client(config, upstream_proxy=proxy, timeout=12.0)
    sample_channels = channels or list(DEFAULT_MEDIA_CHANNELS)
    deadline = time.perf_counter() + 40.0

    try:
        await _await_timeout(client.connect(), _remaining(deadline, 12.0), "connect")
        if not await _await_timeout(client.is_user_authorized(), _remaining(deadline, 8.0), "auth_status"):
            return MediaProbeResult(proxy.key, None, "session_not_authorized", None)

        video_samples, aux_samples = await _collect_media_probe_samples(
            client,
            sample_channels,
            deadline=deadline,
            target_videos=DEEP_MEDIA_TARGET_VIDEOS,
            target_aux=MEDIA_PROBE_AUX_TARGET,
        )
        if not video_samples:
            return MediaProbeResult(proxy.key, None, "no_video_samples_found", None)

        video_downloads_kbps: list[float] = []
        video_kinds: list[str] = []
        for message, media_kind in video_samples:
            download_elapsed_ms, downloaded, note = await _download_sample_bytes(
                client,
                message,
                max_bytes=DEEP_MEDIA_DOWNLOAD_SAMPLE_BYTES,
                timeout=_remaining(deadline, 14.0),
            )
            if note == "dpi_16_20kb_suspected":
                return MediaProbeResult(
                    proxy.key,
                    0.0,
                    f"{media_kind} dpi_16_20kb_suspected",
                    round((time.perf_counter() - started_at) * 1000.0, 2),
                )
            if downloaded <= 0:
                continue
            video_downloads_kbps.append(_rate_kbps(downloaded, download_elapsed_ms))
            video_kinds.append(media_kind)
        if not video_downloads_kbps:
            return MediaProbeResult(proxy.key, None, "video_download_failed", None)

        aux_rates: list[tuple[str, float]] = []
        for message, media_kind in aux_samples:
            aux_elapsed_ms, aux_downloaded, aux_note = await _download_sample_bytes(
                client,
                message,
                max_bytes=256 * 1024,
                timeout=_remaining(deadline, 8.0),
            )
            if aux_note == "dpi_16_20kb_suspected":
                return MediaProbeResult(
                    proxy.key,
                    0.0,
                    f"{media_kind} dpi_16_20kb_suspected",
                    round((time.perf_counter() - started_at) * 1000.0, 2),
                )
            if aux_downloaded > 0:
                aux_rates.append((media_kind, _rate_kbps(aux_downloaded, aux_elapsed_ms)))

        upload_elapsed_ms, uploaded = await _upload_video_probe_sample(
            client,
            size_bytes=DEEP_MEDIA_UPLOAD_SAMPLE_BYTES,
            timeout=_remaining(deadline, 14.0),
        )
        upload_kbps = _rate_kbps(uploaded, upload_elapsed_ms)
        score = _score_hybrid_media_probe(
            video_downloads_kbps=video_downloads_kbps,
            upload_kbps=upload_kbps,
            aux_downloads_kbps=[rate for _kind, rate in aux_rates],
            expected_video_samples=DEEP_MEDIA_TARGET_VIDEOS,
        )
        return MediaProbeResult(
            proxy.key,
            score,
            _format_hybrid_probe_note(
                video_kinds=video_kinds,
                video_downloads_kbps=video_downloads_kbps,
                upload_kbps=upload_kbps,
                aux_rates=aux_rates,
            ),
            round((time.perf_counter() - started_at) * 1000.0, 2),
            upload_kbps=upload_kbps,
            download_kbps=min(video_downloads_kbps) if video_downloads_kbps else None,
            aux_kbps=aux_rates[0][1] if aux_rates else None,
        )
    except Exception as exc:
        return MediaProbeResult(
            proxy.key,
            None,
            _probe_failure_note(exc, "media"),
            round((time.perf_counter() - started_at) * 1000.0, 2),
        )
    finally:
        _save_session(config.session_path, client)
        await _disconnect_quietly(client)


async def light_media_probe(
    proxy: ProxyRecord,
    config: TelegramAuthConfig,
    *,
    channels: list[str] | None = None,
) -> MediaProbeResult:
    _ensure_auth_config(config)
    started_at = time.perf_counter()
    client = build_client(config, upstream_proxy=proxy, timeout=10.0)
    sample_channels = channels or list(DEFAULT_MEDIA_CHANNELS[:2])
    deadline = time.perf_counter() + 18.0

    try:
        await _await_timeout(client.connect(), _remaining(deadline, 10.0), "connect")
        if not await _await_timeout(client.is_user_authorized(), _remaining(deadline, 6.0), "auth_status"):
            return MediaProbeResult(proxy.key, None, "session_not_authorized", None)

        video_samples, aux_samples = await _collect_media_probe_samples(
            client,
            sample_channels,
            deadline=deadline,
            target_videos=LIGHT_MEDIA_TARGET_VIDEOS,
            target_aux=MEDIA_PROBE_AUX_TARGET,
            per_channel_limit=24,
        )
        if not video_samples:
            return MediaProbeResult(proxy.key, None, "no_video_samples_found", None)

        video_downloads_kbps: list[float] = []
        video_kinds: list[str] = []
        for message, media_kind in video_samples:
            elapsed_ms, downloaded, note = await _download_sample_bytes(
                client,
                message,
                max_bytes=LIGHT_MEDIA_DOWNLOAD_SAMPLE_BYTES,
                timeout=_remaining(deadline, 8.0),
            )
            if note == "dpi_16_20kb_suspected":
                return MediaProbeResult(
                    proxy.key,
                    0.0,
                    f"{media_kind} dpi_16_20kb_suspected",
                    round((time.perf_counter() - started_at) * 1000.0, 2),
                )
            if downloaded <= 0:
                continue
            video_downloads_kbps.append(_rate_kbps(downloaded, elapsed_ms))
            video_kinds.append(media_kind)
        if not video_downloads_kbps:
            return MediaProbeResult(proxy.key, None, "video_download_failed", None)

        aux_rates: list[tuple[str, float]] = []
        for message, media_kind in aux_samples:
            aux_elapsed_ms, aux_downloaded, aux_note = await _download_sample_bytes(
                client,
                message,
                max_bytes=160 * 1024,
                timeout=_remaining(deadline, 5.0),
            )
            if aux_note == "dpi_16_20kb_suspected":
                return MediaProbeResult(
                    proxy.key,
                    0.0,
                    f"{media_kind} dpi_16_20kb_suspected",
                    round((time.perf_counter() - started_at) * 1000.0, 2),
                )
            if aux_downloaded > 0:
                aux_rates.append((media_kind, _rate_kbps(aux_downloaded, aux_elapsed_ms)))

        upload_elapsed_ms, uploaded = await _upload_video_probe_sample(
            client,
            size_bytes=LIGHT_MEDIA_UPLOAD_SAMPLE_BYTES,
            timeout=_remaining(deadline, 7.0),
        )
        upload_kbps = _rate_kbps(uploaded, upload_elapsed_ms)
        score = _score_hybrid_media_probe(
            video_downloads_kbps=video_downloads_kbps,
            upload_kbps=upload_kbps,
            aux_downloads_kbps=[rate for _kind, rate in aux_rates],
            expected_video_samples=LIGHT_MEDIA_TARGET_VIDEOS,
        )

        return MediaProbeResult(
            proxy.key,
            score,
            _format_hybrid_probe_note(
                video_kinds=video_kinds,
                video_downloads_kbps=video_downloads_kbps,
                upload_kbps=upload_kbps,
                aux_rates=aux_rates,
            ),
            round((time.perf_counter() - started_at) * 1000.0, 2),
            upload_kbps=upload_kbps,
            download_kbps=min(video_downloads_kbps) if video_downloads_kbps else None,
            aux_kbps=aux_rates[0][1] if aux_rates else None,
        )
    except Exception as exc:
        return MediaProbeResult(
            proxy.key,
            None,
            _probe_failure_note(exc, "light_media"),
            round((time.perf_counter() - started_at) * 1000.0, 2),
        )
    finally:
        _save_session(config.session_path, client)
        await _disconnect_quietly(client)


async def _collect_media_probe_samples(
    client: TelegramClient,
    channels: list[str],
    *,
    deadline: float,
    target_videos: int,
    target_aux: int,
    per_channel_limit: int = 40,
) -> tuple[list[tuple[Any, str]], list[tuple[Any, str]]]:
    video_samples: list[tuple[Any, str]] = []
    aux_samples: list[tuple[Any, str]] = []
    for username in channels:
        entity = await _await_timeout(client.get_entity(username), _remaining(deadline, 8.0), "get_entity")
        iterator = client.iter_messages(entity, limit=per_channel_limit)
        while True:
            if time.perf_counter() >= deadline:
                raise RuntimeError("media_probe_timeout")
            try:
                message = await _await_timeout(iterator.__anext__(), _remaining(deadline, 8.0), "iter_messages")
            except StopAsyncIteration:
                break
            media_kind = _detect_media_kind(message)
            if media_kind in {"video", "video_note"} and len(video_samples) < target_videos:
                video_samples.append((message, media_kind))
            elif media_kind in {"photo", "document"} and len(aux_samples) < target_aux:
                aux_samples.append((message, media_kind))
            if len(video_samples) >= target_videos and len(aux_samples) >= target_aux:
                return video_samples, aux_samples
    return video_samples, aux_samples


async def send_proxy_list_to_saved_messages(
    config: TelegramAuthConfig,
    proxy_urls: list[str],
    *,
    upstream_proxy: ProxyRecord | None = None,
) -> dict[str, Any]:
    _ensure_auth_config(config)
    client = build_client(config, upstream_proxy=upstream_proxy, timeout=DEFAULT_AUTH_TIMEOUT)
    unique_urls: list[str] = []
    seen: set[str] = set()
    for raw_url in proxy_urls:
        url = str(raw_url).strip()
        if not url or url in seen:
            continue
        seen.add(url)
        unique_urls.append(url)

    try:
        await _await_timeout(client.connect(), DEFAULT_AUTH_TIMEOUT, "connect")
        if not await _await_timeout(client.is_user_authorized(), DEFAULT_AUTH_TIMEOUT, "auth_status"):
            raise RuntimeError("telegram_session_not_authorized")

        if not unique_urls:
            await _await_timeout(client.send_message("me", "Рабочих прокси сейчас нет."), DEFAULT_AUTH_TIMEOUT, "send_empty")
            return {"sent": 0, "messages": 1}

        chunks: list[str] = []
        current = f"Рабочие прокси: {len(unique_urls)}"
        for url in unique_urls:
            candidate = f"{current}\n{url}"
            if len(candidate) > 3500:
                chunks.append(current)
                current = url
            else:
                current = candidate
        if current:
            chunks.append(current)

        for chunk in chunks:
            await _await_timeout(client.send_message("me", chunk), DEFAULT_AUTH_TIMEOUT, "send_chunk")
        return {"sent": len(unique_urls), "messages": len(chunks)}
    finally:
        _save_session(config.session_path, client)
        await _disconnect_quietly(client)


def _ensure_auth_config(config: TelegramAuthConfig) -> None:
    if not auth_is_configured(config):
        raise RuntimeError("telegram_api_credentials_missing")


def _emit_progress(event_sink: Any | None, event_name: str, **payload: Any) -> None:
    if callable(event_sink):
        event_sink(event_name, payload)


def _source_cutoff_datetime(max_age_days: int) -> datetime.datetime | None:
    if int(max_age_days or 0) <= 0:
        return None
    return datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(days=int(max_age_days))


def _message_is_older_than(message: Any, cutoff_dt: datetime.datetime | None) -> bool:
    if cutoff_dt is None:
        return False
    message_date = getattr(message, "date", None)
    if not isinstance(message_date, datetime.datetime):
        return False
    if message_date.tzinfo is None:
        message_date = message_date.replace(tzinfo=datetime.timezone.utc)
    return message_date < cutoff_dt


def _extract_message_proxies(message: Any, source_url: str) -> list[ProxyRecord]:
    records: dict[tuple[str, int, str], ProxyRecord] = {}
    text_candidates = [getattr(message, "raw_text", "") or "", getattr(message, "message", "") or ""]

    for text in text_candidates:
        for match in PROXY_URL_RE.finditer(text):
            proxy = parse_proxy_link(match.group(0), source_url, source_url)
            if proxy is not None:
                records[proxy.key] = proxy

    entities = getattr(message, "entities", None) or []
    text = getattr(message, "message", "") or ""
    for entity in entities:
        url = getattr(entity, "url", None)
        if isinstance(url, str) and "proxy?" in url:
            proxy = parse_proxy_link(url, source_url, source_url)
            if proxy is not None:
                records[proxy.key] = proxy
        offset = getattr(entity, "offset", 0)
        length = getattr(entity, "length", 0)
        if length > 0:
            candidate = text[offset : offset + length]
            if candidate.startswith("https://t.me/proxy?") or candidate.startswith("tg://proxy?"):
                proxy = parse_proxy_link(candidate, source_url, source_url)
                if proxy is not None:
                    records[proxy.key] = proxy

    reply_markup = getattr(message, "reply_markup", None)
    rows = getattr(reply_markup, "rows", None) or []
    for row in rows:
        buttons = getattr(row, "buttons", None) or []
        for button in buttons:
            url = getattr(button, "url", None)
            if isinstance(url, str) and (url.startswith("https://t.me/proxy?") or url.startswith("tg://proxy?")):
                proxy = parse_proxy_link(url, source_url, source_url)
                if proxy is not None:
                    records[proxy.key] = proxy

    return list(records.values())


def _register_proxies_from_message(
    registry: dict[tuple[str, int, str], ProxyRecord],
    message: Any,
    source_url: str,
    *,
    max_proxies: int,
) -> bool:
    for proxy in _extract_message_proxies(message, source_url):
        registry[proxy.key] = proxy
        if max_proxies > 0 and len(registry) >= max_proxies:
            return True
    return False


def _detect_media_kind(message: Any) -> str | None:
    if getattr(message, "photo", None) is not None:
        return "photo"

    document = getattr(message, "document", None)
    if document is None:
        media = getattr(message, "media", None)
        # Исправлено: убрано двойное разыменование .document.document
        document = getattr(media, "document", None)

    if document is None:
        return None

    mime_type = str(getattr(document, "mime_type", "") or "").lower()
    for attribute in getattr(document, "attributes", []) or []:
        if getattr(attribute, "voice", False):
            return "voice"
        if getattr(attribute, "round_message", False):
            return "video_note"
        if isinstance(attribute, types.DocumentAttributeVideo):
            return "video"
    if mime_type.startswith("video/"):
        return "video"
    return "document"


async def _upload_video_probe_sample(
    client: TelegramClient,
    *,
    size_bytes: int,
    timeout: float,
) -> tuple[float, int]:
    started_at = time.perf_counter()
    payload = io.BytesIO(b"\x00" * max(64 * 1024, int(size_bytes)))
    payload.name = "mtproxy_probe.mp4"
    await _await_timeout(
        client.upload_file(payload, part_size_kb=64),
        max(2.0, float(timeout)),
        "upload_video",
    )
    elapsed_ms = (time.perf_counter() - started_at) * 1000.0
    return elapsed_ms, int(size_bytes)


def _rate_kbps(transferred_bytes: int, elapsed_ms: float) -> float:
    if transferred_bytes <= 0 or elapsed_ms <= 0:
        return 0.0
    return (float(transferred_bytes) / max(float(elapsed_ms) / 1000.0, 0.001)) / 1024.0


def _format_probe_rate_kbps(rate_kbps: float) -> str:
    value = max(0.0, float(rate_kbps or 0.0))
    if value >= 1024.0:
        return f"{value / 1024.0:.1f}MB/s"
    return f"{value:.0f}KB/s"


def _score_hybrid_media_probe(
    *,
    video_downloads_kbps: list[float],
    upload_kbps: float,
    aux_downloads_kbps: list[float],
    expected_video_samples: int,
) -> float:
    safe_videos = [max(0.0, float(item or 0.0)) for item in video_downloads_kbps if float(item or 0.0) > 0.0]
    if not safe_videos:
        return 0.0
    safe_upload = max(0.0, float(upload_kbps or 0.0))
    video_floor = min(safe_videos)
    video_avg = sum(safe_videos) / len(safe_videos)
    bottleneck = min(video_floor, max(safe_upload * 0.8, 1.0))

    if bottleneck >= 3_072.0:
        score = 1.0
    elif bottleneck >= 2_048.0:
        score = 0.94
    elif bottleneck >= 1_024.0:
        score = 0.84
    elif bottleneck >= 640.0:
        score = 0.72
    elif bottleneck >= 384.0:
        score = 0.58
    elif bottleneck >= 192.0:
        score = 0.4
    else:
        score = 0.2

    consistency = video_floor / max(video_avg, 1.0)
    if len(safe_videos) >= 2:
        if consistency < 0.55:
            score -= 0.18
        elif consistency < 0.75:
            score -= 0.08
    elif max(1, int(expected_video_samples or 1)) > 1:
        score -= 0.06

    completion_ratio = min(1.0, len(safe_videos) / max(1, int(expected_video_samples or 1)))
    if completion_ratio < 1.0:
        score -= (1.0 - completion_ratio) * 0.14

    safe_aux = [max(0.0, float(item or 0.0)) for item in aux_downloads_kbps if float(item or 0.0) > 0.0]
    if safe_aux:
        aux_floor = min(safe_aux)
        if aux_floor >= 1_024.0:
            score += 0.05
        elif aux_floor >= 384.0:
            score += 0.02
        elif aux_floor < 128.0:
            score -= 0.03

    if safe_upload < 192.0:
        score -= 0.06
    elif safe_upload >= 1_024.0:
        score += 0.02

    if video_floor >= 1_024.0:
        score += 0.08
    elif video_floor >= 640.0:
        score += 0.04
    elif video_floor < 160.0:
        score -= 0.08

    return max(0.0, min(1.0, score))


def _format_hybrid_probe_note(
    *,
    video_kinds: list[str],
    video_downloads_kbps: list[float],
    upload_kbps: float,
    aux_rates: list[tuple[str, float]],
) -> str:
    video_floor = min(video_downloads_kbps) if video_downloads_kbps else 0.0
    video_avg = (sum(video_downloads_kbps) / len(video_downloads_kbps)) if video_downloads_kbps else 0.0
    kinds = ",".join(sorted(set(video_kinds))) or "video"
    parts = [
        f"{kinds} ok",
        f"dl_floor={_format_probe_rate_kbps(video_floor)}",
        f"dl_avg={_format_probe_rate_kbps(video_avg)}",
        f"up={_format_probe_rate_kbps(upload_kbps)}",
    ]
    if aux_rates:
        aux_kind, aux_rate = aux_rates[0]
        parts.append(f"aux={aux_kind}:{_format_probe_rate_kbps(aux_rate)}")
    return " ".join(parts)


async def _download_sample_bytes(
    client: TelegramClient,
    message: Any,
    max_bytes: int = 256 * 1024,
    timeout: float = 12.0,
) -> tuple[float, int, str]:
    started_at = time.perf_counter()
    downloaded_total = 0
    measured_bytes = 0
    measured_started_at = 0.0
    try:
        iterator = client.iter_download(message.media, request_size=64 * 1024)
    except (TypeError, AttributeError, ValueError, NotImplementedError, OSError):
        # media-тип не поддерживает скачивание (geo, contact, etc.)
        return await _download_sample_bytes_fallback(
            client,
            message,
            max_bytes=max_bytes,
            timeout=timeout,
            started_at=started_at,
        )
    deadline = time.perf_counter() + max(2.0, float(timeout))
    while True:
        if time.perf_counter() >= deadline:
            break
        try:
            chunk = await _await_timeout(iterator.__anext__(), _remaining(deadline, 6.0), "iter_download")
        except StopAsyncIteration:
            break
        except (TypeError, AttributeError, ValueError, NotImplementedError, OSError):
            return await _download_sample_bytes_fallback(
                client,
                message,
                max_bytes=max_bytes,
                timeout=_remaining(deadline, 4.0),
                started_at=started_at,
            )
        chunk_len = len(chunk)
        downloaded_total += chunk_len
        if downloaded_total > DOWNLOAD_PROBE_WARMUP_BYTES:
            if measured_started_at <= 0.0:
                measured_started_at = time.perf_counter()
            warmup_overlap = max(0, DOWNLOAD_PROBE_WARMUP_BYTES - (downloaded_total - chunk_len))
            measured_bytes += max(0, chunk_len - warmup_overlap)
        if downloaded_total >= max_bytes:
            break
    if measured_started_at > 0.0 and measured_bytes > 0:
        elapsed_ms = (time.perf_counter() - measured_started_at) * 1000.0
    else:
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        measured_bytes = downloaded_total
    if _looks_like_dpi_window_block(downloaded_total):
        return elapsed_ms, measured_bytes, "dpi_16_20kb_suspected"
    return elapsed_ms, measured_bytes, ""


async def _download_sample_bytes_fallback(
    client: TelegramClient,
    message: Any,
    *,
    max_bytes: int,
    timeout: float,
    started_at: float,
) -> tuple[float, int, str]:
    payload = await _await_timeout(
        client.download_media(message, file=bytes),
        max(2.0, float(timeout)),
        "download_media",
    )
    downloaded = len(payload or b"")
    if downloaded > max_bytes:
        downloaded = max_bytes
    elapsed_ms = (time.perf_counter() - started_at) * 1000.0
    if _looks_like_dpi_window_block(downloaded):
        return elapsed_ms, downloaded, "dpi_16_20kb_suspected"
    return elapsed_ms, downloaded, ""


def _looks_like_dpi_window_block(downloaded: int) -> bool:
    return DPI_WINDOW_MIN_BYTES <= int(downloaded or 0) <= DPI_WINDOW_MAX_BYTES


def _probe_failure_note(exc: Exception, prefix: str) -> str:
    text = str(exc or "").lower()
    if isinstance(exc, OSError):
        return f"{prefix}_probe_failed:network_error"
    if isinstance(exc, (TypeError, AttributeError, ValueError, NotImplementedError)):
        return f"{prefix}_probe_failed:unsupported_media"
    if "timeout" in text:
        return f"{prefix}_probe_failed:timeout"
    return f"{prefix}_probe_failed:{type(exc).__name__}"


async def _disconnect_quietly(client: TelegramClient) -> None:
    with contextlib.suppress(Exception):
        await asyncio.wait_for(client.disconnect(), timeout=5.0)


async def _await_timeout(awaitable: Any, timeout: float, label: str) -> Any:
    try:
        return await asyncio.wait_for(awaitable, timeout=max(1.0, float(timeout)))
    except asyncio.TimeoutError as exc:
        raise RuntimeError(f"{label}_timeout") from exc


def _remaining(deadline: float, default_timeout: float) -> float:
    return max(1.0, min(default_timeout, deadline - time.perf_counter()))


def _load_session(path: Path) -> StringSession:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return StringSession()
    try:
        encrypted = path.read_bytes()
        payload = _decrypt_session_blob(path, encrypted)
        session_string = payload.decode("utf-8").strip()
        return StringSession(session_string)
    except Exception:
        return StringSession()


def _save_session(path: Path, client: TelegramClient) -> None:
    with contextlib.suppress(Exception):
        path.parent.mkdir(parents=True, exist_ok=True)
        session_string = StringSession.save(client.session) or ""
        protected = _encrypt_session_blob(path, session_string.encode("utf-8"))
        path.write_bytes(protected)
        _hide_windows_path(path.parent)
        _hide_windows_path(path)


def _delete_session(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def _hide_windows_path(path: Path) -> None:
    if hasattr(ctypes, "windll"):
        with contextlib.suppress(Exception):
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x02)


def _encrypt_session_blob(path: Path, payload: bytes) -> bytes:
    if win32crypt is not None:
        return win32crypt.CryptProtectData(payload, "telegram-session", None, None, None, 0)
    return _fernet_for_path(path).encrypt(payload)


def _decrypt_session_blob(path: Path, encrypted: bytes) -> bytes:
    if win32crypt is not None:
        _description, payload = win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)
        return payload
    return _fernet_for_path(path).decrypt(encrypted)


def _fernet_for_path(path: Path) -> Fernet:
    key_path = path.parent / SESSION_KEY_FILE_NAME
    if key_path.exists():
        key = key_path.read_bytes().strip()
    else:
        key = Fernet.generate_key()
        key_path.write_bytes(key)
        _hide_windows_path(key_path.parent)
        _hide_windows_path(key_path)
    return Fernet(key)
