from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from mtproxy_tg_ws import coerce_domain_list, get_link_host, parse_dc_ip_list, proxy_config
from mtproxy_tg_ws.tg_ws_proxy import _run


def _valid_secret(value: str) -> str:
    secret = str(value or "").strip().lower()
    if len(secret) == 32:
        with contextlib.suppress(ValueError):
            bytes.fromhex(secret)
            return secret
    return os.urandom(16).hex()


@dataclass
class TgWsProxyRuntimeConfig:
    host: str = "127.0.0.1"
    port: int = 1443
    secret: str = field(default_factory=lambda: os.urandom(16).hex())
    dc_ip: list[str] = field(default_factory=lambda: ["2:149.154.167.220", "4:149.154.167.220"])
    buf_kb: int = 256
    pool_size: int = 4
    cfproxy_enabled: bool = True
    cfproxy_priority: bool = True
    cfproxy_user_domain: str = ""
    cfproxy_worker_domain: str = ""
    force_test_dc: bool = False
    fake_tls_domain: str = ""
    proxy_protocol: bool = False

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{int(self.port)}"


class TgWsProxyServer:
    def __init__(
        self,
        config: TgWsProxyRuntimeConfig,
        *,
        log_sink: Callable[[str], None] | None = None,
        event_sink: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.config = config
        self.log_sink = log_sink
        self.event_sink = event_sink
        self._thread: threading.Thread | None = None
        self._async_stop: tuple[asyncio.AbstractEventLoop, asyncio.Event] | None = None
        self._lock = threading.RLock()
        self._last_error = ""

    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def local_tg_url(self) -> str:
        link_host = get_link_host(self.config.host)
        if self.config.fake_tls_domain:
            domain_hex = self.config.fake_tls_domain.encode("ascii", errors="ignore").hex()
            secret = f"ee{self.config.secret}{domain_hex}"
        else:
            secret = f"dd{self.config.secret}"
        return f"tg://proxy?server={link_host}&port={int(self.config.port)}&secret={secret}"

    @property
    def local_proxy_url(self) -> str:
        return self.local_tg_url.replace("tg://", "https://t.me/", 1)

    def start(self) -> None:
        with self._lock:
            if self.is_running():
                return
            self._last_error = ""
            self._configure_global_proxy()
            ready = threading.Event()

            def worker() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                stop_event = asyncio.Event()
                self._async_stop = (loop, stop_event)
                ready.set()
                try:
                    loop.run_until_complete(_run(stop_event=stop_event))
                except Exception as exc:  # pragma: no cover - thread boundary
                    self._last_error = str(exc)
                    self._log(f"[tg-ws] stopped with error: {exc}")
                    self._emit("tg_ws_state", running=False, error=str(exc))
                finally:
                    pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
                    for task in pending:
                        task.cancel()
                    if pending:
                        with contextlib.suppress(Exception):
                            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                    with contextlib.suppress(Exception):
                        loop.close()
                    asyncio.set_event_loop(None)
                    self._async_stop = None

            self._thread = threading.Thread(target=worker, daemon=True, name="tg-ws-proxy")
            self._thread.start()
            ready.wait(timeout=2.0)
            self._log(f"[tg-ws] started on {self.config.endpoint}")
            self._emit("tg_ws_state", running=True, endpoint=self.config.endpoint)

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            async_stop = self._async_stop
            thread = self._thread
            if async_stop is not None:
                loop, event = async_stop
                with contextlib.suppress(Exception):
                    loop.call_soon_threadsafe(event.set)
            if thread is not None and thread.is_alive():
                thread.join(timeout=timeout)
            self._thread = None
            self._async_stop = None
            self._emit("tg_ws_state", running=False)

    def _configure_global_proxy(self) -> None:
        cfg = self.config
        proxy_config.host = str(cfg.host or "127.0.0.1").strip() or "127.0.0.1"
        proxy_config.port = int(cfg.port or 1443)
        proxy_config.secret = _valid_secret(cfg.secret)
        self.config.secret = proxy_config.secret
        proxy_config.dc_redirects = parse_dc_ip_list(list(cfg.dc_ip or []))
        proxy_config.buffer_size = max(4, int(cfg.buf_kb or 256)) * 1024
        proxy_config.pool_size = max(0, int(cfg.pool_size or 4))
        proxy_config.fallback_cfproxy = bool(cfg.cfproxy_enabled)
        proxy_config.cfproxy_user_domains = coerce_domain_list(cfg.cfproxy_user_domain)
        proxy_config.cfproxy_worker_domains = coerce_domain_list(cfg.cfproxy_worker_domain)
        proxy_config.force_test_dc = bool(cfg.force_test_dc)
        proxy_config.fake_tls_domain = str(cfg.fake_tls_domain or "").strip()
        proxy_config.proxy_protocol = bool(cfg.proxy_protocol)
        logging.getLogger("tg-mtproto-proxy").setLevel(logging.INFO)

    def _log(self, message: str) -> None:
        if self.log_sink is not None:
            self.log_sink(str(message))

    def _emit(self, event_name: str, **payload: Any) -> None:
        if self.event_sink is not None:
            self.event_sink(event_name, payload)
