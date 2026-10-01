# Milestone 1 Persistence Architecture Handoff Report
**Subagent**: Explorer M1-1 (Persistence & Database Architecture)  
**Target Milestone**: Milestone 1 — Core Foundation & Session Engine  
**Working Directory**: `/var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/.agents/teamwork/explorer_m1_1`  
**Date**: 2026-10-01  

---

## 1. Observation

### 1.1 Host Environment & SQLite Engine
- **Host OS**: Linux (`Ubuntu 24.04 LTS`, Linux kernel 6.x)
- **Python**: `3.12.3` (`/usr/bin/python3`)
- **SQLite Engine**: `3.45.1` (Python standard library `sqlite3` linked against `libsqlite3.so.0`)
- **Verified SQLite Capabilities**:
  - Full Write-Ahead Logging (WAL) support (`PRAGMA journal_mode = WAL`)
  - Common Table Expressions with recursion (`WITH RECURSIVE`)
  - Foreign key constraints enforcement (`PRAGMA foreign_keys = ON`)
  - Deferred foreign key evaluation (`DEFERRABLE INITIALLY DEFERRED`)
  - Standard JSON support functions

### 1.2 Upstream Contracts & Scope Reference
- **`PROJECT.md`** (Lines 15, 30-36, 110-130):
  - Defines the Local Persistence Layer (R3, A6): SQLite in WAL mode with recursive CTE active branch reconstruction.
  - Requires `app/persistence/`: `db.py`, `session_repo.py`, `project_repo.py`, `conversation_repo.py`, `transport_log_repo.py`.
- **`ORIGINAL_REQUEST.md`** (Lines 18-35, 48-73):
  - R1: Strict local secret isolation (0700 permissions, tokens never leaked or committed).
  - R2: Active branch identification without tree flattening or turn omission.
  - R3 / A6: Full continuity across complete service restarts without duplicating threads or messages.
  - A8: 3 successive continuity cycles without duplicates.
- **`RESEARCH.md`** (Lines 24-47, 50-70):
  - Local database path: `.session_data/poc.db` (permissions `0600`), directory `.session_data/` (permissions `0700`).
  - Conversation DAG mapping: nodes with `id`, `parent`, `children`, `message`, and conversation `current_node`.

### 1.3 Empirical Benchmark & Experimental Observations
The following experiments were executed directly in Python 3.12 on this system:

1. **SQLite PRAGMA Verification**:
   - Command:
     ```python
     cur.execute("PRAGMA journal_mode = WAL;")
     cur.execute("PRAGMA foreign_keys = ON;")
     cur.execute("PRAGMA busy_timeout = 5000;")
     cur.execute("PRAGMA synchronous = NORMAL;")
     ```
   - Verbatim Output: `journal_mode=wal, foreign_keys=1, busy_timeout=5000, synchronous=1`.

2. **Foreign Key Ordering Observation in DAG Mapping Ingestion**:
   - When ChatGPT sends a conversation mapping, nodes are stored in an unordered dictionary.
   - If child messages are inserted before parent messages with immediate foreign keys, SQLite raises:
     `sqlite3.IntegrityError: FOREIGN KEY constraint failed`.
   - By declaring `FOREIGN KEY (parent_id) REFERENCES messages(id) ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED`, arbitrary insertion ordering within an atomic transaction completes with zero errors, verifying referential integrity at transaction commit.

3. **Recursive CTE Performance & Branch Isolation**:
   - Dataset: Conversation with 100 active turns + 20 alternate forked branches.
   - Latency: Average **0.2345 ms** per recursive CTE traversal (over 20x faster than the 5.0ms requirement).
   - Correctness: Exactly 100 active chronological nodes extracted (`m0` to `m99`); all 20 alternate nodes excluded.
   - Dynamic Branch Switching: Updating `conversations.current_node_id` immediately reconstructed the alternate branch without modifying individual message rows.

4. **Query Execution Plan (`EXPLAIN QUERY PLAN`)**:
   - Global Conversations Listing:
     `SCAN conversations USING INDEX idx_conversations_updated` — eliminates filesort entirely.
   - Project-Filtered Conversations Listing:
     `SEARCH conversations USING INDEX idx_conversations_project (project_id=?)` — index point-and-range scan.
   - Recursive CTE Traversal:
     Anchor step: `SEARCH messages USING INDEX sqlite_autoindex_messages_1 (id=?)` (O(1) B-tree lookup).
     Recursive step: `SEARCH m USING INDEX sqlite_autoindex_messages_1 (id=?)` (O(1) B-tree lookup per parent step).

5. **Cold-Boot Benchmark (A6 Acceptance Criterion)**:
   - Cold startup test: Pre-populated database with 1 session, 5 projects, 50 conversations, 500 messages.
   - Simulating complete service restart from cold disk:
     - Open DB file
     - Enforce WAL pragmas
     - Load session status
     - Load projects list
     - Load page 1 of conversations (28 items)
     - Reconstruct active branch for the top conversation via recursive CTE
   - Verbatim Output:
     `Cold boot latencies (10 runs): [1.18, 0.89, 0.84, 0.8, 0.75, 1.18, 1.78, 0.92, 0.76, 0.91] ms`
     `Mean cold boot latency: 1.00 ms (Target: < 20.00 ms)`
     `Max cold boot latency: 1.78 ms`
     `Zero external network calls initiated.`

---

## 2. Logic Chain

```
[Observation 1.1: SQLite 3.45.1 in Python 3.12]
         │
         ▼
[Step 1: Enforce WAL Engine Pragmas in app/persistence/db.py]
  - journal_mode = WAL: enables concurrent reads during writes.
  - synchronous = NORMAL: eliminates redundant fsync calls while guaranteeing durability.
  - foreign_keys = ON: enforces referential integrity on every connection.
  - busy_timeout = 5000: prevents OperationalError during concurrent write contention.
         │
         ▼
[Observation 1.3.2: Topological ingestion order in DAG mappings]
         │
         ▼
[Step 2: Table DDL & Referential Integrity]
  - messages.parent_id uses DEFERRABLE INITIALLY DEFERRED to prevent ingestion order errors.
  - conversations.project_id uses ON DELETE SET NULL to preserve chats if projects are removed.
  - messages.conversation_id uses ON DELETE CASCADE to guarantee complete purge when chat deleted.
         │
         ▼
[Observation 1.3.4: EXPLAIN QUERY PLAN verification]
         │
         ▼
[Step 3: Index Architecture]
  - idx_conversations_updated: guarantees O(1) start for global paginated feeds.
  - idx_conversations_project: guarantees compound index filtering by project + sorted updated_at.
  - idx_messages_conv_parent: indexes parent-child navigation.
  - idx_messages_conv_active: provides fast filtered scan for active markers.
         │
         ▼
[Observation 1.3.3: Recursive CTE latency 0.23ms]
         │
         ▼
[Step 4: Active Branch Traversal Algorithm]
  - Traverse child -> parent using current_node_id leaf pointer.
  - Order by depth DESC to emit strictly chronological root -> leaf message stream.
  - Guard recursion with WHERE depth < 1000 against malformed graph cycles.
         │
         ▼
[Observation 1.3.5: Cold-Boot 1.00ms latency without network]
         │
         ▼
[Step 5: Cold-Boot & Recovery Strategy (A6)]
  - Separation of concerns: Phase 1 (Local Hydration <2ms) vs Phase 2 (Background Sync).
  - UI boots instantly from SQLite WAL offline without network stalls.
```

---

## 3. Caveats

1. **Single Writer Concurrency**: SQLite WAL allows unlimited concurrent readers, but only one write transaction at any given microsecond. The `PRAGMA busy_timeout = 5000;` prevents `database is locked` errors by automatically waiting up to 5 seconds. Write transactions should be kept concise and use batch operations (`executemany`).
2. **Connection Lifecycle in Multi-Threaded / Async FastAPI**: Python `sqlite3.Connection` instances are not thread-safe if shared across concurrent threads. The recommended architecture in `app/persistence/db.py` uses a connection factory context manager (`get_db_connection()`) that provisions a properly configured connection per thread/task, guaranteeing isolation.
3. **Foreign Keys are Per-Connection**: In SQLite, `PRAGMA foreign_keys = ON;` is **not** stored persistently in the database header. It **must** be executed on every newly established connection. The `DatabaseManager` handles this automatically in its connection hook.
4. **Filesystem Permission Mask**: While POSIX systems respect `0700` and `0600` permissions via `os.chmod`, Windows or certain containerized overlay mounts might ignore POSIX permission bits. The permission check should be wrapped defensively.

---

## 4. Conclusion & Concrete Implementation Recommendations

### 4.1 Database Engine & Lifecycle (`app/persistence/db.py`)

The database manager must provide:
- Strict local isolation in `.session_data/poc.db` with `0700` directory and `0600` file permissions.
- Automatic pragma configuration on every connection.
- Idempotent schema initialization (`init_db()`).
- Context-managed connection checkout with automatic commit and rollback.

```python
"""
app/persistence/db.py
SQLite WAL Database Lifecycle & Connection Management
"""
import os
import stat
import sqlite3
import logging
from contextlib import contextmanager
from typing import Generator, Optional

logger = logging.getLogger("app.persistence.db")

DEFAULT_DB_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.session_data"))
DEFAULT_DB_PATH = os.path.join(DEFAULT_DB_DIR, "poc.db")

PRAGMAS = [
    "PRAGMA journal_mode = WAL;",
    "PRAGMA foreign_keys = ON;",
    "PRAGMA busy_timeout = 5000;",
    "PRAGMA synchronous = NORMAL;",
    "PRAGMA temp_store = MEMORY;",
    "PRAGMA cache_size = -64000;",      # 64 MB cache
]

SCHEMA_DDL = """
-- 1. SESSIONS TABLE
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY DEFAULT 'default',
    access_token TEXT,
    cookies_json TEXT NOT NULL DEFAULT '{}',
    user_agent TEXT NOT NULL DEFAULT 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    device_id TEXT,
    account_id TEXT,
    user_email TEXT,
    user_name TEXT,
    status TEXT NOT NULL DEFAULT 'MISSING', -- VALID, EXPIRED, MISSING, CHALLENGE_REQUIRED
    expires_at TIMESTAMP,
    last_verified_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. PROJECTS TABLE
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, -- Gizmo ID: g-p-...
    title TEXT NOT NULL,
    description TEXT,
    instructions TEXT,
    files_metadata_json TEXT DEFAULT '[]',
    workspace_id TEXT,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    last_synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_projects_title ON projects(title ASC);

-- 3. CONVERSATIONS TABLE
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'New Chat',
    project_id TEXT,
    current_node_id TEXT,
    model_slug TEXT DEFAULT 'gpt-4o',
    is_archived BOOLEAN DEFAULT 0,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    last_synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_project ON conversations(project_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at DESC);

-- 4. MESSAGES TABLE
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    parent_id TEXT,
    role TEXT NOT NULL, -- 'user', 'assistant', 'system', 'tool'
    content_text TEXT NOT NULL,
    content_type TEXT DEFAULT 'text',
    metadata_json TEXT DEFAULT '{}',
    status TEXT DEFAULT 'finished_successfully',
    is_active_branch BOOLEAN DEFAULT 0,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_id) REFERENCES messages(id) ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX IF NOT EXISTS idx_messages_conv_parent ON messages(conversation_id, parent_id);
CREATE INDEX IF NOT EXISTS idx_messages_conv_active ON messages(conversation_id, is_active_branch);
CREATE INDEX IF NOT EXISTS idx_messages_conv_created ON messages(conversation_id, created_at ASC);

-- 5. SYNC WATERMARKS TABLE
CREATE TABLE IF NOT EXISTS sync_watermarks (
    resource_key TEXT PRIMARY KEY,
    last_sync_timestamp TIMESTAMP,
    etag TEXT,
    cursor TEXT,
    sync_status TEXT DEFAULT 'IDLE',
    last_error TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 6. TRANSPORT AUDIT LOGS TABLE
CREATE TABLE IF NOT EXISTS transport_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation TEXT NOT NULL,
    transport_mode TEXT NOT NULL,
    target_id TEXT,
    status_code INTEGER,
    latency_ms INTEGER,
    success BOOLEAN NOT NULL,
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_transport_logs_op ON transport_logs(operation, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_transport_logs_mode ON transport_logs(transport_mode, created_at DESC);
"""

class DatabaseManager:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or os.environ.get("DATABASE_PATH", DEFAULT_DB_PATH)
        self.db_dir = os.path.dirname(self.db_path)

    def init_db(self) -> None:
        """Initialize directory with 0700 permissions and execute DDL."""
        if not os.path.exists(self.db_dir):
            os.makedirs(self.db_dir, mode=0o700, exist_ok=True)
            try:
                os.chmod(self.db_dir, 0o700)
            except OSError as e:
                logger.warning(f"Could not enforce 0700 on {self.db_dir}: {e}")

        conn = self._connect()
        try:
            with conn:
                conn.executescript(SCHEMA_DDL)
            logger.info(f"Database schema initialized successfully at {self.db_path}")
        finally:
            conn.close()

        # Enforce 0600 on DB file
        if os.path.exists(self.db_path):
            try:
                os.chmod(self.db_path, 0o600)
            except OSError as e:
                logger.warning(f"Could not enforce 0600 on {self.db_path}: {e}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=5.0,
            check_same_thread=False,
            isolation_level=None  # Explicit autocommit management
        )
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        for pragma in PRAGMAS:
            cur.execute(pragma)
        return conn

    @contextmanager
    def get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager for thread-safe database operations."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            yield conn
            conn.execute("COMMIT;")
        except Exception:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    @contextmanager
    def get_read_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Read-only connection context manager (never takes immediate write lock)."""
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

# Global default instance
db_manager = DatabaseManager()
```

---

### 4.2 Repositories Implementation

#### A. Session Repository (`app/persistence/session_repo.py`)
```python
"""
app/persistence/session_repo.py
Session Persistence & Credentials Repository
"""
import json
import sqlite3
from typing import Optional, Dict, Any
from app.persistence.db import db_manager, DatabaseManager

class SessionRepo:
    def __init__(self, db: DatabaseManager = db_manager):
        self.db = db

    def get_default(self) -> Optional[Dict[str, Any]]:
        query = """
        SELECT id, access_token, cookies_json, user_agent, device_id, account_id,
               user_email, user_name, status, expires_at, last_verified_at, created_at, updated_at
        FROM sessions
        WHERE id = 'default';
        """
        with self.db.get_read_connection() as conn:
            row = conn.execute(query).fetchone()
            if not row:
                return None
            res = dict(row)
            try:
                res["cookies"] = json.loads(res.get("cookies_json") or "{}")
            except Exception:
                res["cookies"] = {}
            return res

    def upsert_session(self, session_data: Dict[str, Any]) -> Dict[str, Any]:
        cookies_json = session_data.get("cookies_json")
        if cookies_json is None and "cookies" in session_data:
            cookies_json = json.dumps(session_data["cookies"])
        elif cookies_json is None:
            cookies_json = "{}"

        sql = """
        INSERT INTO sessions (
            id, access_token, cookies_json, user_agent, device_id, account_id,
            user_email, user_name, status, expires_at, last_verified_at, updated_at
        ) VALUES (
            :id, :access_token, :cookies_json, :user_agent, :device_id, :account_id,
            :user_email, :user_name, :status, :expires_at, :last_verified_at, CURRENT_TIMESTAMP
        )
        ON CONFLICT(id) DO UPDATE SET
            access_token = COALESCE(excluded.access_token, sessions.access_token),
            cookies_json = excluded.cookies_json,
            user_agent = excluded.user_agent,
            device_id = COALESCE(excluded.device_id, sessions.device_id),
            account_id = COALESCE(excluded.account_id, sessions.account_id),
            user_email = COALESCE(excluded.user_email, sessions.user_email),
            user_name = COALESCE(excluded.user_name, sessions.user_name),
            status = excluded.status,
            expires_at = COALESCE(excluded.expires_at, sessions.expires_at),
            last_verified_at = COALESCE(excluded.last_verified_at, sessions.last_verified_at),
            updated_at = CURRENT_TIMESTAMP;
        """
        params = {
            "id": session_data.get("id", "default"),
            "access_token": session_data.get("access_token"),
            "cookies_json": cookies_json,
            "user_agent": session_data.get("user_agent", "Mozilla/5.0 Chrome/124.0.0.0"),
            "device_id": session_data.get("device_id"),
            "account_id": session_data.get("account_id"),
            "user_email": session_data.get("user_email"),
            "user_name": session_data.get("user_name"),
            "status": session_data.get("status", "VALID"),
            "expires_at": session_data.get("expires_at"),
            "last_verified_at": session_data.get("last_verified_at")
        }
        with self.db.get_connection() as conn:
            conn.execute(sql, params)
        return self.get_default()

    def update_status(self, status: str, session_id: str = "default") -> None:
        sql = "UPDATE sessions SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?;"
        with self.db.get_connection() as conn:
            conn.execute(sql, (status, session_id))

    def get_credentials(self, session_id: str = "default") -> Optional[Dict[str, Any]]:
        sess = self.get_default()
        if not sess or sess.get("status") == "MISSING":
            return None
        return {
            "access_token": sess.get("access_token"),
            "cookies": sess.get("cookies", {}),
            "user_agent": sess.get("user_agent"),
            "device_id": sess.get("device_id"),
            "status": sess.get("status")
        }
```

#### B. Project Repository (`app/persistence/project_repo.py`)
```python
"""
app/persistence/project_repo.py
Project Metadata & Instructions Repository
"""
import json
from typing import List, Optional, Dict, Any
from app.persistence.db import db_manager, DatabaseManager

class ProjectRepo:
    def __init__(self, db: DatabaseManager = db_manager):
        self.db = db

    def upsert_project(self, project: Dict[str, Any]) -> Dict[str, Any]:
        files_json = project.get("files_metadata_json")
        if files_json is None and "files" in project:
            files_json = json.dumps(project["files"])
        elif files_json is None:
            files_json = "[]"

        sql = """
        INSERT INTO projects (
            id, title, description, instructions, files_metadata_json, workspace_id, created_at, updated_at, last_synced_at
        ) VALUES (
            :id, :title, :description, :instructions, :files_metadata_json, :workspace_id, :created_at, :updated_at, CURRENT_TIMESTAMP
        )
        ON CONFLICT(id) DO UPDATE SET
            title = excluded.title,
            description = excluded.description,
            instructions = COALESCE(excluded.instructions, projects.instructions),
            files_metadata_json = COALESCE(excluded.files_metadata_json, projects.files_metadata_json),
            workspace_id = COALESCE(excluded.workspace_id, projects.workspace_id),
            updated_at = COALESCE(excluded.updated_at, projects.updated_at),
            last_synced_at = CURRENT_TIMESTAMP;
        """
        params = {
            "id": project["id"],
            "title": project["title"],
            "description": project.get("description"),
            "instructions": project.get("instructions"),
            "files_metadata_json": files_json,
            "workspace_id": project.get("workspace_id"),
            "created_at": project.get("created_at"),
            "updated_at": project.get("updated_at")
        }
        with self.db.get_connection() as conn:
            conn.execute(sql, params)
        return self.get_project_by_id(project["id"])

    def list_projects(self) -> List[Dict[str, Any]]:
        sql = """
        SELECT id, title, description, instructions, files_metadata_json, workspace_id, created_at, updated_at, last_synced_at
        FROM projects
        ORDER BY title ASC;
        """
        with self.db.get_read_connection() as conn:
            rows = conn.execute(sql).fetchall()
            results = []
            for r in rows:
                item = dict(r)
                try:
                    item["files"] = json.loads(item.get("files_metadata_json") or "[]")
                except Exception:
                    item["files"] = []
                results.append(item)
            return results

    def get_project_by_id(self, project_id: str) -> Optional[Dict[str, Any]]:
        sql = """
        SELECT id, title, description, instructions, files_metadata_json, workspace_id, created_at, updated_at, last_synced_at
        FROM projects
        WHERE id = ?;
        """
        with self.db.get_read_connection() as conn:
            row = conn.execute(sql, (project_id,)).fetchone()
            if not row:
                return None
            res = dict(row)
            try:
                res["files"] = json.loads(res.get("files_metadata_json") or "[]")
            except Exception:
                res["files"] = []
            return res
```

#### C. Conversation & Active Branch Repository (`app/persistence/conversation_repo.py`)
```python
"""
app/persistence/conversation_repo.py
Conversation Tree & Recursive CTE Active Branch Repository
"""
import json
from typing import List, Tuple, Optional, Dict, Any
from app.persistence.db import db_manager, DatabaseManager

class ConversationRepo:
    def __init__(self, db: DatabaseManager = db_manager):
        self.db = db

    def upsert_conversation(self, conv: Dict[str, Any]) -> Dict[str, Any]:
        sql = """
        INSERT INTO conversations (
            id, title, project_id, current_node_id, model_slug, is_archived, created_at, updated_at, last_synced_at
        ) VALUES (
            :id, :title, :project_id, :current_node_id, :model_slug, :is_archived, :created_at, :updated_at, CURRENT_TIMESTAMP
        )
        ON CONFLICT(id) DO UPDATE SET
            title = excluded.title,
            project_id = COALESCE(excluded.project_id, conversations.project_id),
            current_node_id = COALESCE(excluded.current_node_id, conversations.current_node_id),
            model_slug = COALESCE(excluded.model_slug, conversations.model_slug),
            is_archived = COALESCE(excluded.is_archived, conversations.is_archived),
            updated_at = excluded.updated_at,
            last_synced_at = CURRENT_TIMESTAMP;
        """
        params = {
            "id": conv["id"],
            "title": conv.get("title", "New Chat"),
            "project_id": conv.get("project_id"),
            "current_node_id": conv.get("current_node_id"),
            "model_slug": conv.get("model_slug", "gpt-4o"),
            "is_archived": 1 if conv.get("is_archived") else 0,
            "created_at": conv["created_at"],
            "updated_at": conv["updated_at"]
        }
        with self.db.get_connection() as conn:
            conn.execute(sql, params)
        return self.get_conversation_by_id(conv["id"])

    def list_conversations(
        self, project_id: Optional[str] = None, offset: int = 0, limit: int = 28
    ) -> Tuple[List[Dict[str, Any]], int]:
        with self.db.get_read_connection() as conn:
            if project_id:
                count_sql = "SELECT COUNT(*) FROM conversations WHERE project_id = ?;"
                total = conn.execute(count_sql, (project_id,)).fetchone()[0]

                data_sql = """
                SELECT id, title, project_id, current_node_id, model_slug, is_archived, created_at, updated_at, last_synced_at
                FROM conversations
                WHERE project_id = ?
                ORDER BY updated_at DESC
                LIMIT ? OFFSET ?;
                """
                rows = conn.execute(data_sql, (project_id, limit, offset)).fetchall()
            else:
                count_sql = "SELECT COUNT(*) FROM conversations;"
                total = conn.execute(count_sql).fetchone()[0]

                data_sql = """
                SELECT id, title, project_id, current_node_id, model_slug, is_archived, created_at, updated_at, last_synced_at
                FROM conversations
                ORDER BY updated_at DESC
                LIMIT ? OFFSET ?;
                """
                rows = conn.execute(data_sql, (limit, offset)).fetchall()

            return [dict(r) for r in rows], total

    def get_conversation_by_id(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        sql = """
        SELECT id, title, project_id, current_node_id, model_slug, is_archived, created_at, updated_at, last_synced_at
        FROM conversations
        WHERE id = ?;
        """
        with self.db.get_read_connection() as conn:
            row = conn.execute(sql, (conversation_id,)).fetchone()
            return dict(row) if row else None

    def update_current_node(self, conversation_id: str, current_node_id: str) -> None:
        sql = "UPDATE conversations SET current_node_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?;"
        with self.db.get_connection() as conn:
            conn.execute(sql, (current_node_id, conversation_id))

    def save_messages(self, messages: List[Dict[str, Any]]) -> None:
        """Batch insert/upsert messages with deferred foreign key checks."""
        sql = """
        INSERT INTO messages (
            id, conversation_id, parent_id, role, content_text, content_type,
            metadata_json, status, is_active_branch, created_at, updated_at
        ) VALUES (
            :id, :conversation_id, :parent_id, :role, :content_text, :content_type,
            :metadata_json, :status, :is_active_branch, :created_at, :updated_at
        )
        ON CONFLICT(id) DO UPDATE SET
            parent_id = COALESCE(excluded.parent_id, messages.parent_id),
            role = excluded.role,
            content_text = excluded.content_text,
            content_type = excluded.content_type,
            metadata_json = COALESCE(excluded.metadata_json, messages.metadata_json),
            status = excluded.status,
            is_active_branch = excluded.is_active_branch,
            updated_at = COALESCE(excluded.updated_at, CURRENT_TIMESTAMP);
        """
        records = []
        for m in messages:
            meta = m.get("metadata_json")
            if meta is None and "metadata" in m:
                meta = json.dumps(m["metadata"])
            elif meta is None:
                meta = "{}"

            records.append({
                "id": m["id"],
                "conversation_id": m["conversation_id"],
                "parent_id": m.get("parent_id"),
                "role": m.get("role", "user"),
                "content_text": m.get("content_text", ""),
                "content_type": m.get("content_type", "text"),
                "metadata_json": meta,
                "status": m.get("status", "finished_successfully"),
                "is_active_branch": 1 if m.get("is_active_branch") else 0,
                "created_at": m["created_at"],
                "updated_at": m.get("updated_at")
            })

        with self.db.get_connection() as conn:
            conn.executemany(sql, records)

    def get_active_branch(self, conversation_id: str) -> List[Dict[str, Any]]:
        """
        Recursive CTE active branch traversal.
        Traverses backwards from current_node_id to root, ordered by depth DESC
        to return pure chronological active turns in <0.5ms.
        """
        cte_query = """
        WITH RECURSIVE branch(
            id, conversation_id, parent_id, role, content_text, content_type,
            metadata_json, status, created_at, updated_at, depth
        ) AS (
            -- Anchor: Leaf message pointed to by conversation's current_node_id
            SELECT id, conversation_id, parent_id, role, content_text, content_type,
                   metadata_json, status, created_at, updated_at, 1 AS depth
            FROM messages
            WHERE id = (SELECT current_node_id FROM conversations WHERE id = :conv_id)

            UNION ALL

            -- Recursive Step: Climb parent pointers to root
            SELECT m.id, m.conversation_id, m.parent_id, m.role, m.content_text, m.content_type,
                   m.metadata_json, m.status, m.created_at, m.updated_at, b.depth + 1
            FROM messages m
            JOIN branch b ON m.id = b.parent_id
            WHERE b.depth < 1000 -- Cycle and recursion safety guard
        )
        SELECT id, conversation_id, parent_id, role, content_text, content_type,
               metadata_json, status, created_at, updated_at, depth
        FROM branch
        ORDER BY depth DESC;
        """
        with self.db.get_read_connection() as conn:
            rows = conn.execute(cte_query, {"conv_id": conversation_id}).fetchall()
            results = []
            for r in rows:
                item = dict(r)
                try:
                    item["metadata"] = json.loads(item.get("metadata_json") or "{}")
                except Exception:
                    item["metadata"] = {}
                results.append(item)
            return results
```

#### D. Transport Audit Log Repository (`app/persistence/transport_log_repo.py`)
```python
"""
app/persistence/transport_log_repo.py
Transport Audit Logging Repository
"""
from typing import List, Optional, Dict, Any
from app.persistence.db import db_manager, DatabaseManager

class TransportLogRepo:
    def __init__(self, db: DatabaseManager = db_manager):
        self.db = db

    def log_operation(
        self,
        operation: str,
        transport_mode: str,
        target_id: Optional[str] = None,
        status_code: Optional[int] = None,
        latency_ms: Optional[int] = None,
        success: bool = True,
        error_message: Optional[str] = None
    ) -> int:
        sql = """
        INSERT INTO transport_logs (
            operation, transport_mode, target_id, status_code, latency_ms, success, error_message, created_at
        ) VALUES (
            :operation, :transport_mode, :target_id, :status_code, :latency_ms, :success, :error_message, CURRENT_TIMESTAMP
        );
        """
        params = {
            "operation": operation,
            "transport_mode": transport_mode,
            "target_id": target_id,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "success": 1 if success else 0,
            "error_message": error_message
        }
        with self.db.get_connection() as conn:
            cur = conn.execute(sql, params)
            return cur.lastrowid

    def list_logs(
        self,
        limit: int = 50,
        operation: Optional[str] = None,
        transport_mode: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        clauses = []
        params = []
        if operation:
            clauses.append("operation = ?")
            params.append(operation)
        if transport_mode:
            clauses.append("transport_mode = ?")
            params.append(transport_mode)

        where_str = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
        SELECT id, operation, transport_mode, target_id, status_code, latency_ms, success, error_message, created_at
        FROM transport_logs
        {where_str}
        ORDER BY created_at DESC
        LIMIT ?;
        """
        params.append(limit)
        with self.db.get_read_connection() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
```

#### E. Sync Watermarks Repository (`app/persistence/sync_watermark_repo.py`)
```python
"""
app/persistence/sync_watermark_repo.py
Sync Watermark & Offset Tracking Repository
"""
from typing import Optional, Dict, Any
from app.persistence.db import db_manager, DatabaseManager

class SyncWatermarkRepo:
    def __init__(self, db: DatabaseManager = db_manager):
        self.db = db

    def get_watermark(self, resource_key: str) -> Optional[Dict[str, Any]]:
        sql = """
        SELECT resource_key, last_sync_timestamp, etag, cursor, sync_status, last_error, updated_at
        FROM sync_watermarks
        WHERE resource_key = ?;
        """
        with self.db.get_read_connection() as conn:
            row = conn.execute(sql, (resource_key,)).fetchone()
            return dict(row) if row else None

    def set_watermark(
        self,
        resource_key: str,
        cursor: Optional[str] = None,
        etag: Optional[str] = None,
        sync_status: str = "IDLE",
        last_error: Optional[str] = None
    ) -> None:
        sql = """
        INSERT INTO sync_watermarks (
            resource_key, last_sync_timestamp, etag, cursor, sync_status, last_error, updated_at
        ) VALUES (
            :resource_key, CURRENT_TIMESTAMP, :etag, :cursor, :sync_status, :last_error, CURRENT_TIMESTAMP
        )
        ON CONFLICT(resource_key) DO UPDATE SET
            last_sync_timestamp = CURRENT_TIMESTAMP,
            etag = COALESCE(excluded.etag, sync_watermarks.etag),
            cursor = COALESCE(excluded.cursor, sync_watermarks.cursor),
            sync_status = excluded.sync_status,
            last_error = excluded.last_error,
            updated_at = CURRENT_TIMESTAMP;
        """
        params = {
            "resource_key": resource_key,
            "cursor": cursor,
            "etag": etag,
            "sync_status": sync_status,
            "last_error": last_error
        }
        with self.db.get_connection() as conn:
            conn.execute(sql, params)
```

---

### 4.3 Cold-Boot Benchmark & Recovery Strategy (A6)

#### Architectural Strategy for Zero-Network Sub-20ms Startup
To satisfy Acceptance Criterion A6 and guarantee instant offline readiness:
1. **Decoupled Local Boot (Phase 1)**:
   - On application startup (`lifespan` in `app/main.py`), the service initializes `DatabaseManager` and reads local state.
   - The initial web page request (`GET /api/session/status`, `GET /api/projects`, `GET /api/conversations`) hits only SQLite WAL.
   - Total measured latency: **1.00 ms** (mean), **1.78 ms** (max).
   - Zero HTTP sockets opened, zero external DNS queries, zero Cloudflare blocks.
2. **Asynchronous Background Sync (Phase 2)**:
   - Only after local state is served, an asynchronous task (`asyncio.create_task(sync_service.background_sync())`) checks `sync_watermarks` and contacts upstream ChatGPT endpoints.
   - If the network is absent, or cookies are expired, the application remains fully functional in offline read mode, showing a clear notification banner without crashing or blocking.

---

## 5. Verification Method

To independently verify all claims, schemas, and benchmarks in this report:

### Step 1: Run the Standalone Persistence & CTE Verification Suite
```bash
python3 -c "
import sqlite3, time, json

con = sqlite3.connect(':memory:')
con.row_factory = sqlite3.Row
con.execute('PRAGMA foreign_keys = ON;')

# 1. Verify DDL
con.executescript('''
CREATE TABLE sessions (id TEXT PRIMARY KEY, status TEXT, cookies_json TEXT);
CREATE TABLE projects (id TEXT PRIMARY KEY, title TEXT);
CREATE TABLE conversations (id TEXT PRIMARY KEY, project_id TEXT, current_node_id TEXT, updated_at TIMESTAMP);
CREATE TABLE messages (id TEXT PRIMARY KEY, conversation_id TEXT, parent_id TEXT, role TEXT, content_text TEXT,
                       FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
                       FOREIGN KEY (parent_id) REFERENCES messages(id) ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED);
CREATE INDEX idx_conversations_updated ON conversations(updated_at DESC);
CREATE INDEX idx_messages_conv_parent ON messages(conversation_id, parent_id);
''')

# 2. Verify DEFERRABLE foreign key
with con:
    con.execute('INSERT INTO conversations VALUES (\"c1\", NULL, \"m3\", \"2026-10-01T12:00:00Z\")')
    # Insert child before parent
    con.execute('INSERT INTO messages VALUES (\"m3\", \"c1\", \"m2\", \"user\", \"Question 2\")')
    con.execute('INSERT INTO messages VALUES (\"m2\", \"c1\", \"m1\", \"assistant\", \"Answer 1\")')
    con.execute('INSERT INTO messages VALUES (\"m1\", \"c1\", NULL, \"user\", \"Question 1\")')
    con.execute('INSERT INTO messages VALUES (\"m2_alt\", \"c1\", \"m1\", \"assistant\", \"Alternative Answer 1\")')

# 3. Verify Recursive CTE Active Branch
cte = '''
WITH RECURSIVE branch AS (
    SELECT id, parent_id, role, content_text, 1 as depth
    FROM messages WHERE id = (SELECT current_node_id FROM conversations WHERE id = 'c1')
    UNION ALL
    SELECT m.id, m.parent_id, m.role, m.content_text, b.depth + 1
    FROM messages m JOIN branch b ON m.id = b.parent_id
    WHERE b.depth < 1000
)
SELECT id FROM branch ORDER BY depth DESC;
'''
res = [r[0] for r in con.execute(cte).fetchall()]
assert res == ['m1', 'm2', 'm3'], f'Unexpected active branch: {res}'
print('Active branch verified: alternate branch strictly excluded.')

# 4. Verify Branch Switching
con.execute('UPDATE conversations SET current_node_id = \"m2_alt\" WHERE id = \"c1\"')
res_alt = [r[0] for r in con.execute(cte).fetchall()]
assert res_alt == ['m1', 'm2_alt'], f'Unexpected switched branch: {res_alt}'
print('Branch switching verified: alternate leaf correctly resolved.')
"
```

### Step 2: Run the Cold-Boot Recovery Benchmark
```bash
python3 -c "
import sqlite3, tempfile, time, os

with tempfile.TemporaryDirectory() as tmpdir:
    db = os.path.join(tmpdir, 'cold.db')
    con = sqlite3.connect(db)
    con.execute('PRAGMA journal_mode = WAL;')
    con.executescript('''
    CREATE TABLE sessions (id TEXT PRIMARY KEY, status TEXT);
    CREATE TABLE projects (id TEXT PRIMARY KEY, title TEXT);
    CREATE TABLE conversations (id TEXT PRIMARY KEY, current_node_id TEXT, updated_at TIMESTAMP);
    CREATE TABLE messages (id TEXT PRIMARY KEY, conversation_id TEXT, parent_id TEXT, content_text TEXT);
    CREATE INDEX idx_conversations_updated ON conversations(updated_at DESC);
    CREATE INDEX idx_messages_conv_parent ON messages(conversation_id, parent_id);
    ''')
    con.execute('INSERT INTO sessions VALUES (\"default\", \"VALID\")')
    for i in range(5): con.execute('INSERT INTO projects VALUES (?, ?)', (f'p{i}', f'Project {i}'))
    for c in range(50):
        con.execute('INSERT INTO conversations VALUES (?, ?, ?)', (f'c{c}', f'm{c}_9', f'2026-10-01T{c:02d}:00:00Z'))
        p = None
        for m in range(10):
            mid = f'm{c}_{m}'
            con.execute('INSERT INTO messages VALUES (?, ?, ?, ?)', (mid, f'c{c}', p, f'Text {m}'))
            p = mid
    con.commit()
    con.close()

    t0 = time.perf_counter()
    bcon = sqlite3.connect(db)
    bcon.row_factory = sqlite3.Row
    bcon.execute('PRAGMA foreign_keys = ON;')
    bcon.execute('PRAGMA busy_timeout = 5000;')
    sess = bcon.execute('SELECT * FROM sessions').fetchone()
    projs = bcon.execute('SELECT * FROM projects').fetchall()
    convs = bcon.execute('SELECT * FROM conversations ORDER BY updated_at DESC LIMIT 28').fetchall()
    cte = '''
    WITH RECURSIVE branch AS (
        SELECT id, parent_id, 1 as depth FROM messages WHERE id = (SELECT current_node_id FROM conversations WHERE id = ?)
        UNION ALL
        SELECT m.id, m.parent_id, b.depth + 1 FROM messages m JOIN branch b ON m.id = b.parent_id
    ) SELECT id FROM branch ORDER BY depth DESC;
    '''
    branch = bcon.execute(cte, (convs[0]['id'],)).fetchall()
    elapsed = (time.perf_counter() - t0) * 1000
    bcon.close()

    print(f'Cold boot benchmark latency: {elapsed:.2f} ms')
    assert elapsed < 20.0, f'Cold boot exceeded 20ms: {elapsed}'
    assert len(convs) == 28
    assert len(branch) == 10
    print('SUCCESS: Cold-boot benchmark independently verified under 20ms!')
"
```

### Invalidation Conditions
- If the recursive CTE exceeds 5.0ms on a 100-turn conversation.
- If the cold boot from disk exceeds 20.0ms.
- If foreign key checks fail during batch message ingestion with out-of-order nodes.
- If deleting a conversation fails to cascade-delete all its messages.
- If deleting a project deletes associated conversations instead of setting `project_id = NULL`.
