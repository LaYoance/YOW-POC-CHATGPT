# Milestone 1 Iteration 2 — Session State Fix Strategy & Architecture Report

**Agent**: Explorer M1-It2 (`explorer_m1_it2_2`)  
**Roles**: explorer, investigator, synthesizer  
**Target File**: `app/services/session_service.py`  
**Parent Orchestrator**: `cc1b08b2-2f82-49cf-9e45-b8c933af6ebf`  
**Date**: 2026-10-01T11:48:00Z  
**Status**: COMPLETE (Verified Python code and patch provided)

---

## Executive Summary

This report delivers the exact, verified fix strategy for `SessionService` in `app/services/session_service.py` resolving four core vulnerabilities identified in Milestone 1 reviews:
1. **Expired Token Ingestion**: Upstream responses returning HTTP 200 with an already-expired access token are currently accepted as `VALID`, poisoning SQLite persistence.
2. **Split-Brain Database State**: Non-200 responses (e.g. HTTP 401) and empty token payloads return an `EXPIRED` response object but fail to update the existing SQLite record, causing UI badges and status endpoints to falsely report `VALID` while transports fail.
3. **Missing DB Invalidation on Refresh Failure**: When `get_valid_access_token()` attempts silent token refresh and fails, it raises `RuntimeError("SESSION_EXPIRED...")` without updating SQLite to `SessionStatus.EXPIRED`.
4. **Circuit Breaker Bypass & Infinite Loops**: Because `import_session_cookies()` resets `self.refresh_attempt_count = 0` whenever HTTP 200 is received, rapid calls on expired tokens continually re-invoke upstream requests, completely bypassing the single-refresh circuit breaker.

All proposed fixes have been implemented and empirically proven in `test_verify_strategy.py` (6/6 tests passing, 100% success rate).

---

## 1. Observation

### 1.1 Codebase Defects in `app/services/session_service.py`

#### Defect 1: Unconditional `VALID` Status on Expired Token
- **Location**: `app/services/session_service.py`, Lines 364–401
- **Verbatim Code**:
  ```python
  # Decode JWT claims
  claims = self.decode_jwt_claims(access_token)
  exp_ts = claims.get("exp")
  now = datetime.now(timezone.utc)
  if exp_ts:
      expires_at = datetime.fromtimestamp(exp_ts, tz=timezone.utc)
  ...
  session = Session(
      id="default",
      access_token=access_token,
      ...
      status=SessionStatus.VALID,  # Line 394: Unconditional VALID assignment
      expires_at=expires_at,
      last_verified_at=now,
  )
  await self._call_repo(self.session_repo.save, session)
  self.enforce_storage_security()
  self.refresh_attempt_count = 0  # Line 400: Unconditional breaker reset
  ```
- **Observed Behavior**: `import_session_cookies()` never compares `expires_at` against `now`. If upstream returns HTTP 200 with a stale or expired JWT (`exp <= now`), the expired session is saved into SQLite with `status=SessionStatus.VALID`, and `self.refresh_attempt_count` is reset to 0.

#### Defect 2: Missing Database Status Synchronization (Split-Brain)
- **Location**: `app/services/session_service.py`, Lines 339–348 and Lines 350–361
- **Verbatim Code**:
  ```python
  if status_code != 200:
      return SessionStatusResponse(
          status=SessionStatus.EXPIRED,
          is_valid=False,
          ...
      )

  access_token = data.get("accessToken")
  if not access_token:
      return SessionStatusResponse(
          status=SessionStatus.EXPIRED,
          is_valid=False,
          ...
      )
  ```
- **Observed Behavior**: In both error branches, `SessionStatusResponse` is returned with `status=SessionStatus.EXPIRED`, but neither branch checks for an existing session or calls `self.session_repo.update_status(SessionStatus.EXPIRED)`. The SQLite database retains `status=VALID`. A subsequent query to `get_current_session_status()` reads the database and reports `VALID` and `is_valid=True`, causing a split-brain condition between backend transport failures and frontend UI badges.

#### Defect 3: Missing Invalidation Before Raising in `get_valid_access_token()`
- **Location**: `app/services/session_service.py`, Lines 427–436
- **Verbatim Code**:
  ```python
  if session.expires_at and (session.expires_at - now).total_seconds() < 60:
      if self.refresh_attempt_count < 1:
          self.refresh_attempt_count += 1
          cookies = session.cookies
          res = await self.import_session_cookies(json.dumps(cookies))
          if res.is_valid:
              fresh_session = await self._call_repo(self.session_repo.get_default)
              return fresh_session.access_token
      raise RuntimeError("SESSION_EXPIRED: Access token expired and automatic refresh failed.")
  ```
- **Observed Behavior**: If `res.is_valid` is False (refresh failed) or if `self.refresh_attempt_count >= 1`, `get_valid_access_token()` raises `RuntimeError` without updating the database record. If `(session.expires_at - now)` is positive (e.g. token expires in 30s), the DB record remains in `status=VALID` until the time actually passes.

#### Defect 4: Circuit Breaker Bypass Under Upstream 200 Responses
- **Location**: `app/services/session_service.py`, Line 400 (`self.refresh_attempt_count = 0`)
- **Observed Behavior**:
  When a token is expiring (`< 60s`), `get_valid_access_token()` increments `refresh_attempt_count` to 1 and calls `import_session_cookies()`. Upstream responds with HTTP 200 containing an expired token. `import_session_cookies()` considers this a success, saves `VALID`, and resets `self.refresh_attempt_count = 0`.
  On subsequent calls to `get_valid_access_token()`, because `refresh_attempt_count` was reset to 0, each call triggers a new upstream HTTP request. 5 sequential calls generated 6 upstream HTTP requests (Challenger 2 Finding 1).

---

## 2. Logic Chain

1. **R1 and A7 Requirements**:
   - R1 mandates strict local session state management across three explicit states: `VALID`, `EXPIRED`, `MISSING`.
   - A7 mandates that invalid or expired sessions guide reconnection cleanly without infinite loops, crashes, or split-brain state.
2. **From Observation 1 (Defect 1) to Fix 1**:
   - If `expires_at` is decoded and `expires_at <= now`, the access token cannot be used to execute requests on ChatGPT's API.
   - Marking such a session `VALID` in SQLite is a false representation of reality.
   - Therefore, `import_session_cookies()` must evaluate `if expires_at and expires_at <= now:`, mark the session `EXPIRED`, ensure `self.refresh_attempt_count` is **not** reset, and return `is_valid=False`.
3. **From Observation 2 (Defect 2) to Fix 2**:
   - When upstream returns HTTP 401/403/500 or an empty body, the credentials present in SQLite have been rejected by the authoritative upstream identity server.
   - Leaving the existing database record as `VALID` contradicts the result of the network verification.
   - Therefore, whenever `status_code != 200` or `not access_token`, `SessionService` must query `self.session_repo.get_default()` and, if a session exists, execute `self.session_repo.update_status(SessionStatus.EXPIRED)`.
4. **From Observation 3 (Defect 3) to Fix 3**:
   - When `get_valid_access_token()` discovers an expiring token and automatic refresh fails, the session is irrevocably expired for the current lifecycle.
   - Raising `RuntimeError` without recording this in SQLite forces subsequent read-only callers (like `get_current_session_status()`) to report stale `VALID` state until timestamp expiration.
   - Therefore, `self.session_repo.update_status(SessionStatus.EXPIRED)` must be executed before raising `RuntimeError`.
5. **From Observation 4 (Defect 4) to Fix 4**:
   - Preserving `self.refresh_attempt_count = 1` when upstream returns an expired token prevents subsequent calls from re-attempting upstream calls.
   - Adding an `asyncio.Lock` with double-checked locking around the refresh section ensures that even when 50 concurrent requests arrive simultaneously on an expiring token, exactly 1 upstream HTTP request is dispatched, and all callers either receive the fresh token or trip the circuit breaker together.

---

## 3. Caveats

1. **NextAuth Clock Skew Tolerance**: Upstream ChatGPT servers and local client machines may have minor clock skew (< 5 seconds). Evaluating `expires_at <= now` is strict; a token expiring within seconds will be flagged expired upon ingestion. This is safe and conservative because near-expiry tokens (< 60s) trigger refresh anyway.
2. **Token Without `exp` Claim**: If an access token lacks both `exp` in JWT claims and `expires` in the JSON response, `expires_at` is `None`. The check `if expires_at and expires_at <= now` safely handles `None` without false positives.
3. **Non-Dict Upstream JSON Responses**: Occasionally proxies or captive portals return raw HTML or JSON arrays. A defensive guard `if not isinstance(data, dict): data = {}` must accompany the `accessToken` check to prevent `AttributeError`.

---

## 4. Conclusion & Complete Fix Strategy

### 4.1 Exact Code Implementation for `app/services/session_service.py`

#### Patch 1: Storage of Concurrency Refresh Lock (`__init__`)
In `SessionService.__init__` (around line 70):
```python
        # Reconnect wizard and circuit breaker state
        self.refresh_attempt_count = 0
        self._refresh_lock: Optional[asyncio.Lock] = None
        self.active_wizard_task: Optional[asyncio.Task] = None
```

#### Patch 2: Reassemble Chunked NextAuth Cookies (`import_session_cookies`)
In `SessionService.import_session_cookies` (lines 283–300):
```python
        cookies = self.parse_cookie_input(cookies_raw)
        token_key = "__Secure-next-auth.session-token"
        if token_key not in cookies:
            if "next-auth.session-token" in cookies:
                token_key = "next-auth.session-token"
            elif any(k.startswith(f"{token_key}.") for k in cookies):
                # Reassemble chunked NextAuth cookie (.0, .1, etc.)
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
```

#### Patch 3: Invalidate DB on Non-200 and Empty Access Token (`import_session_cookies`)
In `SessionService.import_session_cookies` (lines 339–362):
```python
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
        if not access_token:
            # NextAuth frequently returns HTTP 200 with {} or {"expires": ...} on unauthenticated requests
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
```

#### Patch 4: Invalidate Expired Token and Guard Circuit Breaker Reset (`import_session_cookies`)
In `SessionService.import_session_cookies` (lines 364–401):
```python
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

        # REJECT EXPIRED TOKENS: Do not save as VALID and do NOT reset circuit breaker
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

        # Persist session to SQLite (only valid sessions reach here)
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
        self.refresh_attempt_count = 0  # Reset circuit breaker ONLY on valid token
```

#### Patch 5: Invalidate DB on Failure & Add Concurrency Double-Checked Lock (`get_valid_access_token`)
In `SessionService.get_valid_access_token` (lines 416–440):
```python
    async def get_valid_access_token(self) -> str:
        """
        Returns active access token for transports.
        Refreshes silently if token is expiring within 60 seconds.
        Raises RuntimeError if session is EXPIRED or MISSING.
        """
        session = await self._call_repo(self.session_repo.get_default)
        if not session or not session.access_token:
            raise RuntimeError("SESSION_MISSING: No session active. Please authenticate.")

        now = datetime.now(timezone.utc)
        if session.expires_at and (session.expires_at - now).total_seconds() < 60:
            if self._refresh_lock is None:
                self._refresh_lock = asyncio.Lock()
            async with self._refresh_lock:
                # Double-check: another task may have successfully refreshed while we waited for lock
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

                # Update SQLite to EXPIRED before raising to prevent split-brain status
                await self._call_repo(self.session_repo.update_status, SessionStatus.EXPIRED)
                raise RuntimeError("SESSION_EXPIRED: Access token expired and automatic refresh failed.")

        if session.status != SessionStatus.VALID:
            raise RuntimeError(f"SESSION_{session.status}: Session is not in VALID state.")

        return session.access_token
```

---

### 4.2 Complete Diff Patch (`session_service.patch`)

The following unified diff patch applies directly to `app/services/session_service.py`:

```patch
--- a/app/services/session_service.py
+++ b/app/services/session_service.py
@@ -63,6 +63,7 @@ class SessionService:
 
         # Reconnect wizard and circuit breaker state
         self.refresh_attempt_count = 0
+        self._refresh_lock: Optional[asyncio.Lock] = None
         self.active_wizard_task: Optional[asyncio.Task] = None
         self.wizard_start_time: float = 0.0
         self.wizard_status: ReconnectWizardStatus = ReconnectWizardStatus.IDLE
@@ -284,6 +285,17 @@ class SessionService:
         if token_key not in cookies:
             if "next-auth.session-token" in cookies:
                 token_key = "next-auth.session-token"
+            elif any(k.startswith(f"{token_key}.") for k in cookies):
+                chunk_indices = sorted(
+                    [int(k.split(".")[-1]) for k in cookies if k.startswith(f"{token_key}.") and k.split(".")[-1].isdigit()]
+                )
+                cookies[token_key] = "".join(cookies[f"{token_key}.{i}"] for i in chunk_indices)
+            elif any(k.startswith("next-auth.session-token.") for k in cookies):
+                chunk_indices = sorted(
+                    [int(k.split(".")[-1]) for k in cookies if k.startswith("next-auth.session-token.") and k.split(".")[-1].isdigit()]
+                )
+                cookies["next-auth.session-token"] = "".join(cookies[f"next-auth.session-token.{i}"] for i in chunk_indices)
+                token_key = "next-auth.session-token"
             else:
                 return SessionStatusResponse(
                     status=SessionStatus.MISSING,
@@ -337,6 +349,9 @@ class SessionService:
             )
 
         if status_code != 200:
+            existing = await self._call_repo(self.session_repo.get_default)
+            if existing:
+                await self._call_repo(self.session_repo.update_status, SessionStatus.EXPIRED)
             return SessionStatusResponse(
                 status=SessionStatus.EXPIRED,
                 is_valid=False,
@@ -347,9 +362,15 @@ class SessionService:
                 reconnect_guidance=f"Authentication server rejected session credentials with code {status_code}."
             )
 
+        if not isinstance(data, dict):
+            data = {}
+
         access_token = data.get("accessToken")
         if not access_token:
             # NextAuth frequently returns HTTP 200 with {} or {"expires": ...} on unauthenticated requests
+            existing = await self._call_repo(self.session_repo.get_default)
+            if existing:
+                await self._call_repo(self.session_repo.update_status, SessionStatus.EXPIRED)
             return SessionStatusResponse(
                 status=SessionStatus.EXPIRED,
                 is_valid=False,
@@ -381,6 +402,30 @@ class SessionService:
         user_id = user_info.get("id") or claims.get("sub") or auth_claims.get("user_id")
         plan_type = auth_claims.get("plan_type", "unknown")
 
+        # Reject expired token: do not save as VALID and do not reset circuit breaker
+        if expires_at and expires_at <= now:
+            existing = await self._call_repo(self.session_repo.get_default)
+            if existing:
+                await self._call_repo(self.session_repo.update_status, SessionStatus.EXPIRED)
+            return SessionStatusResponse(
+                status=SessionStatus.EXPIRED,
+                is_valid=False,
+                is_authenticated=False,
+                user_email=user_email,
+                user_name=user_name,
+                user_id=user_id,
+                plan_type=plan_type,
+                expires_at=expires_at,
+                last_verified_at=now,
+                message="Access token has expired.",
+                message_fr="Le jeton d'accès a expiré.",
+                action_required="RECONNECT",
+                reconnect_guidance="The session token retrieved from upstream has already expired. Please re-authenticate.",
+                guidance=[
+                    "Click 'Reconnect' to refresh credentials or enter a fresh cookie."
+                ]
+            )
+
         # Persist session to SQLite
         session = Session(
             id="default",
@@ -397,7 +442,7 @@ class SessionService:
         )
         await self._call_repo(self.session_repo.save, session)
         self.enforce_storage_security()
-        self.refresh_attempt_count = 0  # Reset circuit breaker
+        self.refresh_attempt_count = 0  # Reset circuit breaker ONLY on valid token
 
         return SessionStatusResponse(
             status=SessionStatus.VALID,
@@ -425,14 +470,31 @@ class SessionService:
 
         now = datetime.now(timezone.utc)
         if session.expires_at and (session.expires_at - now).total_seconds() < 60:
-            if self.refresh_attempt_count < 1:
-                self.refresh_attempt_count += 1
-                cookies = session.cookies
-                res = await self.import_session_cookies(json.dumps(cookies))
-                if res.is_valid:
-                    fresh_session = await self._call_repo(self.session_repo.get_default)
-                    return fresh_session.access_token
-            raise RuntimeError("SESSION_EXPIRED: Access token expired and automatic refresh failed.")
+            if self._refresh_lock is None:
+                self._refresh_lock = asyncio.Lock()
+            async with self._refresh_lock:
+                refreshed = await self._call_repo(self.session_repo.get_default)
+                if (
+                    refreshed
+                    and refreshed.access_token
+                    and refreshed.status == SessionStatus.VALID
+                    and refreshed.expires_at
+                    and (refreshed.expires_at - datetime.now(timezone.utc)).total_seconds() >= 60
+                ):
+                    return refreshed.access_token
+
+                if self.refresh_attempt_count < 1:
+                    self.refresh_attempt_count += 1
+                    cookies = session.cookies
+                    res = await self.import_session_cookies(json.dumps(cookies))
+                    if res.is_valid:
+                        fresh_session = await self._call_repo(self.session_repo.get_default)
+                        if fresh_session and fresh_session.access_token:
+                            return fresh_session.access_token
+
+                # Update SQLite status to EXPIRED before raising to prevent split-brain
+                await self._call_repo(self.session_repo.update_status, SessionStatus.EXPIRED)
+                raise RuntimeError("SESSION_EXPIRED: Access token expired and automatic refresh failed.")
 
         if session.status != SessionStatus.VALID:
             raise RuntimeError(f"SESSION_{session.status}: Session is not in VALID state.")
```

---

### 4.3 Updating Regression Tests in `test_adversarial_m1_security.py`

In `tests/test_tier2_boundaries/test_adversarial_m1_security.py`, Challenger 2 added tests demonstrating the vulnerabilities:
- `test_adv_jwt_expired_token_import_flaw_proof`
- `test_adv_circuit_breaker_bypass_on_200_expired_loop_proof`

When Worker M1 applies this fix strategy:
1. `test_adv_jwt_expired_token_import_flaw_proof` should be converted to assert that `import_resp.status == SessionStatus.EXPIRED` and `import_resp.is_valid == False`.
2. `test_adv_circuit_breaker_bypass_on_200_expired_loop_proof` should assert that 5 sequential calls to `get_valid_access_token()` each raise `RuntimeError("SESSION_EXPIRED...")` and `mock_http.call_count == 1` (circuit breaker enforced).

---

## 5. Verification Method

### 5.1 Verification Test Suite Execution
A comprehensive validation suite was authored and executed in:
`.agents/teamwork/explorer_m1_it2_2/test_verify_strategy.py`

Command executed:
```bash
PYTHONPATH=. python3 /var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/.agents/teamwork/explorer_m1_it2_2/test_verify_strategy.py -v
```

Verbatim Output:
```
test_req1_import_expired_token_rejection (__main__.TestFixStrategyVerification.test_req1_import_expired_token_rejection)
Req 1: expires_at <= now must reject with EXPIRED, is_valid=False, and NOT reset refresh_attempt_count. ... ok
test_req2_import_failure_updates_db_to_expired (__main__.TestFixStrategyVerification.test_req2_import_failure_updates_db_to_expired)
Req 2: status_code != 200 or not access_token must update existing DB session to EXPIRED. ... ok
test_req3_get_valid_access_token_updates_db_on_refresh_failure (__main__.TestFixStrategyVerification.test_req3_get_valid_access_token_updates_db_on_refresh_failure)
Req 3: In get_valid_access_token(): if token refresh fails, update DB to EXPIRED before raising. ... ok
test_req4_circuit_breaker_rapid_calls_on_200_expired (__main__.TestFixStrategyVerification.test_req4_circuit_breaker_rapid_calls_on_200_expired)
Req 4: Rapid calls when upstream returns 200 with expired token must trigger EXACTLY 1 upstream call. ... ok
test_req4_concurrent_calls_on_200_expired (__main__.TestFixStrategyVerification.test_req4_concurrent_calls_on_200_expired)
Req 4: 50 concurrent calls when upstream returns 200 with expired token trigger EXACTLY 1 upstream call. ... ok
test_successful_refresh_resets_breaker_and_returns_fresh_token (__main__.TestFixStrategyVerification.test_successful_refresh_resets_breaker_and_returns_fresh_token)
Verify happy-path refresh: fresh token resets circuit breaker, updates DB to VALID, and returns token. ... ok

----------------------------------------------------------------------
Ran 6 tests in 0.822s

OK
```

### 5.2 Full Test Suite Execution
```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```
Result: 42 tests passing across all Tier 1 and Tier 2 boundary suites.

### 5.3 Invalidation Conditions
This recommendation is invalidated if:
1. `import_session_cookies()` can be shown to already reject expired tokens without modifications (disproven by Finding 1).
2. SQLite database records are shown to transition to `EXPIRED` on HTTP 401 without updating the DB (disproven by Reviewer 1 Observation 4).
3. Any test in `test_verify_strategy.py` fails to pass under Python 3.12 (disproven by successful execution above).
