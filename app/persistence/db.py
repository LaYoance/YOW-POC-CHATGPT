"""
SQLite WAL Database Lifecycle & Connection Management.
Satisfies R1, R3, A6, and A7 persistence and isolation requirements.
"""

import logging
import os
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, List, Optional, Union

from app.config import settings

logger = logging.getLogger("app.persistence.db")

PRAGMAS: List[str] = [
    "PRAGMA journal_mode = WAL;",
    "PRAGMA foreign_keys = ON;",
    "PRAGMA busy_timeout = 5000;",
    "PRAGMA synchronous = NORMAL;",
    "PRAGMA temp_store = MEMORY;",
    "PRAGMA cache_size = -64000;",  # 64 MB memory cache
]

SCHEMA_DDL = """
-- 1. SESSIONS TABLE (R1, A7)
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

-- 2. PROJECTS TABLE (R2, A1, A5)
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

-- 3. CONVERSATIONS TABLE (R2, R3, A1, A6)
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

-- 4. MESSAGES TABLE (R2, R3, A2, A6)
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
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_messages_conv_parent ON messages(conversation_id, parent_id);
CREATE INDEX IF NOT EXISTS idx_messages_conv_active ON messages(conversation_id, is_active_branch);
CREATE INDEX IF NOT EXISTS idx_messages_conv_created ON messages(conversation_id, created_at ASC);

-- 5. SYNC WATERMARKS TABLE (R3, A8)
CREATE TABLE IF NOT EXISTS sync_watermarks (
    resource_key TEXT PRIMARY KEY,
    last_sync_timestamp TIMESTAMP,
    etag TEXT,
    cursor TEXT,
    sync_status TEXT DEFAULT 'IDLE',
    last_error TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 6. TRANSPORT AUDIT LOGS TABLE (R4)
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
    """Manages SQLite connections, pragmas, schema migrations, and security isolation."""

    def __init__(self, db_path: Optional[Union[str, Path]] = None):
        if db_path is not None:
            self.db_path = Path(db_path)
            self.db_dir = self.db_path.parent
        else:
            self.db_path = settings.db_path
            self.db_dir = settings.full_session_data_dir

    def enforce_permissions(self) -> None:
        """Enforces POSIX 0700 on database directory and 0600 on DB and WAL files."""
        if os.name == "posix":
            try:
                if self.db_dir.exists():
                    os.chmod(self.db_dir, 0o700)
            except OSError as e:
                logger.warning(f"Could not enforce 0700 on {self.db_dir}: {e}")

            for p in [self.db_path, Path(f"{self.db_path}-wal"), Path(f"{self.db_path}-shm")]:
                if p.exists():
                    try:
                        os.chmod(p, 0o600)
                    except OSError as e:
                        logger.warning(f"Could not enforce 0600 on {p}: {e}")

    def init_db(self) -> None:
        """Initializes the database schema and verifies WAL/security isolation."""
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.enforce_permissions()

        conn = self._connect()
        try:
            conn.executescript(SCHEMA_DDL)
            conn.commit()

            # Check for legacy self-referential foreign key on messages(parent_id)
            cur = conn.cursor()
            fks = cur.execute("PRAGMA foreign_key_list(messages);").fetchall()
            has_parent_fk = any(
                (dict(fk).get("from") == "parent_id" if isinstance(fk, sqlite3.Row) else fk[3] == "parent_id")
                for fk in fks
            )
            if has_parent_fk:
                logger.info("Migrating messages table to drop obsolete parent_id self-referential foreign key...")
                cur.execute("PRAGMA foreign_keys = OFF;")
                cur.execute("ALTER TABLE messages RENAME TO _messages_old;")
                cur.execute("""
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    parent_id TEXT,
                    role TEXT NOT NULL,
                    content_text TEXT NOT NULL,
                    content_type TEXT DEFAULT 'text',
                    metadata_json TEXT DEFAULT '{}',
                    status TEXT DEFAULT 'finished_successfully',
                    is_active_branch BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                );
                """)
                cur.execute("""
                INSERT INTO messages (
                    id, conversation_id, parent_id, role, content_text, content_type,
                    metadata_json, status, is_active_branch, created_at, updated_at
                ) SELECT id, conversation_id, parent_id, role, content_text, content_type,
                         metadata_json, status, is_active_branch, created_at, updated_at
                  FROM _messages_old;
                """)
                cur.execute("DROP TABLE _messages_old;")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_conv_parent ON messages(conversation_id, parent_id);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_conv_active ON messages(conversation_id, is_active_branch);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_conv_created ON messages(conversation_id, created_at ASC);")
                cur.execute("PRAGMA foreign_keys = ON;")
                conn.commit()

            logger.info(f"Database schema initialized successfully at {self.db_path}")
        finally:
            conn.close()

        self.enforce_permissions()

    # Alias for schema initialization
    initialize_schema = init_db

    def _connect(self) -> sqlite3.Connection:
        """Establishes a raw connection configured with WAL and performance pragmas."""
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=float(settings.SQLITE_BUSY_TIMEOUT_MS) / 1000.0,
            check_same_thread=False,
            isolation_level=None,  # Explicit transaction control
        )
        conn.row_factory = sqlite3.Row
        conn.executescript("\n".join(PRAGMAS))
        return conn

    @contextmanager
    def get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager for write transactions with immediate locking and rollback safety."""
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
        """Context manager for concurrent read operations without taking write lock."""
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()


# Global default database manager
db_manager = DatabaseManager()
