import os
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.config import settings

# Force settings for the test environment
settings.mock_auth = True
settings.db_path = os.getenv("DB_PATH", "test_queuecraft.db")

client = TestClient(app)

@pytest.fixture(scope="module", autouse=True)
def setup_test_db():
    """
    Initializes and seeds the temporary test database before running tests,
    and cleans it up afterwards.
    """
    from app.database import initialize_schema, seed_database
    initialize_schema()
    seed_database(force=True)
    yield
    # Cleanup test database file
    if os.path.exists("test_queuecraft.db"):
        try:
            os.remove("test_queuecraft.db")
        except PermissionError:
            pass

def test_health():
    """
    Verify that the health check endpoint returns 200 and matches the expected JSON structure.
    """
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "QueueCraft Staff Operations Module"
    assert "timestamp" in data

def test_unauthenticated_access():
    """
    Verify that accessing student endpoints without authentication returns 403 Forbidden.
    """
    response = client.get("/api/student/services")
    assert response.status_code == 403

def test_student_services_success():
    """
    Verify that an authenticated student user can successfully retrieve services with embedded counter metadata.
    """
    response = client.get(
        "/api/student/services",
        headers={"Authorization": "Bearer mock-token-student"}
    )
    assert response.status_code == 200
    data = response.json()
    assert "services" in data
    assert len(data["services"]) > 0
    
    # Check format of services
    for service in data["services"]:
        assert "id" in service
        assert "name" in service
        assert "code" in service
        assert "counters" in service
        assert isinstance(service["counters"], list)

    # Find a service that has counters mapped (e.g. Library Printer or Canteen)
    service_with_counters = next((s for s in data["services"] if len(s["counters"]) > 0), None)
    assert service_with_counters is not None, "Seeded data should contain at least one service with active counters"
    
    first_counter = service_with_counters["counters"][0]
    assert "id" in first_counter
    assert "service_id" in first_counter
    assert "name" in first_counter
    assert "status" in first_counter
    assert "queue_size" in first_counter
    assert "estimated_wait_time" in first_counter

def test_student_counters_success():
    """
    Verify that an authenticated student user can successfully retrieve raw counters linked to parent service information.
    """
    response = client.get(
        "/api/student/counters",
        headers={"Authorization": "Bearer mock-token-student"}
    )
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) > 0
    
    first_counter = data[0]
    assert "id" in first_counter
    assert "name" in first_counter
    assert "service_id" in first_counter
    assert "status" in first_counter
    assert "service_name" in first_counter
    assert "service_code" in first_counter

def test_authorization_role_restriction():
    """
    Verify that access to student endpoints is blocked for users with roles other than STUDENT.
    """
    response = client.get(
        "/api/student/services",
        headers={"Authorization": "Bearer mock-token-staff"}
    )
    assert response.status_code == 403
    assert "Forbidden" in response.json()["message"]

def test_role_escalation_mitigation_admin_email():
    """
    Verify that a new auto-synchronized user with an admin-like email gets role STUDENT.
    """
    import jwt
    token_payload = {
        "id": "usr-hacker-admin",
        "email": "admin-hacker@example.com",
        "name": "Admin Hacker",
        "role": "ADMIN"  # Client-supplied claim should be ignored
    }
    encoded_token = jwt.encode(token_payload, settings.jwt_secret, algorithm="HS256")
    
    # Try calling student endpoint with this token (should be allowed as they are a STUDENT)
    response = client.get(
        "/api/student/services",
        headers={"Authorization": f"Bearer {encoded_token}"}
    )
    assert response.status_code == 200

    # Query the test database to verify they were auto-synced as STUDENT
    import sqlite3
    conn = sqlite3.connect("test_queuecraft.db")
    cursor = conn.cursor()
    cursor.execute("SELECT role FROM users WHERE id = ?", ("usr-hacker-admin",))
    role = cursor.fetchone()[0]
    conn.close()
    assert role == "STUDENT"

def test_role_escalation_mitigation_staff_email():
    """
    Verify that a new auto-synchronized user with a staff-like email gets role STUDENT.
    """
    import jwt
    token_payload = {
        "id": "usr-hacker-staff",
        "email": "staff-hacker@example.com",
        "name": "Staff Hacker",
        "role": "STAFF"  # Client-supplied claim should be ignored
    }
    encoded_token = jwt.encode(token_payload, settings.jwt_secret, algorithm="HS256")
    
    # Try calling student endpoint
    response = client.get(
        "/api/student/services",
        headers={"Authorization": f"Bearer {encoded_token}"}
    )
    assert response.status_code == 200

    # Verify they were auto-synced as STUDENT
    import sqlite3
    conn = sqlite3.connect("test_queuecraft.db")
    cursor = conn.cursor()
    cursor.execute("SELECT role FROM users WHERE id = ?", ("usr-hacker-staff",))
    role = cursor.fetchone()[0]
    conn.close()
    assert role == "STUDENT"

def test_existing_admin_role_preserved():
    """
    Verify that an existing ADMIN user in SQLite maintains their ADMIN role.
    """
    import sqlite3
    conn = sqlite3.connect("test_queuecraft.db")
    cursor = conn.cursor()
    cursor.execute("SELECT role FROM users WHERE id = ?", ("usr-admin-demo",))
    role = cursor.fetchone()[0]
    conn.close()
    assert role == "ADMIN"

def test_existing_staff_role_preserved():
    """
    Verify that an existing STAFF user in SQLite maintains their STAFF role.
    """
    import sqlite3
    conn = sqlite3.connect("test_queuecraft.db")
    cursor = conn.cursor()
    cursor.execute("SELECT role FROM users WHERE id = ?", ("usr-staff-rudresh",))
    role = cursor.fetchone()[0]
    conn.close()
    assert role == "STAFF"

def test_production_environment_mock_auth_disabled():
    """
    Verify that initializing settings with environment='production' and mock_auth=True raises a validation error.
    """
    from pydantic import ValidationError
    from app.config import Settings
    
    with pytest.raises(ValidationError) as excinfo:
        Settings(environment="production", mock_auth=True)
    assert "mock_auth must be disabled in production environment" in str(excinfo.value)

def test_non_production_mock_auth_allowed():
    """
    Verify that non-production environments can still use mock_auth.
    """
    from app.config import Settings
    dev_settings = Settings(environment="development", mock_auth=True)
    assert dev_settings.mock_auth is True
    
    test_settings = Settings(environment="test", mock_auth=True)
    assert test_settings.mock_auth is True

# Phase 2 Token Lifecycle Tests

def test_successful_booking():
    """
    Verify successful booking for Central Library Printer (srv-lp) at Printer Counter 2 (cntr-lp-2).
    """
    # Use aarav who doesn't have an active waiting/serving token (tkn-041 is SERVING, tkn-042 is WAITING for neha/karan/aarav? wait, let's look at seeder)
    # Aarav has tkn-041 which is SERVING.
    # Ananya has tkn-042 which is WAITING.
    # Rohan has tkn-043 which is WAITING.
    # Diya has tkn-044 which is WAITING.
    # Vikram has tkn-045 which is HELD.
    # Aarav, Neha, Karan, and Vikram have active tokens.
    # Demo Student (usr-student-demo) has no active tokens in the seeder.
    response = client.post(
        "/api/student/tokens/book",
        json={"service_id": "srv-lp", "counter_id": "cntr-lp-2"},
        headers={"Authorization": "Bearer mock-token-student"}
    )
    assert response.status_code == 200
    data = response.json()
    assert "token" in data
    token = data["token"]
    assert token["student_id"] == "usr-student-demo"
    assert token["service_id"] == "srv-lp"
    assert token["counter_id"] == "cntr-lp-2"
    assert token["status"] == "WAITING"
    assert "token_number" in token
    assert token["token_number"].startswith("LP-")

def test_booking_invalid_service():
    """
    Verify booking fails for a non-existent service.
    """
    response = client.post(
        "/api/student/tokens/book",
        json={"service_id": "srv-invalid", "counter_id": "cntr-lp-2"},
        headers={"Authorization": "Bearer mock-token-student"}
    )
    assert response.status_code == 404
    assert "Service not found" in response.json()["message"]

def test_booking_invalid_counter():
    """
    Verify booking fails for a non-existent counter or mismatch.
    """
    response = client.post(
        "/api/student/tokens/book",
        json={"service_id": "srv-lp", "counter_id": "cntr-invalid"},
        headers={"Authorization": "Bearer mock-token-student"}
    )
    assert response.status_code == 404

def test_booking_duplicate_active_token():
    """
    Verify that a student cannot book multiple active tokens.
    """
    # Demo Student already booked one in test_successful_booking.
    # Try booking another active token.
    response = client.post(
        "/api/student/tokens/book",
        json={"service_id": "srv-cnt", "counter_id": "cntr-cnt-1"},
        headers={"Authorization": "Bearer mock-token-student"}
    )
    assert response.status_code == 400
    assert "already have an active token" in response.json()["message"]

def test_get_active_token():
    """
    Verify retrieving the student's current active token.
    """
    response = client.get(
        "/api/student/tokens/active",
        headers={"Authorization": "Bearer mock-token-student"}
    )
    assert response.status_code == 200
    data = response.json()
    assert "token" in data
    assert data["token"] is not None
    assert data["token"]["student_id"] == "usr-student-demo"

def test_cancel_own_token():
    """
    Verify a student can cancel their own active token.
    """
    # Find the active token
    active_res = client.get(
        "/api/student/tokens/active",
        headers={"Authorization": "Bearer mock-token-student"}
    )
    token_id = active_res.json()["token"]["id"]

    # Cancel via PATCH method (which frontend uses)
    cancel_res = client.patch(
        f"/api/student/tokens/{token_id}/cancel",
        headers={"Authorization": "Bearer mock-token-student"}
    )
    assert cancel_res.status_code == 200
    assert cancel_res.json()["success"] is True

    # Check that active token is now null
    active_res_after = client.get(
        "/api/student/tokens/active",
        headers={"Authorization": "Bearer mock-token-student"}
    )
    assert active_res_after.json()["token"] is None

def test_completed_previous_token_can_book_again():
    """
    Verify that once a student's active token is cancelled/completed, they can book again.
    """
    # Booking should succeed now since previous was cancelled.
    response = client.post(
        "/api/student/tokens/book",
        json={"service_id": "srv-lp", "counter_id": "cntr-lp-2"},
        headers={"Authorization": "Bearer mock-token-student"}
    )
    assert response.status_code == 200

def test_token_history():
    """
    Verify historical tokens retrieval.
    """
    response = client.get(
        "/api/student/tokens/history",
        headers={"Authorization": "Bearer mock-token-student"}
    )
    assert response.status_code == 200
    data = response.json()
    assert "tokens" in data
    assert len(data["tokens"]) > 0
    # Our cancelled token should be in history
    assert any(t["status"] == "CANCELLED" for t in data["tokens"])

def test_ownership_enforcement():
    """
    Verify Student A cannot cancel Student B's token.
    """
    import jwt
    # Sign token for Student A (usr-student-a)
    student_a_token = jwt.encode(
        {"id": "usr-student-a", "email": "student_a@example.com", "name": "Student A"},
        settings.jwt_secret,
        algorithm="HS256"
    )
    # Sign token for Student B (usr-student-b)
    student_b_token = jwt.encode(
        {"id": "usr-student-b", "email": "student_b@example.com", "name": "Student B"},
        settings.jwt_secret,
        algorithm="HS256"
    )

    # Student A books a token
    book_res = client.post(
        "/api/student/tokens/book",
        json={"service_id": "srv-lp", "counter_id": "cntr-lp-2"},
        headers={"Authorization": f"Bearer {student_a_token}"}
    )
    token_id = book_res.json()["token"]["id"]

    # Student B attempts to cancel Student A's token
    cancel_res = client.patch(
        f"/api/student/tokens/{token_id}/cancel",
        headers={"Authorization": f"Bearer {student_b_token}"}
    )
    assert cancel_res.status_code == 403
    assert "Forbidden" in cancel_res.json()["message"]

def test_queue_position_and_people_ahead_recalculation():
    """
    Create a queue of waiting tokens, verify people ahead calculation,
    and verify people ahead count shifts when an ahead token gets cancelled.
    """
    import jwt
    t1 = jwt.encode({"id": "usr-q1", "email": "q1@example.com", "name": "Q1"}, settings.jwt_secret, algorithm="HS256")
    t2 = jwt.encode({"id": "usr-q2", "email": "q2@example.com", "name": "Q2"}, settings.jwt_secret, algorithm="HS256")
    t3 = jwt.encode({"id": "usr-q3", "email": "q3@example.com", "name": "Q3"}, settings.jwt_secret, algorithm="HS256")

    # Book T1, T2, T3
    r1 = client.post("/api/student/tokens/book", json={"service_id": "srv-cnt", "counter_id": "cntr-cnt-1"}, headers={"Authorization": f"Bearer {t1}"})
    r2 = client.post("/api/student/tokens/book", json={"service_id": "srv-cnt", "counter_id": "cntr-cnt-1"}, headers={"Authorization": f"Bearer {t2}"})
    r3 = client.post("/api/student/tokens/book", json={"service_id": "srv-cnt", "counter_id": "cntr-cnt-1"}, headers={"Authorization": f"Bearer {t3}"})

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 200

    token1_id = r1.json()["token"]["id"]
    token2_id = r2.json()["token"]["id"]
    token3_id = r3.json()["token"]["id"]

    # Get active token for Q3
    active_q3 = client.get("/api/student/tokens/active", headers={"Authorization": f"Bearer {t3}"})
    # T1 and T2 are ahead of T3 for counter cntr-cnt-1. Wait! Does the seeder already have any active tokens on cntr-cnt-1?
    # Let's check seeder: usr-staff-priya is OPEN on cntr-cnt-1, but no tokens are waiting/serving on cntr-cnt-1 in the seeder!
    # So there are exactly 2 tokens (T1, T2) ahead of T3.
    assert active_q3.json()["token"]["people_ahead"] == 2

    # Cancel T2
    cancel_res = client.patch(f"/api/student/tokens/{token2_id}/cancel", headers={"Authorization": f"Bearer {t2}"})
    assert cancel_res.status_code == 200

    # Verify Q3's people ahead shifts to 1
    active_q3_after = client.get("/api/student/tokens/active", headers={"Authorization": f"Bearer {t3}"})
    assert active_q3_after.json()["token"]["people_ahead"] == 1

def test_concurrent_booking_unique_token_numbers():
    """
    Test concurrent booking attempts, verifying atomic token number generation.
    """
    import jwt
    import threading
    import queue

    # Setup unique users
    tokens = []
    for i in range(10):
        t = jwt.encode(
            {"id": f"usr-thread-{i}", "email": f"thread-{i}@example.com", "name": f"Thread User {i}"},
            settings.jwt_secret,
            algorithm="HS256"
        )
        tokens.append(t)

    results = queue.Queue()

    def run_booking(auth_token):
        try:
            response = client.post(
                "/api/student/tokens/book",
                json={"service_id": "srv-lp", "counter_id": "cntr-lp-2"},
                headers={"Authorization": f"Bearer {auth_token}"}
            )
            results.put(response)
        except Exception as e:
            results.put(e)

    # Launch threads
    threads = []
    for auth_token in tokens:
        th = threading.Thread(target=run_booking, args=(auth_token,))
        threads.append(th)
        th.start()

    # Join threads
    for th in threads:
        th.join()

    # Verify results
    booking_numbers = []
    while not results.empty():
        res = results.get()
        assert not isinstance(res, Exception)
        assert res.status_code == 200
        booking_numbers.append(res.json()["token"]["token_number"])

    # Ensure all token numbers are distinct
    assert len(booking_numbers) == 10
    assert len(set(booking_numbers)) == 10

# Phase 3 Staff Operations Tests

def test_staff_unauthorized_access():
    """
    Verify that student users or unassigned users are blocked from staff endpoints.
    """
    # Student token to staff dashboard -> 403
    res1 = client.get(
        "/api/staff/dashboard",
        headers={"Authorization": "Bearer mock-token-student"}
    )
    assert res1.status_code == 403

    # Unassigned staff token -> 403 (usr-admin-demo is ADMIN, not staff)
    res2 = client.get(
        "/api/staff/dashboard",
        headers={"Authorization": "Bearer mock-token-admin"}
    )
    assert res2.status_code == 403

def test_staff_dashboard_success():
    """
    Verify that an assigned staff member can retrieve their dashboard details.
    """
    response = client.get(
        "/api/staff/dashboard",
        headers={"Authorization": "Bearer mock-token-staff"} # usr-staff-rudresh
    )
    assert response.status_code == 200
    data = response.json()
    assert "staff" in data
    assert data["staff"]["id"] == "usr-staff-rudresh"
    assert "counter" in data
    assert data["counter"]["id"] == "cntr-lp-2"
    assert "service" in data
    assert "current_token" in data
    assert "waiting_queue" in data
    assert "stats" in data
    assert "queue_length" in data["stats"]
    assert "completed_today_count" in data["stats"]

def test_staff_counter_queue():
    """
    Verify staff waiting queue retrieval.
    """
    response = client.get(
        "/api/staff/counter/queue",
        headers={"Authorization": "Bearer mock-token-staff"}
    )
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    if len(data) > 0:
        assert "token_number" in data[0]
        assert "people_ahead" in data[0]

def test_staff_get_token_by_id():
    """
    Verify staff can fetch token by ID.
    """
    # Find an active token first
    response = client.get(
        "/api/staff/counter/queue",
        headers={"Authorization": "Bearer mock-token-staff"}
    )
    data = response.json()
    assert len(data) > 0
    token_id = data[0]["id"]

    res_detail = client.get(
        f"/api/staff/tokens/{token_id}",
        headers={"Authorization": "Bearer mock-token-staff"}
    )
    assert res_detail.status_code == 200
    assert res_detail.json()["id"] == token_id

def test_staff_next_complete_cycle():
    """
    Verify complete staff NEXT -> SERVING -> COMPLETED token lifecycle.
    """
    # Clean up pre-seeded serving token on cntr-lp-2
    import sqlite3
    conn = sqlite3.connect("test_queuecraft.db")
    conn.execute("UPDATE tokens SET status = 'COMPLETED' WHERE counter_id = 'cntr-lp-2' AND status = 'SERVING';")
    conn.commit()
    conn.close()

    # 1. Call next token
    next_res = client.post(
        "/api/staff/counter/next",
        headers={"Authorization": "Bearer mock-token-staff"}
    )
    assert next_res.status_code == 200
    token = next_res.json()["token"]
    assert token["status"] == "SERVING"
    assert token["counter_id"] == "cntr-lp-2"
    token_id = token["id"]

    # 2. Check student active token state
    import jwt
    student_jwt = jwt.encode(
        {"id": token["student_id"], "email": token["student_email"], "name": token["student_name"]},
        settings.jwt_secret,
        algorithm="HS256"
    )
    student_active = client.get(
        "/api/student/tokens/active",
        headers={"Authorization": f"Bearer {student_jwt}"}
    )
    assert student_active.status_code == 200
    assert student_active.json()["token"]["status"] == "SERVING"

    # 3. Complete the token
    comp_res = client.post(
        f"/api/staff/tokens/{token_id}/complete",
        headers={"Authorization": "Bearer mock-token-staff"}
    )
    assert comp_res.status_code == 200
    assert comp_res.json()["token"]["status"] == "COMPLETED"

    # 4. Attempting to complete it again should fail
    fail_res = client.post(
        f"/api/staff/tokens/{token_id}/complete",
        headers={"Authorization": "Bearer mock-token-staff"}
    )
    assert fail_res.status_code == 400

def test_staff_hold_and_resume():
    """
    Verify SERVING -> HELD -> SERVING lifecycle.
    """
    # Clean up pre-seeded serving token on cntr-lp-2
    import sqlite3
    conn = sqlite3.connect("test_queuecraft.db")
    conn.execute("UPDATE tokens SET status = 'COMPLETED' WHERE counter_id = 'cntr-lp-2' AND status = 'SERVING';")
    conn.commit()
    conn.close()

    # 1. Call next token
    next_res = client.post(
        "/api/staff/counter/next",
        headers={"Authorization": "Bearer mock-token-staff"}
    )
    assert next_res.status_code == 200
    token = next_res.json()["token"]
    token_id = token["id"]

    # 2. Put on HOLD
    hold_res = client.post(
        f"/api/staff/tokens/{token_id}/hold",
        headers={"Authorization": "Bearer mock-token-staff"}
    )
    assert hold_res.status_code == 200
    assert hold_res.json()["token"]["status"] == "HELD"

    # 3. Resume the token
    res_res = client.post(
        f"/api/staff/tokens/{token_id}/resume",
        headers={"Authorization": "Bearer mock-token-staff"}
    )
    assert res_res.status_code == 200
    assert res_res.json()["token"]["status"] == "SERVING"

    # 4. Complete to clean up
    client.post(
        f"/api/staff/tokens/{token_id}/complete",
        headers={"Authorization": "Bearer mock-token-staff"}
    )

def test_staff_skip():
    """
    Verify skip token mutation.
    """
    # Clean up pre-seeded serving token on cntr-lp-2
    import sqlite3
    conn = sqlite3.connect("test_queuecraft.db")
    conn.execute("UPDATE tokens SET status = 'COMPLETED' WHERE counter_id = 'cntr-lp-2' AND status = 'SERVING';")
    conn.commit()
    conn.close()

    # 1. Call next token
    next_res = client.post(
        "/api/staff/counter/next",
        headers={"Authorization": "Bearer mock-token-staff"}
    )
    assert next_res.status_code == 200
    token = next_res.json()["token"]
    token_id = token["id"]

    # 2. Skip the token
    skip_res = client.post(
        f"/api/staff/tokens/{token_id}/skip",
        headers={"Authorization": "Bearer mock-token-staff"}
    )
    assert skip_res.status_code == 200
    assert skip_res.json()["token"]["status"] == "SKIPPED"

def test_counter_status_toggle():
    """
    Verify changing counter status via staff route.
    """
    # Set to BUSY
    response1 = client.patch(
        "/api/staff/counter/status",
        json={"status": "BUSY"},
        headers={"Authorization": "Bearer mock-token-staff"}
    )
    assert response1.status_code == 200
    assert response1.json()["counter"]["status"] == "BUSY"

    # Set back to OPEN
    response2 = client.patch(
        "/api/staff/counter/status",
        json={"status": "OPEN"},
        headers={"Authorization": "Bearer mock-token-staff"}
    )
    assert response2.status_code == 200
    assert response2.json()["counter"]["status"] == "OPEN"

def test_queue_ordering_priority_first():
    """
    Verify priority queue ordering (FCFS within priority, highest priority first).
    """
    import jwt
    import sqlite3
    
    t_normal = jwt.encode({"id": "usr-ord-normal", "email": "n@example.com", "name": "Normal"}, settings.jwt_secret, algorithm="HS256")
    t_urgent = jwt.encode({"id": "usr-ord-urgent", "email": "u@example.com", "name": "Urgent"}, settings.jwt_secret, algorithm="HS256")

    # Clean srv-cnt tokens to ensure stable FCFS checking
    conn = sqlite3.connect("test_queuecraft.db")
    conn.execute("DELETE FROM tokens WHERE service_id = 'srv-cnt';")
    conn.commit()
    conn.close()

    # 1. Book NORMAL
    client.post(
        "/api/student/tokens/book",
        json={"service_id": "srv-cnt", "counter_id": "cntr-cnt-1"},
        headers={"Authorization": f"Bearer {t_normal}"}
    )

    # 2. Update priority of normal token manually or book URGENT (wait! book_token does not specify priority in payload in Phase 2 book_token - wait, book_token inserts as NORMAL by default).
    # Let's insert a second token for another user with URGENT priority directly in the db, or update it.
    conn = sqlite3.connect("test_queuecraft.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO tokens (id, token_number, student_id, student_name, student_email, service_id, counter_id, priority, status)
        VALUES ('tkn-u1', 'CNT-002', 'usr-ord-urgent', 'Urgent Student', 'u@example.com', 'srv-cnt', 'cntr-cnt-1', 'URGENT', 'WAITING');
    """)
    conn.commit()
    conn.close()

    # Fetch waiting queue
    # The URGENT token should be index 0 (ahead), even though the NORMAL token was booked first!
    response = client.get(
        "/api/student/tokens/active",
        headers={"Authorization": f"Bearer {t_normal}"} # Normal student should have 1 person ahead (the urgent one!)
    )
    assert response.status_code == 200
    assert response.json()["token"]["people_ahead"] == 1

def test_concurrent_staff_next_operations():
    """
    Simulate concurrent NEXT actions by two staff members on different counters of the same service.
    Verify they claim distinct waiting tokens atomically.
    """
    import jwt
    import sqlite3
    import threading
    import queue

    # 1. Set up second counter assignment: usr-staff-priya assigned to cntr-lp-1 (which belongs to srv-lp)
    conn = sqlite3.connect("test_queuecraft.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE counters SET status = 'OPEN', assigned_staff_id = 'usr-staff-priya' WHERE id = 'cntr-lp-1';")
    # Clean up srv-lp active/serving tokens to avoid conflicts
    cursor.execute("UPDATE tokens SET status = 'COMPLETED' WHERE service_id = 'srv-lp' AND status = 'SERVING';")
    # Seed 5 waiting tokens for srv-lp
    for i in range(5):
        cursor.execute(f"""
            INSERT INTO tokens (id, token_number, student_id, student_name, service_id, counter_id, priority, status, created_at)
            VALUES ('tkn-seq-{i}', 'LP-90{i}', 'usr-student-aarav', 'Aarav', 'srv-lp', 'cntr-lp-2', 'NORMAL', 'WAITING', '2026-08-19 00:00:0{i}');
        """)
    conn.commit()
    conn.close()

    # Staff credentials
    staff_rudresh_token = "mock-token-staff" # usr-staff-rudresh -> cntr-lp-2
    staff_priya_token = jwt.encode(
        {"id": "usr-staff-priya", "email": "priya@queuecraft.edu", "name": "Priya Singh", "role": "STAFF"},
        settings.jwt_secret,
        algorithm="HS256"
    ) # usr-staff-priya -> cntr-lp-1

    results = queue.Queue()

    def run_next(auth):
        try:
            res = client.post(
                "/api/staff/counter/next",
                headers={"Authorization": f"Bearer {auth}"}
            )
            results.put(res)
        except Exception as e:
            results.put(e)

    # Trigger concurrent requests
    th1 = threading.Thread(target=run_next, args=(staff_rudresh_token,))
    th2 = threading.Thread(target=run_next, args=(staff_priya_token,))
    
    th1.start()
    th2.start()

    th1.join()
    th2.join()

    # Collect claimed token IDs
    claimed_ids = []
    while not results.empty():
        res = results.get()
        assert not isinstance(res, Exception)
        assert res.status_code == 200
        claimed_ids.append(res.json()["token"]["id"])

    # Assert both claims succeeded and claimed distinct tokens
    assert len(claimed_ids) == 2
    assert len(set(claimed_ids)) == 2

# Phase 4 Socket.IO Synchronization Tests

@pytest.fixture(scope="module")
def run_app_server():
    import time
    import threading
    import uvicorn
    server_thread = threading.Thread(
        target=lambda: uvicorn.run(app, host="127.0.0.1", port=5005, log_level="error"),
        daemon=True
    )
    server_thread.start()
    time.sleep(0.5)  # Let server bind and start
    yield "http://127.0.0.1:5005"

def test_socket_real_time_events(run_app_server):
    import time
    import jwt
    import sqlite3
    import socketio

    server_url = run_app_server
    sio_client = socketio.Client()
    events_received = []

    @sio_client.on('*')
    def catch_all(event, data):
        events_received.append((event, data))

    # 1. Connect
    sio_client.connect(server_url, socketio_path='socket.io')

    # 2. Join Rooms
    sio_client.emit('join_service', 'srv-lp')
    sio_client.emit('join_counter', 'cntr-lp-2')
    time.sleep(0.1)

    # Clean up counters and serving tokens first
    conn = sqlite3.connect("test_queuecraft.db")
    conn.execute("UPDATE tokens SET status = 'COMPLETED' WHERE counter_id = 'cntr-lp-2' AND status = 'SERVING';")
    conn.commit()
    conn.close()

    # 3. Trigger student book via REST API
    student_jwt = jwt.encode(
        {"id": "usr-student-temp-socket", "email": "temp-socket@queuecraft.edu", "name": "Temp Socket"},
        settings.jwt_secret,
        algorithm="HS256"
    )
    book_res = client.post(
        "/api/student/tokens/book",
        json={"service_id": "srv-lp", "counter_id": "cntr-lp-2"},
        headers={"Authorization": f"Bearer {student_jwt}"}
    )
    assert book_res.status_code == 200
    token = book_res.json()["token"]

    time.sleep(0.2)

    # Verify book emits
    event_names = [e[0] for e in events_received]
    assert 'QUEUE_UPDATED' in event_names
    assert 'queueUpdate' in event_names
    
    create_payload = [e[1] for e in events_received if e[0] == 'QUEUE_UPDATED'][-1]
    assert create_payload["action"] == "CREATE"
    assert create_payload["tokenId"] == token["id"]

    events_received.clear()

    # 4. Trigger NEXT operation via REST API
    next_res = client.post(
        "/api/staff/counter/next",
        headers={"Authorization": "Bearer mock-token-staff"}
    )
    assert next_res.status_code == 200
    called_token = next_res.json()["token"]

    time.sleep(0.2)

    # Verify next emits
    event_names = [e[0] for e in events_received]
    assert 'TOKEN_CALLED' in event_names
    assert 'token_called' in event_names

    events_received.clear()

    # 5. Trigger COMPLETE operation via REST API
    comp_res = client.post(
        f"/api/staff/tokens/{called_token['id']}/complete",
        headers={"Authorization": "Bearer mock-token-staff"}
    )
    assert comp_res.status_code == 200

    time.sleep(0.2)

    # Verify complete emits
    event_names = [e[0] for e in events_received]
    assert 'TOKEN_COMPLETED' in event_names
    assert 'token_completed' in event_names

    events_received.clear()

    # 6. Trigger counter status change via REST API
    status_res = client.patch(
        "/api/staff/counter/status",
        json={"status": "BUSY"},
        headers={"Authorization": "Bearer mock-token-staff"}
    )
    assert status_res.status_code == 200

    time.sleep(0.2)

    # Verify status changed emits
    event_names = [e[0] for e in events_received]
    assert 'COUNTER_STATUS_CHANGED' in event_names
    assert 'counter_status_changed' in event_names

    # Clean up and reset counter status to OPEN
    client.patch(
        "/api/staff/counter/status",
        json={"status": "OPEN"},
        headers={"Authorization": "Bearer mock-token-staff"}
    )

    sio_client.disconnect()


# Phase 5 Admin & Staff Counter Resolution Tests

def test_admin_auth_restrictions():
    """
    Verify that unauthenticated, student, and staff requests to admin endpoints are blocked with 403,
    and admin requests are permitted.
    """
    # Unauthenticated
    res_unauth = client.get("/api/admin/dashboard")
    assert res_unauth.status_code == 403

    # Student
    res_student = client.get(
        "/api/admin/dashboard",
        headers={"Authorization": "Bearer mock-token-student"}
    )
    assert res_student.status_code == 403

    # Staff
    res_staff = client.get(
        "/api/admin/dashboard",
        headers={"Authorization": "Bearer mock-token-staff"}
    )
    assert res_staff.status_code == 403

    # Admin
    res_admin = client.get(
        "/api/admin/dashboard",
        headers={"Authorization": "Bearer mock-token-admin"}
    )
    assert res_admin.status_code == 200

def test_admin_dashboard_stats():
    """
    Verify that GET /api/admin/dashboard returns expected database stats schema.
    """
    res = client.get(
        "/api/admin/dashboard",
        headers={"Authorization": "Bearer mock-token-admin"}
    )
    assert res.status_code == 200
    data = res.json()
    assert "services_count" in data
    assert "active_counters_count" in data
    assert "waiting_tokens_count" in data
    assert "currently_serving_count" in data
    assert "completed_today_count" in data
    assert "skipped_today_count" in data
    assert "cancelled_today_count" in data
    assert "avg_waiting_time_minutes" in data

def test_admin_user_crud():
    """
    Verify Admin User CRUD operations: list, create, duplicate email rejection, update, and delete safety.
    """
    admin_headers = {"Authorization": "Bearer mock-token-admin"}

    # 1. List users
    res_list = client.get("/api/admin/users", headers=admin_headers)
    assert res_list.status_code == 200
    users = res_list.json()
    assert isinstance(users, list)
    assert len(users) > 0

    # 2. Create user
    new_user_payload = {
        "name": "Test Operator",
        "email": "testop@queuecraft.edu",
        "password": "securepassword123",
        "role": "STAFF"
    }
    res_create = client.post("/api/admin/users", json=new_user_payload, headers=admin_headers)
    assert res_create.status_code == 201
    created_user = res_create.json()
    assert created_user["email"] == "testop@queuecraft.edu"
    assert created_user["role"] == "STAFF"

    # 3. Duplicate email rejection
    res_dup = client.post("/api/admin/users", json=new_user_payload, headers=admin_headers)
    assert res_dup.status_code == 400

    # 4. Update user
    res_update = client.patch(
        f"/api/admin/users/{created_user['id']}",
        json={"name": "Test Operator Updated", "role": "STUDENT"},
        headers=admin_headers
    )
    assert res_update.status_code == 200
    assert res_update.json()["name"] == "Test Operator Updated"
    assert res_update.json()["role"] == "STUDENT"

    # 5. Delete user
    res_del = client.delete(f"/api/admin/users/{created_user['id']}", headers=admin_headers)
    assert res_del.status_code == 200
    assert res_del.json()["success"] is True

    # 6. Delete self rejection (mock admin user id is usr-admin-demo)
    res_self_del = client.delete("/api/admin/users/usr-admin-demo", headers=admin_headers)
    assert res_self_del.status_code == 400

def test_admin_service_crud():
    """
    Verify Admin Service CRUD operations: list, create, duplicate shortcode rejection, update, and delete safety checks.
    """
    admin_headers = {"Authorization": "Bearer mock-token-admin"}

    # 1. List services
    res_list = client.get("/api/admin/services", headers=admin_headers)
    assert res_list.status_code == 200
    services = res_list.json()
    assert len(services) > 0

    # 2. Create service
    new_srv_payload = {
        "name": "Financial Aid Desk",
        "code": "FIN",
        "description": "Student loans and scholarships assistance"
    }
    res_create = client.post("/api/admin/services", json=new_srv_payload, headers=admin_headers)
    assert res_create.status_code == 201
    created_srv = res_create.json()
    assert created_srv["code"] == "FIN"

    # 3. Duplicate shortcode rejection
    res_dup = client.post("/api/admin/services", json=new_srv_payload, headers=admin_headers)
    assert res_dup.status_code == 400

    # 4. Update service
    res_update = client.patch(
        f"/api/admin/services/{created_srv['id']}",
        json={"name": "Financial Aid & Grants"},
        headers=admin_headers
    )
    assert res_update.status_code == 200
    assert res_update.json()["name"] == "Financial Aid & Grants"

    # 5. Delete newly created service without linked counters/tokens
    res_del = client.delete(f"/api/admin/services/{created_srv['id']}", headers=admin_headers)
    assert res_del.status_code == 200

    # 6. Delete service with linked counters rejection (srv-lp has linked counters)
    res_del_linked = client.delete("/api/admin/services/srv-lp", headers=admin_headers)
    assert res_del_linked.status_code == 400

def test_admin_counter_crud_and_staff_assignment():
    """
    Verify Admin Counter CRUD and Staff Assignment exclusivity.
    """
    admin_headers = {"Authorization": "Bearer mock-token-admin"}

    # 1. List counters
    res_list = client.get("/api/admin/counters", headers=admin_headers)
    assert res_list.status_code == 200
    counters = res_list.json()
    assert len(counters) > 0

    # 2. Create counter under Library Printer (srv-lp)
    new_cntr_payload = {
        "name": "Printer Counter 3",
        "service_id": "srv-lp",
        "status": "CLOSED"
    }
    res_create = client.post("/api/admin/counters", json=new_cntr_payload, headers=admin_headers)
    assert res_create.status_code == 201
    created_cntr = res_create.json()
    assert created_cntr["name"] == "Printer Counter 3"

    # 3. Update counter
    res_update = client.patch(
        f"/api/admin/counters/{created_cntr['id']}",
        json={"status": "MAINTENANCE"},
        headers=admin_headers
    )
    assert res_update.status_code == 200
    assert res_update.json()["status"] == "MAINTENANCE"

    # 4. Assign staff operator (usr-staff-rudresh) to the new counter
    res_assign = client.patch(
        f"/api/admin/counters/{created_cntr['id']}/assign-staff",
        json={"staffId": "usr-staff-rudresh"},
        headers=admin_headers
    )
    assert res_assign.status_code == 200
    assert res_assign.json()["assigned_staff_id"] == "usr-staff-rudresh"

    # 5. Reject non-staff user assignment
    res_invalid_assign = client.patch(
        f"/api/admin/counters/{created_cntr['id']}/assign-staff",
        json={"staffId": "usr-student-aarav"},
        headers=admin_headers
    )
    assert res_invalid_assign.status_code == 400

    # Re-assign back to original counter (cntr-lp-2) for test consistency
    client.patch(
        "/api/admin/counters/cntr-lp-2/assign-staff",
        json={"staffId": "usr-staff-rudresh"},
        headers=admin_headers
    )

    # 6. Delete created counter
    res_del = client.delete(f"/api/admin/counters/{created_cntr['id']}", headers=admin_headers)
    assert res_del.status_code == 200

def test_admin_live_monitor_and_analytics():
    """
    Verify GET /api/admin/live-monitor and GET /api/admin/analytics payloads.
    """
    admin_headers = {"Authorization": "Bearer mock-token-admin"}

    # 1. Live Monitor
    res_monitor = client.get("/api/admin/live-monitor", headers=admin_headers)
    assert res_monitor.status_code == 200
    monitor_data = res_monitor.json()
    assert isinstance(monitor_data, list)
    assert len(monitor_data) > 0
    first_item = monitor_data[0]
    assert "counter_id" in first_item
    assert "counter_name" in first_item
    assert "service_name" in first_item

    # 2. Analytics
    res_analytics = client.get("/api/admin/analytics", headers=admin_headers)
    assert res_analytics.status_code == 200
    analytics_data = res_analytics.json()
    assert "summary" in analytics_data
    assert "service_distribution" in analytics_data
    assert "counter_activity" in analytics_data
    assert "hourly_distribution" in analytics_data

def test_staff_counter_endpoint():
    """
    Verify GET /api/staff/counter returns the assigned counter cntr-lp-2 for the staff member.
    """
    res = client.get(
        "/api/staff/counter",
        headers={"Authorization": "Bearer mock-token-staff"}
    )
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == "cntr-lp-2"
    assert data["service_id"] == "srv-lp"
    assert data["assigned_staff_id"] == "usr-staff-rudresh"





