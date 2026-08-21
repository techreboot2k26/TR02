import path from 'path';
import fs from 'fs';
import { createRequire } from 'module';

const require = createRequire(import.meta.url);

// Database path setting (can be overridden for testing via process.env.DB_PATH)
const isVercel = Boolean(process.env.VERCEL || process.env.AWS_LAMBDA_FUNCTION_NAME);
const defaultDbPath = isVercel
  ? '/tmp/queuecraft.db'
  : path.join(process.env.INIT_CWD || process.cwd(), 'queuecraft.db');

const dbPath = process.env.DB_PATH || defaultDbPath;

let dbInstance: any = null;

function createDummyDb(): any {
  const dummyStatement = {
    run: () => ({ changes: 0, lastInsertRowid: 0 }),
    get: () => undefined,
    all: () => [],
  };
  return {
    exec: () => {},
    pragma: () => {},
    prepare: () => dummyStatement,
    transaction: (fn: any) => fn,
    close: () => {},
  };
}

export function getDb(): any {
  if (!dbInstance) {
    try {
      const dbDir = path.dirname(dbPath);
      if (!fs.existsSync(dbDir)) {
        fs.mkdirSync(dbDir, { recursive: true });
      }
      const Database = require('better-sqlite3');
      dbInstance = new Database(dbPath, { timeout: 5000 });
      // Enable Foreign Keys, Write-Ahead Logging & busy timeout for concurrency safety
      dbInstance.pragma('foreign_keys = ON');
      dbInstance.pragma('busy_timeout = 5000');
      if (!isVercel) {
        dbInstance.pragma('journal_mode = WAL');
      }
    } catch (err) {
      console.error('[Database] Failed to initialize SQLite database, using fallback dummy DB:', err);
      dbInstance = createDummyDb();
    }
  }
  return dbInstance;
}

export function closeDb(): void {
  if (dbInstance) {
    try {
      dbInstance.close();
    } catch {}
    dbInstance = null;
  }
}
