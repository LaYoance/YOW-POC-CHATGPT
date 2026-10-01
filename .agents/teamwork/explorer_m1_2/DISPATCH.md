## 2026-10-01T11:16:48Z

You are the M1 Session Explorer for Milestone 1: Core Foundation & Session Engine.
Working directory: /var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/.agents/teamwork/explorer_m1_2
Project Scope Path: /var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/PROJECT.md
Original Request Path: /var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/.agents/teamwork/ORIGINAL_REQUEST.md
Research Notes Path: /var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/RESEARCH.md

Read ORIGINAL_REQUEST.md and PROJECT.md first.

Your mission:
Investigate and produce concrete implementation recommendations for the Session Management & Security Isolation service (`app/services/session_service.py`):
1. Cookie parsing & JWT extraction:
   - Parsing NextAuth cookies (`__Secure-next-auth.session-token`, `cf_clearance`, `oai-device-id`, etc.) from raw cookie strings or browser jars.
   - Calling `/api/auth/session` on `chatgpt.com` (or local mock) to validate session and obtain fresh `accessToken` JWT.
   - Decoding JWT claims (expiration `exp`, user details) using python `jwt` / `json` safely without leaking secrets.
2. Session State Machine (R1, A7):
   - State definitions: `VALID`, `EXPIRED`, `MISSING`, `CHALLENGE_REQUIRED`.
   - Clear transition triggers (HTTP 200 -> VALID, HTTP 401 -> EXPIRED, HTTP 403 / Turnstile -> CHALLENGE_REQUIRED, empty/unreadable DB -> MISSING).
3. Local Storage & Security Isolation:
   - Dedicated local storage directory: `.session_data/` with filesystem permissions `0700` (`drwx------`).
   - SQLite DB file permissions `0600` (`-rw-------`).
   - Verification that `.session_data/` and `*.db` are strictly excluded from git via `.gitignore`.
   - Secret sanitization filter for logs (never print `accessToken` or cookies in logs).
4. Reconnection Protocol & Error Handling (A7):
   - Handling invalid/expired sessions gracefully: return structured error responses with clear human-readable guidance, zero crashes, zero infinite loops.
   - Reconnect flow: support for manual cookie injection and browser-based login wizard with a bounded timeout (300 seconds).

Write your complete, structured analysis report to:
/var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/.agents/teamwork/explorer_m1_2/handoff.md
Update your progress.md in your working directory as you work.
When finished, send a completion message to the parent orchestrator.
