from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from management_api.auth_service import AuthService


class FakePostgrest:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls = []
        self.error = error

    def request(self, method, table, **kwargs):
        self.calls.append((method, table, kwargs))
        if self.error:
            raise self.error
        return []


class TokenTableAccessTests(unittest.TestCase):
    def test_readable_token_table_passes(self) -> None:
        postgrest = FakePostgrest()
        AuthService(postgrest).require_token_table_access()
        method, table, kwargs = postgrest.calls[0]
        self.assertEqual((method, table), ("GET", "api_tokens"))
        self.assertEqual(kwargs["query"], {"select": "id", "limit": "1"})

    def test_unreadable_token_table_names_the_cause(self) -> None:
        refused = RuntimeError('PostgREST 401: {"message":"permission denied for table api_tokens"}')
        with self.assertRaisesRegex(RuntimeError, "PGRST_JWT_SECRET") as caught:
            AuthService(FakePostgrest(refused)).require_token_table_access()
        self.assertIs(caught.exception.__cause__, refused)
        self.assertIn("permission denied for table api_tokens", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
