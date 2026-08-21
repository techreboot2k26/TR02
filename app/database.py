import sqlite3
import hashlib
from datetime import datetime, timedelta
from typing import Generator
from app.config import settings

def hash_password(password: str) -> str:
    """
    Hashes password using PBKDF2 with SHA512 to match the Node.js implementation.
    """
    salt = b'queuecraft_salt_2026'
    h = hashlib.pbkdf2_hmac('sha512', password.encode('utf-8'), salt, 1000, dklen=64)
    return h.hex()

def get_db() -> Generator[sqlite3.Connection, None, None]:
    """
    Generator dependency to yield a SQLite database connection.
    Enforces foreign key constraints, WAL journal mode, and busy timeout.
    """
    conn = sqlite3.connect(settings.db_path, check_same_thread=False, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    
    # Enable foreign keys
    conn.execute("PRAGMA foreign_keys = ON;")
    
    # Enable WAL mode and busy timeout for concurrent operations
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    
    try:
        yield conn
    finally:
        conn.close()

def initialize_schema() -> None:
    """
    Creates SQLite database tables if they do not exist.
    """
    conn = sqlite3.connect(settings.db_path, timeout=5.0, isolation_level=None)
    try:
        cursor = conn.cursor()
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        
        # 1. USERS TABLE
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          email TEXT UNIQUE NOT NULL,
          password_hash TEXT NOT NULL,
          role TEXT NOT NULL CHECK(role IN ('STUDENT', 'STAFF', 'ADMIN')),
          created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """)
        
        # 2. SERVICES TABLE
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS services (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          code TEXT UNIQUE NOT NULL,
          description TEXT,
          max_capacity INTEGER DEFAULT 10,
          created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """)
        
        # 3. COUNTERS TABLE
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS counters (
          id TEXT PRIMARY KEY,
          service_id TEXT NOT NULL,
          name TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'CLOSED' CHECK(status IN ('OPEN', 'CLOSED', 'BUSY', 'MAINTENANCE')),
          assigned_staff_id TEXT,
          created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE CASCADE,
          FOREIGN KEY (assigned_staff_id) REFERENCES users(id) ON DELETE SET NULL
        );
        """)
        
        # 4. TOKENS TABLE
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS tokens (
          id TEXT PRIMARY KEY,
          token_number TEXT NOT NULL,
          student_id TEXT,
          student_name TEXT NOT NULL,
          student_email TEXT,
          service_id TEXT NOT NULL,
          counter_id TEXT,
          priority TEXT NOT NULL DEFAULT 'NORMAL' CHECK(priority IN ('NORMAL', 'HIGH', 'PRIORITY', 'URGENT')),
          status TEXT NOT NULL DEFAULT 'WAITING' CHECK(status IN ('WAITING', 'SERVING', 'HELD', 'COMPLETED', 'SKIPPED', 'CANCELLED', 'WAITLISTED', 'PROMOTED', 'EXPIRED')),
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          started_at DATETIME,
          completed_at DATETIME,
          skipped_at DATETIME,
          held_at DATETIME,
          notes TEXT,
          FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE CASCADE,
          FOREIGN KEY (counter_id) REFERENCES counters(id) ON DELETE SET NULL,
          FOREIGN KEY (student_id) REFERENCES users(id) ON DELETE SET NULL
        );
        """)
        
        # INDEXES FOR MAX PERFORMANCE & CONCURRENCY CONSTRAINTS
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tokens_service_status ON tokens(service_id, status);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tokens_counter_status ON tokens(counter_id, status);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tokens_created_priority ON tokens(priority, created_at);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tokens_waitlist ON tokens(service_id, status, created_at);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_counters_assigned_staff ON counters(assigned_staff_id);")

        # DATABASE-LEVEL CONCURRENCY INVARIANT ENFORCEMENT
        # 1. At most ONE token can be in SERVING status for any given counter simultaneously
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_counter_serving ON tokens(counter_id) WHERE status = 'SERVING';")
        
        try:
            cursor.execute("ALTER TABLE services ADD COLUMN max_capacity INTEGER DEFAULT 10;")
        except Exception:
            pass

        try:
            cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='tokens';")
            row = cursor.fetchone()
            if row and "WAITLISTED" not in row[0]:
                cursor.execute("ALTER TABLE tokens RENAME TO tokens_old;")
                cursor.execute("""
                CREATE TABLE tokens (
                  id TEXT PRIMARY KEY,
                  token_number TEXT NOT NULL,
                  student_id TEXT,
                  student_name TEXT NOT NULL,
                  student_email TEXT,
                  service_id TEXT NOT NULL,
                  counter_id TEXT,
                  priority TEXT NOT NULL DEFAULT 'NORMAL' CHECK(priority IN ('NORMAL', 'HIGH', 'PRIORITY', 'URGENT')),
                  status TEXT NOT NULL DEFAULT 'WAITING' CHECK(status IN ('WAITING', 'SERVING', 'HELD', 'COMPLETED', 'SKIPPED', 'CANCELLED', 'WAITLISTED', 'PROMOTED', 'EXPIRED')),
                  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  started_at DATETIME,
                  completed_at DATETIME,
                  skipped_at DATETIME,
                  held_at DATETIME,
                  notes TEXT,
                  FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE CASCADE,
                  FOREIGN KEY (counter_id) REFERENCES counters(id) ON DELETE SET NULL,
                  FOREIGN KEY (student_id) REFERENCES users(id) ON DELETE SET NULL
                );
                """)
                cursor.execute("INSERT INTO tokens SELECT * FROM tokens_old;")
                cursor.execute("DROP TABLE tokens_old;")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_tokens_service_status ON tokens(service_id, status);")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_tokens_counter_status ON tokens(counter_id, status);")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_tokens_created_priority ON tokens(priority, created_at);")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_tokens_waitlist ON tokens(service_id, status, created_at);")
                cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_counter_serving ON tokens(counter_id) WHERE status = 'SERVING';")
        except Exception:
            pass

        conn.commit()
    finally:
        conn.close()

def seed_database(force: bool = False) -> None:
    """
    Populates SQLite database with initial seeding information if it is empty or force=True.
    """
    conn = sqlite3.connect(settings.db_path)
    try:
        cursor = conn.cursor()
        
        # Check if database is already seeded
        if not force:
            cursor.execute("SELECT COUNT(*) as count FROM users;")
            user_count = cursor.fetchone()[0]
            if user_count > 0:
                return

        print("[Database] Seeding database with fresh mock data...")
        
        # Clear existing data for fresh seed reset
        cursor.execute("DELETE FROM tokens;")
        cursor.execute("DELETE FROM counters;")
        cursor.execute("DELETE FROM services;")
        cursor.execute("DELETE FROM users;")
        
        password_hash = hash_password("password123")
        
        # Seed Users
        users = [
            ('usr-staff-rudresh', 'Rudresh', 'rudresh@queuecraft.edu', password_hash, 'STAFF'),
            ('usr-staff-priya', 'Priya Singh', 'priya@queuecraft.edu', password_hash, 'STAFF'),
            ('usr-student-aarav', 'Aarav Sharma', 'aarav@queuecraft.edu', password_hash, 'STUDENT'),
            ('usr-student-ananya', 'Ananya Patel', 'ananya@queuecraft.edu', password_hash, 'STUDENT'),
            ('usr-student-rohan', 'Rohan Verma', 'rohan@queuecraft.edu', password_hash, 'STUDENT'),
            ('usr-student-diya', 'Diya Sengupta', 'diya@queuecraft.edu', password_hash, 'STUDENT'),
            ('usr-student-vikram', 'Vikram Malhotra', 'vikram@queuecraft.edu', password_hash, 'STUDENT'),
            ('usr-student-neha', 'Neha Joshi', 'neha@queuecraft.edu', password_hash, 'STUDENT'),
            ('usr-student-karan', 'Karan Mehta', 'karan@queuecraft.edu', password_hash, 'STUDENT'),
            ('usr-student-demo', 'Demo Student', 'student@queuecraft.edu', password_hash, 'STUDENT'),
            ('usr-admin-demo', 'System Admin', 'admin@queuecraft.edu', password_hash, 'ADMIN')
        ]
        cursor.executemany("INSERT INTO users (id, name, email, password_hash, role) VALUES (?, ?, ?, ?, ?);", users)
        
        # Seed Services
        services = [
            ('srv-lp', 'Library Printer', 'LP', 'High-speed printing, binding, and scanning services in the Central Library.'),
            ('srv-cnt', 'Campus Canteen', 'CNT', 'Order pickup and food token counters at the Student Hub.'),
            ('srv-adm', 'Administration Office', 'ADM', 'Student document verification, transcripts, and fee payment desks.')
        ]
        cursor.executemany("INSERT INTO services (id, name, code, description) VALUES (?, ?, ?, ?);", services)
        
        # Seed Counters
        counters = [
            ('cntr-lp-1', 'srv-lp', 'Printer Counter 1', 'CLOSED', None),
            ('cntr-lp-2', 'srv-lp', 'Printer Counter 2', 'OPEN', 'usr-staff-rudresh'),
            ('cntr-cnt-1', 'srv-cnt', 'Canteen Counter 1', 'OPEN', 'usr-staff-priya')
        ]
        cursor.executemany("INSERT INTO counters (id, service_id, name, status, assigned_staff_id) VALUES (?, ?, ?, ?, ?);", counters)
        
        # Seed Tokens
        from datetime import timezone
        now = datetime.now(timezone.utc)
        def minutes_ago(m):
            return (now - timedelta(minutes=m)).strftime('%Y-%m-%d %H:%M:%S')
            
        tokens = [
            ('tkn-039', 'LP-039', 'usr-student-neha', 'Neha Joshi', 'neha@queuecraft.edu',
             'srv-lp', 'cntr-lp-2', 'NORMAL', 'COMPLETED',
             minutes_ago(45), minutes_ago(40), minutes_ago(32), None, None, 'Printed 15 pages thesis draft'),
             
            ('tkn-040', 'LP-040', 'usr-student-karan', 'Karan Mehta', 'karan@queuecraft.edu',
             'srv-lp', 'cntr-lp-2', 'NORMAL', 'COMPLETED',
             minutes_ago(35), minutes_ago(31), minutes_ago(22), None, None, 'Color poster printing'),
             
            ('tkn-041', 'LP-041', 'usr-student-aarav', 'Aarav Sharma', 'aarav@queuecraft.edu',
             'srv-lp', 'cntr-lp-2', 'NORMAL', 'SERVING',
             minutes_ago(25), minutes_ago(10), None, None, None, 'Lab manual spiral binding'),
             
            ('tkn-042', 'LP-042', 'usr-student-ananya', 'Ananya Patel', 'ananya@queuecraft.edu',
             'srv-lp', 'cntr-lp-2', 'NORMAL', 'WAITING',
             minutes_ago(10), None, None, None, None, 'Assignment printout'),
             
            ('tkn-043', 'LP-043', 'usr-student-rohan', 'Rohan Verma', 'rohan@queuecraft.edu',
             'srv-lp', 'cntr-lp-2', 'NORMAL', 'WAITING',
             minutes_ago(5), None, None, None, None, 'Project report 5 copies'),
             
            ('tkn-044', 'LP-044', 'usr-student-diya', 'Diya Sengupta', 'diya@queuecraft.edu',
             'srv-lp', 'cntr-lp-2', 'HIGH', 'WAITING',
             minutes_ago(2), None, None, None, None, 'Urgent exam hall ticket printout'),
             
            ('tkn-045', 'LP-045', 'usr-student-vikram', 'Vikram Malhotra', 'vikram@queuecraft.edu',
             'srv-lp', 'cntr-lp-2', 'NORMAL', 'HELD',
             minutes_ago(30), minutes_ago(22), None, None, minutes_ago(18), 'Awaiting digital payment confirmation')
        ]
        cursor.executemany("""
            INSERT INTO tokens (
                id, token_number, student_id, student_name, student_email, service_id, counter_id,
                priority, status, created_at, started_at, completed_at, skipped_at, held_at, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, tokens)
        
        conn.commit()
        print("[Database] Seeding complete.")
    finally:
        conn.close()
