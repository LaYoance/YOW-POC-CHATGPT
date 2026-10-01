# Dispatch Log — Sub-Orchestrator Milestone 1

## 2026-10-01T11:16:30Z
You are the Sub-Orchestrator for Milestone 1: Core Foundation & Session Engine.

### Working Directory
`/var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/.agents/teamwork/sub_orch_m1`

### Authoritative Files to Read
- `/var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/.agents/teamwork/ORIGINAL_REQUEST.md` (Verbatim user request — MUST read first)
- `/var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/PROJECT.md` (Project architecture, interface contracts, code layout)
- `/var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/RESEARCH.md` (Research findings, session & transport specs)

### Scope of Milestone 1
Features: F01 (Session Lifecycle & Local Isolation), F09 (SQLite Persistence & Restart Resilience).
Deliverables for M1:
1. Project dependency file: `requirements.txt` (FastAPI, uvicorn, pydantic, curl_cffi, playwright, aiosqlite, pytest, etc.).
2. Pydantic Models (`app/models/`):
   - `session.py`: Session domain models, statuses (`VALID`, `EXPIRED`, `MISSING`, `CHALLENGE_REQUIRED`).
   - `project.py`: Project & file metadata models.
   - `conversation.py`: Conversation & Message models.
   - `transport.py`: Transport types & audit log models.
3. SQLite Engine & Repositories (`app/persistence/`):
   - `db.py`: SQLite connection with WAL mode (`PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;`).
   - `session_repo.py`: Storing sessions with local isolation (mode 0700 for `.session_data/`, 0600 for `poc.db`).
   - `project_repo.py`: Upsert and query projects.
   - `conversation_repo.py`: Upsert and query conversations, save messages, and recursive CTE query for active branch (`get_active_branch`).
   - `transport_log_repo.py`: Audit logging.
4. Session Service (`app/services/session_service.py`):
   - Parsing NextAuth cookies (`__Secure-next-auth.session-token`).
   - Token extraction, status checks (`VALID`, `EXPIRED`, `MISSING`).
   - Safe local storage in `.session_data/` with zero external leaks.
   - Structured reconnect flow without infinite loops or crashes.
5. Unit and component tests for M1 verifying session management, SQLite WAL persistence, recursive CTE branch retrieval, and sub-20ms cold recovery.

### Execution Instructions
1. Initialize `BRIEFING.md`, `SCOPE.md`, and `progress.md` in your working directory.
2. Follow the Project Pattern Iteration Loop (2B):
   - Step a: Dispatch Explorer(s) to verify implementation plan.
   - Step b: Dispatch Worker to implement the code, run builds and tests.
     - MANDATORY: Include the mandatory integrity warning in the Worker dispatch:
       > DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.
   - Step c: Dispatch Reviewers (2 independent reviewers) to verify code and tests.
   - Step d: Dispatch Challengers (2 challengers) to stress test.
   - Step e: Dispatch Forensic Auditor (`teamwork_preview_auditor`) to verify zero integrity violations.
   - Step f: Gate check in `GATE_STATUS.md`.
3. When Gate passes, send completion report with `handoff.md` to Project Orchestrator (`cc1b08b2-2f82-49cf-9e45-b8c933af6ebf`).
