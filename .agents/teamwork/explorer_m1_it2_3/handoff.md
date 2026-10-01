# Milestone 1 Iteration 2: NextAuth Cookie Chunking & Regression Tests — Strategy & Verification Report

**Explorer**: Explorer M1-It2 (`explorer_m1_it2_3`)  
**Roles**: explorer, investigator, test architect  
**Parent Orchestrator**: `cc1b08b2-2f82-49cf-9e45-b8c933af6ebf`  
**Target Subagent**: Worker M1 (`worker_m1`)  
**Working Directory**: `/var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/.agents/teamwork/explorer_m1_it2_3`  
**Date**: 2026-10-01T11:45:00Z  

---

## Executive Summary
This investigation formulates the complete fix strategy and regression test suite for Milestone 1 Iteration 2 addressing:
1. **NextAuth Cookie Chunking (.0, .1, ...)** reassembly in `SessionService.import_session_cookies()`.
2. **Defensive non-dict payload protection** (`if not isinstance(data, dict): data = {}`).
3. **Comprehensive design of 5 regression unit test cases** in `tests/test_tier1_features/test_feature_session.py`.
4. **Discovered state sync bug**: `get_current_session_status()` omitting `action_required="RECONNECT"` when DB status is `EXPIRED` if JWT `exp` timestamp is still in the future.
All proposed code and tests were empirically verified in an isolated test harness (`test_scratch_validation.py`), passing 5/5 tests in 0.310s.

---

## 1. Observation

### 1.1 NextAuth Cookie Chunking Defect
- **File**: `app/services/session_service.py`, Lines 282-300:
  ```python
  cookies = self.parse_cookie_input(cookies_raw)
  token_key = "__Secure-next-auth.session-token"
  if token_key not in cookies:
      if "next-auth.session-token" in cookies:
          token_key = "next-auth.session-token"
      else:
          return SessionStatusResponse(
              status=SessionStatus.MISSING, ...
          )
  ```
- **Observed Behavior**:
  Browsers and export extensions split cookies larger than 4KB into chunked keys:
  `__Secure-next-auth.session-token.0`, `__Secure-next-auth.session-token.1`, etc.
  `parse_cookie_input()` correctly populates the dictionary with these chunked keys, but `import_session_cookies()` directly looks up `__Secure-next-auth.session-token` or `next-auth.session-token`.
  Failing to find the unchunked base key, `import_session_cookies()` immediately rejects the valid session as `SessionStatus.MISSING`.
- **Command & Output**:
  ```bash
  python3 -c '
  from app.services.session_service import SessionService
  c = SessionService.parse_cookie_input("__Secure-next-auth.session-token.0=part0; __Secure-next-auth.session-token.1=part1")
  print("Parsed keys:", list(c.keys()))
  assert "__Secure-next-auth.session-token" in c
  '
  # AssertionError: "__Secure-next-auth.session-token" not in c
  ```

### 1.2 Upstream Non-Dict JSON Response Crash
- **File**: `app/services/session_service.py`, Line 314 & 350:
  ```python
  status_code, data = await self._execute_http_get(url, headers)
  ...
  access_token = data.get("accessToken")
  ```
- **Observed Behavior**:
  If upstream returns HTTP 200 with a non-dict body (such as `[]`, string, or null), `data.get("accessToken")` crashes with `AttributeError: 'list' object has no attribute 'get'`.

### 1.3 State Synchronization Gap on 401 & Premature Expiration
- **File**: `app/services/session_service.py`, Lines 210-234:
  ```python
  now = datetime.now(timezone.utc)
  if session.expires_at and session.expires_at <= now:
      if session.status != SessionStatus.EXPIRED:
          await self._call_repo(self.session_repo.update_status, SessionStatus.EXPIRED)
      return SessionStatusResponse(
          status=SessionStatus.EXPIRED,
          action_required="RECONNECT", ...
      )

  is_valid = (session.status == SessionStatus.VALID)
  return SessionStatusResponse(
      status=session.status,
      is_valid=is_valid,
      ...
  )
  ```
- **Observed Behavior**:
  When upstream returns HTTP 401 on an unexpired token (e.g. session revoked on chatgpt.com), the DB status is set to `EXPIRED`. However, `get_current_session_status()` only evaluated `session.expires_at <= now`. Because `expires_at` was still in the future, it bypassed the expiration branch and executed line 233, returning `action_required=None` without reconnection guidance.

### 1.4 Logging Filter Vulnerabilities
- **File**: `app/core/logging_filter.py`, Lines 17-29:
  ```python
  BEARER_PATTERN = re.compile(r'(Bearer\s+)[A-Za-z0-9_\-\.]{10,}', re.IGNORECASE)
  COOKIE_SESSION_PATTERN = re.compile(r'(__Secure-next-auth\.session-token=)[^;,\s]{6,}', re.IGNORECASE)
  ...
  elif isinstance(record.args, tuple):
      record.args = tuple(self.sanitize(str(a)) for a in record.args)
  ```
- **Observed Behavior**:
  - `BEARER_PATTERN` leaked base64 characters `+`, `/`, `=`.
  - `COOKIE_SESSION_PATTERN` ignored chunked cookies `.0`, `.1`.
  - Stringifying args caused `%d` specifiers to crash with `TypeError: %d format: a real number is required, not str`.
  - Exception objects logged as `record.msg` bypassed `isinstance(record.msg, str)` and printed secrets in plaintext.

---

## 2. Logic Chain

1. **R1 (Security & Isolation) & A7 (Session Recovery) Invariants**:
   - Secrets must never leak to logs under any representation (base64 Bearer, chunked cookies, exception objects).
   - Logging filters must never cause unhandled application crashes (`TypeError` on `%d`).
   - Valid sessions exported with NextAuth cookie chunking must be transparently reassembled without user friction.
   - Any session rejection by upstream (HTTP 401, expired JWT) must reliably update SQLite state to `EXPIRED` and present actionable reconnection instructions (`action_required="RECONNECT"`).
2. **Sequential Reassembly Strategy**:
   - In NextAuth, chunks are named `<cookieName>.<index>` where `<index>` is `0, 1, 2, ...`.
   - Matching keys against `rf"^{re.escape(base_key)}\.(\d+)$"` and sorting tuples by `int(index)` ensures correct numerical sequence regardless of dictionary iteration order or chunk counts exceeding 10.
   - Setting `cookies[base_key] = reassembled` guarantees that all downstream code (`token_key in cookies`, Playwright cookie setters, curl_cffi headers) receives the unified, valid token while preserving the original chunk keys in SQLite.
3. **Defensive Body Guard**:
   - Inserting `if not isinstance(data, dict): data = {}` immediately following `_execute_http_get()` guarantees type safety for all subsequent `.get()` calls without changing response semantics.
4. **State Machine Cohesion**:
   - Updating `get_current_session_status()` to check `if session.status == SessionStatus.EXPIRED or (session.expires_at and session.expires_at <= now):` ensures that whenever the DB record is `EXPIRED` (via 401 or expiration), the UI receives `action_required="RECONNECT"` and guidance.

---

## 3. Caveats

1. **Chunk Integrity**: NextAuth chunking assumes contiguous 0-indexed chunks (`.0`, `.1`, ...). If a user manually provides a broken subset (e.g. only `.1` with `.0` missing), the reassembled token will fail upstream JWT validation at `/api/auth/session`, which is cleanly handled as an invalid/expired session.
2. **Challenger 2 Vulnerability Proof Tests**: In `tests/test_tier2_boundaries/test_adversarial_m1_security.py`, Challenger 2 authored two proof tests (`test_adv_log_sanitization_leak_vectors_proof` and `test_adv_circuit_breaker_bypass_on_200_expired_loop_proof`) that assert that the vulnerabilities *exist* (`assertIn(...)` and `call_count == 6`). When Worker M1 applies these fixes, those two proof tests will fail unless inverted to assert the secured behavior (`assertNotIn(...)` and `call_count == 1`).

---

## 4. Conclusion & Recommended Implementation Plan

### 4.1 Implementation Code for `app/services/session_service.py`

#### Change 1: Add `reassemble_cookie_chunks` helper
Add this static method to `SessionService`:
```python
    @staticmethod
    def reassemble_cookie_chunks(cookies: Dict[str, str], base_key: str) -> Optional[str]:
        """
        Detects chunked cookies (${base_key}.0, ${base_key}.1, ...) and reassembles them
        into the full token value in correct numerical sequence.
        """
        pattern = re.compile(rf"^{re.escape(base_key)}\.(\d+)$")
        chunks: List[Tuple[int, str]] = []
        for k, v in cookies.items():
            m = pattern.match(k)
            if m:
                chunks.append((int(m.group(1)), v))
        if not chunks:
            return None
        # Sort numerically by chunk index (e.g. 0, 1, 2... 10)
        chunks.sort(key=lambda x: x[0])
        return "".join(v for _, v in chunks)
```

#### Change 2: Update `import_session_cookies()` in `app/services/session_service.py`
Replace lines 282-362 with the following verified logic:
```python
    async def import_session_cookies(self, cookies_raw: Union[str, dict, list]) -> SessionStatusResponse:
        """
        Parses raw cookies, reassembles chunked tokens, verifies against /api/auth/session,
        and persists session. Enforces 1-refresh circuit breaker and safe secret isolation.
        """
        cookies = self.parse_cookie_input(cookies_raw)

        # 1. Detect and reassemble NextAuth chunked cookies (.0, .1, ...)
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

        device_id = cookies.get("oai-device-id") or str(uuid.uuid4())
        cookies["oai-device-id"] = device_id

        # Verify against upstream session endpoint
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Cookie": cookie_header,
            "Referer": f"{self.base_url}/",
        }

        url = f"{self.base_url}/api/auth/session"
        status_code, data = await self._execute_http_get(url, headers)

        # 2. Defensive check: ensure data is a dictionary
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

        # 3. Guard against expired JWT returned under HTTP 200
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
        self.refresh_attempt_count = 0  # Only reset circuit breaker on verified fresh token

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
```

#### Change 3: Update `get_current_session_status()` in `app/services/session_service.py`
In line 211, update the condition to also check `session.status == SessionStatus.EXPIRED`:
```python
        now = datetime.now(timezone.utc)
        if session.status == SessionStatus.EXPIRED or (session.expires_at and session.expires_at <= now):
            # Token is expired locally or upstream invalidated
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
```

#### Change 4: Update `get_valid_access_token()` in `app/services/session_service.py`
In lines 427-436, ensure failed refresh updates DB to `EXPIRED`:
```python
        now = datetime.now(timezone.utc)
        if session.expires_at and (session.expires_at - now).total_seconds() < 60:
            if self.refresh_attempt_count < 1:
                self.refresh_attempt_count += 1
                cookies = session.cookies
                res = await self.import_session_cookies(json.dumps(cookies))
                if res.is_valid:
                    fresh_session = await self._call_repo(self.session_repo.get_default)
                    return fresh_session.access_token
            # Refresh failed: synchronize DB status to EXPIRED
            await self._call_repo(self.session_repo.update_status, SessionStatus.EXPIRED)
            raise RuntimeError("SESSION_EXPIRED: Access token expired and automatic refresh failed.")
```

---

### 4.2 Implementation Code for `app/core/logging_filter.py`
```python
class SecretMaskingFilter(logging.Filter):
    """
    Log filter that guarantees zero tokens, cookies, or credentials leak to log files or console.
    """

    JWT_PATTERN = re.compile(r'ey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}')
    BEARER_PATTERN = re.compile(r'(Bearer\s+)[A-Za-z0-9_\-\.\+\/=]{10,}', re.IGNORECASE)
    COOKIE_SESSION_PATTERN = re.compile(r'(__Secure-next-auth\.session-token(?:\.\d+)?=)[^;,\s]{6,}', re.IGNORECASE)
    COOKIE_DEV_PATTERN = re.compile(r'(next-auth\.session-token(?:\.\d+)?=)[^;,\s]{6,}', re.IGNORECASE)
    CF_CLEARANCE_PATTERN = re.compile(r'(cf_clearance=)[^;,\s]{6,}', re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # Format message with native types preserving %d, %f before sanitizing
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
        """Sanitizes text by replacing sensitive tokens with masked equivalents."""
        if not isinstance(text, str):
            return text
        text = cls.BEARER_PATTERN.sub(r'\g<1>[REDACTED_TOKEN]', text)
        text = cls.COOKIE_SESSION_PATTERN.sub(r'\g<1>[REDACTED_COOKIE]', text)
        text = cls.COOKIE_DEV_PATTERN.sub(r'\g<1>[REDACTED_COOKIE]', text)
        text = cls.CF_CLEARANCE_PATTERN.sub(r'\g<1>[REDACTED_COOKIE]', text)
        text = cls.JWT_PATTERN.sub(lambda m: f"{m.group(0)[:6]}...[REDACTED_JWT]...{m.group(0)[-4:]}", text)
        return text
```

---

### 4.3 Five Verified Regression Unit Tests for `tests/test_tier1_features/test_feature_session.py`

Append these 5 test methods to class `TestFeatureSession`:

```python
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
        self.assertIn("Bearer [REDACTED_TOKEN]", sanitized)

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
```

---

## 5. Verification Method

### 5.1 Independent Test Execution Command
Execute the scratch validation suite containing the exact proposed implementations and the 5 regression tests:
```bash
cd /var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity
PYTHONPATH=. python3 .agents/teamwork/explorer_m1_it2_3/test_scratch_validation.py -v
```
**Empirical Result**:
```
test_bearer_base64_masking (__main__.TestRegressionM1It2.test_bearer_base64_masking) ... ok
test_chunked_cookie_masking_and_ingestion (__main__.TestRegressionM1It2.test_chunked_cookie_masking_and_ingestion) ... ok
test_database_status_sync_on_401 (__main__.TestRegressionM1It2.test_database_status_sync_on_401) ... ok
test_expired_jwt_import_rejection (__main__.TestRegressionM1It2.test_expired_jwt_import_rejection) ... ok
test_logging_percent_d_specifier_no_crash (__main__.TestRegressionM1It2.test_logging_percent_d_specifier_no_crash) ... ok

----------------------------------------------------------------------
Ran 5 tests in 0.310s

OK
```

### 5.2 Verification After Implementation by Worker M1
Once Worker M1 incorporates the changes into `app/` and `tests/test_tier1_features/test_feature_session.py`:
```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```
Expected output: All Tier 1 tests (now 17 tests) and Tier 2 boundary tests pass with 0 failures and 0 errors.

### 5.3 Invalidation Conditions
This report's strategy is invalidated if:
1. NextAuth chunked cookie index sorting fails on non-contiguous chunk ranges (e.g. gaps in index).
2. `record.getMessage()` fails to evaluate valid logging format strings under custom logging formatters.
