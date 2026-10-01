"""
Scratch validation script for Explorer M1 Iteration 2.
Tests proposed fixes and the 5 regression test cases in isolation before delivering handoff.
"""

import asyncio
import io
import json
import logging
import re
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import jwt

from app.core.logging_filter import SecretMaskingFilter as OriginalSecretMaskingFilter
from app.models.session import (
    Session,
    SessionCredentials,
    SessionStatus,
    SessionStatusResponse,
)
from app.persistence.db import DatabaseManager
from app.persistence.session_repo import SessionRepo
from app.services.session_service import SessionService as OriginalSessionService


# =========================================================================
# PROPOSED FIXED IMPLEMENTATIONS
# =========================================================================

class ProposedSecretMaskingFilter(logging.Filter):
    """Proposed fix for SecretMaskingFilter."""

    JWT_PATTERN = re.compile(r'ey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}')
    BEARER_PATTERN = re.compile(r'(Bearer\s+)[A-Za-z0-9_\-\.\+\/=]{10,}', re.IGNORECASE)
    COOKIE_SESSION_PATTERN = re.compile(r'(__Secure-next-auth\.session-token(?:\.\d+)?=)[^;,\s]{6,}', re.IGNORECASE)
    COOKIE_DEV_PATTERN = re.compile(r'(next-auth\.session-token(?:\.\d+)?=)[^;,\s]{6,}', re.IGNORECASE)
    CF_CLEARANCE_PATTERN = re.compile(r'(cf_clearance=)[^;,\s]{6,}', re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = self.sanitize(record.getMessage())
            record.args = ()
        except Exception:
            if isinstance(record.msg, str):
                record.msg = self.sanitize(record.msg)
            elif record.msg is not None:
                record.msg = self.sanitize(str(record.msg))
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {
                        self.sanitize(str(k)): (self.sanitize(v) if isinstance(v, str) else v)
                        for k, v in record.args.items()
                    }
                elif isinstance(record.args, tuple):
                    record.args = tuple(
                        self.sanitize(a) if isinstance(a, str) else a
                        for a in record.args
                    )
        return True

    @classmethod
    def sanitize(cls, text: str) -> str:
        if not isinstance(text, str):
            return text
        text = cls.BEARER_PATTERN.sub(r'\g<1>[REDACTED_TOKEN]', text)
        text = cls.COOKIE_SESSION_PATTERN.sub(r'\g<1>[REDACTED_COOKIE]', text)
        text = cls.COOKIE_DEV_PATTERN.sub(r'\g<1>[REDACTED_COOKIE]', text)
        text = cls.CF_CLEARANCE_PATTERN.sub(r'\g<1>[REDACTED_COOKIE]', text)
        text = cls.JWT_PATTERN.sub(lambda m: f"{m.group(0)[:6]}...[REDACTED_JWT]...{m.group(0)[-4:]}", text)
        return text


class ProposedSessionService(OriginalSessionService):
    """Proposed fix for SessionService."""

    @staticmethod
    def reassemble_cookie_chunks(cookies: Dict[str, str], base_key: str) -> Optional[str]:
        pattern = re.compile(rf"^{re.escape(base_key)}\.(\d+)$")
        chunks = []
        for k, v in cookies.items():
            m = pattern.match(k)
            if m:
                chunks.append((int(m.group(1)), v))
        if not chunks:
            return None
        chunks.sort(key=lambda x: x[0])
        return "".join(v for _, v in chunks)

    async def get_current_session_status(self) -> SessionStatusResponse:
        session = await self._call_repo(self.session_repo.get_default)
        if not session or session.status == SessionStatus.MISSING or not session.access_token:
            return SessionStatusResponse(
                status=SessionStatus.MISSING,
                is_valid=False,
                is_authenticated=False,
                message="No session configured. Please import cookies or launch login wizard.",
                message_fr="Aucune session configurée. Veuillez importer les cookies ou lancer l'assistant de connexion.",
                action_required="RECONNECT",
                reconnect_guidance=(
                    "Paste your __Secure-next-auth.session-token cookie into the reconnect modal "
                    "or launch the interactive browser login wizard."
                ),
                guidance=[
                    "Option 1: Paste __Secure-next-auth.session-token cookie into the Reconnect Modal.",
                    "Option 2: Launch the interactive browser login wizard."
                ]
            )

        now = datetime.now(timezone.utc)
        if session.status == SessionStatus.EXPIRED or (session.expires_at and session.expires_at <= now):
            if session.status != SessionStatus.EXPIRED:
                await self._call_repo(self.session_repo.update_status, SessionStatus.EXPIRED)
            return SessionStatusResponse(
                status=SessionStatus.EXPIRED,
                is_valid=False,
                is_authenticated=False,
                user_email=session.user_email,
                user_name=session.user_name,
                user_id=session.account_id,
                expires_at=session.expires_at,
                last_verified_at=session.last_verified_at,
                message="Session has expired. Automatic or manual reconnection required.",
                message_fr="La session a expiré. Une reconnexion automatique ou manuelle est requise.",
                action_required="RECONNECT",
                reconnect_guidance="Your session token has expired. Please re-authenticate or import fresh cookies.",
                guidance=[
                    "Click 'Reconnect' to refresh credentials or enter a fresh cookie."
                ]
            )

        is_valid = (session.status == SessionStatus.VALID)
        return SessionStatusResponse(
            status=session.status,
            is_valid=is_valid,
            is_authenticated=is_valid,
            user_email=session.user_email,
            user_name=session.user_name,
            user_id=session.account_id,
            expires_at=session.expires_at,
            last_verified_at=session.last_verified_at,
            message="Session is valid and active." if is_valid else f"Session state is {session.status}.",
            message_fr="Session valide et active." if is_valid else f"État de session: {session.status}."
        )

    async def import_session_cookies(self, cookies_raw: Union[str, dict, list]) -> SessionStatusResponse:
        cookies = self.parse_cookie_input(cookies_raw)

        # Detect chunked cookies and reassemble
        for base_key in ("__Secure-next-auth.session-token", "next-auth.session-token"):
            if base_key not in cookies or not cookies[base_key]:
                reassembled = self.reassemble_cookie_chunks(cookies, base_key)
                if reassembled:
                    cookies[base_key] = reassembled

        token_key = "__Secure-next-auth.session-token"
        if token_key not in cookies or not cookies[token_key]:
            if "next-auth.session-token" in cookies and cookies["next-auth.session-token"]:
                token_key = "next-auth.session-token"
            else:
                return SessionStatusResponse(
                    status=SessionStatus.MISSING,
                    is_valid=False,
                    is_authenticated=False,
                    message="Missing required session token cookie: '__Secure-next-auth.session-token'.",
                    message_fr="Cookie de session '__Secure-next-auth.session-token' manquant.",
                    action_required="RECONNECT",
                    reconnect_guidance="Cookie '__Secure-next-auth.session-token' was not found in the input.",
                    guidance=[
                        "Ensure you copy cookies from chatgpt.com via Developer Tools (F12 -> Application/Storage -> Cookies)."
                    ]
                )

        device_id = cookies.get("oai-device-id") or str(uuid.uuid4()) if hasattr(self, 'uuid') else str(time.time())
        cookies["oai-device-id"] = device_id

        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Cookie": cookie_header,
            "Referer": f"{self.base_url}/",
        }

        url = f"{self.base_url}/api/auth/session"
        status_code, data = await self._execute_http_get(url, headers)

        # Defensive check: ensure data is a dictionary
        if not isinstance(data, dict):
            data = {}

        if status_code == 403:
            existing = await self._call_repo(self.session_repo.get_default)
            if existing:
                await self._call_repo(self.session_repo.update_status, SessionStatus.CHALLENGE_REQUIRED)
            else:
                challenge_sess = Session(
                    id="default",
                    cookies_json=json.dumps(cookies),
                    user_agent=self.user_agent,
                    device_id=device_id,
                    status=SessionStatus.CHALLENGE_REQUIRED,
                )
                await self._call_repo(self.session_repo.save, challenge_sess)
            return SessionStatusResponse(
                status=SessionStatus.CHALLENGE_REQUIRED,
                is_valid=False,
                is_authenticated=False,
                message="Cloudflare Turnstile challenge detected (HTTP 403). Interactive browser verification required.",
                message_fr="Défi Cloudflare Turnstile détecté (HTTP 403). Vérification par navigateur requise.",
                action_required="SOLVE_CHALLENGE",
                reconnect_guidance="Cloudflare challenge encountered. Please launch the interactive browser login."
            )

        if status_code != 200:
            existing = await self._call_repo(self.session_repo.get_default)
            if existing and existing.status != SessionStatus.EXPIRED:
                await self._call_repo(self.session_repo.update_status, SessionStatus.EXPIRED)
            return SessionStatusResponse(
                status=SessionStatus.EXPIRED,
                is_valid=False,
                is_authenticated=False,
                message=f"Session rejected by server with HTTP {status_code}.",
                message_fr=f"Session rejetée par le serveur (HTTP {status_code}).",
                action_required="RECONNECT",
                reconnect_guidance=f"Authentication server rejected session credentials with code {status_code}."
            )

        access_token = data.get("accessToken")
        if not access_token:
            existing = await self._call_repo(self.session_repo.get_default)
            if existing and existing.status != SessionStatus.EXPIRED:
                await self._call_repo(self.session_repo.update_status, SessionStatus.EXPIRED)
            return SessionStatusResponse(
                status=SessionStatus.EXPIRED,
                is_valid=False,
                is_authenticated=False,
                message="Session cookie is recognized but returned an empty access token. Session has expired.",
                message_fr="Cookie reconnu mais jeton d'accès vide. La session a expiré.",
                action_required="RECONNECT",
                reconnect_guidance="Upstream returned no active access token. Please re-login to chatgpt.com."
            )

        # Decode JWT claims
        claims = self.decode_jwt_claims(access_token)
        exp_ts = claims.get("exp")
        now = datetime.now(timezone.utc)
        if exp_ts:
            expires_at = datetime.fromtimestamp(exp_ts, tz=timezone.utc)
        elif data.get("expires"):
            try:
                expires_at = datetime.fromisoformat(data["expires"].replace("Z", "+00:00"))
            except Exception:
                expires_at = None
        else:
            expires_at = None

        # Expiration guard: reject if token has already expired
        if expires_at and expires_at <= now:
            existing = await self._call_repo(self.session_repo.get_default)
            if existing and existing.status != SessionStatus.EXPIRED:
                await self._call_repo(self.session_repo.update_status, SessionStatus.EXPIRED)
            return SessionStatusResponse(
                status=SessionStatus.EXPIRED,
                is_valid=False,
                is_authenticated=False,
                message="Access token has expired.",
                message_fr="Le jeton d'accès a expiré.",
                action_required="RECONNECT",
                reconnect_guidance="Upstream returned an expired access token. Please re-authenticate."
            )

        user_info = data.get("user") or {}
        auth_claims = claims.get("https://api.openai.com/auth") or {}
        user_email = user_info.get("email") or claims.get("email") or auth_claims.get("email")
        user_name = user_info.get("name")
        user_id = user_info.get("id") or claims.get("sub") or auth_claims.get("user_id")
        plan_type = auth_claims.get("plan_type", "unknown")

        # Persist session to SQLite
        session = Session(
            id="default",
            access_token=access_token,
            cookies_json=json.dumps(cookies),
            user_agent=self.user_agent,
            device_id=device_id,
            account_id=user_id,
            user_email=user_email,
            user_name=user_name,
            status=SessionStatus.VALID,
            expires_at=expires_at,
            last_verified_at=now,
        )
        await self._call_repo(self.session_repo.save, session)
        self.enforce_storage_security()
        self.refresh_attempt_count = 0  # Reset circuit breaker on valid token

        return SessionStatusResponse(
            status=SessionStatus.VALID,
            is_valid=True,
            is_authenticated=True,
            user_email=user_email,
            user_name=user_name,
            user_id=user_id,
            plan_type=plan_type,
            expires_at=expires_at,
            last_verified_at=now,
            message="Session successfully authenticated and stored.",
            message_fr="Session authentifiée et enregistrée avec succès."
        )

    async def get_valid_access_token(self) -> str:
        session = await self._call_repo(self.session_repo.get_default)
        if not session or not session.access_token:
            raise RuntimeError("SESSION_MISSING: No session active. Please authenticate.")

        now = datetime.now(timezone.utc)
        if session.expires_at and (session.expires_at - now).total_seconds() < 60:
            if self.refresh_attempt_count < 1:
                self.refresh_attempt_count += 1
                cookies = session.cookies
                res = await self.import_session_cookies(json.dumps(cookies))
                if res.is_valid:
                    fresh_session = await self._call_repo(self.session_repo.get_default)
                    return fresh_session.access_token
            # Failed to refresh: update DB to EXPIRED
            await self._call_repo(self.session_repo.update_status, SessionStatus.EXPIRED)
            raise RuntimeError("SESSION_EXPIRED: Access token expired and automatic refresh failed.")

        if session.status != SessionStatus.VALID:
            raise RuntimeError(f"SESSION_{session.status}: Session is not in VALID state.")

        return session.access_token


# =========================================================================
# REGRESSION TEST SUITE EXECUTION
# =========================================================================

class MockHttpClient:
    def __init__(self, status_code: int = 200, response_data: Any = None):
        self.status_code = status_code
        self.response_data = response_data if response_data is not None else {}
        self.call_count = 0

    def get(self, url: str, headers: dict = None):
        self.call_count += 1
        class MockResponse:
            def __init__(self, code, data):
                self.status_code = code
                self._data = data
            def json(self):
                return self._data
        return MockResponse(self.status_code, self.response_data)


class TestRegressionM1It2(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.session_dir = Path(self.td.name) / ".session_data"
        self.db_path = self.session_dir / "test.db"
        self.db_mgr = DatabaseManager(self.db_path)
        self.db_mgr.init_db()
        self.session_repo = SessionRepo(self.db_mgr)

    def tearDown(self):
        self.td.cleanup()

    def test_bearer_base64_masking(self):
        filt = ProposedSecretMaskingFilter()
        token_b64 = "Bearer abc+def/ghi==1234567890"
        sanitized = filt.sanitize(token_b64)
        self.assertNotIn("abc+def", sanitized)
        self.assertNotIn("/ghi==", sanitized)
        self.assertNotIn("1234567890", sanitized)
        self.assertIn("Bearer [REDACTED_TOKEN]", sanitized)

        token_lower = "bearer   mySecret+Token/Val==999"
        sanitized_lower = filt.sanitize(token_lower)
        self.assertNotIn("mySecret+Token", sanitized_lower)
        self.assertNotIn("/Val==999", sanitized_lower)
        self.assertIn("[REDACTED_TOKEN]", sanitized_lower)

        compound_header = "Authorization: Bearer topSecret+Payload/Token==; X-Custom=123"
        sanitized_compound = filt.sanitize(compound_header)
        self.assertNotIn("topSecret+Payload", sanitized_compound)
        self.assertIn("Authorization: Bearer [REDACTED_TOKEN]; X-Custom=123", sanitized_compound)

        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname=__file__, lineno=10,
            msg="Sending request with Bearer secret+b64/tok==1234567",
            args=(), exc_info=None
        )
        filt.filter(record)
        self.assertNotIn("secret+b64", record.msg)
        self.assertIn("[REDACTED_TOKEN]", record.msg)

    async def test_chunked_cookie_masking_and_ingestion(self):
        filt = ProposedSecretMaskingFilter()
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

        future_ts = int(time.time()) + 7200
        part0 = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        part1 = "eyJzdWIiOiJjaHVua2VkLXVzZXIiLCJlbWFpbCI6ImNodW5rZWRAZXhhbXBsZS5jb20iLCJleHAiOjE4MDAwMDAwMDB9."
        part2 = "test_signature_for_chunked_token"
        full_jwt = part0 + part1 + part2

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

        service = ProposedSessionService(
            session_repo=self.session_repo,
            session_dir=self.session_dir,
            db_path=self.db_path,
            http_client=mock_http
        )

        res = await service.import_session_cookies(chunked_raw)

        self.assertEqual(res.status, SessionStatus.VALID)
        self.assertTrue(res.is_valid)
        self.assertEqual(res.user_email, "chunked@example.com")
        self.assertEqual(res.user_id, "chunked-user")

        db_sess = self.session_repo.get_default()
        self.assertIsNotNone(db_sess)
        self.assertEqual(db_sess.status, SessionStatus.VALID)
        self.assertEqual(db_sess.access_token, full_jwt)
        self.assertEqual(db_sess.cookies.get("__Secure-next-auth.session-token"), full_jwt)
        self.assertEqual(db_sess.cookies.get("__Secure-next-auth.session-token.0"), part0)
        self.assertEqual(db_sess.cookies.get("__Secure-next-auth.session-token.1"), part1)
        self.assertEqual(db_sess.cookies.get("__Secure-next-auth.session-token.2"), part2)

    def test_logging_percent_d_specifier_no_crash(self):
        logger = logging.getLogger("test_percent_d_logger_scratch")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()

        log_capture = []
        class ListHandler(logging.Handler):
            def emit(self, record):
                log_capture.append(self.format(record))

        handler = ListHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        handler.addFilter(ProposedSecretMaskingFilter())
        logger.addHandler(handler)

        try:
            logger.info("Processed %d items, elapsed: %.2f sec, code: 0x%X, tag: %s", 128, 4.567, 255, "auth_ok")
        except TypeError as e:
            self.fail(f"SecretMaskingFilter crashed on %d format specifier: {e}")

        self.assertEqual(len(log_capture), 1)
        self.assertIn("Processed 128 items, elapsed: 4.57 sec, code: 0xFF, tag: auth_ok", log_capture[0])

        logger.info("Batch %d: session token is %s", 1, "Bearer secretTokenWithB64+Val/==")
        self.assertEqual(len(log_capture), 2)
        self.assertIn("Batch 1: session token is Bearer [REDACTED_TOKEN]", log_capture[1])
        self.assertNotIn("secretTokenWithB64", log_capture[1])

        logger.error(Exception("Connection failure: Bearer leakedKey1234567890"))
        self.assertEqual(len(log_capture), 3)
        self.assertIn("[REDACTED_TOKEN]", log_capture[2])
        self.assertNotIn("leakedKey1234567890", log_capture[2])

    async def test_expired_jwt_import_rejection(self):
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

        service = ProposedSessionService(
            session_repo=self.session_repo,
            session_dir=self.session_dir,
            db_path=self.db_path,
            http_client=mock_http
        )

        service.refresh_attempt_count = 1

        raw_cookie = "__Secure-next-auth.session-token=expired_sess_cookie"
        res = await service.import_session_cookies(raw_cookie)

        self.assertEqual(res.status, SessionStatus.EXPIRED)
        self.assertFalse(res.is_valid)
        self.assertFalse(res.is_authenticated)
        self.assertEqual(res.action_required, "RECONNECT")

        db_sess = self.session_repo.get_default()
        if db_sess:
            self.assertNotEqual(db_sess.status, SessionStatus.VALID)

        self.assertEqual(service.refresh_attempt_count, 1)

        # Defensive non-dict test
        mock_http_list = MockHttpClient(status_code=200, response_data=[])
        service.http_client = mock_http_list
        res_list = await service.import_session_cookies(raw_cookie)
        self.assertEqual(res_list.status, SessionStatus.EXPIRED)
        self.assertFalse(res_list.is_valid)

    async def test_database_status_sync_on_401(self):
        near_future = datetime.now(timezone.utc) + timedelta(seconds=30)
        existing_sess = Session(
            id="default",
            access_token="initial_valid_token",
            cookies_json='{"__Secure-next-auth.session-token": "initial_cookie"}',
            status=SessionStatus.VALID,
            expires_at=near_future
        )
        self.session_repo.save(existing_sess)

        mock_http = MockHttpClient(status_code=401, response_data={"error": "unauthorized"})

        service = ProposedSessionService(
            session_repo=self.session_repo,
            session_dir=self.session_dir,
            db_path=self.db_path,
            http_client=mock_http
        )

        with self.assertRaises(RuntimeError) as ctx:
            await service.get_valid_access_token()
        self.assertIn("SESSION_EXPIRED", str(ctx.exception))

        db_sess = self.session_repo.get_default()
        self.assertIsNotNone(db_sess)
        self.assertEqual(db_sess.status, SessionStatus.EXPIRED)

        status_resp = await service.get_current_session_status()
        self.assertEqual(status_resp.status, SessionStatus.EXPIRED)
        self.assertFalse(status_resp.is_valid)
        self.assertFalse(status_resp.is_authenticated)
        self.assertEqual(status_resp.action_required, "RECONNECT")

        self.session_repo.update_status(SessionStatus.VALID)
        res_import = await service.import_session_cookies("__Secure-next-auth.session-token=bad_cookie")
        self.assertEqual(res_import.status, SessionStatus.EXPIRED)
        self.assertEqual(self.session_repo.get_default().status, SessionStatus.EXPIRED)


if __name__ == "__main__":
    unittest.main()
