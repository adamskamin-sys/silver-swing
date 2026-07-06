/**
 * dashboard/server.js — read-only Express status view for the silver-swing bot.
 *
 * Non-negotiables from spec §0 + §10:
 *   - Dashboard NEVER holds Coinbase API keys.
 *   - Dashboard NEVER places orders.
 *   - Auth on the API, not just the page. Every /api/* route rejects sessions
 *     that haven't logged in — password-gating index.html is theater otherwise.
 *   - Single strong login (one user, you).
 *
 * The store is the shared source of truth. Locally that's the same
 * ../data/store.json file the Python bot writes to. In prod, swap for Render KV
 * or Postgres — same shape, different backing.
 *
 * MVP scope: STATUS ONLY. No editable config yet. Spec §12 step 7 explicitly
 * says "read-only status view — watch the bot before it can be changed from
 * the UI." Editing (§12 step 8) comes after we know what's worth editing.
 */

import 'dotenv/config';
import express from 'express';
import session from 'express-session';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { validateConfig } from './validator.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// ---- config -----------------------------------------------------------------

const PORT = parseInt(process.env.PORT || '3000', 10);
const STORE_PATH = process.env.SWING_STORE_PATH ||
  path.resolve(__dirname, '..', 'data', 'store.json');
const TRADE_LOG_PATH = process.env.SWING_TRADE_LOG_PATH ||
  path.resolve(__dirname, '..', 'data', 'trades.jsonl');
const DASHBOARD_PASSWORD = process.env.DASHBOARD_PASSWORD;
const SESSION_SECRET = process.env.SESSION_SECRET || 'dev-only-do-not-ship-this';

if (!DASHBOARD_PASSWORD) {
  console.warn('WARNING: DASHBOARD_PASSWORD not set. Login is disabled — dev mode only.');
}

// ---- app --------------------------------------------------------------------

export function makeApp({
  storePath = STORE_PATH,
  tradeLogPath = TRADE_LOG_PATH,
  password = DASHBOARD_PASSWORD,
  sessionSecret = SESSION_SECRET,
} = {}) {
  const app = express();
  app.use(express.json());
  app.use(session({
    secret: sessionSecret,
    resave: false,
    saveUninitialized: false,
    cookie: {
      httpOnly: true,
      sameSite: 'lax',
      // secure: true — set to true when serving over HTTPS in prod
    },
  }));

  // --- static (public assets, unauth) ---
  app.use(express.static(path.join(__dirname, 'public')));

  // --- auth ---
  app.post('/login', (req, res) => {
    const { password: submitted } = req.body || {};
    if (!password || submitted === password) {
      req.session.authed = true;
      return res.json({ ok: true });
    }
    return res.status(401).json({ ok: false, error: 'invalid password' });
  });

  app.post('/logout', (req, res) => {
    req.session.destroy(() => res.json({ ok: true }));
  });

  app.get('/api/session', (req, res) => {
    res.json({ authed: !!(req.session && req.session.authed), auth_required: !!password });
  });

  // --- auth guard for /api/* (except /api/session) ---
  const requireAuth = (req, res, next) => {
    if (!password) return next();                    // dev mode: no password configured
    if (req.session && req.session.authed) return next();
    return res.status(401).json({ error: 'not authenticated' });
  };

  // --- read-only status endpoints ---

  app.get('/api/status', requireAuth, async (req, res) => {
    try {
      const store = await readStore(storePath);
      // Aggregate a snapshot: all tenants → all symbols → {config, state}
      const view = {};
      for (const [tenant, symbols] of Object.entries(store)) {
        view[tenant] = {};
        for (const [symbol, block] of Object.entries(symbols || {})) {
          view[tenant][symbol] = {
            config: block.config || null,
            state: block.state || null,
            snapshot: block.snapshot || null,
          };
        }
      }
      res.json({ store: view, generated_at: new Date().toISOString() });
    } catch (err) {
      res.status(500).json({ error: String(err) });
    }
  });

  // --- editable config (PUT), validated server-side (spec §10) ---
  app.put('/api/config', requireAuth, async (req, res) => {
    const { tenant, symbol, config } = req.body || {};
    if (!tenant || !symbol) return res.status(400).json({ error: 'tenant and symbol required' });
    if (!config || typeof config !== 'object') return res.status(400).json({ error: 'config object required' });

    const result = validateConfig(config);
    if (!result.ok) return res.status(400).json({ ok: false, issues: result.issues });

    try {
      const store = await readStore(storePath);
      store[tenant] = store[tenant] || {};
      store[tenant][symbol] = store[tenant][symbol] || {};
      store[tenant][symbol].config = config;
      await writeStoreAtomic(storePath, store);
      res.json({ ok: true });
    } catch (err) {
      res.status(500).json({ error: String(err) });
    }
  });

  // --- kill switch ---
  app.post('/api/kill-switch/activate', requireAuth, async (req, res) => {
    const { tenant, reason, confirm } = req.body || {};
    if (!tenant) return res.status(400).json({ error: 'tenant required' });
    if (confirm !== 'YES') return res.status(400).json({ error: 'confirm must be "YES"' });
    try {
      const store = await readStore(storePath);
      store[tenant] = store[tenant] || {};
      store[tenant]['__account_kill_switch__'] = store[tenant]['__account_kill_switch__'] || {};
      store[tenant]['__account_kill_switch__'].config = {
        active: true,
        reason: reason || 'triggered from dashboard',
        activated_ts: Date.now() / 1000,
      };
      await writeStoreAtomic(storePath, store);
      res.json({ ok: true });
    } catch (err) {
      res.status(500).json({ error: String(err) });
    }
  });

  app.post('/api/kill-switch/clear', requireAuth, async (req, res) => {
    const { tenant, confirm, cleared_by } = req.body || {};
    if (!tenant) return res.status(400).json({ error: 'tenant required' });
    if (confirm !== 'YES') return res.status(400).json({ error: 'confirm must be "YES"' });
    try {
      const store = await readStore(storePath);
      const prev = store?.[tenant]?.['__account_kill_switch__']?.config || {};
      store[tenant] = store[tenant] || {};
      store[tenant]['__account_kill_switch__'] = store[tenant]['__account_kill_switch__'] || {};
      store[tenant]['__account_kill_switch__'].config = {
        active: false,
        reason: null,
        cleared_ts: Date.now() / 1000,
        cleared_by: cleared_by || 'dashboard',
        previous_reason: prev.reason || null,
      };
      await writeStoreAtomic(storePath, store);
      res.json({ ok: true });
    } catch (err) {
      res.status(500).json({ error: String(err) });
    }
  });

  // --- backtest (spawns Python subprocess) ---
  app.post('/api/backtest', requireAuth, async (req, res) => {
    const payload = req.body || {};
    if (!payload.symbol) return res.status(400).json({ ok: false, error: 'symbol required' });
    try {
      const result = await runPythonBacktest(payload);
      res.json(result);
    } catch (err) {
      res.status(500).json({ ok: false, error: String(err) });
    }
  });

  app.get('/api/trades', requireAuth, async (req, res) => {
    const n = Math.min(parseInt(req.query.n || '50', 10), 500);
    try {
      const events = await tailJsonl(tradeLogPath, n);
      res.json({ events });
    } catch (err) {
      res.status(500).json({ error: String(err) });
    }
  });

  return app;
}

// ---- helpers ----------------------------------------------------------------

async function readStore(storePath) {
  try {
    const raw = await fs.readFile(storePath, 'utf-8');
    return JSON.parse(raw);
  } catch (err) {
    if (err.code === 'ENOENT') return {};  // no store yet, empty view
    throw err;
  }
}

async function writeStoreAtomic(storePath, data) {
  // Mirror the Python JsonFileStateStore atomicity: write tmp, rename.
  await fs.mkdir(path.dirname(storePath), { recursive: true });
  const tmp = storePath + '.tmp';
  await fs.writeFile(tmp, JSON.stringify(data, null, 2));
  await fs.rename(tmp, storePath);
}

const PYTHON_BIN = process.env.SWING_PYTHON_BIN ||
  path.resolve(__dirname, '..', '.venv', 'bin', 'python');
const BACKTEST_SCRIPT = path.resolve(__dirname, '..', 'scripts', 'run_backtest.py');

function runPythonBacktest(payload) {
  return new Promise((resolve, reject) => {
    const proc = spawn(PYTHON_BIN, [BACKTEST_SCRIPT], {
      cwd: path.resolve(__dirname, '..'),
      env: process.env,
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    let stdout = '', stderr = '';
    proc.stdout.on('data', d => { stdout += d; });
    proc.stderr.on('data', d => { stderr += d; });
    proc.on('error', reject);
    proc.on('close', (code) => {
      try {
        const parsed = JSON.parse(stdout);
        resolve(parsed);
      } catch (e) {
        reject(new Error(`bad backtest output (code=${code}): ${stderr || stdout}`));
      }
    });
    proc.stdin.write(JSON.stringify(payload));
    proc.stdin.end();
  });
}

async function tailJsonl(logPath, n) {
  try {
    const raw = await fs.readFile(logPath, 'utf-8');
    const lines = raw.split('\n').filter(Boolean);
    const tail = lines.slice(-n);
    return tail.map(line => {
      try { return JSON.parse(line); } catch { return null; }
    }).filter(Boolean);
  } catch (err) {
    if (err.code === 'ENOENT') return [];
    throw err;
  }
}

// ---- run --------------------------------------------------------------------

if (process.argv[1] && process.argv[1].endsWith('server.js')) {
  const app = makeApp();
  app.listen(PORT, () => {
    console.log(`silver-swing dashboard on http://localhost:${PORT}`);
    console.log(`  store: ${STORE_PATH}`);
    console.log(`  trade log: ${TRADE_LOG_PATH}`);
    console.log(`  auth: ${DASHBOARD_PASSWORD ? 'ENABLED' : 'disabled (dev only)'}`);
  });
}
