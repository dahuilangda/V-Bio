from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

import requests

try:
    from backend.core import config
except Exception:  # pragma: no cover
    config = None


LOGGER = logging.getLogger(__name__)


def _api_base_url() -> str:
    return (os.environ.get("MONITOR_API_URL", "http://localhost:5000") or "http://localhost:5000").strip()


def _monitor_interval_seconds() -> int:
    raw = (os.environ.get("MONITOR_INTERVAL_SECONDS", "300") or "300").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 300
    return max(10, value)


def _api_token() -> Optional[str]:
    if config is None:
        return None
    value = getattr(config, "BOLTZ_API_TOKEN", None)
    text = str(value or "").strip()
    return text or None


def _make_api_request(
    *,
    endpoint: str,
    method: str = "GET",
    data: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Dict[str, Any]]:
    headers: Dict[str, str] = {}
    token = _api_token()
    if token:
        headers["X-API-Token"] = token

    url = f"{_api_base_url()}{endpoint}"
    try:
        if method == "POST":
            headers["Content-Type"] = "application/json"
            resp = requests.post(url, headers=headers, json=data or {}, timeout=30)
        else:
            resp = requests.get(url, headers=headers, timeout=10)

        payload = resp.json() if resp.content else {}
        return resp.status_code == 200, payload
    except Exception as exc:
        LOGGER.error("API request failed: %s (%s)", endpoint, exc)
        return False, {}


def monitor_and_clean_once() -> None:
    LOGGER.info("Running monitor check...")
    ok, health_data = _make_api_request(endpoint="/monitor/health", method="GET")
    if not ok:
        LOGGER.error("Cannot reach API server.")
        return

    if health_data.get("healthy", False):
        LOGGER.info("System healthy.")
        return

    stuck_count = int(health_data.get("stuck_tasks_count", 0) or 0)
    LOGGER.warning("System unhealthy, stuck tasks: %s", stuck_count)
    clean_ok, clean_result = _make_api_request(
        endpoint="/monitor/clean",
        method="POST",
        data={"force": False},
    )
    if not clean_ok:
        LOGGER.error("Auto cleanup failed.")
        return

    data = clean_result.get("data", {}) if isinstance(clean_result, dict) else {}
    LOGGER.info(
        "Auto cleanup done: cleaned_gpus=%s killed_tasks=%s",
        data.get("total_cleaned_gpus", 0),
        data.get("total_killed_tasks", 0),
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - Monitor - %(levelname)s - %(message)s",
    )
    interval = _monitor_interval_seconds()
    LOGGER.info("Task monitor daemon started (interval=%ss).", interval)

    while True:
        try:
            monitor_and_clean_once()
        except Exception:
            LOGGER.exception("Monitor loop failed")
        time.sleep(interval)


if __name__ == "__main__":
    main()

