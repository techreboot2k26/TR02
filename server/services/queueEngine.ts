import { getDb } from '../db/database.js';

export type TokenStatus = 'WAITING' | 'SERVING' | 'HELD' | 'COMPLETED' | 'SKIPPED' | 'CANCELLED' | 'WAITLISTED' | 'PROMOTED' | 'EXPIRED';

export interface TokenRecord {
  id: string;
  token_number: string;
  student_id: string | null;
  student_name: string;
  student_email: string | null;
  service_id: string;
  counter_id: string | null;
  priority: 'NORMAL' | 'HIGH' | 'PRIORITY' | 'URGENT';
  status: TokenStatus;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  skipped_at: string | null;
  held_at: string | null;
  notes: string | null;
}

export interface QueueEngineResult {
  success: boolean;
  token?: TokenRecord;
  error?: string;
}

export interface TokenDetails extends TokenRecord {
  position: number;
  peopleAhead: number;
  estimatedWaitMinutes: number;
}

export interface IQueueEngine {
  getNextEligibleToken(serviceId: string, counterId?: string): TokenRecord | null;
  callNextToken(serviceId: string, counterId: string): QueueEngineResult;
  completeToken(tokenId: string, counterId: string): QueueEngineResult;
  holdToken(tokenId: string, counterId: string): QueueEngineResult;
  resumeToken(tokenId: string, counterId: string): QueueEngineResult;
  skipToken(tokenId: string, counterId: string): QueueEngineResult;
  getWaitingQueue(serviceId: string): TokenRecord[];
  getCurrentServingToken(counterId: string): TokenRecord | null;

  // New integration services
  createToken(data: {
    student_name: string;
    student_email?: string | null;
    student_id?: string | null;
    service_id: string;
    priority: string;
    notes?: string | null;
  }): QueueEngineResult & { token?: TokenDetails };
  cancelToken(tokenId: string): QueueEngineResult;
  getTokenPositionDetails(tokenId: string): { position: number; peopleAhead: number; estimatedWaitMinutes: number } | null;
  getWaitingQueueWithDetails(serviceId: string): TokenDetails[];
  promoteNextToken(serviceId: string, counterId: string): QueueEngineResult;
  getWaitlistQueue(serviceId: string): TokenRecord[];
  evaluateAndPromoteWaitlist(serviceId: string): { success: boolean; promotedTokens: TokenRecord[]; availableSlots: number; error?: string };
}

/**
 * Starvation-Prevention Sorting function
 * Sorts all waiting tokens for a service by:
 * 1. Effective priority (Base priority + starvation boost)
 * 2. Original creation time (FIFO within effective priority)
 * 3. Token ID (stable tie-breaker)
 */
export function getSortedWaitingTokens(serviceId: string): TokenRecord[] {
  const db = getDb();
  const tokens = db.prepare(`
    SELECT * FROM tokens
    WHERE service_id = ? AND status = 'WAITING'
  `).all(serviceId) as TokenRecord[];

  const threshold = Number(process.env.PRIORITY_WAIT_THRESHOLD_MINUTES) || 15;
  const now = new Date();

  const getPriorityVal = (p: string) => {
    switch (p) {
      case 'URGENT': return 3;
      case 'HIGH':
      case 'PRIORITY': return 2;
      case 'NORMAL':
      default: return 1;
    }
  };

  const getEffectivePriority = (token: TokenRecord) => {
    const created = new Date(token.created_at);
    const elapsedMinutes = Math.max(0, (now.getTime() - created.getTime()) / (60 * 1000));
    const baseVal = getPriorityVal(token.priority);
    const boost = Math.floor(elapsedMinutes / threshold);
    return baseVal + boost;
  };

  const tokensWithKeys = tokens.map(t => ({
    token: t,
    effectivePriority: getEffectivePriority(t),
    createdTime: new Date(t.created_at).getTime()
  }));

  tokensWithKeys.sort((a, b) => {
    if (b.effectivePriority !== a.effectivePriority) {
      return b.effectivePriority - a.effectivePriority;
    }
    if (a.createdTime !== b.createdTime) {
      return a.createdTime - b.createdTime;
    }
    return a.token.id.localeCompare(b.token.id);
  });

  return tokensWithKeys.map(tk => tk.token);
}

/**
 * Deterministic Fairness Policy for Waitlist Promotion:
 * 1. Primary: Effective priority (Base priority + starvation aging factor)
 * 2. Secondary: Waiting duration (Earlier created_at timestamp wins)
 * 3. Tertiary: Stable unique ID (lexicographical comparison on token.id)
 */
export function getSortedWaitlistTokens(serviceId: string): TokenRecord[] {
  const db = getDb();
  const tokens = db.prepare(`
    SELECT * FROM tokens
    WHERE service_id = ? AND status = 'WAITLISTED'
  `).all(serviceId) as TokenRecord[];

  const threshold = Number(process.env.PRIORITY_WAIT_THRESHOLD_MINUTES) || 15;
  const now = new Date();

  const getPriorityVal = (p: string) => {
    switch (p) {
      case 'URGENT': return 3;
      case 'HIGH':
      case 'PRIORITY': return 2;
      case 'NORMAL':
      default: return 1;
    }
  };

  const getEffectivePriority = (token: TokenRecord) => {
    const created = new Date(token.created_at);
    const elapsedMinutes = Math.max(0, (now.getTime() - created.getTime()) / (60 * 1000));
    const baseVal = getPriorityVal(token.priority);
    const boost = Math.floor(elapsedMinutes / threshold);
    return baseVal + boost;
  };

  const tokensWithKeys = tokens.map(t => ({
    token: t,
    effectivePriority: getEffectivePriority(t),
    createdTime: new Date(t.created_at).getTime()
  }));

  tokensWithKeys.sort((a, b) => {
    if (b.effectivePriority !== a.effectivePriority) {
      return b.effectivePriority - a.effectivePriority;
    }
    if (a.createdTime !== b.createdTime) {
      return a.createdTime - b.createdTime;
    }
    return a.token.id.localeCompare(b.token.id);
  });

  return tokensWithKeys.map(tk => tk.token);
}

export class DefaultQueueEngine implements IQueueEngine {
  /**
   * Retrieves the next eligible token from the queue based on priority + starvation algorithm.
   */
  getNextEligibleToken(serviceId: string): TokenRecord | null {
    const sorted = getSortedWaitingTokens(serviceId);
    return sorted.length > 0 ? sorted[0] : null;
  }

  /**
   * Generates a sequential token number per service (e.g. LP-046).
   */
  private generateTokenNumber(serviceId: string): string {
    const db = getDb();
    const service = db.prepare('SELECT code FROM services WHERE id = ?').get(serviceId) as { code: string } | undefined;
    if (!service) throw new Error('Service not found');

    const code = service.code;
    const maxToken = db.prepare(`
      SELECT token_number FROM tokens
      WHERE service_id = ? AND token_number LIKE ?
      ORDER BY ROWID DESC
      LIMIT 1
    `).get(serviceId, `${code}-%`) as { token_number: string } | undefined;

    let nextNum = 1;
    if (maxToken) {
      const parts = maxToken.token_number.split('-');
      if (parts.length === 2) {
        const num = parseInt(parts[1], 10);
        if (!isNaN(num)) {
          nextNum = num + 1;
        }
      }
    }

    const suffix = String(nextNum).padStart(3, '0');
    return `${code}-${suffix}`;
  }

  /**
   * Calculate average service duration dynamically from completed tokens.
   */
  private getAverageServiceTime(serviceId: string): number {
    const db = getDb();
    const avgTimeResult = db.prepare(`
      SELECT AVG((julianday(completed_at) - julianday(started_at)) * 24 * 60) as avg_mins
      FROM tokens
      WHERE service_id = ? AND status = 'COMPLETED' AND started_at IS NOT NULL AND completed_at IS NOT NULL
    `).get(serviceId) as any;

    return avgTimeResult?.avg_mins ? Math.max(1, Math.round(avgTimeResult.avg_mins * 10) / 10) : 4.5;
  }

  /**
   * Fetch live position, people ahead, and estimated wait minutes for a token.
   */
  getTokenPositionDetails(tokenId: string): { position: number; peopleAhead: number; estimatedWaitMinutes: number } | null {
    const db = getDb();
    const token = db.prepare('SELECT * FROM tokens WHERE id = ?').get(tokenId) as TokenRecord | undefined;
    if (!token) return null;

    if (token.status === 'SERVING') {
      return { position: 0, peopleAhead: 0, estimatedWaitMinutes: 0 };
    }

    if (token.status === 'WAITLISTED') {
      const waitlist = getSortedWaitlistTokens(token.service_id);
      const idx = waitlist.findIndex(t => t.id === tokenId);
      if (idx === -1) return null;

      const peopleAhead = idx;
      const position = idx + 1;
      const avgServiceTime = this.getAverageServiceTime(token.service_id);
      return {
        position,
        peopleAhead,
        estimatedWaitMinutes: Math.round((position * avgServiceTime * 1.5) * 10) / 10
      };
    }

    if (token.status !== 'WAITING') {
      return { position: -1, peopleAhead: 0, estimatedWaitMinutes: 0 };
    }

    const sortedQueue = getSortedWaitingTokens(token.service_id);
    const idx = sortedQueue.findIndex(t => t.id === tokenId);
    if (idx === -1) return null;

    const peopleAhead = idx;
    const position = idx + 1;
    const avgServiceTime = this.getAverageServiceTime(token.service_id);

    const openCounters = db.prepare(`
      SELECT id FROM counters WHERE service_id = ? AND status = 'OPEN'
    `).all(token.service_id) as Array<{ id: string }>;

    let activeTokenRemainingTime = 0;
    const now = new Date().getTime();

    for (const c of openCounters) {
      const activeServing = db.prepare(`
        SELECT started_at FROM tokens WHERE counter_id = ? AND status = 'SERVING' LIMIT 1
      `).get(c.id) as { started_at: string | null } | undefined;

      if (activeServing && activeServing.started_at) {
        const startedTime = new Date(activeServing.started_at).getTime();
        const elapsedMins = Math.max(0, (now - startedTime) / (60 * 1000));
        activeTokenRemainingTime += Math.max(0, avgServiceTime - elapsedMins);
      }
    }

    const numCounters = openCounters.length || 1;
    const waitTime = (peopleAhead * avgServiceTime) / numCounters + (activeTokenRemainingTime / numCounters);

    return {
      position,
      peopleAhead,
      estimatedWaitMinutes: Math.round(waitTime * 10) / 10
    };
  }

  /**
   * Retrieve waiting list decorated with live position and wait time metrics.
   */
  getWaitingQueueWithDetails(serviceId: string): TokenDetails[] {
    const sorted = getSortedWaitingTokens(serviceId);
    return sorted.map(token => {
      const details = this.getTokenPositionDetails(token.id)!;
      return {
        ...token,
        ...details
      };
    });
  }

  /**
   * CREATE TOKEN Operation with automatic capacity/waitlist check
   */
  createToken(data: {
    student_name: string;
    student_email?: string | null;
    student_id?: string | null;
    service_id: string;
    priority: string;
    notes?: string | null;
  }): QueueEngineResult & { token?: TokenDetails } {
    const db = getDb();

    const service = db.prepare('SELECT id, max_capacity FROM services WHERE id = ?').get(data.service_id) as any;
    if (!service) {
      return { success: false, error: 'Service not found' };
    }

    const allowedPriorities = ['NORMAL', 'HIGH', 'PRIORITY', 'URGENT'];
    if (!allowedPriorities.includes(data.priority)) {
      return { success: false, error: `Invalid priority level: '${data.priority}'.` };
    }

    let createdToken: TokenRecord | null = null;
    let errorMessage: string | null = null;

    const transaction = db.transaction(() => {
      if (data.student_id) {
        const existingActive = db.prepare(`
          SELECT * FROM tokens
          WHERE student_id = ? AND service_id = ? AND status IN ('WAITING', 'SERVING', 'HELD', 'WAITLISTED')
          LIMIT 1
        `).get(data.student_id, data.service_id) as TokenRecord | undefined;

        if (existingActive) {
          errorMessage = `Active token ${existingActive.token_number} already exists for this student in the queue.`;
          return;
        }
      }

      // Check active queue capacity for this service
      const maxCapacity = service.max_capacity !== null && service.max_capacity !== undefined ? service.max_capacity : (Number(process.env.MAX_QUEUE_CAPACITY) || 10);
      const activeCountResult = db.prepare(`
        SELECT COUNT(*) as count FROM tokens
        WHERE service_id = ? AND status IN ('WAITING', 'SERVING', 'HELD')
      `).get(data.service_id) as any;

      const activeCount = activeCountResult?.count || 0;
      const initialStatus: TokenStatus = activeCount >= maxCapacity ? 'WAITLISTED' : 'WAITING';

      const id = 'tkn-' + Math.random().toString(36).substr(2, 9);
      const tokenNumber = this.generateTokenNumber(data.service_id);
      const now = new Date().toISOString();

      db.prepare(`
        INSERT INTO tokens (
          id, token_number, student_id, student_name, student_email, service_id,
          priority, status, created_at, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      `).run(
        id,
        tokenNumber,
        data.student_id || null,
        data.student_name,
        data.student_email || null,
        data.service_id,
        data.priority,
        initialStatus,
        now,
        data.notes || null
      );

      createdToken = db.prepare('SELECT * FROM tokens WHERE id = ?').get(id) as TokenRecord;
    }).immediate;

    transaction();

    if (errorMessage) {
      return { success: false, error: errorMessage };
    }

    if (!createdToken) {
      return { success: false, error: 'Failed to create token' };
    }

    const details = this.getTokenPositionDetails((createdToken as TokenRecord).id)!;

    return {
      success: true,
      token: {
        ...(createdToken as TokenRecord),
        ...details
      }
    };
  }

  /**
   * CANCEL TOKEN Operation
   */
  cancelToken(tokenId: string): QueueEngineResult {
    const db = getDb();
    let resultToken: TokenRecord | null = null;
    let errorMessage: string | null = null;
    let serviceIdToPromote: string | null = null;

    const transaction = db.transaction(() => {
      const token = db.prepare('SELECT * FROM tokens WHERE id = ?').get(tokenId) as TokenRecord | undefined;
      if (!token) {
        errorMessage = 'Token not found';
        return;
      }

      if (token.status !== 'WAITING' && token.status !== 'HELD' && token.status !== 'WAITLISTED') {
        errorMessage = `Invalid transition: Cannot cancel token with status '${token.status}'. Must be 'WAITING', 'HELD', or 'WAITLISTED'.`;
        return;
      }

      const wasActive = token.status === 'WAITING' || token.status === 'HELD';
      const now = new Date().toISOString();
      const updateRes = db.prepare(`
        UPDATE tokens
        SET status = 'CANCELLED', completed_at = ?
        WHERE id = ? AND status IN ('WAITING', 'HELD', 'WAITLISTED')
      `).run(now, tokenId);

      if (updateRes.changes === 0) {
        errorMessage = 'Failed to cancel token: Token state changed concurrently.';
        return;
      }

      resultToken = db.prepare('SELECT * FROM tokens WHERE id = ?').get(tokenId) as TokenRecord;
      if (wasActive) {
        serviceIdToPromote = token.service_id;
      }
    }).immediate;

    transaction();

    if (errorMessage) {
      return { success: false, error: errorMessage };
    }

    // Auto-evaluate waitlist if active capacity freed up
    if (serviceIdToPromote) {
      this.evaluateAndPromoteWaitlist(serviceIdToPromote);
    }

    return { success: true, token: resultToken! };
  }

  /**
   * CALL NEXT TOKEN Operation
   */
  callNextToken(serviceId: string, counterId: string): QueueEngineResult {
    const db = getDb();

    let resultToken: TokenRecord | null = null;
    let errorMessage: string | null = null;

    const transaction = db.transaction(() => {
      const counter = db.prepare('SELECT status FROM counters WHERE id = ?').get(counterId) as any;
      if (!counter) {
        errorMessage = 'Counter not found';
        return;
      }
      if (counter.status !== 'OPEN') {
        errorMessage = `Cannot call next token: Counter is currently ${counter.status}`;
        return;
      }

      const activeServing = db.prepare(`
        SELECT * FROM tokens WHERE counter_id = ? AND status = 'SERVING'
      `).get(counterId) as TokenRecord | undefined;

      if (activeServing) {
        errorMessage = `Counter already has active serving token ${activeServing.token_number}. Complete, hold, or skip it first.`;
        return;
      }

      const candidateTokens = getSortedWaitingTokens(serviceId);
      if (!candidateTokens || candidateTokens.length === 0) {
        errorMessage = 'Waiting queue is currently empty for this service.';
        return;
      }

      const now = new Date().toISOString();

      // Atomically claim the top eligible waiting token
      for (const candidate of candidateTokens) {
        const updateRes = db.prepare(`
          UPDATE tokens
          SET status = 'SERVING', counter_id = ?, started_at = ?
          WHERE id = ? AND status = 'WAITING'
        `).run(counterId, now, candidate.id);

        if (updateRes.changes > 0) {
          resultToken = db.prepare('SELECT * FROM tokens WHERE id = ?').get(candidate.id) as TokenRecord;
          break;
        }
      }

      if (!resultToken) {
        errorMessage = 'Waiting queue is currently empty for this service.';
        return;
      }
    }).immediate;

    transaction();

    if (errorMessage) {
      return { success: false, error: errorMessage };
    }

    if (!resultToken) {
      return { success: false, error: 'Failed to update token state to SERVING' };
    }

    return { success: true, token: resultToken };
  }

  /**
   * COMPLETE TOKEN Operation
   */
  completeToken(tokenId: string, counterId: string): QueueEngineResult {
    const db = getDb();
    let resultToken: TokenRecord | null = null;
    let errorMessage: string | null = null;
    let serviceIdToPromote: string | null = null;

    const transaction = db.transaction(() => {
      const token = db.prepare('SELECT * FROM tokens WHERE id = ?').get(tokenId) as TokenRecord | undefined;
      if (!token) {
        errorMessage = 'Token not found';
        return;
      }

      if (token.status !== 'SERVING') {
        errorMessage = `Invalid state transition: Cannot complete token with status '${token.status}'. Must be 'SERVING'.`;
        return;
      }

      if (token.counter_id !== counterId) {
        errorMessage = 'Unauthorized: Token is assigned to a different counter';
        return;
      }

      const now = new Date().toISOString();

      const updateRes = db.prepare(`
        UPDATE tokens
        SET status = 'COMPLETED', completed_at = ?
        WHERE id = ? AND status = 'SERVING' AND counter_id = ?
      `).run(now, tokenId, counterId);

      if (updateRes.changes === 0) {
        errorMessage = 'State transition failed: Token is no longer in SERVING status for this counter.';
        return;
      }

      resultToken = db.prepare('SELECT * FROM tokens WHERE id = ?').get(tokenId) as TokenRecord;
      serviceIdToPromote = token.service_id;
    }).immediate;

    transaction();

    if (errorMessage) return { success: false, error: errorMessage };

    // Auto-promote waitlist when capacity is freed up
    if (serviceIdToPromote) {
      this.evaluateAndPromoteWaitlist(serviceIdToPromote);
    }

    return { success: true, token: resultToken! };
  }

  /**
   * HOLD TOKEN Operation
   */
  holdToken(tokenId: string, counterId: string): QueueEngineResult {
    const db = getDb();
    let resultToken: TokenRecord | null = null;
    let errorMessage: string | null = null;

    const transaction = db.transaction(() => {
      const token = db.prepare('SELECT * FROM tokens WHERE id = ?').get(tokenId) as TokenRecord | undefined;
      if (!token) {
        errorMessage = 'Token not found';
        return;
      }

      if (token.status !== 'SERVING') {
        errorMessage = `Invalid state transition: Cannot hold token with status '${token.status}'. Must be 'SERVING'.`;
        return;
      }

      if (token.counter_id !== counterId) {
        errorMessage = 'Unauthorized: Token is assigned to a different counter';
        return;
      }

      const now = new Date().toISOString();

      const updateRes = db.prepare(`
        UPDATE tokens
        SET status = 'HELD', held_at = ?
        WHERE id = ? AND status = 'SERVING' AND counter_id = ?
      `).run(now, tokenId, counterId);

      if (updateRes.changes === 0) {
        errorMessage = 'State transition failed: Token is no longer in SERVING status for this counter.';
        return;
      }

      resultToken = db.prepare('SELECT * FROM tokens WHERE id = ?').get(tokenId) as TokenRecord;
    }).immediate;

    transaction();

    if (errorMessage) return { success: false, error: errorMessage };
    return { success: true, token: resultToken! };
  }

  /**
   * RESUME TOKEN Operation
   */
  resumeToken(tokenId: string, counterId: string): QueueEngineResult {
    const db = getDb();
    let resultToken: TokenRecord | null = null;
    let errorMessage: string | null = null;

    const transaction = db.transaction(() => {
      const token = db.prepare('SELECT * FROM tokens WHERE id = ?').get(tokenId) as TokenRecord | undefined;
      if (!token) {
        errorMessage = 'Token not found';
        return;
      }

      if (token.status !== 'HELD') {
        errorMessage = `Invalid state transition: Cannot resume token with status '${token.status}'. Must be 'HELD'.`;
        return;
      }

      const activeServing = db.prepare(`
        SELECT * FROM tokens WHERE counter_id = ? AND status = 'SERVING'
      `).get(counterId) as TokenRecord | undefined;

      if (activeServing) {
        errorMessage = `Cannot resume token: Counter already has active serving token ${activeServing.token_number}.`;
        return;
      }

      const now = new Date().toISOString();

      const updateRes = db.prepare(`
        UPDATE tokens
        SET status = 'SERVING', counter_id = ?, started_at = ?
        WHERE id = ? AND status = 'HELD'
      `).run(counterId, now, tokenId);

      if (updateRes.changes === 0) {
        errorMessage = 'State transition failed: Token is no longer in HELD status.';
        return;
      }

      resultToken = db.prepare('SELECT * FROM tokens WHERE id = ?').get(tokenId) as TokenRecord;
    }).immediate;

    transaction();

    if (errorMessage) return { success: false, error: errorMessage };
    return { success: true, token: resultToken! };
  }

  /**
   * SKIP TOKEN Operation
   */
  skipToken(tokenId: string, counterId: string): QueueEngineResult {
    const db = getDb();
    let resultToken: TokenRecord | null = null;
    let errorMessage: string | null = null;
    let serviceIdToPromote: string | null = null;

    const transaction = db.transaction(() => {
      const token = db.prepare('SELECT * FROM tokens WHERE id = ?').get(tokenId) as TokenRecord | undefined;
      if (!token) {
        errorMessage = 'Token not found';
        return;
      }

      if (token.status !== 'SERVING' && token.status !== 'HELD' && token.status !== 'WAITING') {
        errorMessage = `Invalid state transition: Cannot skip token with status '${token.status}'.`;
        return;
      }

      const wasActive = token.status === 'SERVING' || token.status === 'HELD' || token.status === 'WAITING';
      const now = new Date().toISOString();

      const updateRes = db.prepare(`
        UPDATE tokens
        SET status = 'SKIPPED', skipped_at = ?
        WHERE id = ? AND status IN ('SERVING', 'HELD', 'WAITING')
      `).run(now, tokenId);

      if (updateRes.changes === 0) {
        errorMessage = `Invalid state transition: Cannot skip token with status '${token.status}'.`;
        return;
      }

      resultToken = db.prepare('SELECT * FROM tokens WHERE id = ?').get(tokenId) as TokenRecord;
      if (wasActive) {
        serviceIdToPromote = token.service_id;
      }
    }).immediate;

    transaction();

    if (errorMessage) return { success: false, error: errorMessage };

    // Auto-promote waitlist when capacity is freed up
    if (serviceIdToPromote) {
      this.evaluateAndPromoteWaitlist(serviceIdToPromote);
    }

    return { success: true, token: resultToken! };
  }

  /**
   * AUTO-PROMOTION/WAITLIST Operation
   * Selects the next eligible token and assigns/promotes it to SERVING at the specified counter.
   */
  promoteNextToken(serviceId: string, counterId: string): QueueEngineResult {
    const db = getDb();
    let resultToken: TokenRecord | null = null;
    let errorMessage: string | null = null;

    const transaction = db.transaction(() => {
      const counter = db.prepare('SELECT status FROM counters WHERE id = ?').get(counterId) as any;
      if (!counter || counter.status !== 'OPEN') {
        errorMessage = 'Counter is not OPEN';
        return;
      }

      const activeServing = db.prepare(`
        SELECT * FROM tokens WHERE counter_id = ? AND status = 'SERVING'
      `).get(counterId) as TokenRecord | undefined;

      if (activeServing) {
        errorMessage = 'Counter already has an active serving token';
        return;
      }

      const candidateTokens = getSortedWaitingTokens(serviceId);
      if (!candidateTokens || candidateTokens.length === 0) {
        errorMessage = 'No eligible tokens found in waitlist';
        return;
      }

      const now = new Date().toISOString();

      for (const candidate of candidateTokens) {
        const updateRes = db.prepare(`
          UPDATE tokens
          SET status = 'SERVING', counter_id = ?, started_at = ?
          WHERE id = ? AND status = 'WAITING'
        `).run(counterId, now, candidate.id);

        if (updateRes.changes > 0) {
          resultToken = db.prepare('SELECT * FROM tokens WHERE id = ?').get(candidate.id) as TokenRecord;
          break;
        }
      }

      if (!resultToken) {
        errorMessage = 'No eligible tokens found in waitlist';
        return;
      }
    }).immediate;

    transaction();

    if (errorMessage) {
      return { success: false, error: errorMessage };
    }

    return { success: true, token: resultToken! };
  }

  /**
   * Evaluate waitlist and promote eligible waitlisted candidates to WAITING status
   * when active capacity becomes available.
   * Concurrency-safe, capacity-aware, starvation-preventing atomic execution.
   */
  evaluateAndPromoteWaitlist(serviceId: string): { success: boolean; promotedTokens: TokenRecord[]; availableSlots: number; error?: string } {
    const db = getDb();
    let promotedTokens: TokenRecord[] = [];
    let availableSlots = 0;
    let errorMessage: string | null = null;

    const transaction = db.transaction(() => {
      const service = db.prepare('SELECT id, max_capacity FROM services WHERE id = ?').get(serviceId) as any;
      if (!service) {
        errorMessage = 'Service not found';
        return;
      }

      const maxCapacity = service.max_capacity !== null && service.max_capacity !== undefined ? service.max_capacity : (Number(process.env.MAX_QUEUE_CAPACITY) || 10);

      const activeCountResult = db.prepare(`
        SELECT COUNT(*) as count FROM tokens
        WHERE service_id = ? AND status IN ('WAITING', 'SERVING', 'HELD')
      `).get(serviceId) as any;

      const activeCount = activeCountResult?.count || 0;
      availableSlots = Math.max(0, maxCapacity - activeCount);

      if (availableSlots <= 0) {
        return;
      }

      const candidates = getSortedWaitlistTokens(serviceId);
      if (!candidates || candidates.length === 0) {
        return;
      }

      for (const candidate of candidates) {
        if (promotedTokens.length >= availableSlots) {
          break;
        }

        // Check if student already has another active token
        if (candidate.student_id) {
          const activeDuplicate = db.prepare(`
            SELECT id FROM tokens
            WHERE student_id = ? AND service_id = ? AND status IN ('WAITING', 'SERVING', 'HELD')
            LIMIT 1
          `).get(candidate.student_id, serviceId);

          if (activeDuplicate) {
            // Student already active, skip candidate
            continue;
          }
        }

        // Conditional atomic update
        const updateRes = db.prepare(`
          UPDATE tokens
          SET status = 'WAITING'
          WHERE id = ? AND status = 'WAITLISTED'
        `).run(candidate.id);

        if (updateRes.changes > 0) {
          const promoted = db.prepare('SELECT * FROM tokens WHERE id = ?').get(candidate.id) as TokenRecord;
          promotedTokens.push(promoted);
        }
      }
    }).immediate;

    transaction();

    if (errorMessage) {
      return { success: false, promotedTokens: [], availableSlots: 0, error: errorMessage };
    }

    return { success: true, promotedTokens, availableSlots };
  }

  /**
   * Get all waiting tokens ordered by priority + starvation algorithm
   */
  getWaitingQueue(serviceId: string): TokenRecord[] {
    return getSortedWaitingTokens(serviceId);
  }

  /**
   * Get all waitlisted tokens ordered by fairness policy
   */
  getWaitlistQueue(serviceId: string): TokenRecord[] {
    return getSortedWaitlistTokens(serviceId);
  }

  /**
   * Get current serving token for counter
   */
  getCurrentServingToken(counterId: string): TokenRecord | null {
    const db = getDb();
    const token = db.prepare(`
      SELECT * FROM tokens
      WHERE counter_id = ? AND status = 'SERVING'
      LIMIT 1
    `).get(counterId) as TokenRecord | undefined;

    return token || null;
  }
}

export const queueEngine: IQueueEngine = new DefaultQueueEngine();
