from __future__ import annotations

import contextlib
import ctypes
import json
import os
import shutil
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
    MTPROXYTG_MIRROR_GROUP,
    MTPROXYTG_MIRRORS,
    ProbeOutcome,
    ProbeSettings,
    ProxyRecord,
    build_report,
    outcome_sort_key,
    parse_proxy_link,
    probe_all,
    run_collection,
    run_async,
    scan_text,
)
from mtproxy_local_proxy import LocalMTProxyServer, ProxyPool
from mtproxy_tg_ws.stats import stats as tg_ws_stats
from mtproxy_tg_ws_runtime import TgWsProxyRuntimeConfig, TgWsProxyServer
from mtproxy_telegram import (
    DEFAULT_SOURCE_MAX_AGE_DAYS,
    DEFAULT_SOURCE_MAX_PROXIES,
    DEFAULT_SOURCE_MAX_MESSAGES,
    DEFAULT_TELEGRAM_SOURCE_URLS,
    TELEGRAM_USER_ERROR_PREFIX,
    MediaProbeResult,
    TelegramAuthConfig,
    auth_is_configured,
    collect_telegram_sources_proxies,
    collect_thread_proxies,
    complete_login,
    deep_media_probe,
    get_auth_status,
    light_media_probe,
    logout,
    normalize_telegram_phone,
    request_login_code,
    send_proxy_list_to_saved_messages,
)
from mtproxy_xray_runtime import DEFAULT_XRAY_SUBSCRIPTIONS, XrayCoreRuntime, XrayRuntimeConfig


def runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        executable_path = Path(sys.executable).resolve()
        if sys.platform == "darwin":
            app_bundle = _macos_app_bundle_path(executable_path)
            if app_bundle is not None:
                bundle_parent = app_bundle.parent
                if any((bundle_parent / name).exists() for name in ("config.json", ".env", "list")):
                    return bundle_parent
                if os.access(bundle_parent, os.W_OK):
                    return bundle_parent
                support_dir = Path.home() / "Library" / "Application Support" / _runtime_app_dir_name()
                support_dir.mkdir(parents=True, exist_ok=True)
                return support_dir
        return executable_path.parent
    return Path(__file__).resolve().parent


def _macos_app_bundle_path(executable_path: Path) -> Path | None:
    macos_dir = executable_path.parent
    if macos_dir.name != "MacOS":
        return None
    contents_dir = macos_dir.parent
    if contents_dir.name != "Contents":
        return None
    app_bundle = contents_dir.parent
    if app_bundle.suffix != ".app":
        return None
    return app_bundle


def _runtime_app_dir_name() -> str:
    return "MTProxyAutoSwitch"


def bundled_resource_roots() -> list[Path]:
    roots: list[Path] = []
    if getattr(sys, "frozen", False):
        executable_path = Path(sys.executable).resolve()
        roots.append(Path(getattr(sys, "_MEIPASS", executable_path.parent)))
        roots.append(executable_path.parent)
        if sys.platform == "darwin":
            app_bundle = _macos_app_bundle_path(executable_path)
            if app_bundle is not None:
                roots.append(app_bundle.parent)
                roots.append(app_bundle / "Contents" / "Resources")
    roots.append(Path(__file__).resolve().parent)
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        marker = str(root.resolve())
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(root)
    return unique


def persistent_state_root(install_dir: Path) -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
        root = base / _runtime_app_dir_name()
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support" / _runtime_app_dir_name()
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
        root = base / _runtime_app_dir_name()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_int(value: object, default: int = 0) -> int:
    text = "".join(ch for ch in str(value or "") if ch.isdigit())
    try:
        return int(text or default)
    except (TypeError, ValueError):
        return int(default)


def _clean_api_hash(value: object) -> str:
    return "".join(str(value or "").split())


DEFAULT_MAX_PROXIES = 500
DEFAULT_DEEP_MEDIA_TOP_N = 0
DEFAULT_FAST_LIST_LIMIT = 24
XRAY_QUICK_SWITCH_LATENCY_MS = 240.0
XRAY_FULL_REFRESH_LATENCY_MS = 300.0
XRAY_HEALTH_INTERVAL_SEC = 45.0
XRAY_QUICK_SWITCH_COOLDOWN_SEC = 120.0
XRAY_AUTO_REFRESH_COOLDOWN_SEC = 900.0
XRAY_QUICK_SWITCH_CONFIRM_STREAK = 2
XRAY_FULL_REFRESH_CONFIRM_STREAK = 14
XRAY_FULL_REFRESH_CONFIRM_SEC = 600.0
XRAY_HEALTH_FAIL_RESTART_STREAK = 2
XRAY_SPEED_SAMPLE_INTERVAL_SEC = 90.0
MTPROXY_QUICK_SWITCH_LATENCY_MS = 240.0
MTPROXY_FULL_REFRESH_LATENCY_MS = 300.0
MTPROXY_HEALTH_INTERVAL_SEC = 45.0
MTPROXY_QUICK_SWITCH_COOLDOWN_SEC = 120.0
MTPROXY_AUTO_REFRESH_COOLDOWN_SEC = 900.0
DAILY_FULL_REFRESH_INTERVAL_SEC = 24.0 * 3600.0
MTPROXY_QUICK_SWITCH_CONFIRM_STREAK = 2
MTPROXY_FULL_REFRESH_CONFIRM_STREAK = 14
MTPROXY_FULL_REFRESH_CONFIRM_SEC = 600.0
FAST_LIST_FILE_NAME = "fast_list.txt"
TG_PARSED_FILE_NAME = "tg_parsed_proxy.txt"
DEFAULT_LOCAL_SECRET = "274763e0d711fd394e833938dd93c8c3"
OLD_DEFAULT_TELEGRAM_API_PROXY_URL = (
    "https://t.me/proxy?server=max.ru.rightarion.ru&port=443"
    "&secret=eedcaae509a2455bbfc6165f1708fd5c586d61782e7275"
)
OLD_DEFAULT_TELEGRAM_API_PROXY_URL_2 = (
    "https://t.me/proxy?server=myrka.bronstein.ar&port=8443"
    "&secret=eea37385d8a4bbf632eabc9091fdc95a9c6d79726b612e62726f6e737465696e2e6172"
)
DEFAULT_TELEGRAM_API_PROXY_URL = (
    f"tg://proxy?server=127.0.0.1&port=1443&secret=dd{DEFAULT_LOCAL_SECRET}"
)
BALANCER_STRATEGIES = {
    "round_robin",
    "consistent_hash",
    "sticky_session",
}
APP_MODES = {
    "mtproxy_picker",
    "xray_core",
    "tg_ws_proxy",
}
REMOVED_WEB_SOURCES = {
    "local:telegram-proxy-collector",
    "telegram-proxy-collector",
    "telegram proxy collector",
    "https://mtpro.xyz/socks5-ru",
}


@dataclass
class AppConfig:
    active_mode: str = "mtproxy_picker"
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
    max_proxies: int = DEFAULT_MAX_PROXIES
    fast_list_limit: int = DEFAULT_FAST_LIST_LIMIT
    local_host: str = "127.0.0.1"
    local_port: int = 1443
    local_secret: str = DEFAULT_LOCAL_SECRET
    balancer_strategy: str = "sticky_session"
    manual_upstream_url: str = ""
    auto_start_local: bool = True
    autostart_enabled: bool = False
    start_minimized_to_tray: bool = False
    close_behavior: str = "ask"
    telegram_sources_enabled: bool = False
    telegram_sources: list[str] = field(default_factory=lambda: list(DEFAULT_TELEGRAM_SOURCE_URLS))
    thread_source_enabled: bool = False
    thread_source_url: str = "https://t.me/strbypass/237103"
    telegram_source_max_age_days: int = DEFAULT_SOURCE_MAX_AGE_DAYS
    telegram_source_max_messages: int = DEFAULT_SOURCE_MAX_MESSAGES
    telegram_source_max_proxies: int = DEFAULT_SOURCE_MAX_PROXIES
    live_probe_interval_sec: int = 20
    live_probe_duration_sec: float = 4.0
    live_probe_top_n: int = 12
    deep_media_enabled: bool = False
    rf_whitelist_check_enabled: bool = False
    deep_media_top_n: int = DEFAULT_DEEP_MEDIA_TOP_N
    appearance: str = "auto"
    auto_update_enabled: bool = True
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_api_proxy_enabled: bool = False
    telegram_api_proxy_url: str = DEFAULT_TELEGRAM_API_PROXY_URL
    telegram_phone: str = ""
    telegram_session_file: str = "telegram_user.sec"
    xray_subscription_urls: list[str] = field(default_factory=lambda: list(DEFAULT_XRAY_SUBSCRIPTIONS))
    xray_socks_host: str = "127.0.0.1"
    xray_socks_port: int = 10808
    xray_probe_workers: int = 4
    xray_probe_timeout_sec: float = 8.0
    xray_max_servers: int = 250
    xray_binary_path: str = ""
    sing_box_binary_path: str = ""
    xray_manual_upstream_url: str = ""
    tg_ws_host: str = "127.0.0.1"
    tg_ws_port: int = 1443
    tg_ws_secret: str = DEFAULT_LOCAL_SECRET
    tg_ws_dc_ip: list[str] = field(default_factory=lambda: ["2:149.154.167.220", "4:149.154.167.220"])
    tg_ws_buf_kb: int = 256
    tg_ws_pool_size: int = 4
    tg_ws_cfproxy_enabled: bool = True
    tg_ws_cfproxy_priority: bool = True
    tg_ws_cfproxy_user_domain: str = ""
    tg_ws_cfproxy_worker_domain: str = ""
    tg_ws_force_test_dc: bool = False
    tg_ws_fake_tls_domain: str = ""
    tg_ws_proxy_protocol: bool = False


LIST_DIR_NAME = "list"
LIST_FILE_NAME = "proxy_list.txt"
FAST_LIST_FILE_NAME = "fast_list.txt"
REJECTED_FILE_NAME = "rejected_list.txt"
ALL_FILE_NAME = "all_list.txt"
SOCKS5_FILE_NAME = "socks5_list.txt"
REPORT_FILE_NAME = "report.json"
SOURCE_AUDIT_FILE_NAME = "source_audit.txt"
LEGACY_OUT_DIR_NAME = "mtproxy_output"
LEGACY_WORKING_FILE_NAME = "mtproxy_working.txt"
LEGACY_REJECTED_FILE_NAME = "mtproxy_rejected.txt"
LEGACY_ALL_FILE_NAME = "mtproxy_all.txt"
LEGACY_SOCKS5_FILE_NAME = "socks5_all.txt"
LEGACY_REPORT_FILE_NAME = "mtproxy_report.json"
CONFIG_FILE_NAME = "config.json"
DATA_DIR_NAME = "data"
TELEGRAM_AUTH_STATE_FILE_NAME = "telegram_auth.json"
FILE_ATTRIBUTE_HIDDEN = 0x02
RECOMMENDED_WEB_SOURCE_ADDITIONS = [
    *DEFAULT_SOURCES,
    "https://raw.githubusercontent.com/soroushmirzaei/telegram-proxies-collector/main/proxies/ip/mtproto",
    "https://raw.githubusercontent.com/4IceG/Personal-proxies/master/zap-mtproto",
    "https://raw.githubusercontent.com/themrb/mtproto-proxy-data/main/all_proxies.txt",
    "https://raw.githubusercontent.com/mheidari98/.proxy/main/all",
]
RECOMMENDED_TELEGRAM_SOURCE_ADDITIONS = [
    "https://t.me/telemtrs/16160",
    "https://t.me/telemtfreeproxy",
    "https://t.me/ProxyFree_Ru",
    "https://t.me/JustMTProxy",
    "https://t.me/ProxyMTProto",
    "https://t.me/LowiKForum/10805",
    "https://t.me/urlsources/5",
    "https://t.me/urlsources/6",
    "https://t.me/TProxyRU",
    "https://t.me/noWhiteListBlock",
    "https://t.me/ProxyFreeMTProto",
    "https://t.me/vpn4everyone/10",
    "https://t.me/freeinternet_byMygalaru/16",
    "https://t.me/c/3953426502/7",
    "https://t.me/AccarMTProto",
    "https://t.me/kfwlforum/8",
]
SAVED_MESSAGES_EXPORT_LIMIT = 20


def is_public_release() -> bool:
    return True


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
        self.install_dir = runtime_root()
        self.root_dir = self.install_dir
        self.state_root = persistent_state_root(self.install_dir)
        self._migrate_legacy_state()
        self.state_dir = self.state_root / DATA_DIR_NAME
        self.state_dir.mkdir(parents=True, exist_ok=True)
        _hide_windows_path(self.state_dir)
        self.env_values = {
            **_read_env_file(self.install_dir),
            **_read_env_file(self.state_root),
        }
        self.config_path = self.state_root / CONFIG_FILE_NAME
        self.config = self._load_config()
        self._migrate_legacy_telegram_session()
        self.pool = ProxyPool()
        self.log_sink = log_sink
        self.event_sink = event_sink
        self._shutdown_requested = False
        self.local_server = LocalMTProxyServer(
            self.pool,
            host=self.config.local_host,
            port=self.config.local_port,
            secret=self.config.local_secret,
            selection_strategy=self.config.balancer_strategy,
            log_sink=self._log,
            event_sink=self._emit,
        )
        self.tg_ws_server = self._build_tg_ws_server()
        self.xray_runtime = self._build_xray_runtime()
        self.last_result: CollectorRunResult | None = None
        self.last_outcomes: list[ProbeOutcome] = []
        self.last_working: list[ProbeOutcome] = []
        self.last_rejected: list[ProbeOutcome] = []
        self.last_export: dict[str, str] = {}
        self.last_refresh_started_at: float = 0.0
        self.last_refresh_finished_at: float = 0.0
        self.last_refresh_stats: dict[str, int] = {}
        self.seed_source: str = ""
        self.seed_loaded_at: float = 0.0
        self.thread_status: str = "not_checked"
        self.thread_proxy_count: int = 0
        self._latest_deep_media_scores: dict[tuple[str, int, str], MediaProbeResult] = {}
        self.telegram_lock = threading.RLock()
        self._last_quick_probe_at: float = 0.0
        self._last_mtproxy_health_at: float = 0.0
        self._last_mtproxy_quick_sort_at: float = 0.0
        self._last_mtproxy_auto_refresh_at: float = 0.0
        self._mtproxy_high_latency_streak: int = 0
        self._mtproxy_full_refresh_candidate_since: float = 0.0
        self._refresh_in_progress = threading.Event()
        with contextlib.suppress(Exception):
            stale_cache_path = self.state_dir / "proxy_list_persist.txt"
            if stale_cache_path.exists():
                stale_cache_path.unlink()
        self._load_initial_pool()
        self._apply_manual_override_from_config()
        self.live_probe_stop = threading.Event()
        self._last_focused_probe_at: float = 0.0
        self._last_broad_probe_at: float = 0.0
        self._last_media_pulse_at: float = 0.0
        self._last_media_activity_at: float = 0.0
        self._last_heavy_upload_at: float = 0.0
        self._last_media_accel_probe_at: float = 0.0
        self._last_xray_health_at: float = 0.0
        self._last_xray_quick_sort_at: float = 0.0
        self._last_xray_auto_refresh_at: float = 0.0
        self._last_xray_speed_sample_at: float = 0.0
        self._xray_high_latency_streak: int = 0
        self._xray_full_refresh_candidate_since: float = 0.0
        self._xray_health_fail_streak: int = 0
        self._xray_restart_attempted_at: float = 0.0
        self._health_cycle_lock = threading.RLock()
        self.live_probe_thread = threading.Thread(target=self._live_probe_loop, daemon=True, name="mtproxy-live-probe")
        self.live_probe_thread.start()
        self._auth_code_hash: str = ""
        self._auth_code_phone: str = ""
        # Старт активного режима выполняется в фоновом потоке, чтобы не блокировать
        # GUI-поток при инициализации (refresh подписок sing-box и проба узлов —
        # длительные сетевые операции). Окно приложения должно отобразиться сразу.
        self._startup_thread = threading.Thread(
            target=self._start_active_mode_background,
            daemon=True,
            name="mtproxy-start-active-mode",
        )
        self._startup_thread.start()


    @property
    def auth_config(self) -> TelegramAuthConfig:
        env_api_id = str(self.env_values.get("MTPROXY_TELEGRAM_API_ID") or os.environ.get("MTPROXY_TELEGRAM_API_ID") or "").strip()
        env_api_hash = _clean_api_hash(self.env_values.get("MTPROXY_TELEGRAM_API_HASH") or os.environ.get("MTPROXY_TELEGRAM_API_HASH") or "")
        config_api_id = _safe_int(self.config.telegram_api_id)
        config_api_hash = _clean_api_hash(self.config.telegram_api_hash)
        return TelegramAuthConfig(
            api_id=_safe_int(env_api_id or config_api_id or 0),
            api_hash=(env_api_hash or config_api_hash or ""),
            session_path=self.telegram_session_path,
            phone=self.config.telegram_phone.strip(),
        )

    def _build_tg_ws_server(self) -> TgWsProxyServer:
        cfg = TgWsProxyRuntimeConfig(
            host=self.config.tg_ws_host,
            port=int(self.config.tg_ws_port),
            secret=self.config.tg_ws_secret,
            dc_ip=list(self.config.tg_ws_dc_ip or []),
            buf_kb=int(self.config.tg_ws_buf_kb or 256),
            pool_size=int(self.config.tg_ws_pool_size or 4),
            cfproxy_enabled=bool(self.config.tg_ws_cfproxy_enabled),
            cfproxy_priority=bool(self.config.tg_ws_cfproxy_priority),
            cfproxy_user_domain=str(self.config.tg_ws_cfproxy_user_domain or ""),
            cfproxy_worker_domain=str(self.config.tg_ws_cfproxy_worker_domain or ""),
            force_test_dc=bool(self.config.tg_ws_force_test_dc),
            fake_tls_domain=str(self.config.tg_ws_fake_tls_domain or ""),
            proxy_protocol=bool(self.config.tg_ws_proxy_protocol),
        )
        return TgWsProxyServer(cfg, log_sink=self._log, event_sink=self._emit)

    def _build_xray_runtime(self) -> XrayCoreRuntime:
        cfg = XrayRuntimeConfig(
            subscription_urls=list(self.config.xray_subscription_urls or []),
            socks_host=self.config.xray_socks_host,
            socks_port=int(self.config.xray_socks_port),
            probe_workers=int(self.config.xray_probe_workers or 4),
            probe_timeout_sec=float(self.config.xray_probe_timeout_sec or 8.0),
            max_servers=int(self.config.xray_max_servers or 250),
            xray_binary_path=str(self.config.xray_binary_path or ""),
            sing_box_binary_path=str(self.config.sing_box_binary_path or ""),
            selection_strategy=str(self.config.balancer_strategy or "sticky_session"),
            manual_upstream_url=str(self.config.xray_manual_upstream_url or ""),
        )
        return XrayCoreRuntime(
            cfg,
            root_dir=self.install_dir,
            out_dir=self._out_dir_path(),
            log_sink=self._log,
            event_sink=self._emit,
        )

    @property
    def telegram_session_path(self) -> Path:
        session_name = Path(str(self.config.telegram_session_file or "telegram_user.sec")).name
        return (self.state_dir / session_name).resolve()

    def _user_list_roots(self) -> list[Path]:
        roots = [self.state_root]
        if self.state_root != self.install_dir:
            roots.append(self.install_dir)
        return roots

    def _out_dir_path(self) -> Path:
        return (self.state_root / self.config.out_dir).resolve()

    def _out_dir_candidates(self) -> list[Path]:
        dirs: list[Path] = []
        seen: set[str] = set()
        for root in self._user_list_roots():
            path = (root / self.config.out_dir).resolve()
            marker = str(path)
            if marker in seen:
                continue
            seen.add(marker)
            dirs.append(path)
        return dirs

    def _list_file_candidates(self, *names: str) -> list[Path]:
        paths: list[Path] = []
        seen: set[str] = set()
        for out_dir in self._out_dir_candidates():
            for name in names:
                path = out_dir / name
                marker = str(path)
                if marker in seen:
                    continue
                seen.add(marker)
                paths.append(path)
        return paths

    def daily_full_refresh_due_seconds(self) -> float | None:
        """Секунд до суточного полного сбора базы прокси.

        None — суточный сбор неприменим (режим не mtproxy_picker либо завершение).
        База — результаты последней полной проверки (файлы списков + tg-кеш).
        """
        if self._shutdown_requested or self.config.active_mode != "mtproxy_picker":
            return None
        base_mtime = self._daily_refresh_base_mtime()
        if base_mtime is None:
            return 0.0
        return max(0.0, DAILY_FULL_REFRESH_INTERVAL_SEC - (time.time() - base_mtime))

    def _daily_refresh_base_mtime(self) -> float | None:
        newest: float | None = None
        for path in self._list_file_candidates(
            FAST_LIST_FILE_NAME,
            TG_PARSED_FILE_NAME,
            REPORT_FILE_NAME,
            LIST_FILE_NAME,
            LEGACY_WORKING_FILE_NAME,
        ):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if newest is None or mtime > newest:
                newest = mtime
        return newest

    def shutdown(self) -> None:
        self._shutdown_requested = True
        self._refresh_in_progress.clear()
        self.live_probe_stop.set()
        with contextlib.suppress(Exception):
            self.stop_local_server()
        with contextlib.suppress(Exception):
            self.tg_ws_server.stop()
        with contextlib.suppress(Exception):
            self.xray_runtime.shutdown()
        if self.config.active_mode == "mtproxy_picker" and self.last_working:
            with contextlib.suppress(Exception):
                self._persist_current_mtproxy_lists()
        if self.live_probe_thread.is_alive():
            self.live_probe_thread.join(timeout=3.0)

    def save_config(self) -> None:
        payload = self._config_payload(self.config)
        self.config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self._save_persistent_telegram_auth(payload)

    def set_telegram_sources_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        current = self._normalize_config(self.config)
        if current.telegram_sources_enabled == enabled and current.thread_source_enabled == enabled:
            return
        current.telegram_sources_enabled = enabled
        current.thread_source_enabled = enabled
        self.config = current
        self.save_config()

    @staticmethod
    def _config_payload(config: AppConfig) -> dict[str, Any]:
        payload = asdict(config)
        payload["telegram_session_file"] = Path(str(payload.get("telegram_session_file") or "telegram_user.sec")).name
        payload.pop("fast_list_limit", None)
        return payload

    def _migrate_legacy_state(self) -> None:
        if self.state_root == self.install_dir:
            return
        self.state_root.mkdir(parents=True, exist_ok=True)
        for name in (CONFIG_FILE_NAME, LIST_DIR_NAME, LEGACY_OUT_DIR_NAME, "app_state"):
            source_path = self.install_dir / name
            target_path = self.state_root / name
            if not source_path.exists() or target_path.exists():
                continue
            with contextlib.suppress(Exception):
                if source_path.is_dir():
                    shutil.copytree(source_path, target_path)
                else:
                    shutil.copy2(source_path, target_path)

    def _migrate_legacy_telegram_session(self) -> None:
        session_name = Path(str(self.config.telegram_session_file or "telegram_user.sec")).name or "telegram_user.sec"
        target_session = self.state_dir / session_name
        target_key = self.state_dir / "session_key.bin"
        legacy_dirs = [
            self.install_dir / DATA_DIR_NAME,
            self.install_dir / "app_state",
            self.state_root / "app_state",
        ]
        legacy_session_names = {
            session_name,
            "telegram_user.sec",
            "telegram_user.session",
            "telegram_user",
        }
        for legacy_dir in legacy_dirs:
            if not legacy_dir.exists() or legacy_dir.resolve() == self.state_dir.resolve():
                continue
            for legacy_name in legacy_session_names:
                source_session = legacy_dir / legacy_name
                if not source_session.exists() or target_session.exists():
                    continue
                with contextlib.suppress(Exception):
                    shutil.copy2(source_session, target_session)
                    _hide_windows_path(target_session)
                break
            source_key = legacy_dir / "session_key.bin"
            if source_key.exists() and not target_key.exists():
                with contextlib.suppress(Exception):
                    shutil.copy2(source_key, target_key)
                    _hide_windows_path(target_key)
        _hide_windows_path(self.state_dir)

    @staticmethod
    def _normalize_config(config: AppConfig) -> AppConfig:
        normalized = AppConfig(**asdict(config))
        if str(normalized.active_mode or "").strip() not in APP_MODES:
            normalized.active_mode = "mtproxy_picker"
        if int(normalized.max_proxies or 0) <= 0:
            normalized.max_proxies = DEFAULT_MAX_PROXIES
        if int(normalized.fast_list_limit or 0) <= 0:
            normalized.fast_list_limit = DEFAULT_FAST_LIST_LIMIT
        if str(normalized.balancer_strategy or "").strip() not in BALANCER_STRATEGIES:
            normalized.balancer_strategy = "sticky_session"
        normalized.manual_upstream_url = str(normalized.manual_upstream_url or "").strip()
        normalized.xray_manual_upstream_url = str(normalized.xray_manual_upstream_url or "").strip()
        normalized.local_secret = str(normalized.local_secret or DEFAULT_LOCAL_SECRET).strip().lower()
        try:
            bytes.fromhex(normalized.local_secret[2:] if normalized.local_secret.startswith(("dd", "ee")) else normalized.local_secret)
        except ValueError:
            normalized.local_secret = DEFAULT_LOCAL_SECRET
        normalized.auto_start_local = True
        normalized.telegram_api_proxy_url = str(normalized.telegram_api_proxy_url or DEFAULT_TELEGRAM_API_PROXY_URL).strip()
        if normalized.telegram_api_proxy_url in {OLD_DEFAULT_TELEGRAM_API_PROXY_URL, OLD_DEFAULT_TELEGRAM_API_PROXY_URL_2}:
            normalized.telegram_api_proxy_url = DEFAULT_TELEGRAM_API_PROXY_URL
        normalized.telegram_session_file = Path(
            str(normalized.telegram_session_file or "telegram_user.sec")
        ).name or "telegram_user.sec"
        if int(normalized.telegram_source_max_age_days or 0) <= 0:
            normalized.telegram_source_max_age_days = DEFAULT_SOURCE_MAX_AGE_DAYS
        if int(normalized.telegram_source_max_messages or 0) <= 0:
            normalized.telegram_source_max_messages = DEFAULT_SOURCE_MAX_MESSAGES
        if int(normalized.telegram_source_max_proxies or 0) <= 0:
            normalized.telegram_source_max_proxies = DEFAULT_SOURCE_MAX_PROXIES
        if int(normalized.deep_media_top_n or 0) < 0:
            normalized.deep_media_top_n = DEFAULT_DEEP_MEDIA_TOP_N
        if not normalized.xray_subscription_urls:
            normalized.xray_subscription_urls = list(DEFAULT_XRAY_SUBSCRIPTIONS)
        else:
            for source in DEFAULT_XRAY_SUBSCRIPTIONS:
                if source not in normalized.xray_subscription_urls:
                    normalized.xray_subscription_urls.append(source)
        normalized.xray_socks_host = str(normalized.xray_socks_host or "127.0.0.1").strip() or "127.0.0.1"
        normalized.xray_socks_port = max(1, min(65535, int(normalized.xray_socks_port or 10808)))
        normalized.xray_probe_workers = max(1, int(normalized.xray_probe_workers or 4))
        normalized.xray_probe_timeout_sec = max(2.0, float(normalized.xray_probe_timeout_sec or 8.0))
        normalized.xray_max_servers = max(1, int(normalized.xray_max_servers or 250))
        normalized.tg_ws_host = str(normalized.tg_ws_host or "127.0.0.1").strip() or "127.0.0.1"
        normalized.tg_ws_port = max(1, min(65535, int(normalized.tg_ws_port or 1443)))
        normalized.tg_ws_secret = str(normalized.tg_ws_secret or DEFAULT_LOCAL_SECRET).strip().lower()
        try:
            bytes.fromhex(normalized.tg_ws_secret)
            if len(normalized.tg_ws_secret) != 32:
                raise ValueError
        except ValueError:
            normalized.tg_ws_secret = DEFAULT_LOCAL_SECRET
        if not normalized.tg_ws_dc_ip:
            normalized.tg_ws_dc_ip = ["2:149.154.167.220", "4:149.154.167.220"]
        normalized.tg_ws_buf_kb = max(4, int(normalized.tg_ws_buf_kb or 256))
        normalized.tg_ws_pool_size = max(0, int(normalized.tg_ws_pool_size or 4))
        normalized.tg_ws_cfproxy_user_domain = str(normalized.tg_ws_cfproxy_user_domain or "").strip()
        normalized.tg_ws_cfproxy_worker_domain = str(normalized.tg_ws_cfproxy_worker_domain or "").strip()
        normalized.tg_ws_force_test_dc = bool(normalized.tg_ws_force_test_dc)
        normalized.tg_ws_fake_tls_domain = str(normalized.tg_ws_fake_tls_domain or "").strip()
        return normalized

    @staticmethod
    def _local_server_signature(config: AppConfig) -> tuple[object, ...]:
        return (
            config.local_host,
            int(config.local_port),
            config.local_secret,
        )

    @staticmethod
    def _tg_ws_signature(config: AppConfig) -> tuple[object, ...]:
        return (
            config.tg_ws_host,
            int(config.tg_ws_port),
            config.tg_ws_secret,
            tuple(config.tg_ws_dc_ip or []),
            int(config.tg_ws_buf_kb),
            int(config.tg_ws_pool_size),
            bool(config.tg_ws_cfproxy_enabled),
            bool(config.tg_ws_cfproxy_priority),
            config.tg_ws_cfproxy_user_domain,
            config.tg_ws_cfproxy_worker_domain,
            bool(config.tg_ws_force_test_dc),
            config.tg_ws_fake_tls_domain,
            bool(config.tg_ws_proxy_protocol),
        )

    @staticmethod
    def _xray_signature(config: AppConfig) -> tuple[object, ...]:
        return (
            tuple(config.xray_subscription_urls or []),
            config.xray_socks_host,
            int(config.xray_socks_port),
            int(config.xray_probe_workers),
            float(config.xray_probe_timeout_sec),
            int(config.xray_max_servers),
            config.xray_binary_path,
            config.sing_box_binary_path,
        )

    def apply_config(self, config: AppConfig) -> bool:
        normalized = self._normalize_config(config)
        current = self._normalize_config(self.config)
        if normalized == current:
            self.config = normalized
            self._apply_manual_override_from_config()
            return False

        restart_local_server = self._local_server_signature(normalized) != self._local_server_signature(current)
        restart_tg_ws = self._tg_ws_signature(normalized) != self._tg_ws_signature(current)
        restart_xray = self._xray_signature(normalized) != self._xray_signature(current)
        active_mode_changed = normalized.active_mode != current.active_mode
        xray_selection_changed = (
            normalized.balancer_strategy != current.balancer_strategy
            or normalized.xray_manual_upstream_url != current.xray_manual_upstream_url
        )
        restart_active_runtime = active_mode_changed
        self.config = normalized
        self.save_config()
        was_running = self.local_server.is_running()
        if restart_local_server:
            if was_running:
                self.stop_local_server()
            self.local_server = LocalMTProxyServer(
                self.pool,
                host=self.config.local_host,
                port=self.config.local_port,
                secret=self.config.local_secret,
                selection_strategy=self.config.balancer_strategy,
                log_sink=self._log,
                event_sink=self._emit,
            )
            if was_running and self.config.active_mode == "mtproxy_picker":
                restart_active_runtime = True
        else:
            self.local_server.set_selection_strategy(self.config.balancer_strategy)
        if restart_tg_ws:
            was_tg_ws = self.tg_ws_server.is_running()
            self.tg_ws_server.stop()
            self.tg_ws_server = self._build_tg_ws_server()
            if was_tg_ws and self.config.active_mode == "tg_ws_proxy":
                restart_active_runtime = True
        if restart_xray:
            was_xray = self.xray_runtime.is_running()
            self.xray_runtime.stop()
            self.xray_runtime = self._build_xray_runtime()
            if was_xray and self.config.active_mode == "xray_core":
                restart_active_runtime = True
        else:
            try:
                self.xray_runtime.update_selection(
                    self.config.balancer_strategy,
                    self.config.xray_manual_upstream_url,
                    restart=False,
                )
            except ValueError:
                self.config.xray_manual_upstream_url = ""
                self.xray_runtime.update_selection(self.config.balancer_strategy, "", restart=False)
                self.save_config()
            if xray_selection_changed and self.config.active_mode == "xray_core" and self.xray_runtime.is_running():
                restart_active_runtime = True
        self._apply_manual_override_from_config()
        if restart_active_runtime:
            self.start_active_mode()
        return True

    def stop_all_modes(self) -> None:
        with contextlib.suppress(Exception):
            self.stop_local_server()
        with contextlib.suppress(Exception):
            self.tg_ws_server.stop()
        with contextlib.suppress(Exception):
            self.xray_runtime.stop()

    def stop_active_mode(self) -> None:
        if self.config.active_mode == "tg_ws_proxy":
            self.tg_ws_server.stop()
        elif self.config.active_mode == "xray_core":
            self.xray_runtime.stop()
        else:
            self.stop_local_server()

    def _start_active_mode_background(self) -> None:
        """Фоновый старт активного режима при инициализации.

        Вызывается в отдельном daemon-потоке, чтобы длительные сетевые операции
        (refresh подписок sing-box, проба узлов) не блокировали GUI-поток.
        """
        try:
            self.start_active_mode(initial=True)
        except Exception as exc:
            self._log(f"[mode] background start failed: {exc}")

    def start_active_mode(self, *, initial: bool = False) -> bool:
        if self._shutdown_requested:
            return False
        mode = self.config.active_mode if self.config.active_mode in APP_MODES else "mtproxy_picker"
        self.stop_all_modes()


        if mode == "tg_ws_proxy":
            self.tg_ws_server.start()
            return self.tg_ws_server.is_running()
        if mode == "xray_core":
            self._refresh_xray_before_start()
            return self.xray_runtime.start()

        if self.pool.count() > 0:
            return self.start_local_server(
                raise_on_verify_failure=False,
                pre_probe=not initial,
                verify=not initial,
            )
        self._log("[mode] mtproxy picker waiting for working pool")
        return False

    def restart_active_mode(self) -> bool:
        if self._shutdown_requested:
            return False
        mode = self.config.active_mode
        self.stop_all_modes()
        if mode == "tg_ws_proxy":
            self.tg_ws_server.start()
            return self.tg_ws_server.is_running()
        if mode == "xray_core":
            self._refresh_xray_before_start()
            return self.xray_runtime.start()
        return self.start_active_mode()

    def _refresh_xray_before_start(self, cancel_event: threading.Event | None = None) -> None:
        self._refresh_in_progress.set()
        self.last_refresh_started_at = time.time()
        try:
            self._log("[mode] refreshing sing-box subscriptions before start")
            self.xray_runtime.refresh(cancel_event=cancel_event)
            self.last_refresh_finished_at = time.time()
        finally:
            self._refresh_in_progress.clear()

    def set_active_mode(self, mode: str) -> None:
        if self._shutdown_requested:
            return
        if mode not in APP_MODES:
            raise ValueError(f"Unknown mode: {mode}")
        if self.config.active_mode == mode:
            self.start_active_mode()
            return
        self.config.active_mode = mode
        self.save_config()
        self.start_active_mode()

    def refresh_active_mode(self, cancel_event: threading.Event | None = None, *, manual: bool = True, fast: bool = False) -> None:
        if self._shutdown_requested:
            return
        if self.config.active_mode == "xray_core":
            if fast:
                self.quick_sort_active_mode(cancel_event=cancel_event)
                return
            self._refresh_in_progress.set()
            self.last_refresh_started_at = time.time()
            try:
                self.xray_runtime.refresh(cancel_event=cancel_event)
                self.last_refresh_finished_at = time.time()
            finally:
                self._refresh_in_progress.clear()
            return
        if self.config.active_mode == "tg_ws_proxy":
            self.restart_active_mode()
            return
        self.run_refresh(cancel_event=cancel_event, manual=manual, fast=fast)

    def quick_sort_active_mode(self, cancel_event: threading.Event | None = None) -> int:
        if self.config.active_mode == "xray_core":
            self._refresh_in_progress.set()
            try:
                return self.xray_runtime.quick_sort_by_ping(cancel_event=cancel_event)
            finally:
                self._refresh_in_progress.clear()
        if self.config.active_mode == "tg_ws_proxy":
            self.restart_active_mode()
            return 1 if self.tg_ws_server.is_running() else 0
        return self.quick_probe_pool(limit=self.config.live_probe_top_n, reason="manual")

    def start_local_server(
        self,
        *,
        raise_on_verify_failure: bool = True,
        pre_probe: bool = True,
        verify: bool = True,
    ) -> bool:
        if self.pool.count() <= 0:
            self._log("[local] start skipped: no working proxies")
            return False
        try:
            if pre_probe:
                self.quick_probe_pool(limit=min(self.config.live_probe_top_n, max(4, self.pool.count())), reason="startup")
            self.local_server.start()
            if verify:
                self._verify_local_server()
            return True
        except Exception as exc:
            self.local_server.stop()
            self._log(f"[local] start self-test failed: {exc}")
            self._emit(
                "local_server_state",
                running=False,
                host=self.config.local_host,
                port=self.config.local_port,
                error=str(exc),
            )
            if raise_on_verify_failure:
                raise
            return False

    def stop_local_server(self) -> None:
        self.local_server.stop()

    def _restart_local_server_if_running(self, *, reason: str) -> None:
        if not self.local_server.is_running():
            return
        self._log(f"[local] restarting frontend ({reason})")
        self.local_server.stop()
        self.local_server.start()
        self._verify_local_server()

    def _verify_local_server(self) -> None:
        local_proxy = parse_proxy_link(self.local_server.local_proxy_url, "local", "local")
        if local_proxy is None:
            raise RuntimeError("local_proxy_url_invalid")
        settings = ProbeSettings(
            duration=5.0,
            interval=0.8,
            timeout=max(5.0, min(float(self.config.timeout or 8.0), 8.0)),
            max_latency_ms=max(2000.0, float(self.config.max_latency_ms or 0) * 4.0),
            min_success_rate=0.25,
            max_high_latency_ratio=1.0,
            high_latency_streak=max(5, int(self.config.high_latency_streak or 0)),
            unreachable_failures=3,
        )
        failures: list[str] = []
        for attempt in range(1, 3):
            outcome = run_async(
                probe_all(
                    proxies=[local_proxy],
                    settings=settings,
                    concurrency=1,
                    verbose=False,
                    log_sink=self._log,
                    event_sink=None,
                )
            )[0]
            if outcome.accepted:
                suffix = f" attempt={attempt}" if attempt > 1 else ""
                self._log(
                    f"[local] self-test ok {outcome.successes}/{outcome.attempts} "
                    f"avg={int(round(outcome.avg_latency_ms or 0))}ms{suffix}"
                )
                return
            failures.append(f"{outcome.reason}:{outcome.successes}/{outcome.attempts}")
            time.sleep(0.35)
        raise RuntimeError(f"local_proxy_self_test_failed:{';'.join(failures)}")

    def _active_media_transfer_pressure(self) -> dict[str, int]:
        pressure = self.pool.media_pressure()
        return {
            "active_media": int(pressure.get("active_media", 0)),
            "active_heavy": int(pressure.get("active_heavy", 0)),
            "recent_media": int(pressure.get("recent_media", 0)),
        }

    def _wait_for_media_idle(
        self,
        *,
        reason: str,
        cancel_event: threading.Event | None = None,
        max_wait_seconds: float | None = None,
    ) -> bool:
        waiting_announced = False
        started_at = time.monotonic()
        while self.local_server.is_running():
            pressure = self._active_media_transfer_pressure()
            if pressure["active_media"] <= 0 and pressure["active_heavy"] <= 0:
                if waiting_announced:
                    self._emit("runtime_refresh_resumed", reason=reason)
                return True
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("refresh_cancelled")
            if not waiting_announced:
                self._log(
                    f"[runtime] waiting for media session to finish before {reason} "
                    f"(active_media={pressure['active_media']} active_heavy={pressure['active_heavy']})"
                )
                self._emit("runtime_refresh_waiting", reason=reason, **pressure)
                waiting_announced = True
            if max_wait_seconds is not None and (time.monotonic() - started_at) >= max_wait_seconds:
                self._log(
                    f"[runtime] media wait timed out before {reason}; continuing refresh "
                    f"(active_media={pressure['active_media']} active_heavy={pressure['active_heavy']})"
                )
                self._emit("runtime_refresh_wait_timeout", reason=reason, **pressure)
                return False
            time.sleep(0.5)
        return True

    @staticmethod
    def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("refresh_cancelled")

    def run_refresh(self, *, cancel_event: threading.Event | None = None, manual: bool = True, fast: bool = False) -> None:
        self._refresh_in_progress.set()
        previous_working = list(self.last_working)
        try:
            self.last_refresh_started_at = time.time()
            self.thread_status = "disabled"
            self.thread_proxy_count = 0
            self._latest_deep_media_scores = {}
            config = CollectorConfig(
                sources=list(self.config.sources),
                out_dir=self._out_dir_path(),
                duration=min(6.0, float(self.config.duration or 35.0)) if fast else self.config.duration,
                interval=1.5 if fast else self.config.interval,
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

            if fast:
                self._log("[runtime] fast refresh: fetching list sources and rechecking cached pool")
            else:
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

            known_proxies = [
                proxy
                for proxy in self._load_known_working_proxy_records()
                if proxy.key not in known_keys
            ]
            if not known_proxies and fast:
                self._log("[runtime] fast refresh: cached pool is empty (sources still fetched)")
            if known_proxies:
                self._log(
                    f"[runtime] rechecking {len(known_proxies)} known proxies from existing lists"
                    + (" (fast)" if fast else "")
                )
                known_outcomes = run_async(
                    probe_all(
                        proxies=known_proxies,
                        settings=self._known_proxy_probe_settings(fast=fast),
                        concurrency=max(1, min(max(self.config.workers, 32), 48)),
                        verbose=False,
                        log_sink=self._log,
                        event_sink=None,
                        cancel_event=cancel_event,
                    )
                )
                combined_outcomes.extend(known_outcomes)
                known_keys.update(item.proxy.key for item in known_outcomes)
                known_working = [item for item in known_outcomes if item.accepted]
                self._log(
                    f"[runtime] known-list recheck complete: "
                    f"{len(known_working)} working / {len(known_outcomes)}"
                )
                if best_upstream is None and known_working:
                    best_upstream = next((item.proxy for item in sorted(known_working, key=self._working_priority_key)), None)

            telegram_sources = self._collect_enabled_telegram_sources()
            if telegram_sources and fast:
                cached_thread_proxies = self._load_tg_parsed_proxy_records()
                self.thread_status = "cached" if cached_thread_proxies else "skipped:fast"
                self.thread_proxy_count = len(cached_thread_proxies)
                self._log(
                    f"[telegram] fast refresh: using cached parsed proxies "
                    f"({len(cached_thread_proxies)}); skipping live parse"
                )
                telegram_sources = []
            if telegram_sources:
                should_parse_telegram = self._telegram_source_cache_is_stale()
                if not should_parse_telegram:
                    self.thread_status = "cached"
                    self.thread_proxy_count = len(self._load_tg_parsed_proxy_records())
                    self._log(
                        f"[telegram] using cached parsed proxies: {self.thread_proxy_count}; "
                        f"current parse slot={self._telegram_source_current_slot_label()}"
                    )
                else:
                    try:
                        if best_upstream is not None and self.config.active_mode == "mtproxy_picker" and not self.local_server.is_running():
                            self.start_local_server(raise_on_verify_failure=False, pre_probe=False, verify=False)
                        preferred_for_telegram = self._configured_telegram_api_proxy() or best_upstream
                        with self.telegram_lock:
                            thread_proxies = self._run_telegram_api_call(
                                "telegram-sources",
                                lambda upstream: collect_telegram_sources_proxies(
                                    telegram_sources,
                                    self.auth_config,
                                    upstream_proxy=upstream,
                                    log_sink=self._log,
                                    event_sink=self._emit,
                                    total_timeout=max(45.0, float(self.config.fetch_timeout) * 4.0),
                                    request_timeout=max(8.0, float(self.config.fetch_timeout)),
                                    max_messages=int(self.config.telegram_source_max_messages or DEFAULT_SOURCE_MAX_MESSAGES),
                                    max_proxies=int(self.config.telegram_source_max_proxies or DEFAULT_SOURCE_MAX_PROXIES),
                                    max_age_days=int(self.config.telegram_source_max_age_days or DEFAULT_SOURCE_MAX_AGE_DAYS),
                                    cancel_event=cancel_event,
                                ),
                                preferred=preferred_for_telegram,
                                include_direct=True,
                                max_pool_candidates=10,
                                attempts_per_proxy=2,
                            )
                        if not thread_proxies:
                            cached_thread_proxies = self._load_tg_parsed_proxy_records()
                            if cached_thread_proxies:
                                self._log(
                                    f"[telegram] parse returned 0 proxies; keeping cached parsed proxies: "
                                    f"{len(cached_thread_proxies)}"
                                )
                                thread_proxies = cached_thread_proxies
                            else:
                                self._log("[telegram] parse returned 0 proxies; cache file was not updated")
                        else:
                            self._save_tg_parsed_proxy_records(thread_proxies)
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
            else:
                self.thread_status = "disabled"
            self._raise_if_cancelled(cancel_event)

            combined_working = sorted((item for item in combined_outcomes if item.accepted), key=outcome_sort_key)
            combined_rejected = sorted(
                (item for item in combined_outcomes if not item.accepted),
                key=lambda item: (item.reason, outcome_sort_key(item)),
            )
            basic_working_count = len(combined_working)

            if (self.config.deep_media_enabled or self.config.rf_whitelist_check_enabled) and combined_working:
                combined_working, combined_rejected = self._run_deep_media_checks(
                    combined_working,
                    combined_rejected,
                    strict=self.config.rf_whitelist_check_enabled,
                    cancel_event=cancel_event,
                )
            media_filtered_count = max(0, basic_working_count - len(combined_working))
            final_by_key = {item.proxy.key: item for item in combined_outcomes}
            for item in combined_rejected:
                final_by_key[item.proxy.key] = item
            for item in combined_working:
                final_by_key[item.proxy.key] = item
            combined_outcomes = list(final_by_key.values())
            self._raise_if_cancelled(cancel_event)
            fresh_working_count = len(combined_working)
            kept_previous = False
            if not combined_working and previous_working:
                combined_working = list(previous_working)
                kept_previous = True
                self._log(
                    f"[runtime] refresh accepted 0 proxies; keeping previous working pool ({len(combined_working)})"
                )
            self.last_result = base_result
            self.last_outcomes = combined_outcomes
            self.last_working = combined_working
            self.last_rejected = combined_rejected
            self._wait_for_media_idle(reason="apply_results", cancel_event=cancel_event, max_wait_seconds=2.0)
            self.pool.replace_outcomes(combined_working)
            self._apply_manual_override_from_config()
            self._apply_latest_deep_media_scores()

            self._raise_if_cancelled(cancel_event)
            self._export_combined_results(base_result, combined_outcomes, combined_working, combined_rejected)
            self.last_refresh_finished_at = time.time()
            unique_count = len({item.proxy.key for item in combined_outcomes})
            self.last_refresh_stats = {
                "working": len(combined_working),
                "fresh_working": fresh_working_count,
                "basic_working": basic_working_count,
                "media_filtered": media_filtered_count,
                "unique": unique_count,
                "rejected": len(combined_rejected),
                "kept_previous": int(kept_previous),
                "fast": int(fast),
            }

            self._raise_if_cancelled(cancel_event)
            if self.config.active_mode == "mtproxy_picker" and combined_working:
                self.start_local_server(raise_on_verify_failure=False)

            self._emit(
                "runtime_refresh_complete",
                working=len(combined_working),
                fresh_working=fresh_working_count,
                basic_working=basic_working_count,
                media_filtered=media_filtered_count,
                rejected=len(combined_rejected),
                unique=unique_count,
                kept_previous=kept_previous,
            )
        finally:
            self._refresh_in_progress.clear()

    def run_auth_status(self) -> dict[str, Any]:
        cfg = self.auth_config
        # FIX: не вызываем API если credentials не настроены — иначе при старте
        # приложения показывается ошибка "telegram_api_credentials_missing".
        if not auth_is_configured(cfg):
            session_path = self.telegram_session_path
            return {
                "authorized": False,
                "display": "",
                "phone": "",
                "session_exists": session_path.exists(),
                "credentials_configured": False,
                "reason": "credentials_missing",
            }
        with self.telegram_lock:
            result = self._run_telegram_api_call(
                "auth-status",
                lambda upstream: get_auth_status(cfg, upstream_proxy=upstream),
                include_pool=False,
            )
        result["credentials_configured"] = True
        if result.get("session_exists") and not result.get("authorized"):
            result["reason"] = "session_not_authorized"
        return result

    def request_auth_code(self, phone: str, *, resend: bool = False) -> dict[str, Any]:
        normalized_phone = normalize_telegram_phone(phone)
        previous_hash = self._auth_code_hash if resend and normalized_phone == self._auth_code_phone else ""
        if not previous_hash:
            self._auth_code_hash = ""
        self._auth_code_phone = normalized_phone
        with self.telegram_lock:
            result = self._run_telegram_api_call(
                "request-code",
                lambda upstream: request_login_code(
                    self.auth_config,
                    phone=normalized_phone,
                    resend_code_hash=previous_hash,
                    reset_unauthorized_session=not bool(previous_hash),
                    upstream_proxy=upstream,
                ),
                include_pool=False,
            )
        self._log(
            "[telegram-api] code request accepted: "
            f"phone={result.get('phone') or normalized_phone} "
            f"request_phone={result.get('request_phone') or '-'} "
            f"type={result.get('type') or '-'} "
            f"type_details={result.get('type_details') or '-'} "
            f"next={result.get('next_type') or '-'} "
            f"next_details={result.get('next_type_details') or '-'} "
            f"timeout={result.get('timeout') or 0} "
            f"hash_present={bool(result.get('phone_code_hash_present'))} "
            f"resend={bool(result.get('resend'))}"
        )
        self._auth_code_hash = result.get("phone_code_hash", "")
        self._auth_code_phone = str(result.get("phone") or normalized_phone)
        return result

    def complete_auth(self, phone: str, code: str, password: str = "") -> dict[str, Any]:
        if not self._auth_code_hash:
            raise RuntimeError("phone_code_hash_missing")
        normalized_phone = normalize_telegram_phone(phone)
        if self._auth_code_phone and normalized_phone != self._auth_code_phone:
            raise RuntimeError("Запросите новый код для текущего номера телефона.")
        with self.telegram_lock:
            try:
                result = self._run_telegram_api_call(
                    "complete-login",
                    lambda upstream: complete_login(
                        self.auth_config,
                        phone=normalized_phone,
                        code=code,
                        phone_code_hash=self._auth_code_hash,
                        password=password,
                        upstream_proxy=upstream,
                    ),
                    include_pool=False,
                )
            except RuntimeError as exc:
                text = str(exc)
                if (
                    "Код подтверждения истек" in text
                    or "hash запроса кода" in text
                    or "активный запрос кода" in text
                ):
                    self._auth_code_hash = ""
                    self._auth_code_phone = ""
                raise
        if result.get("authorized"):
            self._auth_code_hash = ""
            self._auth_code_phone = ""
        return result

    def logout_auth(self) -> None:
        with self.telegram_lock:
            self._run_telegram_api_call(
                "logout",
                lambda upstream: logout(self.auth_config, upstream_proxy=upstream),
                include_pool=False,
            )

    def send_working_proxies_to_saved_messages(self) -> dict[str, Any]:
        urls = [item.proxy.url for item in self.last_working[:SAVED_MESSAGES_EXPORT_LIMIT]]
        with self.telegram_lock:
            return self._run_telegram_api_call(
                "send-proxy-list",
                lambda upstream: send_proxy_list_to_saved_messages(
                    self.auth_config,
                    urls,
                    upstream_proxy=upstream,
                ),
            )

    def snapshot(self) -> dict[str, Any]:
        if self.config.active_mode == "xray_core":
            snapshot = self.xray_runtime.snapshot()
            snapshot.update(
                {
                    "active_mode": self.config.active_mode,
                    "thread_status": self.thread_status,
                    "thread_proxy_count": self.thread_proxy_count,
                    "background_refreshing": self._refresh_in_progress.is_set(),
                    "exports": {
                        "xray_working": str((self._out_dir_path() / "xray_working.json").resolve()),
                        "xray_rejected": str((self._out_dir_path() / "xray_rejected.json").resolve()),
                    },
                }
            )
            return snapshot
        if self.config.active_mode == "tg_ws_proxy":
            running = self.tg_ws_server.is_running()
            return {
                "mode": "tg_ws_proxy",
                "active_mode": self.config.active_mode,
                "running": running,
                "local_running": running,
                "local_url": self.tg_ws_server.local_proxy_url,
                "local_tg_url": self.tg_ws_server.local_tg_url,
                "endpoint": self.tg_ws_server.config.endpoint,
                "best_proxy": "",
                "status_text": "Локальный прокси активен" if running else (self.tg_ws_server.last_error or "Локальный прокси остановлен"),
                "pool_rows": [],
                "working_count": 0,
                "rejected_count": 0,
                "unique_count": 0,
                "bytes_up": int(getattr(tg_ws_stats, "bytes_up", 0) or 0),
                "bytes_down": int(getattr(tg_ws_stats, "bytes_down", 0) or 0),
                "connections_active": int(getattr(tg_ws_stats, "connections_active", 0) or 0),
                "ping_ms": float(getattr(tg_ws_stats, "ping_ms", 0.0) or 0.0),

                "balancer_strategy": self.config.balancer_strategy,
                "manual_upstream_url": "",
                "telegram_api_proxy_url": self.config.telegram_api_proxy_url,
                "last_refresh_started_at": self.last_refresh_started_at,
                "last_refresh_finished_at": self.last_refresh_finished_at,
                "exports": {},
                "seed_source": self.seed_source,
                "seed_loaded_at": self.seed_loaded_at,
                "thread_status": self.thread_status,
                "thread_proxy_count": self.thread_proxy_count,
            }
        working_rows = self.pool.snapshot()
        current_best = self.pool.best()
        health_report = self.pool.get_health_report()
        return {
            "mode": "mtproxy_picker",
            "active_mode": self.config.active_mode,
            "running": self.local_server.is_running(),
            "endpoint": f"{self.config.local_host}:{self.config.local_port}",
            "status_text": "Local MTProto proxy active" if self.local_server.is_running() else "Local MTProto proxy stopped",
            "working_count": len(self.last_working),
            "rejected_count": len(self.last_rejected),
            "unique_count": len({item.proxy.key for item in self.last_outcomes}),
            "pool_rows": working_rows,
            "health_report": health_report,
            "leaderboard": health_report[: DEFAULT_FAST_LIST_LIMIT],
            "local_running": self.local_server.is_running(),
            "local_url": self.local_server.local_proxy_url,
            "local_tg_url": self.local_server.local_proxy_tg_url,
            "best_proxy": current_best.proxy.url if current_best is not None else "",
            "balancer_strategy": self.config.balancer_strategy,
            "manual_upstream_url": self.config.manual_upstream_url,
            "telegram_api_proxy_url": self.config.telegram_api_proxy_url,
            "last_refresh_started_at": self.last_refresh_started_at,
            "last_refresh_finished_at": self.last_refresh_finished_at,
            "last_refresh_stats": dict(self.last_refresh_stats),
            "background_refreshing": self._refresh_in_progress.is_set(),
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

    def _known_proxy_probe_settings(self, *, fast: bool = False) -> ProbeSettings:
        if fast:
            return ProbeSettings(
                duration=4.0,
                interval=0.8,
                timeout=max(3.0, min(4.0, float(self.config.timeout or 8.0))),
                max_latency_ms=max(1500.0, float(self.config.max_latency_ms or 300.0) * 5.0),
                min_success_rate=0.25,
                max_high_latency_ratio=1.0,
                high_latency_streak=6,
                unreachable_failures=2,
            )
        return ProbeSettings(
            duration=max(5.0, min(9.0, float(self.config.timeout or 8.0) + 1.0)),
            interval=1.0,
            timeout=max(4.0, min(6.0, float(self.config.timeout or 8.0))),
            max_latency_ms=max(1500.0, float(self.config.max_latency_ms or 300.0) * 5.0),
            min_success_rate=0.25,
            max_high_latency_ratio=1.0,
            high_latency_streak=6,
            unreachable_failures=2,
        )

    def _import_probe_settings(self) -> ProbeSettings:
        return ProbeSettings(
            duration=max(6.0, min(12.0, float(self.config.duration or 35.0))),
            interval=1.0,
            timeout=max(4.0, min(8.0, float(self.config.timeout or 8.0))),
            max_latency_ms=max(450.0, float(self.config.max_latency_ms or 300.0) * 1.5),
            min_success_rate=0.5,
            max_high_latency_ratio=1.0,
            high_latency_streak=max(3, int(self.config.high_latency_streak or 3)),
            unreachable_failures=2,
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
        if self.local_server.is_running():
            idle_reached = self._wait_for_media_idle(
                reason="deep_media_check",
                cancel_event=cancel_event,
                max_wait_seconds=20.0,
            )
            if not idle_reached:
                self._log("[media] skipped deep media check during active Telegram traffic")
                return working, rejected
        with self.telegram_lock:
            auth_status = self._run_telegram_api_call(
                "media-auth-status",
                lambda upstream: get_auth_status(self.auth_config, upstream_proxy=upstream),
                include_pool=False,
            )
        if not auth_status.get("authorized"):
            reason = "rf_whitelist" if strict else "deep_media"
            self._log(f"[media] skipped: telegram_session_not_authorized ({reason})")
            self._emit("telegram_auth_required", feature=reason)
            return working, rejected
        configured_limit = int(self.config.deep_media_top_n or 0)
        candidate_limit = len(working) if configured_limit <= 0 else max(1, configured_limit)
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
            self._latest_deep_media_scores[result.proxy_key] = result
            self.pool.update_deep_media_score(
                result.proxy_key,
                result.score,
                result.note,
                upload_kbps=result.upload_kbps,
                download_kbps=result.download_kbps,
                aux_kbps=result.aux_kbps,
            )
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
            try:
                if self.config.active_mode == "xray_core":
                    self._run_xray_health_cycle()
                    continue
                if self.pool.count() <= 0:
                    continue
                self._run_background_health_cycle()
            except Exception as exc:
                self._log(f"[live] probe loop error: {exc}")

    def _run_xray_health_cycle(self) -> None:
        if self._shutdown_requested:
            return
        if self.config.active_mode != "xray_core":
            return
        if self._refresh_in_progress.is_set():
            return
        now = time.time()
        if (now - self._last_xray_health_at) < XRAY_HEALTH_INTERVAL_SEC:
            return
        self._last_xray_health_at = now
        if not self._health_cycle_lock.acquire(blocking=False):
            return
        try:
            if self._refresh_in_progress.is_set() or self.config.active_mode != "xray_core":
                return
            if not self.xray_runtime.is_running():
                if self.xray_runtime.active_result is not None and (now - self._xray_restart_attempted_at) >= 20.0:
                    self._xray_restart_attempted_at = now
                    self._log("[sing-box] core is not running, restarting active node")
                    if self._shutdown_requested:
                        return
                    self.xray_runtime.start()
                return
            latency = self.xray_runtime.probe_active_latency(timeout=min(5.0, float(self.config.xray_probe_timeout_sec or 8.0)))
            if latency is None:
                self._xray_health_fail_streak += 1
                self._xray_high_latency_streak = 0
                self._xray_full_refresh_candidate_since = 0.0
                self._log(
                    "[sing-box] active node health probe failed, "
                    f"streak={self._xray_health_fail_streak}/{XRAY_HEALTH_FAIL_RESTART_STREAK}"
                )
                self._emit(
                    "xray_health_degraded",
                    reason="health_failed",
                    fail_streak=self._xray_health_fail_streak,
                    required_streak=XRAY_HEALTH_FAIL_RESTART_STREAK,
                )
                if (
                    self._xray_health_fail_streak >= XRAY_HEALTH_FAIL_RESTART_STREAK
                    and (now - self._xray_restart_attempted_at) >= XRAY_QUICK_SWITCH_COOLDOWN_SEC
                ):
                    self._xray_restart_attempted_at = now
                    self._emit("xray_core_restart_started", fail_streak=self._xray_health_fail_streak)
                    self._log("[sing-box] active tunnel is unhealthy, restarting local core")
                    self.xray_runtime.restart()
                    self._xray_health_fail_streak = 0
                return
            self._xray_health_fail_streak = 0
            self._emit(
                "xray_health",
                latency_ms=latency,
                threshold_ms=XRAY_QUICK_SWITCH_LATENCY_MS,
                full_threshold_ms=XRAY_FULL_REFRESH_LATENCY_MS,
            )
            if (now - self._last_xray_speed_sample_at) >= XRAY_SPEED_SAMPLE_INTERVAL_SEC:
                self._last_xray_speed_sample_at = now
                speed = self.xray_runtime.probe_active_download_speed(
                    timeout=max(6.0, float(self.config.xray_probe_timeout_sec or 8.0))
                )
                if speed is not None:
                    self._emit("xray_speed_sample", download_kbps=speed)
            if latency < XRAY_QUICK_SWITCH_LATENCY_MS:
                self._xray_high_latency_streak = 0
                self._xray_full_refresh_candidate_since = 0.0
                return
            self._xray_high_latency_streak += 1
            full_latency_candidate = latency >= XRAY_FULL_REFRESH_LATENCY_MS
            held_for_sec = 0.0
            if full_latency_candidate:
                if self._xray_full_refresh_candidate_since <= 0:
                    self._xray_full_refresh_candidate_since = now
                held_for_sec = max(0.0, now - self._xray_full_refresh_candidate_since)
            else:
                self._xray_full_refresh_candidate_since = 0.0
            required_streak = XRAY_FULL_REFRESH_CONFIRM_STREAK if full_latency_candidate else XRAY_QUICK_SWITCH_CONFIRM_STREAK
            required_sec = XRAY_FULL_REFRESH_CONFIRM_SEC if full_latency_candidate else 0.0
            self._log(
                f"[sing-box] high latency {latency:.0f} ms, "
                f"streak={self._xray_high_latency_streak}/{required_streak}, "
                f"held={held_for_sec:.0f}/{required_sec:.0f}s"
            )
            self._emit(
                "xray_high_latency_observed",
                latency_ms=latency,
                threshold_ms=XRAY_FULL_REFRESH_LATENCY_MS if full_latency_candidate else XRAY_QUICK_SWITCH_LATENCY_MS,
                streak=self._xray_high_latency_streak,
                required_streak=required_streak,
                held_for_sec=held_for_sec,
                required_sec=required_sec,
            )
            if self._xray_high_latency_streak < XRAY_QUICK_SWITCH_CONFIRM_STREAK:
                return
            best_cached_latency = self._run_xray_quick_sort_if_due(now, latency=latency)
            if best_cached_latency is not None and best_cached_latency < XRAY_FULL_REFRESH_LATENCY_MS:
                self._xray_full_refresh_candidate_since = 0.0
                self._xray_high_latency_streak = 0
                return
            if (
                full_latency_candidate
                and self._xray_high_latency_streak >= XRAY_FULL_REFRESH_CONFIRM_STREAK
                and held_for_sec >= XRAY_FULL_REFRESH_CONFIRM_SEC
                and (best_cached_latency is None or best_cached_latency >= XRAY_FULL_REFRESH_LATENCY_MS)
            ):
                self._run_xray_full_refresh_if_due(
                    now,
                    reason=f"latency_{latency:.0f}ms",
                    latency=latency,
                    streak=self._xray_high_latency_streak,
                    held_for_sec=held_for_sec,
                )
        finally:
            self._health_cycle_lock.release()

    def _best_cached_xray_latency(self) -> float | None:
        latencies: list[float] = []
        for item in getattr(self.xray_runtime, "last_working", []) or []:
            value = getattr(item, "latency_ms", None)
            if value is None:
                continue
            with contextlib.suppress(TypeError, ValueError):
                if float(value) > 0:
                    latencies.append(float(value))
        return min(latencies) if latencies else None

    def _run_xray_quick_sort_if_due(self, now: float, *, latency: float) -> float | None:
        if self._shutdown_requested:
            return self._best_cached_xray_latency()
        if (now - self._last_xray_quick_sort_at) < XRAY_QUICK_SWITCH_COOLDOWN_SEC:
            return self._best_cached_xray_latency()
        if not self.xray_runtime.last_working:
            self._run_xray_full_refresh_if_due(now, reason="no_cached_nodes")
            return None
        self._last_xray_quick_sort_at = now
        self._emit(
            "xray_background_quick_sort_started",
            latency_ms=latency,
            threshold_ms=XRAY_QUICK_SWITCH_LATENCY_MS,
        )
        self._refresh_in_progress.set()
        self.last_refresh_started_at = time.time()
        try:
            self._log(f"[sing-box] background quick ping-sort started ({latency:.0f} ms)")
            self.xray_runtime.quick_sort_by_ping(cancel_event=self.live_probe_stop)
        except Exception as exc:
            self._log(f"[sing-box] background quick ping-sort failed: {exc}")
            self._emit("xray_background_refresh_failed", error=str(exc))
        finally:
            self.last_refresh_finished_at = time.time()
            self._refresh_in_progress.clear()
        return self._best_cached_xray_latency()

    def _run_xray_full_refresh_if_due(
        self,
        now: float,
        *,
        reason: str,
        latency: float | None = None,
        streak: int = 0,
        held_for_sec: float = 0.0,
        fail_streak: int = 0,
    ) -> None:
        if self._shutdown_requested:
            return
        if (now - self._last_xray_auto_refresh_at) < XRAY_AUTO_REFRESH_COOLDOWN_SEC:
            return
        self._last_xray_auto_refresh_at = now
        self._xray_high_latency_streak = 0
        self._xray_full_refresh_candidate_since = 0.0
        self._xray_health_fail_streak = 0
        self._run_xray_background_refresh(
            reason=reason,
            latency_ms=latency,
            streak=streak,
            held_for_sec=held_for_sec,
            fail_streak=fail_streak,
        )

    def _run_xray_background_refresh(
        self,
        *,
        reason: str,
        latency_ms: float | None = None,
        streak: int = 0,
        held_for_sec: float = 0.0,
        fail_streak: int = 0,
    ) -> None:
        if self._shutdown_requested:
            return
        if self._refresh_in_progress.is_set():
            return
        self._refresh_in_progress.set()
        self.last_refresh_started_at = time.time()
        self._emit(
            "xray_background_refresh_started",
            reason=reason,
            threshold_ms=XRAY_FULL_REFRESH_LATENCY_MS,
            latency_ms=latency_ms,
            streak=streak,
            held_for_sec=held_for_sec,
            fail_streak=fail_streak,
        )
        try:
            self._log(f"[sing-box] background refresh started ({reason})")
            self.xray_runtime.refresh(cancel_event=self.live_probe_stop)
        except Exception as exc:
            self._log(f"[sing-box] background refresh failed: {exc}")
            self._emit("xray_background_refresh_failed", error=str(exc))
        finally:
            self.last_refresh_finished_at = time.time()
            self._refresh_in_progress.clear()

    def _run_background_health_cycle(self) -> None:
        if not self._health_cycle_lock.acquire(blocking=False):
            return
        try:
            if self._refresh_in_progress.is_set():
                return
            if self._run_mtproxy_latency_guard():
                return
            pressure = self._active_media_transfer_pressure()
            if pressure["active_media"] > 0 or pressure["active_heavy"] > 0:
                return
            now = time.time()
            prefer_media = (
                pressure["active_media"] > 0
                or pressure["active_heavy"] > 0
                or (now - self._last_media_activity_at) <= 60.0
            )
            focused_interval = 35.0 if self.local_server.is_running() else 75.0
            if prefer_media and self.local_server.is_running():
                focused_interval = 24.0
            if pressure["active_heavy"] > 0:
                focused_interval = 12.0
            broad_interval = max(150.0, float(self.config.live_probe_interval_sec) * 6.0)
            media_interval = 900.0
            if prefer_media:
                media_interval = 180.0
            if pressure["active_heavy"] > 0:
                media_interval = 60.0

            if (now - self._last_focused_probe_at) >= focused_interval:
                self._run_live_probe_once(focused=True, prefer_media=prefer_media)
                self._last_focused_probe_at = now

            if (now - self._last_broad_probe_at) >= broad_interval:
                self._run_live_probe_once(
                    focused=False,
                    prefer_media=prefer_media and pressure["recent_media"] > 0,
                )
                self._last_broad_probe_at = now

            if (now - self._last_media_pulse_at) >= media_interval:
                self._run_background_media_pulse(limit=3 if prefer_media else 1, prefer_media=prefer_media)
                self._last_media_pulse_at = now
        finally:
            self._health_cycle_lock.release()

    def _run_mtproxy_latency_guard(self) -> bool:
        if self.config.active_mode != "mtproxy_picker" or not self.local_server.is_running():
            return False
        now = time.time()
        if (now - self._last_mtproxy_health_at) < MTPROXY_HEALTH_INTERVAL_SEC:
            return False
        self._last_mtproxy_health_at = now
        best = self.pool.best()
        if best is None:
            return False
        latency = best.telegram_ping_ms
        if latency is None:
            return False
        self._emit(
            "mtproxy_health",
            latency_ms=latency,
            threshold_ms=MTPROXY_QUICK_SWITCH_LATENCY_MS,
            full_threshold_ms=MTPROXY_FULL_REFRESH_LATENCY_MS,
        )
        if latency < MTPROXY_QUICK_SWITCH_LATENCY_MS:
            self._mtproxy_high_latency_streak = 0
            self._mtproxy_full_refresh_candidate_since = 0.0
            return False

        self._mtproxy_high_latency_streak += 1
        full_latency_candidate = latency >= MTPROXY_FULL_REFRESH_LATENCY_MS
        held_for_sec = 0.0
        if full_latency_candidate:
            if self._mtproxy_full_refresh_candidate_since <= 0:
                self._mtproxy_full_refresh_candidate_since = now
            held_for_sec = max(0.0, now - self._mtproxy_full_refresh_candidate_since)
        else:
            self._mtproxy_full_refresh_candidate_since = 0.0
        required_streak = MTPROXY_FULL_REFRESH_CONFIRM_STREAK if full_latency_candidate else MTPROXY_QUICK_SWITCH_CONFIRM_STREAK
        required_sec = MTPROXY_FULL_REFRESH_CONFIRM_SEC if full_latency_candidate else 0.0
        self._log(
            f"[mtproxy] high latency {latency:.0f} ms, "
            f"streak={self._mtproxy_high_latency_streak}/{required_streak}, "
            f"held={held_for_sec:.0f}/{required_sec:.0f}s"
        )
        self._emit(
            "mtproxy_high_latency_observed",
            latency_ms=latency,
            threshold_ms=MTPROXY_FULL_REFRESH_LATENCY_MS if full_latency_candidate else MTPROXY_QUICK_SWITCH_LATENCY_MS,
            streak=self._mtproxy_high_latency_streak,
            required_streak=required_streak,
            held_for_sec=held_for_sec,
            required_sec=required_sec,
        )
        if self._mtproxy_high_latency_streak < MTPROXY_QUICK_SWITCH_CONFIRM_STREAK:
            return False

        best_cached_latency = self._run_mtproxy_quick_sort_if_due(now, latency=latency)
        if best_cached_latency is not None and best_cached_latency < MTPROXY_FULL_REFRESH_LATENCY_MS:
            self._mtproxy_high_latency_streak = 0
            self._mtproxy_full_refresh_candidate_since = 0.0
            return True

        if (
            full_latency_candidate
            and self._mtproxy_high_latency_streak >= MTPROXY_FULL_REFRESH_CONFIRM_STREAK
            and held_for_sec >= MTPROXY_FULL_REFRESH_CONFIRM_SEC
            and (best_cached_latency is None or best_cached_latency >= MTPROXY_FULL_REFRESH_LATENCY_MS)
        ):
            self._run_mtproxy_full_refresh_if_due(
                now,
                reason=f"latency_{latency:.0f}ms",
                latency=latency,
                streak=self._mtproxy_high_latency_streak,
                held_for_sec=held_for_sec,
            )
            return True
        return False

    def _best_cached_mtproxy_latency(self) -> float | None:
        candidates = self.pool.select_candidates(is_media=False, limit=max(1, min(16, self.pool.count())))
        latencies: list[float] = []
        for item in candidates:
            value = item.telegram_ping_ms
            if value is None:
                continue
            with contextlib.suppress(TypeError, ValueError):
                if float(value) > 0:
                    latencies.append(float(value))
        best = self.pool.best()
        if best is not None and best.telegram_ping_ms is not None:
            with contextlib.suppress(TypeError, ValueError):
                if float(best.telegram_ping_ms) > 0:
                    latencies.append(float(best.telegram_ping_ms))
        return min(latencies) if latencies else None

    def _run_mtproxy_quick_sort_if_due(self, now: float, *, latency: float) -> float | None:
        if self._shutdown_requested:
            return self._best_cached_mtproxy_latency()
        if (now - self._last_mtproxy_quick_sort_at) < MTPROXY_QUICK_SWITCH_COOLDOWN_SEC:
            return self._best_cached_mtproxy_latency()
        if self.pool.count() <= 0:
            self._run_mtproxy_full_refresh_if_due(now, reason="no_cached_proxies")
            return None
        self._last_mtproxy_quick_sort_at = now
        self._log(f"[mtproxy] high latency {latency:.0f} ms, quick ping-sort")
        self._emit("mtproxy_background_quick_sort_started", latency_ms=latency, threshold_ms=MTPROXY_QUICK_SWITCH_LATENCY_MS)
        self.quick_probe_pool(limit=max(self.config.live_probe_top_n, 12), reason="latency_guard")
        return self._best_cached_mtproxy_latency()

    def _run_mtproxy_full_refresh_if_due(
        self,
        now: float,
        *,
        reason: str,
        latency: float | None = None,
        streak: int = 0,
        held_for_sec: float = 0.0,
    ) -> None:
        if self._shutdown_requested:
            return
        if (now - self._last_mtproxy_auto_refresh_at) < MTPROXY_AUTO_REFRESH_COOLDOWN_SEC:
            return
        self._last_mtproxy_auto_refresh_at = now
        self._mtproxy_high_latency_streak = 0
        self._mtproxy_full_refresh_candidate_since = 0.0
        self._emit(
            "mtproxy_background_refresh_started",
            reason=reason,
            threshold_ms=MTPROXY_FULL_REFRESH_LATENCY_MS,
            latency_ms=latency,
            streak=streak,
            held_for_sec=held_for_sec,
        )
        self._log(f"[mtproxy] sustained high latency, background web refresh ({reason})")
        self.run_refresh(manual=False)

    def _run_live_probe_once(self, *, focused: bool, prefer_media: bool = False) -> None:
        if self._refresh_in_progress.is_set():
            return
        if focused:
            candidates = self.pool.select_monitor_targets(limit=3 if prefer_media else 2, prefer_media=prefer_media)
        elif prefer_media:
            candidates = self.pool.select_turbo_media_candidates(limit=max(2, min(5, self.config.live_probe_top_n)))
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
                concurrency=max(1, min((3 if focused and prefer_media else 2 if focused else 4), len(candidates))),
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
                high_latency_streak_limit=1 if prefer_media and focused else 2 if focused else 3,
                failure_limit=1 if prefer_media and focused else 2 if focused else 3,
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
        self._emit("live_probe_updated", count=len(outcomes), focused=focused, prefer_media=prefer_media)

    def _run_background_media_pulse(self, *, limit: int = 1, prefer_media: bool = False) -> None:
        if self._refresh_in_progress.is_set() or not self.local_server.is_running():
            return
        if not self.auth_config.api_id or not self.auth_config.api_hash.strip():
            return
        candidates = self.pool.select_monitor_targets(limit=max(1, limit), prefer_media=prefer_media)
        if not candidates:
            return
        for target in candidates:
            try:
                with self.telegram_lock:
                    result = run_async(light_media_probe(target.proxy, self.auth_config))
            except Exception as exc:
                self._log(f"[media-bg] probe error for {target.proxy.host}:{target.proxy.port} -> {exc}")
                continue

            if result.note == "session_not_authorized":
                self._emit("telegram_auth_required", feature="background_media")
                self._log("[media-bg] skipped: telegram_session_not_authorized")
                return
            if result.note in {"no_media_samples_found", "no_video_samples_found"}:
                self.pool.update_deep_media_score(
                    result.proxy_key,
                    result.score,
                    result.note,
                    upload_kbps=result.upload_kbps,
                    download_kbps=result.download_kbps,
                    aux_kbps=result.aux_kbps,
                )
                self._log(f"[media-bg] {target.proxy.host}:{target.proxy.port} -> {result.note}")
                continue

            cooldown_reason = self.pool.update_background_media_probe(
                result.proxy_key,
                result.score,
                result.note,
                upload_kbps=result.upload_kbps,
                download_kbps=result.download_kbps,
                aux_kbps=result.aux_kbps,
                failure_score=0.7 if prefer_media else 0.6,
                cooldown_seconds=360.0 if prefer_media else 300.0,
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

    def _schedule_media_acceleration_probe(self, payload: dict[str, Any]) -> None:
        now = time.time()
        if (now - self._last_media_accel_probe_at) < 12.0:
            return
        self._last_media_accel_probe_at = now

        def _runner() -> None:
            if not self._health_cycle_lock.acquire(blocking=False):
                return
            try:
                host = str(payload.get("host") or "")
                port = payload.get("port")
                upload_kbps = payload.get("upload_kbps")
                label = f"{host}:{port}" if host and port else "media session"
                self._log(f"[media-boost] heavy upload detected on {label}, reprobe turbo shortlist ({upload_kbps} KB/s)")
                self._run_live_probe_once(focused=True, prefer_media=True)
                self._run_background_media_pulse(limit=3, prefer_media=True)
                stamp = time.time()
                self._last_focused_probe_at = stamp
                self._last_media_pulse_at = stamp
            finally:
                self._health_cycle_lock.release()

        threading.Thread(target=_runner, daemon=True, name="mtproxy-media-boost").start()

    def _handle_internal_event(self, event_name: str, payload: dict[str, Any]) -> None:
        now = time.time()
        if event_name == "local_upstream_selected" and bool(payload.get("is_media")):
            self._last_media_activity_at = now
            return
        if event_name == "local_media_activity":
            self._last_media_activity_at = now
            if bool(payload.get("heavy_upload")):
                self._last_heavy_upload_at = now
            return
        if event_name == "local_session_closed" and (bool(payload.get("is_media")) or bool(payload.get("heavy_upload"))):
            self._last_media_activity_at = now
            if bool(payload.get("heavy_upload")):
                self._last_heavy_upload_at = now

    def _export_combined_results(
        self,
        base_result: CollectorRunResult,
        all_outcomes: list[ProbeOutcome],
        working: list[ProbeOutcome],
        rejected: list[ProbeOutcome],
    ) -> None:
        out_dir = self._out_dir_path()
        out_dir.mkdir(parents=True, exist_ok=True)
        all_txt_path = out_dir / ALL_FILE_NAME
        working_txt_path = out_dir / LIST_FILE_NAME
        fast_txt_path = out_dir / FAST_LIST_FILE_NAME
        rejected_txt_path = out_dir / REJECTED_FILE_NAME
        report_json_path = out_dir / REPORT_FILE_NAME
        source_audit_path = out_dir / SOURCE_AUDIT_FILE_NAME
        socks5_all_txt_path = out_dir / SOCKS5_FILE_NAME

        fast_urls = [item.proxy.url for item in self._select_fast_candidates(working)]
        self._write_url_list(all_txt_path, [item.proxy.url for item in all_outcomes])
        self._write_url_list(working_txt_path, [item.proxy.url for item in working])
        self._write_url_list(fast_txt_path, fast_urls)
        self._write_url_list(rejected_txt_path, [item.proxy.url for item in rejected])
        with contextlib.suppress(Exception):
            if socks5_all_txt_path.exists():
                socks5_all_txt_path.unlink()

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
        report["telegram_api_proxy_url"] = self.config.telegram_api_proxy_url
        report["deep_media_enabled"] = self.config.deep_media_enabled
        report["rf_whitelist_check_enabled"] = self.config.rf_whitelist_check_enabled
        report["thread_source_enabled"] = self.config.thread_source_enabled
        report["thread_source_url"] = self.config.thread_source_url
        source_audit = self._build_source_audit(base_result.source_summaries, all_outcomes)
        report["source_audit"] = source_audit
        report["proxies"] = self._augment_report_proxy_rows(report["proxies"])
        self._write_json_file(report_json_path, report)
        self._atomic_write(source_audit_path, self._format_source_audit(source_audit))

        self.last_export = {
            "all_txt_path": str(all_txt_path),
            "working_txt_path": str(working_txt_path),
            "fast_txt_path": str(fast_txt_path),
            "rejected_txt_path": str(rejected_txt_path),
            "report_json_path": str(report_json_path),
            "source_audit_path": str(source_audit_path),
        }
        self._emit(
            "files_written",
            out_dir=str(out_dir),
            all_txt_path=str(all_txt_path),
            working_txt_path=str(working_txt_path),
            fast_txt_path=str(fast_txt_path),
            rejected_txt_path=str(rejected_txt_path),
            report_json_path=str(report_json_path),
            source_audit_path=str(source_audit_path),
        )

    def _build_source_audit(self, source_summaries: list[Any], outcomes: list[ProbeOutcome]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for summary in source_summaries:
            source_url = str(summary.source_url)
            source_outcomes = [item for item in outcomes if source_url in item.proxy.sources]
            reason_counts: dict[str, int] = {}
            for outcome in source_outcomes:
                reason_counts[outcome.reason] = reason_counts.get(outcome.reason, 0) + 1
            rows.append(
                {
                    "source_url": source_url,
                    "fetched_count": len(summary.fetched_urls),
                    "fetched_urls": list(summary.fetched_urls),
                    "errors": list(summary.errors),
                    "error_count": len(summary.errors),
                    "mtproxy_found": int(getattr(summary, "mtproxy_found", 0)),
                    "mtproxy_new": int(getattr(summary, "mtproxy_new", 0)),
                    "mtproxy_duplicate": int(getattr(summary, "mtproxy_duplicate", 0)),
                    "socks5_found": int(getattr(summary, "socks5_found", 0)),
                    "socks5_new": int(getattr(summary, "socks5_new", 0)),
                    "socks5_duplicate": int(getattr(summary, "socks5_duplicate", 0)),
                    "script_urls_found": int(getattr(summary, "script_urls_found", 0)),
                    "data_urls_found": int(getattr(summary, "data_urls_found", 0)),
                    "probed_unique": len(source_outcomes),
                    "working": sum(1 for item in source_outcomes if item.accepted),
                    "rejected": sum(1 for item in source_outcomes if not item.accepted),
                    "reasons": dict(sorted(reason_counts.items())),
                }
            )
        rows.sort(key=lambda item: (int(item["working"]), int(item["mtproxy_new"]), int(item["mtproxy_found"])), reverse=True)
        return rows

    @staticmethod
    def _format_source_audit(rows: list[dict[str, Any]]) -> str:
        lines = [
            "MTProxy AutoSwitch source audit",
            f"generated_at={time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
        ]
        totals = {
            "found": sum(int(row.get("mtproxy_found") or 0) for row in rows),
            "new": sum(int(row.get("mtproxy_new") or 0) for row in rows),
            "duplicate": sum(int(row.get("mtproxy_duplicate") or 0) for row in rows),
            "probed": sum(int(row.get("probed_unique") or 0) for row in rows),
            "working": sum(int(row.get("working") or 0) for row in rows),
            "rejected": sum(int(row.get("rejected") or 0) for row in rows),
            "errors": sum(int(row.get("error_count") or 0) for row in rows),
        }
        lines.append(
            "TOTAL "
            f"found={totals['found']} new={totals['new']} duplicate={totals['duplicate']} "
            f"probed_refs={totals['probed']} working_refs={totals['working']} "
            f"rejected_refs={totals['rejected']} errors={totals['errors']}"
        )
        lines.append("")
        for index, row in enumerate(rows, start=1):
            reasons = ", ".join(f"{key}:{value}" for key, value in dict(row.get("reasons") or {}).items()) or "-"
            lines.append(f"[{index}] {row.get('source_url')}")
            lines.append(
                "    "
                f"fetched={row.get('fetched_count')} errors={row.get('error_count')} "
                f"mtproxy_found={row.get('mtproxy_found')} new={row.get('mtproxy_new')} "
                f"duplicate={row.get('mtproxy_duplicate')} probed_refs={row.get('probed_unique')} "
                f"working_refs={row.get('working')} rejected_refs={row.get('rejected')}"
            )
            lines.append(
                "    "
                f"socks5_found={row.get('socks5_found')} scripts={row.get('script_urls_found')} "
                f"data_urls={row.get('data_urls_found')} reasons={reasons}"
            )
            errors = list(row.get("errors") or [])
            if errors:
                for error in errors[:5]:
                    lines.append(f"    error: {error}")
                if len(errors) > 5:
                    lines.append(f"    ... {len(errors) - 5} more errors")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _select_fast_candidates(self, working: list[ProbeOutcome]) -> list[ProbeOutcome]:
        limit = max(1, int(self.config.fast_list_limit or DEFAULT_FAST_LIST_LIMIT))
        if not working:
            return []
        ordered = sorted(working, key=self._working_priority_key)
        latency_cap = max(float(self.config.max_latency_ms or 300.0) * 0.85, 180.0)
        success_floor = max(float(self.config.min_success_rate or 0.7), 0.85)
        ratio_cap = min(float(self.config.max_high_latency_ratio or 0.6), 0.35)

        selected: list[ProbeOutcome] = []
        selected_keys: set[tuple[str, int, str]] = set()
        selected_hosts: set[str] = set()

        def try_append(outcome: ProbeOutcome, *, unique_host: bool) -> None:
            if len(selected) >= limit or outcome.proxy.key in selected_keys:
                return
            if unique_host and outcome.proxy.host in selected_hosts:
                return
            selected.append(outcome)
            selected_keys.add(outcome.proxy.key)
            selected_hosts.add(outcome.proxy.host)

        preferred = [
            outcome
            for outcome in ordered
            if outcome.success_rate >= success_floor
            and outcome.high_latency_ratio <= ratio_cap
            and (outcome.avg_latency_ms is None or outcome.avg_latency_ms <= latency_cap)
        ]
        for outcome in preferred:
            try_append(outcome, unique_host=True)
        for outcome in preferred:
            try_append(outcome, unique_host=False)
        for outcome in ordered:
            try_append(outcome, unique_host=True)
        for outcome in ordered:
            try_append(outcome, unique_host=False)
        return selected[:limit]

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
        for proxy_key, result in self._latest_deep_media_scores.items():
            self.pool.update_deep_media_score(
                proxy_key,
                result.score,
                result.note,
                upload_kbps=result.upload_kbps,
                download_kbps=result.download_kbps,
                aux_kbps=result.aux_kbps,
            )

    def _apply_manual_override_from_config(self) -> None:
        raw_url = str(getattr(self.config, "manual_upstream_url", "") or "").strip()
        if not raw_url:
            self.pool.set_manual_override(None)
            return
        proxy = parse_proxy_link(raw_url, "config", "config")
        if proxy is None:
            self.pool.set_manual_override(None)
            return
        self.pool.set_manual_override(proxy.key)

    def select_manual_upstream(self, proxy_url: str) -> None:
        proxy = parse_proxy_link(str(proxy_url or "").strip(), "manual_select", "manual_select")
        if proxy is None:
            raise ValueError("invalid proxy url")
        if self.pool.snapshot_by_key(proxy.key) is None:
            raise ValueError("proxy not found in current pool")
        self.config.manual_upstream_url = proxy.url
        self.save_config()
        self._apply_manual_override_from_config()
        self._restart_local_server_if_running(reason="manual upstream selected")
        self._emit("manual_upstream_changed", url=self.config.manual_upstream_url)

    def clear_manual_upstream(self) -> None:
        if self.config.manual_upstream_url:
            self.config.manual_upstream_url = ""
            self.save_config()
        self.pool.set_manual_override(None)
        self._restart_local_server_if_running(reason="auto balance selected")
        self._emit("manual_upstream_changed", url="")

    def select_xray_upstream(self, node_url: str) -> None:
        raw_url = str(node_url or "").strip()
        if not raw_url:
            raise ValueError("invalid xray node url")
        self.xray_runtime.update_selection(self.config.balancer_strategy, raw_url, restart=self.config.active_mode == "xray_core")
        self.config.xray_manual_upstream_url = raw_url
        self.save_config()
        self._emit("xray_upstream_changed", url=raw_url)

    def clear_xray_upstream(self) -> None:
        if self.config.xray_manual_upstream_url:
            self.config.xray_manual_upstream_url = ""
            self.save_config()
        self.xray_runtime.update_selection(self.config.balancer_strategy, "", restart=self.config.active_mode == "xray_core")
        self._emit("xray_upstream_changed", url="")

    def sort_mtproxy_pool(self, *, limit: int | None = None) -> int:
        if self.config.active_mode != "mtproxy_picker":
            return self.quick_sort_active_mode()
        checked = self.quick_probe_pool(limit=limit or self.config.live_probe_top_n, reason="manual_sort")
        self._sync_last_working_from_pool_order()
        self._persist_current_mtproxy_lists()
        self._emit("mtproxy_sort_finished", checked=checked)
        return checked

    def import_mtproxy_urls_from_text(self, text: str) -> dict[str, int]:
        if self.config.active_mode != "mtproxy_picker":
            raise RuntimeError("mtproxy_import_requires_mtproxy_mode")
        raw_text = str(text or "").strip()
        artifacts = scan_text(raw_text, "clipboard", "clipboard")
        parsed: dict[tuple[str, int, str], ProxyRecord] = {}
        for proxy in artifacts.proxies:
            parsed.setdefault(proxy.key, proxy)
        if not parsed:
            self._emit("mtproxy_import_finished", parsed=0, new=0, accepted=0, rejected=0)
            return {"parsed": 0, "new": 0, "accepted": 0, "rejected": 0}

        pool_keys = {tuple(row.get("key")) for row in self.pool.snapshot() if row.get("key")}
        candidates = [proxy for key, proxy in parsed.items() if key not in pool_keys]
        if not candidates:
            self._emit("mtproxy_import_finished", parsed=len(parsed), new=0, accepted=0, rejected=0)
            return {"parsed": len(parsed), "new": 0, "accepted": 0, "rejected": 0}

        self._emit("mtproxy_import_started", parsed=len(parsed), new=len(candidates))
        outcomes = run_async(
            probe_all(
                proxies=candidates,
                settings=self._import_probe_settings(),
                concurrency=max(1, min(8, len(candidates), int(self.config.workers or 1))),
                verbose=False,
                log_sink=self._log,
                event_sink=lambda name, payload: self._emit(f"mtproxy_import_{name}", payload),
            )
        )
        accepted = sorted((item for item in outcomes if item.accepted), key=outcome_sort_key)
        rejected = sorted((item for item in outcomes if not item.accepted), key=lambda item: (item.reason, outcome_sort_key(item)))
        outcome_keys = {item.proxy.key for item in outcomes}
        accepted_keys = {item.proxy.key for item in accepted}

        self.last_working = sorted(
            [item for item in self.last_working if item.proxy.key not in accepted_keys] + accepted,
            key=self._working_priority_key,
        )
        self.last_rejected = sorted(
            [item for item in self.last_rejected if item.proxy.key not in outcome_keys] + rejected,
            key=lambda item: (item.reason, outcome_sort_key(item)),
        )
        self.last_outcomes = [item for item in self.last_outcomes if item.proxy.key not in outcome_keys] + outcomes
        self.pool.replace_outcomes(self.last_working)
        self._apply_manual_override_from_config()
        self._persist_current_mtproxy_lists()
        self._emit(
            "mtproxy_import_finished",
            parsed=len(parsed),
            new=len(candidates),
            accepted=len(accepted),
            rejected=len(rejected),
        )
        return {
            "parsed": len(parsed),
            "new": len(candidates),
            "accepted": len(accepted),
            "rejected": len(rejected),
        }

    def import_proxies(self, text: str) -> dict[str, int]:
        """Alias for import_mtproxy_urls_from_text (import proxy list from text)."""
        return self.import_mtproxy_urls_from_text(text)

    def delete_unavailable_mtproxies(self) -> dict[str, int]:
        if self.config.active_mode != "mtproxy_picker":
            raise RuntimeError("mtproxy_delete_requires_mtproxy_mode")
        rejected_keys = {item.proxy.key for item in self.last_rejected}
        cooldown_keys = self.pool.cooldown_keys()
        delete_keys = rejected_keys | cooldown_keys
        if not delete_keys:
            self._emit("mtproxy_delete_finished", removed=0)
            return {"removed": 0}

        manual_proxy = parse_proxy_link(self.config.manual_upstream_url, "manual", "manual") if self.config.manual_upstream_url else None
        if manual_proxy is not None and manual_proxy.key in delete_keys:
            self.config.manual_upstream_url = ""
            self.save_config()

        removed_from_pool = self.pool.drop_unavailable()
        removed_from_pool += self.pool.remove_keys(delete_keys)
        self.last_working = [item for item in self.last_working if item.proxy.key not in delete_keys]
        self.last_rejected = [item for item in self.last_rejected if item.proxy.key not in delete_keys]
        self.last_outcomes = [item for item in self.last_outcomes if item.proxy.key not in delete_keys]
        self._persist_current_mtproxy_lists()
        self._emit("mtproxy_delete_finished", removed=len(delete_keys), removed_from_pool=removed_from_pool)
        return {"removed": len(delete_keys), "removed_from_pool": removed_from_pool}

    def delete_unavailable_proxies(self) -> dict[str, int]:
        """Alias for delete_unavailable_mtproxies."""
        return self.delete_unavailable_mtproxies()

    def pool_health_report(self) -> list[dict[str, Any]]:
        """Return a health report table for the current proxy pool."""
        return self.pool.get_health_report()

    def pool_leaderboard(self, *, limit: int = 10) -> list[dict[str, Any]]:
        """Return the top-N most stable proxies (leaderboard)."""
        return self.pool.get_health_report()[: max(1, int(limit))]

    def pool_priority_list(self) -> list[dict[str, Any]]:
        """Return the full pool ordered by selection priority."""
        states = self.pool.get_priority_list()
        rows_by_key = {row["key"]: row for row in self.pool.get_health_report()}
        return [dict(rows_by_key[state.key]) for state in states if state.key in rows_by_key]

    def stress_test_mtproxy_pool(self, *, limit: int = 24) -> dict[str, int]:
        if self.config.active_mode != "mtproxy_picker":
            raise RuntimeError("mtproxy_stress_requires_mtproxy_mode")
        if self.pool.count() <= 0:
            self._emit("mtproxy_stress_finished", total=0, stable=0, rejected=0, media_probed=0)
            return {"total": 0, "stable": 0, "rejected": 0, "media_probed": 0}

        candidates = self.pool.select_candidates(is_media=False, limit=max(1, min(int(limit or 24), self.pool.count())))
        proxies = [item.proxy for item in candidates]
        self._emit("mtproxy_stress_started", total=len(proxies))
        settings = ProbeSettings(
            duration=60.0,
            interval=2.0,
            timeout=max(5.0, min(10.0, float(self.config.timeout or 8.0))),
            max_latency_ms=float(self.config.max_latency_ms or 300.0),
            min_success_rate=0.9,
            max_high_latency_ratio=0.25,
            high_latency_streak=2,
            unreachable_failures=2,
        )
        outcomes = run_async(
            probe_all(
                proxies=proxies,
                settings=settings,
                concurrency=max(1, min(6, len(proxies), int(self.config.workers or 1))),
                verbose=False,
                log_sink=self._log,
                event_sink=lambda name, payload: self._emit(f"mtproxy_stress_{name}", payload),
            )
        )

        stable: list[ProbeOutcome] = []
        rejected: list[ProbeOutcome] = []
        for outcome in outcomes:
            strict_ok = (
                outcome.accepted
                and outcome.success_rate >= 0.9
                and outcome.high_latency_ratio <= 0.25
                and outcome.max_consecutive_failures <= 1
                and outcome.max_consecutive_high_latency < 2
            )
            if strict_ok:
                stable.append(outcome)
            else:
                outcome.accepted = False
                if outcome.reason == "ok":
                    outcome.reason = "stress_unstable"
                rejected.append(outcome)

        media_probed = 0
        if stable and auth_is_configured(self.auth_config):
            for outcome in self._select_fast_candidates(stable)[: min(6, len(stable))]:
                result = run_async(light_media_probe(outcome.proxy, self.auth_config))
                self._latest_deep_media_scores[outcome.proxy.key] = result
                self.pool.update_deep_media_score(
                    outcome.proxy.key,
                    result.score,
                    result.note,
                    upload_kbps=result.upload_kbps,
                    download_kbps=result.download_kbps,
                    aux_kbps=result.aux_kbps,
                )
                media_probed += 1

        tested_keys = {item.proxy.key for item in outcomes}
        self.last_working = sorted(
            [item for item in self.last_working if item.proxy.key not in tested_keys] + stable,
            key=self._working_priority_key,
        )
        self.last_rejected = sorted(
            [item for item in self.last_rejected if item.proxy.key not in tested_keys] + rejected,
            key=lambda item: (item.reason, outcome_sort_key(item)),
        )
        self.last_outcomes = [item for item in self.last_outcomes if item.proxy.key not in tested_keys] + outcomes
        self.pool.replace_outcomes(self.last_working)
        self._apply_manual_override_from_config()
        self._apply_latest_deep_media_scores()
        self._persist_current_mtproxy_lists()
        self._emit(
            "mtproxy_stress_finished",
            total=len(outcomes),
            stable=len(stable),
            rejected=len(rejected),
            media_probed=media_probed,
        )
        return {
            "total": len(outcomes),
            "stable": len(stable),
            "rejected": len(rejected),
            "media_probed": media_probed,
        }

    def quick_probe_pool(self, *, limit: int = 8, reason: str = "manual") -> int:
        if self.pool.count() <= 0:
            return 0
        with self._health_cycle_lock:
            pressure = self._active_media_transfer_pressure()
            if pressure["active_media"] > 0 or pressure["active_heavy"] > 0:
                self._log(
                    f"[quick-probe] skipped during active media (reason={reason} "
                    f"active_media={pressure['active_media']} active_heavy={pressure['active_heavy']})"
                )
                self._emit("quick_probe_skipped", reason=reason, **pressure)
                return 0
            now = time.time()
            if reason == "startup" and (now - self._last_quick_probe_at) < 20.0:
                return 0
            candidates = self.pool.select_candidates(is_media=False, limit=max(1, min(limit, self.pool.count())))
            if not candidates:
                return 0
            self._emit("quick_probe_started", total=len(candidates), reason=reason)
            settings = ProbeSettings(
                duration=2.4 if reason == "startup" else 3.0,
                interval=0.6,
                timeout=min(5.5, max(3.0, float(self.config.timeout))),
                max_latency_ms=min(280.0, max(180.0, float(self.config.max_latency_ms or 300.0))),
                min_success_rate=0.34,
                max_high_latency_ratio=1.0,
                high_latency_streak=4,
                unreachable_failures=1,
            )
            outcomes = run_async(
                probe_all(
                    proxies=[item.proxy for item in candidates],
                    settings=settings,
                    concurrency=max(1, min(4, len(candidates))),
                    verbose=False,
                    log_sink=self._log,
                    event_sink=None,
                )
            )
            for outcome in outcomes:
                self.pool.update_live_probe(
                    outcome.proxy.key,
                    outcome.avg_latency_ms,
                    outcome.successes > 0,
                    outcome.reason,
                    max_latency_ms=float(self.config.max_latency_ms or 300.0),
                    high_latency_streak_limit=3,
                    failure_limit=2,
                    cooldown_seconds=240.0 if reason == "startup" else 180.0,
                )
            self._last_quick_probe_at = time.time()
            self._emit("quick_probe_finished", total=len(outcomes), reason=reason)
            return len(outcomes)

    def _configured_telegram_api_proxy(self) -> ProxyRecord | None:
        raw_url = str(getattr(self.config, "telegram_api_proxy_url", "") or "").strip()
        if not raw_url:
            return None
        proxy = parse_proxy_link(raw_url, "telegram_api_proxy", "telegram_api_proxy")
        if proxy is None:
            self._log("[telegram-api] configured proxy url is invalid; using runtime fallback")
        return proxy

    @staticmethod
    def _proxy_identity(proxy: ProxyRecord | None) -> tuple[str, int, str] | None:
        if proxy is None:
            return None
        return proxy.key

    def _telegram_api_proxy_candidates(
        self,
        *,
        preferred: ProxyRecord | None = None,
        include_direct: bool = True,
        include_pool: bool = True,
        max_pool_candidates: int = 2,
    ) -> list[ProxyRecord | None]:
        candidates: list[ProxyRecord | None] = []
        seen: set[tuple[str, int, str] | None] = set()

        def add(proxy: ProxyRecord | None, *, allow_none: bool = False) -> None:
            if proxy is None and not allow_none:
                return
            key = self._proxy_identity(proxy)
            if key in seen:
                return
            seen.add(key)
            candidates.append(proxy)

        if bool(getattr(self.config, "telegram_api_proxy_enabled", False)):
            add(self._configured_telegram_api_proxy())
        add(preferred)
        if include_pool:
            add(self._best_proxy())
            for state in self.pool.select_candidates(is_media=False, limit=max(0, int(max_pool_candidates or 0))):
                add(state.proxy)
            for outcome in sorted(self.last_working, key=self._working_priority_key)[: max(0, int(max_pool_candidates or 0))]:
                add(outcome.proxy)
            if int(max_pool_candidates or 0) > 2:
                for proxy in self._load_known_working_proxy_records()[: max(0, int(max_pool_candidates or 0))]:
                    add(proxy)
        if include_direct:
            add(None, allow_none=True)
        return candidates

    @staticmethod
    def _telegram_proxy_label(proxy: ProxyRecord | None) -> str:
        if proxy is None:
            return "direct"
        return f"{proxy.host}:{proxy.port}"

    def _run_telegram_api_call(
        self,
        operation: str,
        factory: Any,
        *,
        preferred: ProxyRecord | None = None,
        include_direct: bool = True,
        include_pool: bool = True,
        max_pool_candidates: int = 2,
        attempts_per_proxy: int = 1,
    ) -> Any:
        no_retry_errors = {
            "resend_code_timeout",
            "send_code_timeout",
            "sign_in_timeout",
            "password_sign_in_timeout",
            "send_empty_timeout",
            "send_chunk_timeout",
        }
        last_exc: Exception | None = None
        for upstream in self._telegram_api_proxy_candidates(
            preferred=preferred,
            include_direct=include_direct,
            include_pool=include_pool,
            max_pool_candidates=max_pool_candidates,
        ):
            for attempt in range(1, max(1, int(attempts_per_proxy or 1)) + 1):
                try:
                    suffix = f" attempt {attempt}" if int(attempts_per_proxy or 1) > 1 else ""
                    self._log(f"[telegram-api] {operation} via {self._telegram_proxy_label(upstream)}{suffix}")
                    return run_async(factory(upstream))
                except Exception as exc:
                    text = str(exc)
                    if text.startswith(TELEGRAM_USER_ERROR_PREFIX):
                        raise RuntimeError(text[len(TELEGRAM_USER_ERROR_PREFIX):]) from exc
                    if text in no_retry_errors:
                        self._log(f"[telegram-api] {operation} failed via {self._telegram_proxy_label(upstream)} without retry: {exc}")
                        raise
                    last_exc = exc
                    self._log(f"[telegram-api] {operation} failed via {self._telegram_proxy_label(upstream)}: {exc}")
                    if attempt < max(1, int(attempts_per_proxy or 1)):
                        time.sleep(min(2.0, 0.35 * attempt))
        if last_exc is not None:
            raise last_exc
        return run_async(factory(None))

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

    def _tg_parsed_proxy_path(self) -> Path:
        return (self._out_dir_path() / TG_PARSED_FILE_NAME).resolve()

    @staticmethod
    def _telegram_source_parse_slot(timestamp: float | None = None) -> tuple[int, int, str]:
        value = time.time() if timestamp is None else float(timestamp)
        local = time.localtime(value)
        if 6 <= int(local.tm_hour) < 18:
            return (int(local.tm_year), int(local.tm_yday), "day")
        slot_time = value if int(local.tm_hour) >= 18 else value - 6 * 60 * 60
        slot_local = time.localtime(slot_time)
        return (int(slot_local.tm_year), int(slot_local.tm_yday), "evening")

    def _telegram_source_current_slot_label(self) -> str:
        _, _, slot = self._telegram_source_parse_slot()
        return "day" if slot == "day" else "evening"

    def _telegram_source_cache_is_stale(self) -> bool:
        path = self._tg_parsed_proxy_path()
        if not path.exists():
            return True
        try:
            return self._telegram_source_parse_slot(path.stat().st_mtime) != self._telegram_source_parse_slot()
        except Exception:
            return True

    def _save_tg_parsed_proxy_records(self, proxies: list[ProxyRecord]) -> None:
        path = self._tg_parsed_proxy_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        urls = [proxy.url for proxy in proxies]
        self._write_url_list(path, urls)

    def _load_tg_parsed_proxy_records(self) -> list[ProxyRecord]:
        path = self._tg_parsed_proxy_path()
        proxies: dict[tuple[str, int, str], ProxyRecord] = {}
        for raw_url in self._read_url_list(path):
            proxy = parse_proxy_link(raw_url, f"telegram-cache:{path.name}", str(path))
            if proxy is None:
                continue
            proxy.sources.add(f"telegram-cache:{path.name}")
            proxies.setdefault(proxy.key, proxy)
        return list(proxies.values())

    def _load_manual_list_proxies(self) -> list[ProxyRecord]:
        paths = self._list_file_candidates(FAST_LIST_FILE_NAME, LIST_FILE_NAME)
        for root in self._user_list_roots():
            paths.append(root / LEGACY_OUT_DIR_NAME / LEGACY_WORKING_FILE_NAME)
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

    def _load_known_working_proxy_records(self) -> list[ProxyRecord]:
        limit = max(48, min(96, int(self.config.max_proxies or 0) or 96))
        paths = self._list_file_candidates(FAST_LIST_FILE_NAME, TG_PARSED_FILE_NAME, REPORT_FILE_NAME, LIST_FILE_NAME)
        for root in self._user_list_roots():
            paths.extend(
                [
                    root / LEGACY_OUT_DIR_NAME / LEGACY_WORKING_FILE_NAME,
                    root / LEGACY_OUT_DIR_NAME / LEGACY_REPORT_FILE_NAME,
                ]
            )
        proxies: dict[tuple[str, int, str], ProxyRecord] = {}
        for path in paths:
            if len(proxies) >= limit:
                break
            if path.suffix.lower() == ".json":
                for outcome in self._load_seed_outcomes(path, source_name=f"known:{path.name}"):
                    if len(proxies) >= limit:
                        break
                    outcome.proxy.sources.add(f"known:{path.name}")
                    proxies.setdefault(outcome.proxy.key, outcome.proxy)
                continue
            for raw_url in self._read_url_list(path):
                if len(proxies) >= limit:
                    break
                proxy = parse_proxy_link(raw_url, f"known:{path.name}", str(path))
                if proxy is None:
                    continue
                proxy.sources.add(f"known:{path.name}")
                proxies.setdefault(proxy.key, proxy)
        return list(proxies.values())

    def _read_existing_proxy_list_urls(self) -> list[str]:
        candidates = self._list_file_candidates(FAST_LIST_FILE_NAME, LIST_FILE_NAME)
        for root in self._user_list_roots():
            candidates.append(root / LEGACY_OUT_DIR_NAME / LEGACY_WORKING_FILE_NAME)
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

    def _read_url_list(self, path: Path) -> list[str]:
        if not path.exists():
            return []
        try:
            return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except Exception as exc:
            self._log(f"[manual-list] failed to read {path.name}: {exc}")
            return []

    def _write_url_list(self, path: Path, urls: list[str]) -> None:
        if not urls and path.name == LIST_FILE_NAME and path.exists():
            self._log(f"[export] keeping previous {path.name} unchanged")
            return
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

    def _sync_last_working_from_pool_order(self) -> None:
        if not self.last_working:
            return
        order = {
            str(row.get("url") or ""): index
            for index, row in enumerate(self.pool.snapshot())
            if row.get("url")
        }
        self.last_working = sorted(
            self.last_working,
            key=lambda item: (order.get(item.proxy.url, 1_000_000), self._working_priority_key(item)),
        )

    def _persist_current_mtproxy_lists(self) -> None:
        out_dir = self._out_dir_path()
        out_dir.mkdir(parents=True, exist_ok=True)
        all_txt_path = out_dir / ALL_FILE_NAME
        working_txt_path = out_dir / LIST_FILE_NAME
        fast_txt_path = out_dir / FAST_LIST_FILE_NAME
        rejected_txt_path = out_dir / REJECTED_FILE_NAME

        working = list(self.last_working)
        rejected = sorted(self.last_rejected, key=lambda item: (item.reason, outcome_sort_key(item)))
        fast_urls = [item.proxy.url for item in self._select_fast_candidates(working)]

        self._write_url_list(all_txt_path, [item.proxy.url for item in self.last_outcomes])
        self._write_url_list(working_txt_path, [item.proxy.url for item in working])
        self._write_url_list(fast_txt_path, fast_urls)
        self._write_url_list(rejected_txt_path, [item.proxy.url for item in rejected])
        self.last_export.update(
            {
                "all_txt_path": str(all_txt_path),
                "working_txt_path": str(working_txt_path),
                "fast_txt_path": str(fast_txt_path),
                "rejected_txt_path": str(rejected_txt_path),
            }
        )
        self._emit(
            "files_written",
            out_dir=str(out_dir),
            all_txt_path=str(all_txt_path),
            working_txt_path=str(working_txt_path),
            fast_txt_path=str(fast_txt_path),
            rejected_txt_path=str(rejected_txt_path),
        )

    def _load_initial_pool(self) -> None:
        # Load the full working pool first. fast_list.txt is a capped export subset
        # (DEFAULT_FAST_LIST_LIMIT) and must not win over the complete saved pool.
        report_candidates: list[tuple[Path, str]] = []
        for path in self._list_file_candidates(LIST_FILE_NAME):
            report_candidates.append((path, "default_list"))
        for path in self._list_file_candidates(REPORT_FILE_NAME):
            report_candidates.append((path, "cached_report"))
        for root in self._user_list_roots():
            report_candidates.append((root / LEGACY_OUT_DIR_NAME / LEGACY_WORKING_FILE_NAME, "legacy_working_list"))
        for root in self._user_list_roots():
            report_candidates.append((root / LEGACY_OUT_DIR_NAME / LEGACY_REPORT_FILE_NAME, "legacy_cached_report"))
        for path in self._list_file_candidates(FAST_LIST_FILE_NAME):
            report_candidates.append((path, "fast_list"))
        for bundle_root in bundled_resource_roots():
            report_candidates.append((bundle_root / "mtproxy_seed.json", "bundled_seed"))

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
            self._apply_manual_override_from_config()
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

        for index, raw_line in enumerate(lines, start=1):
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
                    avg_latency_ms=float(index),
                    p95_latency_ms=float(index),
                    min_latency_ms=float(index),
                    max_latency_ms=float(index),
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
        legacy_paths = [
            self.state_root / CONFIG_FILE_NAME,
            self.state_root / "app_state" / CONFIG_FILE_NAME,
            self.install_dir / CONFIG_FILE_NAME,
            self.install_dir / "app_state" / CONFIG_FILE_NAME,
        ]
        if not self.config_path.exists():
            for legacy_path in legacy_paths:
                if not legacy_path.exists():
                    continue
                with contextlib.suppress(Exception):
                    self.config_path.write_text(legacy_path.read_text(encoding="utf-8"), encoding="utf-8")
                    break
        if not self.config_path.exists():
            config = AppConfig()
            self.config_path.write_text(
                json.dumps(self._config_payload(config), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return config
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        valid_keys = set(asdict(AppConfig()).keys())
        data = {key: value for key, value in data.items() if key in valid_keys}
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
        try:
            max_proxies = int(data.get("max_proxies") or 0)
        except (TypeError, ValueError):
            max_proxies = 0
        if "max_proxies" not in data or max_proxies <= 0:
            data["max_proxies"] = DEFAULT_MAX_PROXIES
            normalized = True
        if data.get("telegram_session_file") in (
            "",
            "app_state/telegram_user",
            "app_state/telegram_user.session",
            f"{DATA_DIR_NAME}/telegram_user.sec",
            None,
        ):
            data["telegram_session_file"] = "telegram_user.sec"
            normalized = True
        if "local_fake_tls_enabled" in data:
            data.pop("local_fake_tls_enabled", None)
            normalized = True
        if "local_fake_tls_domain" in data:
            data.pop("local_fake_tls_domain", None)
            normalized = True
        try:
            source_max_age_days = int(data.get("telegram_source_max_age_days") or 0)
        except (TypeError, ValueError):
            source_max_age_days = 0
        if "telegram_source_max_age_days" not in data or source_max_age_days <= 0:
            data["telegram_source_max_age_days"] = DEFAULT_SOURCE_MAX_AGE_DAYS
            normalized = True
        try:
            source_max_messages = int(data.get("telegram_source_max_messages") or 0)
        except (TypeError, ValueError):
            source_max_messages = 0
        if "telegram_source_max_messages" not in data or source_max_messages <= 0:
            data["telegram_source_max_messages"] = DEFAULT_SOURCE_MAX_MESSAGES
            normalized = True
        try:
            source_max_proxies = int(data.get("telegram_source_max_proxies") or 0)
        except (TypeError, ValueError):
            source_max_proxies = 0
        if "telegram_source_max_proxies" not in data or source_max_proxies <= 0:
            data["telegram_source_max_proxies"] = DEFAULT_SOURCE_MAX_PROXIES
            normalized = True
        if "rf_whitelist_check_enabled" not in data:
            data["rf_whitelist_check_enabled"] = False
            normalized = True
        try:
            deep_media_top_n = int(data.get("deep_media_top_n") or 0)
        except (TypeError, ValueError):
            deep_media_top_n = 0
        if "deep_media_top_n" not in data or deep_media_top_n < 0 or deep_media_top_n == 10:
            data["deep_media_top_n"] = DEFAULT_DEEP_MEDIA_TOP_N
            normalized = True
        if "auto_update_enabled" not in data:
            data["auto_update_enabled"] = True
            normalized = True
        if "telegram_api_proxy_url" not in data:
            data["telegram_api_proxy_url"] = DEFAULT_TELEGRAM_API_PROXY_URL
            normalized = True
        if data.get("telegram_api_proxy_url") in (OLD_DEFAULT_TELEGRAM_API_PROXY_URL, OLD_DEFAULT_TELEGRAM_API_PROXY_URL_2):
            data["telegram_api_proxy_url"] = DEFAULT_TELEGRAM_API_PROXY_URL
            normalized = True
        if "telegram_api_proxy_enabled" not in data:
            data["telegram_api_proxy_enabled"] = False
            normalized = True
        cleaned_api_id = _safe_int(data.get("telegram_api_id"))
        if data.get("telegram_api_id") != cleaned_api_id:
            data["telegram_api_id"] = cleaned_api_id
            normalized = True
        cleaned_api_hash = _clean_api_hash(data.get("telegram_api_hash"))
        if data.get("telegram_api_hash") != cleaned_api_hash:
            data["telegram_api_hash"] = cleaned_api_hash
            normalized = True
        persistent_auth = self._load_persistent_telegram_auth() or self._load_legacy_telegram_auth(legacy_paths)
        if persistent_auth:
            for key, value in persistent_auth.items():
                if key == "telegram_api_id":
                    if _safe_int(data.get(key)) <= 0 and _safe_int(value) > 0:
                        data[key] = _safe_int(value)
                        normalized = True
                elif key == "telegram_api_hash":
                    cleaned_value = _clean_api_hash(value)
                    if not _clean_api_hash(data.get(key)) and cleaned_value:
                        data[key] = cleaned_value
                        normalized = True
                elif key in {"telegram_phone", "telegram_api_proxy_url", "telegram_session_file"}:
                    if not str(data.get(key) or "").strip() and str(value or "").strip():
                        data[key] = str(value).strip()
                        normalized = True
                elif key == "telegram_api_proxy_enabled":
                    if key not in data:
                        data[key] = bool(value)
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
        if bool(data.get("telegram_sources_enabled", False)) != bool(data.get("thread_source_enabled", False)):
            data["thread_source_enabled"] = bool(data.get("telegram_sources_enabled", False))
            normalized = True
        sources = [
            str(item).strip()
            for item in data.get("sources", [])
            if str(item).strip() and str(item).strip().lower() not in REMOVED_WEB_SOURCES
        ]
        mtproxytg_mirror_set = {source.lower() for source in MTPROXYTG_MIRRORS}
        compact_sources: list[str] = []
        found_mtproxytg_mirror = False
        for source in sources:
            normalized_source = source.lower()
            if normalized_source in mtproxytg_mirror_set or normalized_source in {"mtproxytg", "mtproxytg-mirrors", MTPROXYTG_MIRROR_GROUP}:
                found_mtproxytg_mirror = True
                continue
            if source not in compact_sources:
                compact_sources.append(source)
        sources = compact_sources
        if found_mtproxytg_mirror or any(source not in compact_sources for source in data.get("sources", [])):
            normalized = True
        if found_mtproxytg_mirror and MTPROXYTG_MIRROR_GROUP not in sources:
            sources.append(MTPROXYTG_MIRROR_GROUP)
        if any(str(item).strip().lower() in REMOVED_WEB_SOURCES for item in data.get("sources", [])):
            normalized = True
        for source in RECOMMENDED_WEB_SOURCE_ADDITIONS:
            if source not in sources:
                sources.append(source)
                normalized = True
        data["sources"] = sources
        xray_sources = []
        for item in data.get("xray_subscription_urls", []):
            source = str(item).strip()
            if not source:
                continue
            if source.startswith("https://mifa.world/vless#"):
                source = "https://mifa.world/vless"
            if source not in xray_sources:
                xray_sources.append(source)
        for source in DEFAULT_XRAY_SUBSCRIPTIONS:
            if source not in xray_sources:
                xray_sources.append(source)
                normalized = True
        data["xray_subscription_urls"] = xray_sources
        telegram_sources = [str(item).strip() for item in data.get("telegram_sources", []) if str(item).strip()]
        for source in RECOMMENDED_TELEGRAM_SOURCE_ADDITIONS:
            if source not in telegram_sources:
                telegram_sources.append(source)
                normalized = True
        data["telegram_sources"] = telegram_sources
        if normalized:
            with contextlib.suppress(Exception):
                normalized_payload = self._config_payload(AppConfig(**(asdict(AppConfig()) | data)))
                self.config_path.write_text(json.dumps(normalized_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        defaults = asdict(AppConfig())
        defaults.update(data)
        config = self._normalize_config(AppConfig(**defaults))
        self._save_persistent_telegram_auth(self._config_payload(config))
        return config

    def _persistent_telegram_auth_path(self) -> Path:
        return self.state_dir / TELEGRAM_AUTH_STATE_FILE_NAME

    def _load_persistent_telegram_auth(self) -> dict[str, Any]:
        auth_path = self._persistent_telegram_auth_path()
        if not auth_path.exists():
            return {}
        try:
            payload = json.loads(auth_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        allowed = {
            "telegram_api_id",
            "telegram_api_hash",
            "telegram_phone",
            "telegram_api_proxy_enabled",
            "telegram_api_proxy_url",
            "telegram_session_file",
        }
        return {key: payload[key] for key in allowed if key in payload}

    def _load_legacy_telegram_auth(self, config_paths: list[Path]) -> dict[str, Any]:
        for config_path in config_paths:
            if not config_path.exists():
                continue
            try:
                payload = json.loads(config_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            api_id = _safe_int(payload.get("telegram_api_id"))
            api_hash = _clean_api_hash(payload.get("telegram_api_hash"))
            if api_id <= 0 or not api_hash:
                continue
            return {
                "telegram_api_id": api_id,
                "telegram_api_hash": api_hash,
                "telegram_phone": str(payload.get("telegram_phone") or "").strip(),
                "telegram_api_proxy_enabled": bool(payload.get("telegram_api_proxy_enabled", False)),
                "telegram_api_proxy_url": str(payload.get("telegram_api_proxy_url") or DEFAULT_TELEGRAM_API_PROXY_URL).strip(),
                "telegram_session_file": Path(str(payload.get("telegram_session_file") or "telegram_user.sec")).name,
            }
        return {}

    def _save_persistent_telegram_auth(self, payload: dict[str, Any]) -> None:
        auth_payload = {
            "telegram_api_id": _safe_int(payload.get("telegram_api_id")),
            "telegram_api_hash": _clean_api_hash(payload.get("telegram_api_hash")),
            "telegram_phone": str(payload.get("telegram_phone") or "").strip(),
            "telegram_api_proxy_enabled": bool(payload.get("telegram_api_proxy_enabled", False)),
            "telegram_api_proxy_url": str(payload.get("telegram_api_proxy_url") or DEFAULT_TELEGRAM_API_PROXY_URL).strip(),
            "telegram_session_file": Path(str(payload.get("telegram_session_file") or "telegram_user.sec")).name,
        }
        with contextlib.suppress(Exception):
            auth_path = self._persistent_telegram_auth_path()
            auth_path.parent.mkdir(parents=True, exist_ok=True)
            auth_path.write_text(json.dumps(auth_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            _hide_windows_path(auth_path.parent)
            _hide_windows_path(auth_path)

    def _working_priority_key(self, outcome: ProbeOutcome) -> tuple[float, float, float, float, str]:
        latency = outcome.avg_latency_ms if outcome.avg_latency_ms is not None else 9_999.0
        pool_row = self.pool.snapshot_by_key(outcome.proxy.key)
        latest_media = self._latest_deep_media_scores.get(outcome.proxy.key)
        media_score = latest_media.score if latest_media is not None else None
        deep_download_kbps = latest_media.download_kbps if latest_media is not None else None
        deep_upload_kbps = latest_media.upload_kbps if latest_media is not None else None
        if media_score is None and pool_row:
            media_score = pool_row.get("deep_media_score")
        if deep_download_kbps is None and pool_row:
            deep_download_kbps = pool_row.get("deep_media_download_kbps")
        if deep_upload_kbps is None and pool_row:
            deep_upload_kbps = pool_row.get("deep_media_upload_kbps")
        if pool_row:
            latency = pool_row.get("live_latency_ms") or pool_row.get("connect_latency_ms") or latency
        media_penalty = -float(media_score) if media_score is not None else 0.0
        deep_download_penalty = -float(deep_download_kbps) if deep_download_kbps is not None else 0.0
        deep_upload_penalty = -float(deep_upload_kbps) if deep_upload_kbps is not None else 0.0
        return (
            deep_download_penalty,
            deep_upload_penalty,
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
        merged = dict(payload or {})
        merged.update(kwargs)
        self._handle_internal_event(event_name, merged)
        if self.event_sink is not None:
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
