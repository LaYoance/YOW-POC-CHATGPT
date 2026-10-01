"""
Tier 1 Feature Tests: SQLite Persistence & Restart Resilience (F09).
Satisfies R2, R3, A2, A6, and DISPATCH.md requirements.
"""

import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.models.conversation import Conversation, Message
from app.models.project import Project
from app.models.transport import TransportMode
from app.persistence.conversation_repo import ConversationRepo
from app.persistence.db import DatabaseManager
from app.persistence.project_repo import ProjectRepo
from app.persistence.transport_log_repo import TransportLogRepo


class TestFeatureResilience(unittest.TestCase):
    """Test suite for F09: SQLite Persistence & Restart Resilience."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="poc_resilience_test_")
        self.db_path = Path(self.test_dir) / "resilience.db"
        self.db_mgr = DatabaseManager(db_path=self.db_path)
        self.db_mgr.init_db()

        self.conv_repo = ConversationRepo(db=self.db_mgr)
        self.project_repo = ProjectRepo(db=self.db_mgr)
        self.transport_log_repo = TransportLogRepo(db=self.db_mgr)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_sqlite_wal_pragmas_and_schema_initialization(self):
        """Verifies SQLite WAL mode, foreign keys, synchronous pragmas, and schema completeness."""
        with self.db_mgr.get_read_connection() as conn:
            # Pragmas
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode;")
            journal_mode = cur.fetchone()[0]
            self.assertEqual(journal_mode.lower(), "wal")

            cur.execute("PRAGMA foreign_keys;")
            foreign_keys = cur.fetchone()[0]
            self.assertEqual(foreign_keys, 1)

            cur.execute("PRAGMA synchronous;")
            synchronous = cur.fetchone()[0]
            # 1 corresponds to NORMAL
            self.assertIn(synchronous, (1, 2))

            cur.execute("PRAGMA busy_timeout;")
            busy_timeout = cur.fetchone()[0]
            self.assertEqual(busy_timeout, 5000)

            # Tables
            cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = {row[0] for row in cur.fetchall()}
            expected_tables = {
                "sessions", "projects", "conversations", "messages",
                "sync_watermarks", "transport_logs"
            }
            for tbl in expected_tables:
                self.assertIn(tbl, tables, f"Expected table '{tbl}' not found in SQLite schema")

            # Indexes
            cur.execute("SELECT name FROM sqlite_master WHERE type='index';")
            indexes = {row[0] for row in cur.fetchall()}
            expected_indexes = {
                "idx_conversations_project", "idx_conversations_updated",
                "idx_messages_conv_parent", "idx_messages_conv_active",
                "idx_messages_conv_created"
            }
            for idx in expected_indexes:
                self.assertIn(idx, indexes, f"Expected index '{idx}' not found in SQLite schema")

    def test_cold_boot_recovery_benchmark(self):
        """
        Populate DB with 50 conversations and 500 messages (10 messages per thread).
        Close connections completely.
        Reopen DB cold and run recursive CTE to recover active branch.
        Assert cold recovery executes in < 20.0ms (aim for < 2ms).
        """
        # Pre-populate dataset
        for c_idx in range(50):
            c_id = f"conv-bench-{c_idx:03d}"
            last_msg_id = f"msg-{c_idx:03d}-09"
            self.conv_repo.upsert_conversation({
                "id": c_id,
                "title": f"Benchmark Thread {c_idx}",
                "current_node_id": last_msg_id,
                "created_at": "2026-10-01T12:00:00Z",
                "updated_at": "2026-10-01T12:10:00Z"
            })

            messages = []
            parent_id = None
            for m_idx in range(10):
                m_id = f"msg-{c_idx:03d}-{m_idx:02d}"
                role = "user" if m_idx % 2 == 0 else "assistant"
                messages.append({
                    "id": m_id,
                    "conversation_id": c_id,
                    "parent_id": parent_id,
                    "role": role,
                    "content_text": f"Content for turn {m_idx} of conversation {c_idx}",
                    "created_at": f"2026-10-01T12:0{m_idx}:00Z",
                })
                parent_id = m_id
            self.conv_repo.save_messages(messages)

        # Ensure everything is committed and close any open connections
        target_conv_id = "conv-bench-037"

        # Complete cold boot: instantiate a brand new DatabaseManager and ConversationRepo
        t0 = time.perf_counter()
        cold_db_mgr = DatabaseManager(db_path=self.db_path)
        cold_repo = ConversationRepo(db=cold_db_mgr)
        active_messages = cold_repo.get_active_branch(target_conv_id)
        t1 = time.perf_counter()

        elapsed_ms = (t1 - t0) * 1000.0

        # Assertions
        self.assertEqual(len(active_messages), 10, f"Expected 10 active messages, got {len(active_messages)}")
        self.assertEqual(active_messages[0].id, "msg-037-00")
        self.assertEqual(active_messages[-1].id, "msg-037-09")
        self.assertLess(elapsed_ms, 20.0, f"Cold boot benchmark exceeded 20ms: {elapsed_ms:.3f}ms")

        # Log measured benchmark time
        print(f"\n[BENCHMARK] Cold recovery latency: {elapsed_ms:.3f} ms (budget < 20.0 ms)")

    def test_active_branch_isolation_from_branching_dag(self):
        """
        Reconstructs active branch from DAG with multiple branches.
        Branch A: abandoned edit branch.
        Branch B: active leaf branch.
        Asserts only Branch B is returned, strictly excluding Branch A turns.
        """
        conv_id = "conv-dag-branching"
        self.conv_repo.upsert_conversation({
            "id": conv_id,
            "title": "DAG Branching Test",
            "current_node_id": "m5b",
            "created_at": "2026-10-01T12:00:00Z",
            "updated_at": "2026-10-01T12:15:00Z"
        })

        # Messages DAG topology:
        # m0 (user) -> m1 (assistant)
        # Fork 1 (Branch A): m2a (user edit) -> m3a (assistant answer)
        # Fork 2 (Branch B): m2b (user edit) -> m3b (assistant answer) -> m4b (user follow-up) -> m5b (assistant leaf)
        messages = [
            # Shared trunk
            {"id": "m0", "conversation_id": conv_id, "parent_id": None, "role": "user", "content_text": "Trunk turn 0", "created_at": "2026-10-01T12:00:00Z"},
            {"id": "m1", "conversation_id": conv_id, "parent_id": "m0", "role": "assistant", "content_text": "Trunk turn 1", "created_at": "2026-10-01T12:01:00Z"},
            # Branch A (abandoned edit)
            {"id": "m2a", "conversation_id": conv_id, "parent_id": "m1", "role": "user", "content_text": "Branch A edit", "created_at": "2026-10-01T12:02:00Z"},
            {"id": "m3a", "conversation_id": conv_id, "parent_id": "m2a", "role": "assistant", "content_text": "Branch A response", "created_at": "2026-10-01T12:03:00Z"},
            # Branch B (active branch)
            {"id": "m2b", "conversation_id": conv_id, "parent_id": "m1", "role": "user", "content_text": "Branch B edit", "created_at": "2026-10-01T12:04:00Z"},
            {"id": "m3b", "conversation_id": conv_id, "parent_id": "m2b", "role": "assistant", "content_text": "Branch B response", "created_at": "2026-10-01T12:05:00Z"},
            {"id": "m4b", "conversation_id": conv_id, "parent_id": "m3b", "role": "user", "content_text": "Branch B question", "created_at": "2026-10-01T12:06:00Z"},
            {"id": "m5b", "conversation_id": conv_id, "parent_id": "m4b", "role": "assistant", "content_text": "Branch B final leaf", "created_at": "2026-10-01T12:07:00Z"},
        ]
        self.conv_repo.save_messages(messages)

        # Retrieve active branch
        branch_b = self.conv_repo.get_active_branch(conv_id)
        msg_ids = [m.id for m in branch_b]

        expected_ids = ["m0", "m1", "m2b", "m3b", "m4b", "m5b"]
        self.assertEqual(msg_ids, expected_ids)
        self.assertNotIn("m2a", msg_ids)
        self.assertNotIn("m3a", msg_ids)

    def test_active_branch_dynamic_switch(self):
        """Switching current_node_id immediately reconstructs alternate branch without message edits."""
        conv_id = "conv-switch-test"
        self.conv_repo.upsert_conversation({
            "id": conv_id,
            "title": "Branch Switch Test",
            "current_node_id": "m5b",
            "created_at": "2026-10-01T12:00:00Z",
            "updated_at": "2026-10-01T12:15:00Z"
        })

        messages = [
            {"id": "m0", "conversation_id": conv_id, "parent_id": None, "role": "user", "content_text": "Root", "created_at": "2026-10-01T12:00:00Z"},
            {"id": "m1", "conversation_id": conv_id, "parent_id": "m0", "role": "assistant", "content_text": "Turn 1", "created_at": "2026-10-01T12:01:00Z"},
            {"id": "m2a", "conversation_id": conv_id, "parent_id": "m1", "role": "user", "content_text": "Edit A", "created_at": "2026-10-01T12:02:00Z"},
            {"id": "m3a", "conversation_id": conv_id, "parent_id": "m2a", "role": "assistant", "content_text": "Answer A", "created_at": "2026-10-01T12:03:00Z"},
            {"id": "m2b", "conversation_id": conv_id, "parent_id": "m1", "role": "user", "content_text": "Edit B", "created_at": "2026-10-01T12:04:00Z"},
            {"id": "m3b", "conversation_id": conv_id, "parent_id": "m2b", "role": "assistant", "content_text": "Answer B", "created_at": "2026-10-01T12:05:00Z"},
            {"id": "m4b", "conversation_id": conv_id, "parent_id": "m3b", "role": "user", "content_text": "Followup B", "created_at": "2026-10-01T12:06:00Z"},
            {"id": "m5b", "conversation_id": conv_id, "parent_id": "m4b", "role": "assistant", "content_text": "Final B", "created_at": "2026-10-01T12:07:00Z"},
        ]
        self.conv_repo.save_messages(messages)

        # Switch leaf pointer to Branch A leaf
        self.conv_repo.update_current_node(conv_id, "m3a")

        # Retrieve active branch
        branch_a = self.conv_repo.get_active_branch(conv_id)
        msg_ids = [m.id for m in branch_a]

        expected_ids = ["m0", "m1", "m2a", "m3a"]
        self.assertEqual(msg_ids, expected_ids)
        self.assertNotIn("m2b", msg_ids)
        self.assertNotIn("m5b", msg_ids)

    def test_cold_restart_data_integrity(self):
        """Verifies zero data loss or duplicate rows across complete restart."""
        # Insert 10 conversations with 5 messages each
        for i in range(10):
            c_id = f"conv-integrity-{i}"
            self.conv_repo.upsert_conversation({
                "id": c_id,
                "title": f"Integrity {i}",
                "current_node_id": f"msg-integ-{i}-4",
                "created_at": "2026-10-01T12:00:00Z",
                "updated_at": "2026-10-01T12:00:00Z"
            })
            msgs = []
            pid = None
            for j in range(5):
                m_id = f"msg-integ-{i}-{j}"
                msgs.append({
                    "id": m_id,
                    "conversation_id": c_id,
                    "parent_id": pid,
                    "role": "user" if j % 2 == 0 else "assistant",
                    "content_text": f"Text {i}-{j}",
                    "created_at": "2026-10-01T12:00:00Z"
                })
                pid = m_id
            self.conv_repo.save_messages(msgs)

        # Reopen cold
        cold_db = DatabaseManager(db_path=self.db_path)
        cold_repo = ConversationRepo(db=cold_db)

        convs, total = cold_repo.list_conversations(limit=100)
        self.assertEqual(total, 10)
        self.assertEqual(len(convs), 10)

        # Check total messages count
        with cold_db.get_read_connection() as conn:
            cnt = conn.execute("SELECT COUNT(*) FROM messages;").fetchone()[0]
            self.assertEqual(cnt, 50)

    def test_synthetic_root_and_empty_content_resilience(self):
        """ChatGPT trees often have synthetic root with empty or None content."""
        conv_id = "conv-synthetic-root"
        self.conv_repo.upsert_conversation({
            "id": conv_id,
            "title": "Synthetic Root Test",
            "current_node_id": "turn-1",
            "created_at": "2026-10-01T12:00:00Z",
            "updated_at": "2026-10-01T12:01:00Z"
        })

        messages = [
            {
                "id": "root-synthetic-uuid",
                "conversation_id": conv_id,
                "parent_id": None,
                "role": "system",
                "content_text": "",
                "created_at": "2026-10-01T12:00:00Z"
            },
            {
                "id": "turn-1",
                "conversation_id": conv_id,
                "parent_id": "root-synthetic-uuid",
                "role": "user",
                "content_text": "Hello world",
                "created_at": "2026-10-01T12:01:00Z"
            }
        ]
        self.conv_repo.save_messages(messages)

        branch = self.conv_repo.get_active_branch(conv_id)
        self.assertEqual(len(branch), 2)
        self.assertEqual(branch[0].id, "root-synthetic-uuid")
        self.assertEqual(branch[0].content_text, "")
        self.assertEqual(branch[1].id, "turn-1")

    def test_transport_audit_log_persistence(self):
        """Verifies TransportLogRepo correctly logs and filters audit events."""
        self.transport_log_repo.log_operation(
            operation="fetch_conversations",
            transport_mode="DIRECT_HTTP",
            status_code=200,
            latency_ms=125.4,
            success=True
        )
        self.transport_log_repo.log_operation(
            operation="send_message_stream",
            transport_mode="BROWSER_FETCH",
            target_id="conv-123",
            status_code=200,
            latency_ms=450.0,
            success=True
        )
        self.transport_log_repo.log_operation(
            operation="fetch_projects",
            transport_mode="DIRECT_HTTP",
            status_code=403,
            latency_ms=80.0,
            success=False,
            error_message="Cloudflare Turnstile challenge"
        )

        all_logs = self.transport_log_repo.list_logs(limit=10)
        self.assertEqual(len(all_logs), 3)

        direct_logs = self.transport_log_repo.list_logs(transport_mode="DIRECT_HTTP")
        self.assertEqual(len(direct_logs), 2)

        stream_logs = self.transport_log_repo.list_logs(operation="send_message_stream")
        self.assertEqual(len(stream_logs), 1)
        self.assertEqual(stream_logs[0].target_id, "conv-123")
        self.assertTrue(stream_logs[0].success)

    def test_foreign_key_cascade_and_set_null(self):
        """
        Verifies referential integrity rules:
        - Deleting project sets conversation project_id to NULL (ON DELETE SET NULL).
        - Deleting conversation purges all associated messages (ON DELETE CASCADE).
        """
        project_id = "g-p-test-cascade"
        self.project_repo.upsert_project({
            "id": project_id,
            "title": "Cascade Test Project",
            "instructions": "Test instructions",
            "created_at": "2026-10-01T12:00:00Z"
        })

        conv_id = "conv-cascade-test"
        self.conv_repo.upsert_conversation({
            "id": conv_id,
            "title": "Conversation with Project",
            "project_id": project_id,
            "current_node_id": "m1-cascade",
            "created_at": "2026-10-01T12:00:00Z",
            "updated_at": "2026-10-01T12:01:00Z"
        })

        self.conv_repo.save_messages([
            {"id": "m0-cascade", "conversation_id": conv_id, "parent_id": None, "role": "user", "content_text": "Hello", "created_at": "2026-10-01T12:00:00Z"},
            {"id": "m1-cascade", "conversation_id": conv_id, "parent_id": "m0-cascade", "role": "assistant", "content_text": "World", "created_at": "2026-10-01T12:01:00Z"}
        ])

        # Delete project -> conversation should still exist, but project_id becomes None
        self.project_repo.delete_project(project_id)
        conv_after_project_delete = self.conv_repo.get_conversation(conv_id)
        self.assertIsNotNone(conv_after_project_delete)
        self.assertIsNone(conv_after_project_delete.project_id)

        # Delete conversation -> messages should be cascade deleted
        self.conv_repo.delete_conversation(conv_id)
        conv_deleted = self.conv_repo.get_conversation(conv_id)
        self.assertIsNone(conv_deleted)

        with self.db_mgr.get_read_connection() as conn:
            remaining_msgs = conn.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?;", (conv_id,)).fetchone()[0]
            self.assertEqual(remaining_msgs, 0)


if __name__ == "__main__":
    unittest.main()
