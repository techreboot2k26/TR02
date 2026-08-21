import sqlite3
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
    Marks a serving token as COMPLETED.
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
    Skips a waiting, serving, or held token to state SKIPPED.
    """
    cursor = db.cursor()
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
