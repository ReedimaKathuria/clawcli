from __future__ import annotations
"""Input validation, SSRF protection and secret redaction."""
import ipaddress
import os
import re
import socket
from urllib.parse import urlparse

ALLOW_PRIVATE = os.getenv("ALLOW_PRIVATE_HOSTS", "false").lower() == "true"
URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.I)
BARE_DOMAIN_RE = re.compile(r"\b((?:[a-z0-9-]+\.)+[a-z]{2,})(/[^\s]*)?\b", re.I)


class ValidationError(ValueError):
    pass


def extract_url(text: str) -> str | None:
    m = URL_RE.search(text)
    if m:
        return m.group(0).rstrip(".,);]")
    m = BARE_DOMAIN_RE.search(text)
    return f"https://{m.group(0).rstrip('.,);]')}" if m else None


def validate_url(url: str) -> str:
    """Accept only public http(s) URLs. This blocks SSRF to localhost, private ranges and cloud metadata."""
    if not url or len(url) > 2048:
        raise ValidationError("URL is missing or too long.")
    if any(c in url for c in " \n\r\t;|&`$<>\\"):
        raise ValidationError("URL contains forbidden characters.")
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ValidationError("Only http:// and https:// URLs are allowed.")
    if not p.hostname or p.username or p.password:
        raise ValidationError("URL must have a hostname and no embedded credentials.")
    if p.port and p.port not in (80, 443, 8080, 8443):
        raise ValidationError("Non-standard ports are not allowed.")
    if not ALLOW_PRIVATE:
        try:
            infos = socket.getaddrinfo(p.hostname, None)
        except socket.gaierror:
            raise ValidationError(f"Could not resolve host '{p.hostname}'. Is the domain correct?")
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise ValidationError("Internal/private network addresses are blocked (SSRF protection).")
    return url


_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[=:]\s*\S+"),
]


def redact(text: str) -> str:
    """Remove secrets from anything that reaches logs or the chat window."""
    for k, v in os.environ.items():
        if v and len(v) > 8 and any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASS")):
            text = text.replace(v, "[REDACTED]")
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text
