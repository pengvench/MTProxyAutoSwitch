from __future__ import annotations

import ssl
from urllib.error import URLError

try:
    import certifi
except ImportError:  # pragma: no cover
    certifi = None


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
