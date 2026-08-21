import { getDb } from './database.js';

export function initializeSchema(): void {
  const db = getDb();

  db.exec(`
    -- USERS TABLE
    CREATE TABLE IF NOT EXISTS users (
      id TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      email TEXT UNIQUE NOT NULL,
      password_hash TEXT NOT NULL,
      role TEXT NOT NULL CHECK(role IN ('STUDENT', 'STAFF', 'ADMIN')),
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    -- SERVICES TABLE
    CREATE TABLE IF NOT EXISTS services (
      id TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      code TEXT UNIQUE NOT NULL,
      description TEXT,
      max_capacity INTEGER DEFAULT 10,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    -- COUNTERS TABLE
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

    -- TOKENS TABLE
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

    -- INDEXES FOR QUEUE PERFORMANCE & CONCURRENCY CONSTRAINTS
    CREATE INDEX IF NOT EXISTS idx_tokens_service_status ON tokens(service_id, status);
    CREATE INDEX IF NOT EXISTS idx_tokens_counter_status ON tokens(counter_id, status);
    CREATE INDEX IF NOT EXISTS idx_tokens_created_priority ON tokens(priority, created_at);
    CREATE INDEX IF NOT EXISTS idx_tokens_waitlist ON tokens(service_id, status, created_at);
    CREATE INDEX IF NOT EXISTS idx_counters_assigned_staff ON counters(assigned_staff_id);

    -- DATABASE-LEVEL CONCURRENCY INVARIANT ENFORCEMENT
    -- 1. At most ONE token can be in SERVING status for any given counter simultaneously
    CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_counter_serving ON tokens(counter_id) WHERE status = 'SERVING';
  `);

  // Safe migration for max_capacity column if services already exists
  try {
    db.exec(`ALTER TABLE services ADD COLUMN max_capacity INTEGER DEFAULT 10;`);
  } catch (err) {
    // Column already exists
  }

  // Safe migration for tokens table CHECK constraint if WAITLISTED is missing
  try {
    const tableInfo = db.prepare(`SELECT sql FROM sqlite_master WHERE type='table' AND name='tokens'`).get() as { sql: string } | undefined;
    if (tableInfo && !tableInfo.sql.includes('WAITLISTED')) {
      db.exec(`
        ALTER TABLE tokens RENAME TO tokens_old;

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

        INSERT INTO tokens SELECT * FROM tokens_old;
        DROP TABLE tokens_old;

        CREATE INDEX IF NOT EXISTS idx_tokens_service_status ON tokens(service_id, status);
        CREATE INDEX IF NOT EXISTS idx_tokens_counter_status ON tokens(counter_id, status);
        CREATE INDEX IF NOT EXISTS idx_tokens_created_priority ON tokens(priority, created_at);
        CREATE INDEX IF NOT EXISTS idx_tokens_waitlist ON tokens(service_id, status, created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_counter_serving ON tokens(counter_id) WHERE status = 'SERVING';
      `);
    }
  } catch (err) {
    // Migration handled
  }
}
