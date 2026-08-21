import { describe, it, expect, beforeEach, afterAll } from 'vitest';
import { getDb, closeDb } from '../db/database.js';
import { initializeSchema } from '../db/schema.js';
import { seedDatabase } from '../db/seed.js';
import { queueEngine } from '../services/queueEngine.js';

describe('QueueCraft Database-Level Concurrency & Invariant Test Suite', () => {
  beforeEach(() => {
    process.env.DB_PATH = 'test_concurrency_ts.db';
    initializeSchema();
    seedDatabase();
  });

  afterAll(() => {
    closeDb();
  });

  /**
   * TEST 1 — Two simultaneous NEXT
   * Create: T101 WAITING, T102 WAITING, T103 WAITING
   * Run two NEXT operations concurrently on two open counters.
   * Assert: Two different tokens are assigned; never same token assigned twice.
   */
  it('Test 1: Two simultaneous NEXT assign two distinct tokens without collision', async () => {
    const db = getDb();

    // Ensure 2 counters are OPEN for service srv-lp
    db.prepare("UPDATE counters SET status = 'OPEN' WHERE id IN ('cntr-lp-1', 'cntr-lp-2')").run();
    db.prepare("UPDATE tokens SET status = 'COMPLETED' WHERE service_id = 'srv-lp' AND status = 'SERVING'").run();

    // Clean out existing waiting tokens and seed exactly 3 waiting tokens
    db.prepare("UPDATE tokens SET status = 'COMPLETED' WHERE service_id = 'srv-lp' AND status = 'WAITING'").run();
    db.prepare(`
      INSERT INTO tokens (id, token_number, student_name, service_id, priority, status, created_at)
      VALUES 
        ('tkn-test1-101', 'LP-101', 'Student 101', 'srv-lp', 'NORMAL', 'WAITING', datetime('now', '-3 minutes')),
        ('tkn-test1-102', 'LP-102', 'Student 102', 'srv-lp', 'NORMAL', 'WAITING', datetime('now', '-2 minutes')),
        ('tkn-test1-103', 'LP-103', 'Student 103', 'srv-lp', 'NORMAL', 'WAITING', datetime('now', '-1 minutes'))
    `).run();

    // Trigger two NEXT operations concurrently
    const [res1, res2] = await Promise.all([
      Promise.resolve().then(() => queueEngine.callNextToken('srv-lp', 'cntr-lp-1')),
      Promise.resolve().then(() => queueEngine.callNextToken('srv-lp', 'cntr-lp-2')),
    ]);

    expect(res1.success).toBe(true);
    expect(res2.success).toBe(true);
    expect(res1.token).toBeDefined();
    expect(res2.token).toBeDefined();

    // Assert two different tokens were assigned
    expect(res1.token!.id).not.toBe(res2.token!.id);
    expect(res1.token!.token_number).not.toBe(res2.token!.token_number);

    // Verify database state: each counter has its token in SERVING status
    const serving1 = db.prepare("SELECT * FROM tokens WHERE counter_id = 'cntr-lp-1' AND status = 'SERVING'").get() as any;
    const serving2 = db.prepare("SELECT * FROM tokens WHERE counter_id = 'cntr-lp-2' AND status = 'SERVING'").get() as any;

    expect(serving1).toBeDefined();
    expect(serving2).toBeDefined();
    expect(serving1.id).not.toBe(serving2.id);

    // Assert the 3rd token is still WAITING
    const remainingWaiting = db.prepare("SELECT COUNT(*) as cnt FROM tokens WHERE service_id = 'srv-lp' AND status = 'WAITING'").get() as any;
    expect(remainingWaiting.cnt).toBe(1);
  });

  /**
   * TEST 2 — Multiple simultaneous NEXT (10 concurrent NEXT requests)
   * Create 10 waiting tokens and 10 counters. Run 10 NEXT operations concurrently.
   * Assert: No duplicate token assignments; no token assigned twice; correct final states.
   */
  it('Test 2: Multiple simultaneous NEXT (10 concurrent operations) assign 10 distinct tokens', async () => {
    const db = getDb();

    // Clear existing counters & tokens for fresh service
    const serviceId = 'srv-lp';
    db.prepare("UPDATE tokens SET status = 'COMPLETED' WHERE service_id = ?").run(serviceId);

    // Create 10 open counters
    for (let i = 1; i <= 10; i++) {
      db.prepare(`
        INSERT OR REPLACE INTO counters (id, service_id, name, status)
        VALUES (?, ?, ?, 'OPEN')
      `).run(`cntr-multi-${i}`, serviceId, `Counter Multi ${i}`);
    }

    // Create 10 waiting tokens with varying created timestamps
    for (let i = 1; i <= 10; i++) {
      db.prepare(`
        INSERT INTO tokens (id, token_number, student_name, service_id, priority, status, created_at)
        VALUES (?, ?, ?, ?, 'NORMAL', 'WAITING', datetime('now', '-' || ? || ' minutes'))
      `).run(`tkn-multi-${i}`, `LP-20${i}`, `Student Multi ${i}`, serviceId, 15 - i);
    }

    // Execute 10 concurrent NEXT requests
    const promises = Array.from({ length: 10 }, (_, i) =>
      Promise.resolve().then(() => queueEngine.callNextToken(serviceId, `cntr-multi-${i + 1}`))
    );

    const results = await Promise.all(promises);

    const successfulResults = results.filter(r => r.success);
    expect(successfulResults.length).toBe(10);

    const claimedTokenIds = successfulResults.map(r => r.token!.id);
    const uniqueClaimedIds = new Set(claimedTokenIds);

    // Invariant: No duplicate token assignments
    expect(uniqueClaimedIds.size).toBe(10);

    // Invariant: Number of remaining waiting tokens is 0
    const remainingWaiting = db.prepare("SELECT COUNT(*) as cnt FROM tokens WHERE service_id = ? AND status = 'WAITING'").get(serviceId) as any;
    expect(remainingWaiting.cnt).toBe(0);

    // Invariant: Exactly 10 tokens in SERVING status across the 10 counters
    const totalServing = db.prepare("SELECT COUNT(*) as cnt FROM tokens WHERE service_id = ? AND status = 'SERVING'").get(serviceId) as any;
    expect(totalServing.cnt).toBe(10);
  });

  /**
   * TEST 3 — NEXT + CANCEL concurrently
   * Run NEXT and CANCEL concurrently against a relevant waiting token.
   * Assert: Valid final state, CANCELLED token never becomes SERVING.
   */
  it('Test 3: Concurrent NEXT and CANCEL guarantees cancelled token never becomes SERVING', async () => {
    const db = getDb();
    const serviceId = 'srv-lp';
    const counterId = 'cntr-lp-2';

    db.prepare("UPDATE counters SET status = 'OPEN' WHERE id = ?").run(counterId);
    db.prepare("UPDATE tokens SET status = 'COMPLETED' WHERE service_id = ?").run(serviceId);

    // Seed a single waiting token
    const targetTokenId = 'tkn-race-cancel';
    db.prepare(`
      INSERT INTO tokens (id, token_number, student_name, service_id, priority, status, created_at)
      VALUES (?, 'LP-301', 'Race Student', ?, 'NORMAL', 'WAITING', datetime('now'))
    `).run(targetTokenId, serviceId);

    // Run NEXT and CANCEL concurrently
    const [nextRes, cancelRes] = await Promise.all([
      Promise.resolve().then(() => queueEngine.callNextToken(serviceId, counterId)),
      Promise.resolve().then(() => queueEngine.cancelToken(targetTokenId)),
    ]);

    const finalToken = db.prepare('SELECT * FROM tokens WHERE id = ?').get(targetTokenId) as any;

    // Check invariants
    if (cancelRes.success) {
      // If CANCEL won, token MUST be CANCELLED and NOT SERVING
      expect(finalToken.status).toBe('CANCELLED');
      expect(finalToken.status).not.toBe('SERVING');
    } else {
      // If NEXT won, token is SERVING and CANCEL failed
      expect(nextRes.success).toBe(true);
      expect(finalToken.status).toBe('SERVING');
      expect(cancelRes.error).toBeDefined();
    }

    // Invariant: Token is in exactly ONE valid state (never invalid)
    expect(['SERVING', 'CANCELLED']).toContain(finalToken.status);
  });

  /**
   * TEST 4 — NEXT + COMPLETE concurrently
   * Run concurrently. Assert final state is valid and no stale update overwrites another committed state.
   */
  it('Test 4: Concurrent NEXT and COMPLETE maintains state consistency without stale overwrites', async () => {
    const db = getDb();
    const serviceId = 'srv-lp';
    const counterId = 'cntr-lp-2';

    db.prepare("UPDATE counters SET status = 'OPEN' WHERE id = ?").run(counterId);
    db.prepare("UPDATE tokens SET status = 'COMPLETED' WHERE service_id = ?").run(serviceId);

    // Setup: Counter 2 is SERVING token T1, and T2 is WAITING
    const activeTokenId = 'tkn-serving-active';
    const waitingTokenId = 'tkn-waiting-next';

    db.prepare(`
      INSERT INTO tokens (id, token_number, student_name, service_id, counter_id, priority, status, created_at, started_at)
      VALUES (?, 'LP-401', 'Active Student', ?, ?, 'NORMAL', 'SERVING', datetime('now', '-5 minutes'), datetime('now', '-5 minutes'))
    `).run(activeTokenId, serviceId, counterId);

    db.prepare(`
      INSERT INTO tokens (id, token_number, student_name, service_id, priority, status, created_at)
      VALUES (?, 'LP-402', 'Waiting Student', ?, 'NORMAL', 'WAITING', datetime('now', '-2 minutes'))
    `).run(waitingTokenId, serviceId);

    // Run COMPLETE(T1) and NEXT(counter) concurrently
    const [completeRes, nextRes] = await Promise.all([
      Promise.resolve().then(() => queueEngine.completeToken(activeTokenId, counterId)),
      Promise.resolve().then(() => queueEngine.callNextToken(serviceId, counterId)),
    ]);

    const finalT1 = db.prepare('SELECT * FROM tokens WHERE id = ?').get(activeTokenId) as any;
    const finalT2 = db.prepare('SELECT * FROM tokens WHERE id = ?').get(waitingTokenId) as any;

    // T1 must be COMPLETED
    expect(finalT1.status).toBe('COMPLETED');

    // T2 must be either SERVING (if NEXT ran after COMPLETE) or WAITING (if NEXT ran before COMPLETE and got rejected)
    expect(['SERVING', 'WAITING']).toContain(finalT2.status);

    // Invariant: Counter has at most ONE active serving token
    const servingAtCounter = db.prepare("SELECT COUNT(*) as cnt FROM tokens WHERE counter_id = ? AND status = 'SERVING'").get(counterId) as any;
    expect(servingAtCounter.cnt).toBeLessThanOrEqual(1);
  });

  /**
   * TEST 5 — COMPLETE + SKIP concurrently
   * Run concurrently against the same serving token.
   * Assert: Token ends in only one valid state; no contradictory records.
   */
  it('Test 5: Concurrent COMPLETE and SKIP on the same token results in exactly one winner', async () => {
    const db = getDb();
    const counterId = 'cntr-lp-2';
    const serviceId = 'srv-lp';

    db.prepare("UPDATE counters SET status = 'OPEN' WHERE id = ?").run(counterId);
    db.prepare("UPDATE tokens SET status = 'COMPLETED' WHERE service_id = ?").run(serviceId);

    const tokenId = 'tkn-race-complete-skip';
    db.prepare(`
      INSERT INTO tokens (id, token_number, student_name, service_id, counter_id, priority, status, created_at, started_at)
      VALUES (?, 'LP-501', 'Contested Student', ?, ?, 'NORMAL', 'SERVING', datetime('now', '-10 minutes'), datetime('now', '-5 minutes'))
    `).run(tokenId, serviceId, counterId);

    const [compRes, skipRes] = await Promise.all([
      Promise.resolve().then(() => queueEngine.completeToken(tokenId, counterId)),
      Promise.resolve().then(() => queueEngine.skipToken(tokenId, counterId)),
    ]);

    // Exactly one must succeed and one must fail
    const successCount = [compRes.success, skipRes.success].filter(Boolean).length;
    expect(successCount).toBe(1);

    const finalToken = db.prepare('SELECT * FROM tokens WHERE id = ?').get(tokenId) as any;
    expect(['COMPLETED', 'SKIPPED']).toContain(finalToken.status);
    expect(finalToken.status).not.toBe('SERVING');
  });

  /**
   * TEST 6 — HOLD + RESUME concurrently
   * Run concurrently. Assert no invalid state transitions; final state is valid.
   */
  it('Test 6: Concurrent HOLD and RESUME prevents invalid transitions', async () => {
    const db = getDb();
    const counterId = 'cntr-lp-2';
    const serviceId = 'srv-lp';

    db.prepare("UPDATE counters SET status = 'OPEN' WHERE id = ?").run(counterId);
    db.prepare("UPDATE tokens SET status = 'COMPLETED' WHERE service_id = ?").run(serviceId);

    const tokenId = 'tkn-race-hold-resume';
    db.prepare(`
      INSERT INTO tokens (id, token_number, student_name, service_id, counter_id, priority, status, created_at, started_at)
      VALUES (?, 'LP-601', 'Hold Resume Student', ?, ?, 'NORMAL', 'SERVING', datetime('now', '-5 minutes'), datetime('now', '-3 minutes'))
    `).run(tokenId, serviceId, counterId);

    const [holdRes, resumeRes] = await Promise.all([
      Promise.resolve().then(() => queueEngine.holdToken(tokenId, counterId)),
      Promise.resolve().then(() => queueEngine.resumeToken(tokenId, counterId)),
    ]);

    const finalToken = db.prepare('SELECT * FROM tokens WHERE id = ?').get(tokenId) as any;

    // The final state must be either HELD or SERVING, never corrupted
    expect(['HELD', 'SERVING']).toContain(finalToken.status);
  });

  /**
   * TEST 7 — Cancelled token cannot be reactivated
   * Create a token, cancel it, then create concurrent operations attempting to serve or resume it.
   * Assert: CANCELLED != SERVING.
   */
  it('Test 7: Cancelled token cannot be reactivated by any concurrent operations', async () => {
    const db = getDb();
    const serviceId = 'srv-lp';
    const counterId = 'cntr-lp-2';

    db.prepare("UPDATE counters SET status = 'OPEN' WHERE id = ?").run(counterId);
    db.prepare("UPDATE tokens SET status = 'COMPLETED' WHERE service_id = ?").run(serviceId);

    const cancelledTokenId = 'tkn-already-cancelled';
    db.prepare(`
      INSERT INTO tokens (id, token_number, student_name, service_id, counter_id, priority, status, created_at)
      VALUES (?, 'LP-701', 'Cancelled Student', ?, ?, 'NORMAL', 'CANCELLED', datetime('now', '-10 minutes'))
    `).run(cancelledTokenId, serviceId, counterId);

    // Attempt concurrent operations to reactivate or mutate it
    const [callNextRes, resumeRes, completeRes, holdRes] = await Promise.all([
      Promise.resolve().then(() => queueEngine.callNextToken(serviceId, counterId)),
      Promise.resolve().then(() => queueEngine.resumeToken(cancelledTokenId, counterId)),
      Promise.resolve().then(() => queueEngine.completeToken(cancelledTokenId, counterId)),
      Promise.resolve().then(() => queueEngine.holdToken(cancelledTokenId, counterId)),
    ]);

    expect(callNextRes.success).toBe(false);
    expect(resumeRes.success).toBe(false);
    expect(completeRes.success).toBe(false);
    expect(holdRes.success).toBe(false);

    const finalToken = db.prepare('SELECT * FROM tokens WHERE id = ?').get(cancelledTokenId) as any;
    expect(finalToken.status).toBe('CANCELLED');
  });

  /**
   * TEST 8 — Completed token cannot be reactivated
   * Assert COMPLETED != SERVING under any concurrent attempts.
   */
  it('Test 8: Completed token cannot be reactivated or re-served', async () => {
    const db = getDb();
    const serviceId = 'srv-lp';
    const counterId = 'cntr-lp-2';

    db.prepare("UPDATE counters SET status = 'OPEN' WHERE id = ?").run(counterId);
    db.prepare("UPDATE tokens SET status = 'COMPLETED' WHERE service_id = ?").run(serviceId);

    const completedTokenId = 'tkn-already-completed';
    db.prepare(`
      INSERT INTO tokens (id, token_number, student_name, service_id, counter_id, priority, status, created_at, started_at, completed_at)
      VALUES (?, 'LP-801', 'Done Student', ?, ?, 'NORMAL', 'COMPLETED', datetime('now', '-20 minutes'), datetime('now', '-10 minutes'), datetime('now', '-5 minutes'))
    `).run(completedTokenId, serviceId, counterId);

    const [callNextRes, resumeRes, completeRes, holdRes, cancelRes, skipRes] = await Promise.all([
      Promise.resolve().then(() => queueEngine.callNextToken(serviceId, counterId)),
      Promise.resolve().then(() => queueEngine.resumeToken(completedTokenId, counterId)),
      Promise.resolve().then(() => queueEngine.completeToken(completedTokenId, counterId)),
      Promise.resolve().then(() => queueEngine.holdToken(completedTokenId, counterId)),
      Promise.resolve().then(() => queueEngine.cancelToken(completedTokenId)),
      Promise.resolve().then(() => queueEngine.skipToken(completedTokenId, counterId)),
    ]);

    expect(callNextRes.success).toBe(false);
    expect(resumeRes.success).toBe(false);
    expect(completeRes.success).toBe(false);
    expect(holdRes.success).toBe(false);
    expect(cancelRes.success).toBe(false);
    expect(skipRes.success).toBe(false);

    const finalToken = db.prepare('SELECT * FROM tokens WHERE id = ?').get(completedTokenId) as any;
    expect(finalToken.status).toBe('COMPLETED');
  });

  /**
   * TEST 9 — Queue position & database constraint uniqueness
   * Enforce that no two active tokens have the same SERVING slot at a counter.
   */
  it('Test 9: Database constraints enforce at most ONE SERVING token per counter', () => {
    const db = getDb();
    const counterId = 'cntr-lp-2';
    const serviceId = 'srv-lp';

    db.prepare("UPDATE tokens SET status = 'COMPLETED' WHERE service_id = ?").run(serviceId);

    // Insert first SERVING token
    db.prepare(`
      INSERT INTO tokens (id, token_number, student_name, service_id, counter_id, priority, status, created_at, started_at)
      VALUES ('tkn-c-1', 'LP-901', 'First Serv', ?, ?, 'NORMAL', 'SERVING', datetime('now'), datetime('now'))
    `).run(serviceId, counterId);

    // Attempting to insert a 2nd SERVING token on the SAME counter MUST fail via SQLite UNIQUE constraint
    expect(() => {
      db.prepare(`
        INSERT INTO tokens (id, token_number, student_name, service_id, counter_id, priority, status, created_at, started_at)
        VALUES ('tkn-c-2', 'LP-902', 'Second Serv', ?, ?, 'NORMAL', 'SERVING', datetime('now'), datetime('now'))
      `).run(serviceId, counterId);
    }).toThrow(/UNIQUE constraint failed/);

    const servingTokens = db.prepare("SELECT * FROM tokens WHERE counter_id = ? AND status = 'SERVING'").all(counterId);
    expect(servingTokens.length).toBe(1);
  });

  /**
   * TEST 10 — Rollback safety
   * Force a failure during a queue mutation. Verify no partially updated token, no orphaned serving record.
   */
  it('Test 10: Rollback safety ensures atomic failure with zero partial database mutations', () => {
    const db = getDb();
    const serviceId = 'srv-lp';
    const counterId = 'cntr-lp-2';

    db.prepare("UPDATE counters SET status = 'OPEN' WHERE id = ?").run(counterId);
    db.prepare("UPDATE tokens SET status = 'COMPLETED' WHERE service_id = ?").run(serviceId);

    const tokenId = 'tkn-rollback-test';
    db.prepare(`
      INSERT INTO tokens (id, token_number, student_name, service_id, priority, status, created_at)
      VALUES (?, 'LP-999', 'Rollback Student', ?, 'NORMAL', 'WAITING', datetime('now'))
    `).run(tokenId, serviceId);

    // Execute a transaction that attempts to update the token and then encounters a deliberate error
    expect(() => {
      const tx = db.transaction(() => {
        db.prepare("UPDATE tokens SET status = 'SERVING', counter_id = ? WHERE id = ?").run(counterId, tokenId);
        // Force failure
        throw new Error('Simulated failure during multi-step operation');
      }).immediate;
      tx();
    }).toThrow('Simulated failure during multi-step operation');

    // Assert that the token remains WAITING and counter_id is NULL (no partial mutation)
    const token = db.prepare('SELECT * FROM tokens WHERE id = ?').get(tokenId) as any;
    expect(token.status).toBe('WAITING');
    expect(token.counter_id).toBeNull();
  });
});
