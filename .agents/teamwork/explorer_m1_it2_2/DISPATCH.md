## 2026-10-01T11:39:36Z

You are the M1-It2 Session State Explorer for Milestone 1 Iteration 2.
Working directory: /var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/.agents/teamwork/explorer_m1_it2_2
Project Scope Path: /var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/PROJECT.md
Original Request Path: /var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/.agents/teamwork/ORIGINAL_REQUEST.md
Reviewer 1 Report: /var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/.agents/teamwork/reviewer_m1_1/handoff.md
Challenger 2 Report: /var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/.agents/teamwork/challenger_m1_2/handoff.md

Read ORIGINAL_REQUEST.md, Reviewer 1 handoff, and Challenger 2 handoff first.

Your mission:
Formulate the exact fix strategy for SessionService in app/services/session_service.py:
1. In import_session_cookies(): validate that if expires_at is present and expires_at <= now, the session must be rejected with status SessionStatus.EXPIRED, is_valid=False, and refresh_attempt_count must NOT be reset.
2. In import_session_cookies(): if status_code != 200 or not access_token, call self.session_repo.update_status(SessionStatus.EXPIRED) if an existing session is in the database, preventing split-brain status.
3. In get_valid_access_token(): if token refresh fails, call self.session_repo.update_status(SessionStatus.EXPIRED) before raising RuntimeError.
4. Prevent circuit breaker bypass: verify that rapid calls with expired tokens do not trigger multiple upstream network requests.
Write your complete recommendation report with verified python code to:
/var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/.agents/teamwork/explorer_m1_it2_2/handoff.md
When complete, send a message to the parent orchestrator.
