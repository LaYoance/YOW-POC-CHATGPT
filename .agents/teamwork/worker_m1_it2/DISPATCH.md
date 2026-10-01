# Dispatch: Worker M1 Iteration 2

## Mission
Apply the verified fix strategies from the 3 Milestone 1 Iteration 2 Explorers (`explorer_m1_it2_1`, `explorer_m1_it2_2`, `explorer_m1_it2_3`) to `app/core/logging_filter.py`, `app/services/session_service.py`, `tests/test_tier1_features/test_feature_session.py`, and `tests/test_tier2_boundaries/test_adversarial_m1_security.py`.

## Input Artifacts to Read First
1. `/var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/.agents/teamwork/ORIGINAL_REQUEST.md`
2. `/var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/PROJECT.md`
3. `/var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/.agents/teamwork/explorer_m1_it2_1/handoff.md` (Logging filter fix & diff)
4. `/var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/.agents/teamwork/explorer_m1_it2_2/handoff.md` (Session state, DB sync, circuit breaker lock & diff)
5. `/var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/.agents/teamwork/explorer_m1_it2_3/handoff.md` (NextAuth cookie chunking & 5 regression test cases)

## Exclusive File Ownership
You have exclusive write ownership of:
- `app/core/logging_filter.py`
- `app/services/session_service.py`
- `tests/test_tier1_features/test_feature_session.py`
- `tests/test_tier2_boundaries/test_adversarial_m1_security.py`

## Mandatory Tasks
1. Update `app/core/logging_filter.py`:
   - Expand `BEARER_PATTERN` to `r'(Bearer\s+)[A-Za-z0-9_\-\.\+\/=]{10,}'`.
   - Update `COOKIE_SESSION_PATTERN` to `r'(__Secure-next-auth\.session-token(?:\.\d+)?=)[^;,\s]{6,}'`.
   - Update `COOKIE_DEV_PATTERN` to `r'(next-auth\.session-token(?:\.\d+)?=)[^;,\s]{6,}'`.
   - Fix `%d` / `%f` format specifier crash using type-preserving recursive sanitization (`_sanitize_value`), preserving integer/float types for formatting.
   - Sanitize non-string `record.msg` (e.g. `Exception` objects) and dict keys in `record.args`.
2. Update `app/services/session_service.py`:
   - Add `_refresh_lock: Optional[asyncio.Lock] = None` in `__init__`.
   - Add `reassemble_cookie_chunks()` to reassemble NextAuth chunked cookies (`.0`, `.1`, etc.) into the base cookie key.
   - Add defensive non-dict JSON body check: `if not isinstance(data, dict): data = {}`.
   - In `import_session_cookies()`: if `status_code != 200` or `not access_token`, synchronize existing DB session status to `EXPIRED`.
   - In `import_session_cookies()`: if `expires_at and expires_at <= now`, synchronize DB to `EXPIRED`, reject with status `EXPIRED`, `is_valid=False`, and DO NOT reset `refresh_attempt_count`.
   - In `get_current_session_status()`: check `session.status == SessionStatus.EXPIRED or (session.expires_at and session.expires_at <= now)`.
   - In `get_valid_access_token()`: use `_refresh_lock` with double-checked locking; on refresh failure update DB to `EXPIRED` before raising `RuntimeError`.
3. Update `tests/test_tier1_features/test_feature_session.py`:
   - Append the 5 regression tests from `explorer_m1_it2_3`:
     - `test_bearer_base64_masking`
     - `test_chunked_cookie_masking_and_ingestion`
     - `test_logging_percent_d_specifier_no_crash`
     - `test_expired_jwt_import_rejection`
     - `test_database_status_sync_on_401`
4. Update `tests/test_tier2_boundaries/test_adversarial_m1_security.py`:
   - Invert Challenger 2 vulnerability proofs (`test_adv_jwt_expired_token_import_flaw_proof`, `test_adv_circuit_breaker_bypass_on_200_expired_loop_proof`, and `test_adv_log_sanitization_leak_vectors_proof`) so they assert the secured/remediated behavior (`assertNotIn`, `mock_http.call_count == 1`, `status == EXPIRED`, `is_valid == False`).
5. Execute full test suite:
   `python3 -m unittest discover -s tests -p "test_*.py" -v`
   Verify all tests pass with 0 failures and 0 errors.

## Mandatory Integrity Warning
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

## Completion Deliverables
Write your handoff report to:
`.agents/teamwork/worker_m1_it2/handoff.md`
Report verification commands and outputs, then send a completion message to the parent orchestrator.
