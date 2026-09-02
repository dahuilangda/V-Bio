"""Runtime settings persistence and live-apply for the Copilot.

Users configure an outbound proxy, LLM server URL, API key, and model through the
Copilot UI.  These are stored in a server-side JSON file (the API key is never
fully exposed to the browser) and applied live to the running ``CopilotAssistant``
/ ``CopilotCompleter`` singletons without a server restart.

The settings are **global** (one configuration per deployment) because the LLM
client and HTTP session are shared singletons.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests

try:
    import fcntl  # POSIX only — used for cross-process file locking
except ImportError:
    fcntl = None  # type: ignore[assignment]

SETTINGS_FILE = os.environ.get("VBIO_COPILOT_SETTINGS_FILE", "frontend/.run/copilot_settings.json").strip()

# A small, stable UniProt entry used purely for proxy connectivity checks.
_UNIPROT_PROBE_URL = "https://rest.uniprot.org/uniprotkb/P12345.json"
_TEST_TIMEOUT_SECONDS = 15.0

# Thread-level lock for the read-modify-write cycle within one process.
_settings_lock = threading.Lock()

# Tracks the last-known mtime of the settings file so other gunicorn workers can detect
# that a save happened and hot-reload without a restart.
_settings_last_mtime: float = 0.0
_settings_reload_lock = threading.Lock()

_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h", "socks4"}
_URL_SCHEMES = {"http", "https"}


# --------------------------------------------------------------------------- #
#  Persistence                                                                #
# --------------------------------------------------------------------------- #

def load_saved_settings() -> Dict[str, Any]:
    """Load persisted settings from the JSON file, or ``{}`` when missing/invalid."""
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        # A corrupt settings file silently reverted proxy/API-key/model to env defaults
        # with zero trace — the operator had no way to know why the config "stopped
        # working". Degrade the same way, but say so on stderr.
        print(
            f"[copilot_settings] failed to load {SETTINGS_FILE} ({exc}); "
            "falling back to environment defaults",
            file=sys.stderr,
        )
        return {}


def save_settings(data: Dict[str, Any]) -> None:
    """Atomically persist *data* to the settings file (write-temp then rename)."""
    directory = os.path.dirname(SETTINGS_FILE) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp_path, SETTINGS_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
#  Helpers                                                                     #
# --------------------------------------------------------------------------- #

def mask_key(key: str) -> str:
    """Mask an API key for browser display: show only the last 4 characters."""
    key = str(key or "")
    if not key:
        return ""
    if len(key) <= 4:
        return "•" * len(key)
    return "•" * (len(key) - 4) + key[-4:]


def build_proxies(proxy_str: str) -> Optional[Dict[str, str]]:
    """Convert a proxy URL string into a ``requests``-compatible proxies dict.

    Returns ``None`` when the proxy string is empty.  The pooled session has
    ``trust_env = False`` so ``None`` reliably means "direct connection" — no
    environment-variable proxy can sneak in.
    """
    proxy = str(proxy_str or "").strip()
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def validate_proxy_url(url: str) -> str:
    """Validate a proxy URL scheme. Returns the cleaned URL, or raises ``ValueError``.

    If the user entered just ``host:port`` without a scheme, ``http://`` is auto-prepended.
    """
    url = str(url or "").strip()
    if not url:
        return ""
    # Auto-prepend http:// when no scheme delimiter is present.  Checking for "://" is more
    # reliable than urlparse, which mis-parses "hostname:port" as scheme="hostname".
    if "://" not in url:
        url = "http://" + url
    scheme = (urlparse(url).scheme or "").lower()
    if scheme not in _PROXY_SCHEMES:
        raise ValueError(
            f"Proxy URL scheme must be one of {sorted(_PROXY_SCHEMES)} (got: '{scheme or 'none'})."
        )
    return url


def validate_api_url(url: str) -> str:
    """Validate an LLM API URL scheme. Returns the cleaned URL, or raises ``ValueError``."""
    url = str(url or "").strip()
    if not url:
        return ""
    scheme = (urlparse(url).scheme or "").lower()
    if scheme not in _URL_SCHEMES:
        raise ValueError(
            f"LLM server URL scheme must be http or https (got: '{scheme or 'none'})."
        )
    return url


def merge_and_save(updates: Dict[str, Any]) -> Dict[str, Any]:
    """Atomically merge *updates* into the persisted settings file.

    Uses a cross-process file lock (``fcntl.flock`` on a dedicated lock file) so that
    concurrent saves from different gunicorn workers don't silently clobber each other.
    ``proxy`` / ``api_url`` / ``model`` are replaced (empty string clears the override).
    ``api_key`` is only updated when a non-empty value is supplied.
    """
    with _settings_lock:
        lock_path = SETTINGS_FILE + ".lock"
        os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            current = load_saved_settings()
            for field in ("proxy", "api_url", "model"):
                if field in updates:
                    current[field] = str(updates.get(field) or "").strip()
            new_key = str(updates.get("api_key") or "").strip()
            if new_key:
                current["api_key"] = new_key
            save_settings(current)
        finally:
            if fcntl is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        return current


def reload_settings_if_changed() -> Optional[Dict[str, Any]]:
    """Check if the settings file changed on disk since the last load.

    Returns the new settings dict if the file's mtime advanced, or ``None`` if unchanged.
    Called by each gunicorn worker before handling a Copilot request, so a save in
    worker A is picked up by worker B on its very next request — without a restart.
    """
    global _settings_last_mtime
    try:
        mtime = os.path.getmtime(SETTINGS_FILE)
    except OSError:
        return None
    if mtime <= _settings_last_mtime:
        return None
    with _settings_reload_lock:
        try:
            mtime = os.path.getmtime(SETTINGS_FILE)
        except OSError:
            return None
        if mtime <= _settings_last_mtime:
            return None
        settings = load_saved_settings()
        _settings_last_mtime = mtime
        return settings


def public_view(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Build a browser-safe representation (API key masked, never raw)."""
    raw_key = str(settings.get("api_key") or "")
    return {
        "proxy": str(settings.get("proxy") or ""),
        "api_url": str(settings.get("api_url") or ""),
        "model": str(settings.get("model") or ""),
        "api_key_masked": mask_key(raw_key),
        "has_api_key": bool(raw_key),
    }


def mark_settings_reloaded() -> None:
    """Bump the mtime cache so the current worker doesn't redundantly re-apply on its next request.

    Call this after an in-worker save+apply so that ``reload_settings_if_changed`` doesn't
    detect the just-written file as "changed" and re-apply the same settings a second time.
    """
    global _settings_last_mtime
    try:
        _settings_last_mtime = os.path.getmtime(SETTINGS_FILE)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
#  Live apply                                                                  #
# --------------------------------------------------------------------------- #

def apply_runtime_overrides(assistant: Any, completer: Any, settings: Dict[str, Any]) -> None:
    """Apply saved settings to the live Copilot singletons.

    ``proxy`` is always applied (``None`` clears a previously-set proxy).
    Empty ``api_url`` / ``api_key`` / ``model`` revert each singleton to its env-var
    default (stored at ``__init__`` time), so clearing a field in the UI correctly
    restores the original configuration.
    """
    proxies = build_proxies(str(settings.get("proxy") or ""))
    api_url = str(settings.get("api_url") or "").strip()
    api_key = str(settings.get("api_key") or "").strip()
    model = str(settings.get("model") or "").strip()
    for obj in (assistant, completer):
        if obj is None or not hasattr(obj, "update_runtime_overrides"):
            continue
        obj.update_runtime_overrides(
            proxies=proxies,
            chat_api_url=api_url,
            chat_api_key=api_key,
            chat_model=model,
        )


# --------------------------------------------------------------------------- #
#  Connectivity test                                                           #
# --------------------------------------------------------------------------- #

def test_connectivity(
    session: requests.Session,
    *,
    proxy: str = "",
    api_url: str = "",
    api_key: str = "",
    model: str = "",
) -> Dict[str, Any]:
    """Test proxy (UniProt reachability) and LLM endpoint connectivity.

    Each sub-test is independent — one failure does not block the other.
    """
    proxies = build_proxies(proxy)
    return {
        "proxy": _test_proxy(session, proxies),
        "llm": _test_llm(session, str(api_url or "").strip(), str(api_key or "").strip(),
                         str(model or "").strip(), proxies),
    }


def _test_proxy(session: requests.Session, proxies: Optional[Dict[str, str]]) -> Dict[str, Any]:
    start = time.monotonic()
    try:
        resp = session.get(
            _UNIPROT_PROBE_URL,
            headers={"Accept": "application/json", "User-Agent": "vbio-copilot-settings-test"},
            timeout=_TEST_TIMEOUT_SECONDS,
            proxies=proxies,
        )
        ms = int((time.monotonic() - start) * 1000)
        if resp.ok:
            return {"ok": True, "detail": f"UniProt reachable ({ms} ms)", "ms": ms}
        return {"ok": False, "detail": f"UniProt returned HTTP {resp.status_code}", "ms": ms}
    except Exception as exc:
        ms = int((time.monotonic() - start) * 1000)
        return {"ok": False, "detail": f"Connection failed: {str(exc)[:200]}", "ms": ms}


def _test_llm(
    session: requests.Session,
    api_url: str,
    api_key: str,
    model: str,
    proxies: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    # Only test what is actually configured: an empty URL / key / model is reported
    # as "skipped" (neutral) instead of firing a request that is guaranteed to fail —
    # e.g. OpenAI returns 401 when no bearer key is sent and 400 for an unknown model.
    missing = [
        label
        for label, value in (
            ("LLM server URL", api_url),
            ("API key", api_key),
            ("model", model),
        )
        if not value
    ]
    if missing:
        return {"ok": True, "detail": f"Skipped — {', '.join(missing)} not configured", "ms": 0, "skipped": True}
    headers: Dict[str, str] = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    body: Dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
        "temperature": 0,
    }
    start = time.monotonic()
    try:
        # Direct call — mirrors production: the chat endpoint never rides the database proxy.
        resp = session.post(api_url, headers=headers, json=body, timeout=_TEST_TIMEOUT_SECONDS)
        ms = int((time.monotonic() - start) * 1000)
        if resp.ok:
            return {"ok": True, "detail": f"LLM responded OK ({ms} ms)", "ms": ms}
        return {
            "ok": False,
            "detail": f"LLM returned HTTP {resp.status_code}: {str(getattr(resp, 'text', '') or '')[:200]}",
            "ms": ms,
        }
    except Exception as exc:
        ms = int((time.monotonic() - start) * 1000)
        return {"ok": False, "detail": f"Connection failed: {str(exc)[:200]}", "ms": ms}
