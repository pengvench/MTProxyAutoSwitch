from __future__ import annotations

import asyncio
import contextlib
import ctypes
import datetime
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telethon import TelegramClient, errors, types
from telethon.network.connection.tcpmtproxy import ConnectionTcpMTProxyRandomizedIntermediate
from telethon.sessions import StringSession
from TelethonFakeTLS import ConnectionTcpMTProxyFakeTLS
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
    "https://t.me/mtpro_xyz",
    "https://t.me/ProxyFree_Ru",
]
DEFAULT_AUTH_TIMEOUT = 20.0
DEFAULT_THREAD_TOTAL_TIMEOUT = 90.0
DEFAULT_THREAD_REQUEST_TIMEOUT = 12.0
DEFAULT_THREAD_MAX_MESSAGES = 8000
THREAD_PROGRESS_EVERY = 200
DEFAULT_QR_TOTAL_TIMEOUT = 90.0
SESSION_KEY_FILE_NAME = "session_key.bin"


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
        if upstream_proxy.secret.startswith("ee"):
            connection = ConnectionTcpMTProxyFakeTLS
            proxy_tuple = (upstream_proxy.host, upstream_proxy.port, upstream_proxy.secret[2:])
        else:
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
    client = build_client(config, upstream_proxy=upstream_proxy, timeout=DEFAULT_AUTH_TIMEOUT)
    try:
        await _await_timeout(client.connect(), DEFAULT_AUTH_TIMEOUT, "connect")
        sent = await _await_timeout(client.send_code_request(phone), DEFAULT_AUTH_TIMEOUT, "send_code")
        return {
            "phone_code_hash": sent.phone_code_hash,
            "type": type(sent.type).__name__ if sent.type is not None else "",
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
    client = build_client(config, upstream_proxy=upstream_proxy, timeout=DEFAULT_AUTH_TIMEOUT)
    try:
        await _await_timeout(client.connect(), DEFAULT_AUTH_TIMEOUT, "connect")
        try:
            await _await_timeout(
                client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash),
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
) -> list[ProxyRecord]:
    _ensure_auth_config(config)
    username, thread_id = parse_thread_url(thread_url)
    source_url = thread_url.strip()
    client = build_client(config, upstream_proxy=upstream_proxy, timeout=request_timeout)
    registry: dict[tuple[str, int, str], ProxyRecord] = {}
    deadline = time.perf_counter() + max(5.0, float(total_timeout))
    scanned_messages = 0
    timed_out = False
    hit_limit = False

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
        if root_message is not None:
            for proxy in _extract_message_proxies(root_message, source_url):
                registry[proxy.key] = proxy

        iterator = client.iter_messages(entity, reply_to=thread_id, limit=None)
        while True:
            if time.perf_counter() >= deadline:
                timed_out = True
                break
            if scanned_messages >= max_messages:
                hit_limit = True
                break
            try:
                message = await _await_timeout(
                    iterator.__anext__(),
                    _remaining(deadline, request_timeout),
                    "iter_messages",
                )
            except StopAsyncIteration:
                break
            scanned_messages += 1
            for proxy in _extract_message_proxies(message, source_url):
                registry[proxy.key] = proxy
            if log_sink is not None and scanned_messages % THREAD_PROGRESS_EVERY == 0:
                log_sink(
                    f"[thread] scanned={scanned_messages} proxies={len(registry)} "
                    f"source={thread_url}"
                )

        if log_sink is not None:
            suffix = ""
            if timed_out:
                suffix = f" partial_timeout_after={scanned_messages}"
            elif hit_limit:
                suffix = f" partial_limit={max_messages}"
            log_sink(f"[thread] {thread_url} -> {len(registry)} proxies{suffix}")
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
) -> list[ProxyRecord]:
    _ensure_auth_config(config)
    spec = parse_telegram_source_url(source_url)
    client = build_client(config, upstream_proxy=upstream_proxy, timeout=request_timeout)
    registry: dict[tuple[str, int, str], ProxyRecord] = {}
    deadline = time.perf_counter() + max(5.0, float(total_timeout))
    scanned_messages = 0
    timed_out = False
    hit_limit = False

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
            if root_message is not None:
                scanned_messages += 1
                for proxy in _extract_message_proxies(root_message, spec.normalized_url):
                    registry[proxy.key] = proxy

            with contextlib.suppress(Exception):
                iterator = client.iter_messages(entity, reply_to=spec.message_id, limit=None)
                while True:
                    if time.perf_counter() >= deadline:
                        timed_out = True
                        break
                    if scanned_messages >= max_messages:
                        hit_limit = True
                        break
                    try:
                        message = await _await_timeout(
                            iterator.__anext__(),
                            _remaining(deadline, request_timeout),
                            "iter_messages",
                        )
                    except StopAsyncIteration:
                        break
                    scanned_messages += 1
                    for proxy in _extract_message_proxies(message, spec.normalized_url):
                        registry[proxy.key] = proxy
                    if log_sink is not None and scanned_messages % THREAD_PROGRESS_EVERY == 0:
                        log_sink(
                            f"[telegram] scanned={scanned_messages} proxies={len(registry)} "
                            f"source={spec.normalized_url}"
                        )
        else:
            iterator = client.iter_messages(entity, limit=None)
            while True:
                if time.perf_counter() >= deadline:
                    timed_out = True
                    break
                if scanned_messages >= max_messages:
                    hit_limit = True
                    break
                try:
                    message = await _await_timeout(
                        iterator.__anext__(),
                        _remaining(deadline, request_timeout),
                        "iter_messages",
                    )
                except StopAsyncIteration:
                    break
                scanned_messages += 1
                for proxy in _extract_message_proxies(message, spec.normalized_url):
                    registry[proxy.key] = proxy
                if log_sink is not None and scanned_messages % THREAD_PROGRESS_EVERY == 0:
                    log_sink(
                        f"[telegram] scanned={scanned_messages} proxies={len(registry)} "
                        f"source={spec.normalized_url}"
                    )

        if log_sink is not None:
            suffix = ""
            if timed_out:
                suffix = f" partial_timeout_after={scanned_messages}"
            elif hit_limit:
                suffix = f" partial_limit={max_messages}"
            log_sink(f"[telegram] {spec.normalized_url} -> {len(registry)} proxies{suffix}")
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
) -> list[ProxyRecord]:
    unique_urls: list[str] = []
    seen_urls: set[str] = set()
    for raw_url in source_urls:
        url = str(raw_url).strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        unique_urls.append(url)

    registry: dict[tuple[str, int, str], ProxyRecord] = {}
    for source_url in unique_urls:
        proxies = await collect_telegram_source_proxies(
            source_url,
            config,
            upstream_proxy=upstream_proxy,
            log_sink=log_sink,
            total_timeout=total_timeout,
            request_timeout=request_timeout,
            max_messages=max_messages,
        )
        for proxy in proxies:
            registry[proxy.key] = proxy
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

        results: list[tuple[str, float, int]] = []
        for username in sample_channels:
            entity = await _await_timeout(client.get_entity(username), _remaining(deadline, 8.0), "get_entity")
            iterator = client.iter_messages(entity, limit=30)
            while True:
                if time.perf_counter() >= deadline:
                    raise RuntimeError("media_probe_timeout")
                try:
                    message = await _await_timeout(iterator.__anext__(), _remaining(deadline, 8.0), "iter_messages")
                except StopAsyncIteration:
                    break
                media_kind = _detect_media_kind(message)
                if media_kind is None:
                    continue
                elapsed_ms, downloaded = await _download_sample_bytes(client, message, timeout=_remaining(deadline, 12.0))
                if downloaded > 0:
                    results.append((media_kind, elapsed_ms, downloaded))
                break
            if len(results) >= 2:
                break

        if not results:
            return MediaProbeResult(proxy.key, None, "no_media_samples_found", None)

        avg_latency = sum(item[1] for item in results) / len(results)
        score = 1.0
        if avg_latency > 3_000.0:
            score = 0.35
        elif avg_latency > 2_000.0:
            score = 0.55
        elif avg_latency > 1_200.0:
            score = 0.75

        covered = "+".join(sorted({item[0] for item in results}))
        return MediaProbeResult(
            proxy.key,
            score,
            f"{covered} ok",
            round((time.perf_counter() - started_at) * 1000.0, 2),
        )
    except Exception as exc:
        return MediaProbeResult(
            proxy.key,
            None,
            f"media_probe_failed:{type(exc).__name__}",
            round((time.perf_counter() - started_at) * 1000.0, 2),
        )
    finally:
        _save_session(config.session_path, client)
        await _disconnect_quietly(client)


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


def _detect_media_kind(message: Any) -> str | None:
    if getattr(message, "photo", None) is not None:
        return "photo"

    document = getattr(message, "document", None)
    if document is None:
        media = getattr(message, "media", None)
        document = getattr(getattr(media, "document", None), "document", None)

    if document is None:
        return None

    for attribute in getattr(document, "attributes", []) or []:
        if getattr(attribute, "voice", False):
            return "voice"
        if getattr(attribute, "round_message", False):
            return "video_note"
    return "document"


async def _download_sample_bytes(
    client: TelegramClient,
    message: Any,
    max_bytes: int = 256 * 1024,
    timeout: float = 12.0,
) -> tuple[float, int]:
    started_at = time.perf_counter()
    downloaded = 0
    iterator = client.iter_download(message.media, request_size=64 * 1024)
    deadline = time.perf_counter() + max(2.0, float(timeout))
    while True:
        if time.perf_counter() >= deadline:
            break
        try:
            chunk = await _await_timeout(iterator.__anext__(), _remaining(deadline, 6.0), "iter_download")
        except StopAsyncIteration:
            break
        downloaded += len(chunk)
        if downloaded >= max_bytes:
            break
    elapsed_ms = (time.perf_counter() - started_at) * 1000.0
    return elapsed_ms, downloaded


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
