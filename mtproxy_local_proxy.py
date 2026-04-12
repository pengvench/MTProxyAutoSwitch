from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import os
import random
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from TelethonFakeTLS.FakeTLS.FakeTLSHello import MTProxyFakeTLSClientCodec
from TelethonFakeTLS.FakeTLS.TLSInOut import FakeTLSStreamReader, FakeTLSStreamWriter

from mtproxy_collector import ProbeOutcome, ProxyRecord

HANDSHAKE_LEN = 64
SKIP_LEN = 8
PREKEY_LEN = 32
IV_LEN = 16
KEY_LEN = 32
PROTO_TAG_POS = 56
DC_IDX_POS = 60
ZERO_64 = b"\x00" * 64

PROTO_TAG_ABRIDGED = b"\xef\xef\xef\xef"
PROTO_TAG_INTERMEDIATE = b"\xee\xee\xee\xee"
PROTO_TAG_SECURE = b"\xdd\xdd\xdd\xdd"

RESERVED_FIRST_BYTES = {0xEF}
RESERVED_STARTS = {
    b"HEAD",
    b"POST",
    b"GET ",
    b"\xee\xee\xee\xee",
    b"\xdd\xdd\xdd\xdd",
    b"\x16\x03\x01\x02",
}
RESERVED_CONTINUE = b"\x00\x00\x00\x00"

TLS_RECORD_HANDSHAKE = 0x16
TLS_RECORD_CCS = 0x14
TLS_RECORD_APPDATA = 0x17
CLIENT_RANDOM_OFFSET = 11
CLIENT_RANDOM_LEN = 32
SESSION_ID_OFFSET = 44
SESSION_ID_LEN = 32
TIMESTAMP_TOLERANCE = 120
TLS_APPDATA_MAX = 16384
DEFAULT_FAKE_TLS_DOMAIN = "ya.ru"
_CCS_FRAME = b"\x14\x03\x03\x00\x01\x01"
_SERVER_HELLO_TEMPLATE = bytearray(
    b"\x16\x03\x03\x00\x7a"
    b"\x02\x00\x00\x76"
    b"\x03\x03"
    + b"\x00" * 32
    + b"\x20"
    + b"\x00" * 32
    + b"\x13\x01\x00"
    + b"\x00\x2e"
    + b"\x00\x33\x00\x24\x00\x1d\x00\x20"
    + b"\x00" * 32
    + b"\x00\x2b\x00\x02\x03\x04"
)
_SH_RANDOM_OFF = 11
_SH_SESSID_OFF = 44
_SH_PUBKEY_OFF = 89


@dataclass
class RuntimeCounters:
    selected_count: int = 0
    active_connections: int = 0
    successful_sessions: int = 0
    failed_sessions: int = 0
    recent_failures: int = 0
    recent_successes: int = 0
    live_latency_ms: float | None = None
    last_selected_at: float = 0.0
    last_success_at: float = 0.0
    last_failure_at: float = 0.0
    last_error: str = ""
    total_bytes_up: int = 0
    total_bytes_down: int = 0
    media_successes: int = 0
    media_failures: int = 0
    deep_media_score: float | None = None
    deep_media_note: str = ""
    consecutive_high_latency: int = 0
    consecutive_media_failures: int = 0
    cooldown_until: float = 0.0
    cooldown_reason: str = ""


@dataclass
class UpstreamProxyState:
    outcome: ProbeOutcome
    counters: RuntimeCounters = field(default_factory=RuntimeCounters)

    @property
    def proxy(self) -> ProxyRecord:
        return self.outcome.proxy

    @property
    def key(self) -> tuple[str, int, str]:
        return self.proxy.key

    @property
    def avg_latency_ms(self) -> float:
        if self.counters.live_latency_ms is not None:
            return self.counters.live_latency_ms
        if self.outcome.avg_latency_ms is not None:
            return self.outcome.avg_latency_ms
        return 9_999.0

    @property
    def media_score(self) -> float:
        total = self.counters.media_successes + self.counters.media_failures
        if total > 0:
            return self.counters.media_successes / total
        if self.counters.deep_media_score is not None:
            return self.counters.deep_media_score
        return -1.0

    @property
    def runtime_success_rate(self) -> float:
        total = self.counters.recent_successes + self.counters.recent_failures
        if total <= 0:
            return self.outcome.success_rate
        return self.counters.recent_successes / total


class ProxyPool:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[tuple[str, int, str], UpstreamProxyState] = {}

    def replace_outcomes(self, outcomes: list[ProbeOutcome]) -> None:
        with self._lock:
            next_states: dict[tuple[str, int, str], UpstreamProxyState] = {}
            for outcome in outcomes:
                if not outcome.accepted:
                    continue
                current = self._states.get(outcome.proxy.key)
                if current is None:
                    next_states[outcome.proxy.key] = UpstreamProxyState(outcome=outcome)
                else:
                    current.outcome = outcome
                    next_states[outcome.proxy.key] = current
            self._states = next_states

    def update_deep_media_score(
        self,
        proxy_key: tuple[str, int, str],
        score: float | None,
        note: str,
    ) -> None:
        with self._lock:
            state = self._states.get(proxy_key)
            if state is None:
                return
            state.counters.deep_media_score = score
            state.counters.deep_media_note = note

    def update_live_probe(
        self,
        proxy_key: tuple[str, int, str],
        latency_ms: float | None,
        ok: bool,
        reason: str,
        *,
        max_latency_ms: float = 300.0,
        high_latency_streak_limit: int = 3,
        failure_limit: int = 3,
        cooldown_seconds: float = 120.0,
    ) -> str | None:
        with self._lock:
            state = self._states.get(proxy_key)
            if state is None:
                return None
            soft_latency_limit = min(max(80.0, float(max_latency_ms or 300.0)), 150.0)
            if ok and latency_ms is not None:
                state.counters.live_latency_ms = latency_ms
                state.counters.recent_successes = min(50, state.counters.recent_successes + 1)
                state.counters.recent_failures = max(0, state.counters.recent_failures - 1)
                state.counters.last_success_at = time.time()
                if latency_ms > soft_latency_limit:
                    state.counters.consecutive_high_latency += 1
                    if state.counters.consecutive_high_latency >= max(1, high_latency_streak_limit):
                        return self._enter_cooldown(
                            state,
                            reason=f"high_latency:{int(round(latency_ms))}ms",
                            cooldown_seconds=cooldown_seconds,
                        )
                else:
                    state.counters.consecutive_high_latency = 0
            else:
                state.counters.recent_failures = min(50, state.counters.recent_failures + 1)
                state.counters.last_failure_at = time.time()
                state.counters.last_error = reason
                state.counters.consecutive_high_latency = 0
                if state.counters.recent_failures >= max(1, failure_limit):
                    return self._enter_cooldown(
                        state,
                        reason=reason or "live_probe_failed",
                        cooldown_seconds=cooldown_seconds,
                    )
            return None

    def update_background_media_probe(
        self,
        proxy_key: tuple[str, int, str],
        score: float | None,
        note: str,
        *,
        failure_score: float = 0.6,
        cooldown_seconds: float = 300.0,
    ) -> str | None:
        with self._lock:
            state = self._states.get(proxy_key)
            if state is None:
                return None
            state.counters.deep_media_score = score
            state.counters.deep_media_note = note
            if score is None or score < failure_score:
                state.counters.consecutive_media_failures += 1
                return self._enter_cooldown(
                    state,
                    reason=f"media:{note or 'failed'}",
                    cooldown_seconds=cooldown_seconds,
                )
            state.counters.consecutive_media_failures = 0
            return None

    def select_candidates(self, *, is_media: bool, limit: int = 5) -> list[UpstreamProxyState]:
        with self._lock:
            candidates = self._available_states()
            if not candidates:
                candidates = list(self._states.values())
            ordered = sorted(candidates, key=lambda item: self._score(item, is_media), reverse=True)
            return ordered[:limit]

    def select_monitor_targets(self, *, limit: int = 2) -> list[UpstreamProxyState]:
        with self._lock:
            candidates = self._available_states()
            if not candidates:
                candidates = list(self._states.values())
            ordered = sorted(
                candidates,
                key=lambda item: (
                    item.counters.active_connections > 0,
                    item.counters.selected_count,
                    self._score(item, True),
                    self._score(item, False),
                ),
                reverse=True,
            )
            return ordered[:limit]

    def mark_selected(self, proxy_key: tuple[str, int, str], latency_ms: float | None) -> None:
        with self._lock:
            state = self._states.get(proxy_key)
            if state is None:
                return
            state.counters.selected_count += 1
            state.counters.active_connections += 1
            state.counters.last_selected_at = time.time()
            if latency_ms is not None:
                state.counters.live_latency_ms = latency_ms
                state.counters.last_success_at = time.time()
                state.counters.recent_successes = min(50, state.counters.recent_successes + 1)
                state.counters.recent_failures = max(0, state.counters.recent_failures - 1)

    def mark_session_result(
        self,
        proxy_key: tuple[str, int, str],
        *,
        ok: bool,
        is_media: bool,
        bytes_up: int,
        bytes_down: int,
        error: str = "",
    ) -> None:
        with self._lock:
            state = self._states.get(proxy_key)
            if state is None:
                return
            state.counters.active_connections = max(0, state.counters.active_connections - 1)
            state.counters.total_bytes_up += bytes_up
            state.counters.total_bytes_down += bytes_down
            if ok:
                state.counters.successful_sessions += 1
                state.counters.recent_successes = min(50, state.counters.recent_successes + 1)
                state.counters.recent_failures = max(0, state.counters.recent_failures - 1)
                state.counters.last_success_at = time.time()
                if is_media:
                    state.counters.media_successes += 1
            else:
                state.counters.failed_sessions += 1
                state.counters.recent_failures = min(50, state.counters.recent_failures + 1)
                state.counters.last_failure_at = time.time()
                state.counters.last_error = error
                if is_media:
                    state.counters.media_failures += 1

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = []
            now = time.time()
            for state in sorted(self._states.values(), key=lambda item: self._score(item, False), reverse=True):
                cooldown_remaining = max(0.0, state.counters.cooldown_until - now)
                runtime_state = (
                    f"cooldown {int(round(cooldown_remaining))}s: {state.counters.cooldown_reason}"
                    if cooldown_remaining > 0
                    else state.outcome.reason
                )
                rows.append(
                    {
                        "key": state.key,
                        "host": state.proxy.host,
                        "port": state.proxy.port,
                        "url": state.proxy.url,
                        "base_latency_ms": state.outcome.avg_latency_ms,
                        "live_latency_ms": state.counters.live_latency_ms,
                        "success_rate": state.outcome.success_rate,
                        "runtime_success_rate": state.runtime_success_rate,
                        "selected_count": state.counters.selected_count,
                        "active_connections": state.counters.active_connections,
                        "media_score": state.media_score,
                        "deep_media_score": state.counters.deep_media_score,
                        "deep_media_note": state.counters.deep_media_note,
                        "last_error": state.counters.cooldown_reason or state.counters.last_error,
                        "reason": runtime_state,
                        "score": round(self._score(state, False), 2),
                    }
                )
            return rows

    def count(self) -> int:
        with self._lock:
            return len(self._states)

    def best(self) -> UpstreamProxyState | None:
        items = self.select_candidates(is_media=False, limit=1)
        return items[0] if items else None

    def _score(self, state: UpstreamProxyState, is_media: bool) -> float:
        base_latency = state.avg_latency_ms
        success_rate = max(state.outcome.success_rate, state.runtime_success_rate)
        score = success_rate * 650.0
        score -= base_latency * 3.6
        if base_latency > 80.0:
            score -= (base_latency - 80.0) * 1.2
        if base_latency > 140.0:
            score -= (base_latency - 140.0) * 2.4
        if base_latency > 220.0:
            score -= (base_latency - 220.0) * 3.2
        score -= state.counters.recent_failures * 120.0
        score += min(20, state.counters.recent_successes) * 12.0
        score -= state.counters.active_connections * 18.0

        media_score = state.media_score
        if media_score >= 0:
            score += media_score * (460.0 if is_media else 110.0)
        elif is_media:
            score -= 60.0

        if state.counters.deep_media_score is not None:
            score += state.counters.deep_media_score * (240.0 if is_media else 80.0)

        cooldown_remaining = state.counters.cooldown_until - time.time()
        if cooldown_remaining > 0:
            score -= 10_000.0 + min(1_000.0, cooldown_remaining)
        return score

    def _available_states(self) -> list[UpstreamProxyState]:
        now = time.time()
        return [state for state in self._states.values() if state.counters.cooldown_until <= now]

    def _enter_cooldown(
        self,
        state: UpstreamProxyState,
        *,
        reason: str,
        cooldown_seconds: float,
    ) -> str:
        until = time.time() + max(30.0, float(cooldown_seconds))
        state.counters.cooldown_until = max(state.counters.cooldown_until, until)
        state.counters.cooldown_reason = reason
        state.counters.last_error = reason
        return reason


class LocalMTProxyServer:
    def __init__(
        self,
        pool: ProxyPool,
        *,
        host: str = "127.0.0.1",
        port: int = 1443,
        secret: str,
        fake_tls_enabled: bool = False,
        fake_tls_domain: str = "",
        connect_timeout: float = 8.0,
        log_sink: Any | None = None,
        event_sink: Any | None = None,
    ) -> None:
        self.pool = pool
        self.host = host
        self.port = port
        self.secret = secret.lower()
        self.fake_tls_enabled = bool(fake_tls_enabled)
        self.fake_tls_domain = _normalize_fake_tls_domain(
            fake_tls_domain or (DEFAULT_FAKE_TLS_DOMAIN if self.fake_tls_enabled else "")
        )
        self.connect_timeout = connect_timeout
        self.log_sink = log_sink
        self.event_sink = event_sink
        self._server: asyncio.base_events.Server | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop_event: asyncio.Event | None = None
        self._local_secret_bytes = _normalize_proxy_secret(self.secret)

    @property
    def local_proxy_url(self) -> str:
        return (
            f"https://t.me/proxy?server={self.host}"
            f"&port={self.port}&secret={self.link_secret}"
        )

    @property
    def local_proxy_tg_url(self) -> str:
        return (
            f"tg://proxy?server={self.host}"
            f"&port={self.port}&secret={self.link_secret}"
        )

    @property
    def link_secret(self) -> str:
        if self.fake_tls_enabled and self.fake_tls_domain:
            return f"ee{self.secret}{self.fake_tls_domain.encode('ascii').hex()}"
        return self.secret

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running():
            return
        self._thread = threading.Thread(target=self._thread_main, daemon=True, name="mtproxy-local-server")
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        loop = self._loop
        stop_event = self._stop_event
        if loop is None or stop_event is None:
            return
        if loop.is_closed():
            self._thread = None
            self._loop = None
            self._stop_event = None
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(stop_event.set)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None
        self._loop = None
        self._stop_event = None

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        stop_event = asyncio.Event()
        self._stop_event = stop_event
        try:
            loop.run_until_complete(self._run(stop_event))
        except Exception as exc:
            self._log(f"[local] failed to start on {self.host}:{self.port}: {exc}")
            self._emit("local_server_state", running=False, host=self.host, port=self.port, error=str(exc))
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            with contextlib.suppress(Exception):
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._server = None
            loop.close()

    async def _run(self, stop_event: asyncio.Event) -> None:
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
        self._log(f"[local] listening on {self.host}:{self.port}")
        self._emit("local_server_state", running=True, host=self.host, port=self.port)
        async with self._server:
            await stop_event.wait()
        self._emit("local_server_state", running=False, host=self.host, port=self.port)
        self._log("[local] stopped")

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        label = f"{peer[0]}:{peer[1]}" if peer else "?"
        started_at = time.perf_counter()
        bytes_up = 0
        bytes_down = 0
        chosen_state: UpstreamProxyState | None = None
        is_media = False
        client_reader: Any = reader
        client_writer: Any = writer

        try:
            if self.fake_tls_enabled:
                handshake, client_reader, client_writer = await self._accept_fake_tls_client(reader, writer, label)
            else:
                handshake = await asyncio.wait_for(reader.readexactly(HANDSHAKE_LEN), timeout=self.connect_timeout)
            parsed = _try_handshake(handshake, self._local_secret_bytes)
            if parsed is None:
                self._log(f"[local] bad handshake from {label}")
                return

            dc_id, is_media, proto_tag, client_prekey_iv = parsed
            dc_idx = -dc_id if is_media else dc_id
            local_dec, local_enc = _build_local_ciphers(client_prekey_iv, self._local_secret_bytes)
            candidates = self.pool.select_candidates(is_media=is_media, limit=5)
            if not candidates:
                self._log("[local] no working upstream proxies in pool")
                return

            upstream_reader = None
            upstream_writer = None
            upstream_dec = None
            upstream_enc = None
            connect_errors: list[str] = []

            for state in candidates:
                try:
                    (
                        upstream_reader,
                        upstream_writer,
                        upstream_enc,
                        upstream_dec,
                        latency_ms,
                    ) = await self._connect_upstream(state.proxy, dc_idx, proto_tag)
                except Exception as exc:
                    connect_errors.append(f"{state.proxy.host}:{state.proxy.port} -> {exc}")
                    self.pool.mark_session_result(
                        state.key,
                        ok=False,
                        is_media=is_media,
                        bytes_up=0,
                        bytes_down=0,
                        error=str(exc),
                    )
                    continue

                chosen_state = state
                self.pool.mark_selected(state.key, latency_ms)
                self._emit(
                    "local_upstream_selected",
                    host=state.proxy.host,
                    port=state.proxy.port,
                    latency_ms=latency_ms,
                    is_media=is_media,
                    dc_id=dc_id,
                )
                break

            if chosen_state is None or upstream_reader is None or upstream_writer is None:
                if connect_errors:
                    self._log(f"[local] upstream connect failed: {' | '.join(connect_errors[:3])}")
                return

            async def client_to_upstream() -> None:
                nonlocal bytes_up
                while True:
                    chunk = await client_reader.read(65536)
                    if not chunk:
                        break
                    plain = local_dec.update(chunk)
                    encrypted = upstream_enc.update(plain)
                    upstream_writer.write(encrypted)
                    await upstream_writer.drain()
                    bytes_up += len(chunk)

            async def upstream_to_client() -> None:
                nonlocal bytes_down
                while True:
                    chunk = await upstream_reader.read(65536)
                    if not chunk:
                        break
                    plain = upstream_dec.update(chunk)
                    encrypted = local_enc.update(plain)
                    client_writer.write(encrypted)
                    await client_writer.drain()
                    bytes_down += len(chunk)

            tasks = [
                asyncio.create_task(client_to_upstream()),
                asyncio.create_task(upstream_to_client()),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                exc = task.exception()
                if exc is not None:
                    raise exc
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        except Exception as exc:
            if chosen_state is not None:
                self.pool.mark_session_result(
                    chosen_state.key,
                    ok=False,
                    is_media=is_media,
                    bytes_up=bytes_up,
                    bytes_down=bytes_down,
                    error=str(exc),
                )
            self._log(f"[local] {label} -> {exc}")
        finally:
            duration = time.perf_counter() - started_at
            if chosen_state is not None:
                success = bytes_down > 0 or duration >= 2.0
                self.pool.mark_session_result(
                    chosen_state.key,
                    ok=success,
                    is_media=is_media,
                    bytes_up=bytes_up,
                    bytes_down=bytes_down,
                    error="" if success else "session_closed_early",
                )
                self._emit(
                    "local_session_closed",
                    host=chosen_state.proxy.host,
                    port=chosen_state.proxy.port,
                    is_media=is_media,
                    bytes_up=bytes_up,
                    bytes_down=bytes_down,
                    duration_seconds=round(duration, 2),
                    success=success,
                )
            with contextlib.suppress(Exception):
                client_writer.close()
            with contextlib.suppress(Exception):
                await client_writer.wait_closed()

    async def _accept_fake_tls_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        label: str,
    ) -> tuple[bytes, Any, Any]:
        first_byte = await asyncio.wait_for(reader.readexactly(1), timeout=self.connect_timeout)
        if first_byte[0] != TLS_RECORD_HANDSHAKE:
            raise RuntimeError("fake_tls_expected_client_hello")

        header_rest = await asyncio.wait_for(reader.readexactly(4), timeout=self.connect_timeout)
        tls_header = first_byte + header_rest
        record_len = struct.unpack(">H", tls_header[3:5])[0]
        record_body = await asyncio.wait_for(reader.readexactly(record_len), timeout=self.connect_timeout)
        client_hello = tls_header + record_body

        tls_result = _verify_fake_tls_client_hello(client_hello, self._local_secret_bytes)
        if tls_result is None:
            raise RuntimeError("fake_tls_handshake_failed")

        client_random, session_id, _timestamp = tls_result
        writer.write(_build_fake_tls_server_hello(self._local_secret_bytes, client_random, session_id))
        await writer.drain()

        wrapped_reader = FakeTlsStreamReader(reader)
        wrapped_writer = FakeTlsStreamWriter(writer)
        handshake = await asyncio.wait_for(wrapped_reader.readexactly(HANDSHAKE_LEN), timeout=self.connect_timeout)
        self._log(f"[local] Fake TLS handshake ok from {label}")
        return handshake, wrapped_reader, wrapped_writer

    async def _connect_upstream(
        self,
        proxy: ProxyRecord,
        dc_idx: int,
        proto_tag: bytes,
    ) -> tuple[Any, Any, Any, Any, float]:
        started_at = time.perf_counter()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(proxy.host, proxy.port),
            timeout=self.connect_timeout,
        )

        normalized_secret = _normalize_proxy_secret(proxy.secret)
        if proxy.secret.startswith("ee"):
            fake_tls = MTProxyFakeTLSClientCodec(proxy.secret[2:])
            writer.write(fake_tls.build_new_client_hello_packet())
            await writer.drain()
            wrapped_reader = FakeTLSStreamReader(reader)
            wrapped_writer = FakeTLSStreamWriter(writer)
            server_hello = await asyncio.wait_for(
                wrapped_reader.read_server_hello(),
                timeout=self.connect_timeout,
            )
            if not fake_tls.verify_server_hello(server_hello):
                raise RuntimeError("fake_tls_handshake_failed")
            reader = wrapped_reader
            writer = wrapped_writer
            normalized_secret = fake_tls.secret

        header, upstream_enc, upstream_dec = _build_upstream_header(
            normalized_secret,
            dc_idx,
            proto_tag,
        )
        writer.write(header)
        await writer.drain()
        latency_ms = (time.perf_counter() - started_at) * 1000.0
        return reader, writer, upstream_enc, upstream_dec, latency_ms

    def _log(self, message: str) -> None:
        if self.log_sink is not None:
            self.log_sink(message)

    def _emit(self, event_name: str, **payload: Any) -> None:
        if self.event_sink is not None:
            self.event_sink(event_name, payload)


def _try_handshake(handshake: bytes, secret: bytes) -> tuple[int, bool, bytes, bytes] | None:
    client_prekey_iv = handshake[SKIP_LEN : SKIP_LEN + PREKEY_LEN + IV_LEN]
    client_prekey = client_prekey_iv[:PREKEY_LEN]
    client_iv = client_prekey_iv[PREKEY_LEN:]

    key = hashlib.sha256(client_prekey + secret).digest()
    decryptor = Cipher(algorithms.AES(key), modes.CTR(client_iv)).encryptor()
    decrypted = decryptor.update(handshake)

    proto_tag = decrypted[PROTO_TAG_POS : PROTO_TAG_POS + 4]
    if proto_tag not in {PROTO_TAG_ABRIDGED, PROTO_TAG_INTERMEDIATE, PROTO_TAG_SECURE}:
        return None

    dc_idx = int.from_bytes(
        decrypted[DC_IDX_POS : DC_IDX_POS + 2],
        "little",
        signed=True,
    )
    dc_id = abs(dc_idx)
    is_media = dc_idx < 0
    return dc_id, is_media, proto_tag, client_prekey_iv


def _build_local_ciphers(client_prekey_iv: bytes, secret: bytes) -> tuple[Any, Any]:
    dec_prekey = client_prekey_iv[:PREKEY_LEN]
    dec_iv = client_prekey_iv[PREKEY_LEN:]
    dec_key = hashlib.sha256(dec_prekey + secret).digest()

    enc_prekey_iv = client_prekey_iv[::-1]
    enc_prekey = enc_prekey_iv[:PREKEY_LEN]
    enc_iv = enc_prekey_iv[PREKEY_LEN:]
    enc_key = hashlib.sha256(enc_prekey + secret).digest()

    decryptor = Cipher(algorithms.AES(dec_key), modes.CTR(dec_iv)).encryptor()
    encryptor = Cipher(algorithms.AES(enc_key), modes.CTR(enc_iv)).encryptor()
    decryptor.update(ZERO_64)
    return decryptor, encryptor


def _build_upstream_header(secret: bytes, dc_idx: int, proto_tag: bytes) -> tuple[bytes, Any, Any]:
    while True:
        random_bytes = bytearray(random.randbytes(HANDSHAKE_LEN))
        if random_bytes[0] in RESERVED_FIRST_BYTES:
            continue
        if bytes(random_bytes[:4]) in RESERVED_STARTS:
            continue
        if random_bytes[4:8] == RESERVED_CONTINUE:
            continue
        break

    reversed_bytes = random_bytes[55:7:-1]
    encrypt_key = hashlib.sha256(bytes(random_bytes[8:40]) + secret).digest()
    encrypt_iv = bytes(random_bytes[40:56])
    decrypt_key = hashlib.sha256(bytes(reversed_bytes[:32]) + secret).digest()
    decrypt_iv = bytes(reversed_bytes[32:48])

    encryptor = Cipher(algorithms.AES(encrypt_key), modes.CTR(encrypt_iv)).encryptor()
    decryptor = Cipher(algorithms.AES(decrypt_key), modes.CTR(decrypt_iv)).encryptor()

    random_bytes[56:60] = proto_tag
    random_bytes[60:62] = dc_idx.to_bytes(2, "little", signed=True)
    random_bytes[56:64] = encryptor.update(bytes(random_bytes))[56:64]
    return bytes(random_bytes), encryptor, decryptor


def _normalize_proxy_secret(secret: str) -> bytes:
    normalized = secret.strip().lower()
    if normalized.startswith("ee"):
        normalized = normalized[2:]
    elif normalized.startswith("dd"):
        normalized = normalized[2:]
    raw = bytes.fromhex(normalized)
    return raw[:16]


def _normalize_fake_tls_domain(domain: str) -> str:
    value = str(domain or "").strip().lower().rstrip(".")
    if not value:
        return ""
    value.encode("ascii")
    return value


def _verify_fake_tls_client_hello(data: bytes, secret: bytes) -> tuple[bytes, bytes, int] | None:
    n = len(data)
    if n < 43:
        return None
    if data[0] != TLS_RECORD_HANDSHAKE or data[5] != 0x01:
        return None

    client_random = bytes(data[CLIENT_RANDOM_OFFSET : CLIENT_RANDOM_OFFSET + CLIENT_RANDOM_LEN])
    zeroed = bytearray(data)
    zeroed[CLIENT_RANDOM_OFFSET : CLIENT_RANDOM_OFFSET + CLIENT_RANDOM_LEN] = b"\x00" * CLIENT_RANDOM_LEN
    expected = hmac.new(secret, bytes(zeroed), hashlib.sha256).digest()
    if not hmac.compare_digest(expected[:28], client_random[:28]):
        return None

    ts_xor = bytes(client_random[28 + index] ^ expected[28 + index] for index in range(4))
    timestamp = struct.unpack("<I", ts_xor)[0]
    if abs(int(time.time()) - timestamp) > TIMESTAMP_TOLERANCE:
        return None

    session_id = b"\x00" * SESSION_ID_LEN
    if n >= SESSION_ID_OFFSET + SESSION_ID_LEN and data[43] == 0x20:
        session_id = bytes(data[SESSION_ID_OFFSET : SESSION_ID_OFFSET + SESSION_ID_LEN])
    return client_random, session_id, timestamp


def _build_fake_tls_server_hello(secret: bytes, client_random: bytes, session_id: bytes) -> bytes:
    server_hello = bytearray(_SERVER_HELLO_TEMPLATE)
    server_hello[_SH_SESSID_OFF : _SH_SESSID_OFF + 32] = session_id
    server_hello[_SH_PUBKEY_OFF : _SH_PUBKEY_OFF + 32] = os.urandom(32)

    encrypted_size = random.randint(1900, 2100)
    encrypted_data = os.urandom(encrypted_size)
    app_record = b"\x17\x03\x03" + struct.pack(">H", encrypted_size) + encrypted_data
    response = bytes(server_hello) + _CCS_FRAME + app_record
    server_random = hmac.new(secret, client_random + response, hashlib.sha256).digest()

    final = bytearray(response)
    final[_SH_RANDOM_OFF : _SH_RANDOM_OFF + 32] = server_random
    return bytes(final)
