"""Outbound-error sanitizer — internal topology must never reach the client.

Production gap this suite pins: safe_error_message compiled host/IP redaction regexes but
never applied them, so requests' exception text ("HTTPSConnectionPool(host='10.x.x.x',
port=8080)…") shipped verbatim in SSE error frames.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from management_api.copilot_error import safe_error_message


class SafeErrorTests(unittest.TestCase):
    def test_internal_host_and_ip_are_redacted(self) -> None:
        raw = (
            "HTTPSConnectionPool(host='10.20.3.4', port=8080): Max retries exceeded "
            "with url: http://10.20.3.4:8080/v1/chat/completions (Caused by ConnectTimeoutError)"
        )
        out = safe_error_message(Exception(raw))
        self.assertNotIn("10.20.3.4", out)
        self.assertNotIn("host='", out)
        self.assertNotIn("http://", out)

    def test_path_segments_are_redacted(self) -> None:
        out = safe_error_message(Exception("failed reading /etc/passwd and /data/V-Bio/secrets"))
        self.assertNotIn("/data/V-Bio", out)

    def test_plain_message_passes_through(self) -> None:
        self.assertEqual(safe_error_message(Exception("model timeout")), "model timeout")

    def test_empty_message_falls_back_to_default(self) -> None:
        self.assertEqual(safe_error_message(Exception("")), "Internal server error")


if __name__ == "__main__":
    unittest.main()
