from __future__ import annotations

import contextlib
import os
import socket
import ssl
import threading
from urllib.error import URLError

try:
    import certifi
except ImportError:  # pragma: no cover
    certifi = None


TELEGRAM_WEB_HOSTS_LINES = [
    "149.154.167.220 my.telegram.org",
    "149.154.167.220 oauth.telegram.org",
    "149.154.167.220 cdn.telesco.pe",
    "149.154.167.220 cdn1.telesco.pe",
    "149.154.167.220 cdn2.telesco.pe",
    "149.154.167.220 cdn3.telesco.pe",
    "149.154.167.220 cdn4.telesco.pe",
    "149.154.167.220 cdn5.telesco.pe",
    "149.154.167.220 core.telegram.org",
    "149.154.167.220 zws4.web.telegram.org",
    "149.154.167.220 zws4-1.web.telegram.org",
    "149.154.167.220 vesta.web.telegram.org",
    "149.154.167.220 vesta-1.web.telegram.org",
    "149.154.167.220 venus-1.web.telegram.org",
    "149.154.167.220 telegram.me",
    "149.154.167.220 telegram.dog",
    "149.154.167.220 telegram.space",
    "149.154.167.220 telesco.pe",
    "149.154.167.220 tg.dev",
    "149.154.167.220 telegram.org",
    "149.154.167.220 t.me",
    "149.154.167.220 api.telegram.org",
    "149.154.167.220 td.telegram.org",
    "149.154.167.220 venus.web.telegram.org",
    "149.154.167.220 web.telegram.org",
    "149.154.167.220 kws2-1.web.telegram.org",
    "149.154.167.220 kws2.web.telegram.org",
    "149.154.167.220 kws4-1.web.telegram.org",
    "149.154.167.220 kws4.web.telegram.org",
    "149.154.167.220 zws2-1.web.telegram.org",
    "149.154.167.220 zws2.web.telegram.org",
]
TELEGRAM_WEB_HOST_MAP = {
    host.lower(): ip
    for line in TELEGRAM_WEB_HOSTS_LINES
    for ip, host in [line.split(maxsplit=1)]
}
_DNS_OVERRIDE_LOCK = threading.RLock()


def create_verified_ssl_context() -> ssl.SSLContext:
    cafile = None
    if certifi is not None:
        try:
            cafile = certifi.where()
        except Exception:
            cafile = None
    if cafile:
        return ssl.create_default_context(cafile=cafile)
    return ssl.create_default_context()


def create_insecure_ssl_context() -> ssl.SSLContext:
    return ssl._create_unverified_context()


def telegram_web_dns_override_enabled() -> bool:
    return os.environ.get("MTPROXY_TELEGRAM_WEB_DNS_OVERRIDE", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


@contextlib.contextmanager
def telegram_web_dns_override(enabled: bool = True):
    if not enabled:
        yield
        return

    original_getaddrinfo = socket.getaddrinfo

    def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        normalized_host = str(host or "").rstrip(".").lower()
        mapped_ip = TELEGRAM_WEB_HOST_MAP.get(normalized_host)
        if mapped_ip:
            return original_getaddrinfo(mapped_ip, port, family, type, proto, flags)
        return original_getaddrinfo(host, port, family, type, proto, flags)

    with _DNS_OVERRIDE_LOCK:
        socket.getaddrinfo = patched_getaddrinfo
        try:
            yield
        finally:
            socket.getaddrinfo = original_getaddrinfo


def is_tls_verification_error(exc: BaseException) -> bool:
    if isinstance(exc, ssl.SSLCertVerificationError):
        return True
    if isinstance(exc, ssl.SSLError) and "CERTIFICATE_VERIFY_FAILED" in str(exc):
        return True
    if isinstance(exc, URLError):
        reason = exc.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            return True
        if isinstance(reason, ssl.SSLError) and "CERTIFICATE_VERIFY_FAILED" in str(reason):
            return True
        if "CERTIFICATE_VERIFY_FAILED" in str(reason):
            return True
    return "CERTIFICATE_VERIFY_FAILED" in str(exc)
