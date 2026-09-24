from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from management_api.jwt_auth import JwtTokenError, decode_login_jwt, issue_login_jwt
from management_api.jwt_clients import JwtClientStore


class JwtIssuanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = JwtClientStore(str(Path(self.temp_dir.name) / "jwt_clients.json"))
        self.client, _secret = self.store.create_client(
            name="External system",
            issuer="navigation",
            audience="vbio",
            max_ttl_seconds=300,
        )

    def test_issued_jwt_round_trips_through_login_validation(self) -> None:
        token, expires_at = issue_login_jwt(self.client)
        claims = decode_login_jwt(token, self.store)

        self.assertEqual(claims["sub"], self.client.client_id)
        self.assertEqual(claims["username"], self.client.client_id)
        self.assertEqual(claims["iss"], "navigation")
        self.assertEqual(claims["aud"], "vbio")
        self.assertEqual(claims["exp"], expires_at)
        self.assertLessEqual(claims["exp"] - claims["iat"], self.client.max_ttl_seconds)

    def test_requested_lifetime_is_capped_by_client_policy(self) -> None:
        token, _expires_at = issue_login_jwt(self.client, ttl_seconds=3600)
        claims = decode_login_jwt(token, self.store)

        self.assertEqual(claims["exp"] - claims["iat"], self.client.max_ttl_seconds)

    def test_disabled_client_cannot_issue_jwt(self) -> None:
        disabled = self.store.update_client(self.client.client_id, {"active": False})
        with self.assertRaisesRegex(JwtTokenError, "not active"):
            issue_login_jwt(disabled)


if __name__ == "__main__":
    unittest.main()

