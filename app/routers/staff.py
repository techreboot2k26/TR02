import sqlite3
from fastapi import APIRouter, Depends, HTTPException, status
from typing import List, Optional

from app.database import get_db
from app.dependencies import get_assigned_counter, require_staff
from app.models.schemas import TokenResponseDetail
from app.models.staff_schemas import (
    StaffDashboardResponse,
    StaffActionResponse,
    CounterStatusUpdateRequest,
    CounterStatusUpdateResponse
)
from app.services import queue_service

router = APIRouter()

def get_dashboard_data(db: sqlite3.Connection, counter: dict) -> dict:
    """
    Builds the consolidated staff dashboard response payload matching Express `getDashboardData`.
    """
    cursor = db.cursor()
    
    # 1. Fetch service details
    cursor.execute("SELECT id, name, code, description FROM services WHERE id = ?;", (counter["service_id"],))
    service_row = cursor.fetchone()
    if not service_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Service not found for this counter"
        )
    service = dict(service_row)

    # 2. Fetch staff member name
    cursor.execute("SELECT name FROM users WHERE id = ?;", (counter["assigned_staff_id"],))
    staff_row = cursor.fetchone()
    staff_name = staff_row["name"] if staff_row else "Staff Member"

    # 3. Get currently serving token
    cursor.execute("""
        SELECT t.*, s.name as service_name, c.name as counter_name
        FROM tokens t
        JOIN services s ON t.service_id = s.id
        LEFT JOIN counters c ON t.counter_id = c.id
        WHERE t.counter_id = ? AND t.status = 'SERVING'
        LIMIT 1;
    """, (counter["id"],))
    current_row = cursor.fetchone()
    current_token = dict(current_row) if current_row else None

    # 4. Get waiting queue tokens
    waiting_queue = queue_service.get_waiting_queue(db, counter["service_id"])
    
    # 5. Populate waiting queue with Position Details (including service/counter names)
    waiting_queue_details = []
    for t in waiting_queue:
        cursor.execute("""
            SELECT t.*, s.name as service_name, c.name as counter_name
            FROM tokens t
            JOIN services s ON t.service_id = s.id
            LEFT JOIN counters c ON t.counter_id = c.id
            WHERE t.id = ?;
        """, (t["id"],))
        row = cursor.fetchone()
        if row:
            token_detail = dict(row)
            pos_details = queue_service.get_token_position_details(db, t["id"])
            if pos_details:
                token_detail.update(pos_details)
            waiting_queue_details.append(token_detail)

    # 6. Operational Stats
    # held count
    cursor.execute("SELECT COUNT(*) as count FROM tokens WHERE service_id = ? AND status = 'HELD';", (counter["service_id"],))
    held_count = cursor.fetchone()["count"]

    # completed today count
    cursor.execute("""
        SELECT COUNT(*) as count FROM tokens 
        WHERE counter_id = ? AND status = 'COMPLETED' AND date(completed_at) = date('now');
    """, (counter["id"],))
    completed_today_count = cursor.fetchone()["count"]

    # average service time calculation
    cursor.execute("""
        SELECT AVG((strftime('%s', completed_at) - strftime('%s', started_at)) / 60.0) as avg_mins
        FROM tokens
        WHERE counter_id = ? AND status = 'COMPLETED' AND started_at IS NOT NULL AND completed_at IS NOT NULL;
    """, (counter["id"],))
    avg_row = cursor.fetchone()
    avg_mins = round(avg_row["avg_mins"], 1) if avg_row and avg_row["avg_mins"] is not None else 4.5

    return {
        "staff": {
            "id": counter["assigned_staff_id"],
            "name": staff_name
        },
        "counter": {
            "id": counter["id"],
            "name": counter["name"],
            "status": counter["status"],
            "service_id": counter["service_id"],
            "service_name": service["name"],
            "service_code": service["code"]
        },
        "service": service,
        "current_token": current_token,
        "waiting_queue": waiting_queue_details,
        "stats": {
            "queue_length": len(waiting_queue_details),
            "currently_serving_number": current_token["token_number"] if current_token else None,
            "waiting_count": len(waiting_queue_details),
            "held_count": held_count,
            "completed_today_count": completed_today_count,
            "avg_service_time_minutes": avg_mins
        }
    }

@router.get("/dashboard", response_model=StaffDashboardResponse)
def get_staff_dashboard(
    counter: dict = Depends(get_assigned_counter),
    db: sqlite3.Connection = Depends(get_db)
):
    """
    Exposes consolidated staff operational dashboard metrics.
    """
    return get_dashboard_data(db, counter)

@router.get("/counter")
def get_staff_counter(counter: dict = Depends(get_assigned_counter)):
    """
    Returns the currently assigned counter for the logged-in staff member.
    """
    return counter

@router.get("/counter/queue", response_model=List[TokenResponseDetail])
def get_counter_waiting_queue(
    counter: dict = Depends(get_assigned_counter),
    db: sqlite3.Connection = Depends(get_db)
):
    """
    Retrieves the service-wide waiting queue for the staff counter.
    """
    # Fetch queue
    queue = queue_service.get_waiting_queue(db, counter["service_id"])
    
    # Decorate with names and position details
    cursor = db.cursor()
    detailed_queue = []
    for t in queue:
        cursor.execute("""
            SELECT t.*, s.name as service_name, c.name as counter_name
            FROM tokens t
            JOIN services s ON t.service_id = s.id
            LEFT JOIN counters c ON t.counter_id = c.id
            WHERE t.id = ?;
        """, (t["id"],))
        row = cursor.fetchone()
        if row:
            token_detail = dict(row)
            pos_details = queue_service.get_token_position_details(db, t["id"])
            if pos_details:
                token_detail.update(pos_details)
            detailed_queue.append(token_detail)
        
    return detailed_queue

@router.get("/tokens/{token_id}", response_model=TokenResponseDetail)
def get_token_details(
    token_id: str,
    db: sqlite3.Connection = Depends(get_db),
    current_user: dict = Depends(require_staff)  # Standard staff validation
):
    """
    Retrieves full details of a specific token.
    """
    cursor = db.cursor()
    cursor.execute("""
        SELECT t.*, s.name as service_name, s.code as service_code, c.name as counter_name
        FROM tokens t
        JOIN services s ON t.service_id = s.id
        LEFT JOIN counters c ON t.counter_id = c.id
        WHERE t.id = ?;
    """, (token_id,))
    row = cursor.fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Token not found"
        )
    return dict(row)

@router.post("/counter/next", response_model=StaffActionResponse)
def call_next_queue_token(
    counter: dict = Depends(get_assigned_counter),
    db: sqlite3.Connection = Depends(get_db)
):
    """
    Assigns and starts serving the next waiting token in line for the counter's service.
    """
    token = queue_service.call_next_token(db, counter_id=counter["id"], service_id=counter["service_id"])
    
    # Fetch updated dashboard
    cursor = db.cursor()
    cursor.execute("SELECT * FROM counters WHERE id = ?;", (counter["id"],))
    updated_counter = dict(cursor.fetchone())
    
    dashboard = get_dashboard_data(db, updated_counter)
    
    # Emit socket updates after database commit
    from app.services import socket_service
    socket_service.emit_token_called(counter_id=counter["id"], token=token)
    socket_service.emit_queue_updated(
        service_id=counter["service_id"],
        payload={"action": "CALL_NEXT", "tokenId": token["id"], "counterId": counter["id"]}
    )
    
    return {
        "message": f"Token {token['token_number']} called successfully",
        "token": token,
        "dashboard": dashboard
    }

@router.post("/tokens/{token_id}/complete", response_model=StaffActionResponse)
def complete_serving_token(
    token_id: str,
    counter: dict = Depends(get_assigned_counter),
    db: sqlite3.Connection = Depends(get_db)
):
    """
    Marks the currently serving token as completed.
    """
    token = queue_service.complete_token(db, token_id=token_id, counter_id=counter["id"])
    dashboard = get_dashboard_data(db, counter)
    
    # Emit socket updates after database commit
    from app.services import socket_service
    socket_service.emit_token_completed(counter_id=counter["id"], token=token)
    socket_service.emit_queue_updated(
        service_id=counter["service_id"],
        payload={"action": "COMPLETE", "tokenId": token["id"], "counterId": counter["id"]}
    )
    
    return {
        "message": f"Token {token['token_number']} completed",
        "token": token,
        "dashboard": dashboard
    }

@router.post("/tokens/{token_id}/hold", response_model=StaffActionResponse)
def place_token_on_hold(
    token_id: str,
    counter: dict = Depends(get_assigned_counter),
    db: sqlite3.Connection = Depends(get_db)
):
    """
    Suspends a serving token by placing it on hold.
    """
    token = queue_service.hold_token(db, token_id=token_id, counter_id=counter["id"])
    dashboard = get_dashboard_data(db, counter)
    
    # Emit socket updates after database commit
    from app.services import socket_service
    socket_service.emit_token_held(counter_id=counter["id"], token=token)
    socket_service.emit_queue_updated(
        service_id=counter["service_id"],
        payload={"action": "HOLD", "tokenId": token["id"], "counterId": counter["id"]}
    )
    
    return {
        "message": f"Token {token['token_number']} placed on hold",
        "token": token,
        "dashboard": dashboard
    }

@router.post("/tokens/{token_id}/resume", response_model=StaffActionResponse)
def resume_held_token(
    token_id: str,
    counter: dict = Depends(get_assigned_counter),
    db: sqlite3.Connection = Depends(get_db)
):
    """
    Resumes a held token back to active serving status.
    """
    token = queue_service.resume_token(db, token_id=token_id, counter_id=counter["id"])
    dashboard = get_dashboard_data(db, counter)
    
    # Emit socket updates after database commit
    from app.services import socket_service
    socket_service.emit_token_resumed(counter_id=counter["id"], token=token)
    socket_service.emit_queue_updated(
        service_id=counter["service_id"],
        payload={"action": "RESUME", "tokenId": token["id"], "counterId": counter["id"]}
    )
    
    return {
        "message": f"Token {token['token_number']} resumed to SERVING",
        "token": token,
        "dashboard": dashboard
    }

@router.post("/tokens/{token_id}/skip", response_model=StaffActionResponse)
def skip_active_token(
    token_id: str,
    counter: dict = Depends(get_assigned_counter),
    db: sqlite3.Connection = Depends(get_db)
):
    """
    Skips a waiting, serving, or held token.
    """
    token = queue_service.skip_token(db, token_id=token_id, counter_id=counter["id"])
    dashboard = get_dashboard_data(db, counter)
    
    # Emit socket updates after database commit
    from app.services import socket_service
    socket_service.emit_token_skipped(counter_id=counter["id"], token=token)
    socket_service.emit_queue_updated(
        service_id=counter["service_id"],
        payload={"action": "SKIP", "tokenId": token["id"], "counterId": counter["id"]}
    )
    
    return {
        "message": f"Token {token['token_number']} skipped",
        "token": token,
        "dashboard": dashboard
    }

@router.patch("/counter/status", response_model=CounterStatusUpdateResponse)
def toggle_counter_status(
    payload: CounterStatusUpdateRequest,
    counter: dict = Depends(get_assigned_counter),
    db: sqlite3.Connection = Depends(get_db)
):
    """
    Updates the operational status of the counter (OPEN, CLOSED, BUSY, MAINTENANCE).
    """
    allowed_statuses = ["OPEN", "CLOSED", "BUSY", "MAINTENANCE"]
    if payload.status not in allowed_statuses:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status. Must be one of: {', '.join(allowed_statuses)}"
        )
        
    cursor = db.cursor()
    cursor.execute("UPDATE counters SET status = ? WHERE id = ?;", (payload.status, counter["id"]))
    db.commit()
    
    # Reload counter
    cursor.execute("SELECT * FROM counters WHERE id = ?;", (counter["id"],))
    updated_counter = dict(cursor.fetchone())
    
    dashboard = get_dashboard_data(db, updated_counter)
    
    # Emit socket updates after database commit
    from app.services import socket_service
    socket_service.emit_counter_status_changed(counter_id=counter["id"], status=payload.status)
    socket_service.emit_queue_updated(
        service_id=counter["service_id"],
        payload={"action": "COUNTER_STATUS", "status": payload.status}
    )
    
    return {
        "message": f"Counter status updated to {payload.status}",
        "counter": {
            "id": counter["id"],
            "status": payload.status
        },
        "dashboard": dashboard
    }
