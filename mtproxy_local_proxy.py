from __future__ import annotations

import asyncio
import contextlib
import hashlib
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

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

HEAVY_MEDIA_UPLOAD_TRIGGER_BYTES = 96 * 1024
HEAVY_MEDIA_UPLOAD_MIN_RATE_BPS = 80 * 1024
HEAVY_MEDIA_SLOW_RATE_BPS = 96 * 1024
HEAVY_MEDIA_BAD_RATE_BPS = 48 * 1024
HEAVY_MEDIA_WINDOW_SECONDS = 1.6
HEAVY_MEDIA_WINDOW_TRIGGER_BYTES = 128 * 1024
HEAVY_MEDIA_MIN_BURST_DURATION_SECONDS = 0.35
MEDIA_ACTIVITY_RECENT_SECONDS = 180.0
MEDIA_TURBO_CANDIDATE_LIMIT = 12
LIVE_ACTIVITY_UPDATE_INTERVAL_SECONDS = 0.45
LIVE_ACTIVITY_UPDATE_MIN_DELTA_BYTES = 24 * 1024
LIVE_ACTIVITY_MIN_SAMPLE_SECONDS = 0.12
RATE_EWMA_ALPHA = 0.45
MEDIA_PIN_TTL_SECONDS = 120.0


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
    recent_upload_bps: float = 0.0
    recent_download_bps: float = 0.0
    recent_media_upload_bps: float = 0.0
    recent_media_download_bps: float = 0.0
    live_media_upload_bps: float = 0.0
    live_media_download_bps: float = 0.0
    media_successes: int = 0
    media_failures: int = 0
    deep_media_score: float | None = None
    deep_media_note: str = ""
    deep_media_upload_kbps: float = 0.0
    deep_media_download_kbps: float = 0.0
    deep_media_aux_kbps: float = 0.0
    active_media_connections: int = 0
    active_heavy_uploads: int = 0
    last_media_selected_at: float = 0.0
    last_media_activity_at: float = 0.0
    last_heavy_upload_at: float = 0.0
    last_live_activity_at: float = 0.0
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
        self._media_pin_key: tuple[str, int, str] | None = None
        self._media_pin_until: float = 0.0

    def _active_media_pin_key(self) -> tuple[str, int, str] | None:
        now = time.time()
        if self._media_pin_key is None or self._media_pin_until <= now:
            self._media_pin_key = None
            self._media_pin_until = 0.0
            return None
        return self._media_pin_key

    def pin_media_proxy(self, proxy_key: tuple[str, int, str], *, ttl_seconds: float = MEDIA_PIN_TTL_SECONDS) -> None:
        with self._lock:
            if proxy_key not in self._states:
                return
            self._media_pin_key = proxy_key
            self._media_pin_until = time.time() + max(15.0, float(ttl_seconds))

    def clear_media_pin(self, proxy_key: tuple[str, int, str] | None = None) -> None:
        with self._lock:
            if proxy_key is None or self._media_pin_key == proxy_key:
                self._media_pin_key = None
                self._media_pin_until = 0.0

    def _has_strong_media_candidate(self, candidates: list[UpstreamProxyState]) -> bool:
        if not candidates:
            return False
        best = max(candidates, key=self._media_turbo_score)
        return (
            best.counters.deep_media_download_kbps >= 160.0
            or best.counters.deep_media_upload_kbps >= 320.0
            or (best.counters.recent_media_download_bps / 1024.0) >= 192.0
            or (best.counters.recent_media_upload_bps / 1024.0) >= 384.0
            or (best.counters.deep_media_score is not None and best.counters.deep_media_score >= 0.45)
        )

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
            if self._media_pin_key is not None and self._media_pin_key not in self._states:
                self._media_pin_key = None
                self._media_pin_until = 0.0

    def update_deep_media_score(
        self,
        proxy_key: tuple[str, int, str],
        score: float | None,
        note: str,
        *,
        upload_kbps: float | None = None,
        download_kbps: float | None = None,
        aux_kbps: float | None = None,
    ) -> None:
        with self._lock:
            state = self._states.get(proxy_key)
            if state is None:
                return
            state.counters.deep_media_score = score
            state.counters.deep_media_note = note
            if upload_kbps is not None and upload_kbps > 0:
                state.counters.deep_media_upload_kbps = float(upload_kbps)
            if download_kbps is not None and download_kbps > 0:
                state.counters.deep_media_download_kbps = float(download_kbps)
            if aux_kbps is not None and aux_kbps > 0:
                state.counters.deep_media_aux_kbps = float(aux_kbps)

    @staticmethod
    def _apply_ewma(current: float, sample: float, *, alpha: float = RATE_EWMA_ALPHA) -> float:
        sample = max(0.0, float(sample or 0.0))
        if current <= 0.0:
            return sample
        return (current * (1.0 - alpha)) + (sample * alpha)

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
        upload_kbps: float | None = None,
        download_kbps: float | None = None,
        aux_kbps: float | None = None,
        failure_score: float = 0.6,
        cooldown_seconds: float = 300.0,
    ) -> str | None:
        with self._lock:
            state = self._states.get(proxy_key)
            if state is None:
                return None
            state.counters.deep_media_note = note
            if upload_kbps is not None and upload_kbps > 0:
                state.counters.deep_media_upload_kbps = max(state.counters.deep_media_upload_kbps, float(upload_kbps))
            if download_kbps is not None and download_kbps > 0:
                state.counters.deep_media_download_kbps = max(state.counters.deep_media_download_kbps, float(download_kbps))
            if aux_kbps is not None and aux_kbps > 0:
                state.counters.deep_media_aux_kbps = max(state.counters.deep_media_aux_kbps, float(aux_kbps))
            dpi_suspected = "dpi_16_20kb_suspected" in str(note or "")
            timeout_like = "timeout" in str(note or "")
            if score is not None:
                state.counters.deep_media_score = score
            elif timeout_like and state.counters.deep_media_score is not None:
                return None
            if score is None or score < failure_score:
                state.counters.consecutive_media_failures += 1
                return self._enter_cooldown(
                    state,
                    reason=f"media:{note or 'failed'}",
                    cooldown_seconds=max(cooldown_seconds, 480.0 if dpi_suspected else cooldown_seconds),
                )
            state.counters.consecutive_media_failures = 0
            return None

    def select_candidates(self, *, is_media: bool, limit: int = 5) -> list[UpstreamProxyState]:
        with self._lock:
            candidates = self._available_states()
            if not candidates:
                candidates = list(self._states.values())
            pressure = self.media_pressure()
            prefer_media = (
                is_media
                or pressure["active_heavy"] > 0
                or pressure["recent_media"] > 0
                or self._has_strong_media_candidate(candidates)
            )
            if prefer_media:
                ordered = sorted(candidates, key=self._media_turbo_score, reverse=True)
            else:
                ordered = sorted(candidates, key=lambda item: self._score(item, is_media), reverse=True)
            pinned_key = self._active_media_pin_key() if prefer_media else None
            if pinned_key is not None:
                pinned_state = next((item for item in ordered if item.key == pinned_key), None)
                if pinned_state is not None:
                    ordered = [pinned_state] + [item for item in ordered if item.key != pinned_key]
            return ordered[:limit]

    def select_turbo_media_candidates(self, *, limit: int = 5) -> list[UpstreamProxyState]:
        with self._lock:
            candidates = self._available_states()
            if not candidates:
                candidates = list(self._states.values())
            ordered = sorted(candidates, key=self._media_turbo_score, reverse=True)
            pinned_key = self._active_media_pin_key()
            if pinned_key is not None:
                pinned_state = next((item for item in ordered if item.key == pinned_key), None)
                if pinned_state is not None:
                    ordered = [pinned_state] + [item for item in ordered if item.key != pinned_key]
            return ordered[:limit]

    def best_media_leader(self) -> UpstreamProxyState | None:
        with self._lock:
            candidates = self._available_states()
            if not candidates:
                candidates = list(self._states.values())
            if not candidates:
                return None
            pinned_key = self._active_media_pin_key()
            if pinned_key is not None:
                pinned_state = next((item for item in candidates if item.key == pinned_key), None)
                if pinned_state is not None:
                    return pinned_state
            return max(candidates, key=self._media_turbo_score)

    def select_monitor_targets(self, *, limit: int = 2, prefer_media: bool = False) -> list[UpstreamProxyState]:
        with self._lock:
            candidates = self._available_states()
            if not candidates:
                candidates = list(self._states.values())
            if prefer_media:
                ordered = sorted(
                    candidates,
                    key=lambda item: (
                        item.counters.active_heavy_uploads > 0,
                        item.counters.active_media_connections > 0,
                        item.counters.last_heavy_upload_at,
                        item.counters.last_media_activity_at,
                        self._media_turbo_score(item),
                        self._score(item, False),
                    ),
                    reverse=True,
                )
            else:
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

    def mark_selected(self, proxy_key: tuple[str, int, str], latency_ms: float | None, *, is_media: bool = False) -> None:
        with self._lock:
            state = self._states.get(proxy_key)
            if state is None:
                return
            now = time.time()
            state.counters.selected_count += 1
            state.counters.active_connections += 1
            state.counters.last_selected_at = now
            if latency_ms is not None:
                state.counters.live_latency_ms = latency_ms
                state.counters.last_success_at = now
                state.counters.recent_successes = min(50, state.counters.recent_successes + 1)
                state.counters.recent_failures = max(0, state.counters.recent_failures - 1)
            if is_media:
                state.counters.active_media_connections += 1
                state.counters.last_media_selected_at = now
                state.counters.last_media_activity_at = now
                self._media_pin_key = proxy_key
                self._media_pin_until = max(self._media_pin_until, now + MEDIA_PIN_TTL_SECONDS)

    def mark_heavy_upload_started(self, proxy_key: tuple[str, int, str]) -> bool:
        with self._lock:
            state = self._states.get(proxy_key)
            if state is None:
                return False
            state.counters.active_heavy_uploads += 1
            state.counters.last_heavy_upload_at = time.time()
            state.counters.last_media_activity_at = state.counters.last_heavy_upload_at
            self._media_pin_key = proxy_key
            self._media_pin_until = max(self._media_pin_until, state.counters.last_heavy_upload_at + MEDIA_PIN_TTL_SECONDS)
            return True

    def update_session_activity(
        self,
        proxy_key: tuple[str, int, str],
        *,
        upload_bps: float,
        download_bps: float,
        heavy_upload: bool,
        is_media: bool,
    ) -> None:
        with self._lock:
            state = self._states.get(proxy_key)
            if state is None:
                return
            now = time.time()
            upload_bps = max(0.0, float(upload_bps or 0.0))
            download_bps = max(0.0, float(download_bps or 0.0))
            state.counters.last_live_activity_at = now
            state.counters.last_media_activity_at = now
            if heavy_upload or is_media:
                state.counters.live_media_upload_bps = self._apply_ewma(state.counters.live_media_upload_bps, upload_bps)
                state.counters.live_media_download_bps = self._apply_ewma(state.counters.live_media_download_bps, download_bps)
                if heavy_upload and upload_bps < HEAVY_MEDIA_BAD_RATE_BPS:
                    state.counters.consecutive_media_failures = min(8, state.counters.consecutive_media_failures + 1)
                    state.counters.last_error = f"media_live_slow:{int(round(upload_bps / 1024.0))}KBps"

    def mark_session_result(
        self,
        proxy_key: tuple[str, int, str],
        *,
        ok: bool,
        is_media: bool,
        bytes_up: int,
        bytes_down: int,
        error: str = "",
        duration_seconds: float | None = None,
        heavy_upload: bool = False,
        measured_upload_bps: float | None = None,
        measured_download_bps: float | None = None,
    ) -> str | None:
        with self._lock:
            state = self._states.get(proxy_key)
            if state is None:
                return None
            now = time.time()
            state.counters.active_connections = max(0, state.counters.active_connections - 1)
            media_like = is_media or heavy_upload
            if media_like:
                state.counters.active_media_connections = max(0, state.counters.active_media_connections - 1)
                state.counters.last_media_activity_at = now
                state.counters.live_media_upload_bps = 0.0
                state.counters.live_media_download_bps = 0.0
                state.counters.last_live_activity_at = now
            if heavy_upload:
                state.counters.active_heavy_uploads = max(0, state.counters.active_heavy_uploads - 1)
            state.counters.total_bytes_up += bytes_up
            state.counters.total_bytes_down += bytes_down
            upload_bps = max(0.0, float(measured_upload_bps or 0.0))
            download_bps = max(0.0, float(measured_download_bps or 0.0))
            if duration_seconds and duration_seconds > 0.0:
                avg_upload_bps = max(0.0, float(bytes_up) / float(duration_seconds))
                avg_download_bps = max(0.0, float(bytes_down) / float(duration_seconds))
                upload_bps = max(upload_bps, avg_upload_bps)
                download_bps = max(download_bps, avg_download_bps)
                state.counters.recent_upload_bps = self._apply_ewma(state.counters.recent_upload_bps, upload_bps)
                state.counters.recent_download_bps = self._apply_ewma(state.counters.recent_download_bps, download_bps)
                if media_like:
                    state.counters.recent_media_upload_bps = self._apply_ewma(
                        state.counters.recent_media_upload_bps,
                        upload_bps,
                    )
                    state.counters.recent_media_download_bps = self._apply_ewma(
                        state.counters.recent_media_download_bps,
                        download_bps,
                    )
            if ok:
                state.counters.successful_sessions += 1
                state.counters.recent_successes = min(50, state.counters.recent_successes + 1)
                state.counters.recent_failures = max(0, state.counters.recent_failures - 1)
                state.counters.last_success_at = now
                if media_like:
                    state.counters.media_successes += 1
                    self._media_pin_key = proxy_key
                    self._media_pin_until = max(self._media_pin_until, now + MEDIA_PIN_TTL_SECONDS)
                    if not heavy_upload or upload_bps >= HEAVY_MEDIA_SLOW_RATE_BPS:
                        state.counters.consecutive_media_failures = 0
            else:
                state.counters.failed_sessions += 1
                state.counters.recent_failures = min(50, state.counters.recent_failures + 1)
                state.counters.last_failure_at = now
                state.counters.last_error = error
                if media_like:
                    state.counters.media_failures += 1
                    state.counters.consecutive_media_failures += 1
                    if self._media_pin_key == proxy_key:
                        self._media_pin_key = None
                        self._media_pin_until = 0.0
            if media_like:
                dpi_suspected = "dpi_16_20kb_suspected" in str(state.counters.deep_media_note or "")
                slow_heavy_upload = (
                    bool(heavy_upload)
                    and bytes_up >= HEAVY_MEDIA_UPLOAD_TRIGGER_BYTES
                    and upload_bps > 0.0
                    and upload_bps < HEAVY_MEDIA_SLOW_RATE_BPS
                )
                if slow_heavy_upload and ok:
                    state.counters.consecutive_media_failures += 1
                    state.counters.last_error = f"media_slow:{int(round(upload_bps / 1024.0))}KBps"
                if dpi_suspected:
                    return self._enter_cooldown(
                        state,
                        reason=f"media:{state.counters.deep_media_note or 'dpi_16_20kb_suspected'}",
                        cooldown_seconds=480.0,
                    )
                if not ok and heavy_upload:
                    return self._enter_cooldown(
                        state,
                        reason=error or "media_session_failed",
                        cooldown_seconds=300.0,
                    )
                if slow_heavy_upload and state.counters.consecutive_media_failures >= 2:
                    return self._enter_cooldown(
                        state,
                        reason=f"media_slow:{int(round(upload_bps / 1024.0))}KBps",
                        cooldown_seconds=240.0,
                    )
            return None

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
                        "active_media_connections": state.counters.active_media_connections,
                        "active_heavy_uploads": state.counters.active_heavy_uploads,
                        "media_score": state.media_score,
                        "deep_media_score": state.counters.deep_media_score,
                        "deep_media_note": state.counters.deep_media_note,
                        "deep_media_upload_kbps": round(state.counters.deep_media_upload_kbps, 1),
                        "deep_media_download_kbps": round(state.counters.deep_media_download_kbps, 1),
                        "deep_media_aux_kbps": round(state.counters.deep_media_aux_kbps, 1),
                        "last_live_activity_at": state.counters.last_live_activity_at,
                        "last_media_activity_at": state.counters.last_media_activity_at,
                        "live_media_upload_kbps": round(state.counters.live_media_upload_bps / 1024.0, 1),
                        "live_media_download_kbps": round(state.counters.live_media_download_bps / 1024.0, 1),
                        "recent_media_upload_kbps": round(state.counters.recent_media_upload_bps / 1024.0, 1),
                        "recent_media_download_kbps": round(state.counters.recent_media_download_bps / 1024.0, 1),
                        "last_error": state.counters.cooldown_reason or state.counters.last_error,
                        "reason": runtime_state,
                        "score": round(self._score(state, False), 2),
                    }
                )
            return rows

    def count(self) -> int:
        with self._lock:
            return len(self._states)

    def snapshot_by_key(self, key: tuple) -> dict | None:
        """Return pool-row dict for a single proxy key, or None if not found."""
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return None
            return {
                "url": state.outcome.proxy.url,
                "host": state.outcome.proxy.host,
                "port": state.outcome.proxy.port,
                "media_score": state.media_score,
                "deep_media_score": state.counters.deep_media_score,
                "deep_media_upload_kbps": round(state.counters.deep_media_upload_kbps, 1),
                "deep_media_download_kbps": round(state.counters.deep_media_download_kbps, 1),
                "deep_media_aux_kbps": round(state.counters.deep_media_aux_kbps, 1),
                "last_live_activity_at": state.counters.last_live_activity_at,
                "live_media_upload_kbps": round(state.counters.live_media_upload_bps / 1024.0, 1),
                "live_media_download_kbps": round(state.counters.live_media_download_bps / 1024.0, 1),
                "recent_media_upload_kbps": round(state.counters.recent_media_upload_bps / 1024.0, 1),
                "recent_media_download_kbps": round(state.counters.recent_media_download_bps / 1024.0, 1),
            }

    def best(self) -> UpstreamProxyState | None:
        items = self.select_candidates(is_media=True, limit=1)
        return items[0] if items else None

    def _score(self, state: UpstreamProxyState, is_media: bool) -> float:
        base_latency = state.avg_latency_ms
        success_rate = max(state.outcome.success_rate, state.runtime_success_rate)
        score = success_rate * (760.0 if is_media else 650.0)
        score -= base_latency * (2.4 if is_media else 3.6)
        if base_latency > 80.0:
            score -= (base_latency - 80.0) * (0.7 if is_media else 1.2)
        if base_latency > 140.0:
            score -= (base_latency - 140.0) * (1.4 if is_media else 2.4)
        if base_latency > 220.0:
            score -= (base_latency - 220.0) * (2.0 if is_media else 3.2)
        score -= state.counters.recent_failures * 120.0
        score += min(20, state.counters.recent_successes) * 12.0
        score -= state.counters.active_connections * 18.0

        media_score = state.media_score
        if media_score >= 0:
            score += media_score * (2800.0 if is_media else 1800.0)
        elif is_media:
            score -= 160.0

        if state.counters.deep_media_score is not None:
            score += state.counters.deep_media_score * (4600.0 if is_media else 2600.0)
        if state.counters.deep_media_upload_kbps > 0.0:
            score += min(1_800.0, state.counters.deep_media_upload_kbps * (1.6 if is_media else 0.6))
        if state.counters.deep_media_download_kbps > 0.0:
            score += min(2_800.0, state.counters.deep_media_download_kbps * (3.0 if is_media else 1.0))
        if state.counters.deep_media_aux_kbps > 0.0 and is_media:
            score += min(240.0, state.counters.deep_media_aux_kbps * 0.25)
        media_upload_kbps = state.counters.recent_media_upload_bps / 1024.0
        media_download_kbps = state.counters.recent_media_download_bps / 1024.0
        if media_upload_kbps > 0.0:
            score += min(1_100.0, media_upload_kbps * (1.0 if is_media else 0.3))
        if media_download_kbps > 0.0:
            score += min(1_700.0, media_download_kbps * (1.8 if is_media else 0.35))
        if is_media and state.counters.consecutive_media_failures > 0:
            score -= state.counters.consecutive_media_failures * 260.0
        deep_note = str(state.counters.deep_media_note or "")
        if "dpi_16_20kb_suspected" in deep_note:
            score -= 2_800.0 if is_media else 1_200.0
        if "video_download_failed" in deep_note:
            score -= 1_400.0 if is_media else 400.0
        if "no_video_samples_found" in deep_note and is_media:
            score -= 240.0

        cooldown_remaining = state.counters.cooldown_until - time.time()
        if cooldown_remaining > 0:
            score -= 10_000.0 + min(1_000.0, cooldown_remaining)
        return score

    def _media_turbo_score(self, state: UpstreamProxyState) -> float:
        score = self._score(state, True)
        score += min(1_400.0, (state.counters.recent_media_upload_bps / 1024.0) * 1.4)
        score += min(2_000.0, (state.counters.recent_media_download_bps / 1024.0) * 2.0)
        score += min(2_100.0, state.counters.deep_media_upload_kbps * 2.0)
        score += min(3_200.0, state.counters.deep_media_download_kbps * 3.4)
        live_upload_kbps = state.counters.live_media_upload_bps / 1024.0
        live_download_kbps = state.counters.live_media_download_bps / 1024.0
        if live_upload_kbps > 0.0:
            score += min(1_200.0, live_upload_kbps * 2.0)
            if state.counters.active_heavy_uploads > 0 and state.counters.live_media_upload_bps < HEAVY_MEDIA_BAD_RATE_BPS:
                score -= 2_200.0
        if live_download_kbps > 0.0:
            score += min(1_800.0, live_download_kbps * 2.2)
        if state.counters.active_heavy_uploads > 0:
            score += 260.0
        if state.counters.active_media_connections > 0:
            score += 120.0
        age = time.time() - state.counters.last_heavy_upload_at if state.counters.last_heavy_upload_at > 0.0 else None
        if age is not None and age < MEDIA_ACTIVITY_RECENT_SECONDS:
            score += max(0.0, 260.0 - age)
        if "dpi_16_20kb_suspected" in str(state.counters.deep_media_note or ""):
            score -= 2_400.0
        return score

    def media_pressure(self) -> dict[str, int]:
        with self._lock:
            now = time.time()
            active_media = sum(state.counters.active_media_connections for state in self._states.values())
            active_heavy = sum(state.counters.active_heavy_uploads for state in self._states.values())
            recent_media = sum(
                1
                for state in self._states.values()
                if state.counters.last_media_activity_at > 0.0
                and (now - state.counters.last_media_activity_at) <= MEDIA_ACTIVITY_RECENT_SECONDS
            )
            return {
                "active_media": int(active_media),
                "active_heavy": int(active_heavy),
                "recent_media": int(recent_media),
            }

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
        connect_timeout: float = 8.0,
        log_sink: Any | None = None,
        event_sink: Any | None = None,
    ) -> None:
        self.pool = pool
        self.host = host
        self.port = port
        self.secret = secret.lower()
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
        last_activity_upload_bps = 0.0
        last_activity_download_bps = 0.0
        chosen_state: UpstreamProxyState | None = None
        is_media = False
        heavy_upload_detected = False
        session_error = ""
        try:
            handshake = await asyncio.wait_for(reader.readexactly(HANDSHAKE_LEN), timeout=self.connect_timeout)
            parsed = _try_handshake(handshake, self._local_secret_bytes)
            if parsed is None:
                self._log(f"[local] bad handshake from {label}")
                return

            dc_id, is_media, proto_tag, client_prekey_iv = parsed
            dc_idx = -dc_id if is_media else dc_id
            local_dec, local_enc = _build_local_ciphers(client_prekey_iv, self._local_secret_bytes)
            use_media_shortlist = is_media or self.pool.media_pressure()["recent_media"] > 0 or self.pool.media_pressure()["active_heavy"] > 0
            candidates = (
                self.pool.select_turbo_media_candidates(limit=MEDIA_TURBO_CANDIDATE_LIMIT)
                if use_media_shortlist
                else self.pool.select_candidates(is_media=is_media, limit=5)
            )
            media_leader = self.pool.best_media_leader() if not use_media_shortlist else None
            if media_leader is not None and media_leader in candidates:
                candidates = [media_leader] + [item for item in candidates if item.key != media_leader.key]
            elif media_leader is not None and not use_media_shortlist:
                candidates = [media_leader] + [item for item in candidates if item.key != media_leader.key]
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
                self.pool.mark_selected(state.key, latency_ms, is_media=is_media)
                self._emit(
                    "local_upstream_selected",
                    host=state.proxy.host,
                    port=state.proxy.port,
                    latency_ms=latency_ms,
                    is_media=is_media,
                    dc_id=dc_id,
                    proxy_key=state.key,
                )
                self._log(
                    f"[local] upstream selected {state.proxy.host}:{state.proxy.port} "
                    f"latency={int(round(latency_ms)) if latency_ms is not None else 'n/a'}ms "
                    f"is_media={is_media} dc_id={dc_id}"
                )
                break

            if chosen_state is None or upstream_reader is None or upstream_writer is None:
                if connect_errors:
                    self._log(f"[local] upstream connect failed: {' | '.join(connect_errors[:3])}")
                return

            async def client_to_upstream() -> None:
                nonlocal bytes_up, heavy_upload_detected, last_activity_upload_bps, last_activity_download_bps
                last_live_activity_at = 0.0
                last_live_activity_bytes = 0
                last_live_activity_down = 0
                burst_started_at = 0.0
                burst_bytes = 0
                while True:
                    chunk = await reader.read(65536)
                    if not chunk:
                        break
                    plain = local_dec.update(chunk)
                    encrypted = upstream_enc.update(plain)
                    upstream_writer.write(encrypted)
                    await upstream_writer.drain()
                    bytes_up += len(chunk)
                    elapsed = time.perf_counter() - started_at
                    if burst_started_at <= 0.0:
                        burst_started_at = elapsed
                        burst_bytes = len(chunk)
                    else:
                        if (elapsed - burst_started_at) > HEAVY_MEDIA_WINDOW_SECONDS:
                            burst_started_at = elapsed
                            burst_bytes = len(chunk)
                        else:
                            burst_bytes += len(chunk)
                    if chosen_state is not None and not heavy_upload_detected:
                        burst_duration = max(elapsed - burst_started_at, 0.001)
                        burst_upload_bps = burst_bytes / burst_duration
                        if (
                            burst_duration >= HEAVY_MEDIA_MIN_BURST_DURATION_SECONDS
                            and burst_duration <= HEAVY_MEDIA_WINDOW_SECONDS
                            and burst_bytes >= HEAVY_MEDIA_WINDOW_TRIGGER_BYTES
                            and burst_upload_bps >= HEAVY_MEDIA_UPLOAD_MIN_RATE_BPS
                            and bytes_up >= HEAVY_MEDIA_UPLOAD_TRIGGER_BYTES
                        ):
                                heavy_upload_detected = self.pool.mark_heavy_upload_started(chosen_state.key)
                                media_hint = bool(is_media or heavy_upload_detected)
                                self._emit(
                                    "local_media_activity",
                                    host=chosen_state.proxy.host,
                                    port=chosen_state.proxy.port,
                                    proxy_key=chosen_state.key,
                                    is_media=is_media,
                                    media_hint=media_hint,
                                    heavy_upload=heavy_upload_detected,
                                    bytes_up=bytes_up,
                                    duration_seconds=round(burst_duration, 2),
                                    upload_kbps=round(burst_upload_bps / 1024.0, 1),
                                )
                                self._log(
                                    f"[local] heavy upload {chosen_state.proxy.host}:{chosen_state.proxy.port} "
                                    f"upload={round(burst_upload_bps / 1024.0, 1)}KB/s bytes_up={bytes_up} "
                                    f"is_media={is_media}"
                                )
                    if chosen_state is not None and (heavy_upload_detected or is_media):
                        if last_live_activity_at <= 0.0:
                            last_live_activity_at = elapsed
                            last_live_activity_bytes = bytes_up
                            last_live_activity_down = bytes_down
                        elif (
                            (elapsed - last_live_activity_at) >= LIVE_ACTIVITY_UPDATE_INTERVAL_SECONDS
                            or (bytes_up - last_live_activity_bytes) >= LIVE_ACTIVITY_UPDATE_MIN_DELTA_BYTES
                        ):
                            delta_time = elapsed - last_live_activity_at
                            if delta_time < LIVE_ACTIVITY_MIN_SAMPLE_SECONDS:
                                continue
                            delta_up = max(0, bytes_up - last_live_activity_bytes)
                            delta_down = max(0, bytes_down - last_live_activity_down)
                            last_activity_upload_bps = float(delta_up) / delta_time
                            last_activity_download_bps = float(delta_down) / delta_time
                            self.pool.update_session_activity(
                                chosen_state.key,
                                upload_bps=last_activity_upload_bps,
                                download_bps=last_activity_download_bps,
                                heavy_upload=heavy_upload_detected,
                                is_media=is_media,
                            )
                            self._emit(
                                "local_media_activity",
                                host=chosen_state.proxy.host,
                                port=chosen_state.proxy.port,
                                proxy_key=chosen_state.key,
                                is_media=is_media,
                                media_hint=bool(is_media or heavy_upload_detected),
                                heavy_upload=heavy_upload_detected,
                                bytes_up=bytes_up,
                                bytes_down=bytes_down,
                                duration_seconds=round(delta_time, 2),
                                upload_kbps=round(last_activity_upload_bps / 1024.0, 1),
                                download_kbps=round(last_activity_download_bps / 1024.0, 1),
                            )
                            last_live_activity_at = elapsed
                            last_live_activity_bytes = bytes_up
                            last_live_activity_down = bytes_down

            async def upstream_to_client() -> None:
                nonlocal bytes_down
                while True:
                    chunk = await upstream_reader.read(65536)
                    if not chunk:
                        break
                    plain = upstream_dec.update(chunk)
                    encrypted = local_enc.update(plain)
                    writer.write(encrypted)
                    await writer.drain()
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
            session_error = str(exc)
            self._log(f"[local] {label} -> {session_error}")
        finally:
            duration = time.perf_counter() - started_at
            if chosen_state is not None:
                measured_upload_bps = max(last_activity_upload_bps, (bytes_up / max(duration, 0.001)))
                measured_download_bps = max(last_activity_download_bps, (bytes_down / max(duration, 0.001)))
                success = not session_error and (bytes_down > 0 or duration >= 2.0)
                cooldown_reason = self.pool.mark_session_result(
                    chosen_state.key,
                    ok=success,
                    is_media=is_media,
                    bytes_up=bytes_up,
                    bytes_down=bytes_down,
                    error="" if success else (session_error or "session_closed_early"),
                    duration_seconds=duration,
                    heavy_upload=heavy_upload_detected,
                    measured_upload_bps=measured_upload_bps,
                    measured_download_bps=measured_download_bps,
                )
                if cooldown_reason:
                    self._emit(
                        "proxy_cooldown",
                        host=chosen_state.proxy.host,
                        port=chosen_state.proxy.port,
                        reason=cooldown_reason,
                    )
                self._emit(
                    "local_session_closed",
                    host=chosen_state.proxy.host,
                    port=chosen_state.proxy.port,
                    proxy_key=chosen_state.key,
                    is_media=is_media,
                    heavy_upload=heavy_upload_detected,
                    bytes_up=bytes_up,
                    bytes_down=bytes_down,
                    upload_kbps=round(measured_upload_bps / 1024.0, 1),
                    download_kbps=round(measured_download_bps / 1024.0, 1),
                    duration_seconds=round(duration, 2),
                    success=success,
                    error=session_error,
                )
                self._log(
                    f"[local] session closed {chosen_state.proxy.host}:{chosen_state.proxy.port} "
                    f"success={success} heavy={heavy_upload_detected} is_media={is_media} "
                    f"up={round(bytes_up / 1024.0, 1)}KB down={round(bytes_down / 1024.0, 1)}KB "
                    f"dur={round(duration, 2)}s"
                )
            with contextlib.suppress(Exception):
                writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

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
    if normalized.startswith("dd"):
        normalized = normalized[2:]
    raw = bytes.fromhex(normalized)
    return raw[:16]
