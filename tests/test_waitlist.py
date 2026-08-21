import os
import sqlite3
import pytest
from concurrent.futures import ThreadPoolExecutor
from app.config import settings
from app.services import queue_service, student_service

settings.mock_auth = True
settings.db_path = "test_queuecraft.db"

@pytest.fixture(scope="module", autouse=True)
def setup_waitlist_db():
    settings.db_path = "test_queuecraft.db"
    from app.database import initialize_schema, seed_database
    initialize_schema()
    seed_database()
    yield

def get_test_conn():
    conn = sqlite3.connect(settings.db_path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn

# TEST 1 — Empty waitlist
def test_1_empty_waitlist():
    conn = get_test_conn()
    conn.execute("UPDATE services SET max_capacity = 5 WHERE id = 'srv-lp';")
    conn.execute("DELETE FROM tokens WHERE service_id = 'srv-lp';")

    result = queue_service.evaluate_and_promote_waitlist(conn, 'srv-lp')
    assert len(result["promoted_tokens"]) == 0
    assert result["available_slots"] == 5

# TEST 2 — One eligible candidate
def test_2_one_eligible_candidate():
    conn = get_test_conn()
    conn.execute("UPDATE services SET max_capacity = 2 WHERE id = 'srv-lp';")
    conn.execute("DELETE FROM tokens WHERE service_id = 'srv-lp';")

    conn.execute("""
        INSERT INTO tokens (id, token_number, student_name, service_id, priority, status, created_at)
        VALUES 
          ('tkn-py-act1', 'LP-001', 'Active Student', 'srv-lp', 'NORMAL', 'WAITING', datetime('now', '-5 minutes')),
          ('tkn-py-wl1', 'LP-002', 'Waitlisted Student', 'srv-lp', 'NORMAL', 'WAITLISTED', datetime('now', '-2 minutes'));
    """)

    result = queue_service.evaluate_and_promote_waitlist(conn, 'srv-lp')
    assert len(result["promoted_tokens"]) == 1
    assert result["promoted_tokens"][0]["id"] == "tkn-py-wl1"

    cursor = conn.cursor()
    cursor.execute("SELECT status FROM tokens WHERE id = 'tkn-py-wl1';")
    assert cursor.fetchone()["status"] == "WAITING"

# TEST 3 — Multiple candidates
def test_3_multiple_candidates():
    conn = get_test_conn()
    conn.execute("UPDATE services SET max_capacity = 1 WHERE id = 'srv-lp';")
    conn.execute("DELETE FROM tokens WHERE service_id = 'srv-lp';")

    conn.execute("""
        INSERT INTO tokens (id, token_number, student_name, service_id, priority, status, created_at)
        VALUES 
          ('tkn-py-low', 'LP-001', 'Low Priority', 'srv-lp', 'NORMAL', 'WAITLISTED', datetime('now', '-2 minutes')),
          ('tkn-py-high', 'LP-002', 'High Priority', 'srv-lp', 'HIGH', 'WAITLISTED', datetime('now', '-2 minutes'));
    """)

    result = queue_service.evaluate_and_promote_waitlist(conn, 'srv-lp')
    assert len(result["promoted_tokens"]) == 1
    assert result["promoted_tokens"][0]["id"] == "tkn-py-high"

# TEST 4 — Priority ordering
def test_4_priority_ordering():
    conn = get_test_conn()
    conn.execute("UPDATE services SET max_capacity = 2 WHERE id = 'srv-lp';")
    conn.execute("DELETE FROM tokens WHERE service_id = 'srv-lp';")

    conn.execute("""
        INSERT INTO tokens (id, token_number, student_name, service_id, priority, status, created_at)
        VALUES 
          ('tkn-py-norm', 'LP-001', 'Normal Student', 'srv-lp', 'NORMAL', 'WAITLISTED', datetime('now', '-5 minutes')),
          ('tkn-py-urg', 'LP-002', 'Urgent Student', 'srv-lp', 'URGENT', 'WAITLISTED', datetime('now', '-5 minutes')),
          ('tkn-py-hi', 'LP-003', 'High Student', 'srv-lp', 'HIGH', 'WAITLISTED', datetime('now', '-5 minutes'));
    """)

    result = queue_service.evaluate_and_promote_waitlist(conn, 'srv-lp')
    assert len(result["promoted_tokens"]) == 2
    assert result["promoted_tokens"][0]["id"] == "tkn-py-urg"
    assert result["promoted_tokens"][1]["id"] == "tkn-py-hi"

# TEST 5 — Same priority FIFO
def test_5_same_priority_fifo():
    conn = get_test_conn()
    conn.execute("UPDATE services SET max_capacity = 1 WHERE id = 'srv-lp';")
    conn.execute("DELETE FROM tokens WHERE service_id = 'srv-lp';")

    conn.execute("""
        INSERT INTO tokens (id, token_number, student_name, service_id, priority, status, created_at)
        VALUES 
          ('tkn-py-rec', 'LP-002', 'Recent Student', 'srv-lp', 'NORMAL', 'WAITLISTED', datetime('now', '-2 minutes')),
          ('tkn-py-old', 'LP-001', 'Older Student', 'srv-lp', 'NORMAL', 'WAITLISTED', datetime('now', '-10 minutes'));
    """)

    result = queue_service.evaluate_and_promote_waitlist(conn, 'srv-lp')
    assert len(result["promoted_tokens"]) == 1
    assert result["promoted_tokens"][0]["id"] == "tkn-py-old"

# TEST 6 — Deterministic tie breaking
def test_6_deterministic_tie_breaking():
    conn = get_test_conn()
    conn.execute("UPDATE services SET max_capacity = 1 WHERE id = 'srv-lp';")
    conn.execute("DELETE FROM tokens WHERE service_id = 'srv-lp';")

    fixed_ts = '2026-08-21 12:00:00.000'
    conn.execute("""
        INSERT INTO tokens (id, token_number, student_name, service_id, priority, status, created_at)
        VALUES 
          ('tkn-py-z', 'LP-099', 'Student Z', 'srv-lp', 'NORMAL', 'WAITLISTED', ?),
          ('tkn-py-a', 'LP-001', 'Student A', 'srv-lp', 'NORMAL', 'WAITLISTED', ?);
    """, (fixed_ts, fixed_ts))

    result = queue_service.evaluate_and_promote_waitlist(conn, 'srv-lp')
    assert len(result["promoted_tokens"]) == 1
    assert result["promoted_tokens"][0]["id"] == "tkn-py-a"

# TEST 7 — Cancelled candidate skipped
def test_7_cancelled_candidate_skipped():
    conn = get_test_conn()
    conn.execute("UPDATE services SET max_capacity = 1 WHERE id = 'srv-lp';")
    conn.execute("DELETE FROM tokens WHERE service_id = 'srv-lp';")

    conn.execute("""
        INSERT INTO tokens (id, token_number, student_name, service_id, priority, status, created_at)
        VALUES 
          ('tkn-py-canc', 'LP-001', 'Cancelled Student', 'srv-lp', 'URGENT', 'CANCELLED', datetime('now', '-10 minutes')),
          ('tkn-py-valid', 'LP-002', 'Valid Student', 'srv-lp', 'NORMAL', 'WAITLISTED', datetime('now', '-5 minutes'));
    """)

    result = queue_service.evaluate_and_promote_waitlist(conn, 'srv-lp')
    assert len(result["promoted_tokens"]) == 1
    assert result["promoted_tokens"][0]["id"] == "tkn-py-valid"

# TEST 8 — Ineligible candidate with active duplicate token skipped
def test_8_ineligible_candidate_skipped():
    conn = get_test_conn()
    conn.execute("UPDATE services SET max_capacity = 2 WHERE id = 'srv-lp';")
    conn.execute("DELETE FROM tokens WHERE service_id = 'srv-lp';")

    conn.execute("""
        INSERT INTO tokens (id, token_number, student_id, student_name, service_id, priority, status, created_at)
        VALUES 
          ('tkn-py-act-stu1', 'LP-001', 'usr-student-aarav', 'Aarav Sharma', 'srv-lp', 'NORMAL', 'WAITING', datetime('now', '-10 minutes')),
          ('tkn-py-wl-stu1', 'LP-002', 'usr-student-aarav', 'Aarav Sharma', 'srv-lp', 'URGENT', 'WAITLISTED', datetime('now', '-5 minutes')),
          ('tkn-py-wl-stu2', 'LP-003', 'usr-student-ananya', 'Ananya Patel', 'srv-lp', 'NORMAL', 'WAITLISTED', datetime('now', '-4 minutes'));
    """)

    result = queue_service.evaluate_and_promote_waitlist(conn, 'srv-lp')
    assert len(result["promoted_tokens"]) == 1
    assert result["promoted_tokens"][0]["id"] == "tkn-py-wl-stu2"

# TEST 9 — Multiple available slots
def test_9_multiple_slots():
    conn = get_test_conn()
    conn.execute("UPDATE services SET max_capacity = 3 WHERE id = 'srv-lp';")
    conn.execute("DELETE FROM tokens WHERE service_id = 'srv-lp';")

    for i in range(1, 6):
        conn.execute("""
            INSERT INTO tokens (id, token_number, student_name, service_id, priority, status, created_at)
            VALUES (?, ?, ?, 'srv-lp', 'NORMAL', 'WAITLISTED', datetime('now', ?));
        """, (f"tkn-py-mult-{i}", f"LP-00{i}", f"Student {i}", f"-{10 - i} minutes"))

    result = queue_service.evaluate_and_promote_waitlist(conn, 'srv-lp')
    assert len(result["promoted_tokens"]) == 3
    promoted_ids = [t["id"] for t in result["promoted_tokens"]]
    assert promoted_ids == ["tkn-py-mult-1", "tkn-py-mult-2", "tkn-py-mult-3"]

# TEST 10 — Concurrent promotion (1 available slot)
def test_10_concurrent_promotion_single_slot():
    conn = get_test_conn()
    conn.execute("UPDATE services SET max_capacity = 1 WHERE id = 'srv-lp';")
    conn.execute("DELETE FROM tokens WHERE service_id = 'srv-lp';")

    for i in range(1, 5):
        conn.execute("""
            INSERT INTO tokens (id, token_number, student_name, service_id, priority, status, created_at)
            VALUES (?, ?, ?, 'srv-lp', 'NORMAL', 'WAITLISTED', datetime('now', ?));
        """, (f"tkn-py-rc-{i}", f"LP-00{i}", f"Student {i}", f"-{10 - i} minutes"))

    def promote():
        c = get_test_conn()
        try:
            return queue_service.evaluate_and_promote_waitlist(c, 'srv-lp')
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(promote) for _ in range(4)]
        results = [f.result() for f in futures]

    total_promoted = sum(len(r["promoted_tokens"]) for r in results)
    assert total_promoted == 1

    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) as count FROM tokens WHERE service_id = 'srv-lp' AND status = 'WAITING';")
    assert cursor.fetchone()["count"] == 1

# TEST 11 — Concurrent promotion (multiple slots)
def test_11_concurrent_promotion_multiple_slots():
    conn = get_test_conn()
    conn.execute("UPDATE services SET max_capacity = 3 WHERE id = 'srv-lp';")
    conn.execute("DELETE FROM tokens WHERE service_id = 'srv-lp';")

    for i in range(1, 11):
        conn.execute("""
            INSERT INTO tokens (id, token_number, student_name, service_id, priority, status, created_at)
            VALUES (?, ?, ?, 'srv-lp', 'NORMAL', 'WAITLISTED', datetime('now', ?));
        """, (f"tkn-py-ten-{i}", f"LP-0{i}", f"Student {i}", f"-{20 - i} minutes"))

    def promote():
        c = get_test_conn()
        try:
            return queue_service.evaluate_and_promote_waitlist(c, 'srv-lp')
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(promote) for _ in range(5)]
        results = [f.result() for f in futures]

    all_promoted_ids = [t["id"] for r in results for t in r["promoted_tokens"]]
    assert len(set(all_promoted_ids)) == 3

    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) as count FROM tokens WHERE service_id = 'srv-lp' AND status = 'WAITING';")
    assert cursor.fetchone()["count"] == 3

# TEST 12 — Candidate invalidated concurrently
def test_12_candidate_invalidated_concurrently():
    conn = get_test_conn()
    conn.execute("UPDATE services SET max_capacity = 1 WHERE id = 'srv-lp';")
    conn.execute("DELETE FROM tokens WHERE service_id = 'srv-lp';")

    conn.execute("""
        INSERT INTO tokens (id, token_number, student_name, service_id, priority, status, created_at)
        VALUES 
          ('tkn-py-canc-race', 'LP-001', 'Cancel Race Student', 'srv-lp', 'URGENT', 'WAITLISTED', datetime('now', '-10 minutes')),
          ('tkn-py-next-race', 'LP-002', 'Next Student', 'srv-lp', 'NORMAL', 'WAITLISTED', datetime('now', '-5 minutes'));
    """)

    # Cancel top candidate first
    conn.execute("UPDATE tokens SET status = 'CANCELLED' WHERE id = 'tkn-py-canc-race';")

    result = queue_service.evaluate_and_promote_waitlist(conn, 'srv-lp')
    assert len(result["promoted_tokens"]) == 1
    assert result["promoted_tokens"][0]["id"] == "tkn-py-next-race"

# TEST 13 — Capacity protection
def test_13_capacity_protection():
    conn = get_test_conn()
    conn.execute("UPDATE services SET max_capacity = 2 WHERE id = 'srv-lp';")
    conn.execute("DELETE FROM tokens WHERE service_id = 'srv-lp';")

    t1 = student_service.book_token(conn, "usr-student-aarav", "Aarav", "aarav@test.edu", "srv-lp", "cntr-lp-2")
    t2 = student_service.book_token(conn, "usr-student-ananya", "Ananya", "ananya@test.edu", "srv-lp", "cntr-lp-2")
    t3 = student_service.book_token(conn, "usr-student-rohan", "Rohan", "rohan@test.edu", "srv-lp", "cntr-lp-2")
    t4 = student_service.book_token(conn, "usr-student-diya", "Diya", "diya@test.edu", "srv-lp", "cntr-lp-2")

    assert t1["status"] == "WAITING"
    assert t2["status"] == "WAITING"
    assert t3["status"] == "WAITLISTED"
    assert t4["status"] == "WAITLISTED"

    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) as count FROM tokens WHERE service_id = 'srv-lp' AND status IN ('WAITING', 'SERVING', 'HELD');")
    assert cursor.fetchone()["count"] == 2

# TEST 14 — Auto promotion on cancel
def test_14_auto_promotion_on_cancel():
    conn = get_test_conn()
    conn.execute("UPDATE services SET max_capacity = 1 WHERE id = 'srv-lp';")
    conn.execute("DELETE FROM tokens WHERE service_id = 'srv-lp';")

    t1 = student_service.book_token(conn, "usr-student-aarav", "Aarav", "aarav@test.edu", "srv-lp", "cntr-lp-2")
    t2 = student_service.book_token(conn, "usr-student-ananya", "Ananya", "ananya@test.edu", "srv-lp", "cntr-lp-2")

    assert t1["status"] == "WAITING"
    assert t2["status"] == "WAITLISTED"

    # Cancel active token t1
    student_service.cancel_token(conn, "usr-student-aarav", t1["id"])

    # t2 should now be WAITING
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM tokens WHERE id = ?;", (t2["id"],))
    assert cursor.fetchone()["status"] == "WAITING"
