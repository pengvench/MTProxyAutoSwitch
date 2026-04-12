from __future__ import annotations

import contextlib
import ctypes
import json
import os
import secrets
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from mtproxy_collector import (
    CollectorConfig,
    CollectorRunResult,
    DEFAULT_SOURCES,
    ProbeOutcome,
    ProbeSettings,
    ProxyRecord,
    build_report,
    outcome_sort_key,
    parse_proxy_link,
    probe_all,
    run_collection,
    run_async,
)
from mtproxy_local_proxy import DEFAULT_FAKE_TLS_DOMAIN, LocalMTProxyServer, ProxyPool
from mtproxy_telegram import (
    DEFAULT_SOURCE_MAX_AGE_DAYS,
    DEFAULT_TELEGRAM_SOURCE_URLS,
    TelegramAuthConfig,
    collect_telegram_sources_proxies,
    collect_thread_proxies,
    complete_login,
    deep_media_probe,
    get_auth_status,
    light_media_probe,
    logout,
    qr_login_flow,
    request_login_code,
    send_proxy_list_to_saved_messages,
)


def runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


@dataclass
class AppConfig:
    sources: list[str] = field(default_factory=lambda: list(DEFAULT_SOURCES))
    out_dir: str = "list"
    duration: float = 35.0
    interval: float = 3.0
    timeout: float = 8.0
    workers: int = 25
    fetch_timeout: float = 15.0
    max_latency_ms: float = 300.0
    min_success_rate: float = 0.7
    max_high_latency_ratio: float = 0.6
    high_latency_streak: int = 3
    max_proxies: int = 0
    local_host: str = "127.0.0.1"
    local_port: int = 1443
    local_secret: str = field(default_factory=lambda: secrets.token_hex(16))
    local_fake_tls_enabled: bool = False
    local_fake_tls_domain: str = ""
    auto_start_local: bool = True
    autostart_enabled: bool = False
    start_minimized_to_tray: bool = False
    close_behavior: str = "ask"
    telegram_sources_enabled: bool = False
    telegram_sources: list[str] = field(default_factory=lambda: list(DEFAULT_TELEGRAM_SOURCE_URLS))
    thread_source_enabled: bool = False
    thread_source_url: str = "https://t.me/strbypass/237103"
    telegram_source_max_age_days: int = DEFAULT_SOURCE_MAX_AGE_DAYS
    live_probe_interval_sec: int = 20
    live_probe_duration_sec: float = 4.0
    live_probe_top_n: int = 12
    deep_media_enabled: bool = False
    rf_whitelist_check_enabled: bool = False
    deep_media_top_n: int = 5
    appearance: str = "auto"
    auto_update_enabled: bool = True
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_phone: str = ""
    telegram_session_file: str = "data/telegram_user.sec"


LIST_DIR_NAME = "list"
LIST_FILE_NAME = "proxy_list.txt"
REJECTED_FILE_NAME = "rejected_list.txt"
ALL_FILE_NAME = "all_list.txt"
SOCKS5_FILE_NAME = "socks5_list.txt"
REPORT_FILE_NAME = "report.json"
LEGACY_OUT_DIR_NAME = "mtproxy_output"
LEGACY_WORKING_FILE_NAME = "mtproxy_working.txt"
LEGACY_REJECTED_FILE_NAME = "mtproxy_rejected.txt"
LEGACY_ALL_FILE_NAME = "mtproxy_all.txt"
LEGACY_SOCKS5_FILE_NAME = "socks5_all.txt"
LEGACY_REPORT_FILE_NAME = "mtproxy_report.json"
CONFIG_FILE_NAME = "config.json"
DATA_DIR_NAME = "data"
FILE_ATTRIBUTE_HIDDEN = 0x02
PERSISTENT_PROXY_CACHE_FILE_NAME = "proxy_list_persist.txt"
RECOMMENDED_WEB_SOURCE_ADDITIONS = [
    "https://t.me/s/ProxyFree_Ru",
]
RECOMMENDED_TELEGRAM_SOURCE_ADDITIONS = [
    "https://t.me/telemtrs/16160",
    "https://t.me/ProxyFree_Ru",
]


def is_public_release() -> bool:
    return os.environ.get("MTPROXY_PUBLIC_RELEASE", "").strip().lower() in {"1", "true", "yes", "on"}


def _read_env_file(root_dir: Path) -> dict[str, str]:
    env_path = root_dir / ".env"
    if not env_path.exists():
        return {}
    values: dict[str, str] = {}
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    except Exception:
        return {}
    return values


class AppRuntime:
    def __init__(
        self,
        *,
        log_sink: Any | None = None,
        event_sink: Any | None = None,
    ) -> None:
        self.root_dir = runtime_root()
        self.state_dir = self.root_dir / DATA_DIR_NAME
        self.state_dir.mkdir(parents=True, exist_ok=True)
        _hide_windows_path(self.state_dir)
        self.env_values = _read_env_file(self.root_dir)
        self.config_path = self.root_dir / CONFIG_FILE_NAME
        self.config = self._load_config()
        self.pool = ProxyPool()
        self.log_sink = log_sink
        self.event_sink = event_sink
        self.local_server = LocalMTProxyServer(
            self.pool,
            host=self.config.local_host,
            port=self.config.local_port,
            secret=self.config.local_secret,
            fake_tls_enabled=False,
            fake_tls_domain="",
            log_sink=self._log,
            event_sink=self._emit,
        )
        self.last_result: CollectorRunResult | None = None
        self.last_outcomes: list[ProbeOutcome] = []
        self.last_working: list[ProbeOutcome] = []
        self.last_rejected: list[ProbeOutcome] = []
        self.last_export: dict[str, str] = {}
        self.last_refresh_started_at: float = 0.0
        self.last_refresh_finished_at: float = 0.0
        self.seed_source: str = ""
        self.seed_loaded_at: float = 0.0
        self.thread_status: str = "not_checked"
        self.thread_proxy_count: int = 0
        self._latest_deep_media_scores: dict[tuple[str, int, str], tuple[float | None, str]] = {}
        self.telegram_lock = threading.RLock()
        self._load_initial_pool()
        self.live_probe_stop = threading.Event()
        self._last_focused_probe_at: float = 0.0
        self._last_broad_probe_at: float = 0.0
        self._last_media_pulse_at: float = 0.0
        self.live_probe_thread = threading.Thread(target=self._live_probe_loop, daemon=True, name="mtproxy-live-probe")
        self.live_probe_thread.start()
        self._auth_code_hash: str = ""
        if self.config.auto_start_local and self.pool.count() > 0:
            self.start_local_server()

    @property
    def auth_config(self) -> TelegramAuthConfig:
        env_api_id = str(self.env_values.get("MTPROXY_TELEGRAM_API_ID") or os.environ.get("MTPROXY_TELEGRAM_API_ID") or "").strip()
        env_api_hash = str(self.env_values.get("MTPROXY_TELEGRAM_API_HASH") or os.environ.get("MTPROXY_TELEGRAM_API_HASH") or "").strip()
        config_api_id = int(self.config.telegram_api_id or 0)
        config_api_hash = self.config.telegram_api_hash.strip()
        return TelegramAuthConfig(
            api_id=int(env_api_id or config_api_id or 0),
            api_hash=(env_api_hash or config_api_hash or ""),
            session_path=(self.root_dir / self.config.telegram_session_file).resolve(),
            phone=self.config.telegram_phone.strip(),
        )

    def shutdown(self) -> None:
        self.live_probe_stop.set()
        if self.live_probe_thread.is_alive():
            self.live_probe_thread.join(timeout=3.0)
        self.stop_local_server()

    def save_config(self) -> None:
        payload = asdict(self.config)
        self.config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def apply_config(self, config: AppConfig) -> None:
        config.local_fake_tls_enabled = False
        config.local_fake_tls_domain = ""
        if int(config.telegram_source_max_age_days or 0) <= 0:
            config.telegram_source_max_age_days = DEFAULT_SOURCE_MAX_AGE_DAYS
        self.config = config
        self.save_config()
        was_running = self.local_server.is_running()
        if was_running:
            self.stop_local_server()
        self.local_server = LocalMTProxyServer(
            self.pool,
            host=self.config.local_host,
            port=self.config.local_port,
            secret=self.config.local_secret,
            fake_tls_enabled=False,
            fake_tls_domain="",
            log_sink=self._log,
            event_sink=self._emit,
        )
        if was_running and self.config.auto_start_local and self.pool.count() > 0:
            self.start_local_server()

    def start_local_server(self) -> None:
        if self.pool.count() <= 0:
            self._log("[local] start skipped: no working proxies")
            return
        self.local_server.start()

    def stop_local_server(self) -> None:
        self.local_server.stop()

    @staticmethod
    def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("refresh_cancelled")

    def run_refresh(self, *, cancel_event: threading.Event | None = None) -> None:
        self.last_refresh_started_at = time.time()
        self.thread_status = "disabled"
        self.thread_proxy_count = 0
        self._latest_deep_media_scores = {}
        existing_list_urls = self._read_existing_proxy_list_urls()
        self._write_url_list(self._persistent_proxy_cache_path(), existing_list_urls)
        config = CollectorConfig(
            sources=list(self.config.sources),
            out_dir=(self.root_dir / self.config.out_dir).resolve(),
            duration=self.config.duration,
            interval=self.config.interval,
            timeout=self.config.timeout,
            workers=self.config.workers,
            max_latency_ms=self.config.max_latency_ms,
            min_success_rate=self.config.min_success_rate,
            max_high_latency_ratio=self.config.max_high_latency_ratio,
            high_latency_streak=self.config.high_latency_streak,
            max_proxies=self.config.max_proxies,
            fetch_timeout=self.config.fetch_timeout,
            verbose=True,
        )

        self._log("[runtime] refreshing proxy pool")
        base_result = run_collection(
            config,
            log_sink=self._log,
            event_sink=self._emit,
            write_output=False,
            cancel_event=cancel_event,
        )
        self._raise_if_cancelled(cancel_event)
        combined_outcomes = list(base_result.outcomes)
        known_keys = {item.proxy.key for item in combined_outcomes}
        best_upstream = next((item.proxy for item in sorted(base_result.working, key=self._working_priority_key)), None)

        manual_proxies = [item for item in self._load_manual_list_proxies() if item.key not in known_keys]
        if manual_proxies:
            self._log(f"[manual-list] probing {len(manual_proxies)} proxies from existing list")
            manual_outcomes = run_async(
                probe_all(
                    proxies=manual_proxies,
                    settings=self._probe_settings(),
                    concurrency=max(1, min(self.config.workers, 10)),
                    verbose=False,
                    log_sink=self._log,
                    event_sink=None,
                    cancel_event=cancel_event,
                )
            )
            combined_outcomes.extend(manual_outcomes)
            known_keys.update(item.proxy.key for item in manual_outcomes)
        self._raise_if_cancelled(cancel_event)

        telegram_sources = self._collect_enabled_telegram_sources()
        if telegram_sources and best_upstream is not None:
            try:
                with self.telegram_lock:
                    thread_proxies = run_async(
                        collect_telegram_sources_proxies(
                            telegram_sources,
                            self.auth_config,
                            upstream_proxy=best_upstream,
                            log_sink=self._log,
                            event_sink=self._emit,
                            total_timeout=max(75.0, float(self.config.fetch_timeout) * 6.0),
                            request_timeout=max(8.0, float(self.config.fetch_timeout)),
                            max_messages=max(1500, self.config.max_proxies or 8000),
                            max_age_days=int(self.config.telegram_source_max_age_days or DEFAULT_SOURCE_MAX_AGE_DAYS),
                            cancel_event=cancel_event,
                        )
                    )
                self.thread_proxy_count = len(thread_proxies)
                self.thread_status = f"loaded:{len(thread_proxies)}"
                new_proxies = [item for item in thread_proxies if item.key not in known_keys]
                if new_proxies:
                    self._log(f"[telegram] probing {len(new_proxies)} new proxies from Telegram sources")
                    self._emit("telegram_sources_probing_started", total_proxies=len(new_proxies))
                    extra_outcomes = run_async(
                        probe_all(
                            proxies=new_proxies,
                            settings=self._probe_settings(),
                            concurrency=max(1, min(self.config.workers, 10)),
                            verbose=False,
                            log_sink=self._log,
                            event_sink=None,
                            cancel_event=cancel_event,
                        )
                    )
                    combined_outcomes.extend(extra_outcomes)
                    self._emit("telegram_sources_probing_finished", total_proxies=len(new_proxies))
                elif thread_proxies:
                    self._log(f"[telegram] sources parsed, all {len(thread_proxies)} proxies were duplicates")
            except Exception as exc:
                self.thread_status = f"skipped:{exc}"
                self._log(f"[telegram] skipped: {exc}")
        elif telegram_sources and best_upstream is None:
            self.thread_status = "skipped:no_working_upstream"
        else:
            self.thread_status = "disabled"
        self._raise_if_cancelled(cancel_event)

        combined_working = sorted((item for item in combined_outcomes if item.accepted), key=outcome_sort_key)
        combined_rejected = sorted(
            (item for item in combined_outcomes if not item.accepted),
            key=lambda item: (item.reason, outcome_sort_key(item)),
        )

        if (self.config.deep_media_enabled or self.config.rf_whitelist_check_enabled) and combined_working:
            combined_working, combined_rejected = self._run_deep_media_checks(
                combined_working,
                combined_rejected,
                strict=self.config.rf_whitelist_check_enabled,
                cancel_event=cancel_event,
            )
        self._raise_if_cancelled(cancel_event)
        self.last_result = base_result
        self.last_outcomes = combined_outcomes
        self.last_working = combined_working
        self.last_rejected = combined_rejected
        self.pool.replace_outcomes(combined_working)
        self._apply_latest_deep_media_scores()

        self._raise_if_cancelled(cancel_event)
        self._export_combined_results(base_result, combined_outcomes, combined_working, combined_rejected, existing_list_urls)
        self.last_refresh_finished_at = time.time()

        self._raise_if_cancelled(cancel_event)
        if self.config.auto_start_local and combined_working:
            self.start_local_server()

        self._emit(
            "runtime_refresh_complete",
            working=len(combined_working),
            rejected=len(combined_rejected),
            unique=len({item.proxy.key for item in combined_outcomes}),
        )

    def run_auth_status(self) -> dict[str, Any]:
        with self.telegram_lock:
            return run_async(get_auth_status(self.auth_config, upstream_proxy=self._best_proxy()))

    def request_auth_code(self, phone: str) -> dict[str, Any]:
        with self.telegram_lock:
            result = run_async(
                request_login_code(
                    self.auth_config,
                    phone=phone,
                    upstream_proxy=self._best_proxy(),
                )
            )
        self._auth_code_hash = result.get("phone_code_hash", "")
        return result

    def complete_auth(self, phone: str, code: str, password: str = "") -> dict[str, Any]:
        if not self._auth_code_hash:
            raise RuntimeError("phone_code_hash_missing")
        with self.telegram_lock:
            result = run_async(
                complete_login(
                    self.auth_config,
                    phone=phone,
                    code=code,
                    phone_code_hash=self._auth_code_hash,
                    password=password,
                    upstream_proxy=self._best_proxy(),
                )
            )
        if result.get("authorized"):
            self._auth_code_hash = ""
        return result

    def logout_auth(self) -> None:
        with self.telegram_lock:
            run_async(logout(self.auth_config, upstream_proxy=self._best_proxy()))

    def run_qr_login(self, *, password: str = "") -> dict[str, Any]:
        with self.telegram_lock:
            return run_async(
                qr_login_flow(
                    self.auth_config,
                    upstream_proxy=self._best_proxy(),
                    password=password,
                    qr_ready=lambda payload: self._emit("telegram_qr_ready", payload),
                )
            )

    def send_working_proxies_to_saved_messages(self) -> dict[str, Any]:
        urls = [item.proxy.url for item in self.last_working]
        with self.telegram_lock:
            return run_async(
                send_proxy_list_to_saved_messages(
                    self.auth_config,
                    urls,
                    upstream_proxy=self._best_proxy(),
                )
            )

    def snapshot(self) -> dict[str, Any]:
        working_rows = self.pool.snapshot()
        current_best = self.pool.best()
        return {
            "working_count": len(self.last_working),
            "rejected_count": len(self.last_rejected),
            "unique_count": len({item.proxy.key for item in self.last_outcomes}),
            "pool_rows": working_rows,
            "local_running": self.local_server.is_running(),
            "local_url": self.local_server.local_proxy_url,
            "local_tg_url": self.local_server.local_proxy_tg_url,
            "local_fake_tls_enabled": self.local_server.fake_tls_enabled,
            "local_fake_tls_domain": self.local_server.fake_tls_domain,
            "best_proxy": current_best.proxy.url if current_best is not None else "",
            "last_refresh_started_at": self.last_refresh_started_at,
            "last_refresh_finished_at": self.last_refresh_finished_at,
            "exports": dict(self.last_export),
            "seed_source": self.seed_source,
            "seed_loaded_at": self.seed_loaded_at,
            "thread_status": self.thread_status,
            "thread_proxy_count": self.thread_proxy_count,
        }

    def _probe_settings(self) -> ProbeSettings:
        return ProbeSettings(
            duration=self.config.duration,
            interval=self.config.interval,
            timeout=self.config.timeout,
            max_latency_ms=self.config.max_latency_ms,
            min_success_rate=self.config.min_success_rate,
            max_high_latency_ratio=self.config.max_high_latency_ratio,
            high_latency_streak=self.config.high_latency_streak,
            unreachable_failures=3,
        )

    def _run_deep_media_checks(
        self,
        working: list[ProbeOutcome],
        rejected: list[ProbeOutcome],
        *,
        strict: bool,
        cancel_event: threading.Event | None = None,
    ) -> tuple[list[ProbeOutcome], list[ProbeOutcome]]:
        working = sorted(working, key=self._working_priority_key)
        with self.telegram_lock:
            auth_status = run_async(get_auth_status(self.auth_config, upstream_proxy=self._best_proxy()))
        if not auth_status.get("authorized"):
            reason = "rf_whitelist" if strict else "deep_media"
            self._log(f"[media] skipped: telegram_session_not_authorized ({reason})")
            self._emit("telegram_auth_required", feature=reason)
            return working, rejected
        candidate_limit = max(1, self.config.deep_media_top_n)
        if strict:
            candidate_limit = max(candidate_limit, min(20, max(10, len(working))))
        top_candidates = working[:candidate_limit]
        self._log(f"[media] deep-checking {len(top_candidates)} proxies")
        self._emit(
            "deep_media_started",
            total=len(top_candidates),
            strict=strict,
        )
        rejected_keys: set[tuple[str, int, str]] = set()
        for index, outcome in enumerate(top_candidates, start=1):
            self._raise_if_cancelled(cancel_event)
            with self.telegram_lock:
                result = run_async(deep_media_probe(outcome.proxy, self.auth_config))
            self._latest_deep_media_scores[result.proxy_key] = (result.score, result.note)
            self.pool.update_deep_media_score(result.proxy_key, result.score, result.note)
            self._log(f"[media] {outcome.proxy.host}:{outcome.proxy.port} -> {result.note}")
            self._emit(
                "deep_media_progress",
                index=index,
                total=len(top_candidates),
                host=outcome.proxy.host,
                port=outcome.proxy.port,
                score=result.score,
                note=result.note,
                strict=strict,
            )
            if strict and (result.score is None or result.score < 0.75):
                rejected_keys.add(result.proxy_key)

        self._emit(
            "deep_media_finished",
            total=len(top_candidates),
            strict=strict,
            rejected=len(rejected_keys),
        )

        if not strict or not rejected_keys:
            return sorted(working, key=self._working_priority_key), rejected

        filtered_working: list[ProbeOutcome] = []
        for outcome in working:
            if outcome.proxy.key in rejected_keys:
                rejected.append(
                    ProbeOutcome(
                        proxy=outcome.proxy,
                        attempts=outcome.attempts,
                        successes=outcome.successes,
                        failures=outcome.failures,
                        success_rate=outcome.success_rate,
                        avg_latency_ms=outcome.avg_latency_ms,
                        p95_latency_ms=outcome.p95_latency_ms,
                        min_latency_ms=outcome.min_latency_ms,
                        max_latency_ms=outcome.max_latency_ms,
                        high_latency_ratio=outcome.high_latency_ratio,
                        max_consecutive_failures=outcome.max_consecutive_failures,
                        max_consecutive_high_latency=outcome.max_consecutive_high_latency,
                        accepted=False,
                        reason="rf_whitelist_media_failed",
                        elapsed_seconds=outcome.elapsed_seconds,
                        early_stop=outcome.early_stop,
                    )
                )
            else:
                filtered_working.append(outcome)

        return sorted(filtered_working, key=self._working_priority_key), sorted(
            rejected,
            key=lambda item: (item.reason, outcome_sort_key(item)),
        )

    def _live_probe_loop(self) -> None:
        while not self.live_probe_stop.wait(timeout=5.0):
            if self.pool.count() <= 0:
                continue
            try:
                self._run_background_health_cycle()
            except Exception as exc:
                self._log(f"[live] probe loop error: {exc}")

    def _run_background_health_cycle(self) -> None:
        now = time.time()
        focused_interval = 35.0 if self.local_server.is_running() else 75.0
        broad_interval = max(150.0, float(self.config.live_probe_interval_sec) * 6.0)
        media_interval = 900.0

        if (now - self._last_focused_probe_at) >= focused_interval:
            self._run_live_probe_once(focused=True)
            self._last_focused_probe_at = now

        if (now - self._last_broad_probe_at) >= broad_interval:
            self._run_live_probe_once(focused=False)
            self._last_broad_probe_at = now

        if (now - self._last_media_pulse_at) >= media_interval:
            self._run_background_media_pulse()
            self._last_media_pulse_at = now

    def _run_live_probe_once(self, *, focused: bool) -> None:
        if focused:
            candidates = self.pool.select_monitor_targets(limit=2)
        else:
            candidates = self.pool.select_candidates(is_media=False, limit=max(1, min(4, self.config.live_probe_top_n)))
        if not candidates:
            return

        settings = ProbeSettings(
            duration=min(3.5, max(2.0, float(self.config.live_probe_duration_sec if not focused else 2.5))),
            interval=0.7,
            timeout=min(6.0, self.config.timeout),
            max_latency_ms=self.config.max_latency_ms,
            min_success_rate=0.34,
            max_high_latency_ratio=1.0,
            high_latency_streak=5,
            unreachable_failures=2,
        )
        outcomes = run_async(
            probe_all(
                proxies=[item.proxy for item in candidates],
                settings=settings,
                concurrency=max(1, min(2 if focused else 4, len(candidates))),
                verbose=False,
                log_sink=self._log,
                event_sink=None,
            )
        )
        for outcome in outcomes:
            ok = outcome.successes > 0
            cooldown_reason = self.pool.update_live_probe(
                outcome.proxy.key,
                outcome.avg_latency_ms,
                ok,
                outcome.reason,
                max_latency_ms=float(self.config.max_latency_ms or 300.0),
                high_latency_streak_limit=2 if focused else 3,
                failure_limit=2 if focused else 3,
                cooldown_seconds=180.0 if focused else 120.0,
            )
            if cooldown_reason:
                self._log(f"[live] demoted {outcome.proxy.host}:{outcome.proxy.port} -> {cooldown_reason}")
                self._emit(
                    "proxy_cooldown",
                    host=outcome.proxy.host,
                    port=outcome.proxy.port,
                    reason=cooldown_reason,
                )
        self._emit("live_probe_updated", count=len(outcomes), focused=focused)

    def _run_background_media_pulse(self) -> None:
        if not self.local_server.is_running():
            return
        if not self.auth_config.api_id or not self.auth_config.api_hash.strip():
            return
        candidates = self.pool.select_monitor_targets(limit=1)
        if not candidates:
            return
        target = candidates[0]
        try:
            with self.telegram_lock:
                result = run_async(light_media_probe(target.proxy, self.auth_config))
        except Exception as exc:
            self._log(f"[media-bg] probe error for {target.proxy.host}:{target.proxy.port} -> {exc}")
            return

        if result.note == "session_not_authorized":
            self._emit("telegram_auth_required", feature="background_media")
            self._log("[media-bg] skipped: telegram_session_not_authorized")
            return
        if result.note == "no_media_samples_found":
            self.pool.update_deep_media_score(result.proxy_key, result.score, result.note)
            self._log(f"[media-bg] {target.proxy.host}:{target.proxy.port} -> {result.note}")
            return

        cooldown_reason = self.pool.update_background_media_probe(
            result.proxy_key,
            result.score,
            result.note,
            failure_score=0.6,
            cooldown_seconds=300.0,
        )
        self._log(
            f"[media-bg] {target.proxy.host}:{target.proxy.port} -> "
            f"{result.note} score={result.score if result.score is not None else 'n/a'}"
        )
        if cooldown_reason:
            self._emit(
                "proxy_cooldown",
                host=target.proxy.host,
                port=target.proxy.port,
                reason=cooldown_reason,
            )

    def _export_combined_results(
        self,
        base_result: CollectorRunResult,
        all_outcomes: list[ProbeOutcome],
        working: list[ProbeOutcome],
        rejected: list[ProbeOutcome],
        existing_list_urls: list[str],
    ) -> None:
        out_dir = (self.root_dir / self.config.out_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        all_txt_path = out_dir / ALL_FILE_NAME
        working_txt_path = out_dir / LIST_FILE_NAME
        rejected_txt_path = out_dir / REJECTED_FILE_NAME
        report_json_path = out_dir / REPORT_FILE_NAME
        socks5_all_txt_path = out_dir / SOCKS5_FILE_NAME

        persistent_urls = self._read_url_list(self._persistent_proxy_cache_path())
        merged_working_urls = self._merge_existing_proxy_list(
            persistent_urls + existing_list_urls,
            [item.proxy.url for item in working],
        )
        self._write_url_list(all_txt_path, [item.proxy.url for item in all_outcomes])
        self._write_url_list(working_txt_path, merged_working_urls)
        self._write_url_list(rejected_txt_path, [item.proxy.url for item in rejected])
        self._write_url_list(socks5_all_txt_path, [item.url for item in base_result.socks5])
        self._write_url_list(self._persistent_proxy_cache_path(), merged_working_urls)

        report = build_report(
            base_result.source_summaries,
            [item.proxy for item in all_outcomes],
            base_result.socks5,
            all_outcomes,
            base_result.config,
        )
        report["notes"].append("Local app runtime may further reprioritize proxies using live media/session telemetry.")
        report["telegram_sources_enabled"] = self.config.telegram_sources_enabled
        report["telegram_sources"] = list(self._collect_enabled_telegram_sources())
        report["deep_media_enabled"] = self.config.deep_media_enabled
        report["rf_whitelist_check_enabled"] = self.config.rf_whitelist_check_enabled
        report["thread_source_enabled"] = self.config.thread_source_enabled
        report["thread_source_url"] = self.config.thread_source_url
        report["proxies"] = self._augment_report_proxy_rows(report["proxies"])
        self._write_json_file(report_json_path, report)

        self.last_export = {
            "all_txt_path": str(all_txt_path),
            "working_txt_path": str(working_txt_path),
            "rejected_txt_path": str(rejected_txt_path),
            "socks5_all_txt_path": str(socks5_all_txt_path),
            "report_json_path": str(report_json_path),
        }
        self._emit(
            "files_written",
            out_dir=str(out_dir),
            all_txt_path=str(all_txt_path),
            working_txt_path=str(working_txt_path),
            rejected_txt_path=str(rejected_txt_path),
            socks5_all_txt_path=str(socks5_all_txt_path),
            report_json_path=str(report_json_path),
        )

    def _augment_report_proxy_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        pool_rows = {row["url"]: row for row in self.pool.snapshot()}
        for row in rows:
            extra = pool_rows.get(row["url"])
            if extra is None:
                continue
            row["live_latency_ms"] = extra["live_latency_ms"]
            row["media_score"] = extra["media_score"]
            row["deep_media_score"] = extra["deep_media_score"]
            row["deep_media_note"] = extra["deep_media_note"]
        return rows

    def _apply_latest_deep_media_scores(self) -> None:
        for proxy_key, (score, note) in self._latest_deep_media_scores.items():
            self.pool.update_deep_media_score(proxy_key, score, note)

    def _best_proxy(self):
        best = self.pool.best()
        if best is not None:
            return best.proxy
        if self.last_working:
            return self.last_working[0].proxy
        return None

    def _collect_enabled_telegram_sources(self) -> list[str]:
        if not bool(self.config.telegram_sources_enabled):
            return []
        merged: list[str] = []
        seen: set[str] = set()
        for raw_url in self.config.telegram_sources:
            url = str(raw_url).strip()
            if not url or url in seen:
                continue
            seen.add(url)
            merged.append(url)
        if not merged and bool(self.config.thread_source_enabled):
            legacy_url = str(self.config.thread_source_url).strip()
            if legacy_url:
                merged.append(legacy_url)
        return merged

    @staticmethod
    def _effective_fake_tls_domain(enabled: bool, domain: str) -> str:
        return ""

    def _load_manual_list_proxies(self) -> list[ProxyRecord]:
        paths = [
            self.root_dir / self.config.out_dir / LIST_FILE_NAME,
            self._persistent_proxy_cache_path(),
            self.root_dir / LEGACY_OUT_DIR_NAME / LEGACY_WORKING_FILE_NAME,
        ]
        proxies: dict[tuple[str, int, str], ProxyRecord] = {}
        for path in paths:
            if not path.exists():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except Exception as exc:
                self._log(f"[manual-list] failed to read {path.name}: {exc}")
                continue
            for raw_line in lines:
                line = raw_line.strip()
                if not line:
                    continue
                proxy = parse_proxy_link(line, str(path), str(path))
                if proxy is None:
                    continue
                proxy.sources.add(f"file:{path.name}")
                proxies[proxy.key] = proxy
        return list(proxies.values())

    def _read_existing_proxy_list_urls(self) -> list[str]:
        candidates = [
            self.root_dir / self.config.out_dir / LIST_FILE_NAME,
            self._persistent_proxy_cache_path(),
            self.root_dir / LEGACY_OUT_DIR_NAME / LEGACY_WORKING_FILE_NAME,
        ]
        merged: list[str] = []
        seen: set[str] = set()
        for path in candidates:
            for url in self._read_url_list(path):
                if url in seen:
                    continue
                seen.add(url)
                merged.append(url)
        return merged

    def _merge_existing_proxy_list(self, existing_urls: list[str], fresh_urls: list[str]) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for line in existing_urls:
            url = line.strip()
            if not url or url in seen:
                continue
            seen.add(url)
            merged.append(url)
        for line in fresh_urls:
            url = line.strip()
            if not url or url in seen:
                continue
            seen.add(url)
            merged.append(url)
        return merged

    def _persistent_proxy_cache_path(self) -> Path:
        return self.state_dir / PERSISTENT_PROXY_CACHE_FILE_NAME

    def _read_url_list(self, path: Path) -> list[str]:
        if not path.exists():
            return []
        try:
            return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except Exception as exc:
            self._log(f"[manual-list] failed to read {path.name}: {exc}")
            return []

    def _write_url_list(self, path: Path, urls: list[str]) -> None:
        unique_urls = self._merge_existing_proxy_list([], urls)
        content = "\n".join(unique_urls)
        if content:
            content += "\n"
        self._atomic_write(path, content)

    def _write_json_file(self, path: Path, payload: dict[str, Any]) -> None:
        self._atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2))

    def _atomic_write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(content, encoding="utf-8")
        temp_path.replace(path)

    def _load_initial_pool(self) -> None:
        report_candidates = [
            (self.root_dir / self.config.out_dir / LIST_FILE_NAME, "default_list"),
            (self.root_dir / LEGACY_OUT_DIR_NAME / LEGACY_WORKING_FILE_NAME, "legacy_working_list"),
            (self.root_dir / self.config.out_dir / REPORT_FILE_NAME, "cached_report"),
            (self.root_dir / LEGACY_OUT_DIR_NAME / LEGACY_REPORT_FILE_NAME, "legacy_cached_report"),
            (self.root_dir / "mtproxy_seed.json", "bundled_seed"),
        ]

        for report_path, source_name in report_candidates:
            outcomes = self._load_seed_outcomes(report_path, source_name=source_name)
            if not outcomes:
                continue
            if source_name in {"cached_report", "legacy_cached_report"} and len(outcomes) < 3:
                self._log(f"[seed] skipped weak cache {report_path.name}: only {len(outcomes)} working proxies")
                continue

            self.last_outcomes = list(outcomes)
            self.last_working = sorted((item for item in outcomes if item.accepted), key=outcome_sort_key)
            self.last_rejected = sorted(
                (item for item in outcomes if not item.accepted),
                key=lambda item: (item.reason, outcome_sort_key(item)),
            )
            self.pool.replace_outcomes(self.last_working)
            self.seed_source = source_name
            self.seed_loaded_at = time.time()
            self._log(f"[seed] loaded {len(self.last_working)} working proxies from {report_path.name}")
            self._emit(
                "seed_loaded",
                source=source_name,
                count=len(self.last_working),
                path=str(report_path),
            )
            break

    def _load_seed_outcomes(self, report_path: Path, *, source_name: str) -> list[ProbeOutcome]:
        if not report_path.exists():
            return []

        if report_path.suffix.lower() == ".txt":
            return self._load_seed_outcomes_from_txt(report_path, source_name=source_name)

        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self._log(f"[seed] failed to read {report_path.name}: {exc}")
            return []

        proxy_rows = payload.get("proxies")
        if not isinstance(proxy_rows, list):
            return []

        outcomes: list[ProbeOutcome] = []
        for row in proxy_rows:
            outcome = self._seed_row_to_outcome(row)
            if outcome is not None and outcome.accepted:
                outcomes.append(outcome)
        return outcomes

    def _load_seed_outcomes_from_txt(self, path: Path, *, source_name: str) -> list[ProbeOutcome]:
        outcomes: list[ProbeOutcome] = []
        seen: set[tuple[str, int, str]] = set()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception as exc:
            self._log(f"[seed] failed to read {path.name}: {exc}")
            return outcomes

        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            proxy = parse_proxy_link(line, str(path), str(path))
            if proxy is None or proxy.key in seen:
                continue
            seen.add(proxy.key)
            outcomes.append(
                ProbeOutcome(
                    proxy=proxy,
                    attempts=1,
                    successes=1,
                    failures=0,
                    success_rate=1.0,
                    avg_latency_ms=None,
                    p95_latency_ms=None,
                    min_latency_ms=None,
                    max_latency_ms=None,
                    high_latency_ratio=0.0,
                    max_consecutive_failures=0,
                    max_consecutive_high_latency=0,
                    accepted=True,
                    reason=source_name,
                    elapsed_seconds=0.0,
                    early_stop="seed_list",
                )
            )
        return outcomes

    def _seed_row_to_outcome(self, row: dict[str, Any]) -> ProbeOutcome | None:
        try:
            proxy = ProxyRecord(
                host=str(row["host"]).strip().lower(),
                port=int(row["port"]),
                secret=str(row["secret"]).strip().lower(),
                sources=set(row.get("sources", []) or []),
                discovered_from=set(row.get("discovered_from", []) or []),
            )
            return ProbeOutcome(
                proxy=proxy,
                attempts=int(row.get("attempts") or 0),
                successes=int(row.get("successes") or 0),
                failures=int(row.get("failures") or 0),
                success_rate=float(row.get("success_rate") or 0.0),
                avg_latency_ms=_to_float(row.get("avg_latency_ms")),
                p95_latency_ms=_to_float(row.get("p95_latency_ms")),
                min_latency_ms=_to_float(row.get("min_latency_ms")),
                max_latency_ms=_to_float(row.get("max_latency_ms")),
                high_latency_ratio=float(row.get("high_latency_ratio") or 0.0),
                max_consecutive_failures=int(row.get("max_consecutive_failures") or 0),
                max_consecutive_high_latency=int(row.get("max_consecutive_high_latency") or 0),
                accepted=bool(row.get("accepted")),
                reason=str(row.get("reason") or "seed"),
                elapsed_seconds=float(row.get("elapsed_seconds") or 0.0),
                early_stop=row.get("early_stop"),
            )
        except Exception:
            return None

    def _load_config(self) -> AppConfig:
        legacy_path = self.root_dir / "app_state" / CONFIG_FILE_NAME
        if not self.config_path.exists() and legacy_path.exists():
            with contextlib.suppress(Exception):
                self.config_path.write_text(legacy_path.read_text(encoding="utf-8"), encoding="utf-8")
        if not self.config_path.exists():
            config = AppConfig()
            self.config_path.write_text(json.dumps(asdict(config), ensure_ascii=False, indent=2), encoding="utf-8")
            return config
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        normalized = False
        if data.get("out_dir") in ("", LEGACY_OUT_DIR_NAME, None):
            data["out_dir"] = LIST_DIR_NAME
            normalized = True
        if data.get("out_dir") == "list_test":
            data["out_dir"] = LIST_DIR_NAME
            normalized = True
        if data.get("appearance") not in {"auto", "light", "dark"}:
            data["appearance"] = "auto"
            normalized = True
        if data.get("telegram_session_file") in ("", "app_state/telegram_user", "app_state/telegram_user.session", None):
            data["telegram_session_file"] = f"{DATA_DIR_NAME}/telegram_user.sec"
            normalized = True
        if "local_fake_tls_enabled" not in data:
            data["local_fake_tls_enabled"] = False
            normalized = True
        elif data.get("local_fake_tls_enabled"):
            data["local_fake_tls_enabled"] = False
            normalized = True
        if "local_fake_tls_domain" not in data:
            data["local_fake_tls_domain"] = ""
            normalized = True
        elif data.get("local_fake_tls_domain"):
            data["local_fake_tls_domain"] = ""
            normalized = True
        try:
            source_max_age_days = int(data.get("telegram_source_max_age_days") or 0)
        except (TypeError, ValueError):
            source_max_age_days = 0
        if "telegram_source_max_age_days" not in data or source_max_age_days <= 0:
            data["telegram_source_max_age_days"] = DEFAULT_SOURCE_MAX_AGE_DAYS
            normalized = True
        if "rf_whitelist_check_enabled" not in data:
            data["rf_whitelist_check_enabled"] = False
            normalized = True
        if "auto_update_enabled" not in data:
            data["auto_update_enabled"] = True
            normalized = True
        if "telegram_sources_enabled" not in data:
            data["telegram_sources_enabled"] = bool(data.get("thread_source_enabled", False))
            normalized = True
        if "telegram_sources" not in data or not isinstance(data.get("telegram_sources"), list):
            legacy_url = str(data.get("thread_source_url") or "").strip()
            data["telegram_sources"] = [legacy_url] if legacy_url else list(DEFAULT_TELEGRAM_SOURCE_URLS)
            normalized = True
        if not data.get("thread_source_url"):
            telegram_sources = [str(item).strip() for item in data.get("telegram_sources", []) if str(item).strip()]
            if telegram_sources:
                data["thread_source_url"] = telegram_sources[0]
                normalized = True
        if "thread_source_enabled" not in data:
            data["thread_source_enabled"] = bool(data.get("telegram_sources_enabled", False))
            normalized = True
        sources = [str(item).strip() for item in data.get("sources", []) if str(item).strip()]
        for source in RECOMMENDED_WEB_SOURCE_ADDITIONS:
            if source not in sources:
                sources.append(source)
                normalized = True
        data["sources"] = sources
        telegram_sources = [str(item).strip() for item in data.get("telegram_sources", []) if str(item).strip()]
        for source in RECOMMENDED_TELEGRAM_SOURCE_ADDITIONS:
            if source not in telegram_sources:
                telegram_sources.append(source)
                normalized = True
        data["telegram_sources"] = telegram_sources
        if normalized:
            with contextlib.suppress(Exception):
                self.config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        defaults = asdict(AppConfig())
        defaults.update(data)
        return AppConfig(**defaults)

    def _working_priority_key(self, outcome: ProbeOutcome) -> tuple[float, float, float, float, str]:
        latency = outcome.avg_latency_ms if outcome.avg_latency_ms is not None else 9_999.0
        pool_row = self.pool.snapshot_by_key(outcome.proxy.key)
        latest_media = self._latest_deep_media_scores.get(outcome.proxy.key)
        media_score = latest_media[0] if latest_media is not None else None
        if media_score is None and pool_row:
            media_score = pool_row.get("deep_media_score")
        if media_score is None and pool_row:
            fallback_media = pool_row.get("media_score")
            if fallback_media is not None and float(fallback_media) >= 0.0:
                media_score = float(fallback_media)
        media_penalty = -float(media_score) if media_score is not None else 0.0
        return (
            media_penalty,
            latency,
            -outcome.success_rate,
            outcome.high_latency_ratio,
            outcome.proxy.url,
        )

    def _log(self, message: str) -> None:
        if self.log_sink is not None:
            self.log_sink(message)

    def _emit(self, event_name: str, payload: dict[str, Any] | None = None, **kwargs: Any) -> None:
        if self.event_sink is not None:
            merged = dict(payload or {})
            merged.update(kwargs)
            self.event_sink(event_name, merged)


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _hide_windows_path(path: Path) -> None:
    if sys.platform != "win32":
        return
    with contextlib.suppress(Exception):
        ctypes.windll.kernel32.SetFileAttributesW(str(path), FILE_ATTRIBUTE_HIDDEN)
