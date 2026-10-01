"""
Verification script for SessionService fix strategy.
Tests:
1. import_session_cookies() rejects expired token when expires_at <= now,
   returns SessionStatus.EXPIRED, is_valid=False, does NOT reset refresh_attempt_count.
2. import_session_cookies() updates DB to SessionStatus.EXPIRED on status_code != 200 and on empty accessToken.
3. get_valid_access_token() updates DB to SessionStatus.EXPIRED before raising RuntimeError.
4. Circuit breaker bounds upstream requests:
   - 100 sequential calls on expired token trigger EXACTLY 1 upstream call.
   - 50 concurrent calls on expired token trigger EXACTLY 1 upstream call.
   - Rapid calls with 200+expired token trigger EXACTLY 1 upstream call (no bypass).
"""

import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt

from app.models.session import Session, SessionStatus, SessionStatusResponse
from app.persistence.db import DatabaseManager
from app.persistence.session_repo import SessionRepo
from app.services.session_service import SessionService


class MockCountingHttpClient:
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


class FixedSessionService(SessionService):
    """
    Subclass of SessionService implementing the exact fix strategy.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._refresh_lock: asyncio.Lock = asyncio.Lock()

    async def import_session_cookies(self, cookies_raw) -> SessionStatusResponse:
        cookies = self.parse_cookie_input(cookies_raw)
        token_key = "__Secure-next-auth.session-token"
        if token_key not in cookies:
            if "next-auth.session-token" in cookies:
                token_key = "next-auth.session-token"
            elif any(k.startswith(f"{token_key}.") for k in cookies):
                chunk_indices = sorted(
                    [int(k.split(".")[-1]) for k in cookies if k.startswith(f"{token_key}.") and k.split(".")[-1].isdigit()]
                )
                cookies[token_key] = "".join(cookies[f"{token_key}.{i}"] for i in chunk_indices)
            elif any(k.startswith("next-auth.session-token.") for k in cookies):
                chunk_indices = sorted(
                    [int(k.split(".")[-1]) for k in cookies if k.startswith("next-auth.session-token.") and k.split(".")[-1].isdigit()]
                )
                cookies["next-auth.session-token"] = "".join(cookies[f"next-auth.session-token.{i}"] for i in chunk_indices)
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

        device_id = cookies.get("oai-device-id") or str("mock-dev-id")
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

        # Requirement 2: If status_code != 200, call self.session_repo.update_status(SessionStatus.EXPIRED) if existing in DB
        if status_code != 200:
            existing = await self._call_repo(self.session_repo.get_default)
            if existing:
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

        if not isinstance(data, dict):
            data = {}

        access_token = data.get("accessToken")
        # Requirement 2: If not access_token, call self.session_repo.update_status(SessionStatus.EXPIRED) if existing in DB
        if not access_token:
            existing = await self._call_repo(self.session_repo.get_default)
            if existing:
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

        user_info = data.get("user") or {}
        auth_claims = claims.get("https://api.openai.com/auth") or {}
        user_email = user_info.get("email") or claims.get("email") or auth_claims.get("email")
        user_name = user_info.get("name")
        user_id = user_info.get("id") or claims.get("sub") or auth_claims.get("user_id")
        plan_type = auth_claims.get("plan_type", "unknown")

        # Requirement 1: If expires_at is present and expires_at <= now, reject with status EXPIRED,
        # is_valid=False, and refresh_attempt_count must NOT be reset.
        if expires_at and expires_at <= now:
            existing = await self._call_repo(self.session_repo.get_default)
            if existing:
                await self._call_repo(self.session_repo.update_status, SessionStatus.EXPIRED)
            return SessionStatusResponse(
                status=SessionStatus.EXPIRED,
                is_valid=False,
                is_authenticated=False,
                user_email=user_email,
                user_name=user_name,
                user_id=user_id,
                plan_type=plan_type,
                expires_at=expires_at,
                last_verified_at=now,
                message="Access token has expired.",
                message_fr="Le jeton d'accès a expiré.",
                action_required="RECONNECT",
                reconnect_guidance="The session token retrieved from upstream has already expired. Please re-authenticate.",
                guidance=[
                    "Click 'Reconnect' to refresh credentials or enter a fresh cookie."
                ]
            )

        # Persist session to SQLite (VALID session)
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
        self.refresh_attempt_count = 0  # Reset circuit breaker ONLY on valid token!

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
            async with self._refresh_lock:
                # Double-check in case another task refreshed it while waiting for lock
                refreshed = await self._call_repo(self.session_repo.get_default)
                if (
                    refreshed
                    and refreshed.access_token
                    and refreshed.status == SessionStatus.VALID
                    and refreshed.expires_at
                    and (refreshed.expires_at - datetime.now(timezone.utc)).total_seconds() >= 60
                ):
                    return refreshed.access_token

                if self.refresh_attempt_count < 1:
                    self.refresh_attempt_count += 1
                    cookies = session.cookies
                    res = await self.import_session_cookies(json.dumps(cookies))
                    if res.is_valid:
                        fresh_session = await self._call_repo(self.session_repo.get_default)
                        if fresh_session and fresh_session.access_token:
                            return fresh_session.access_token

                # Requirement 3: If token refresh fails, call self.session_repo.update_status(SessionStatus.EXPIRED)
                await self._call_repo(self.session_repo.update_status, SessionStatus.EXPIRED)
                raise RuntimeError("SESSION_EXPIRED: Access token expired and automatic refresh failed.")

        if session.status != SessionStatus.VALID:
            raise RuntimeError(f"SESSION_{session.status}: Session is not in VALID state.")

        return session.access_token


class TestFixStrategyVerification(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="fix_strategy_test_")
        self.session_dir = Path(self.test_dir) / ".session_data"
        self.db_path = self.session_dir / "poc.db"
        self.db_mgr = DatabaseManager(db_path=self.db_path)
        self.db_mgr.init_db()
        self.session_repo = SessionRepo(db=self.db_mgr)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    async def test_req1_import_expired_token_rejection(self):
        """Req 1: expires_at <= now must reject with EXPIRED, is_valid=False, and NOT reset refresh_attempt_count."""
        past_ts = int(time.time()) - 3600
        expired_jwt = jwt.encode({"sub": "u", "exp": past_ts}, "k", algorithm="HS256")

        mock_http = MockCountingHttpClient(
            status_code=200,
            response_data={
                "accessToken": expired_jwt,
                "expires": datetime.fromtimestamp(past_ts, tz=timezone.utc).isoformat()
            }
        )
        service = FixedSessionService(
            session_repo=self.session_repo,
            session_dir=self.session_dir,
            db_path=self.db_path,
            http_client=mock_http
        )
        # Pre-set refresh_attempt_count to simulate mid-refresh
        service.refresh_attempt_count = 1

        resp = await service.import_session_cookies("__Secure-next-auth.session-token=tok")

        self.assertEqual(resp.status, SessionStatus.EXPIRED)
        self.assertFalse(resp.is_valid)
        self.assertFalse(resp.is_authenticated)
        # refresh_attempt_count must NOT be reset to 0
        self.assertEqual(service.refresh_attempt_count, 1)

    async def test_req2_import_failure_updates_db_to_expired(self):
        """Req 2: status_code != 200 or not access_token must update existing DB session to EXPIRED."""
        # Seed valid session in DB
        near_future = datetime.now(timezone.utc) + timedelta(seconds=30)
        s = Session(
            id="default",
            access_token="tok",
            cookies_json='{"__Secure-next-auth.session-token": "tok"}',
            status=SessionStatus.VALID,
            expires_at=near_future
        )
        self.session_repo.save(s)
        self.assertEqual(self.session_repo.get_default().status, SessionStatus.VALID)

        # Case A: status_code != 200 (e.g. 401)
        mock_401 = MockCountingHttpClient(status_code=401, response_data={"error": "unauthorized"})
        service_401 = FixedSessionService(
            session_repo=self.session_repo,
            session_dir=self.session_dir,
            db_path=self.db_path,
            http_client=mock_401
        )
        resp_401 = await service_401.import_session_cookies("__Secure-next-auth.session-token=tok")
        self.assertEqual(resp_401.status, SessionStatus.EXPIRED)
        db_s1 = self.session_repo.get_default()
        self.assertEqual(db_s1.status, SessionStatus.EXPIRED, "DB must be updated to EXPIRED on 401")

        # Re-seed VALID session
        self.session_repo.update_status(SessionStatus.VALID)

        # Case B: HTTP 200 but missing access token
        mock_empty = MockCountingHttpClient(status_code=200, response_data={"expires": "2026-10-01T12:00:00Z"})
        service_empty = FixedSessionService(
            session_repo=self.session_repo,
            session_dir=self.session_dir,
            db_path=self.db_path,
            http_client=mock_empty
        )
        resp_empty = await service_empty.import_session_cookies("__Secure-next-auth.session-token=tok")
        self.assertEqual(resp_empty.status, SessionStatus.EXPIRED)
        db_s2 = self.session_repo.get_default()
        self.assertEqual(db_s2.status, SessionStatus.EXPIRED, "DB must be updated to EXPIRED on empty accessToken")

    async def test_req3_get_valid_access_token_updates_db_on_refresh_failure(self):
        """Req 3: In get_valid_access_token(): if token refresh fails, update DB to EXPIRED before raising."""
        near_future = datetime.now(timezone.utc) + timedelta(seconds=30)
        s = Session(
            id="default",
            access_token="expiring_tok",
            cookies_json='{"__Secure-next-auth.session-token": "tok"}',
            status=SessionStatus.VALID,
            expires_at=near_future
        )
        self.session_repo.save(s)

        mock_401 = MockCountingHttpClient(status_code=401, response_data={"error": "unauthorized"})
        service = FixedSessionService(
            session_repo=self.session_repo,
            session_dir=self.session_dir,
            db_path=self.db_path,
            http_client=mock_401
        )

        with self.assertRaises(RuntimeError) as ctx:
            await service.get_valid_access_token()
        self.assertIn("SESSION_EXPIRED", str(ctx.exception))

        # Check DB status is EXPIRED, preventing split-brain
        db_sess = self.session_repo.get_default()
        self.assertEqual(db_sess.status, SessionStatus.EXPIRED)

        # get_current_session_status must report EXPIRED, not VALID
        status_resp = await service.get_current_session_status()
        self.assertEqual(status_resp.status, SessionStatus.EXPIRED)
        self.assertFalse(status_resp.is_valid)

    async def test_req4_circuit_breaker_rapid_calls_on_200_expired(self):
        """Req 4: Rapid calls when upstream returns 200 with expired token must trigger EXACTLY 1 upstream call."""
        past_ts = int(time.time()) - 3600
        expired_jwt = jwt.encode({"sub": "u", "exp": past_ts}, "k", algorithm="HS256")

        mock_http = MockCountingHttpClient(
            status_code=200,
            response_data={
                "accessToken": expired_jwt,
                "expires": datetime.fromtimestamp(past_ts, tz=timezone.utc).isoformat()
            }
        )

        # Pre-seed session in DB
        near_future = datetime.now(timezone.utc) + timedelta(seconds=30)
        s = Session(
            id="default",
            access_token="expiring_tok",
            cookies_json='{"__Secure-next-auth.session-token": "tok"}',
            status=SessionStatus.VALID,
            expires_at=near_future
        )
        self.session_repo.save(s)

        service = FixedSessionService(
            session_repo=self.session_repo,
            session_dir=self.session_dir,
            db_path=self.db_path,
            http_client=mock_http
        )

        # Sequential burst of 50 calls
        caught = 0
        for _ in range(50):
            try:
                await service.get_valid_access_token()
            except RuntimeError as e:
                caught += 1
                self.assertIn("SESSION_EXPIRED", str(e))

        self.assertEqual(caught, 50)
        # CRITICAL ASSERTION: Exactly 1 upstream call made, circuit breaker was NOT bypassed!
        self.assertEqual(mock_http.call_count, 1, f"Expected 1 upstream call, got {mock_http.call_count}")

    async def test_req4_concurrent_calls_on_200_expired(self):
        """Req 4: 50 concurrent calls when upstream returns 200 with expired token trigger EXACTLY 1 upstream call."""
        past_ts = int(time.time()) - 3600
        expired_jwt = jwt.encode({"sub": "u", "exp": past_ts}, "k", algorithm="HS256")

        mock_http = MockCountingHttpClient(
            status_code=200,
            response_data={
                "accessToken": expired_jwt,
                "expires": datetime.fromtimestamp(past_ts, tz=timezone.utc).isoformat()
            },
            delay=0.01
        )

        near_future = datetime.now(timezone.utc) + timedelta(seconds=30)
        s = Session(
            id="default",
            access_token="expiring_tok",
            cookies_json='{"__Secure-next-auth.session-token": "tok"}',
            status=SessionStatus.VALID,
            expires_at=near_future
        )
        self.session_repo.save(s)

        service = FixedSessionService(
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
        self.assertEqual(mock_http.call_count, 1, f"Expected 1 upstream call, got {mock_http.call_count}")

    async def test_successful_refresh_resets_breaker_and_returns_fresh_token(self):
        """Verify happy-path refresh: fresh token resets circuit breaker, updates DB to VALID, and returns token."""
        future_ts = int(time.time()) + 7200
        fresh_jwt = jwt.encode({"sub": "u", "exp": future_ts}, "k", algorithm="HS256")

        mock_http = MockCountingHttpClient(
            status_code=200,
            response_data={
                "accessToken": fresh_jwt,
                "expires": datetime.fromtimestamp(future_ts, tz=timezone.utc).isoformat()
            }
        )

        # Pre-seed session expiring in 30 seconds
        near_future = datetime.now(timezone.utc) + timedelta(seconds=30)
        s = Session(
            id="default",
            access_token="old_expiring_token",
            cookies_json='{"__Secure-next-auth.session-token": "tok"}',
            status=SessionStatus.VALID,
            expires_at=near_future
        )
        self.session_repo.save(s)

        service = FixedSessionService(
            session_repo=self.session_repo,
            session_dir=self.session_dir,
            db_path=self.db_path,
            http_client=mock_http
        )

        token = await service.get_valid_access_token()
        self.assertEqual(token, fresh_jwt)
        self.assertEqual(mock_http.call_count, 1)
        self.assertEqual(service.refresh_attempt_count, 0, "Circuit breaker must reset on valid fresh token")

        # Subsequent 10 calls use cached token without network request
        for _ in range(10):
            t = await service.get_valid_access_token()
            self.assertEqual(t, fresh_jwt)
        self.assertEqual(mock_http.call_count, 1, "Subsequent calls must not hit network when token is fresh")


if __name__ == "__main__":
    unittest.main()

