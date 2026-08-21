import sqlite3
import os
from datetime import datetime, timezone
from fastapi import HTTPException, status
from app.services.student_service import calculate_estimated_wait

def get_waiting_queue(db: sqlite3.Connection, service_id: str) -> list[dict]:
    """
    Retrieves all WAITING tokens for a service, ordered by the canonical priority + FCFS rule.
    Canonical Ordering:
      1. Priority level descending ('URGENT' -> 'HIGH'/'PRIORITY' -> 'NORMAL')
      2. created_at ascending (FIFO)
      3. id ascending (lexicographical stable tie-breaker)
    """
    cursor = db.cursor()
    cursor.execute("""
        SELECT * FROM tokens
        WHERE service_id = ? AND status = 'WAITING'
        ORDER BY 
          CASE priority
            WHEN 'URGENT' THEN 3
            WHEN 'HIGH' THEN 2
            WHEN 'PRIORITY' THEN 2
            WHEN 'NORMAL' THEN 1
            ELSE 0
          END DESC,
          created_at ASC,
          id ASC;
    """, (service_id,))
    return [dict(row) for row in cursor.fetchall()]

def get_sorted_waitlist_tokens(db: sqlite3.Connection, service_id: str) -> list[dict]:
    """
    Retrieves all WAITLISTED tokens for a service, ordered by deterministic fairness policy:
    1. Effective priority (Base priority + starvation aging factor)
    2. created_at ascending (FIFO within effective priority)
    3. id ascending (stable tie-breaker)
    """
    cursor = db.cursor()
    cursor.execute("SELECT * FROM tokens WHERE service_id = ? AND status = 'WAITLISTED';", (service_id,))
    tokens = [dict(row) for row in cursor.fetchall()]
    
    threshold = float(os.environ.get("PRIORITY_WAIT_THRESHOLD_MINUTES", "15"))
    now = datetime.now(timezone.utc)

    def get_priority_val(p: str) -> int:
        if p == "URGENT": return 3
        elif p in ("HIGH", "PRIORITY"): return 2
        else: return 1

    def parse_dt(val: str) -> datetime:
        try:
            if "T" in val:
                return datetime.fromisoformat(val.replace("Z", "+00:00"))
            dt = datetime.strptime(val.split(".")[0], "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            return now

    def get_effective_priority(token: dict) -> int:
        created = parse_dt(token["created_at"])
        elapsed_mins = max(0.0, (now - created).total_seconds() / 60.0)
        base_val = get_priority_val(token.get("priority", "NORMAL"))
        boost = int(elapsed_mins // threshold)
        return base_val + boost

    def sort_key(t: dict):
        eff_p = get_effective_priority(t)
        created_ts = parse_dt(t["created_at"]).timestamp()
        return (-eff_p, created_ts, t["id"])

    tokens.sort(key=sort_key)
    return tokens

def evaluate_and_promote_waitlist(db: sqlite3.Connection, service_id: str) -> dict:
    """
    Evaluates waitlist and promotes eligible candidates to WAITING status
    when active capacity becomes available. Concurrency-safe & atomic.
    """
    cursor = db.cursor()
    try:
        db.execute("BEGIN IMMEDIATE;")
        cursor.execute("SELECT max_capacity FROM services WHERE id = ?;", (service_id,))
        service = cursor.fetchone()
        if not service:
            db.execute("COMMIT;")
            return {"promoted_tokens": [], "available_slots": 0}
        
        max_cap = service["max_capacity"] if service["max_capacity"] is not None else int(os.environ.get("MAX_QUEUE_CAPACITY", "10"))
        
        cursor.execute("""
            SELECT COUNT(*) as count FROM tokens
            WHERE service_id = ? AND status IN ('WAITING', 'SERVING', 'HELD');
        """, (service_id,))
        active_count = cursor.fetchone()["count"]
        available_slots = max(0, max_cap - active_count)
        
        if available_slots <= 0:
            db.execute("COMMIT;")
            return {"promoted_tokens": [], "available_slots": 0}
            
        candidates = get_sorted_waitlist_tokens(db, service_id)
        promoted = []
        
        for cand in candidates:
            if len(promoted) >= available_slots:
                break
                
            # Check duplicate active
            if cand.get("student_id"):
                cursor.execute("""
                    SELECT id FROM tokens
                    WHERE student_id = ? AND service_id = ? AND status IN ('WAITING', 'SERVING', 'HELD')
                    LIMIT 1;
                """, (cand["student_id"], service_id))
                if cursor.fetchone():
                    continue
                    
            cursor.execute("""
                UPDATE tokens
                SET status = 'WAITING'
                WHERE id = ? AND status = 'WAITLISTED';
            """, (cand["id"],))
            if cursor.rowcount > 0:
                cursor.execute("SELECT * FROM tokens WHERE id = ?;", (cand["id"],))
                promoted.append(dict(cursor.fetchone()))
                
        db.execute("COMMIT;")
        return {"promoted_tokens": promoted, "available_slots": available_slots}
    except Exception:
        try:
            db.execute("ROLLBACK;")
        except Exception:
            pass
        raise

def get_token_position_details(db: sqlite3.Connection, token_id: str) -> dict | None:
    """
    Computes a token's real-time queue position and estimates.
    Returns: {"position": int, "people_ahead": int, "estimated_wait_time": int}
    """
    cursor = db.cursor()
    cursor.execute("SELECT * FROM tokens WHERE id = ?;", (token_id,))
    row = cursor.fetchone()
    if not row:
        return None
        
    token = dict(row)
    if token["status"] == "SERVING":
        return {"position": 0, "people_ahead": 0, "estimated_wait_time": 0}

    if token["status"] == "WAITLISTED":
        sorted_waitlist = get_sorted_waitlist_tokens(db, token["service_id"])
        idx = -1
        for i, t in enumerate(sorted_waitlist):
            if t["id"] == token_id:
                idx = i
                break
        if idx == -1:
            return None
        return {
            "position": idx + 1,
            "people_ahead": idx,
            "estimated_wait_time": int((idx + 1) * 7.5)
        }
        
    if token["status"] != "WAITING":
        return {"position": -1, "people_ahead": 0, "estimated_wait_time": 0}
        
    sorted_queue = get_waiting_queue(db, token["service_id"])
    idx = -1
    for i, t in enumerate(sorted_queue):
        if t["id"] == token_id:
            idx = i
            break
            
    if idx == -1:
        return None
        
    people_ahead = idx
    position = idx + 1
    return {
        "position": position,
        "people_ahead": people_ahead,
        "estimated_wait_time": calculate_estimated_wait(people_ahead)
    }

def call_next_token(db: sqlite3.Connection, counter_id: str, service_id: str) -> dict:
    """
    Enforces active serving checks, picks the next waiting token, and assigns it to counter_id.
    Runs inside a strict SQLite BEGIN IMMEDIATE transaction lock.
    """
    cursor = db.cursor()
    try:
        db.execute("BEGIN IMMEDIATE;")
        
        # 1. Verify counter status is OPEN
        cursor.execute("SELECT status FROM counters WHERE id = ?;", (counter_id,))
        counter = cursor.fetchone()
        if not counter:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Counter not found")
        if counter["status"] != "OPEN":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot call next token: Counter is currently {counter['status']}"
            )
            
        # 2. Assert no token is currently SERVING at this counter
        cursor.execute("SELECT * FROM tokens WHERE counter_id = ? AND status = 'SERVING';", (counter_id,))
        active_serving = cursor.fetchone()
        if active_serving:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Counter already has active serving token {active_serving['token_number']}. Complete, hold, or skip it first."
            )
            
        # 3. Pull next eligible token candidates
        waiting = get_waiting_queue(db, service_id)
        if not waiting:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Waiting queue is currently empty for this service."
            )
        
        # 4. Atomically claim the top eligible waiting token
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')
        claimed_token_id = None
        for candidate in waiting:
            cursor.execute("""
                UPDATE tokens
                SET status = 'SERVING', counter_id = ?, started_at = ?
                WHERE id = ? AND status = 'WAITING';
            """, (counter_id, now, candidate["id"]))
            if cursor.rowcount > 0:
                claimed_token_id = candidate["id"]
                break

        if not claimed_token_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Waiting queue is currently empty for this service."
            )
        
        db.execute("COMMIT;")
        
        # Get updated token
        cursor.execute("""
            SELECT t.*, s.name as service_name, c.name as counter_name
            FROM tokens t
            JOIN services s ON t.service_id = s.id
            JOIN counters c ON t.counter_id = c.id
            WHERE t.id = ?;
        """, (claimed_token_id,))
        return dict(cursor.fetchone())
        
    except HTTPException:
        try:
            db.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    except sqlite3.Error as e:
        try:
            db.execute("ROLLBACK;")
        except Exception:
            pass
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database transaction error: {str(e)}"
        )

def complete_token(db: sqlite3.Connection, token_id: str, counter_id: str) -> dict:
    """
    Completes a serving token and automatically evaluates waitlist promotions.
    """
    cursor = db.cursor()
    service_id_to_promote = None
    try:
        db.execute("BEGIN IMMEDIATE;")
        cursor.execute("SELECT * FROM tokens WHERE id = ?;", (token_id,))
        token_row = cursor.fetchone()
        if not token_row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
        token = dict(token_row)
        
        if token["status"] != "SERVING":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid state transition: Cannot complete token with status '{token['status']}'. Must be 'SERVING'."
            )
            
        if token["counter_id"] != counter_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Unauthorized: Token is assigned to a different counter"
            )
            
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')
        cursor.execute("""
            UPDATE tokens
            SET status = 'COMPLETED', completed_at = ?
            WHERE id = ? AND status = 'SERVING' AND counter_id = ?;
        """, (now, token_id, counter_id))
        
        if cursor.rowcount == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="State transition failed: Token is no longer in SERVING status for this counter."
            )
            
        service_id_to_promote = token["service_id"]
        db.execute("COMMIT;")
        
        # Auto-promote waitlist
        if service_id_to_promote:
            evaluate_and_promote_waitlist(db, service_id_to_promote)
        
        cursor.execute("""
            SELECT t.*, s.name as service_name, c.name as counter_name
            FROM tokens t
            JOIN services s ON t.service_id = s.id
            JOIN counters c ON t.counter_id = c.id
            WHERE t.id = ?;
        """, (token_id,))
        return dict(cursor.fetchone())
        
    except HTTPException:
        try:
            db.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    except sqlite3.Error as e:
        try:
            db.execute("ROLLBACK;")
        except Exception:
            pass
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database transaction error: {str(e)}"
        )

def hold_token(db: sqlite3.Connection, token_id: str, counter_id: str) -> dict:
    """
    Places a serving token on HELD.
    """
    cursor = db.cursor()
    try:
        db.execute("BEGIN IMMEDIATE;")
        cursor.execute("SELECT * FROM tokens WHERE id = ?;", (token_id,))
        token_row = cursor.fetchone()
        if not token_row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
        token = dict(token_row)
        
        if token["status"] != "SERVING":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid state transition: Cannot hold token with status '{token['status']}'. Must be 'SERVING'."
            )
            
        if token["counter_id"] != counter_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Unauthorized: Token is assigned to a different counter"
            )
            
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')
        cursor.execute("""
            UPDATE tokens
            SET status = 'HELD', held_at = ?
            WHERE id = ? AND status = 'SERVING' AND counter_id = ?;
        """, (now, token_id, counter_id))
        
        if cursor.rowcount == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="State transition failed: Token is no longer in SERVING status for this counter."
            )
            
        db.execute("COMMIT;")
        
        cursor.execute("""
            SELECT t.*, s.name as service_name, c.name as counter_name
            FROM tokens t
            JOIN services s ON t.service_id = s.id
            JOIN counters c ON t.counter_id = c.id
            WHERE t.id = ?;
        """, (token_id,))
        return dict(cursor.fetchone())
        
    except HTTPException:
        try:
            db.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    except sqlite3.Error as e:
        try:
            db.execute("ROLLBACK;")
        except Exception:
            pass
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database transaction error: {str(e)}"
        )

def resume_token(db: sqlite3.Connection, token_id: str, counter_id: str) -> dict:
    """
    Resumes a held token back to SERVING.
    """
    cursor = db.cursor()
    try:
        db.execute("BEGIN IMMEDIATE;")
        cursor.execute("SELECT * FROM tokens WHERE id = ?;", (token_id,))
        token_row = cursor.fetchone()
        if not token_row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
        token = dict(token_row)
        
        if token["status"] != "HELD":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid state transition: Cannot resume token with status '{token['status']}'. Must be 'HELD'."
            )
            
        # Assert no token is currently SERVING at this counter
        cursor.execute("SELECT * FROM tokens WHERE counter_id = ? AND status = 'SERVING';", (counter_id,))
        active_serving = cursor.fetchone()
        if active_serving:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot resume token: Counter already has active serving token {active_serving['token_number']}."
            )
            
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')
        cursor.execute("""
            UPDATE tokens
            SET status = 'SERVING', counter_id = ?, started_at = ?
            WHERE id = ? AND status = 'HELD';
        """, (counter_id, now, token_id))
        
        if cursor.rowcount == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="State transition failed: Token is no longer in HELD status."
            )
            
        db.execute("COMMIT;")
        
        cursor.execute("""
            SELECT t.*, s.name as service_name, c.name as counter_name
            FROM tokens t
            JOIN services s ON t.service_id = s.id
            JOIN counters c ON t.counter_id = c.id
            WHERE t.id = ?;
        """, (token_id,))
        return dict(cursor.fetchone())
        
    except HTTPException:
        try:
            db.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    except sqlite3.Error as e:
        try:
            db.execute("ROLLBACK;")
        except Exception:
            pass
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database transaction error: {str(e)}"
        )

def skip_token(db: sqlite3.Connection, token_id: str, counter_id: str) -> dict:
    """
    Skips a waiting, serving, or held token to state SKIPPED and evaluates waitlist.
    """
    cursor = db.cursor()
    service_id_to_promote = None
    try:
        db.execute("BEGIN IMMEDIATE;")
        cursor.execute("SELECT * FROM tokens WHERE id = ?;", (token_id,))
        token_row = cursor.fetchone()
        if not token_row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
        token = dict(token_row)
        
        if token["status"] not in ("WAITING", "SERVING", "HELD"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid state transition: Cannot skip token with status '{token['status']}'."
            )
            
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')
        cursor.execute("""
            UPDATE tokens
            SET status = 'SKIPPED', skipped_at = ?
            WHERE id = ? AND status IN ('WAITING', 'SERVING', 'HELD');
        """, (now, token_id))
        
        if cursor.rowcount == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid state transition: Cannot skip token with status '{token['status']}'."
            )
            
        service_id_to_promote = token["service_id"]
        db.execute("COMMIT;")
        
        # Auto-promote waitlist
        if service_id_to_promote:
            evaluate_and_promote_waitlist(db, service_id_to_promote)
        
        cursor.execute("""
            SELECT t.*, s.name as service_name, c.name as counter_name
            FROM tokens t
            JOIN services s ON t.service_id = s.id
            JOIN counters c ON t.counter_id = c.id
            WHERE t.id = ?;
        """, (token_id,))
        return dict(cursor.fetchone())
        
    except HTTPException:
        try:
            db.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    except sqlite3.Error as e:
        try:
            db.execute("ROLLBACK;")
        except Exception:
            pass
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database transaction error: {str(e)}"
        )
