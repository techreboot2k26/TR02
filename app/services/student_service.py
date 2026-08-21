import sqlite3
import uuid
from datetime import datetime, timezone
from fastapi import HTTPException, status

def calculate_estimated_wait(people_ahead: int) -> int:
    """
    Calculates estimated wait time in minutes.
    Currently uses 5 minutes per person ahead as a prototype placeholder.
    """
    return people_ahead * 5

def book_token(
    db: sqlite3.Connection,
    user_id: str,
    user_name: str,
    user_email: str,
    service_id: str,
    counter_id: str
) -> dict:
    """
    Atomically books a new queue token for a service and counter.
    Runs inside a strict SQLite BEGIN IMMEDIATE transaction boundary.
    """
    cursor = db.cursor()
    try:
        # Enforce write lock immediately to prevent sequence and active booking races
        db.execute("BEGIN IMMEDIATE;")

        # 1. Verify service exists
        cursor.execute("SELECT name, code FROM services WHERE id = ?;", (service_id,))
        service = cursor.fetchone()
        if not service:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Service not found"
            )

        # 2. Verify counter exists and is operational
        cursor.execute("SELECT status, name FROM counters WHERE id = ? AND service_id = ?;", (counter_id, service_id))
        counter = cursor.fetchone()
        if not counter:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Counter not found for this service"
            )
        
        if counter["status"] in ("CLOSED", "MAINTENANCE"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Counter is currently not accepting new tokens"
            )

        # 3. Check for any existing active token (global check matching Express Reference)
        cursor.execute("""
            SELECT id FROM tokens 
            WHERE student_id = ? AND status IN ('WAITING', 'SERVING', 'HELD');
        """, (user_id,))
        active_token = cursor.fetchone()
        if active_token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You already have an active token. Complete or cancel it first."
            )

        # 4. Generate unique sequential token number (e.g. LP-042)
        cursor.execute("""
            SELECT COUNT(*) as count 
            FROM tokens 
            WHERE service_id = ? AND date(created_at) = date('now');
        """, (service_id,))
        count = cursor.fetchone()["count"]
        
        seq_num = str(count + 1).zfill(3)
        token_number = f"{service['code']}-{seq_num}"
        token_id = str(uuid.uuid4())

        # 5. Insert new token with high-precision timestamp to avoid same-second queue collisions in SQLite
        created_at_val = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')
        cursor.execute("""
            INSERT INTO tokens (id, token_number, student_id, student_name, student_email, service_id, counter_id, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'WAITING', ?);
        """, (token_id, token_number, user_id, user_name, user_email, service_id, counter_id, created_at_val))

        db.execute("COMMIT;")

        # 6. Retrieve complete token details (including names) for response payload
        cursor.execute("""
            SELECT t.*, s.name as service_name, c.name as counter_name
            FROM tokens t
            JOIN services s ON t.service_id = s.id
            JOIN counters c ON t.counter_id = c.id
            WHERE t.id = ?;
        """, (token_id,))
        new_token = dict(cursor.fetchone())
        
        # Default wait stats for new waiting token
        new_token["people_ahead"] = 0
        new_token["estimated_wait_time"] = 0
        
        return new_token

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

def get_active_token(db: sqlite3.Connection, user_id: str) -> dict | None:
    """
    Retrieves the current active token (WAITING, SERVING, HELD) for a student,
    including real-time queue position and wait estimates.
    """
    cursor = db.cursor()
    try:
        # Get active token
        cursor.execute("""
            SELECT t.*, s.name as service_name, c.name as counter_name
            FROM tokens t
            JOIN services s ON t.service_id = s.id
            JOIN counters c ON t.counter_id = c.id
            WHERE t.student_id = ? AND t.status IN ('WAITING', 'SERVING', 'HELD')
            ORDER BY t.created_at DESC
            LIMIT 1;
        """, (user_id,))
        row = cursor.fetchone()
        if not row:
            return None
            
        token = dict(row)
        
        # Calculate queue stats using unified queue engine logic
        from app.services import queue_service
        details = queue_service.get_token_position_details(db, token["id"])
        if details:
            token["people_ahead"] = details["people_ahead"]
            token["estimated_wait_time"] = details["estimated_wait_time"]
        else:
            token["people_ahead"] = 0
            token["estimated_wait_time"] = 0
            
        return token
        
    except sqlite3.Error as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database query error: {str(e)}"
        )

def get_token_history(db: sqlite3.Connection, user_id: str) -> list:
    """
    Retrieves past completed, skipped, or cancelled tokens for a student.
    """
    cursor = db.cursor()
    try:
        cursor.execute("""
            SELECT t.*, s.name as service_name, c.name as counter_name
            FROM tokens t
            JOIN services s ON t.service_id = s.id
            JOIN counters c ON t.counter_id = c.id
            WHERE t.student_id = ? AND t.status IN ('COMPLETED', 'CANCELLED', 'SKIPPED')
            ORDER BY t.created_at DESC;
        """, (user_id,))
        return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database query error: {str(e)}"
        )

def cancel_token(db: sqlite3.Connection, user_id: str, token_id: str) -> dict:
    """
    Cancels a student's active waiting or held token.
    Enforces ownership and state transitions inside an immediate transaction.
    """
    cursor = db.cursor()
    try:
        db.execute("BEGIN IMMEDIATE;")
        # Fetch token to verify existence and check details
        cursor.execute("""
            SELECT student_id, status, counter_id, service_id, token_number
            FROM tokens 
            WHERE id = ?;
        """, (token_id,))
        token = cursor.fetchone()
        
        if not token:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Token not found"
            )
            
        # Verify ownership
        if token["student_id"] != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: You do not own this token"
            )
            
        # Verify state transition
        if token["status"] not in ("WAITING", "HELD"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot cancel token with status: {token['status']}"
            )
            
        # Update token state to CANCELLED
        cursor.execute("""
            UPDATE tokens 
            SET status = 'CANCELLED', completed_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status IN ('WAITING', 'HELD');
        """, (token_id,))
        
        if cursor.rowcount == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to cancel token: State changed concurrently"
            )
            
        db.execute("COMMIT;")
        
        return {
            "success": True,
            "message": "Token cancelled successfully",
            "token": {
                "id": token_id,
                "token_number": token["token_number"],
                "service_id": token["service_id"],
                "counter_id": token["counter_id"]
            }
        }
        
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
            detail=f"Database mutation error: {str(e)}"
        )
