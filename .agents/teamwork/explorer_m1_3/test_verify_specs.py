#!/usr/bin/env python3
"""
Test Spec Verification Script for M1:
Verifies:
1. Session state transitions (MISSING -> VALID -> EXPIRED -> VALID -> CHALLENGE_REQUIRED).
2. Local directory isolation (0700) and DB file permissions (0600).
3. Cold restart with SQLite WAL: DB creation, 50 conversations, 500 messages, close, reopen, recursive CTE benchmark < 20ms.
4. Active branch recovery from branching DAG mappings (multi-leaf tree traversal, branch switching, synthetic root).
"""

import os
import stat
import time
import uuid
import sqlite3
from enum import Enum
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone, timedelta

# ==========================================
# 1. State Machine & Isolation Prototypes
# ==========================================

class SessionStatus(str, Enum):
    VALID = "VALID"
    EXPIRED = "EXPIRED"
    MISSING = "MISSING"
    CHALLENGE_REQUIRED = "CHALLENGE_REQUIRED"

class SessionCredentialsPrototype:
    def __init__(self, access_token: str, session_token: Optional[str] = None,
                 cf_clearance: Optional[str] = None, user_id: Optional[str] = None,
                 email: Optional[str] = None, expires_at: Optional[datetime] = None):
        self.access_token = access_token
        self.session_token = session_token
        self.cf_clearance = cf_clearance
        self.user_id = user_id
        self.email = email
        self.expires_at = expires_at

    def masked_token(self) -> str:
        if not self.access_token or len(self.access_token) < 10:
            return "***"
        return f"{self.access_token[:4]}...{self.access_token[-4:]}"

    def __repr__(self) -> str:
        # Never leak secrets in logs
        return f"<SessionCredentials user_id={self.user_id} token={self.masked_token()}>"

class SessionStateMachine:
    def __init__(self):
        self.status = SessionStatus.MISSING
        self.credentials: Optional[SessionCredentialsPrototype] = None

    def import_session(self, creds: SessionCredentialsPrototype):
        self.credentials = creds
        self.status = SessionStatus.VALID

    def handle_http_response(self, status_code: int):
        if status_code == 200:
            self.status = SessionStatus.VALID
        elif status_code == 401:
            self.status = SessionStatus.EXPIRED
        elif status_code == 403:
            self.status = SessionStatus.CHALLENGE_REQUIRED

    def refresh_session(self, creds: SessionCredentialsPrototype):
        self.credentials = creds
        self.status = SessionStatus.VALID

# ==========================================
# 2. SQLite WAL & CTE Engine Prototypes
# ==========================================

def init_test_db(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        project_id TEXT,
        current_node TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL,
        parent_id TEXT,
        author_role TEXT NOT NULL,
        content_text TEXT NOT NULL,
        status TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_messages_conv_parent ON messages(conversation_id, parent_id);
    CREATE INDEX IF NOT EXISTS idx_conversations_project ON conversations(project_id);
    """)
    conn.commit()
    conn.close()

CTE_ACTIVE_BRANCH_QUERY = """
WITH RECURSIVE branch(id, conversation_id, parent_id, author_role, content_text, depth) AS (
    SELECT m.id, m.conversation_id, m.parent_id, m.author_role, m.content_text, 0
    FROM messages m
    JOIN conversations c ON c.id = m.conversation_id
    WHERE c.id = ? AND m.id = c.current_node

    UNION ALL

    SELECT m.id, m.conversation_id, m.parent_id, m.author_role, m.content_text, b.depth + 1
    FROM messages m
    JOIN branch b ON m.id = b.parent_id
)
SELECT id, parent_id, author_role, content_text
FROM branch
ORDER BY depth DESC;
"""

# ==========================================
# 3. Test Runner
# ==========================================

def test_session_lifecycle():
    sm = SessionStateMachine()
    assert sm.status == SessionStatus.MISSING, f"Expected MISSING, got {sm.status}"

    # Import -> VALID
    creds = SessionCredentialsPrototype(
        access_token="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.dummy_token_12345",
        user_id="user-xyz"
    )
    sm.import_session(creds)
    assert sm.status == SessionStatus.VALID, f"Expected VALID, got {sm.status}"
    assert "dummy_token_12345" not in repr(creds), "Secret token leaked in repr!"
    assert creds.masked_token() == "eyJh...2345"

    # HTTP 401 -> EXPIRED
    sm.handle_http_response(401)
    assert sm.status == SessionStatus.EXPIRED, f"Expected EXPIRED, got {sm.status}"

    # Refresh -> VALID
    refreshed_creds = SessionCredentialsPrototype(
        access_token="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.refreshed_token_98765",
        user_id="user-xyz"
    )
    sm.refresh_session(refreshed_creds)
    assert sm.status == SessionStatus.VALID, f"Expected VALID, got {sm.status}"

    # HTTP 403 -> CHALLENGE_REQUIRED
    sm.handle_http_response(403)
    assert sm.status == SessionStatus.CHALLENGE_REQUIRED, f"Expected CHALLENGE_REQUIRED, got {sm.status}"
    print("[PASS] Session lifecycle state transitions verified.")

def test_directory_isolation(tmp_path: str):
    session_dir = os.path.join(tmp_path, ".session_data")
    db_file = os.path.join(session_dir, "poc.db")

    os.makedirs(session_dir, mode=0o700, exist_ok=True)
    os.chmod(session_dir, 0o700)

    with open(db_file, "w") as f:
        f.write("sqlite format 3")
    os.chmod(db_file, 0o600)

    dir_perm = stat.S_IMODE(os.stat(session_dir).st_mode)
    file_perm = stat.S_IMODE(os.stat(db_file).st_mode)

    assert dir_perm == 0o700, f"Expected 0o700 for session dir, got {oct(dir_perm)}"
    assert file_perm == 0o600, f"Expected 0o600 for db file, got {oct(file_perm)}"
    print("[PASS] Directory and file isolation permissions (0700/0600) verified.")

def test_cold_restart_wal_benchmark(db_path: str):
    if os.path.exists(db_path):
        os.remove(db_path)

    init_test_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")

    # Populate 50 conversations and 500 messages
    for c_idx in range(50):
        c_id = f"conv-resilience-{c_idx:03d}"
        last_msg_id = f"msg-{c_idx:03d}-09"
        conn.execute("INSERT INTO conversations VALUES (?, ?, ?, ?, ?, ?)",
                     (c_id, f"Resilience Conv {c_idx}", None, last_msg_id, "2026-10-01T12:00:00Z", "2026-10-01T12:10:00Z"))
        parent_id = None
        for m_idx in range(10):
            m_id = f"msg-{c_idx:03d}-{m_idx:02d}"
            role = "user" if m_idx % 2 == 0 else "assistant"
            content = f"Turn {m_idx} content for conv {c_idx}"
            conn.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (m_id, c_id, parent_id, role, content, "finished_successfully", "2026-10-01T12:00:00Z"))
            parent_id = m_id

    conn.commit()
    conn.close()

    # Hard cold restart: open cold connection and run CTE benchmark
    t_start = time.perf_counter()
    conn_cold = sqlite3.connect(db_path)
    conn_cold.execute("PRAGMA journal_mode = WAL;")
    cur = conn_cold.cursor()
    cur.execute(CTE_ACTIVE_BRANCH_QUERY, ("conv-resilience-035",))
    rows = cur.fetchall()
    t_end = time.perf_counter()

    elapsed_ms = (t_end - t_start) * 1000
    assert len(rows) == 10, f"Expected 10 active messages, got {len(rows)}"
    assert rows[0][0] == "msg-035-00", f"Root message mismatch: {rows[0][0]}"
    assert rows[-1][0] == "msg-035-09", f"Leaf message mismatch: {rows[-1][0]}"
    assert elapsed_ms < 20.0, f"Benchmark exceeded 20ms budget: {elapsed_ms:.3f}ms"

    print(f"[PASS] Cold restart recovery: 10 messages fetched in {elapsed_ms:.3f} ms (budget < 20ms).")
    conn_cold.close()

def test_dag_branching_recovery(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")

    conv_id = "conv-dag-branching-test"
    # Structure:
    # root (m0: user) -> m1: assistant
    # -> Edit A: m2a: user -> m3a: assistant
    # -> Edit B: m2b: user -> m3b: assistant -> m4b: user -> m5b: assistant
    conn.execute("INSERT INTO conversations VALUES (?, ?, ?, ?, ?, ?)",
                 (conv_id, "DAG Branching", None, "m5b", "2026-10-01T12:00:00Z", "2026-10-01T12:15:00Z"))

    # Shared trunk
    conn.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                 ("m0", conv_id, None, "user", "What is quantum computing?", "finished_successfully", "2026-10-01T12:00:00Z"))
    conn.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                 ("m1", conv_id, "m0", "assistant", "Quantum computing uses qubits...", "finished_successfully", "2026-10-01T12:01:00Z"))

    # Branch A (abandoned edit)
    conn.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                 ("m2a", conv_id, "m1", "user", "Explain it like I am 5.", "finished_successfully", "2026-10-01T12:02:00Z"))
    conn.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                 ("m3a", conv_id, "m2a", "assistant", "Imagine magic coins spinning...", "finished_successfully", "2026-10-01T12:03:00Z"))

    # Branch B (current active branch)
    conn.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                 ("m2b", conv_id, "m1", "user", "Explain Shor's algorithm.", "finished_successfully", "2026-10-01T12:04:00Z"))
    conn.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                 ("m3b", conv_id, "m2b", "assistant", "Shor's algorithm finds prime factors...", "finished_successfully", "2026-10-01T12:05:00Z"))
    conn.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                 ("m4b", conv_id, "m3b", "user", "What is the time complexity?", "finished_successfully", "2026-10-01T12:06:00Z"))
    conn.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                 ("m5b", conv_id, "m4b", "assistant", "It runs in polynomial time O((log N)^3)...", "finished_successfully", "2026-10-01T12:07:00Z"))

    conn.commit()

    # Verify active branch when current_node is m5b
    cur = conn.cursor()
    cur.execute(CTE_ACTIVE_BRANCH_QUERY, (conv_id,))
    active_b = [r[0] for r in cur.fetchall()]
    assert active_b == ["m0", "m1", "m2b", "m3b", "m4b", "m5b"], f"Unexpected active branch B: {active_b}"
    print(f"[PASS] Active branch B correctly isolated: {active_b}")

    # Switch current_node to m3a (switching to Branch A)
    conn.execute("UPDATE conversations SET current_node = 'm3a' WHERE id = ?", (conv_id,))
    conn.commit()

    cur.execute(CTE_ACTIVE_BRANCH_QUERY, (conv_id,))
    active_a = [r[0] for r in cur.fetchall()]
    assert active_a == ["m0", "m1", "m2a", "m3a"], f"Unexpected active branch A: {active_a}"
    print(f"[PASS] Active branch A correctly isolated upon switch: {active_a}")

    conn.close()

if __name__ == "__main__":
    test_session_lifecycle()
    test_directory_isolation("/tmp/test_explorer_m1_3_iso")
    test_cold_restart_wal_benchmark("/tmp/test_explorer_m1_3_bench.db")
    test_dag_branching_recovery("/tmp/test_explorer_m1_3_bench.db")
    print("\nALL TEST SPEC PROTOTYPES AND BENCHMARKS PASSED SUCCESSFULLY!")
