import { describe, it, expect, beforeEach, afterAll } from 'vitest';
import { getDb, closeDb } from '../db/database.js';
import { initializeSchema } from '../db/schema.js';
import { seedDatabase } from '../db/seed.js';
import { queueEngine } from '../services/queueEngine.js';

describe('QueueCraft Fair Automatic Waitlist Promotion Test Suite', () => {
  beforeEach(() => {
    process.env.DB_PATH = 'test_waitlist_ts.db';
    initializeSchema();
    seedDatabase();
  });

  afterAll(() => {
    closeDb();
  });

  // TEST 1 — Empty waitlist
  it('Test 1: Empty waitlist -> No candidate promoted when capacity is available', () => {
    const db = getDb();
    db.prepare("UPDATE services SET max_capacity = 5 WHERE id = 'srv-lp'").run();
    db.prepare("DELETE FROM tokens WHERE service_id = 'srv-lp'").run();

    const result = queueEngine.evaluateAndPromoteWaitlist('srv-lp');
    expect(result.success).toBe(true);
    expect(result.promotedTokens.length).toBe(0);
    expect(result.availableSlots).toBe(5);
  });

  // TEST 2 — One eligible candidate
  it('Test 2: One eligible candidate is promoted to active WAITING status', () => {
    const db = getDb();
    db.prepare("UPDATE services SET max_capacity = 2 WHERE id = 'srv-lp'").run();
    db.prepare("DELETE FROM tokens WHERE service_id = 'srv-lp'").run();

    // 1 active token + 1 waitlisted token
    db.prepare(`
      INSERT INTO tokens (id, token_number, student_name, service_id, priority, status, created_at)
      VALUES 
        ('tkn-act-1', 'LP-001', 'Active Student', 'srv-lp', 'NORMAL', 'WAITING', datetime('now', '-5 minutes')),
        ('tkn-wl-1', 'LP-002', 'Waitlisted Student', 'srv-lp', 'NORMAL', 'WAITLISTED', datetime('now', '-2 minutes'))
    `).run();

    // Available slots = 2 - 1 = 1
    const result = queueEngine.evaluateAndPromoteWaitlist('srv-lp');
    expect(result.success).toBe(true);
    expect(result.promotedTokens.length).toBe(1);
    expect(result.promotedTokens[0].id).toBe('tkn-wl-1');
    expect(result.promotedTokens[0].status).toBe('WAITING');

    const updatedToken = db.prepare('SELECT status FROM tokens WHERE id = ?').get('tkn-wl-1') as any;
    expect(updatedToken.status).toBe('WAITING');
  });

  // TEST 3 — Multiple candidates (Highest ranked candidate promoted)
  it('Test 3: Multiple candidates -> Highest ranked candidate is promoted according to policy', () => {
    const db = getDb();
    db.prepare("UPDATE services SET max_capacity = 1 WHERE id = 'srv-lp'").run();
    db.prepare("DELETE FROM tokens WHERE service_id = 'srv-lp'").run();

    db.prepare(`
      INSERT INTO tokens (id, token_number, student_name, service_id, priority, status, created_at)
      VALUES 
        ('tkn-wl-low', 'LP-001', 'Low Priority', 'srv-lp', 'NORMAL', 'WAITLISTED', datetime('now', '-2 minutes')),
        ('tkn-wl-high', 'LP-002', 'High Priority', 'srv-lp', 'HIGH', 'WAITLISTED', datetime('now', '-2 minutes'))
    `).run();

    const result = queueEngine.evaluateAndPromoteWaitlist('srv-lp');
    expect(result.promotedTokens.length).toBe(1);
    expect(result.promotedTokens[0].id).toBe('tkn-wl-high');

    const remaining = db.prepare("SELECT status FROM tokens WHERE id = 'tkn-wl-low'").get() as any;
    expect(remaining.status).toBe('WAITLISTED');
  });

  // TEST 4 — Priority ordering (High/Urgent before Normal with equal wait time)
  it('Test 4: Priority ordering -> Urgent and High promoted before Normal with equal wait time', () => {
    const db = getDb();
    db.prepare("UPDATE services SET max_capacity = 2 WHERE id = 'srv-lp'").run();
    db.prepare("DELETE FROM tokens WHERE service_id = 'srv-lp'").run();

    db.prepare(`
      INSERT INTO tokens (id, token_number, student_name, service_id, priority, status, created_at)
      VALUES 
        ('tkn-p-normal', 'LP-001', 'Normal Student', 'srv-lp', 'NORMAL', 'WAITLISTED', datetime('now', '-5 minutes')),
        ('tkn-p-urgent', 'LP-002', 'Urgent Student', 'srv-lp', 'URGENT', 'WAITLISTED', datetime('now', '-5 minutes')),
        ('tkn-p-high', 'LP-003', 'High Student', 'srv-lp', 'HIGH', 'WAITLISTED', datetime('now', '-5 minutes'))
    `).run();

    const result = queueEngine.evaluateAndPromoteWaitlist('srv-lp');
    expect(result.promotedTokens.length).toBe(2);
    expect(result.promotedTokens[0].id).toBe('tkn-p-urgent');
    expect(result.promotedTokens[1].id).toBe('tkn-p-high');

    const normalToken = db.prepare("SELECT status FROM tokens WHERE id = 'tkn-p-normal'").get() as any;
    expect(normalToken.status).toBe('WAITLISTED');
  });

  // TEST 5 — Same priority (FIFO / Longer waiting promoted first)
  it('Test 5: Same priority -> Longer waiting candidate promoted first (FIFO within priority)', () => {
    const db = getDb();
    db.prepare("UPDATE services SET max_capacity = 1 WHERE id = 'srv-lp'").run();
    db.prepare("DELETE FROM tokens WHERE service_id = 'srv-lp'").run();

    db.prepare(`
      INSERT INTO tokens (id, token_number, student_name, service_id, priority, status, created_at)
      VALUES 
        ('tkn-recent', 'LP-002', 'Recent Student', 'srv-lp', 'NORMAL', 'WAITLISTED', datetime('now', '-2 minutes')),
        ('tkn-older', 'LP-001', 'Older Student', 'srv-lp', 'NORMAL', 'WAITLISTED', datetime('now', '-10 minutes'))
    `).run();

    const result = queueEngine.evaluateAndPromoteWaitlist('srv-lp');
    expect(result.promotedTokens.length).toBe(1);
    expect(result.promotedTokens[0].id).toBe('tkn-older');
  });

  // TEST 6 — Deterministic tie breaking (Stable ID ASC)
  it('Test 6: Deterministic tie breaking -> Stable ID ASC breaks ties consistently', () => {
    const db = getDb();
    db.prepare("UPDATE services SET max_capacity = 1 WHERE id = 'srv-lp'").run();
    db.prepare("DELETE FROM tokens WHERE service_id = 'srv-lp'").run();

    const fixedTimestamp = '2026-08-21 12:00:00.000';
    db.prepare(`
      INSERT INTO tokens (id, token_number, student_name, service_id, priority, status, created_at)
      VALUES 
        ('tkn-z-99', 'LP-099', 'Student Z', 'srv-lp', 'NORMAL', 'WAITLISTED', ?),
        ('tkn-a-01', 'LP-001', 'Student A', 'srv-lp', 'NORMAL', 'WAITLISTED', ?)
    `).run(fixedTimestamp, fixedTimestamp);

    const result = queueEngine.evaluateAndPromoteWaitlist('srv-lp');
    expect(result.promotedTokens.length).toBe(1);
    expect(result.promotedTokens[0].id).toBe('tkn-a-01');
  });

  // TEST 7 — Cancelled candidate is skipped
  it('Test 7: Cancelled candidate is skipped; next eligible candidate promoted', () => {
    const db = getDb();
    db.prepare("UPDATE services SET max_capacity = 1 WHERE id = 'srv-lp'").run();
    db.prepare("DELETE FROM tokens WHERE service_id = 'srv-lp'").run();

    db.prepare(`
      INSERT INTO tokens (id, token_number, student_name, service_id, priority, status, created_at)
      VALUES 
        ('tkn-canc', 'LP-001', 'Cancelled Student', 'srv-lp', 'URGENT', 'CANCELLED', datetime('now', '-10 minutes')),
        ('tkn-valid', 'LP-002', 'Valid Student', 'srv-lp', 'NORMAL', 'WAITLISTED', datetime('now', '-5 minutes'))
    `).run();

    const result = queueEngine.evaluateAndPromoteWaitlist('srv-lp');
    expect(result.promotedTokens.length).toBe(1);
    expect(result.promotedTokens[0].id).toBe('tkn-valid');
  });

  // TEST 8 — Ineligible candidate with active duplicate token is skipped
  it('Test 8: Ineligible candidate (already active in queue) is skipped', () => {
    const db = getDb();
    db.prepare("UPDATE services SET max_capacity = 2 WHERE id = 'srv-lp'").run();
    db.prepare("DELETE FROM tokens WHERE service_id = 'srv-lp'").run();

    // Student usr-student-aarav has an active WAITING token and a waitlisted token
    db.prepare(`
      INSERT INTO tokens (id, token_number, student_id, student_name, service_id, priority, status, created_at)
      VALUES 
        ('tkn-active-stu1', 'LP-001', 'usr-student-aarav', 'Aarav Sharma', 'srv-lp', 'NORMAL', 'WAITING', datetime('now', '-10 minutes')),
        ('tkn-wl-stu1', 'LP-002', 'usr-student-aarav', 'Aarav Sharma', 'srv-lp', 'URGENT', 'WAITLISTED', datetime('now', '-5 minutes')),
        ('tkn-wl-stu2', 'LP-003', 'usr-student-ananya', 'Ananya Patel', 'srv-lp', 'NORMAL', 'WAITLISTED', datetime('now', '-4 minutes'))
    `).run();

    const result = queueEngine.evaluateAndPromoteWaitlist('srv-lp');
    expect(result.promotedTokens.length).toBe(1);
    // Aarav skipped due to active duplicate; Ananya promoted
    expect(result.promotedTokens[0].id).toBe('tkn-wl-stu2');
  });

  // TEST 9 — Multiple available slots (3 slots, 5 candidates)
  it('Test 9: Multiple available slots -> exactly 3 promoted, 2 remain WAITLISTED', () => {
    const db = getDb();
    db.prepare("UPDATE services SET max_capacity = 3 WHERE id = 'srv-lp'").run();
    db.prepare("DELETE FROM tokens WHERE service_id = 'srv-lp'").run();

    for (let i = 1; i <= 5; i++) {
      db.prepare(`
        INSERT INTO tokens (id, token_number, student_name, service_id, priority, status, created_at)
        VALUES (?, ?, ?, 'srv-lp', 'NORMAL', 'WAITLISTED', datetime('now', ?))
      `).run(`tkn-multi-${i}`, `LP-00${i}`, `Student ${i}`, `-${10 - i} minutes`);
    }

    const result = queueEngine.evaluateAndPromoteWaitlist('srv-lp');
    expect(result.promotedTokens.length).toBe(3);
    expect(result.promotedTokens.map(t => t.id)).toEqual(['tkn-multi-1', 'tkn-multi-2', 'tkn-multi-3']);

    const remainingWaitlisted = db.prepare("SELECT COUNT(*) as count FROM tokens WHERE service_id = 'srv-lp' AND status = 'WAITLISTED'").get() as any;
    expect(remainingWaitlisted.count).toBe(2);
  });

  // TEST 10 — Concurrent promotion (1 available slot)
  it('Test 10: Concurrent promotion (1 slot) -> exactly 1 candidate promoted, 0 double promotions', async () => {
    const db = getDb();
    db.prepare("UPDATE services SET max_capacity = 1 WHERE id = 'srv-lp'").run();
    db.prepare("DELETE FROM tokens WHERE service_id = 'srv-lp'").run();

    for (let i = 1; i <= 4; i++) {
      db.prepare(`
        INSERT INTO tokens (id, token_number, student_name, service_id, priority, status, created_at)
        VALUES (?, ?, ?, 'srv-lp', 'NORMAL', 'WAITLISTED', datetime('now', ?))
      `).run(`tkn-race-${i}`, `LP-00${i}`, `Race Student ${i}`, `-${10 - i} minutes`);
    }

    // 4 concurrent promotion attempts
    const results = await Promise.all([
      Promise.resolve().then(() => queueEngine.evaluateAndPromoteWaitlist('srv-lp')),
      Promise.resolve().then(() => queueEngine.evaluateAndPromoteWaitlist('srv-lp')),
      Promise.resolve().then(() => queueEngine.evaluateAndPromoteWaitlist('srv-lp')),
      Promise.resolve().then(() => queueEngine.evaluateAndPromoteWaitlist('srv-lp')),
    ]);

    const totalPromoted = results.reduce((acc, r) => acc + r.promotedTokens.length, 0);
    expect(totalPromoted).toBe(1);

    const activeCount = db.prepare("SELECT COUNT(*) as count FROM tokens WHERE service_id = 'srv-lp' AND status = 'WAITING'").get() as any;
    expect(activeCount.count).toBe(1);
  });

  // TEST 11 — Concurrent promotion (multiple slots)
  it('Test 11: Concurrent promotion (3 slots, 10 candidates) -> exactly 3 unique candidates promoted', async () => {
    const db = getDb();
    db.prepare("UPDATE services SET max_capacity = 3 WHERE id = 'srv-lp'").run();
    db.prepare("DELETE FROM tokens WHERE service_id = 'srv-lp'").run();

    for (let i = 1; i <= 10; i++) {
      db.prepare(`
        INSERT INTO tokens (id, token_number, student_name, service_id, priority, status, created_at)
        VALUES (?, ?, ?, 'srv-lp', 'NORMAL', 'WAITLISTED', datetime('now', ?))
      `).run(`tkn-ten-${i}`, `LP-0${i}`, `Student ${i}`, `-${20 - i} minutes`);
    }

    // 5 concurrent requests
    const results = await Promise.all([
      Promise.resolve().then(() => queueEngine.evaluateAndPromoteWaitlist('srv-lp')),
      Promise.resolve().then(() => queueEngine.evaluateAndPromoteWaitlist('srv-lp')),
      Promise.resolve().then(() => queueEngine.evaluateAndPromoteWaitlist('srv-lp')),
      Promise.resolve().then(() => queueEngine.evaluateAndPromoteWaitlist('srv-lp')),
      Promise.resolve().then(() => queueEngine.evaluateAndPromoteWaitlist('srv-lp')),
    ]);

    const allPromotedIds = results.flatMap(r => r.promotedTokens.map(t => t.id));
    const uniquePromotedIds = new Set(allPromotedIds);
    expect(uniquePromotedIds.size).toBe(3);

    const activeCount = db.prepare("SELECT COUNT(*) as count FROM tokens WHERE service_id = 'srv-lp' AND status = 'WAITING'").get() as any;
    expect(activeCount.count).toBe(3);
  });

  // TEST 12 — Candidate invalidated concurrently
  it('Test 12: Candidate invalidated / cancelled concurrently is not activated', async () => {
    const db = getDb();
    db.prepare("UPDATE services SET max_capacity = 1 WHERE id = 'srv-lp'").run();
    db.prepare("DELETE FROM tokens WHERE service_id = 'srv-lp'").run();

    db.prepare(`
      INSERT INTO tokens (id, token_number, student_name, service_id, priority, status, created_at)
      VALUES 
        ('tkn-canc-race', 'LP-001', 'Cancel Race Student', 'srv-lp', 'URGENT', 'WAITLISTED', datetime('now', '-10 minutes')),
        ('tkn-next-race', 'LP-002', 'Next Student', 'srv-lp', 'NORMAL', 'WAITLISTED', datetime('now', '-5 minutes'))
    `).run();

    // Concurrently cancel top candidate and trigger promotion
    await Promise.all([
      Promise.resolve().then(() => queueEngine.cancelToken('tkn-canc-race')),
      Promise.resolve().then(() => queueEngine.evaluateAndPromoteWaitlist('srv-lp')),
    ]);

    const activeToken = db.prepare("SELECT id FROM tokens WHERE service_id = 'srv-lp' AND status = 'WAITING'").get() as any;
    expect(activeToken.id).toBe('tkn-next-race');

    const cancelledToken = db.prepare("SELECT status FROM tokens WHERE id = 'tkn-canc-race'").get() as any;
    expect(cancelledToken.status).toBe('CANCELLED');
  });

  // TEST 13 — Capacity protection under concurrent bookings & promotions
  it('Test 13: Capacity protection -> Active queue count never exceeds max_capacity', () => {
    const db = getDb();
    db.prepare("UPDATE services SET max_capacity = 2 WHERE id = 'srv-lp'").run();
    db.prepare("DELETE FROM tokens WHERE service_id = 'srv-lp'").run();

    // Create 4 tokens via queueEngine.createToken
    const t1 = queueEngine.createToken({ student_name: 'S1', service_id: 'srv-lp', priority: 'NORMAL' });
    const t2 = queueEngine.createToken({ student_name: 'S2', service_id: 'srv-lp', priority: 'NORMAL' });
    const t3 = queueEngine.createToken({ student_name: 'S3', service_id: 'srv-lp', priority: 'NORMAL' });
    const t4 = queueEngine.createToken({ student_name: 'S4', service_id: 'srv-lp', priority: 'NORMAL' });

    expect(t1.token?.status).toBe('WAITING');
    expect(t2.token?.status).toBe('WAITING');
    expect(t3.token?.status).toBe('WAITLISTED');
    expect(t4.token?.status).toBe('WAITLISTED');

    const activeCount = db.prepare("SELECT COUNT(*) as count FROM tokens WHERE service_id = 'srv-lp' AND status IN ('WAITING', 'SERVING', 'HELD')").get() as any;
    expect(activeCount.count).toBe(2);
  });

  // TEST 14 — Real-time event flow & auto-triggering on capacity release
  it('Test 14: Auto-promotion triggers when capacity is freed up via cancelToken', () => {
    const db = getDb();
    db.prepare("UPDATE services SET max_capacity = 1 WHERE id = 'srv-lp'").run();
    db.prepare("DELETE FROM tokens WHERE service_id = 'srv-lp'").run();

    const t1 = queueEngine.createToken({ student_name: 'Active Student', service_id: 'srv-lp', priority: 'NORMAL' });
    const t2 = queueEngine.createToken({ student_name: 'Waitlist Student', service_id: 'srv-lp', priority: 'NORMAL' });

    expect(t1.token?.status).toBe('WAITING');
    expect(t2.token?.status).toBe('WAITLISTED');

    // Cancel active token t1 -> triggers evaluateAndPromoteWaitlist
    const cancelRes = queueEngine.cancelToken(t1.token!.id);
    expect(cancelRes.success).toBe(true);

    // t2 should now be automatically promoted to WAITING!
    const t2Updated = db.prepare('SELECT status FROM tokens WHERE id = ?').get(t2.token!.id) as any;
    expect(t2Updated.status).toBe('WAITING');
  });
});
