"""
Tier 1 Feature Tests: Session Lifecycle & Local Security Isolation (F01).
Tests satisfy R1, A7, and DISPATCH.md requirements.
"""

import asyncio
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


class MockHttpClient:
    """Mock HTTP client to simulate ChatGPT NextAuth /api/auth/session responses."""

    def __init__(self, status_code: int = 200, response_data: dict = None):
        self.status_code = status_code
        self.response_data = response_data if response_data is not None else {}
        self.call_count = 0
        self.last_headers = {}

    def get(self, url: str, headers: dict = None):
        self.call_count += 1
        self.last_headers = headers or {}

        class MockResponse:
            def __init__(self, code, data):
                self.status_code = code
                self._data = data

            def json(self):
                return self._data

        return MockResponse(self.status_code, self.response_data)


class TestFeatureSession(unittest.IsolatedAsyncioTestCase):
    """Test suite for F01: Session Lifecycle & Local Isolation."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="poc_session_test_")
        self.session_dir = Path(self.test_dir) / ".session_data"
        self.db_path = self.session_dir / "test_session.db"

        # Initialize test database
        self.db_mgr = DatabaseManager(db_path=self.db_path)
        self.db_mgr.init_db()
        self.session_repo = SessionRepo(db=self.db_mgr)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    async def test_session_initial_missing_state(self):
        """On cold start with no session, status must be MISSING."""
        service = SessionService(
            session_repo=self.session_repo,
            session_dir=self.session_dir,
            db_path=self.db_path,
        )

        status_resp = await service.get_current_session_status()
        self.assertEqual(status_resp.status, SessionStatus.MISSING)
        self.assertFalse(status_resp.is_valid)
        self.assertFalse(status_resp.is_authenticated)
        self.assertEqual(status_resp.action_required, "RECONNECT")
        self.assertTrue(len(status_resp.guidance) > 0)
        self.assertIn("reconnect", status_resp.reconnect_guidance.lower())

    def test_cookie_parsing_multi_format(self):
        """Test multi-format cookie parsing across headers, lists, dicts, and Netscape format."""
        # 1. Standard Header format with Cookie: prefix
        raw_header = (
            "Cookie: __Secure-next-auth.session-token=jwt_tok_123; "
            "cf_clearance=cf_clear_456; oai-device-id=dev_789"
        )
        cookies = SessionService.parse_cookie_input(raw_header)
        self.assertEqual(cookies.get("__Secure-next-auth.session-token"), "jwt_tok_123")
        self.assertEqual(cookies.get("cf_clearance"), "cf_clear_456")
        self.assertEqual(cookies.get("oai-device-id"), "dev_789")

        # 2. Semicolon-separated without Cookie: prefix, with quotes
        raw_semi = (
            '__Secure-next-auth.session-token="quoted_token_val"; '
            "__cf_bm=bm_val"
        )
        cookies_semi = SessionService.parse_cookie_input(raw_semi)
        self.assertEqual(cookies_semi.get("__Secure-next-auth.session-token"), "quoted_token_val")
        self.assertEqual(cookies_semi.get("__cf_bm"), "bm_val")

        # 3. JSON Array format from browser extensions
        raw_json_arr = json.dumps([
            {"name": "__Secure-next-auth.session-token", "value": "json_arr_tok"},
            {"name": "cf_clearance", "value": "json_arr_cf"}
        ])
        cookies_json = SessionService.parse_cookie_input(raw_json_arr)
        self.assertEqual(cookies_json.get("__Secure-next-auth.session-token"), "json_arr_tok")
        self.assertEqual(cookies_json.get("cf_clearance"), "json_arr_cf")

        # 4. JSON Dictionary format
        raw_json_dict = json.dumps({"next-auth.session-token": "dev_token_abc"})
        cookies_dict = SessionService.parse_cookie_input(raw_json_dict)
        self.assertEqual(cookies_dict.get("next-auth.session-token"), "dev_token_abc")

        # 5. Netscape tab-delimited cookie file format
        netscape_raw = (
            "# Netscape HTTP Cookie File\n"
            "chatgpt.com\tTRUE\t/\tTRUE\t1790000000\t__Secure-next-auth.session-token\tnetscape_tok_xyz\n"
            "chatgpt.com\tTRUE\t/\tTRUE\t1790000000\toai-device-id\tnetscape_device_1\n"
        )
        cookies_netscape = SessionService.parse_cookie_input(netscape_raw)
        self.assertEqual(cookies_netscape.get("__Secure-next-auth.session-token"), "netscape_tok_xyz")
        self.assertEqual(cookies_netscape.get("oai-device-id"), "netscape_device_1")

    def test_jwt_claims_decoding(self):
        """Decodes JWT claims without signature verification and handles malformed tokens."""
        now_ts = int(time.time())
        exp_ts = now_ts + 3600

        # Construct unsigned test token
        payload = {
            "sub": "user-uuid-12345",
            "email": "tester@example.com",
            "exp": exp_ts,
            "iat": now_ts,
            "https://api.openai.com/auth": {
                "user_id": "user-uuid-12345",
                "email": "tester@example.com",
                "plan_type": "plus"
            }
        }
        token = jwt.encode(payload, "secret-key", algorithm="HS256")

        decoded = SessionService.decode_jwt_claims(token)
        self.assertEqual(decoded.get("sub"), "user-uuid-12345")
        self.assertEqual(decoded.get("email"), "tester@example.com")
        self.assertEqual(decoded.get("exp"), exp_ts)

        # Malformed token test: must return {} without raising unhandled exception
        malformed = SessionService.decode_jwt_claims("not.a.valid.jwt.string")
        self.assertEqual(malformed, {})

    async def test_session_import_and_valid_transition(self):
        """Importing valid session cookies transitions status to VALID and persists session."""
        future_ts = int(time.time()) + 7200
        test_jwt = jwt.encode(
            {"sub": "user-999", "email": "valid@example.com", "exp": future_ts},
            "test-key",
            algorithm="HS256"
        )

        mock_http = MockHttpClient(
            status_code=200,
            response_data={
                "user": {"id": "user-999", "name": "Valid User", "email": "valid@example.com"},
                "accessToken": test_jwt,
                "expires": datetime.fromtimestamp(future_ts, tz=timezone.utc).isoformat()
            }
        )

        service = SessionService(
            session_repo=self.session_repo,
            session_dir=self.session_dir,
            db_path=self.db_path,
            http_client=mock_http
        )

        raw_cookie = f"__Secure-next-auth.session-token=sess_tok; oai-device-id=dev_1"
        res = await service.import_session_cookies(raw_cookie)

        self.assertEqual(res.status, SessionStatus.VALID)
        self.assertTrue(res.is_valid)
        self.assertTrue(res.is_authenticated)
        self.assertEqual(res.user_email, "valid@example.com")
        self.assertEqual(res.user_name, "Valid User")

        # Verify DB persistence
        persisted = self.session_repo.get_default()
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.status, SessionStatus.VALID)
        self.assertEqual(persisted.access_token, test_jwt)
        self.assertEqual(persisted.user_email, "valid@example.com")

        # Verify token retrieval
        retrieved_token = await service.get_valid_access_token()
        self.assertEqual(retrieved_token, test_jwt)

    async def test_session_expiration_transition(self):
        """A session with an expired timestamp transitions to EXPIRED."""
        past_time = datetime.now(timezone.utc) - timedelta(hours=2)
        expired_sess = Session(
            id="default",
            access_token="expired_token",
            cookies_json='{"__Secure-next-auth.session-token": "expired_tok"}',
            status=SessionStatus.VALID,
            expires_at=past_time
        )
        self.session_repo.save(expired_sess)

        service = SessionService(
            session_repo=self.session_repo,
            session_dir=self.session_dir,
            db_path=self.db_path,
        )

        # Check status detects expiration
        status_resp = await service.get_current_session_status()
        self.assertEqual(status_resp.status, SessionStatus.EXPIRED)
        self.assertFalse(status_resp.is_valid)
        self.assertEqual(status_resp.action_required, "RECONNECT")

        # Verify DB was updated
        db_sess = self.session_repo.get_default()
        self.assertEqual(db_sess.status, SessionStatus.EXPIRED)

        # Attempting get_valid_access_token() should raise RuntimeError
        with self.assertRaises(RuntimeError) as ctx:
            await service.get_valid_access_token()
        self.assertIn("SESSION_EXPIRED", str(ctx.exception))

    async def test_session_challenge_required_transition(self):
        """When upstream returns HTTP 403 (Turnstile), status transitions to CHALLENGE_REQUIRED."""
        mock_http = MockHttpClient(status_code=403, response_data={"error": "Cloudflare Turnstile"})

        service = SessionService(
            session_repo=self.session_repo,
            session_dir=self.session_dir,
            db_path=self.db_path,
            http_client=mock_http
        )

        raw_cookie = "__Secure-next-auth.session-token=challenge_token"
        res = await service.import_session_cookies(raw_cookie)

        self.assertEqual(res.status, SessionStatus.CHALLENGE_REQUIRED)
        self.assertFalse(res.is_valid)
        self.assertEqual(res.action_required, "SOLVE_CHALLENGE")

        # Verify DB reflects CHALLENGE_REQUIRED
        db_sess = self.session_repo.get_default()
        self.assertIsNotNone(db_sess)
        self.assertEqual(db_sess.status, SessionStatus.CHALLENGE_REQUIRED)

    async def test_empty_body_or_missing_access_token_handled_as_expired(self):
        """
        NextAuth often returns HTTP 200 with {} or {"expires": ...} on unauthenticated requests.
        The service must detect missing accessToken and flag status as EXPIRED.
        """
        mock_http = MockHttpClient(
            status_code=200,
            response_data={"expires": "2026-10-15T12:00:00.000Z"}  # Missing accessToken!
        )

        service = SessionService(
            session_repo=self.session_repo,
            session_dir=self.session_dir,
            db_path=self.db_path,
            http_client=mock_http
        )

        raw_cookie = "__Secure-next-auth.session-token=stale_tok"
        res = await service.import_session_cookies(raw_cookie)

        self.assertEqual(res.status, SessionStatus.EXPIRED)
        self.assertFalse(res.is_valid)
        self.assertEqual(res.action_required, "RECONNECT")
        self.assertIn("empty access token", res.message)

    def test_storage_permissions_enforcement(self):
        """Verifies POSIX 0700 on session data directory and 0600 on DB file."""
        if os.name != "posix":
            self.skipTest("POSIX filesystem permissions test only applies to POSIX OS")

        service = SessionService(
            session_repo=self.session_repo,
            session_dir=self.session_dir,
            db_path=self.db_path,
        )
        service.enforce_storage_security()

        dir_stat = os.stat(self.session_dir)
        dir_mode = stat.S_IMODE(dir_stat.st_mode)
        self.assertEqual(dir_mode, 0o700, f"Expected 0o700 for session dir, got {oct(dir_mode)}")

        if self.db_path.exists():
            file_stat = os.stat(self.db_path)
            file_mode = stat.S_IMODE(file_stat.st_mode)
            self.assertEqual(file_mode, 0o600, f"Expected 0o600 for DB file, got {oct(file_mode)}")

    def test_git_exclusion_verification(self):
        """Verifies that verify_git_isolation detects .session_data and *.db in .gitignore."""
        repo_root = Path(self.test_dir)
        gitignore = repo_root / ".gitignore"

        service = SessionService(
            session_repo=self.session_repo,
            session_dir=self.session_dir,
            db_path=self.db_path,
        )

        # 1. No .gitignore -> False
        self.assertFalse(service.verify_git_isolation(repo_root=repo_root))

        # 2. Incomplete .gitignore -> False
        gitignore.write_text("*.log\nnode_modules/\n", encoding="utf-8")
        self.assertFalse(service.verify_git_isolation(repo_root=repo_root))

        # 3. Complete .gitignore -> True
        gitignore.write_text(".session_data/\n*.db\n*.sqlite\n.env\n", encoding="utf-8")
        self.assertTrue(service.verify_git_isolation(repo_root=repo_root))

    def test_secret_masking_filter(self):
        """Verifies SecretMaskingFilter intercepts and masks JWTs, bearer tokens, and session cookies."""
        filt = SecretMaskingFilter()

        # Dummy JWT
        dummy_jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.do_not_leak_signature_here"
        msg = f"Connecting with Bearer oauth_secret_token_123 and token {dummy_jwt} and cookie __Secure-next-auth.session-token=secret_sess_val;"

        sanitized = filt.sanitize(msg)
        self.assertNotIn("secret_sess_val", sanitized)
        self.assertNotIn("do_not_leak_signature_here", sanitized)
        self.assertNotIn("oauth_secret_token_123", sanitized)
        self.assertIn("[REDACTED_TOKEN]", sanitized)
        self.assertIn("[REDACTED_COOKIE]", sanitized)
        self.assertIn("[REDACTED_JWT]", sanitized)

        # Logging record filtering test
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname=__file__, lineno=10,
            msg=msg, args=(), exc_info=None
        )
        filt.filter(record)
        self.assertNotIn("secret_sess_val", record.msg)
        self.assertIn("[REDACTED_COOKIE]", record.msg)

    def test_session_credentials_repr_masking(self):
        """Verifies SessionCredentials string representation never leaks access token."""
        creds = SessionCredentials(
            access_token="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.top_secret_token_12345",
            session_token="secret_sess_cookie",
            user_id="user-xyz"
        )
        repr_str = repr(creds)
        str_str = str(creds)

        self.assertNotIn("top_secret_token_12345", repr_str)
        self.assertNotIn("top_secret_token_12345", str_str)
        self.assertIn("user-xyz", repr_str)
        self.assertEqual(creds.masked_access_token(), "eyJh...2345")

    async def test_circuit_breaker_and_infinite_loop_prevention(self):
        """Verifies that token refresh attempts are bounded to 1 and prevent infinite loops."""
        near_expiry = datetime.now(timezone.utc) + timedelta(seconds=30)
        sess = Session(
            id="default",
            access_token="old_near_expiry_token",
            cookies_json='{"__Secure-next-auth.session-token": "old_cookie"}',
            status=SessionStatus.VALID,
            expires_at=near_expiry
        )
        self.session_repo.save(sess)

        # Mock server that rejects refresh (returns 401)
        mock_http = MockHttpClient(status_code=401, response_data={"error": "unauthorized"})

        service = SessionService(
            session_repo=self.session_repo,
            session_dir=self.session_dir,
            db_path=self.db_path,
            http_client=mock_http
        )

        with self.assertRaises(RuntimeError) as ctx:
            await service.get_valid_access_token()

        self.assertIn("SESSION_EXPIRED", str(ctx.exception))
        # Ensure exactly 1 refresh was attempted and not a loop
        self.assertEqual(mock_http.call_count, 1)

    def test_bearer_base64_masking(self):
        """
        Regression test: Bearer tokens with base64 symbols (+, /, =) must be fully masked.
        Prevents credential leakage of base64-encoded or encrypted Bearer tokens into logs.
        """
        filt = SecretMaskingFilter()

        # 1. Bearer token with +, /, = symbols
        token_b64 = "Bearer abc+def/ghi==1234567890"
        sanitized = filt.sanitize(token_b64)
        self.assertNotIn("abc+def", sanitized)
        self.assertNotIn("/ghi==", sanitized)
        self.assertNotIn("1234567890", sanitized)
        self.assertEqual(sanitized, "Bearer [REDACTED_TOKEN]")

        # 2. Case-insensitive and multi-space Bearer header
        token_lower = "bearer   mySecret+Token/Val==999"
        sanitized_lower = filt.sanitize(token_lower)
        self.assertNotIn("mySecret+Token", sanitized_lower)
        self.assertNotIn("/Val==999", sanitized_lower)
        self.assertIn("[REDACTED_TOKEN]", sanitized_lower)

        # 3. Bearer token embedded in compound header string
        compound_header = "Authorization: Bearer topSecret+Payload/Token==; X-Custom=123"
        sanitized_compound = filt.sanitize(compound_header)
        self.assertNotIn("topSecret+Payload", sanitized_compound)
        self.assertIn("Authorization: Bearer [REDACTED_TOKEN]; X-Custom=123", sanitized_compound)

        # 4. Formatted logging record execution
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname=__file__, lineno=10,
            msg="Sending request with Bearer secret+b64/tok==1234567",
            args=(), exc_info=None
        )
        filt.filter(record)
        self.assertNotIn("secret+b64", record.msg)
        self.assertIn("[REDACTED_TOKEN]", record.msg)

    async def test_chunked_cookie_masking_and_ingestion(self):
        """
        Regression test: NextAuth chunked cookies (.0, .1, ...) must be:
        1. Masked in logs by SecretMaskingFilter (no cleartext leakage).
        2. Ingested and sequentially reassembled in SessionService.import_session_cookies().
        """
        filt = SecretMaskingFilter()

        # Part 1: Log Masking Verification
        chunked_cookie_str = (
            "Cookie: __Secure-next-auth.session-token.0=secret_part_0_val; "
            "__Secure-next-auth.session-token.1=secret_part_1_val; "
            "next-auth.session-token.0=dev_secret_part_0"
        )
        sanitized_cookies = filt.sanitize(chunked_cookie_str)
        self.assertNotIn("secret_part_0_val", sanitized_cookies)
        self.assertNotIn("secret_part_1_val", sanitized_cookies)
        self.assertNotIn("dev_secret_part_0", sanitized_cookies)
        self.assertIn("__Secure-next-auth.session-token.0=[REDACTED_COOKIE]", sanitized_cookies)
        self.assertIn("__Secure-next-auth.session-token.1=[REDACTED_COOKIE]", sanitized_cookies)
        self.assertIn("next-auth.session-token.0=[REDACTED_COOKIE]", sanitized_cookies)

        # Part 2: Ingestion & Reassembly Verification
        future_ts = int(time.time()) + 7200
        part0 = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        part1 = "eyJzdWIiOiJjaHVua2VkLXVzZXIiLCJlbWFpbCI6ImNodW5rZWRAZXhhbXBsZS5jb20iLCJleHAiOjE4MDAwMDAwMDB9."
        part2 = "test_signature_for_chunked_token"
        full_jwt = part0 + part1 + part2

        # Provide cookies out-of-order in dictionary / raw header
        chunked_raw = (
            f"__Secure-next-auth.session-token.1={part1}; "
            f"__Secure-next-auth.session-token.0={part0}; "
            f"__Secure-next-auth.session-token.2={part2}; "
            f"oai-device-id=chunk-dev-123"
        )

        mock_http = MockHttpClient(
            status_code=200,
            response_data={
                "user": {"id": "chunked-user", "name": "Chunked User", "email": "chunked@example.com"},
                "accessToken": full_jwt,
                "expires": datetime.fromtimestamp(future_ts, tz=timezone.utc).isoformat()
            }
        )

        service = SessionService(
            session_repo=self.session_repo,
            session_dir=self.session_dir,
            db_path=self.db_path,
            http_client=mock_http
        )

        res = await service.import_session_cookies(chunked_raw)

        # Ingestion assertions
        self.assertEqual(res.status, SessionStatus.VALID)
        self.assertTrue(res.is_valid)
        self.assertEqual(res.user_email, "chunked@example.com")
        self.assertEqual(res.user_id, "chunked-user")

        # Database reassembly assertions
        db_sess = self.session_repo.get_default()
        self.assertIsNotNone(db_sess)
        self.assertEqual(db_sess.status, SessionStatus.VALID)
        self.assertEqual(db_sess.access_token, full_jwt)
        # Check that reassembled base key is stored in session cookies
        self.assertEqual(db_sess.cookies.get("__Secure-next-auth.session-token"), full_jwt)
        self.assertEqual(db_sess.cookies.get("__Secure-next-auth.session-token.0"), part0)
        self.assertEqual(db_sess.cookies.get("__Secure-next-auth.session-token.1"), part1)
        self.assertEqual(db_sess.cookies.get("__Secure-next-auth.session-token.2"), part2)

    def test_logging_percent_d_specifier_no_crash(self):
        """
        Regression test: Standard Python logging with %d, %f, and %s specifiers must
        not crash with TypeError when SecretMaskingFilter is active.
        """
        logger = logging.getLogger("test_percent_d_logger")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()

        log_capture = []
        class ListHandler(logging.Handler):
            def emit(self, record):
                log_capture.append(self.format(record))

        handler = ListHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        handler.addFilter(SecretMaskingFilter())
        logger.addHandler(handler)

        # 1. Format string with %d, %f, %s, %x
        try:
            logger.info("Processed %d items, elapsed: %.2f sec, code: 0x%X, tag: %s", 128, 4.567, 255, "auth_ok")
        except TypeError as e:
            self.fail(f"SecretMaskingFilter crashed on %d format specifier: {e}")

        self.assertEqual(len(log_capture), 1)
        self.assertIn("Processed 128 items, elapsed: 4.57 sec, code: 0xFF, tag: auth_ok", log_capture[0])

        # 2. Format string containing secret in string arg alongside %d
        logger.info("Batch %d: session token is %s", 1, "Bearer secretTokenWithB64+Val/==")
        self.assertEqual(len(log_capture), 2)
        self.assertIn("Batch 1: session token is Bearer [REDACTED_TOKEN]", log_capture[1])
        self.assertNotIn("secretTokenWithB64", log_capture[1])

        # 3. Direct exception logged as record.msg
        logger.error(Exception("Connection failure: Bearer leakedKey1234567890"))
        self.assertEqual(len(log_capture), 3)
        self.assertIn("[REDACTED_TOKEN]", log_capture[2])
        self.assertNotIn("leakedKey1234567890", log_capture[2])

    async def test_expired_jwt_import_rejection(self):
        """
        Regression test: When upstream returns HTTP 200 with an already-expired JWT,
        import_session_cookies must reject it as EXPIRED, must not save status as VALID,
        and must not reset circuit breaker refresh_attempt_count to 0.
        """
        past_ts = int(time.time()) - 3600
        expired_jwt = jwt.encode(
            {"sub": "expired-user", "email": "expired@example.com", "exp": past_ts},
            "secret-key",
            algorithm="HS256"
        )

        mock_http = MockHttpClient(
            status_code=200,
            response_data={
                "user": {"id": "expired-user", "email": "expired@example.com"},
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

        # Set an in-flight refresh attempt count
        service.refresh_attempt_count = 1

        raw_cookie = "__Secure-next-auth.session-token=expired_sess_cookie"
        res = await service.import_session_cookies(raw_cookie)

        # Must be rejected as EXPIRED
        self.assertEqual(res.status, SessionStatus.EXPIRED)
        self.assertFalse(res.is_valid)
        self.assertFalse(res.is_authenticated)
        self.assertEqual(res.action_required, "RECONNECT")

        # Database must NOT be marked VALID
        db_sess = self.session_repo.get_default()
        if db_sess:
            self.assertNotEqual(db_sess.status, SessionStatus.VALID)

        # Circuit breaker refresh count must NOT be reset to 0
        self.assertEqual(service.refresh_attempt_count, 1)

        # Defensive check: non-dict data body must also be safely rejected
        mock_http_list = MockHttpClient(status_code=200, response_data=[])
        service.http_client = mock_http_list
        res_list = await service.import_session_cookies(raw_cookie)
        self.assertEqual(res_list.status, SessionStatus.EXPIRED)
        self.assertFalse(res_list.is_valid)

    async def test_database_status_sync_on_401(self):
        """
        Regression test: When upstream returns HTTP 401 on token refresh or cookie verification,
        the SQLite database record status must be synchronized to EXPIRED, preventing split-brain
        between get_current_session_status() and backend transports.
        """
        near_future = datetime.now(timezone.utc) + timedelta(seconds=30)
        existing_sess = Session(
            id="default",
            access_token="initial_valid_token",
            cookies_json='{"__Secure-next-auth.session-token": "initial_cookie"}',
            status=SessionStatus.VALID,
            expires_at=near_future
        )
        self.session_repo.save(existing_sess)

        # Mock HTTP client returning 401 Unauthorized
        mock_http = MockHttpClient(status_code=401, response_data={"error": "unauthorized"})

        service = SessionService(
            session_repo=self.session_repo,
            session_dir=self.session_dir,
            db_path=self.db_path,
            http_client=mock_http
        )

        # 1. Trigger token retrieval which attempts refresh on near-expiry token
        with self.assertRaises(RuntimeError) as ctx:
            await service.get_valid_access_token()
        self.assertIn("SESSION_EXPIRED", str(ctx.exception))

        # 2. Database record must be updated to EXPIRED
        db_sess = self.session_repo.get_default()
        self.assertIsNotNone(db_sess)
        self.assertEqual(db_sess.status, SessionStatus.EXPIRED)

        # 3. get_current_session_status() must report EXPIRED (no split-brain)
        status_resp = await service.get_current_session_status()
        self.assertEqual(status_resp.status, SessionStatus.EXPIRED)
        self.assertFalse(status_resp.is_valid)
        self.assertFalse(status_resp.is_authenticated)
        self.assertEqual(status_resp.action_required, "RECONNECT")

        # 4. Direct call to import_session_cookies with 401 also ensures DB status is EXPIRED
        self.session_repo.update_status(SessionStatus.VALID)
        res_import = await service.import_session_cookies("__Secure-next-auth.session-token=bad_cookie")
        self.assertEqual(res_import.status, SessionStatus.EXPIRED)
        self.assertEqual(self.session_repo.get_default().status, SessionStatus.EXPIRED)


if __name__ == "__main__":
    unittest.main()
