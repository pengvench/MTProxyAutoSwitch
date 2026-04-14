from __future__ import annotations

import argparse
import asyncio
import contextlib
import html
import json
import logging
import os
import re
import statistics
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit
from urllib.request import Request, urlopen

from telethon import TelegramClient, functions
from telethon.network.connection.tcpmtproxy import ConnectionTcpMTProxyRandomizedIntermediate
from telethon.sessions import MemorySession
from TelethonFakeTLS import ConnectionTcpMTProxyFakeTLS

from mtproxy_net import create_insecure_ssl_context, create_verified_ssl_context, is_tls_verification_error


DEFAULT_SOURCES = [
    "https://mtpro.xyz/mtproto-ru",
    "https://mtpro.xyz/socks5-ru",
    "https://hookzof.github.io/mtpro.xyz/mtproto.html",
    "https://mtproxy.tg/",
    "https://mtproxytg.netlify.app/",
    "https://mtproxytg2.vercel.app/",
    "https://t.me/s/mtpro_xyz",
    "https://t.me/s/ProxyFree_Ru",
]

LIST_DIR_NAME = "list"
LIST_FILE_NAME = "proxy_list.txt"
REJECTED_FILE_NAME = "rejected_list.txt"
ALL_FILE_NAME = "all_list.txt"
SOCKS5_FILE_NAME = "socks5_list.txt"
REPORT_FILE_NAME = "report.json"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/135.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json,text/javascript,*/*;q=0.9",
    "Accept-Language": "ru,en-US;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

SCRIPT_SRC_RE = re.compile(r"<script[^>]+\bsrc=[\"']([^\"']+)[\"']", re.IGNORECASE)
CONFIG_URL_RE = re.compile(r"(?:API_URL|PUB_URL)\s*[:=]\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
JSON_URL_RE = re.compile(
    r"https?://[^\s\"'<>]+(?:\.json(?:[?#][^\s\"'<>]*)?|/api/\?type=[^\s\"'<>]+)",
    re.IGNORECASE,
)
PROXY_LINK_RE = re.compile(
    r"(?:https?://(?:t|telegram)\.me/proxy\?[^\s\"'<>]+|tg://proxy\?[^\s\"'<>]+)",
    re.IGNORECASE,
)
SOCKS_LINK_RE = re.compile(
    r"(?:https?://(?:t|telegram)\.me/socks\?[^\s\"'<>]+|tg://socks\?[^\s\"'<>]+|socks5h?://[^\s\"'<>]+)",
    re.IGNORECASE,
)
PROXY_OBJECT_RE = re.compile(
    r"\{[^{}]*?(?:host|server)\s*:\s*[\"'](?P<host>[^\"']+)[\"']"
    r"[^{}]*?port\s*:\s*(?P<port>\d+)"
    r"[^{}]*?secret\s*:\s*[\"'](?P<secret>[^\"']+)[\"'][^{}]*?\}",
    re.IGNORECASE | re.DOTALL,
)
HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
SECRET_RE = re.compile(r"^[0-9a-fA-F]{16,512}$")

PROBE_API_ID = 9
PROBE_API_HASH = "00000000000000000000000000000000"

logging.getLogger().setLevel(logging.CRITICAL)
logging.getLogger("telethon").setLevel(logging.CRITICAL)

LogSink = Callable[[str], None]
EventSink = Callable[[str, dict[str, Any]], None]


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("refresh_cancelled")


def run_async(coro):
    if sys.platform == "win32" and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        loop = asyncio.WindowsSelectorEventLoopPolicy().new_event_loop()
    else:
        loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        with contextlib.suppress(Exception):
            loop.run_until_complete(loop.shutdown_asyncgens())
        with contextlib.suppress(Exception):
            loop.run_until_complete(loop.shutdown_default_executor())
        asyncio.set_event_loop(None)
        loop.close()


@dataclass
class ProxyRecord:
    host: str
    port: int
    secret: str
    sources: set[str] = field(default_factory=set)
    discovered_from: set[str] = field(default_factory=set)

    @property
    def key(self) -> tuple[str, int, str]:
        return (self.host, self.port, self.secret)

    @property
    def url(self) -> str:
        return (
            f"https://t.me/proxy?server={quote(self.host, safe='')}"
            f"&port={self.port}&secret={self.secret}"
        )


@dataclass
class Socks5Record:
    host: str
    port: int
    username: str = ""
    password: str = ""
    sources: set[str] = field(default_factory=set)
    discovered_from: set[str] = field(default_factory=set)

    @property
    def key(self) -> tuple[str, int, str, str]:
        return (self.host, self.port, self.username, self.password)

    @property
    def url(self) -> str:
        user_query = f"&user={quote(self.username, safe='')}" if self.username else ""
        pass_query = f"&pass={quote(self.password, safe='')}" if self.password else ""
        return (
            f"https://t.me/socks?server={quote(self.host, safe='')}"
            f"&port={self.port}{user_query}{pass_query}"
        )


@dataclass
class ScanArtifacts:
    proxies: list[ProxyRecord] = field(default_factory=list)
    socks5: list[Socks5Record] = field(default_factory=list)
    data_urls: set[str] = field(default_factory=set)
    script_urls: set[str] = field(default_factory=set)


@dataclass
class SourceSummary:
    source_url: str
    fetched_urls: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class ProbeOutcome:
    proxy: ProxyRecord
    attempts: int
    successes: int
    failures: int
    success_rate: float
    avg_latency_ms: float | None
    p95_latency_ms: float | None
    min_latency_ms: float | None
    max_latency_ms: float | None
    high_latency_ratio: float
    max_consecutive_failures: int
    max_consecutive_high_latency: int
    accepted: bool
    reason: str
    elapsed_seconds: float
    early_stop: str | None


@dataclass(frozen=True)
class ProbeSettings:
    duration: float
    interval: float
    timeout: float
    max_latency_ms: float
    min_success_rate: float
    max_high_latency_ratio: float
    high_latency_streak: int
    unreachable_failures: int


@dataclass
class CollectorConfig:
    sources: list[str] = field(default_factory=lambda: list(DEFAULT_SOURCES))
    out_dir: Path = field(default_factory=lambda: Path(LIST_DIR_NAME))
    duration: float = 35.0
    interval: float = 3.0
    timeout: float = 8.0
    workers: int = 25
    max_latency_ms: float = 300.0
    min_success_rate: float = 0.7
    max_high_latency_ratio: float = 0.6
    high_latency_streak: int = 3
    max_proxies: int = 0
    fetch_timeout: float = 15.0
    verbose: bool = True


@dataclass
class CollectorRunResult:
    config: CollectorConfig
    source_summaries: list[SourceSummary]
    proxies: list[ProxyRecord]
    socks5: list[Socks5Record]
    outcomes: list[ProbeOutcome]
    working: list[ProbeOutcome]
    rejected: list[ProbeOutcome]
    out_dir: Path
    all_txt_path: Path
    working_txt_path: Path
    rejected_txt_path: Path
    socks5_all_txt_path: Path
    report_json_path: Path


class Fetcher:
    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        self.ssl_context = create_verified_ssl_context()
        self.insecure_ssl_context = create_insecure_ssl_context()
        self.allow_insecure_tls = os.environ.get("MTPROXY_ALLOW_INSECURE_TLS", "1").strip().lower() not in {"0", "false", "no", "off"}

    def fetch_text(self, url: str, referer: str | None = None) -> str:
        headers = dict(BROWSER_HEADERS)
        if referer:
            headers["Referer"] = referer
        request = Request(url, headers=headers)

        try:
            return self._fetch_text(request, self.ssl_context)
        except Exception as exc:
            if self.allow_insecure_tls and is_tls_verification_error(exc):
                try:
                    return self._fetch_text(request, self.insecure_ssl_context)
                except Exception as retry_exc:
                    exc = retry_exc
            if isinstance(exc, TimeoutError):
                raise RuntimeError(f"{url} -> timed out") from exc
            if isinstance(exc, HTTPError):
                raise RuntimeError(f"{url} -> HTTP {exc.code}") from exc
            if isinstance(exc, URLError):
                raise RuntimeError(f"{url} -> {exc.reason}") from exc
            if isinstance(exc, OSError):
                raise RuntimeError(f"{url} -> {exc}") from exc
            raise RuntimeError(f"{url} -> {exc}") from exc

    def _fetch_text(self, request: Request, context) -> str:
        with urlopen(request, timeout=self.timeout, context=context) as response:
            payload = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")


def emit_event(event_sink: EventSink | None, event_name: str, **payload: Any) -> None:
    if event_sink is not None:
        event_sink(event_name, payload)


def log(
    message: str,
    *,
    verbose_only: bool = False,
    verbose: bool = True,
    sink: LogSink | None = None,
) -> None:
    if verbose_only and not verbose:
        return
    if sink is not None:
        sink(message)
    else:
        print(message)


def percent(part: int, whole: int) -> float:
    if whole <= 0:
        return 0.0
    return part / whole


def percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * ratio)))
    return ordered[index]


def normalize_host(host: str) -> str | None:
    host = host.strip().rstrip(".").lower()
    if not host or "${" in host or "{{" in host:
        return None
    if not HOST_RE.fullmatch(host):
        return None
    return host


def normalize_secret(secret: str) -> str | None:
    secret = "".join(secret.strip().split()).lower()
    if not secret or "${" in secret or "{{" in secret:
        return None
    if not SECRET_RE.fullmatch(secret):
        return None
    return secret


def normalize_auth_value(value: str | None) -> str:
    if value is None:
        return ""
    return value.strip()


def make_proxy(host: str, port: Any, secret: str, source_url: str, found_in: str) -> ProxyRecord | None:
    host_value = normalize_host(host)
    secret_value = normalize_secret(secret)
    try:
        port_value = int(str(port).strip())
    except ValueError:
        return None

    if not host_value or not secret_value or not (1 <= port_value <= 65535):
        return None

    return ProxyRecord(
        host=host_value,
        port=port_value,
        secret=secret_value,
        sources={source_url},
        discovered_from={found_in},
    )


def make_socks5(
    host: str,
    port: Any,
    username: str | None,
    password: str | None,
    source_url: str,
    found_in: str,
) -> Socks5Record | None:
    host_value = normalize_host(host)
    try:
        port_value = int(str(port).strip())
    except ValueError:
        return None

    if not host_value or not (1 <= port_value <= 65535):
        return None

    return Socks5Record(
        host=host_value,
        port=port_value,
        username=normalize_auth_value(username),
        password=normalize_auth_value(password),
        sources={source_url},
        discovered_from={found_in},
    )


def parse_proxy_link(link: str, source_url: str, found_in: str) -> ProxyRecord | None:
    raw_link = html.unescape(link).replace("&#038;", "&")
    parsed = urlsplit(raw_link)
    query = parse_qs(parsed.query, keep_blank_values=True)
    host = query.get("server", [None])[0]
    port = query.get("port", [None])[0]
    secret = query.get("secret", [None])[0]
    if not host or not port or not secret:
        return None
    return make_proxy(host, port, secret, source_url, found_in)


def parse_socks5_link(link: str, source_url: str, found_in: str) -> Socks5Record | None:
    raw_link = html.unescape(link).replace("&#038;", "&")
    parsed = urlsplit(raw_link)
    if parsed.scheme.lower().startswith("socks5"):
        host = parsed.hostname
        port = parsed.port
        username = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        if host is None or port is None:
            return None
        return make_socks5(host, port, username, password, source_url, found_in)

    query = parse_qs(parsed.query, keep_blank_values=True)
    host = query.get("server", [None])[0]
    port = query.get("port", [None])[0]
    username = query.get("user", query.get("username", query.get("login", [""])))[0]
    password = query.get("pass", query.get("password", [""]))[0]
    if not host or not port:
        return None
    return make_socks5(host, port, username, password, source_url, found_in)


def parse_json_proxies(payload: Any, source_url: str, found_in: str) -> list[ProxyRecord]:
    results: list[ProxyRecord] = []

    if isinstance(payload, dict):
        candidates = []
        for key in ("proxies", "data", "items", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates.extend(value)
        if not candidates:
            candidates.append(payload)
    elif isinstance(payload, list):
        candidates = payload
    else:
        return results

    for item in candidates:
        if not isinstance(item, dict):
            continue
        host = item.get("host") or item.get("server") or item.get("ip")
        port = item.get("port")
        secret = item.get("secret")
        if host is None or port is None or secret is None:
            continue
        proxy = make_proxy(str(host), port, str(secret), source_url, found_in)
        if proxy:
            results.append(proxy)
    return results


def parse_json_socks5(payload: Any, source_url: str, found_in: str) -> list[Socks5Record]:
    results: list[Socks5Record] = []

    if isinstance(payload, dict):
        candidates = []
        for key in ("proxies", "data", "items", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates.extend(value)
        if not candidates:
            candidates.append(payload)
    elif isinstance(payload, list):
        candidates = payload
    else:
        return results

    for item in candidates:
        if not isinstance(item, dict):
            continue
        host = item.get("host") or item.get("server") or item.get("ip")
        port = item.get("port")
        protocol = str(item.get("type") or item.get("protocol") or item.get("scheme") or "").lower()
        username = item.get("user") or item.get("username") or item.get("login") or ""
        password = item.get("pass") or item.get("password") or ""
        if host is None or port is None:
            continue
        if "socks" not in protocol and not (username or password):
            continue
        proxy = make_socks5(str(host), port, str(username), str(password), source_url, found_in)
        if proxy is not None:
            results.append(proxy)
    return results


def extract_scripts(text: str, base_url: str) -> set[str]:
    urls: set[str] = set()
    for match in SCRIPT_SRC_RE.finditer(text):
        script_url = urljoin(base_url, html.unescape(match.group(1)))
        split = urlsplit(script_url)
        if split.scheme in {"http", "https"}:
            urls.add(script_url)
    return urls


def scan_text(text: str, source_url: str, current_url: str) -> ScanArtifacts:
    normalized = html.unescape(text).replace("\\/", "/").replace("&#038;", "&")
    artifacts = ScanArtifacts()

    for match in PROXY_LINK_RE.finditer(normalized):
        proxy = parse_proxy_link(match.group(0), source_url, current_url)
        if proxy:
            artifacts.proxies.append(proxy)

    for match in SOCKS_LINK_RE.finditer(normalized):
        proxy = parse_socks5_link(match.group(0), source_url, current_url)
        if proxy:
            artifacts.socks5.append(proxy)

    for match in PROXY_OBJECT_RE.finditer(normalized):
        proxy = make_proxy(
            match.group("host"),
            match.group("port"),
            match.group("secret"),
            source_url,
            current_url,
        )
        if proxy:
            artifacts.proxies.append(proxy)

    for match in CONFIG_URL_RE.finditer(normalized):
        artifacts.data_urls.add(urljoin(current_url, match.group(1)))

    for match in JSON_URL_RE.finditer(normalized):
        artifacts.data_urls.add(match.group(0))

    artifacts.script_urls = extract_scripts(normalized, current_url)
    return artifacts


def merge_proxy(registry: dict[tuple[str, int, str], ProxyRecord], proxy: ProxyRecord) -> None:
    existing = registry.get(proxy.key)
    if existing is None:
        registry[proxy.key] = proxy
        return
    existing.sources.update(proxy.sources)
    existing.discovered_from.update(proxy.discovered_from)


def merge_socks5(registry: dict[tuple[str, int, str, str], Socks5Record], proxy: Socks5Record) -> None:
    existing = registry.get(proxy.key)
    if existing is None:
        registry[proxy.key] = proxy
        return
    existing.sources.update(proxy.sources)
    existing.discovered_from.update(proxy.discovered_from)


def fetch_data_url(
    data_url: str,
    source_url: str,
    fetcher: Fetcher,
    registry: dict[tuple[str, int, str], ProxyRecord],
    socks5_registry: dict[tuple[str, int, str, str], Socks5Record],
    summary: SourceSummary,
    visited_data_urls: set[str],
    *,
    verbose: bool,
    log_sink: LogSink | None,
) -> None:
    if data_url in visited_data_urls:
        return
    visited_data_urls.add(data_url)
    summary.fetched_urls.append(data_url)

    try:
        payload = fetcher.fetch_text(data_url, referer=source_url)
    except RuntimeError as exc:
        summary.errors.append(str(exc))
        log(
            f"[source] data fetch failed: {exc}",
            verbose_only=True,
            verbose=verbose,
            sink=log_sink,
        )
        return

    try:
        parsed_json = json.loads(payload)
    except json.JSONDecodeError:
        parsed_json = None

    if parsed_json is not None:
        for proxy in parse_json_proxies(parsed_json, source_url, data_url):
            merge_proxy(registry, proxy)
        for proxy in parse_json_socks5(parsed_json, source_url, data_url):
            merge_socks5(socks5_registry, proxy)
        return

    artifacts = scan_text(payload, source_url, data_url)
    for proxy in artifacts.proxies:
        merge_proxy(registry, proxy)
    for proxy in artifacts.socks5:
        merge_socks5(socks5_registry, proxy)


def scrape_source(
    source_url: str,
    fetcher: Fetcher,
    registry: dict[tuple[str, int, str], ProxyRecord],
    socks5_registry: dict[tuple[str, int, str, str], Socks5Record],
    visited_data_urls: set[str],
    *,
    verbose: bool,
    log_sink: LogSink | None,
) -> SourceSummary:
    summary = SourceSummary(source_url=source_url)
    summary.fetched_urls.append(source_url)

    try:
        html_text = fetcher.fetch_text(source_url, referer=source_url)
    except RuntimeError as exc:
        summary.errors.append(str(exc))
        return summary

    artifacts = scan_text(html_text, source_url, source_url)
    for proxy in artifacts.proxies:
        merge_proxy(registry, proxy)
    for proxy in artifacts.socks5:
        merge_socks5(socks5_registry, proxy)

    data_urls = set(artifacts.data_urls)
    source_host = urlsplit(source_url).netloc
    for script_url in sorted(artifacts.script_urls):
        if urlsplit(script_url).netloc != source_host:
            continue
        if script_url in summary.fetched_urls:
            continue
        summary.fetched_urls.append(script_url)
        try:
            script_text = fetcher.fetch_text(script_url, referer=source_url)
        except RuntimeError as exc:
            summary.errors.append(str(exc))
            continue

        script_artifacts = scan_text(script_text, source_url, script_url)
        for proxy in script_artifacts.proxies:
            merge_proxy(registry, proxy)
        for proxy in script_artifacts.socks5:
            merge_socks5(socks5_registry, proxy)
        data_urls.update(script_artifacts.data_urls)

    for data_url in sorted(data_urls):
        fetch_data_url(
            data_url=data_url,
            source_url=source_url,
            fetcher=fetcher,
            registry=registry,
            socks5_registry=socks5_registry,
            summary=summary,
            visited_data_urls=visited_data_urls,
            verbose=verbose,
            log_sink=log_sink,
        )

    return summary


def create_probe_client(proxy: ProxyRecord, timeout: float) -> TelegramClient:
    if proxy.secret.startswith("ee"):
        connection = ConnectionTcpMTProxyFakeTLS
        proxy_tuple = (proxy.host, proxy.port, proxy.secret[2:])
    else:
        connection = ConnectionTcpMTProxyRandomizedIntermediate
        proxy_tuple = (proxy.host, proxy.port, proxy.secret)

    return TelegramClient(
        MemorySession(),
        PROBE_API_ID,
        PROBE_API_HASH,
        connection=connection,
        proxy=proxy_tuple,
        timeout=max(1, int(timeout)),
        connection_retries=0,
        request_retries=0,
        auto_reconnect=False,
        receive_updates=False,
    )


async def perform_mtproto_request(
    client: TelegramClient,
    request: object,
    timeout: float,
) -> float | None:
    try:
        started_at = time.perf_counter()
        await asyncio.wait_for(client(request), timeout=timeout)
        return (time.perf_counter() - started_at) * 1000.0
    except Exception:
        return None


async def disconnect_probe_client(client: TelegramClient, timeout: float) -> None:
    try:
        await asyncio.wait_for(client.disconnect(), timeout=timeout)
    except Exception:
        return
    background_tasks = [
        task
        for task in (
            getattr(client, "_updates_handle", None),
            getattr(client, "_keepalive_handle", None),
        )
        if task is not None
    ]
    if background_tasks:
        with contextlib.suppress(Exception):
            await asyncio.wait(background_tasks, timeout=timeout)
    disconnected = getattr(client, "disconnected", None)
    if disconnected is not None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(disconnected, timeout=timeout)


def classify_probe(
    proxy: ProxyRecord,
    latencies_ms: list[float],
    attempts: int,
    failures: int,
    max_consecutive_failures: int,
    max_consecutive_high_latency: int,
    settings: ProbeSettings,
    elapsed_seconds: float,
    early_stop: str | None,
) -> ProbeOutcome:
    successes = len(latencies_ms)
    high_latency_count = sum(value > settings.max_latency_ms for value in latencies_ms)
    success_rate = percent(successes, attempts)
    high_latency_ratio = percent(high_latency_count, successes)
    avg_latency_ms = statistics.mean(latencies_ms) if latencies_ms else None
    p95_latency_ms = percentile(latencies_ms, 0.95)
    min_latency_ms = min(latencies_ms) if latencies_ms else None
    max_latency_ms = max(latencies_ms) if latencies_ms else None

    accepted = True
    reason = "ok"
    if successes == 0:
        accepted = False
        reason = "unreachable"
    elif success_rate < settings.min_success_rate:
        accepted = False
        reason = "unstable"
    elif avg_latency_ms is not None and avg_latency_ms > settings.max_latency_ms:
        accepted = False
        reason = "high_latency"
    elif (
        max_consecutive_high_latency >= settings.high_latency_streak
        or high_latency_ratio >= settings.max_high_latency_ratio
    ):
        accepted = False
        reason = "high_latency"

    return ProbeOutcome(
        proxy=proxy,
        attempts=attempts,
        successes=successes,
        failures=failures,
        success_rate=success_rate,
        avg_latency_ms=avg_latency_ms,
        p95_latency_ms=p95_latency_ms,
        min_latency_ms=min_latency_ms,
        max_latency_ms=max_latency_ms,
        high_latency_ratio=high_latency_ratio,
        max_consecutive_failures=max_consecutive_failures,
        max_consecutive_high_latency=max_consecutive_high_latency,
        accepted=accepted,
        reason=reason,
        elapsed_seconds=elapsed_seconds,
        early_stop=early_stop,
    )


async def probe_proxy(
    proxy: ProxyRecord,
    settings: ProbeSettings,
    *,
    cancel_event: threading.Event | None = None,
) -> ProbeOutcome:
    started_at = time.perf_counter()
    deadline = started_at + settings.duration
    attempts = 0
    failures = 0
    latencies_ms: list[float] = []
    consecutive_failures = 0
    max_consecutive_failures = 0
    consecutive_high_latency = 0
    max_consecutive_high_latency = 0
    early_stop: str | None = None
    request_index = 0
    client: TelegramClient | None = None
    requests = [functions.help.GetConfigRequest(), functions.help.GetNearestDcRequest()]

    try:
        while True:
            _raise_if_cancelled(cancel_event)
            if client is None or not client.is_connected():
                try:
                    client = create_probe_client(proxy, timeout=settings.timeout)
                    await asyncio.wait_for(client.connect(), timeout=settings.timeout)
                except Exception:
                    if client is not None:
                        with contextlib.suppress(Exception):
                            await disconnect_probe_client(client, settings.timeout)
                    failures += 1
                    attempts += 1
                    consecutive_failures += 1
                    max_consecutive_failures = max(max_consecutive_failures, consecutive_failures)
                    consecutive_high_latency = 0
                    client = None
                    if not latencies_ms and consecutive_failures >= settings.unreachable_failures:
                        early_stop = "unreachable"
                        break
                    now = time.perf_counter()
                    if now >= deadline:
                        break
                    sleep_for = min(settings.interval, max(0.0, deadline - now))
                    if sleep_for > 0:
                        _raise_if_cancelled(cancel_event)
                        await asyncio.sleep(sleep_for)
                    continue

            request = requests[request_index % len(requests)]
            request_index += 1
            latency_ms = await perform_mtproto_request(client, request, settings.timeout)
            _raise_if_cancelled(cancel_event)
            attempts += 1

            if latency_ms is None:
                failures += 1
                consecutive_failures += 1
                max_consecutive_failures = max(max_consecutive_failures, consecutive_failures)
                consecutive_high_latency = 0
                try:
                    await disconnect_probe_client(client, settings.timeout)
                except Exception:
                    pass
                client = None
            else:
                latencies_ms.append(latency_ms)
                consecutive_failures = 0
                if latency_ms > settings.max_latency_ms:
                    consecutive_high_latency += 1
                    max_consecutive_high_latency = max(
                        max_consecutive_high_latency,
                        consecutive_high_latency,
                    )
                    if consecutive_high_latency >= settings.high_latency_streak:
                        early_stop = "high_latency"
                        break
                else:
                    consecutive_high_latency = 0

            now = time.perf_counter()
            if now >= deadline:
                break
            sleep_for = min(settings.interval, max(0.0, deadline - now))
            if sleep_for > 0:
                _raise_if_cancelled(cancel_event)
                await asyncio.sleep(sleep_for)
    finally:
        if client is not None:
            try:
                await disconnect_probe_client(client, settings.timeout)
            except Exception:
                pass

    elapsed_seconds = time.perf_counter() - started_at
    return classify_probe(
        proxy=proxy,
        latencies_ms=latencies_ms,
        attempts=attempts,
        failures=failures,
        max_consecutive_failures=max_consecutive_failures,
        max_consecutive_high_latency=max_consecutive_high_latency,
        settings=settings,
        elapsed_seconds=elapsed_seconds,
        early_stop=early_stop,
    )


async def probe_all(
    proxies: list[ProxyRecord],
    settings: ProbeSettings,
    concurrency: int,
    *,
    verbose: bool,
    log_sink: LogSink | None,
    event_sink: EventSink | None,
    cancel_event: threading.Event | None = None,
) -> list[ProbeOutcome]:
    semaphore = asyncio.Semaphore(concurrency)
    progress_lock = asyncio.Lock()
    results: list[ProbeOutcome] = []
    completed = 0
    total = len(proxies)

    async def runner(proxy: ProxyRecord) -> None:
        nonlocal completed
        _raise_if_cancelled(cancel_event)
        async with semaphore:
            _raise_if_cancelled(cancel_event)
            outcome = await probe_proxy(proxy, settings, cancel_event=cancel_event)
            results.append(outcome)

            if verbose:
                avg = (
                    f"{outcome.avg_latency_ms:.1f} ms"
                    if outcome.avg_latency_ms is not None
                    else "n/a"
                )
                log(
                    f"[probe] {proxy.host}:{proxy.port} -> {outcome.reason} "
                    f"(ok={outcome.successes}/{outcome.attempts}, avg={avg})",
                    verbose_only=True,
                    verbose=verbose,
                    sink=log_sink,
                )

            async with progress_lock:
                completed += 1
                completed_now = completed

            emit_event(
                event_sink,
                "probe_result",
                outcome=outcome,
                completed=completed_now,
                total=total,
            )

    _raise_if_cancelled(cancel_event)
    await asyncio.gather(*(runner(proxy) for proxy in proxies))
    return results


def write_text_file(path: Path, lines: list[str]) -> None:
    content = "\n".join(lines).strip()
    if content:
        content += "\n"
    path.write_text(content, encoding="utf-8")


def outcome_sort_key(outcome: ProbeOutcome) -> tuple[float, float, float, str]:
    avg_latency = outcome.avg_latency_ms if outcome.avg_latency_ms is not None else 10_000_000.0
    p95_latency = outcome.p95_latency_ms if outcome.p95_latency_ms is not None else 10_000_000.0
    return (-outcome.success_rate, avg_latency, p95_latency, outcome.proxy.url)


def build_report(
    source_summaries: list[SourceSummary],
    all_proxies: list[ProxyRecord],
    all_socks5: list[Socks5Record],
    outcomes: list[ProbeOutcome],
    config: CollectorConfig,
) -> dict[str, Any]:
    working = [item for item in outcomes if item.accepted]
    rejected = [item for item in outcomes if not item.accepted]

    return {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "settings": {
            "sources": list(config.sources),
            "duration_seconds": config.duration,
            "interval_seconds": config.interval,
            "timeout_seconds": config.timeout,
            "workers": config.workers,
            "max_latency_ms": config.max_latency_ms,
            "min_success_rate": config.min_success_rate,
            "max_high_latency_ratio": config.max_high_latency_ratio,
            "high_latency_streak": config.high_latency_streak,
            "max_proxies": config.max_proxies,
            "fetch_timeout": config.fetch_timeout,
        },
        "counts": {
            "unique_proxies": len(all_proxies),
            "unique_socks5": len(all_socks5),
            "working": len(working),
            "rejected": len(rejected),
        },
        "sources": [
            {
                "source_url": summary.source_url,
                "fetched_urls": summary.fetched_urls,
                "errors": summary.errors,
            }
            for summary in source_summaries
        ],
        "proxies": [
            {
                "url": outcome.proxy.url,
                "host": outcome.proxy.host,
                "port": outcome.proxy.port,
                "secret": outcome.proxy.secret,
                "sources": sorted(outcome.proxy.sources),
                "discovered_from": sorted(outcome.proxy.discovered_from),
                "accepted": outcome.accepted,
                "reason": outcome.reason,
                "attempts": outcome.attempts,
                "successes": outcome.successes,
                "failures": outcome.failures,
                "success_rate": round(outcome.success_rate, 4),
                "avg_latency_ms": round(outcome.avg_latency_ms, 2)
                if outcome.avg_latency_ms is not None
                else None,
                "p95_latency_ms": round(outcome.p95_latency_ms, 2)
                if outcome.p95_latency_ms is not None
                else None,
                "min_latency_ms": round(outcome.min_latency_ms, 2)
                if outcome.min_latency_ms is not None
                else None,
                "max_latency_ms": round(outcome.max_latency_ms, 2)
                if outcome.max_latency_ms is not None
                else None,
                "high_latency_ratio": round(outcome.high_latency_ratio, 4),
                "max_consecutive_failures": outcome.max_consecutive_failures,
                "max_consecutive_high_latency": outcome.max_consecutive_high_latency,
                "elapsed_seconds": round(outcome.elapsed_seconds, 2),
                "early_stop": outcome.early_stop,
            }
            for outcome in sorted(outcomes, key=lambda item: item.proxy.url)
        ],
        "socks5": [
            {
                "url": proxy.url,
                "host": proxy.host,
                "port": proxy.port,
                "username": proxy.username,
                "password": proxy.password,
                "sources": sorted(proxy.sources),
                "discovered_from": sorted(proxy.discovered_from),
            }
            for proxy in sorted(all_socks5, key=lambda item: item.url)
        ],
        "notes": [
            "Validation is performed through real Telegram MTProto requests over the proxy.",
            "Each successful sample includes connect + help.getConfig + help.getNearestDc round-trips.",
            "Media download verification is still not included because it requires an authenticated Telegram session.",
            "SOCKS5 links are exported separately and are not used by the local MTProto frontend.",
        ],
    }


def run_collection(
    config: CollectorConfig,
    *,
    log_sink: LogSink | None = None,
    event_sink: EventSink | None = None,
    write_output: bool = True,
    cancel_event: threading.Event | None = None,
) -> CollectorRunResult:
    out_dir = Path(config.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    fetcher = Fetcher(timeout=config.fetch_timeout)
    registry: dict[tuple[str, int, str], ProxyRecord] = {}
    socks5_registry: dict[tuple[str, int, str, str], Socks5Record] = {}
    source_summaries: list[SourceSummary] = []
    visited_data_urls: set[str] = set()

    log("[phase] scraping sources", verbose=config.verbose, sink=log_sink)
    emit_event(
        event_sink,
        "phase",
        phase="scraping",
        total_sources=len(config.sources),
        out_dir=str(out_dir),
    )

    for index, source_url in enumerate(config.sources, start=1):
        _raise_if_cancelled(cancel_event)
        log(f"[source] {source_url}", verbose=config.verbose, sink=log_sink)
        emit_event(
            event_sink,
            "source_started",
            source_url=source_url,
            index=index,
            total=len(config.sources),
        )

        summary = scrape_source(
            source_url=source_url,
            fetcher=fetcher,
            registry=registry,
            socks5_registry=socks5_registry,
            visited_data_urls=visited_data_urls,
            verbose=config.verbose,
            log_sink=log_sink,
        )
        source_summaries.append(summary)

        log(
            f"[source] done -> fetched={len(summary.fetched_urls)} errors={len(summary.errors)} "
            f"unique_total={len(registry)}",
            verbose=config.verbose,
            sink=log_sink,
        )
        emit_event(
            event_sink,
            "source_finished",
            source_url=source_url,
            index=index,
            total=len(config.sources),
            fetched=len(summary.fetched_urls),
            errors=len(summary.errors),
            unique_total=len(registry),
            summary=summary,
        )
        _raise_if_cancelled(cancel_event)

    proxies = sorted(registry.values(), key=lambda item: item.url)
    socks5 = sorted(socks5_registry.values(), key=lambda item: item.url)
    if config.max_proxies > 0:
        proxies = proxies[: config.max_proxies]

    if not proxies:
        raise RuntimeError("No MTProto proxies were collected from the configured sources.")

    log(f"[phase] probing {len(proxies)} unique proxies", verbose=config.verbose, sink=log_sink)
    emit_event(
        event_sink,
        "phase",
        phase="probing",
        total_proxies=len(proxies),
    )

    settings = ProbeSettings(
        duration=config.duration,
        interval=config.interval,
        timeout=config.timeout,
        max_latency_ms=config.max_latency_ms,
        min_success_rate=config.min_success_rate,
        max_high_latency_ratio=config.max_high_latency_ratio,
        high_latency_streak=config.high_latency_streak,
        unreachable_failures=3,
    )

    _raise_if_cancelled(cancel_event)
    outcomes = run_async(
        probe_all(
            proxies=proxies,
            settings=settings,
            concurrency=max(1, config.workers),
            verbose=config.verbose,
            log_sink=log_sink,
            event_sink=event_sink,
            cancel_event=cancel_event,
        )
    )

    working = sorted((item for item in outcomes if item.accepted), key=outcome_sort_key)
    rejected = sorted(
        (item for item in outcomes if not item.accepted),
        key=lambda item: (item.reason, outcome_sort_key(item)),
    )

    all_txt_path = out_dir / ALL_FILE_NAME
    working_txt_path = out_dir / LIST_FILE_NAME
    rejected_txt_path = out_dir / REJECTED_FILE_NAME
    socks5_all_txt_path = out_dir / SOCKS5_FILE_NAME
    report_json_path = out_dir / REPORT_FILE_NAME

    _raise_if_cancelled(cancel_event)
    if write_output:
        write_text_file(all_txt_path, [proxy.url for proxy in proxies])
        write_text_file(working_txt_path, [item.proxy.url for item in working])
        write_text_file(rejected_txt_path, [item.proxy.url for item in rejected])
        write_text_file(socks5_all_txt_path, [proxy.url for proxy in socks5])
        report_json_path.write_text(
            json.dumps(
                build_report(source_summaries, proxies, socks5, outcomes, config),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    result = CollectorRunResult(
        config=config,
        source_summaries=source_summaries,
        proxies=proxies,
        socks5=socks5,
        outcomes=outcomes,
        working=working,
        rejected=rejected,
        out_dir=out_dir,
        all_txt_path=all_txt_path,
        working_txt_path=working_txt_path,
        rejected_txt_path=rejected_txt_path,
        socks5_all_txt_path=socks5_all_txt_path,
        report_json_path=report_json_path,
    )

    if write_output:
        emit_event(
            event_sink,
            "files_written",
            out_dir=str(out_dir),
            all_txt_path=str(all_txt_path),
            working_txt_path=str(working_txt_path),
            rejected_txt_path=str(rejected_txt_path),
            socks5_all_txt_path=str(socks5_all_txt_path),
            report_json_path=str(report_json_path),
        )
    log(
        f"[done] unique={len(proxies)} socks5={len(socks5)} working={len(working)} rejected={len(rejected)} "
        f"out_dir={out_dir}",
        verbose=config.verbose,
        sink=log_sink,
    )
    log("[note] media validation is not part of the base mode.", verbose=config.verbose, sink=log_sink)
    emit_event(
        event_sink,
        "run_complete",
        working=len(working),
        rejected=len(rejected),
        unique=len(proxies),
        out_dir=str(out_dir),
    )
    return result


def config_from_args(args: argparse.Namespace) -> CollectorConfig:
    return CollectorConfig(
        sources=list(args.sources),
        out_dir=Path(args.out_dir),
        duration=args.duration,
        interval=args.interval,
        timeout=args.timeout,
        workers=args.workers,
        max_latency_ms=args.max_latency_ms,
        min_success_rate=args.min_success_rate,
        max_high_latency_ratio=args.max_high_latency_ratio,
        high_latency_streak=args.high_latency_streak,
        max_proxies=args.max_proxies,
        fetch_timeout=args.fetch_timeout,
        verbose=not args.quiet,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Collect MTProto proxy links from configured sources, deduplicate them, "
            "probe stability, and write TXT/JSON reports."
        )
    )
    parser.add_argument(
        "--sources",
        nargs="*",
        default=DEFAULT_SOURCES,
        help="Source URLs to scrape. Defaults to the six URLs requested in the task.",
    )
    parser.add_argument(
        "--out-dir",
        default=LIST_DIR_NAME,
        help="Output directory for txt/json files.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=35.0,
        help="Probe window per proxy in seconds. Default: 35.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=3.0,
        help="Delay between connect attempts in seconds. Default: 3.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=8.0,
        help="Timeout for one Telegram MTProto attempt in seconds. Default: 8.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=25,
        help="Max concurrent proxy probes. Default: 25.",
    )
    parser.add_argument(
        "--max-latency-ms",
        type=float,
        default=300.0,
        help="Latency above this value counts as high latency. Default: 300.",
    )
    parser.add_argument(
        "--min-success-rate",
        type=float,
        default=0.7,
        help="Minimum success ratio to keep a proxy. Default: 0.7.",
    )
    parser.add_argument(
        "--max-high-latency-ratio",
        type=float,
        default=0.6,
        help="Drop proxy if high latency dominates successful attempts. Default: 0.6.",
    )
    parser.add_argument(
        "--high-latency-streak",
        type=int,
        default=3,
        help="Drop proxy early after this many high-latency successes in a row. Default: 3.",
    )
    parser.add_argument(
        "--max-proxies",
        type=int,
        default=0,
        help="Optional cap for quick tests. 0 means no cap.",
    )
    parser.add_argument(
        "--fetch-timeout",
        type=float,
        default=15.0,
        help="Timeout for HTTP fetches while scraping sources. Default: 15.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Reduce console output.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run_collection(config_from_args(args))
    except RuntimeError as exc:
        print(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
