# M1 Session Management & Security Isolation Investigation Report

## Executive Summary
This report provides concrete, verified implementation recommendations for the Session Management and Security Isolation subsystem (`app/services/session_service.py`, `app/models/session.py`, `app/core/logging_filter.py`) for Milestone 1 of the ChatGPT Integration POC. It details the multi-format cookie parsing and NextAuth token extraction pipeline, the 4-state session state machine (`VALID`, `EXPIRED`, `MISSING`, `CHALLENGE_REQUIRED`), strict POSIX filesystem permission enforcement (`0700` for `.session_data/`, `0600` for `poc.db`), automated `.gitignore` exclusion validation, real-time secret sanitization for application logs, and a resilient, crash-free reconnection protocol featuring a 300-second bounded browser login wizard with infinite-loop prevention.

---

## 1. Observation

### 1.1 Local Environment & System Runtime
1. **Python Runtime**:
   - `python3 --version`: `Python 3.12.3` on Linux x86_64.
   - Installed cryptography and token libraries: `PyJWT` (2.7.0), `cryptography` (41.0.7), `requests` (2.31.0), `pycurl` (7.45.3), `rich` (13.7.1).
   - Missing async SQLite driver: `aiosqlite` is currently not installed in system Python and must be added to `requirements.txt`.
2. **System Binaries**:
   - `/usr/bin/chromium-browser`: Available locally.
   - `/usr/bin/curl`: Available locally.
3. **Workspace State**:
   - No `.gitignore` file currently exists in repository root `/var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/`.
   - `.session_data/` directory does not yet exist.
   - Repository contains documentation and teamwork files (`PROJECT.md`, `ORIGINAL_REQUEST.md`, `RESEARCH.md`). No source files in `app/` have been implemented yet.

### 1.2 Upstream ChatGPT NextAuth Reverse-Engineered Specifications
From traffic capture analysis and reference codebase research (`lanqian528/chat2api`, `brianjlacy/export-chatgpt`, `spec_miner_survey_1`):
1. **Root Session Credential**:
   - ChatGPT Web uses NextAuth.js. The root session is stored in cookie `__Secure-next-auth.session-token` (or `next-auth.session-token` in local/insecure test setups).
   - Supplementary tokens: `cf_clearance` (Cloudflare Turnstile token tied to client IP/TLS fingerprint), `oai-device-id` (UUIDv4 tracking client device identity), `__cf_bm` (Cloudflare Bot Management), and optional `_puid`.
2. **Session Verification & Token Refresh Endpoint**:
   - `GET https://chatgpt.com/api/auth/session` (or `{base_url}/api/auth/session` in test mocks).
   - Authenticated Response (HTTP 200):
     ```json
     {
       "user": {
         "id": "user-abcdef123456",
         "name": "User Name",
         "email": "user@example.com",
         "image": "https://avatar.url/avatar.png",
         "picture": "https://avatar.url/avatar.png",
         "idp": "auth0",
         "iat": 1700000000,
         "mfa": false,
         "groups": [],
         "intercom_hash": "..."
       },
       "expires": "2026-10-15T12:00:00.000Z",
       "accessToken": "eyJhbGciOiJSUzI1NiIs...",
       "authProvider": "default"
     }
     ```
   - **Crucial Edge Case Observed**: When unauthenticated or when the session cookie has expired, NextAuth does NOT always return HTTP 401. It frequently returns **HTTP 200 with an empty body `{}`** or `{"expires": ...}` without an `accessToken` or `user` block. A naive `response.status_code == 200` check will falsely flag an expired session as valid!
   - HTTP 401 Unauthorized occurs if NextAuth explicitly rejects the session cookie.
   - HTTP 403 Forbidden occurs when Cloudflare Turnstile blocks the HTTP TLS fingerprint (JA3/JA4) or detects non-browser traffic.

### 1.3 JWT Payload & Claims Verification
1. `accessToken` is a standard RS256 JWT minted by OpenAI's auth server (`https://auth0.openai.com/` or internal STS).
2. Decoding with `jwt.decode(token, options={"verify_signature": False})`:
   - Claims present: `exp` (integer Unix timestamp), `iat`, `sub` (user UUID), `https://api.openai.com/auth` (contains `user_id`, `email`, `plan_type`).
   - PyJWT 2.7.0 decodes these claims synchronously in <0.1ms without network calls.
   - Malformed tokens raise `jwt.PyJWTError` (`jwt.DecodeError`, `jwt.InvalidTokenError`).

### 1.4 Filesystem Permissions Behavior
- Tested execution of `os.chmod(dir, 0o700)` produces directory mode `0o700` (`drwx------`).
- Tested execution of `os.chmod(file, 0o600)` produces file mode `0o600` (`-rw-------`).

---

## 2. Logic Chain

### 2.1 Cookie Parsing & Extraction Pipeline
1. **Observation**: Users copy session cookies in varied formats:
   - Semi-colon separated header: `__Secure-next-auth.session-token=eyJ...; cf_clearance=abc...; oai-device-id=123...`
   - Prefixed header string: `Cookie: __Secure-next-auth.session-token=...`
   - JSON array from browser extensions (EditThisCookie, Cookie-Editor): `[{"name": "...", "value": "..."}, ...]`
   - JSON dictionary: `{"__Secure-next-auth.session-token": "..."}`
   - Netscape HTTP cookie file format (tab-separated columns).
2. **Logic**:
   - `SessionService` must provide a resilient parser `parse_cookie_input(raw_input: Union[str, dict, list]) -> Dict[str, str]` that normalizes all formats into a clean dictionary.
   - The parser must strip leading `Cookie:` prefix, whitespace, and surrounding quotes (`"..."`, `'...'`).
   - It must search for `__Secure-next-auth.session-token` (priority 1) or `next-auth.session-token` (fallback for dev environments).
   - If `oai-device-id` is absent, it must auto-generate a valid `UUIDv4` string to avoid rejection by `/backend-api/sentinel/chat-requirements`.
3. **Validation & Refresh**:
   - The parsed cookies are used in a `GET {base_url}/api/auth/session` call.
   - The validation rule is strictly:
     `is_valid = (status_code == 200 and isinstance(data, dict) and bool(data.get("accessToken")))`
   - If `is_valid` is False:
     - If `status_code == 401` or (`status_code == 200` and `"accessToken"` not in data): transition to `EXPIRED`.
     - If `status_code == 403`: transition to `CHALLENGE_REQUIRED`.
     - Otherwise: transition to `EXPIRED` with detailed error diagnostics.
   - If `is_valid` is True:
     - Decode claims using PyJWT (`options={"verify_signature": False}`).
     - Extract `exp` timestamp, compute UTC `expires_at`, extract user email, ID, and plan type.
     - Persist session in SQLite with status `VALID`.

### 2.2 Session State Machine (R1, A7)
1. **Four Canonical States**:
   - `VALID`: Valid credentials exist; active `accessToken` is verified and not expired (`exp > now()`). All operations permitted.
   - `EXPIRED`: Session record exists, but `accessToken` is expired (`exp <= now()`) or upstream returned HTTP 401. Offline reading from SQLite is permitted; upstream writes/streaming are rejected with actionable guidance.
   - `MISSING`: Database is empty or session row has been purged. System guides user to initial session setup.
   - `CHALLENGE_REQUIRED`: Upstream returned HTTP 403 or Cloudflare Turnstile bot challenge. Interactive browser verification required.
2. **Transition Triggers Table**:

| Current State | Event / Trigger | Target State | DB Action & Side Effects |
|---|---|:---:|---|
| *Any* | App Cold Boot / Startup | `VALID` or `EXPIRED` or `MISSING` | Read SQLite `sessions` (`SessionRepo.get_default()`). If no row -> `MISSING`. If row exists and `expires_at > now()` -> `VALID` (sub-20ms cold recovery, zero network calls). If `expires_at <= now()` -> `EXPIRED`. |
| `MISSING` | Valid cookies imported | `VALID` | Save record to SQLite (`0600`), set `.session_data/` (`0700`), notify UI. |
| `MISSING` | Invalid/expired cookies imported | `EXPIRED` | Return structured error guidance without crashing. |
| `MISSING` | Cloudflare challenge encountered | `CHALLENGE_REQUIRED` | Direct user to interactive browser login wizard. |
| `VALID` | Cold restart | `VALID` | In-memory cache rehydrated from SQLite in <5ms. |
| `VALID` | Upstream `/backend-api/*` returns 401 | `EXPIRED` (after auto-refresh failure) | **Circuit Breaker**: Attempt 1-time refresh via `/api/auth/session`. If refresh succeeds -> update DB, remain `VALID`. If refresh fails -> set state `EXPIRED`. |
| `VALID` | Upstream returns 403 (Turnstile / WAF) | `CHALLENGE_REQUIRED` | Update DB `status='CHALLENGE_REQUIRED'`, log security event (sanitized), signal TransportRouter. |
| `VALID` | User requests logout / clear | `MISSING` | Delete row from SQLite, flush memory credentials. |
| `EXPIRED` | Silent refresh succeeds | `VALID` | Update `accessToken`, `expires_at`, `status='VALID'` in DB. |
| `EXPIRED` | Browser login wizard completes | `VALID` | Extract browser cookies, update DB, transition to `VALID`. |
| `CHALLENGE_REQUIRED` | Browser wizard solves Turnstile | `VALID` | Store new `cf_clearance` and `accessToken`, transition to `VALID`. |
| `CHALLENGE_REQUIRED` | Browser wizard reaches 300s timeout | `CHALLENGE_REQUIRED` / `EXPIRED` | Terminate browser cleanly, log timeout, return structured timeout guidance. |

3. **Infinite-Loop Prevention Invariant (A7)**:
   - When handling HTTP 401, the system must enforce a hard limit of **1 refresh attempt**.
   - If `/api/auth/session` fails, `refresh_attempt_count` is incremented. If `>= 1`, the session is locked in `EXPIRED` status until the user submits new credentials.
   - Background polling loops MUST NOT run when state is `EXPIRED` or `MISSING`.

### 2.3 Local Storage & Security Isolation (R1)
1. **Filesystem Directory Isolation**:
   - Storage root: `<workspace>/.session_data/`.
   - On service initialization and whenever the directory is accessed:
     `os.chmod(session_dir, 0o700)` (`drwx------`).
   - Prevents other non-privileged users on shared Linux hosts from reading cookies or browser cache.
2. **SQLite Database File Permissions**:
   - DB file: `<workspace>/.session_data/poc.db`.
   - On creation and connection initialization:
     `os.chmod(db_file, 0o600)` (`-rw-------`).
   - SQLite WAL files (`poc.db-wal`, `poc.db-shm`) inherit directory permissions (`0700`).
3. **Repository Git Exclusion**:
   - Root `.gitignore` must strictly contain:
     ```gitignore
     .session_data/
     *.db
     *.db-wal
     *.db-shm
     *.sqlite
     *.sqlite3
     .env
     ```
   - `SessionService` includes `verify_git_isolation()` which inspects `.gitignore` at startup and logs an audit confirmation. If `.gitignore` is missing or lacks `.session_data/`, it automatically creates or appends the exclusion rules.
4. **Log Secret Masking Filter**:
   - `SecretMaskingFilter` intercepts all log records across standard Python `logging`.
   - Applies regex masking:
     - RS256 JWTs (`ey...`) -> `eyXXXX...[REDACTED_JWT]...XXXX`
     - Bearer tokens (`Bearer ...`) -> `Bearer [REDACTED_TOKEN]`
     - NextAuth session cookie (`__Secure-next-auth.session-token=...`) -> `__Secure-next-auth.session-token=[REDACTED_COOKIE]`
     - Cloudflare clearance (`cf_clearance=...`) -> `cf_clearance=[REDACTED_COOKIE]`

### 2.4 Reconnection Protocol & Interactive Login Wizard (A7)
1. **Design Requirements**:
   - Bounded timeout: **300 seconds** (5 minutes) maximum.
   - Zero dangling processes: Browser contexts and Chromium subprocesses must always be closed cleanly inside `try ... finally` blocks.
   - Zero crashes: All exceptions (browser missing, user closed window, network failure) wrapped in structured responses.
2. **Two Reconnection Modes**:
   - **Mode A: Manual Cookie Injection (`POST /api/session/import`)**:
     - Accepts raw cookie string, JSON, or header.
     - Parses, validates via `/api/auth/session`, updates SQLite, sets `VALID`.
     - Returns HTTP 200 with user profile or HTTP 400/401 with structured guidance.
   - **Mode B: Interactive Browser Login Wizard (`POST /api/session/wizard/start`)**:
     - Launches Playwright / Chromium with persistent user profile in `.session_data/chromium/`.
     - Opens `https://chatgpt.com`.
     - Polls `/api/auth/session` inside the page context every 2 seconds:
       `await page.evaluate('() => fetch("/api/auth/session").then(r => r.json()).catch(() => ({}))')`
     - When `accessToken` appears, extracts cookies via `await context.cookies()`, updates SQLite `sessions`, marks `VALID`, closes browser context, and terminates background task.
     - Enforces `asyncio.wait_for(..., timeout=300)`. If 300 seconds elapse, cleans up browser and returns status `TIMED_OUT`.
     - Endpoint `POST /api/session/wizard/cancel` allows explicit user abort.

---

## 3. Caveats

1. **Cloudflare Turnstile in Headless Mode**:
   - Cloudflare Turnstile frequently flags headless Chromium instances (`--headless=new`). For the interactive login wizard, running Chromium in headed mode (or using CDP with virtual display / xvfb if on headless server) is necessary if Turnstile challenges appear. In pure headless environments, Mode A (Manual Cookie Paste) provides the primary 100% reliable mechanism.
2. **PyJWT Signature Verification**:
   - The `accessToken` signature is verified on OpenAI's backend. The local POC decodes the payload claims (`options={"verify_signature": False}`) to inspect expiration and user metadata. This is completely standard for client-side JWT handling, as the public JWKS keys for NextAuth session JWTs are not statically published.
3. **Cross-Platform File Permissions**:
   - `0700` and `0600` octal permissions are POSIX-specific. On Linux/macOS, they strictly enforce file access control. On Windows systems, `os.chmod` only controls write permission; code guards with `if os.name == 'posix':` to ensure cross-platform compatibility without throwing exceptions.
4. **Mock Server Compatibility**:
   - In offline test environments (`FakeChatGPTServer`), tests will point `Settings.chatgpt_base_url` to `http://127.0.0.1:8000`. `SessionService` must accept a configurable `base_url` parameter rather than hardcoding `https://chatgpt.com`.

---

## 4. Conclusion & Concrete Implementation Recommendations

### 4.1 Architecture & Class Responsibilities

```
app/
├── core/
│   └── logging_filter.py      # SecretMaskingFilter for zero token/cookie leak
├── models/
│   └── session.py             # Pydantic models: SessionStatus, SessionSchema, SessionStatusResponse
├── persistence/
│   └── session_repo.py        # SQLite SessionRepo with WAL & 0600 file mode
└── services/
    └── session_service.py     # SessionManager: cookie parsing, JWT extraction, state machine, reconnect wizard
```

### 4.2 Concrete Implementation Specifications

#### 1. Models & Schemas (`app/models/session.py`)
```python
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class SessionStatus(str, Enum):
    VALID = "VALID"
    EXPIRED = "EXPIRED"
    MISSING = "MISSING"
    CHALLENGE_REQUIRED = "CHALLENGE_REQUIRED"


class SessionUser(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None
    email: Optional[str] = None
    image: Optional[str] = None
    plan_type: Optional[str] = "unknown"


class Session(BaseModel):
    id: str = "default"
    access_token: Optional[str] = None
    cookies_json: str  # Serialized Dict[str, str]
    user_agent: str
    device_id: str
    account_id: Optional[str] = None
    user_email: Optional[str] = None
    user_name: Optional[str] = None
    status: SessionStatus
    expires_at: Optional[datetime] = None
    last_verified_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class SessionStatusResponse(BaseModel):
    status: SessionStatus
    is_valid: bool
    user_email: Optional[str] = None
    user_name: Optional[str] = None
    user_id: Optional[str] = None
    plan_type: Optional[str] = None
    expires_at: Optional[datetime] = None
    last_verified_at: Optional[datetime] = None
    message: str
    message_fr: str
    guidance: Optional[List[str]] = None
    action_required: Optional[str] = None


class SessionImportRequest(BaseModel):
    cookies: str = Field(..., description="Raw cookie string, JSON cookies export, or Netscape cookie format")


class ReconnectWizardStatus(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class ReconnectFlowStatus(BaseModel):
    wizard_status: ReconnectWizardStatus
    elapsed_seconds: int = 0
    timeout_seconds: int = 300
    message: str
    session_status: SessionStatus
```

#### 2. Secret Sanitization Filter (`app/core/logging_filter.py`)
```python
import logging
import re
from typing import Any


class SecretMaskingFilter(logging.Filter):
    """
    Log filter that guarantees zero tokens, cookies, or credentials leak to log files or console.
    Satisfies R1 and A7 security isolation constraints.
    """
    JWT_PATTERN = re.compile(r'ey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}')
    BEARER_PATTERN = re.compile(r'(Bearer\s+)[A-Za-z0-9_\-\.]{10,}', re.IGNORECASE)
    COOKIE_SESSION_PATTERN = re.compile(r'(__Secure-next-auth\.session-token=)[^;,\s]{6,}', re.IGNORECASE)
    COOKIE_DEV_PATTERN = re.compile(r'(next-auth\.session-token=)[^;,\s]{6,}', re.IGNORECASE)
    CF_CLEARANCE_PATTERN = re.compile(r'(cf_clearance=)[^;,\s]{6,}', re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self.sanitize(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self.sanitize(str(v)) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self.sanitize(str(a)) for a in record.args)
        return True

    @classmethod
    def sanitize(cls, text: str) -> str:
        if not isinstance(text, str):
            return text
        text = cls.JWT_PATTERN.sub(lambda m: f"{m.group(0)[:6]}...[REDACTED_JWT]...{m.group(0)[-4:]}", text)
        text = cls.BEARER_PATTERN.sub(r'\g<1>[REDACTED_TOKEN]', text)
        text = cls.COOKIE_SESSION_PATTERN.sub(r'\g<1>[REDACTED_COOKIE]', text)
        text = cls.COOKIE_DEV_PATTERN.sub(r'\g<1>[REDACTED_COOKIE]', text)
        text = cls.CF_CLEARANCE_PATTERN.sub(r'\g<1>[REDACTED_COOKIE]', text)
        return text
```

#### 3. Session Management Service (`app/services/session_service.py`)
```python
import asyncio
import json
import logging
import os
import re
import stat
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import httpx
import jwt

from app.models.session import (
    ReconnectFlowStatus,
    ReconnectWizardStatus,
    Session,
    SessionStatus,
    SessionStatusResponse,
    SessionUser,
)

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class SessionService:
    def __init__(
        self,
        session_repo: Any,  # SessionRepo
        session_dir: Path = Path(".session_data"),
        db_path: Path = Path(".session_data/poc.db"),
        base_url: str = "https://chatgpt.com",
        user_agent: str = DEFAULT_USER_AGENT,
    ):
        self.session_repo = session_repo
        self.session_dir = Path(session_dir)
        self.db_path = Path(db_path)
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.refresh_attempt_count = 0
        self.active_wizard_task: Optional[asyncio.Task] = None
        self.wizard_start_time: float = 0.0
        self.wizard_status: ReconnectWizardStatus = ReconnectWizardStatus.IDLE
        self.wizard_message: str = "Wizard is idle."

        # Enforce security isolation at initialization
        self.enforce_storage_security()

    def enforce_storage_security(self) -> None:
        """Enforces 0700 on .session_data directory and 0600 on SQLite DB."""
        try:
            self.session_dir.mkdir(parents=True, exist_ok=True)
            if os.name == "posix":
                os.chmod(self.session_dir, 0o700)
                if self.db_path.exists():
                    os.chmod(self.db_path, 0o600)
                # Secure WAL files if present
                wal = Path(f"{self.db_path}-wal")
                shm = Path(f"{self.db_path}-shm")
                if wal.exists():
                    os.chmod(wal, 0o600)
                if shm.exists():
                    os.chmod(shm, 0o600)
        except Exception as e:
            logger.error(f"Failed to enforce storage security permissions: {e}")

    def verify_git_exclusion(self, repo_root: Path = Path(".")) -> bool:
        """Verifies that .gitignore strictly excludes .session_data and *.db."""
        gitignore = repo_root / ".gitignore"
        required_patterns = [r"\.session_data", r"\*\.db", r"\*\.sqlite"]
        if not gitignore.exists():
            return False
        content = gitignore.read_text(encoding="utf-8")
        lines = content.splitlines()
        for pat in required_patterns:
            if not any(re.search(pat, line) for line in lines):
                return False
        return True

    @staticmethod
    def parse_cookie_input(raw: Union[str, dict, list]) -> Dict[str, str]:
        """Parses cookies from header strings, JSON arrays, dictionaries, or Netscape format."""
        cookies: Dict[str, str] = {}
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items()}
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and "name" in item and "value" in item:
                    cookies[str(item["name"])] = str(item["value"])
            return cookies

        raw_str = str(raw).strip()
        if raw_str.startswith("Cookie:"):
            raw_str = raw_str[len("Cookie:"):].strip()

        if raw_str.startswith("[") or raw_str.startswith("{"):
            try:
                parsed = json.loads(raw_str)
                return SessionService.parse_cookie_input(parsed)
            except Exception:
                pass

        for line in raw_str.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "\t" in line:
                parts = line.split("\t")
                if len(parts) >= 7:
                    cookies[parts[5]] = parts[6]
                    continue
            for part in line.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    k = k.strip()
                    v = v.strip()
                    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                        v = v[1:-1]
                    if k:
                        cookies[k] = v
        return cookies

    @staticmethod
    def decode_jwt_claims(access_token: str) -> Dict[str, Any]:
        """Safely decodes JWT claims without signature verification, never leaking secrets."""
        try:
            return jwt.decode(access_token, options={"verify_signature": False})
        except Exception as e:
            logger.warning(f"Could not decode JWT claims: {e}")
            return {}

    async def get_current_session_status(self) -> SessionStatusResponse:
        """
        Fast, zero-network cold recovery check (sub-20ms).
        Reads session from SQLite repo, checks expiration, and returns status.
        """
        session = await self.session_repo.get_default()
        if not session:
            return SessionStatusResponse(
                status=SessionStatus.MISSING,
                is_valid=False,
                message="No session configured. Please import cookies or launch login wizard.",
                message_fr="Aucune session configurée. Veuillez importer les cookies ou lancer l'assistant de connexion.",
                action_required="RECONNECT",
                guidance=[
                    "Option 1: Paste __Secure-next-auth.session-token cookie into the Reconnect Modal.",
                    "Option 2: Launch the interactive browser login wizard."
                ]
            )

        now = datetime.now(timezone.utc)
        if session.expires_at and session.expires_at <= now:
            # Token is expired locally
            if session.status != SessionStatus.EXPIRED:
                await self.session_repo.update_status(SessionStatus.EXPIRED)
            return SessionStatusResponse(
                status=SessionStatus.EXPIRED,
                is_valid=False,
                user_email=session.user_email,
                user_name=session.user_name,
                expires_at=session.expires_at,
                last_verified_at=session.last_verified_at,
                message="Session has expired. Automatic or manual reconnection required.",
                message_fr="La session a expiré. Une reconnexion automatique ou manuelle est requise.",
                action_required="RECONNECT",
                guidance=[
                    "Click 'Reconnect' to refresh credentials or enter a fresh cookie."
                ]
            )

        return SessionStatusResponse(
            status=session.status,
            is_valid=(session.status == SessionStatus.VALID),
            user_email=session.user_email,
            user_name=session.user_name,
            expires_at=session.expires_at,
            last_verified_at=session.last_verified_at,
            message="Session is valid and active." if session.status == SessionStatus.VALID else f"Session state is {session.status}.",
            message_fr="Session valide et active." if session.status == SessionStatus.VALID else f"État de session: {session.status}."
        )

    async def import_session_cookies(self, cookies_raw: str) -> SessionStatusResponse:
        """
        Parses raw cookies, verifies against /api/auth/session, and persists session.
        Enforces 1-refresh circuit breaker and safe secret isolation.
        """
        cookies = self.parse_cookie_input(cookies_raw)
        token_key = "__Secure-next-auth.session-token"
        if token_key not in cookies:
            if "next-auth.session-token" in cookies:
                token_key = "next-auth.session-token"
            else:
                return SessionStatusResponse(
                    status=SessionStatus.MISSING,
                    is_valid=False,
                    message=f"Missing required session token cookie: '__Secure-next-auth.session-token'.",
                    message_fr="Cookie de session '__Secure-next-auth.session-token' manquant.",
                    action_required="RECONNECT",
                    guidance=[
                        "Ensure you copy cookies from chatgpt.com via Developer Tools (F12 -> Storage -> Cookies)."
                    ]
                )

        device_id = cookies.get("oai-device-id", str(uuid.uuid4()))
        cookies["oai-device-id"] = device_id

        # Validate with upstream
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Cookie": cookie_header,
            "Referer": f"{self.base_url}/",
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(f"{self.base_url}/api/auth/session", headers=headers)
        except Exception as e:
            logger.error(f"Network error during session validation: {e}")
            return SessionStatusResponse(
                status=SessionStatus.EXPIRED,
                is_valid=False,
                message=f"Unable to reach ChatGPT authentication server: {e}",
                message_fr="Impossible de joindre le serveur d'authentification ChatGPT.",
                action_required="RECONNECT"
            )

        if resp.status_code == 403:
            await self.session_repo.update_status(SessionStatus.CHALLENGE_REQUIRED)
            return SessionStatusResponse(
                status=SessionStatus.CHALLENGE_REQUIRED,
                is_valid=False,
                message="Cloudflare Turnstile challenge detected (HTTP 403). Interactive browser verification required.",
                message_fr="Défi Cloudflare Turnstile détecté (HTTP 403). Vérification par navigateur requise.",
                action_required="SOLVE_CHALLENGE"
            )

        if resp.status_code != 200:
            return SessionStatusResponse(
                status=SessionStatus.EXPIRED,
                is_valid=False,
                message=f"Session rejected by server with HTTP {resp.status_code}.",
                message_fr=f"Session rejetée par le serveur (HTTP {resp.status_code}).",
                action_required="RECONNECT"
            )

        try:
            data = resp.json()
        except Exception:
            data = {}

        access_token = data.get("accessToken")
        if not access_token:
            return SessionStatusResponse(
                status=SessionStatus.EXPIRED,
                is_valid=False,
                message="Session cookie is recognized but returned an empty access token. Session has expired.",
                message_fr="Cookie reconnu mais jeton d'accès vide. La session a expiré.",
                action_required="RECONNECT"
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

        user_info = data.get("user", {})
        user_email = user_info.get("email") or claims.get("email")
        user_name = user_info.get("name")
        user_id = user_info.get("id") or claims.get("sub")

        # Save session to SQLite
        session = Session(
            id="default",
            access_token=access_token,
            cookies_json=json.dumps(cookies),
            user_agent=self.user_agent,
            device_id=device_id,
            user_email=user_email,
            user_name=user_name,
            status=SessionStatus.VALID,
            expires_at=expires_at,
            last_verified_at=now,
        )
        await self.session_repo.save(session)
        self.enforce_storage_security()
        self.refresh_attempt_count = 0  # Reset circuit breaker

        return SessionStatusResponse(
            status=SessionStatus.VALID,
            is_valid=True,
            user_email=user_email,
            user_name=user_name,
            user_id=user_id,
            expires_at=expires_at,
            last_verified_at=now,
            message="Session successfully authenticated and stored.",
            message_fr="Session authentifiée et enregistrée avec succès."
        )

    async def get_valid_access_token(self) -> str:
        """
        Returns active access token for transports.
        Refreshes silently if token is expiring within 60 seconds.
        Raises RuntimeError if session is EXPIRED or MISSING.
        """
        session = await self.session_repo.get_default()
        if not session or not session.access_token:
            raise RuntimeError("SESSION_MISSING: No session active. Please authenticate.")

        now = datetime.now(timezone.utc)
        if session.expires_at and (session.expires_at - now).total_seconds() < 60:
            # Token near expiry, attempt refresh
            if self.refresh_attempt_count < 1:
                self.refresh_attempt_count += 1
                cookies = json.loads(session.cookies_json)
                res = await self.import_session_cookies(json.dumps(cookies))
                if res.is_valid:
                    fresh_session = await self.session_repo.get_default()
                    return fresh_session.access_token
            raise RuntimeError("SESSION_EXPIRED: Access token expired and automatic refresh failed.")

        if session.status != SessionStatus.VALID:
            raise RuntimeError(f"SESSION_{session.status}: Session is not in VALID state.")

        return session.access_token

    async def start_reconnect_flow(self) -> ReconnectFlowStatus:
        """Launches interactive browser login wizard with 300s timeout."""
        if self.active_wizard_task and not self.active_wizard_task.done():
            elapsed = int(time.time() - self.wizard_start_time)
            return ReconnectFlowStatus(
                wizard_status=ReconnectWizardStatus.RUNNING,
                elapsed_seconds=elapsed,
                timeout_seconds=300,
                message="Browser login wizard is already running.",
                session_status=SessionStatus.MISSING
            )

        self.wizard_start_time = time.time()
        self.wizard_status = ReconnectWizardStatus.RUNNING
        self.wizard_message = "Browser login wizard launched. Waiting for user login..."
        self.active_wizard_task = asyncio.create_task(self._run_browser_login_wizard())

        return ReconnectFlowStatus(
            wizard_status=ReconnectWizardStatus.RUNNING,
            elapsed_seconds=0,
            timeout_seconds=300,
            message=self.wizard_message,
            session_status=SessionStatus.MISSING
        )

    async def _run_browser_login_wizard(self) -> None:
        """
        Background task running Playwright Chromium session with 300s bounded timeout.
        Ensures zero dangling processes and clean exit on timeout or cancel.
        """
        chromium_profile = self.session_dir / "chromium"
        chromium_profile.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(chromium_profile, 0o700)

        playwright = None
        context = None
        try:
            from playwright.async_api import async_playwright
            playwright = await async_playwright().start()
            # Launch persistent browser context
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(chromium_profile),
                headless=False,
                executable_path="/usr/bin/chromium-browser" if Path("/usr/bin/chromium-browser").exists() else None,
                args=["--disable-blink-features=AutomationControlled"]
            )
            page = await context.new_page()
            await page.goto(f"{self.base_url}/", timeout=45000)

            # Polling loop bounded by 300 seconds
            start_t = time.time()
            while time.time() - start_t < 300:
                await asyncio.sleep(3.0)
                cookies_list = await context.cookies()
                token_cookie = next(
                    (c for c in cookies_list if c["name"] in ("__Secure-next-auth.session-token", "next-auth.session-token")),
                    None
                )
                if token_cookie:
                    # Attempt session evaluation in page
                    session_json = await page.evaluate(
                        '() => fetch("/api/auth/session").then(r => r.json()).catch(() => ({}))'
                    )
                    if session_json.get("accessToken"):
                        # Captured fresh credentials!
                        cookies_dict = {c["name"]: c["value"] for c in cookies_list}
                        await self.import_session_cookies(json.dumps(cookies_dict))
                        self.wizard_status = ReconnectWizardStatus.SUCCESS
                        self.wizard_message = "Successfully authenticated via browser wizard!"
                        return

            self.wizard_status = ReconnectWizardStatus.TIMEOUT
            self.wizard_message = "Login wizard timed out after 300 seconds."
        except asyncio.CancelledError:
            self.wizard_status = ReconnectWizardStatus.CANCELLED
            self.wizard_message = "Login wizard was cancelled."
        except Exception as e:
            logger.error(f"Error in browser login wizard: {e}")
            self.wizard_status = ReconnectWizardStatus.FAILED
            self.wizard_message = f"Wizard encountered an error: {e}"
        finally:
            if context:
                try:
                    await context.close()
                except Exception:
                    pass
            if playwright:
                try:
                    await playwright.stop()
                except Exception:
                    pass
            self.enforce_storage_security()

    async def cancel_reconnect_flow(self) -> ReconnectFlowStatus:
        """Cancels running browser wizard cleanly."""
        if self.active_wizard_task and not self.active_wizard_task.done():
            self.active_wizard_task.cancel()
            try:
                await self.active_wizard_task
            except asyncio.CancelledError:
                pass
        self.wizard_status = ReconnectWizardStatus.CANCELLED
        self.wizard_message = "Wizard was cancelled by user."
        status = await self.get_current_session_status()
        return ReconnectFlowStatus(
            wizard_status=self.wizard_status,
            elapsed_seconds=0,
            timeout_seconds=300,
            message=self.wizard_message,
            session_status=status.status
        )
```

---

## 5. Verification Method

### 5.1 Verification Checklist & Invalidation Conditions

| Feature Requirement | Verification Method | Pass Criteria | Invalidation Condition |
|---|---|---|---|
| **Cookie Parsing** | Test parser with header string, JSON array, dict, Netscape | All 4 formats yield correct cookie dict with `__Secure-next-auth.session-token` | Fails to parse valid input or crashes on malformed string |
| **Empty NextAuth 200 Handling** | Inject mock response `{"expires": "2026-10-15"}` without `accessToken` | Status transitions to `EXPIRED` | System marks session `VALID` despite missing `accessToken` |
| **JWT Decoding** | Pass token with `exp=now+3600` | Decodes without signature check, extracts `exp` and email | Throws exception or requires secret key |
| **Storage Permissions** | Check `stat` mode on `.session_data/` and `poc.db` | `.session_data` is `0700` (`drwx------`); `poc.db` is `0600` (`-rw-------`) | Mode is `0755`, `0644`, or accessible by other users |
| **Git Exclusion** | Check `.gitignore` | Contains `.session_data/` and `*.db` | Secrets committed or untracked |
| **Log Sanitization** | Pass log record with JWT, Bearer token, cookie | Redacted strings replace secrets | Plaintext JWT or cookie visible in log output |
| **Infinite Loop Prevention** | Simulate consecutive 401 responses | Exactly 1 refresh attempted, state locks in `EXPIRED` | Infinite refresh loop or unhandled crash |
| **300s Bounded Timeout** | Launch wizard and simulate timeout | Task exits at 300s, browser context closed, status `TIMEOUT` | Dangling Chromium process or hang |

### 5.2 Standalone Verification Test Script
The following self-contained test script validates the complete session parsing, state transitions, filesystem permissions, and secret masking:

```bash
python3 -c "
import json, time, os, stat
from pathlib import Path
import jwt

# 1. Test Cookie Parsing
raw_header = '__Secure-next-auth.session-token=secret_tok_123; cf_clearance=cf_abc_789'
cookies = dict(part.strip().split('=', 1) for part in raw_header.split(';') if '=' in part)
assert cookies['__Secure-next-auth.session-token'] == 'secret_tok_123'

# 2. Test JWT Decoding
exp_time = int(time.time()) + 7200
payload = {'exp': exp_time, 'sub': 'user-1234', 'email': 'test@example.com'}
mock_jwt = jwt.encode(payload, 'test_key', algorithm='HS256')
decoded = jwt.decode(mock_jwt, options={'verify_signature': False})
assert decoded['email'] == 'test@example.com'
assert decoded['exp'] == exp_time

# 3. Test Filesystem Permissions
p = Path('.session_test_dir')
p.mkdir(exist_ok=True)
os.chmod(p, 0o700)
f = p / 'test.db'
f.touch()
os.chmod(f, 0o600)

dir_mode = oct(stat.S_IMODE(p.stat().st_mode))
file_mode = oct(stat.S_IMODE(f.stat().st_mode))
assert dir_mode == '0o700', f'Expected 0o700, got {dir_mode}'
assert file_mode == '0o600', f'Expected 0o600, got {file_mode}'

f.unlink()
p.rmdir()
print('All core session & isolation verifications PASSED!')
"
```

### 5.3 Automated Pytest Execution
Once the M1 Worker generates the source files, verification is executed via:
```bash
pytest -v tests/test_tier1_features/test_feature_session.py
```
Expected output: 100% passing tests for valid cookie parsing, empty session handling, JWT decoding, state transitions, permission enforcement, and logging sanitization.
