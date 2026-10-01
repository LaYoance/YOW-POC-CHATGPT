# M1 Models & Test Spec Investigation Report

**Target Milestone**: Milestone 1: Core Foundation & Session Engine  
**Author**: Explorer M1-3 (Models & Test Spec Explorer)  
**Date**: 2026-10-01T11:22:00Z  
**Output Path**: `/var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/.agents/teamwork/explorer_m1_3/handoff.md`

---

## 1. Observation

Direct system and environment inspections performed:

### 1.1 Python & System Environment
- **Operating System**: Linux x86_64 (`WSL2`, Ubuntu 24.04 LTS Noble)
- **Python Version**: `Python 3.12.3` at `/usr/bin/python3`
- **Pip Version**: `pip 24.0` from `/usr/lib/python3/dist-packages/pip`
- **SQLite Engine**: SQLite version `3.45.1` (built-in standard library `sqlite3`), OpenSSL `3.0.13`
- **Pre-installed System Python Packages** (verified via `pip list` and `importlib.metadata`):
  - `requests`: `2.31.0`
  - `pycurl`: `7.45.3` (libcurl/8.5.0 GnuTLS/3.8.3 zlib/1.3 brotli/1.1.0 zstd/1.5.5 nghttp2/1.59.0)
  - `cryptography`: `41.0.7`
  - `PyJWT`: `2.7.0` (module `jwt`)
  - `rich`: `13.7.1`
  - `Jinja2`: `3.1.2`
  - `typing_extensions`: `4.10.0`
  - `urllib3`: `2.0.7`
  - `certifi`: `2023.11.17`

### 1.2 Sandbox & Installation Constraints
- The sandboxed execution environment enforces strict network containment (`Errno -2 Name or service not known` when running unauthenticated pip requests).
- Consequently, all project dependencies must be cleanly specified in `requirements.txt` with locked versions compatible with Python 3.12, allowing offline or controlled wheel installation during environment setup.
- SQLite WAL mode and recursive Common Table Expressions (`WITH RECURSIVE`) were directly tested and executed in Python 3.12.3:
  - Benchmark test inserting 50 conversations and 500 messages, closing the connection (cold boot), reopening with WAL, and querying active branch via recursive CTE retrieved 10 active messages in **0.742 ms to 0.956 ms**, well within the **< 20 ms** requirement.
  - Foreign key constraints (`PRAGMA foreign_keys = ON;`) require parent conversations to be inserted prior to child messages.
  - Filesystem permission enforcement (`0700` directory, `0600` DB file) was directly validated using `stat.S_IMODE`.

---

## 2. Logic Chain

1. **Python 3.12 Compatibility**:
   - Python 3.12 deprecated `distutils` and modified coroutine/asyncio internals. 
   - `pydantic` v2 (specifically `>=2.6.0`, e.g. `2.6.4`) provides native Rust-compiled `pydantic-core` wheels for Python 3.12, eliminating runtime conversion overhead.
   - Pydantic v2 decoupled environment configuration into `pydantic-settings>=2.2.0`, which is required for `app/config.py`.
   - `fastapi>=0.110.0` fully embraces Pydantic v2 schemas and native response serializations.
   - `aiosqlite>=0.20.0` and `pytest-asyncio>=0.23.0` provide proper event loop lifecycle management for async tests in Python 3.12.

2. **Domain Models Structure (`app/models/`)**:
   - Models must strictly match the interface contracts defined in `PROJECT.md` section 11 and reverse-engineered specifications in `RESEARCH.md`.
   - `SessionCredentials` must never leak raw tokens in logs or error messages; a masking property and sanitized `__repr__` are essential.
   - `ConversationSchema` and `MessageNode` must accurately model ChatGPT's DAG structure (where each message points to a `parent` node and contains a list of `children` node IDs, with `current_node` pointing to the tip of the active branch).
   - `SendMessageRequest` must supply `parent_message_id = current_node` and optional `project_id` (`gizmo_id`), preventing thread fragmentation.

3. **Milestone 1 Test Suite Verification**:
   - `test_feature_session.py` must exhaustively test the session state machine: `MISSING` (cold start) -> `VALID` (after cookie/JWT import) -> `EXPIRED` (on HTTP 401 or expiration) -> `VALID` (on refresh) -> `CHALLENGE_REQUIRED` (on Cloudflare Turnstile HTTP 403).
   - In addition, security isolation must verify directory permission `0700`, database file permission `0600`, and git exclusion.
   - `test_feature_resilience.py` must prove zero-network cold recovery by populating 50 conversations and 500 messages, completely terminating connections, reopening the SQLite WAL database cold, and running the recursive CTE query to extract the active branch in `< 20ms`.
   - It must also verify multi-leaf DAG resolution: when a conversation contains alternate edited branches, the CTE must strictly isolate the branch leading to `current_node` without flattening or omitting turns, and cleanly switch when `current_node` changes.

---

## 3. Caveats

1. **Filesystem `umask` vs Explicit Permissions**:
   - Calling `os.makedirs(dir, mode=0o700)` does not guarantee `0700` if the user's `umask` masks execute/write bits. Therefore, `app/config.py` and `SessionRepo` must explicitly call `os.chmod(dir, 0o700)` and `os.chmod(file, 0o600)`.
2. **SQLite Foreign Keys in Fixtures**:
   - When `PRAGMA foreign_keys = ON;` is enabled, inserting a message whose `conversation_id` does not exist in `conversations` raises `sqlite3.IntegrityError: FOREIGN KEY constraint failed`. Tests and repositories must commit conversation headers before or alongside messages.
3. **Synthetic Root Node in ChatGPT DAG**:
   - ChatGPT conversation trees often have a synthetic root node with `id: "client-created-root"` or a UUID where `message` is `None` or `author.role == "system"`. The CTE query and models must handle nodes with null/empty content without crashing.

---

## 4. Conclusion & Concrete Recommendations

### 4.1 Locked `requirements.txt`

```ini
# Core Web Framework & Server (Python 3.12 compatible)
fastapi>=0.110.0,<0.112.0
uvicorn[standard]>=0.28.0,<0.30.0
pydantic>=2.6.0,<2.8.0
pydantic-settings>=2.2.0,<2.4.0
python-multipart>=0.0.9

# Persistence & Async DB
aiosqlite>=0.20.0,<0.21.0

# Transport & Scraping Engines (R4)
curl_cffi>=0.6.2,<0.8.0
playwright>=1.42.0,<1.45.0
httpx>=0.27.0,<0.28.0

# Authentication & Cryptography
PyJWT>=2.7.0,<3.0.0
cryptography>=41.0.7,<43.0.0
requests>=2.31.0,<3.0.0

# CLI & Formatting
rich>=13.7.1,<14.0.0

# Test Harness
pytest>=8.0.0,<9.0.0
pytest-asyncio>=0.23.0,<0.25.0
```

---

### 4.2 Pydantic v2 Domain Models (`app/models/` and `app/config.py`)

#### A. `app/config.py`
```python
"""Application Configuration & Settings using Pydantic Settings v2."""
import os
import stat
from pathlib import Path
from typing import Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Global configuration settings for ChatGPT Integration POC."""

    APP_NAME: str = "YOW-ChatGPT-Integration-POC"
    ENV: str = "development"
    DEBUG: bool = False
    HOST: str = "127.0.0.1"
    PORT: int = 8000

    # Local Storage & Security Isolation (R1, A7)
    BASE_DIR: Path = Field(default_factory=lambda: Path(__file__).resolve().parent.parent)
    SESSION_DATA_DIR: Path = Path(".session_data")
    DB_FILENAME: str = "poc.db"

    # SQLite Pragmas
    SQLITE_BUSY_TIMEOUT_MS: int = 5000
    SQLITE_SYNCHRONOUS: str = "NORMAL"

    # Transport & Session Timeouts (R4)
    DEFAULT_TRANSPORT_MODE: str = "BROWSER_FETCH"
    RECONNECT_TIMEOUT_SECONDS: int = 300
    UPSTREAM_BASE_URL: str = "https://chatgpt.com"
    BROWSER_HEADLESS: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    @property
    def full_session_data_dir(self) -> Path:
        """Absolute path to isolated session directory."""
        if self.SESSION_DATA_DIR.is_absolute():
            return self.SESSION_DATA_DIR
        return self.BASE_DIR / self.SESSION_DATA_DIR

    @property
    def db_path(self) -> Path:
        """Absolute path to SQLite database."""
        return self.full_session_data_dir / self.DB_FILENAME

    def ensure_isolated_storage(self) -> None:
        """Enforces 0700 permissions on session data directory and 0600 on db file."""
        target_dir = self.full_session_data_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(target_dir, 0o700)

        db_file = self.db_path
        if db_file.exists():
            os.chmod(db_file, 0o600)


settings = Settings()
```

#### B. `app/models/session.py`
```python
"""Session domain models and state machine schemas (R1, A7)."""
import uuid
from enum import Enum
from datetime import datetime, timezone
from typing import Optional, Literal
from pydantic import BaseModel, Field, ConfigDict


class SessionStatus(str, Enum):
    """Session operational states."""
    VALID = "VALID"
    EXPIRED = "EXPIRED"
    MISSING = "MISSING"
    CHALLENGE_REQUIRED = "CHALLENGE_REQUIRED"


class SessionCredentials(BaseModel):
    """Encapsulated session secrets stored in isolated local storage."""
    access_token: str
    session_token: Optional[str] = None
    cf_clearance: Optional[str] = None
    device_id: Optional[str] = None
    user_id: Optional[str] = None
    email: Optional[str] = None
    account_id: Optional[str] = None
    expires_at: Optional[datetime] = None

    def masked_access_token(self) -> str:
        """Returns safe masked representation for diagnostics."""
        if not self.access_token or len(self.access_token) < 10:
            return "***"
        return f"{self.access_token[:4]}...{self.access_token[-4:]}"

    def __repr__(self) -> str:
        """Prevents accidental token leakage in standard loggers."""
        return f"<SessionCredentials user_id={self.user_id} token={self.masked_access_token()}>"

    def __str__(self) -> str:
        return self.__repr__()


class SessionCreate(BaseModel):
    """Payload to import or create a session."""
    raw_cookie_header: Optional[str] = None
    session_token: Optional[str] = None
    access_token: Optional[str] = None
    cf_clearance: Optional[str] = None
    device_id: Optional[str] = None


class SessionSchema(BaseModel):
    """Public session view excluding raw tokens."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    status: SessionStatus = SessionStatus.MISSING
    user_id: Optional[str] = None
    email: Optional[str] = None
    account_id: Optional[str] = None
    expires_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = ConfigDict(from_attributes=True)


class ReconnectRequest(BaseModel):
    """Request to initiate reconnection."""
    mode: Literal["browser", "manual"] = "browser"
    cookie_data: Optional[str] = None


class SessionStatusResponse(BaseModel):
    """Structured response for UI session badge and health checks."""
    status: SessionStatus
    user_id: Optional[str] = None
    email: Optional[str] = None
    expires_at: Optional[datetime] = None
    is_authenticated: bool
    message: str
    reconnect_guidance: Optional[str] = None
```

#### C. `app/models/project.py`
```python
"""Project and Project Context models (R2, A5)."""
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field, ConfigDict


class ProjectFileSchema(BaseModel):
    """File attached to a ChatGPT Project (Gizmo)."""
    id: str
    name: str
    size_bytes: Optional[int] = None
    mime_type: Optional[str] = None
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class ProjectSchema(BaseModel):
    """ChatGPT Project metadata and instructions."""
    id: str  # Project ID / gizmo_id (e.g. g-p-674b8892abcdef)
    title: str
    description: Optional[str] = None
    instructions: Optional[str] = None  # Custom system instructions
    gizmo_id: Optional[str] = None
    files: List[ProjectFileSchema] = Field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class ProjectListResponse(BaseModel):
    """Response returned by GET /api/projects."""
    items: List[ProjectSchema]
    total: int


class ProjectContextSchema(BaseModel):
    """Resolved project context including witness markers and files."""
    project: ProjectSchema
    effective_instructions: str
    attached_files_count: int
    gizmo_id: Optional[str] = None
```

#### D. `app/models/conversation.py`
```python
"""Conversation and Message DAG models (R2, R3, A2)."""
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field, ConfigDict


class MessageAuthor(BaseModel):
    """Message sender identity."""
    role: Literal["user", "assistant", "system", "tool"] = "user"
    name: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class MessageContent(BaseModel):
    """Message text payload."""
    content_type: str = "text"
    parts: List[str] = Field(default_factory=list)

    @property
    def text(self) -> str:
        """Concatenates parts into single text string."""
        return "".join(self.parts)


class MessageNode(BaseModel):
    """Individual node in ChatGPT conversation DAG."""
    id: str
    conversation_id: Optional[str] = None
    parent: Optional[str] = None
    children: List[str] = Field(default_factory=list)
    author_role: str = "user"
    content_text: str = ""
    status: Optional[str] = "finished_successfully"
    created_at: Optional[datetime] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(from_attributes=True)


class ConversationSchema(BaseModel):
    """ChatGPT conversation metadata."""
    id: str
    title: str
    project_id: Optional[str] = None
    current_node: Optional[str] = None  # Leaf UUID of active branch
    created_at: datetime
    updated_at: datetime
    mapping: Optional[Dict[str, MessageNode]] = None

    model_config = ConfigDict(from_attributes=True)


class ActiveBranchResponse(BaseModel):
    """Chronologically ordered active branch from root to leaf."""
    conversation_id: str
    title: str
    project_id: Optional[str] = None
    current_node: Optional[str] = None
    messages: List[MessageNode]
    total_messages: int


class SendMessageRequest(BaseModel):
    """Outbound message payload for continuing conversation."""
    conversation_id: Optional[str] = None
    parent_message_id: Optional[str] = None  # Must point to current_node
    project_id: Optional[str] = None  # Bound gizmo_id
    content: str
    model: str = "auto"


class PaginatedConversations(BaseModel):
    """Paginated conversation list."""
    items: List[ConversationSchema]
    total: int
    offset: int = 0
    limit: int = 28
    has_more: bool = False
```

#### E. `app/models/transport.py`
```python
"""Transport layer modes and audit records (R4)."""
from enum import Enum
from datetime import datetime, timezone
from typing import Optional, List, Dict
from pydantic import BaseModel, Field, ConfigDict


class TransportMode(str, Enum):
    """Available transport mechanisms."""
    DIRECT_HTTP = "DIRECT_HTTP"      # Mode 1: curl_cffi / Direct HTTP
    BROWSER_FETCH = "BROWSER_FETCH"  # Mode 2: Playwright CDP browser context fetch
    UI_DRIVEN = "UI_DRIVEN"          # Mode 3: DOM automation fallback


class TransportAuditRecord(BaseModel):
    """Execution telemetry for each transport operation."""
    id: Optional[int] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    operation: str
    attempted_mode: TransportMode
    resolved_mode: TransportMode
    status_code: Optional[int] = None
    latency_ms: float
    success: bool
    error_message: Optional[str] = None
    fallback_triggered: bool = False

    model_config = ConfigDict(from_attributes=True)


class TransportStatusReport(BaseModel):
    """Current health and configuration of the transport router."""
    current_mode: TransportMode
    forced_mode: Optional[TransportMode] = None
    available_modes: List[TransportMode] = Field(
        default_factory=lambda: [
            TransportMode.DIRECT_HTTP,
            TransportMode.BROWSER_FETCH,
            TransportMode.UI_DRIVEN
        ]
    )
    last_successful_mode: Optional[TransportMode] = None
    consecutive_failures: Dict[str, int] = Field(default_factory=dict)
    circuit_breaker_open: bool = False
```

---

### 4.3 Milestone 1 Test Suite Design

#### Test Suite 1: `tests/test_tier1_features/test_feature_session.py`
Target Feature: **F01 (Session Lifecycle & Local Isolation)**  
Test cases to implement:

1. `test_session_initial_missing_state()`:
   - When no database or credentials exist, `get_current_session_status()` returns `SessionStatus.MISSING`.
   - `is_authenticated` is `False`.
   - `reconnect_guidance` is populated with human-readable instructions.
2. `test_session_cookie_import_and_valid_state()`:
   - Provide a valid NextAuth cookie header containing `__Secure-next-auth.session-token` and `cf_clearance`.
   - Service parses cookies, decodes JWT claims (`user_id`, `exp`, `email`), persists credentials in isolated DB.
   - Status transitions to `SessionStatus.VALID`, `is_authenticated=True`.
3. `test_session_expiration_transition()`:
   - Given an active session, simulate an upstream HTTP 401 response or token whose `exp <= now()`.
   - Session state transitions cleanly to `SessionStatus.EXPIRED`.
   - Service returns clear expiration message without throwing unhandled exceptions.
4. `test_session_reconnect_recovery()`:
   - Start from `SessionStatus.EXPIRED`.
   - Call reconnect with refreshed cookies or simulated `/api/auth/session` response.
   - Status transitions from `EXPIRED` back to `VALID`.
5. `test_session_challenge_required_transition()`:
   - Simulate an upstream HTTP 403 or Cloudflare Turnstile challenge.
   - Session state transitions to `SessionStatus.CHALLENGE_REQUIRED`.
   - System prompts user to solve Turnstile via headed browser.
6. `test_local_directory_isolation_permissions()`:
   - Verify `.session_data/` directory has filesystem permission `0700` (`drwx------`).
   - Verify `poc.db` database file has filesystem permission `0600` (`-rw-------`).
   - Verify non-owner read/write access is denied.
7. `test_secret_sanitization_and_isolation()`:
   - Verify `repr(credentials)` and `str(credentials)` mask `access_token` and `session_token`.
   - Verify logger output does not contain raw JWT strings.
   - Verify `.gitignore` contains `.session_data/` and `*.db`.

#### Test Suite 2: `tests/test_tier1_features/test_feature_resilience.py`
Target Feature: **F09 (SQLite Persistence & Restart Resilience)**  
Test cases to implement:

1. `test_sqlite_wal_pragmas_and_schema_initialization()`:
   - Initialize database engine.
   - Execute PRAGMA checks:
     - `PRAGMA journal_mode;` returns `wal`.
     - `PRAGMA foreign_keys;` returns `1` (ON).
     - `PRAGMA synchronous;` returns `1` (NORMAL) or `2` (FULL).
     - `PRAGMA busy_timeout;` returns `5000`.
   - Assert all required tables (`sessions`, `projects`, `conversations`, `messages`, `transport_logs`) and indexes exist.
2. `test_cold_boot_recovery_benchmark()`:
   - Populate database with 50 conversations and 500 messages (10 messages per conversation).
   - Close SQLite connection completely, destroying all in-memory connection pools and caches.
   - Record `t0 = time.perf_counter()`.
   - Reopen connection to existing SQLite WAL database from cold state.
   - Execute recursive CTE query to retrieve active branch for a target conversation.
   - Record `t1 = time.perf_counter()`.
   - Assert `elapsed_ms = (t1 - t0) * 1000 < 20.0` ms. (Observed benchmark: < 1.0 ms).
   - Assert all 10 messages are retrieved in exact root-to-leaf chronological sequence.
3. `test_active_branch_recovery_from_branching_dag()`:
   - Construct a conversation with branching turns:
     - Root `m0` (user) -> `m1` (assistant).
     - User edits Turn 1:
       - Branch A: `m2a` (user edit A) -> `m3a` (assistant answer A).
       - Branch B: `m2b` (user edit B) -> `m3b` (assistant answer B) -> `m4b` (user follow-up) -> `m5b` (assistant response).
   - Set `current_node = 'm5b'`.
   - Execute recursive CTE query.
   - Assert active branch is strictly `['m0', 'm1', 'm2b', 'm3b', 'm4b', 'm5b']` (excluding `m2a` and `m3a`).
4. `test_active_branch_switch_leaf()`:
   - In the same branching DAG, update conversation `current_node = 'm3a'`.
   - Re-execute recursive CTE query.
   - Assert returned active branch immediately switches to `['m0', 'm1', 'm2a', 'm3a']`.
5. `test_cold_restart_data_integrity()`:
   - Close and restart process.
   - Verify zero data loss across all 50 conversations and 500 messages.
   - Verify no duplicate rows, no orphan foreign keys, and no message corruption.
6. `test_synthetic_root_and_orphan_resilience()`:
   - Test conversation where root node has `message: None` or empty content.
   - Verify recursive CTE terminates cleanly at root without recursion depth overflow or errors.

---

## 5. Verification Method

To independently verify all findings and test specifications:

1. **Verify Prototype Test Script**:
   ```bash
   python3 /var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/.agents/teamwork/explorer_m1_3/test_verify_specs.py
   ```
   **Expected Result**:
   - `[PASS] Session lifecycle state transitions verified.`
   - `[PASS] Directory and file isolation permissions (0700/0600) verified.`
   - `[PASS] Cold restart recovery: 10 messages fetched in 0.956 ms (budget < 20ms).`
   - `[PASS] Active branch B correctly isolated: ['m0', 'm1', 'm2b', 'm3b', 'm4b', 'm5b']`
   - `[PASS] Active branch A correctly isolated upon switch: ['m0', 'm1', 'm2a', 'm3a']`
   - `ALL TEST SPEC PROTOTYPES AND BENCHMARKS PASSED SUCCESSFULLY!`

2. **Verify SQLite WAL Performance Independently**:
   - Run the recursive CTE benchmark with `time.perf_counter()`.
   - Invalidation condition: retrieval time exceeds 20.0ms or active branch includes messages from abandoned edits.

3. **Verify Security Isolation**:
   - Check `stat -c "%a" .session_data` -> must be `700`.
   - Check `stat -c "%a" .session_data/poc.db` -> must be `600`.
   - Invalidation condition: file readable by non-owner group/others.
