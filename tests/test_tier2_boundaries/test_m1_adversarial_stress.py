"""
Milestone 1 Adversarial Stress Tests: Persistence, Active Branch DAG, Concurrency, Scale & Recovery.
Targeting F09 and R1, R2, R3, A2, A6.
"""

import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional

from app.models.conversation import Conversation, Message
from app.persistence.conversation_repo import ConversationRepo
from app.persistence.db import DatabaseManager


class TestM1AdversarialStress(unittest.TestCase):
    """Adversarial stress and boundary suite for Milestone 1 persistence and DAG active branch resolution."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="poc_stress_test_")
        self.db_path = Path(self.test_dir) / "stress.db"
        self.db_mgr = DatabaseManager(db_path=self.db_path)
        self.db_mgr.init_db()
        self.conv_repo = ConversationRepo(db=self.db_mgr)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_01_high_data_scale_wal(self):
        """
        Adversarial Test 1: High data scale.
        Inserts 120 conversations and 2,400 messages (20 messages per thread) in SQLite WAL.
        Verifies:
        - Insertion integrity across large batches.
        - Strict pagination integrity (offset/limit across 120 items).
        - Total message count in DB.
        - Correctness of sampled active branches.
        - SQLite WAL mode and PRAGMA integrity_check.
        """
        num_convs = 120
        msgs_per_conv = 20
        total_expected_msgs = num_convs * msgs_per_conv

        t_insert_start = time.perf_counter()

        # Insert 120 conversations
        for c_idx in range(num_convs):
            c_id = f"scale-conv-{c_idx:04d}"
            leaf_id = f"scale-msg-{c_idx:04d}-{msgs_per_conv - 1:02d}"
            self.conv_repo.upsert_conversation({
                "id": c_id,
                "title": f"Scale Conversation {c_idx}",
                "current_node_id": leaf_id,
                "model_slug": "gpt-4o",
                "created_at": f"2026-10-01T10:{c_idx % 60:02d}:00Z",
                "updated_at": f"2026-10-01T11:{c_idx % 60:02d}:00Z",
            })

            # Generate 20 chained messages for this conversation
            messages: List[Dict] = []
            prev_id = None
            for m_idx in range(msgs_per_conv):
                m_id = f"scale-msg-{c_idx:04d}-{m_idx:02d}"
                role = "user" if m_idx % 2 == 0 else "assistant"
                messages.append({
                    "id": m_id,
                    "conversation_id": c_id,
                    "parent_id": prev_id,
                    "role": role,
                    "content_text": f"Scale payload turn {m_idx} for conversation {c_idx}. Some longer text payload with unicode characters: 🚀 àéïôù 12345.",
                    "status": "finished_successfully",
                    "created_at": f"2026-10-01T11:{m_idx:02d}:00Z",
                })
                prev_id = m_id

            self.conv_repo.save_messages(messages)

        t_insert_end = time.perf_counter()
        insert_duration_s = t_insert_end - t_insert_start

        # Verify DB counts and WAL status
        with self.db_mgr.get_read_connection() as conn:
            conv_count = conn.execute("SELECT COUNT(*) FROM conversations;").fetchone()[0]
            msg_count = conn.execute("SELECT COUNT(*) FROM messages;").fetchone()[0]
            integrity = conn.execute("PRAGMA integrity_check;").fetchone()[0]
            journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]

        self.assertEqual(conv_count, num_convs)
        self.assertEqual(msg_count, total_expected_msgs)
        self.assertEqual(integrity, "ok")
        self.assertEqual(journal_mode.lower(), "wal")

        # Verify Pagination across 120 conversations
        page_size = 28
        seen_ids = set()
        total_retrieved = 0

        for page in range(5):  # 5 pages: 0-27, 28-55, 56-83, 84-111, 112-119
            offset = page * page_size
            convs, total = self.conv_repo.list_conversations(offset=offset, limit=page_size)
            self.assertEqual(total, num_convs)

            expected_page_len = min(page_size, num_convs - offset)
            self.assertEqual(len(convs), expected_page_len)

            for c in convs:
                self.assertNotIn(c.id, seen_ids, f"Duplicate conversation ID {c.id} returned in pagination")
                seen_ids.add(c.id)
                total_retrieved += 1

        self.assertEqual(total_retrieved, num_convs)
        self.assertEqual(len(seen_ids), num_convs)

        # Sample and verify active branches on 5 random conversations
        sample_indices = [0, 25, 50, 75, 119]
        for s_idx in sample_indices:
            c_id = f"scale-conv-{s_idx:04d}"
            branch = self.conv_repo.get_active_branch(c_id)
            self.assertEqual(len(branch), msgs_per_conv)
            self.assertEqual(branch[0].id, f"scale-msg-{s_idx:04d}-00")
            self.assertEqual(branch[-1].id, f"scale-msg-{s_idx:04d}-{msgs_per_conv - 1:02d}")

        print(f"\n[SCALE TEST] Successfully ingested {num_convs} convs and {total_expected_msgs} msgs in {insert_duration_s:.3f}s")

    def test_02_deep_conversation_tree_cte_recursion(self):
        """
        Adversarial Test 2: Deep conversation tree (500+ chained messages).
        Constructs a linear chain of 550 and 800 messages in a single conversation.
        Verifies:
        - Recursive CTE executes without recursion limits, stack overflow, or memory error.
        - Exact message count of 550 and 800 returned.
        - Chronological root-to-leaf ordering is strictly preserved.
        - Parent pointer link continuity holds across all nodes.
        """
        # Test A: 550 chained messages
        conv_id_550 = "conv-deep-550"
        leaf_id_550 = "msg-deep-550-0549"
        self.conv_repo.upsert_conversation({
            "id": conv_id_550,
            "title": "Deep Tree 550 Messages",
            "current_node_id": leaf_id_550,
            "created_at": "2026-10-01T12:00:00Z",
            "updated_at": "2026-10-01T12:55:00Z"
        })

        messages_550: List[Dict] = []
        prev_id = None
        for i in range(550):
            m_id = f"msg-deep-550-{i:04d}"
            messages_550.append({
                "id": m_id,
                "conversation_id": conv_id_550,
                "parent_id": prev_id,
                "role": "user" if i % 2 == 0 else "assistant",
                "content_text": f"Deep turn {i}",
                "created_at": "2026-10-01T12:00:00Z",
            })
            prev_id = m_id

        self.conv_repo.save_messages(messages_550)

        # Retrieve active branch via recursive CTE
        t0 = time.perf_counter()
        branch_550 = self.conv_repo.get_active_branch(conv_id_550)
        t1 = time.perf_counter()
        lat_550_ms = (t1 - t0) * 1000.0

        self.assertEqual(len(branch_550), 550, f"Expected 550 messages, got {len(branch_550)}")
        self.assertEqual(branch_550[0].id, "msg-deep-550-0000")
        self.assertIsNone(branch_550[0].parent_id)
        self.assertEqual(branch_550[-1].id, leaf_id_550)

        # Verify every node links to previous
        for idx in range(1, 550):
            self.assertEqual(branch_550[idx].parent_id, branch_550[idx - 1].id)

        print(f"\n[DEEP TREE 550] Recovered 550 chained messages in {lat_550_ms:.3f} ms")

        # Test B: 800 chained messages
        conv_id_800 = "conv-deep-800"
        leaf_id_800 = "msg-deep-800-0799"
        self.conv_repo.upsert_conversation({
            "id": conv_id_800,
            "title": "Deep Tree 800 Messages",
            "current_node_id": leaf_id_800,
            "created_at": "2026-10-01T12:00:00Z",
            "updated_at": "2026-10-01T13:00:00Z"
        })

        messages_800: List[Dict] = []
        prev_id = None
        for i in range(800):
            m_id = f"msg-deep-800-{i:04d}"
            messages_800.append({
                "id": m_id,
                "conversation_id": conv_id_800,
                "parent_id": prev_id,
                "role": "user" if i % 2 == 0 else "assistant",
                "content_text": f"Turn {i}",
                "created_at": "2026-10-01T12:00:00Z",
            })
            prev_id = m_id

        self.conv_repo.save_messages(messages_800)

        t0 = time.perf_counter()
        branch_800 = self.conv_repo.get_active_branch(conv_id_800)
        t1 = time.perf_counter()
        lat_800_ms = (t1 - t0) * 1000.0

        self.assertEqual(len(branch_800), 800, f"Expected 800 messages, got {len(branch_800)}")
        self.assertEqual(branch_800[0].id, "msg-deep-800-0000")
        self.assertEqual(branch_800[-1].id, leaf_id_800)
        print(f"[DEEP TREE 800] Recovered 800 chained messages in {lat_800_ms:.3f} ms")

    def test_03_highly_branched_conversation_active_isolation(self):
        """
        Adversarial Test 3: Highly branched conversation (12 regenerations and multiple forks).
        Topology:
        - Root: Q0 -> A0
        - Q1 (user follow-up on A0)
        - 12 sibling regenerations for Q1: A1_v01 to A1_v12 (all have parent_id = Q1)
        - Branch from A1_v03: Q2_b3 -> A2_b3 -> Q3_b3 -> A3_b3
        - Branch from A1_v07: Q2_b7 -> A2_b7
        - Fork off A2_b3: Q3_b3_alt -> A3_b3_alt
        - Branch from A1_v12: Q2_b12 -> A2_b12 -> Q3_b12 -> A3_b12 -> Q4_b12 -> A4_b12

        Total messages: 2 + 1 + 12 + 4 + 2 + 2 + 6 = 29 messages in DAG.
        Verifies:
        - Active branch isolation strictly isolates each leaf path with ZERO leakage from siblings or abandoned subtrees.
        - Dynamic branch switching correctly changes the path instantly.
        - Boundary handling: current_node_id = None, current_node_id = non-existent.
        """
        conv_id = "conv-complex-branches"
        self.conv_repo.upsert_conversation({
            "id": conv_id,
            "title": "Complex Branching Tree",
            "current_node_id": "A4_b12",
            "created_at": "2026-10-01T12:00:00Z",
            "updated_at": "2026-10-01T12:30:00Z"
        })

        all_messages: List[Dict] = [
            # Trunk
            {"id": "Q0", "conversation_id": conv_id, "parent_id": None, "role": "user", "content_text": "Question 0", "created_at": "2026-10-01T12:00:00Z"},
            {"id": "A0", "conversation_id": conv_id, "parent_id": "Q0", "role": "assistant", "content_text": "Answer 0", "created_at": "2026-10-01T12:01:00Z"},
            {"id": "Q1", "conversation_id": conv_id, "parent_id": "A0", "role": "user", "content_text": "Question 1", "created_at": "2026-10-01T12:02:00Z"},
        ]

        # 12 regenerations of A1
        for v in range(1, 13):
            all_messages.append({
                "id": f"A1_v{v:02d}",
                "conversation_id": conv_id,
                "parent_id": "Q1",
                "role": "assistant",
                "content_text": f"Regeneration variant {v}",
                "created_at": f"2026-10-01T12:03:{v:02d}Z",
            })

        # Branch from A1_v03
        all_messages.extend([
            {"id": "Q2_b3", "conversation_id": conv_id, "parent_id": "A1_v03", "role": "user", "content_text": "Q2 on b3", "created_at": "2026-10-01T12:04:00Z"},
            {"id": "A2_b3", "conversation_id": conv_id, "parent_id": "Q2_b3", "role": "assistant", "content_text": "A2 on b3", "created_at": "2026-10-01T12:05:00Z"},
            {"id": "Q3_b3", "conversation_id": conv_id, "parent_id": "A2_b3", "role": "user", "content_text": "Q3 on b3", "created_at": "2026-10-01T12:06:00Z"},
            {"id": "A3_b3", "conversation_id": conv_id, "parent_id": "Q3_b3", "role": "assistant", "content_text": "A3 on b3", "created_at": "2026-10-01T12:07:00Z"},
        ])

        # Fork off A2_b3
        all_messages.extend([
            {"id": "Q3_b3_alt", "conversation_id": conv_id, "parent_id": "A2_b3", "role": "user", "content_text": "Q3 alternative", "created_at": "2026-10-01T12:08:00Z"},
            {"id": "A3_b3_alt", "conversation_id": conv_id, "parent_id": "Q3_b3_alt", "role": "assistant", "content_text": "A3 alternative", "created_at": "2026-10-01T12:09:00Z"},
        ])

        # Branch from A1_v07
        all_messages.extend([
            {"id": "Q2_b7", "conversation_id": conv_id, "parent_id": "A1_v07", "role": "user", "content_text": "Q2 on b7", "created_at": "2026-10-01T12:10:00Z"},
            {"id": "A2_b7", "conversation_id": conv_id, "parent_id": "Q2_b7", "role": "assistant", "content_text": "A2 on b7", "created_at": "2026-10-01T12:11:00Z"},
        ])

        # Branch from A1_v12 (Active leaf A4_b12)
        all_messages.extend([
            {"id": "Q2_b12", "conversation_id": conv_id, "parent_id": "A1_v12", "role": "user", "content_text": "Q2 on b12", "created_at": "2026-10-01T12:12:00Z"},
            {"id": "A2_b12", "conversation_id": conv_id, "parent_id": "Q2_b12", "role": "assistant", "content_text": "A2 on b12", "created_at": "2026-10-01T12:13:00Z"},
            {"id": "Q3_b12", "conversation_id": conv_id, "parent_id": "A2_b12", "role": "user", "content_text": "Q3 on b12", "created_at": "2026-10-01T12:14:00Z"},
            {"id": "A3_b12", "conversation_id": conv_id, "parent_id": "Q3_b12", "role": "assistant", "content_text": "A3 on b12", "created_at": "2026-10-01T12:15:00Z"},
            {"id": "Q4_b12", "conversation_id": conv_id, "parent_id": "A3_b12", "role": "user", "content_text": "Q4 on b12", "created_at": "2026-10-01T12:16:00Z"},
            {"id": "A4_b12", "conversation_id": conv_id, "parent_id": "Q4_b12", "role": "assistant", "content_text": "A4 on b12 leaf", "created_at": "2026-10-01T12:17:00Z"},
        ])

        self.conv_repo.save_messages(all_messages)

        # Verification 1: Active leaf is A4_b12
        branch_b12 = self.conv_repo.get_active_branch(conv_id)
        ids_b12 = [m.id for m in branch_b12]
        expected_b12 = ["Q0", "A0", "Q1", "A1_v12", "Q2_b12", "A2_b12", "Q3_b12", "A3_b12", "Q4_b12", "A4_b12"]
        self.assertEqual(ids_b12, expected_b12)

        # Check strict non-leakage from any other branch
        for v in range(1, 12):
            self.assertNotIn(f"A1_v{v:02d}", ids_b12)
        for leaked in ["Q2_b3", "A2_b3", "Q3_b3", "A3_b3", "Q3_b3_alt", "A3_b3_alt", "Q2_b7", "A2_b7"]:
            self.assertNotIn(leaked, ids_b12)

        # Verification 2: Switch to alternate branch fork A3_b3_alt
        self.conv_repo.update_current_node(conv_id, "A3_b3_alt")
        branch_alt = self.conv_repo.get_active_branch(conv_id)
        ids_alt = [m.id for m in branch_alt]
        expected_alt = ["Q0", "A0", "Q1", "A1_v03", "Q2_b3", "A2_b3", "Q3_b3_alt", "A3_b3_alt"]
        self.assertEqual(ids_alt, expected_alt)
        self.assertNotIn("A3_b3", ids_alt)  # Sibling fork
        self.assertNotIn("A1_v12", ids_alt)

        # Verification 3: Switch to sibling fork A3_b3
        self.conv_repo.update_current_node(conv_id, "A3_b3")
        branch_b3 = self.conv_repo.get_active_branch(conv_id)
        ids_b3 = [m.id for m in branch_b3]
        expected_b3 = ["Q0", "A0", "Q1", "A1_v03", "Q2_b3", "A2_b3", "Q3_b3", "A3_b3"]
        self.assertEqual(ids_b3, expected_b3)
        self.assertNotIn("A3_b3_alt", ids_b3)

        # Verification 4: Switch to a bare regeneration variant with no children (A1_v05)
        self.conv_repo.update_current_node(conv_id, "A1_v05")
        branch_v5 = self.conv_repo.get_active_branch(conv_id)
        ids_v5 = [m.id for m in branch_v5]
        expected_v5 = ["Q0", "A0", "Q1", "A1_v05"]
        self.assertEqual(ids_v5, expected_v5)

        # Verification 5: Boundary cases
        # Non-existent node ID
        self.conv_repo.update_current_node(conv_id, "non_existent_node_uuid")
        branch_none = self.conv_repo.get_active_branch(conv_id)
        self.assertEqual(len(branch_none), 0)

        # None current_node_id
        sql = "UPDATE conversations SET current_node_id = NULL WHERE id = ?;"
        with self.db_mgr.get_connection() as conn:
            conn.execute(sql, (conv_id,))
        branch_null = self.conv_repo.get_active_branch(conv_id)
        self.assertEqual(len(branch_null), 0)

        print("\n[BRANCHING TEST] Successfully verified strict active branch isolation across 12 regenerations and 4 forks")

    def test_04_concurrent_read_write_wal_stress(self):
        """
        Adversarial Test 4: Concurrent read/write stress under SQLite WAL mode.
        Simulates 8 concurrent threads (4 writers, 4 readers).
        - Writers: continuous upserts and message additions.
        - Readers: continuous active branch reconstructions and conversation listing.
        Verifies:
        - 100% transaction completion without 'database is locked' unhandled errors.
        - Zero DB corruption or partial reads.
        - Integrity check returns 'ok' post-concurrency.
        """
        num_writers = 4
        num_readers = 4
        writes_per_thread = 20
        reads_per_thread = 40

        # Pre-seed a base conversation for readers
        base_conv_id = "conv-concurrent-base"
        self.conv_repo.upsert_conversation({
            "id": base_conv_id,
            "title": "Concurrent Base Thread",
            "current_node_id": "base-m-0",
            "created_at": "2026-10-01T12:00:00Z",
            "updated_at": "2026-10-01T12:00:00Z"
        })
        self.conv_repo.save_messages([{
            "id": "base-m-0",
            "conversation_id": base_conv_id,
            "parent_id": None,
            "role": "user",
            "content_text": "Initial seed message",
            "created_at": "2026-10-01T12:00:00Z"
        }])

        errors: List[str] = []
        writer_completed = 0
        reader_completed = 0

        def writer_worker(thread_idx: int) -> int:
            nonlocal errors
            count = 0
            for op in range(writes_per_thread):
                c_id = f"conv-write-t{thread_idx}-op{op}"
                leaf_id = f"msg-t{thread_idx}-op{op}-leaf"
                try:
                    self.conv_repo.upsert_conversation({
                        "id": c_id,
                        "title": f"Thread {thread_idx} Op {op}",
                        "current_node_id": leaf_id,
                        "created_at": "2026-10-01T12:00:00Z",
                        "updated_at": "2026-10-01T12:00:00Z"
                    })
                    self.conv_repo.save_messages([
                        {
                            "id": f"msg-t{thread_idx}-op{op}-root",
                            "conversation_id": c_id,
                            "parent_id": None,
                            "role": "user",
                            "content_text": f"Writer root {thread_idx}:{op}",
                            "created_at": "2026-10-01T12:00:00Z"
                        },
                        {
                            "id": leaf_id,
                            "conversation_id": c_id,
                            "parent_id": f"msg-t{thread_idx}-op{op}-root",
                            "role": "assistant",
                            "content_text": f"Writer leaf {thread_idx}:{op}",
                            "created_at": "2026-10-01T12:01:00Z"
                        }
                    ])
                    count += 1
                except Exception as e:
                    errors.append(f"Writer t{thread_idx} op{op} failed: {type(e).__name__}: {e}")
            return count

        def reader_worker(thread_idx: int) -> int:
            nonlocal errors
            count = 0
            for op in range(reads_per_thread):
                try:
                    if op % 2 == 0:
                        branch = self.conv_repo.get_active_branch(base_conv_id)
                        if len(branch) < 1:
                            errors.append(f"Reader t{thread_idx} op{op}: empty branch for base conversation")
                    else:
                        convs, total = self.conv_repo.list_conversations(limit=20)
                        if total < 1:
                            errors.append(f"Reader t{thread_idx} op{op}: empty total in list_conversations")
                    count += 1
                except Exception as e:
                    errors.append(f"Reader t{thread_idx} op{op} failed: {type(e).__name__}: {e}")
            return count

        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=num_writers + num_readers) as executor:
            writer_futures = [executor.submit(writer_worker, i) for i in range(num_writers)]
            reader_futures = [executor.submit(reader_worker, i) for i in range(num_readers)]

            for f in as_completed(writer_futures):
                writer_completed += f.result()
            for f in as_completed(reader_futures):
                reader_completed += f.result()
        t1 = time.perf_counter()
        elapsed_s = t1 - t0

        self.assertEqual(len(errors), 0, f"Encountered concurrency errors: {errors[:5]}")
        self.assertEqual(writer_completed, num_writers * writes_per_thread)
        self.assertEqual(reader_completed, num_readers * reads_per_thread)

        # Verify DB integrity after concurrent stress
        with self.db_mgr.get_read_connection() as conn:
            integrity = conn.execute("PRAGMA integrity_check;").fetchone()[0]
        self.assertEqual(integrity, "ok")

        print(f"\n[CONCURRENCY TEST] Completed {writer_completed} writes and {reader_completed} reads in {elapsed_s:.3f}s with 0 errors")

    def test_05_cold_restart_latency_under_populated_load(self):
        """
        Adversarial Test 5: Cold restart latency under populated load.
        Populates DB with 100 conversations and 2,000 messages (including a 500-message deep tree).
        Closes all connections.
        Measures cold initialization and active branch resolution time from disk.
        Verifies:
        - Latency is strictly < 20.0ms (typically < 3ms).
        - Active branch has full data fidelity and zero missing turns.
        """
        # Pre-populate with 99 conversations x 15 messages = 1485 messages
        for c_idx in range(99):
            c_id = f"cold-conv-{c_idx:03d}"
            leaf_id = f"cold-msg-{c_idx:03d}-14"
            self.conv_repo.upsert_conversation({
                "id": c_id,
                "title": f"Cold Thread {c_idx}",
                "current_node_id": leaf_id,
                "created_at": "2026-10-01T12:00:00Z",
                "updated_at": "2026-10-01T12:15:00Z"
            })
            msgs = []
            pid = None
            for m_idx in range(15):
                m_id = f"cold-msg-{c_idx:03d}-{m_idx:02d}"
                msgs.append({
                    "id": m_id,
                    "conversation_id": c_id,
                    "parent_id": pid,
                    "role": "user" if m_idx % 2 == 0 else "assistant",
                    "content_text": f"Payload turn {m_idx} in thread {c_idx}",
                    "created_at": "2026-10-01T12:00:00Z"
                })
                pid = m_id
            self.conv_repo.save_messages(msgs)

        # 100th conversation is a 515-message deep chain (total 2000 messages in DB)
        deep_conv_id = "cold-conv-deep-515"
        deep_leaf_id = "cold-deep-msg-0514"
        self.conv_repo.upsert_conversation({
            "id": deep_conv_id,
            "title": "Cold Deep Chain",
            "current_node_id": deep_leaf_id,
            "created_at": "2026-10-01T12:00:00Z",
            "updated_at": "2026-10-01T13:00:00Z"
        })
        deep_msgs = []
        pid = None
        for i in range(515):
            m_id = f"cold-deep-msg-{i:04d}"
            deep_msgs.append({
                "id": m_id,
                "conversation_id": deep_conv_id,
                "parent_id": pid,
                "role": "user" if i % 2 == 0 else "assistant",
                "content_text": f"Turn {i}",
                "created_at": "2026-10-01T12:00:00Z"
            })
            pid = m_id
        self.conv_repo.save_messages(deep_msgs)

        # Forcibly delete in-memory repo instances and simulate cold boot
        del self.conv_repo
        del self.db_mgr

        # Cold boot measurement 1: Standard conversation recovery
        t0 = time.perf_counter()
        cold_mgr = DatabaseManager(db_path=self.db_path)
        cold_repo = ConversationRepo(db=cold_mgr)
        branch_std = cold_repo.get_active_branch("cold-conv-042")
        t1 = time.perf_counter()
        lat_std_ms = (t1 - t0) * 1000.0

        self.assertEqual(len(branch_std), 15)
        self.assertEqual(branch_std[0].id, "cold-msg-042-00")
        self.assertEqual(branch_std[-1].id, "cold-msg-042-14")
        self.assertLess(lat_std_ms, 20.0, f"Cold recovery exceeded 20ms: {lat_std_ms:.3f}ms")

        # Cold boot measurement 2: Deep 515-message conversation recovery
        del cold_repo
        del cold_mgr

        t2 = time.perf_counter()
        cold_mgr_2 = DatabaseManager(db_path=self.db_path)
        cold_repo_2 = ConversationRepo(db=cold_mgr_2)
        branch_deep = cold_repo_2.get_active_branch(deep_conv_id)
        t3 = time.perf_counter()
        lat_deep_ms = (t3 - t2) * 1000.0

        self.assertEqual(len(branch_deep), 515)
        self.assertEqual(branch_deep[0].id, "cold-deep-msg-0000")
        self.assertEqual(branch_deep[-1].id, deep_leaf_id)
        self.assertLess(lat_deep_ms, 20.0, f"Deep cold recovery exceeded 20ms: {lat_deep_ms:.3f}ms")

        print(f"\n[COLD RECOVERY BENCHMARK under 100 convs / 2,000 msgs load]")
        print(f"Standard branch (15 msgs) cold recovery: {lat_std_ms:.3f} ms (budget < 20.0 ms)")
        print(f"Deep branch (515 msgs) cold recovery: {lat_deep_ms:.3f} ms (budget < 20.0 ms)")

    def test_06_recursion_limit_and_cycle_boundary(self):
        """
        Adversarial Test 6: Boundary behavior at recursion depth limit and circular reference graph.
        Verifies:
        - 999 chained messages resolves cleanly.
        - 1000 chained messages resolves cleanly.
        - 1050 chained messages bounds safely at 1000 messages without SQLite OperationalError exception.
        - Circular reference (A -> B -> A) terminates safely at depth 1000 without infinite hang.
        """
        # Test A: 999 chained messages
        conv_id_999 = "conv-depth-999"
        self.conv_repo.upsert_conversation({
            "id": conv_id_999,
            "title": "Depth 999 Thread",
            "current_node_id": "msg-999-0998",
            "created_at": "2026-10-01T12:00:00Z",
            "updated_at": "2026-10-01T12:00:00Z"
        })
        msgs_999 = []
        pid = None
        for i in range(999):
            m_id = f"msg-999-{i:04d}"
            msgs_999.append({
                "id": m_id,
                "conversation_id": conv_id_999,
                "parent_id": pid,
                "role": "user" if i % 2 == 0 else "assistant",
                "content_text": f"Turn {i}",
                "created_at": "2026-10-01T12:00:00Z"
            })
            pid = m_id
        self.conv_repo.save_messages(msgs_999)

        branch_999 = self.conv_repo.get_active_branch(conv_id_999)
        self.assertEqual(len(branch_999), 999)

        # Test B: 1050 chained messages - recursion guard boundary test
        conv_id_1050 = "conv-depth-1050"
        self.conv_repo.upsert_conversation({
            "id": conv_id_1050,
            "title": "Depth 1050 Thread",
            "current_node_id": "msg-1050-1049",
            "created_at": "2026-10-01T12:00:00Z",
            "updated_at": "2026-10-01T12:00:00Z"
        })
        msgs_1050 = []
        pid = None
        for i in range(1050):
            m_id = f"msg-1050-{i:04d}"
            msgs_1050.append({
                "id": m_id,
                "conversation_id": conv_id_1050,
                "parent_id": pid,
                "role": "user" if i % 2 == 0 else "assistant",
                "content_text": f"Turn {i}",
                "created_at": "2026-10-01T12:00:00Z"
            })
            pid = m_id
        self.conv_repo.save_messages(msgs_1050)

        # Must not throw sqlite3.OperationalError: recursive query max loop count of 1000 exceeded
        branch_1050 = self.conv_repo.get_active_branch(conv_id_1050)
        # Because WHERE b.depth < 1000 stops expansion when depth hits 1000, 1000 rows are emitted
        self.assertEqual(len(branch_1050), 1000)
        self.assertEqual(branch_1050[-1].id, "msg-1050-1049")  # Leaf message preserved

        # Test C: Circular reference graph resilience (m_circ_A -> m_circ_B -> m_circ_A)
        conv_id_circ = "conv-circular"
        self.conv_repo.upsert_conversation({
            "id": conv_id_circ,
            "title": "Circular Graph Thread",
            "current_node_id": "m_circ_B",
            "created_at": "2026-10-01T12:00:00Z",
            "updated_at": "2026-10-01T12:00:00Z"
        })
        self.conv_repo.save_messages([
            {
                "id": "m_circ_A",
                "conversation_id": conv_id_circ,
                "parent_id": "m_circ_B",  # Circular pointer
                "role": "user",
                "content_text": "Loop node A",
                "created_at": "2026-10-01T12:00:00Z"
            },
            {
                "id": "m_circ_B",
                "conversation_id": conv_id_circ,
                "parent_id": "m_circ_A",  # Circular pointer
                "role": "assistant",
                "content_text": "Loop node B",
                "created_at": "2026-10-01T12:01:00Z"
            }
        ])

        # Must terminate safely and not infinite-loop
        t_circ_0 = time.perf_counter()
        branch_circ = self.conv_repo.get_active_branch(conv_id_circ)
        t_circ_1 = time.perf_counter()
        self.assertEqual(len(branch_circ), 1000)
        self.assertLess(t_circ_1 - t_circ_0, 1.0, "Cycle query took longer than 1.0s")

    def test_07_deferred_foreign_keys_out_of_order_insertion(self):
        """
        Adversarial Test 7: Out-of-order parent-child batch insertion.
        Ingests child nodes BEFORE parent nodes in the same batch.
        Verifies DEFERRABLE INITIALLY DEFERRED foreign keys allow out-of-order tree streaming.
        """
        conv_id = "conv-out-of-order"
        self.conv_repo.upsert_conversation({
            "id": conv_id,
            "title": "Out of Order Ingestion",
            "current_node_id": "child-leaf",
            "created_at": "2026-10-01T12:00:00Z",
            "updated_at": "2026-10-01T12:00:00Z"
        })

        # Leaf first, intermediate second, root last
        reversed_messages = [
            {
                "id": "child-leaf",
                "conversation_id": conv_id,
                "parent_id": "intermediate-node",
                "role": "assistant",
                "content_text": "Leaf inserted first",
                "created_at": "2026-10-01T12:02:00Z"
            },
            {
                "id": "intermediate-node",
                "conversation_id": conv_id,
                "parent_id": "root-node",
                "role": "user",
                "content_text": "Intermediate inserted second",
                "created_at": "2026-10-01T12:01:00Z"
            },
            {
                "id": "root-node",
                "conversation_id": conv_id,
                "parent_id": None,
                "role": "system",
                "content_text": "Root inserted last",
                "created_at": "2026-10-01T12:00:00Z"
            }
        ]

        # Must not raise sqlite3.IntegrityError: FOREIGN KEY constraint failed
        self.conv_repo.save_messages(reversed_messages)

        branch = self.conv_repo.get_active_branch(conv_id)
        self.assertEqual(len(branch), 3)
        self.assertEqual(branch[0].id, "root-node")
        self.assertEqual(branch[1].id, "intermediate-node")
        self.assertEqual(branch[2].id, "child-leaf")

    def test_08_large_payload_and_metadata_fidelity(self):
        """
        Adversarial Test 8: Large text payloads and complex metadata serialization.
        Verifies:
        - Ingestion and recovery of 100KB+ text turns.
        - Deep JSON metadata preservation without corruption.
        - Multi-lingual unicode and emoji stability.
        """
        conv_id = "conv-large-payload"
        self.conv_repo.upsert_conversation({
            "id": conv_id,
            "title": "Large Payload Test",
            "current_node_id": "msg-large-leaf",
            "created_at": "2026-10-01T12:00:00Z",
            "updated_at": "2026-10-01T12:00:00Z"
        })

        large_payload = "A" * 128000  # 128 KB string
        complex_unicode = "🔥 ChatGPT 4o 🚀 | 日本語 | 한국어 | العربية | עִבְרִית | 🎯 Special chars: <script>alert(1)</script> \n\t \"''\\"
        complex_meta = {
            "model": "gpt-4o",
            "gizmo_id": "g-p-test-gizmo-12345",
            "finish_details": {"type": "stop", "stop_tokens": [100257]},
            "nested_list": [{"step": 1, "score": 0.99}, {"step": 2, "score": 0.95}],
            "witness_marker": "WITNESS_ALPHA_998877"
        }

        self.conv_repo.save_messages([
            {
                "id": "msg-large-root",
                "conversation_id": conv_id,
                "parent_id": None,
                "role": "user",
                "content_text": complex_unicode,
                "metadata": complex_meta,
                "created_at": "2026-10-01T12:00:00Z"
            },
            {
                "id": "msg-large-leaf",
                "conversation_id": conv_id,
                "parent_id": "msg-large-root",
                "role": "assistant",
                "content_text": large_payload,
                "metadata": {"length_kb": 128},
                "created_at": "2026-10-01T12:01:00Z"
            }
        ])

        branch = self.conv_repo.get_active_branch(conv_id)
        self.assertEqual(len(branch), 2)
        self.assertEqual(branch[0].content_text, complex_unicode)
        self.assertEqual(branch[0].metadata["witness_marker"], "WITNESS_ALPHA_998877")
        self.assertEqual(branch[0].metadata["nested_list"][0]["score"], 0.99)
        self.assertEqual(len(branch[1].content_text), 128000)
        self.assertEqual(branch[1].metadata["length_kb"], 128)


if __name__ == "__main__":
    unittest.main()

