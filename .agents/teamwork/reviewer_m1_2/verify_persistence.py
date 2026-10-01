"""
Independent Verification and Adversarial Stress Test Script for Milestone 1 Persistence Layer.
Conducted by Reviewer 2.
"""

import os
import sys
import time
import json
import sqlite3
import shutil
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path("/var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.persistence.db import DatabaseManager, SCHEMA_DDL, PRAGMAS
from app.persistence.conversation_repo import ConversationRepo
from app.persistence.project_repo import ProjectRepo
from app.persistence.session_repo import SessionRepo
from app.persistence.transport_log_repo import TransportLogRepo
from app.models.session import Session, SessionStatus
from app.models.conversation import Conversation, Message
from app.models.project import Project

RESULTS = []

def report(name, status, details=""):
    RESULTS.append((name, status, details))
    badge = "[PASS]" if status else "[FAIL]"
    print(f"{badge} {name}: {details}")

def test_wal_pragmas_and_concurrency(scratch_dir):
    db_path = scratch_dir / "wal_test.db"
    db_mgr = DatabaseManager(db_path=db_path)
    db_mgr.init_db()

    # 1. Verify pragmas on direct connection
    with db_mgr.get_read_connection() as conn:
        j_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        f_keys = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
        sync = conn.execute("PRAGMA synchronous;").fetchone()[0]
        timeout = conn.execute("PRAGMA busy_timeout;").fetchone()[0]

    pragmas_ok = (
        j_mode.lower() == "wal" and
        f_keys == 1 and
        sync in (1, 2) and  # 1 = NORMAL
        timeout == 5000
    )
    report("Pragmas Verification", pragmas_ok, f"journal_mode={j_mode}, foreign_keys={f_keys}, sync={sync}, timeout={timeout}")

    # 2. Concurrency test: Can reader read while writer holds uncommitted transaction?
    # In WAL mode, a writer in transaction should NOT block a concurrent reader.
    writer_conn = db_mgr._connect()
    reader_conn = db_mgr._connect()
    try:
        writer_conn.execute("BEGIN IMMEDIATE;")
        writer_conn.execute(
            "INSERT INTO projects (id, title, created_at, updated_at) VALUES ('p-conc', 'Concurrent Project', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);"
        )

        # Reader reads concurrently
        t0 = time.perf_counter()
        reader_res = reader_conn.execute("SELECT COUNT(*) FROM projects;").fetchone()[0]
        t1 = time.perf_counter()
        # Reader should see 0 (snapshot isolation), writer uncommitted
        concurrent_read_ok = (reader_res == 0) and ((t1 - t0) < 0.5)

        writer_conn.execute("COMMIT;")
        reader_after = reader_conn.execute("SELECT COUNT(*) FROM projects;").fetchone()[0]
        commit_read_ok = (reader_after == 1)

        report("WAL Concurrent Read During Write", concurrent_read_ok and commit_read_ok,
               f"Pre-commit reader saw count={reader_res} in {(t1-t0)*1000:.2f}ms, post-commit saw {reader_after}")
    finally:
        writer_conn.close()
        reader_conn.close()

def test_deferred_foreign_keys(scratch_dir):
    """
    Test insertion of child message BEFORE parent message within a single transaction.
    DEFERRABLE INITIALLY DEFERRED must allow this without IntegrityError.
    """
    db_path = scratch_dir / "deferred_fk.db"
    db_mgr = DatabaseManager(db_path=db_path)
    db_mgr.init_db()

    conv_repo = ConversationRepo(db=db_mgr)
    conv_id = "conv-fk-test"
    conv_repo.upsert_conversation({
        "id": conv_id,
        "title": "Deferred FK Test",
        "current_node_id": "child-msg",
        "created_at": "2026-10-01T12:00:00Z",
        "updated_at": "2026-10-01T12:00:00Z"
    })

    # Inverted order: child message is at index 0 (points to parent-msg), parent message is at index 1!
    inverted_messages = [
        {
            "id": "child-msg",
            "conversation_id": conv_id,
            "parent_id": "parent-msg",  # Doesn't exist in DB yet!
            "role": "assistant",
            "content_text": "I am the child inserted first",
            "created_at": "2026-10-01T12:01:00Z"
        },
        {
            "id": "parent-msg",
            "conversation_id": conv_id,
            "parent_id": None,
            "role": "user",
            "content_text": "I am the parent inserted second",
            "created_at": "2026-10-01T12:00:00Z"
        }
    ]

    try:
        conv_repo.save_messages(inverted_messages)
        branch = conv_repo.get_active_branch(conv_id)
        fk_ok = len(branch) == 2 and branch[0].id == "parent-msg" and branch[1].id == "child-msg"
        report("Deferred Foreign Key Inverted Insertion", fk_ok, f"Loaded active branch: {[m.id for m in branch]}")
    except Exception as e:
        report("Deferred Foreign Key Inverted Insertion", False, f"Failed with exception: {e}")

def test_recursive_cte_adversarial(scratch_dir):
    """
    Stress-test recursive CTE:
    - Multiple branching forks (A, B, C)
    - Dynamic leaf switching between forks
    - Synthetic root with empty text
    - Cycle loop (A -> B -> A) to test cycle guard
    - Missing/invalid current_node_id
    """
    db_path = scratch_dir / "cte_adv.db"
    db_mgr = DatabaseManager(db_path=db_path)
    db_mgr.init_db()
    conv_repo = ConversationRepo(db=db_mgr)

    conv_id = "conv-forks"
    conv_repo.upsert_conversation({
        "id": conv_id,
        "title": "Adversarial Forks",
        "current_node_id": "leaf-b2",
        "created_at": "2026-10-01T12:00:00Z",
        "updated_at": "2026-10-01T12:00:00Z"
    })

    # Topology:
    # root (synthetic empty)
    #   -> msg-1 (user)
    #       -> msg-2a (assistant fork A) -> msg-3a (user fork A) -> leaf-a (assistant fork A)
    #       -> msg-2b (assistant fork B) -> leaf-b1 (user fork B) -> leaf-b2 (assistant fork B)
    #       -> msg-2c (assistant fork C) -> leaf-c (user fork C)
    all_messages = [
        {"id": "root", "conversation_id": conv_id, "parent_id": None, "role": "system", "content_text": "", "created_at": "2026-10-01T12:00:00Z"},
        {"id": "msg-1", "conversation_id": conv_id, "parent_id": "root", "role": "user", "content_text": "Question 1", "created_at": "2026-10-01T12:01:00Z"},
        # Fork A
        {"id": "msg-2a", "conversation_id": conv_id, "parent_id": "msg-1", "role": "assistant", "content_text": "Fork A 2", "created_at": "2026-10-01T12:02:00Z"},
        {"id": "msg-3a", "conversation_id": conv_id, "parent_id": "msg-2a", "role": "user", "content_text": "Fork A 3", "created_at": "2026-10-01T12:03:00Z"},
        {"id": "leaf-a", "conversation_id": conv_id, "parent_id": "msg-3a", "role": "assistant", "content_text": "Fork A Leaf", "created_at": "2026-10-01T12:04:00Z"},
        # Fork B
        {"id": "msg-2b", "conversation_id": conv_id, "parent_id": "msg-1", "role": "assistant", "content_text": "Fork B 2", "created_at": "2026-10-01T12:02:30Z"},
        {"id": "leaf-b1", "conversation_id": conv_id, "parent_id": "msg-2b", "role": "user", "content_text": "Fork B Leaf 1", "created_at": "2026-10-01T12:03:30Z"},
        {"id": "leaf-b2", "conversation_id": conv_id, "parent_id": "leaf-b1", "role": "assistant", "content_text": "Fork B Leaf 2", "created_at": "2026-10-01T12:04:30Z"},
        # Fork C
        {"id": "msg-2c", "conversation_id": conv_id, "parent_id": "msg-1", "role": "assistant", "content_text": "Fork C 2", "created_at": "2026-10-01T12:02:45Z"},
        {"id": "leaf-c", "conversation_id": conv_id, "parent_id": "msg-2c", "role": "user", "content_text": "Fork C Leaf", "created_at": "2026-10-01T12:03:45Z"},
    ]
    conv_repo.save_messages(all_messages)

    # 1. Test Fork B isolation
    branch_b = conv_repo.get_active_branch(conv_id)
    ids_b = [m.id for m in branch_b]
    expected_b = ["root", "msg-1", "msg-2b", "leaf-b1", "leaf-b2"]
    report("Active Branch Isolation (Fork B)", ids_b == expected_b, f"Got: {ids_b}")

    # 2. Test Dynamic Switch to Fork A
    conv_repo.update_current_node(conv_id, "leaf-a")
    branch_a = conv_repo.get_active_branch(conv_id)
    ids_a = [m.id for m in branch_a]
    expected_a = ["root", "msg-1", "msg-2a", "msg-3a", "leaf-a"]
    report("Dynamic Switch to Fork A", ids_a == expected_a, f"Got: {ids_a}")

    # 3. Test Dynamic Switch to Fork C
    conv_repo.update_current_node(conv_id, "leaf-c")
    branch_c = conv_repo.get_active_branch(conv_id)
    ids_c = [m.id for m in branch_c]
    expected_c = ["root", "msg-1", "msg-2c", "leaf-c"]
    report("Dynamic Switch to Fork C", ids_c == expected_c, f"Got: {ids_c}")

    # 4. Test Missing Current Node (points to non-existent ID)
    conv_repo.update_current_node(conv_id, "non-existent-leaf-id")
    branch_missing = conv_repo.get_active_branch(conv_id)
    report("Missing Current Node Handling", branch_missing == [], f"Returned {len(branch_missing)} items for non-existent leaf")

    # 5. Test Cycle Guard (Adversarial Cycle Injection)
    # Create cycle: cycle-1 -> cycle-2 -> cycle-1
    conv_cycle_id = "conv-cycle"
    conv_repo.upsert_conversation({
        "id": conv_cycle_id,
        "title": "Cycle Test",
        "current_node_id": "cycle-2",
        "created_at": "2026-10-01T12:00:00Z",
        "updated_at": "2026-10-01T12:00:00Z"
    })
    cycle_msgs = [
        {"id": "cycle-1", "conversation_id": conv_cycle_id, "parent_id": "cycle-2", "role": "user", "content_text": "Cycle 1", "created_at": "2026-10-01T12:00:00Z"},
        {"id": "cycle-2", "conversation_id": conv_cycle_id, "parent_id": "cycle-1", "role": "assistant", "content_text": "Cycle 2", "created_at": "2026-10-01T12:01:00Z"},
    ]
    conv_repo.save_messages(cycle_msgs)
    t0 = time.perf_counter()
    branch_cycle = conv_repo.get_active_branch(conv_cycle_id)
    t1 = time.perf_counter()
    cycle_time_ms = (t1 - t0) * 1000.0
    # Because depth < 1000, recursion terminates and does NOT hang indefinitely
    cycle_ok = (len(branch_cycle) == 1000) and (cycle_time_ms < 50.0)
    report("CTE Recursion Cycle Guard", cycle_ok, f"Terminated at {len(branch_cycle)} items in {cycle_time_ms:.2f}ms without hang")

def test_cold_boot_benchmark(scratch_dir):
    """
    Rigorous Cold Boot Latency Benchmark:
    50 conversations, 500 messages (10 messages per thread).
    Measure 100 independent cold starts.
    Calculate min, mean, p95, max against 20.0 ms budget.
    """
    db_path = scratch_dir / "bench_500.db"
    db_mgr = DatabaseManager(db_path=db_path)
    db_mgr.init_db()
    conv_repo = ConversationRepo(db=db_mgr)

    # Populate 50 convs with 10 msgs each
    for c in range(50):
        c_id = f"bench-conv-{c:02d}"
        last_id = f"bench-msg-{c:02d}-09"
        conv_repo.upsert_conversation({
            "id": c_id,
            "title": f"Bench {c}",
            "current_node_id": last_id,
            "created_at": "2026-10-01T12:00:00Z",
            "updated_at": "2026-10-01T12:10:00Z"
        })
        msgs = []
        pid = None
        for m in range(10):
            m_id = f"bench-msg-{c:02d}-{m:02d}"
            msgs.append({
                "id": m_id,
                "conversation_id": c_id,
                "parent_id": pid,
                "role": "user" if m % 2 == 0 else "assistant",
                "content_text": f"Benchmark message {m} in thread {c}",
                "created_at": f"2026-10-01T12:{m:02d}:00Z"
            })
            pid = m_id
        conv_repo.save_messages(msgs)

    # Now run 100 cold starts
    durations = []
    target_conv = "bench-conv-42"
    for i in range(100):
        t0 = time.perf_counter()
        cold_mgr = DatabaseManager(db_path=db_path)
        cold_repo = ConversationRepo(db=cold_mgr)
        branch = cold_repo.get_active_branch(target_conv)
        t1 = time.perf_counter()
        durations.append((t1 - t0) * 1000.0)
        assert len(branch) == 10

    durations.sort()
    min_lat = durations[0]
    mean_lat = sum(durations) / len(durations)
    p50_lat = durations[50]
    p95_lat = durations[95]
    max_lat = durations[-1]

    bench_ok = max_lat < 20.0
    report(
        "Cold Boot Latency Benchmark (100 runs)",
        bench_ok,
        f"min={min_lat:.3f}ms, mean={mean_lat:.3f}ms, p50={p50_lat:.3f}ms, p95={p95_lat:.3f}ms, max={max_lat:.3f}ms (Budget < 20.0ms)"
    )

def test_session_repo_integrity(scratch_dir):
    """Test SessionRepo operations, timestamps, credentials masking."""
    db_path = scratch_dir / "session_test.db"
    db_mgr = DatabaseManager(db_path=db_path)
    db_mgr.init_db()
    repo = SessionRepo(db=db_mgr)

    # 1. Initial None
    self_none = repo.get_default()
    report("Initial Session None", self_none is None, f"Initial state={self_none}")

    # 2. Save session
    sess = Session(
        id="default",
        access_token="test_token_1234567890",
        cookies_json='{"__Secure-next-auth.session-token": "tok_xyz", "cf_clearance": "clear_xyz"}',
        status=SessionStatus.VALID,
        user_email="test@example.com",
        account_id="acc_123"
    )
    repo.save(sess)
    loaded = repo.get_default()
    save_ok = (
        loaded is not None and
        loaded.access_token == "test_token_1234567890" and
        loaded.user_email == "test@example.com" and
        loaded.status == SessionStatus.VALID
    )
    report("Session Save & Load", save_ok, f"Loaded status={loaded.status if loaded else None}")

    # 3. Credentials encapsulation
    creds = repo.get_credentials()
    creds_ok = (
        creds is not None and
        creds.access_token == "test_token_1234567890" and
        creds.session_token == "tok_xyz" and
        creds.cf_clearance == "clear_xyz" and
        "test_token_1234567890" not in repr(creds)
    )
    report("Credentials Encapsulation & Masking", creds_ok, f"Masked: {creds.masked_access_token() if creds else None}")

    # 4. Status update
    repo.update_status(SessionStatus.EXPIRED)
    expired_sess = repo.get_default()
    status_ok = expired_sess.status == SessionStatus.EXPIRED
    report("Session Status Update", status_ok, f"Updated status={expired_sess.status}")

def test_project_and_transport_repo(scratch_dir):
    """Test ProjectRepo and TransportLogRepo CRUD and filtering."""
    db_path = scratch_dir / "proj_trans.db"
    db_mgr = DatabaseManager(db_path=db_path)
    db_mgr.init_db()

    p_repo = ProjectRepo(db=db_mgr)
    t_repo = TransportLogRepo(db=db_mgr)

    # Project with files
    p = p_repo.upsert_project({
        "id": "g-p-test-proj",
        "title": "Alpha Project",
        "description": "Description",
        "instructions": "Be accurate and concise",
        "files": [{"id": "f-1", "name": "spec.pdf", "size_bytes": 1024}]
    })
    loaded_p = p_repo.get_project("g-p-test-proj")
    proj_ok = (
        loaded_p is not None and
        loaded_p.title == "Alpha Project" and
        loaded_p.instructions == "Be accurate and concise" and
        len(loaded_p.files) == 1 and
        loaded_p.files[0].name == "spec.pdf"
    )
    report("Project Metadata & Files Serialization", proj_ok, f"Files loaded: {[f.name for f in loaded_p.files] if loaded_p else []}")

    # Transport Log
    id1 = t_repo.log_operation("fetch_projects", "DIRECT_HTTP", latency_ms=12.5, success=True)
    id2 = t_repo.log_operation("send_message", "BROWSER_FETCH", latency_ms=150.0, success=False, error_message="Challenge")

    logs = t_repo.list_logs(limit=10)
    direct_logs = t_repo.list_logs(transport_mode="DIRECT_HTTP")
    trans_ok = len(logs) == 2 and len(direct_logs) == 1 and direct_logs[0].operation == "fetch_projects"
    report("Transport Log Audit Filtering", trans_ok, f"Total logs={len(logs)}, DIRECT_HTTP logs={len(direct_logs)}")

def run_all():
    scratch_dir = Path("/var/www/POC/YOW-ChatGPT-Integration-POC-By-Antigravity/.agents/teamwork/reviewer_m1_2/scratch")
    if scratch_dir.exists():
        shutil.rmtree(scratch_dir)
    scratch_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("STARTING INDEPENDENT VERIFICATION & ADVERSARIAL TESTS")
    print("=" * 60)

    try:
        test_wal_pragmas_and_concurrency(scratch_dir)
        test_deferred_foreign_keys(scratch_dir)
        test_recursive_cte_adversarial(scratch_dir)
        test_cold_boot_benchmark(scratch_dir)
        test_session_repo_integrity(scratch_dir)
        test_project_and_transport_repo(scratch_dir)
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)

    print("=" * 60)
    all_passed = all(status for _, status, _ in RESULTS)
    print(f"VERIFICATION SUMMARY: {'ALL PASSED' if all_passed else 'SOME FAILED'}")
    print("=" * 60)
    return all_passed

if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
