"""Shared outbound-error sanitizer.

The /turn JSON route sanitizes exceptions before returning them (no internal hosts, paths, or
stack traces reach the client). The SSE streaming route sends error frames through a queue that
cannot call back into the Flask app module — both import this single implementation so the two
transports can never drift.
"""

from __future__ import annotations

import re

_SENSITIVE_PATTERNS = (
    re.compile(r"https?://[^\s'\"<>]+"),
    re.compile(r"/[A-Za-z0-9_.\-]+/[^\s'\"<>]*"),
    # requests' exception text embeds host='10.x.x.x', port=NNNN — internal topology.
    re.compile(r"host='[^']*'"),
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?"),
)


def safe_error_message(exc: BaseException, *, default_msg: str = "Internal server error") -> str:
    """A client-safe one-line error: known-safe message text, else the generic default."""
    text = " ".join(str(exc).split())[:500]
    if not text:
        return default_msg
    # Apply EVERY pattern: the host/IP regexes were compiled but never substituted, so
    # requests' "HTTPSConnectionPool(host='10.20.3.4', port=8080)" shipped internal
    # topology verbatim in SSE error frames.
    sanitized = text
    for pattern, replacement in (
        (_SENSITIVE_PATTERNS[0], "<url>"),
        (_SENSITIVE_PATTERNS[1], "<path>"),
        (_SENSITIVE_PATTERNS[2], "<host>"),
        (_SENSITIVE_PATTERNS[3], "<ip>"),
    ):
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized or default_msg
