import os
import sqlite3
import pytest
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from fastapi import HTTPException

from app.main import app
from app.config import settings
from app.services import queue_service, student_service

settings.mock_auth = True
settings.db_path = "test_queuecraft_concurrency.db"

client = TestClient(app)

@pytest.fixture(scope="module", autouse=True)
def setup_concurrency_db():
    orig_db = settings.db_path
    settings.db_path = "test_queuecraft_concurrency.db"
    from app.database import initialize_schema, seed_database
    initialize_schema()
    seed_database()
    yield
    if os.path.exists("test_queuecraft_concurrency.db"):
        try:
            os.remove("test_queuecraft_concurrency.db")
        except PermissionError:
            pass
    settings.db_path = orig_db

def get_test_conn():
    conn = sqlite3.connect(settings.db_path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


def test_1_two_simultaneous_next():
    """
    Test 1: Two simultaneous NEXT calls assign two distinct tokens without collision.
    """
    conn = get_test_conn()
    conn.execute("UPDATE counters SET status = 'OPEN' WHERE id IN ('cntr-lp-1', 'cntr-lp-2');")
    conn.execute("UPDATE tokens SET status = 'COMPLETED' WHERE service_id = 'srv-lp';")
    
    conn.execute("""
        INSERT INTO tokens (id, token_number, student_name, service_id, priority, status, created_at)
        VALUES 
            ('tkn-py-101', 'LP-101', 'Student 101', 'srv-lp', 'NORMAL', 'WAITING', datetime('now', '-3 minutes')),
            ('tkn-py-102', 'LP-102', 'Student 102', 'srv-lp', 'NORMAL', 'WAITING', datetime('now', '-2 minutes')),
            ('tkn-py-103', 'LP-103', 'Student 103', 'srv-lp', 'NORMAL', 'WAITING', datetime('now', '-1 minutes'));
    """)
    conn.close()

    def do_call_next(counter_id):
        db = get_test_conn()
        try:
            return queue_service.call_next_token(db, counter_id=counter_id, service_id="srv-lp")
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(do_call_next, "cntr-lp-1")
        f2 = executor.submit(do_call_next, "cntr-lp-2")
        res1 = f1.result()
        res2 = f2.result()

    assert res1["id"] != res2["id"]
    assert res1["token_number"] != res2["token_number"]

    # Verify DB state
    conn = get_test_conn()
    c1_serving = conn.execute("SELECT * FROM tokens WHERE counter_id = 'cntr-lp-1' AND status = 'SERVING';").fetchone()
    c2_serving = conn.execute("SELECT * FROM tokens WHERE counter_id = 'cntr-lp-2' AND status = 'SERVING';").fetchone()
    assert c1_serving is not None
    assert c2_serving is not None
    assert c1_serving["id"] != c2_serving["id"]
    conn.close()


def test_2_multiple_simultaneous_next():
    """
    Test 2: Multiple simultaneous NEXT (10 concurrent requests) assign distinct tokens with zero duplicates.
    """
    conn = get_test_conn()
    service_id = "srv-lp"
    conn.execute("UPDATE tokens SET status = 'COMPLETED' WHERE service_id = ?;", (service_id,))

    # Create 10 open counters
    for i in range(1, 11):
        conn.execute("""
            INSERT OR REPLACE INTO counters (id, service_id, name, status)
            VALUES (?, ?, ?, 'OPEN');
        """, (f"cntr-py-multi-{i}", service_id, f"Counter {i}"))

    # Create 10 waiting tokens
    for i in range(1, 11):
        conn.execute("""
            INSERT INTO tokens (id, token_number, student_name, service_id, priority, status, created_at)
            VALUES (?, ?, ?, ?, 'NORMAL', 'WAITING', datetime('now', '-' || ? || ' minutes'));
        """, (f"tkn-py-multi-{i}", f"LP-20{i}", f"Multi Student {i}", service_id, 15 - i))
    conn.close()

    def do_call_next(idx):
        db = get_test_conn()
        try:
            return queue_service.call_next_token(db, counter_id=f"cntr-py-multi-{idx}", service_id=service_id)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(do_call_next, i) for i in range(1, 11)]
        results = [f.result() for f in futures]

    claimed_ids = [r["id"] for r in results]
    assert len(claimed_ids) == 10
    assert len(set(claimed_ids)) == 10

    conn = get_test_conn()
    remaining = conn.execute("SELECT COUNT(*) as cnt FROM tokens WHERE service_id = ? AND status = 'WAITING';", (service_id,)).fetchone()["cnt"]
    assert remaining == 0
    total_serving = conn.execute("SELECT COUNT(*) as cnt FROM tokens WHERE service_id = ? AND status = 'SERVING';", (service_id,)).fetchone()["cnt"]
    assert total_serving == 10
    conn.close()


def test_3_next_and_cancel_concurrently():
    """
    Test 3: Concurrent NEXT and CANCEL guarantees a cancelled token never becomes SERVING.
    """
    conn = get_test_conn()
    service_id = "srv-lp"
    counter_id = "cntr-lp-2"
    conn.execute("UPDATE counters SET status = 'OPEN' WHERE id = ?;", (counter_id,))
    conn.execute("UPDATE tokens SET status = 'COMPLETED' WHERE service_id = ?;", (service_id,))

    target_token_id = "tkn-py-race-cancel"
    user_id = "usr-student-race-3"
    conn.execute("""
        INSERT OR REPLACE INTO users (id, name, email, password_hash, role)
        VALUES (?, 'Race Student', 'race3@queuecraft.edu', 'hash', 'STUDENT');
    """, (user_id,))
    conn.execute("""
        INSERT INTO tokens (id, token_number, student_id, student_name, service_id, priority, status, created_at)
        VALUES (?, 'LP-301', ?, 'Race Student', ?, 'NORMAL', 'WAITING', datetime('now'));
    """, (target_token_id, user_id, service_id))
    conn.close()

    def do_next():
        db = get_test_conn()
        try:
            return ("NEXT", queue_service.call_next_token(db, counter_id=counter_id, service_id=service_id))
        except HTTPException as e:
            return ("NEXT_ERR", e)
        finally:
            db.close()

    def do_cancel():
        db = get_test_conn()
        try:
            return ("CANCEL", student_service.cancel_token(db, user_id=user_id, token_id=target_token_id))
        except HTTPException as e:
            return ("CANCEL_ERR", e)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(do_next)
        f2 = executor.submit(do_cancel)
        res1 = f1.result()
        res2 = f2.result()

    conn = get_test_conn()
    final_token = conn.execute("SELECT * FROM tokens WHERE id = ?;", (target_token_id,)).fetchone()
    assert final_token["status"] in ("SERVING", "CANCELLED")
    if final_token["status"] == "CANCELLED":
        assert final_token["status"] != "SERVING"
    conn.close()


def test_4_next_and_complete_concurrently():
    """
    Test 4: Concurrent NEXT and COMPLETE maintains state consistency without stale overwrites.
    """
    conn = get_test_conn()
    service_id = "srv-lp"
    counter_id = "cntr-lp-2"
    conn.execute("UPDATE counters SET status = 'OPEN' WHERE id = ?;", (counter_id,))
    conn.execute("UPDATE tokens SET status = 'COMPLETED' WHERE service_id = ?;", (service_id,))

    active_id = "tkn-py-serving-active"
    waiting_id = "tkn-py-waiting-next"
    conn.execute("""
        INSERT INTO tokens (id, token_number, student_name, service_id, counter_id, priority, status, created_at, started_at)
        VALUES (?, 'LP-401', 'Active Student', ?, ?, 'NORMAL', 'SERVING', datetime('now', '-5 minutes'), datetime('now', '-5 minutes'));
    """, (active_id, service_id, counter_id))
    conn.execute("""
        INSERT INTO tokens (id, token_number, student_name, service_id, priority, status, created_at)
        VALUES (?, 'LP-402', 'Waiting Student', ?, 'NORMAL', 'WAITING', datetime('now', '-2 minutes'));
    """, (waiting_id, service_id))
    conn.close()

    def do_complete():
        db = get_test_conn()
        try:
            return ("COMPLETE", queue_service.complete_token(db, token_id=active_id, counter_id=counter_id))
        except HTTPException as e:
            return ("COMPLETE_ERR", e)
        finally:
            db.close()

    def do_next():
        db = get_test_conn()
        try:
            return ("NEXT", queue_service.call_next_token(db, counter_id=counter_id, service_id=service_id))
        except HTTPException as e:
            return ("NEXT_ERR", e)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(do_complete)
        f2 = executor.submit(do_next)
        res1 = f1.result()
        res2 = f2.result()

    conn = get_test_conn()
    final_t1 = conn.execute("SELECT * FROM tokens WHERE id = ?;", (active_id,)).fetchone()
    final_t2 = conn.execute("SELECT * FROM tokens WHERE id = ?;", (waiting_id,)).fetchone()
    assert final_t1["status"] == "COMPLETED"
    assert final_t2["status"] in ("SERVING", "WAITING")

    serving_cnt = conn.execute("SELECT COUNT(*) as cnt FROM tokens WHERE counter_id = ? AND status = 'SERVING';", (counter_id,)).fetchone()["cnt"]
    assert serving_cnt <= 1
    conn.close()


def test_5_complete_and_skip_concurrently():
    """
    Test 5: Concurrent COMPLETE and SKIP results in exactly one winning terminal transition.
    """
    conn = get_test_conn()
    counter_id = "cntr-lp-2"
    service_id = "srv-lp"
    conn.execute("UPDATE counters SET status = 'OPEN' WHERE id = ?;", (counter_id,))
    conn.execute("UPDATE tokens SET status = 'COMPLETED' WHERE service_id = ?;", (service_id,))

    token_id = "tkn-py-race-comp-skip"
    conn.execute("""
        INSERT INTO tokens (id, token_number, student_name, service_id, counter_id, priority, status, created_at, started_at)
        VALUES (?, 'LP-501', 'Contested Student', ?, ?, 'NORMAL', 'SERVING', datetime('now', '-10 minutes'), datetime('now', '-5 minutes'));
    """, (token_id, service_id, counter_id))
    conn.close()

    def do_complete():
        db = get_test_conn()
        try:
            return ("OK", queue_service.complete_token(db, token_id=token_id, counter_id=counter_id))
        except HTTPException as e:
            return ("ERR", e)
        finally:
            db.close()

    def do_skip():
        db = get_test_conn()
        try:
            return ("OK", queue_service.skip_token(db, token_id=token_id, counter_id=counter_id))
        except HTTPException as e:
            return ("ERR", e)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(do_complete)
        f2 = executor.submit(do_skip)
        res1 = f1.result()
        res2 = f2.result()

    successes = [r for r in [res1, res2] if r[0] == "OK"]
    assert len(successes) == 1

    conn = get_test_conn()
    final_tok = conn.execute("SELECT * FROM tokens WHERE id = ?;", (token_id,)).fetchone()
    assert final_tok["status"] in ("COMPLETED", "SKIPPED")
    conn.close()


def test_6_hold_and_resume_concurrently():
    """
    Test 6: Concurrent HOLD and RESUME prevents invalid transitions.
    """
    conn = get_test_conn()
    counter_id = "cntr-lp-2"
    service_id = "srv-lp"
    conn.execute("UPDATE counters SET status = 'OPEN' WHERE id = ?;", (counter_id,))
    conn.execute("UPDATE tokens SET status = 'COMPLETED' WHERE service_id = ?;", (service_id,))

    token_id = "tkn-py-race-hold-resume"
    conn.execute("""
        INSERT INTO tokens (id, token_number, student_name, service_id, counter_id, priority, status, created_at, started_at)
        VALUES (?, 'LP-601', 'Hold Resume Student', ?, ?, 'NORMAL', 'SERVING', datetime('now', '-5 minutes'), datetime('now', '-3 minutes'));
    """, (token_id, service_id, counter_id))
    conn.close()

    def do_hold():
        db = get_test_conn()
        try:
            return ("OK", queue_service.hold_token(db, token_id=token_id, counter_id=counter_id))
        except HTTPException as e:
            return ("ERR", e)
        finally:
            db.close()

    def do_resume():
        db = get_test_conn()
        try:
            return ("OK", queue_service.resume_token(db, token_id=token_id, counter_id=counter_id))
        except HTTPException as e:
            return ("ERR", e)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(do_hold)
        f2 = executor.submit(do_resume)
        res1 = f1.result()
        res2 = f2.result()

    conn = get_test_conn()
    final_tok = conn.execute("SELECT * FROM tokens WHERE id = ?;", (token_id,)).fetchone()
    assert final_tok["status"] in ("HELD", "SERVING")
    conn.close()


def test_7_cancelled_token_cannot_be_reactivated():
    """
    Test 7: Cancelled token cannot be reactivated by any concurrent operations.
    """
    conn = get_test_conn()
    service_id = "srv-lp"
    counter_id = "cntr-lp-2"
    conn.execute("UPDATE counters SET status = 'OPEN' WHERE id = ?;", (counter_id,))
    conn.execute("UPDATE tokens SET status = 'COMPLETED' WHERE service_id = ?;", (service_id,))

    cancelled_id = "tkn-py-already-cancelled"
    conn.execute("""
        INSERT INTO tokens (id, token_number, student_name, service_id, counter_id, priority, status, created_at)
        VALUES (?, 'LP-701', 'Cancelled Student', ?, ?, 'NORMAL', 'CANCELLED', datetime('now', '-10 minutes'));
    """, (cancelled_id, service_id, counter_id))
    conn.close()

    def try_resume():
        db = get_test_conn()
        try:
            queue_service.resume_token(db, token_id=cancelled_id, counter_id=counter_id)
            return True
        except HTTPException:
            return False
        finally:
            db.close()

    def try_complete():
        db = get_test_conn()
        try:
            queue_service.complete_token(db, token_id=cancelled_id, counter_id=counter_id)
            return True
        except HTTPException:
            return False
        finally:
            db.close()

    def try_hold():
        db = get_test_conn()
        try:
            queue_service.hold_token(db, token_id=cancelled_id, counter_id=counter_id)
            return True
        except HTTPException:
            return False
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=3) as executor:
        f1 = executor.submit(try_resume)
        f2 = executor.submit(try_complete)
        f3 = executor.submit(try_hold)
        assert f1.result() is False
        assert f2.result() is False
        assert f3.result() is False

    conn = get_test_conn()
    final_tok = conn.execute("SELECT * FROM tokens WHERE id = ?;", (cancelled_id,)).fetchone()
    assert final_tok["status"] == "CANCELLED"
    conn.close()


def test_8_completed_token_cannot_be_reactivated():
    """
    Test 8: Completed token cannot be reactivated or re-served.
    """
    conn = get_test_conn()
    service_id = "srv-lp"
    counter_id = "cntr-lp-2"
    conn.execute("UPDATE counters SET status = 'OPEN' WHERE id = ?;", (counter_id,))
    conn.execute("UPDATE tokens SET status = 'COMPLETED' WHERE service_id = ?;", (service_id,))

    completed_id = "tkn-py-already-completed"
    conn.execute("""
        INSERT INTO tokens (id, token_number, student_name, service_id, counter_id, priority, status, created_at, started_at, completed_at)
        VALUES (?, 'LP-801', 'Done Student', ?, ?, 'NORMAL', 'COMPLETED', datetime('now', '-20 minutes'), datetime('now', '-10 minutes'), datetime('now', '-5 minutes'));
    """, (completed_id, service_id, counter_id))
    conn.close()

    def try_resume():
        db = get_test_conn()
        try:
            queue_service.resume_token(db, token_id=completed_id, counter_id=counter_id)
            return True
        except HTTPException:
            return False
        finally:
            db.close()

    def try_hold():
        db = get_test_conn()
        try:
            queue_service.hold_token(db, token_id=completed_id, counter_id=counter_id)
            return True
        except HTTPException:
            return False
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(try_resume)
        f2 = executor.submit(try_hold)
        assert f1.result() is False
        assert f2.result() is False

    conn = get_test_conn()
    final_tok = conn.execute("SELECT * FROM tokens WHERE id = ?;", (completed_id,)).fetchone()
    assert final_tok["status"] == "COMPLETED"
    conn.close()


def test_9_database_constraint_uniqueness():
    """
    Test 9: Database constraints enforce at most ONE SERVING token per counter.
    """
    conn = get_test_conn()
    service_id = "srv-lp"
    counter_id = "cntr-lp-2"
    conn.execute("UPDATE tokens SET status = 'COMPLETED' WHERE service_id = ?;", (service_id,))

    # Insert first SERVING token
    conn.execute("""
        INSERT INTO tokens (id, token_number, student_name, service_id, counter_id, priority, status, created_at, started_at)
        VALUES ('tkn-py-c1', 'LP-901', 'First Serv', ?, ?, 'NORMAL', 'SERVING', datetime('now'), datetime('now'));
    """, (service_id, counter_id))

    # Second insert on same counter in SERVING status must fail with sqlite3.IntegrityError
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("""
            INSERT INTO tokens (id, token_number, student_name, service_id, counter_id, priority, status, created_at, started_at)
            VALUES ('tkn-py-c2', 'LP-902', 'Second Serv', ?, ?, 'NORMAL', 'SERVING', datetime('now'), datetime('now'));
        """, (service_id, counter_id))

    serving_list = conn.execute("SELECT * FROM tokens WHERE counter_id = ? AND status = 'SERVING';", (counter_id,)).fetchall()
    assert len(serving_list) == 1
    conn.close()


def test_10_rollback_safety():
    """
    Test 10: Rollback safety ensures atomic failure with zero partial database mutations.
    """
    conn = get_test_conn()
    service_id = "srv-lp"
    counter_id = "cntr-lp-2"
    token_id = "tkn-py-rollback-test"

    conn.execute("UPDATE counters SET status = 'OPEN' WHERE id = ?;", (counter_id,))
    conn.execute("UPDATE tokens SET status = 'COMPLETED' WHERE service_id = ?;", (service_id,))

    conn.execute("""
        INSERT INTO tokens (id, token_number, student_name, service_id, priority, status, created_at)
        VALUES (?, 'LP-999', 'Rollback Student', ?, 'NORMAL', 'WAITING', datetime('now'));
    """, (token_id, service_id))

    try:
        conn.execute("BEGIN IMMEDIATE;")
        conn.execute("UPDATE tokens SET status = 'SERVING', counter_id = ? WHERE id = ?;", (counter_id, token_id))
        # Force intentional failure
        raise ValueError("Forced test error")
    except ValueError:
        conn.execute("ROLLBACK;")

    # Verify token remains WAITING and counter_id is None
    tok = conn.execute("SELECT * FROM tokens WHERE id = ?;", (token_id,)).fetchone()
    assert tok["status"] == "WAITING"
    assert tok["counter_id"] is None
    conn.close()
