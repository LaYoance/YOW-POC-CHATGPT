"""
Tier 2 Adversarial Stress Tests: Milestone 1 Security Isolation & Session Engine.
Empirically stress-tests:
1. Malformed/corrupted cookie injections (unicode junk, missing keys, giant strings).
2. Expired, malformed, or tampered JWTs.
3. Rapid HTTP 401 hammering & circuit breaker infinite loop prevention.
4. Log sanitization robustness across raw JWTs, bearer tokens, cookies, formatted strings, and dicts.
5. Storage isolation tampering: verification of 0700 on .session_data and 0600 on DB files restored and enforced.
"""

import asyncio
import io
import json
import logging
import os
import shutil
import stat
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt

from app.core.logging_filter import SecretMaskingFilter
from app.models.session import (
    Session,
    SessionCredentials,
    SessionStatus,
    SessionStatusResponse,
)
from app.persistence.db import DatabaseManager
from app.persistence.session_repo import SessionRepo
from app.services.session_service import SessionService


class MockCountingHttpClient:
    """Mock HTTP client tracking call count, latency, and status codes."""

    def __init__(self, status_code: int = 200, response_data: dict = None, delay: float = 0.0):
        self.status_code = status_code
        self.response_data = response_data if response_data is not None else {}
        self.delay = delay
        self.call_count = 0
        self.lock = asyncio.Lock()

    async def get(self, url: str, headers: dict = None):
        async with self.lock:
            self.call_count += 1
        if self.delay > 0:
            await asyncio.sleep(self.delay)

        class MockResponse:
            def __init__(self, code, data):
                self.status_code = code
                self._data = data

            def json(self):
                return self._data

        return MockResponse(self.status_code, self.response_data)


class TestAdversarialSessionSecurity(unittest.IsolatedAsyncioTestCase):
    """Adversarial challenger test suite for Milestone 1."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="adv_session_test_")
        self.session_dir = Path(self.test_dir) / ".session_data"
        self.db_path = self.session_dir / "poc.db"

        self.db_mgr = DatabaseManager(db_path=self.db_path)
        self.db_mgr.init_db()
        self.session_repo = SessionRepo(db=self.db_mgr)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # =========================================================================
    # SUITE 1: MALFORMED & CORRUPTED COOKIE INJECTIONS
    # =========================================================================

    def test_adv_cookie_unicode_junk_and_control_chars(self):
        """Verify parser resilience against unicode, emoji, control chars, and null bytes."""
        inputs = [
            # Unicode and emojis
            "Cookie: __Secure-next-auth.session-token=こんにちは世界; oai-device-id=🤖🔥",
            # Null bytes and escapes
            "__Secure-next-auth.session-token=tok\x00en\x01with\x02ctrl; cf_clearance=cf\tval",
            # RTL text
            "__Secure-next-auth.session-token=مرحبا_بالعالم; oai-device-id=שלום",
            # Whitespace and redundant separators
            ";;;   __Secure-next-auth.session-token=clean_token;;;   oai-device-id=dev_1;;;",
            # Degenerate key-value pairs
            "=; ==; =value; key=; key==val; ___;;; __Secure-next-auth.session-token=ok_tok;",
        ]

        for raw in inputs:
            res = SessionService.parse_cookie_input(raw)
            self.assertIsInstance(res, dict, f"Parser failed to return dict for {raw!r}")
            self.assertIn("__Secure-next-auth.session-token", res)

    def test_adv_cookie_missing_keys_and_broken_structures(self):
        """Verify handling of empty inputs, broken JSON, missing fields, and non-dict items."""
        broken_inputs = [
            "",
            "   ",
            None,
            12345,
            [],
            {},
            # Broken JSON syntax
            '{"__Secure-next-auth.session-token": ',
            '[{name: "unquoted"}]',
            # Valid JSON with invalid schema
            json.dumps([1, 2, "not_a_dict"]),
            json.dumps([{"wrong_key": "val"}]),
            json.dumps([{"name": None, "value": None}]),
            json.dumps({"__Secure-next-auth.session-token": None}),
            # Corrupted Netscape formats
            "chatgpt.com\tTRUE\t/\n",  # fewer than 7 fields
            "invalid_netscape_line_without_tabs\n",
        ]

        for raw in broken_inputs:
            # Must not crash
            try:
                res = SessionService.parse_cookie_input(raw)
                self.assertIsInstance(res, dict)
            except Exception as e:
                self.fail(f"parse_cookie_input crashed with {type(e).__name__}: {e} on input {raw!r}")

    async def test_adv_cookie_import_without_session_token_returns_missing(self):
        """Verify import_session_cookies returns MISSING cleanly without session token."""
        service = SessionService(
            session_repo=self.session_repo,
            session_dir=self.session_dir,
            db_path=self.db_path,
        )

        invalid_payloads = [
            "random_cookie=123; other_cookie=456",
            json.dumps([{"name": "unrelated", "value": "xyz"}]),
            json.dumps({"irrelevant": "abc"}),
            "",
        ]

        for payload in invalid_payloads:
            resp = await service.import_session_cookies(payload)
            self.assertEqual(resp.status, SessionStatus.MISSING)
            self.assertFalse(resp.is_valid)
            self.assertEqual(resp.action_required, "RECONNECT")

    def test_adv_cookie_giant_payload_stress(self):
        """Verify performance and memory bounds against giant cookie strings (1 MB and 10k parts)."""
        # 1. Very long token (100,000 characters)
        giant_token = "A" * 100000
        raw_giant = f"__Secure-next-auth.session-token={giant_token}; oai-device-id=dev_1"

        start_t = time.perf_counter()
        parsed = SessionService.parse_cookie_input(raw_giant)
        duration = time.perf_counter() - start_t

        self.assertLess(duration, 0.5, f"Giant cookie parsing took too long: {duration:.4f}s")
        self.assertEqual(parsed.get("__Secure-next-auth.session-token"), giant_token)

        # 2. 10,000 cookie pairs
        many_pairs = "; ".join(f"k_{i}=v_{i}" for i in range(10000))
        start_t = time.perf_counter()
        parsed_many = SessionService.parse_cookie_input(many_pairs)
        duration_many = time.perf_counter() - start_t

        self.assertLess(duration_many, 1.0, f"Parsing 10,000 pairs took too long: {duration_many:.4f}s")
        self.assertEqual(len(parsed_many), 10000)

    # =========================================================================
    # SUITE 2: EXPIRED, MALFORMED, OR TAMPERED JWTS
    # =========================================================================

    def test_adv_jwt_malformed_and_tampered_structures(self):
        """Stress-test decode_jwt_claims with malformed, truncated, and tampered tokens."""
        malformed_tokens = [
            "",
            "   ",
            "not_a_jwt",
            "ey.invalid.base64",
            "ey12345",
            "part1.part2",
            "part1.part2.part3.part4",
            # Base64 non-JSON
            "eyJhbGciOiJIUzI1NiJ9.bm9uX2pzb25fcGF5bG9hZA.signature",
            # Corrupted base64 characters
            "eyJhbGciOiJIUzI1NiJ9.!!!@@@###$$$.sig",
            # Huge string
            "ey" + ("x" * 50000) + ".payload.sig",
        ]

        for tok in malformed_tokens:
            try:
                claims = SessionService.decode_jwt_claims(tok)
                self.assertIsInstance(claims, dict)
                self.assertEqual(claims, {})
            except Exception as e:
                self.fail(f"decode_jwt_claims crashed with {type(e).__name__}: {e} on token {tok!r}")

    def test_adv_jwt_anomalous_claims(self):
        """Verify behavior with bizarre or extreme claim values (future, negative, string exp)."""
        # Token with large exp
        tok_str_exp = jwt.encode({"sub": "u1", "exp": 9999999999}, "key", algorithm="HS256")
        claims = SessionService.decode_jwt_claims(tok_str_exp)
        self.assertEqual(claims.get("exp"), 9999999999)

        # Token with negative exp
        tok_neg_exp = jwt.encode({"sub": "u2", "exp": -100}, "key", algorithm="HS256")
        claims_neg = SessionService.decode_jwt_claims(tok_neg_exp)
        self.assertEqual(claims_neg.get("exp"), -100)

    async def test_adv_jwt_expired_token_import_flaw_proof(self):
        """
        EMPIRICAL PROOF OF FLAW:
        When upstream returns HTTP 200 containing an expired JWT (exp in the past),
        import_session_cookies blindly marks the session VALID instead of EXPIRED.
        """
        past_ts = int(time.time()) - 3600  # Expired 1 hour ago
        expired_jwt = jwt.encode(
            {"sub": "user-expired", "email": "exp@example.com", "exp": past_ts},
            "key",
            algorithm="HS256"
        )

        mock_http = MockCountingHttpClient(
            status_code=200,
            response_data={
                "user": {"id": "user-expired", "name": "Expired User", "email": "exp@example.com"},
                "accessToken": expired_jwt,
                "expires": datetime.fromtimestamp(past_ts, tz=timezone.utc).isoformat()
            }
        )

        service = SessionService(
            session_repo=self.session_repo,
            session_dir=self.session_dir,
            db_path=self.db_path,
            http_client=mock_http
        )

        raw_cookie = "__Secure-next-auth.session-token=sess_token"
        import_resp = await service.import_session_cookies(raw_cookie)

        # REMEDIATED BEHAVIOR:
        # import_session_cookies compares expires_at with now, so it flags EXPIRED:
        self.assertEqual(import_resp.status, SessionStatus.EXPIRED)
        self.assertFalse(import_resp.is_valid)
        self.assertEqual(import_resp.action_required, "RECONNECT")

        # Database must not contain a VALID session:
        db_sess = self.session_repo.get_default()
        if db_sess:
            self.assertNotEqual(db_sess.status, SessionStatus.VALID)

    # =========================================================================
    # SUITE 3: RAPID HTTP 401 HAMMERING & CIRCUIT BREAKER
    # =========================================================================

    async def test_adv_rapid_401_hammering_sequential(self):
        """
        Verify that sequential burst of 100 get_valid_access_token calls on expired token
        triggers EXACTLY 1 upstream refresh attempt when upstream returns 401.
        """
        near_expiry = datetime.now(timezone.utc) + timedelta(seconds=10)
        sess = Session(
            id="default",
            access_token="expiring_token_123",
            cookies_json='{"__Secure-next-auth.session-token": "old_cookie"}',
            status=SessionStatus.VALID,
            expires_at=near_expiry
        )
        self.session_repo.save(sess)

        # Mock server returning 401 Unauthorized
        mock_http = MockCountingHttpClient(status_code=401, response_data={"error": "unauthorized"})

        service = SessionService(
            session_repo=self.session_repo,
            session_dir=self.session_dir,
            db_path=self.db_path,
            http_client=mock_http
        )

        hammer_count = 100
        exceptions_caught = 0

        for _ in range(hammer_count):
            try:
                await service.get_valid_access_token()
            except RuntimeError as e:
                exceptions_caught += 1
                self.assertIn("SESSION_EXPIRED", str(e))

        self.assertEqual(exceptions_caught, hammer_count)
        # CRITICAL CIRCUIT BREAKER INVARIANT:
        # Exactly 1 HTTP call must have been made across all 100 attempts!
        self.assertEqual(
            mock_http.call_count, 1,
            f"Circuit breaker failed: expected 1 upstream call, got {mock_http.call_count}"
        )

    async def test_adv_concurrent_401_hammering(self):
        """
        Verify that 50 concurrent requests hitting an expiring token simultaneously
        do not overwhelm upstream with duplicate refresh calls.
        """
        near_expiry = datetime.now(timezone.utc) + timedelta(seconds=10)
        sess = Session(
            id="default",
            access_token="expiring_token_456",
            cookies_json='{"__Secure-next-auth.session-token": "old_cookie"}',
            status=SessionStatus.VALID,
            expires_at=near_expiry
        )
        self.session_repo.save(sess)

        mock_http = MockCountingHttpClient(
            status_code=401,
            response_data={"error": "unauthorized"},
            delay=0.01
        )

        service = SessionService(
            session_repo=self.session_repo,
            session_dir=self.session_dir,
            db_path=self.db_path,
            http_client=mock_http
        )

        async def worker():
            try:
                await service.get_valid_access_token()
                return "SUCCESS"
            except RuntimeError:
                return "SESSION_EXPIRED"

        results = await asyncio.gather(*(worker() for _ in range(50)))
        self.assertEqual(len(results), 50)
        self.assertTrue(all(r == "SESSION_EXPIRED" for r in results))

        self.assertEqual(
            mock_http.call_count, 1,
            f"Concurrent circuit breaker failed: expected 1 upstream call, got {mock_http.call_count}"
        )

    async def test_adv_circuit_breaker_bypass_on_200_expired_loop_proof(self):
        """
        VERIFICATION OF CIRCUIT BREAKER UNDER HTTP 200 EXPIRED TOKEN:
        When upstream returns HTTP 200 with an expired token during refresh,
        the service flags EXPIRED, does NOT reset refresh_attempt_count to 0,
        and bounds upstream calls to EXACTLY 1 (circuit breaker enforced).
        """
        past_ts = int(time.time()) - 3600
        expired_jwt = jwt.encode({"sub": "u", "exp": past_ts}, "k", algorithm="HS256")

        mock_http = MockCountingHttpClient(
            status_code=200,
            response_data={
                "accessToken": expired_jwt,
                "expires": datetime.fromtimestamp(past_ts, tz=timezone.utc).isoformat()
            }
        )

        # Pre-seed session in DB with near-expiry token
        near_future = datetime.now(timezone.utc) + timedelta(seconds=30)
        sess = Session(
            id="default",
            access_token="expiring_token_near_expiry",
            cookies_json='{"__Secure-next-auth.session-token": "old_cookie"}',
            status=SessionStatus.VALID,
            expires_at=near_future
        )
        self.session_repo.save(sess)

        service = SessionService(
            session_repo=self.session_repo,
            session_dir=self.session_dir,
            db_path=self.db_path,
            http_client=mock_http
        )

        # Fire 5 get_valid_access_token calls
        exceptions_caught = 0
        for _ in range(5):
            try:
                await service.get_valid_access_token()
            except RuntimeError as e:
                exceptions_caught += 1
                self.assertIn("SESSION_EXPIRED", str(e))

        self.assertEqual(exceptions_caught, 5)
        # REMEDIATED BEHAVIOR:
        # Exactly 1 upstream call made, circuit breaker was NOT bypassed!
        self.assertEqual(mock_http.call_count, 1)

    # =========================================================================
    # SUITE 4: LOG SANITIZATION ROBUSTNESS
    # =========================================================================

    def test_adv_log_sanitization_raw_jwt_and_cookies(self):
        """Verify standard JWT, session cookies, and bearer tokens are sanitized."""
        filt = SecretMaskingFilter()
        dummy_jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.super_secret_jwt_signature_here"
        raw_cookie = "__Secure-next-auth.session-token=sess_token_very_secret_xyz123;"

        msg = f"Connecting using {raw_cookie} and auth {dummy_jwt} and Bearer token_secret_val_123"
        sanitized = filt.sanitize(msg)

        self.assertNotIn("super_secret_jwt_signature_here", sanitized)
        self.assertNotIn("sess_token_very_secret_xyz123", sanitized)
        self.assertNotIn("token_secret_val_123", sanitized)
        self.assertIn("[REDACTED_COOKIE]", sanitized)
        self.assertIn("[REDACTED_JWT]", sanitized)
        self.assertIn("[REDACTED_TOKEN]", sanitized)

    def test_adv_log_sanitization_leak_vectors_proof(self):
        """
        VERIFICATION OF LOG SANITIZATION REMEDIATION:
        1. Bearer tokens containing base64 symbols (+, /, =) are masked.
        2. NextAuth chunked cookies (__Secure-next-auth.session-token.0) are masked.
        3. LogRecord with non-string msg (e.g. Exception) is sanitized and masked.
        """
        filt = SecretMaskingFilter()

        # Vector 1: Bearer token with base64 symbols
        bearer_with_b64 = "Bearer abc+def/ghi==1234567890"
        sanitized_b64 = filt.sanitize(bearer_with_b64)
        self.assertNotIn("abc+def/ghi==1234567890", sanitized_b64)
        self.assertIn("Bearer [REDACTED_TOKEN]", sanitized_b64)

        # Vector 2: Chunked session cookie
        chunked_cookie = "__Secure-next-auth.session-token.0=chunked_secret_val_12345"
        sanitized_chunked = filt.sanitize(chunked_cookie)
        self.assertNotIn("chunked_secret_val_12345", sanitized_chunked)
        self.assertIn("__Secure-next-auth.session-token.0=[REDACTED_COOKIE]", sanitized_chunked)

        # Vector 3: LogRecord with Exception object
        dummy_jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.leaked_sig_in_exception"
        record_exc = logging.LogRecord(
            name="test", level=logging.ERROR, pathname=__file__, lineno=1,
            msg=Exception(f"Failed with {dummy_jwt}"), args=(), exc_info=None
        )
        filt.filter(record_exc)
        formatter = logging.Formatter("%(message)s")
        formatted_output = formatter.format(record_exc)
        self.assertNotIn("leaked_sig_in_exception", formatted_output)
        self.assertIn("[REDACTED_JWT]", formatted_output)

    # =========================================================================
    # SUITE 5: STORAGE ISOLATION TAMPERING & RESTORATION
    # =========================================================================

    def test_adv_storage_tampering_and_restoration(self):
        """
        Verify that if permissions on .session_data and DB files are tampered (e.g. 0777, 0666),
        the system detects and actively restores 0700 on the directory and 0600 on files.
        """
        if os.name != "posix":
            self.skipTest("POSIX filesystem permissions test only applies to POSIX OS")

        # 1. Tamper directory permissions to 0777
        os.chmod(self.session_dir, 0o777)
        self.assertEqual(stat.S_IMODE(os.stat(self.session_dir).st_mode), 0o777)

        # 2. Tamper DB file permissions to 0666
        self.assertTrue(self.db_path.exists())
        os.chmod(self.db_path, 0o666)
        self.assertEqual(stat.S_IMODE(os.stat(self.db_path).st_mode), 0o666)

        # 3. Create mock WAL and SHM files and tamper them to 0666
        wal_file = Path(f"{self.db_path}-wal")
        shm_file = Path(f"{self.db_path}-shm")
        wal_file.write_text("wal_data")
        shm_file.write_text("shm_data")
        os.chmod(wal_file, 0o666)
        os.chmod(shm_file, 0o666)
        self.assertEqual(stat.S_IMODE(os.stat(wal_file).st_mode), 0o666)
        self.assertEqual(stat.S_IMODE(os.stat(shm_file).st_mode), 0o666)

        # 4. Trigger restoration via SessionService.enforce_storage_security
        service = SessionService(
            session_repo=self.session_repo,
            session_dir=self.session_dir,
            db_path=self.db_path,
        )
        service.enforce_storage_security()

        # 5. Verify restored permissions
        dir_mode = stat.S_IMODE(os.stat(self.session_dir).st_mode)
        db_mode = stat.S_IMODE(os.stat(self.db_path).st_mode)
        wal_mode = stat.S_IMODE(os.stat(wal_file).st_mode)
        shm_mode = stat.S_IMODE(os.stat(shm_file).st_mode)

        self.assertEqual(dir_mode, 0o700, f"Directory mode not restored: {oct(dir_mode)}")
        self.assertEqual(db_mode, 0o600, f"DB file mode not restored: {oct(db_mode)}")
        self.assertEqual(wal_mode, 0o600, f"WAL file mode not restored: {oct(wal_mode)}")
        self.assertEqual(shm_mode, 0o600, f"SHM file mode not restored: {oct(shm_mode)}")

    def test_adv_storage_restoration_via_db_manager(self):
        """Verify DatabaseManager.enforce_permissions also restores 0700 and 0600."""
        if os.name != "posix":
            self.skipTest("POSIX permissions only")

        os.chmod(self.session_dir, 0o755)
        os.chmod(self.db_path, 0o644)

        self.db_mgr.enforce_permissions()

        self.assertEqual(stat.S_IMODE(os.stat(self.session_dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.db_path).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
