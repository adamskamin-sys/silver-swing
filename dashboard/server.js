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

const PORT = parseInt(process.env.PORT || process.env.DASHBOARD_PORT || '3000', 10);
// SWING_DATA_DIR is the source of truth in prod (Render disk mount path).
// SWING_STORE_PATH / SWING_TRADE_LOG_PATH still work for explicit override.
const DATA_DIR = process.env.SWING_DATA_DIR || path.resolve(__dirname, '..', 'data');
const STORE_PATH = process.env.SWING_STORE_PATH || path.join(DATA_DIR, 'store.json');
const TRADE_LOG_PATH = process.env.SWING_TRADE_LOG_PATH || path.join(DATA_DIR, 'trades.jsonl');
const DASHBOARD_PASSWORD = process.env.DASHBOARD_PASSWORD;
const SESSION_SECRET = process.env.DASHBOARD_SESSION_SECRET
  || process.env.SESSION_SECRET
  || 'dev-only-do-not-ship-this';
const IS_PRODUCTION = process.env.NODE_ENV === 'production' || !!process.env.RENDER;

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
  // Trust the Render proxy so req.secure reflects the client-facing HTTPS
  // rather than the internal HTTP hop. Without this, secure cookies never set.
  if (IS_PRODUCTION) app.set('trust proxy', 1);
  app.use(session({
    secret: sessionSecret,
    resave: false,
    saveUninitialized: false,
    cookie: {
      httpOnly: true,
      sameSite: 'lax',
      secure: IS_PRODUCTION,
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

  // --- manual market order intent (dashboard queues, bot executes) ---
  app.post('/api/manual-trade', requireAuth, async (req, res) => {
    const { tenant, symbol, side, qty, confirm } = req.body || {};
    if (!tenant || !symbol) return res.status(400).json({ ok: false, error: 'tenant and symbol required' });
    if (confirm !== 'YES') return res.status(400).json({ ok: false, error: 'confirm must be "YES"' });
    const s = String(side || '').toUpperCase();
    if (!['BUY', 'SELL'].includes(s)) return res.status(400).json({ ok: false, error: 'side must be BUY or SELL' });
    const q = Number(qty);
    if (!Number.isFinite(q) || q < 1 || q > 100 || q !== Math.floor(q)) {
      return res.status(400).json({ ok: false, error: 'qty must be a whole number 1–100' });
    }
    try {
      const store = await readStore(storePath);
      store[tenant] = store[tenant] || {};
      store[tenant][symbol] = store[tenant][symbol] || {};
      // Server-side floor check for SELL — mirror the bot's guard so the UI
      // can show an immediate error rather than silently rejecting later.
      if (s === 'SELL') {
        const snap = store[tenant][symbol].snapshot || {};
        const cfg = store[tenant][symbol].config || {};
        const pos = Number(snap.position_qty ?? 0);
        const core = Number(cfg.core_qty ?? 0);
        if (pos - q < core) {
          return res.status(400).json({
            ok: false,
            error: `sell ${q} would take position ${pos} below core ${core}. Increase core, or sell fewer contracts.`,
          });
        }
      }
      const snap = store[tenant][symbol].snapshot || {};
      store[tenant][symbol].intent = {
        side: s, qty: q,
        mark: Number(snap.last_mark) || null,
        submitted_ts: Date.now() / 1000,
        submitted_by: 'dashboard',
      };
      await writeStoreAtomic(storePath, store);
      res.json({ ok: true, queued: true });
    } catch (err) {
      res.status(500).json({ ok: false, error: String(err) });
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

  // --- paper reset (wipe all paper state, restore starting balance) ------
  // Live mode is refused — the bot's broker.reset() check prevents damage
  // even if this endpoint fires, but we also gate here as defense in depth.
  app.post('/api/reset-paper', requireAuth, async (req, res) => {
    const { tenant, symbol, confirm, starting_balance } = req.body || {};
    if (!tenant || !symbol) return res.status(400).json({ ok: false, error: 'tenant and symbol required' });
    if (confirm !== 'YES') return res.status(400).json({ ok: false, error: 'confirm must be "YES"' });
    try {
      const store = await readStore(storePath);
      const mode = store?.[tenant]?.[symbol]?.snapshot?.mode;
      if (mode && mode !== 'paper') {
        return res.status(400).json({ ok: false, error: `refusing to reset — current mode is ${mode}, not paper` });
      }
      store[tenant] = store[tenant] || {};
      store[tenant][symbol] = store[tenant][symbol] || {};
      // Full wipe: also clear user-defined strategies (sleeves) so the reset
      // gives a genuine clean slate. Bot picks up the resulting empty sleeves
      // list on its next tick, cancels any live orders those sleeves held,
      // and their state is reset by _maybe_consume_reset_intent().
      if (store[tenant][symbol].config?.sleeves) {
        store[tenant][symbol].config.sleeves = [];
      }
      store[tenant][symbol].reset_intent = {
        requested_ts: Date.now() / 1000,
        requested_by: 'dashboard',
        starting_balance: Number(starting_balance) || 100000,
      };
      await writeStoreAtomic(storePath, store);
      res.json({ ok: true, queued: true });
    } catch (err) {
      res.status(500).json({ ok: false, error: String(err) });
    }
  });

  // --- cancel a strategy's live order (primary or sleeve) -----------------
  app.post('/api/cancel-order', requireAuth, async (req, res) => {
    const { tenant, symbol, sleeve_id } = req.body || {};
    if (!tenant || !symbol) return res.status(400).json({ ok: false, error: 'tenant and symbol required' });
    try {
      const store = await readStore(storePath);
      store[tenant] = store[tenant] || {};
      store[tenant][symbol] = store[tenant][symbol] || {};
      store[tenant][symbol].cancel_intent = {
        requested_ts: Date.now() / 1000,
        sleeve_id: sleeve_id || null,  // null = primary
      };
      await writeStoreAtomic(storePath, store);
      res.json({ ok: true, queued: true });
    } catch (err) {
      res.status(500).json({ ok: false, error: String(err) });
    }
  });

  // --- resume a HALTED strategy (dashboard → bot bridge) ------------------
  app.post('/api/resume', requireAuth, async (req, res) => {
    const { tenant, symbol } = req.body || {};
    if (!tenant || !symbol) return res.status(400).json({ ok: false, error: 'tenant and symbol required' });
    try {
      const store = await readStore(storePath);
      store[tenant] = store[tenant] || {};
      store[tenant][symbol] = store[tenant][symbol] || {};
      const prevReason = store[tenant][symbol].state?.halt_reason || null;
      store[tenant][symbol].resume_intent = {
        requested_ts: Date.now() / 1000,
        requested_by: 'dashboard',
        previous_reason: prevReason,
      };
      await writeStoreAtomic(storePath, store);
      res.json({ ok: true, queued: true });
    } catch (err) {
      res.status(500).json({ ok: false, error: String(err) });
    }
  });

  // --- sleeves: additional per-symbol strategies -------------------------
  // Each sleeve manages its own qty of contracts with its own params. Sum
  // across sleeves + primary swing_qty must fit under (position - core_qty).
  // We validate that ceiling here; the bot re-checks it on every arm as the
  // last line of defense.
  app.put('/api/sleeves', requireAuth, async (req, res) => {
    const { tenant, symbol, sleeves } = req.body || {};
    if (!tenant || !symbol) return res.status(400).json({ ok: false, error: 'tenant and symbol required' });
    if (!Array.isArray(sleeves)) return res.status(400).json({ ok: false, error: 'sleeves must be an array' });

    const issues = [];
    const seenIds = new Set();
    for (const s of sleeves) {
      if (!s.id || typeof s.id !== 'string') { issues.push({ field: 'id', message: 'sleeve id required' }); continue; }
      if (seenIds.has(s.id)) issues.push({ field: 'id', message: `duplicate sleeve id ${s.id}` });
      seenIds.add(s.id);
      const q = Number(s.qty);
      if (!Number.isFinite(q) || q < 1 || q !== Math.floor(q))
        issues.push({ field: `${s.id}.qty`, message: 'qty must be integer >= 1' });
      const sell = Number(s.sell_px); const buy = Number(s.buy_px);
      if (!Number.isFinite(sell) || !Number.isFinite(buy) || buy >= sell)
        issues.push({ field: `${s.id}.buy_px`, message: 'buy_px must be < sell_px' });
      if (s.exit_mode === 'hybrid') {
        const act = Number(s.trail_activation_px);
        const delay = Number(s.hybrid_delay_secs);
        if (!Number.isFinite(act) || act <= sell)
          issues.push({ field: `${s.id}.trail_activation_px`, message: 'trail_activation_px must be > sell_px' });
        if (!Number.isFinite(delay) || delay < 1)
          issues.push({ field: `${s.id}.hybrid_delay_secs`, message: 'hybrid_delay_secs must be >= 1' });
      }
    }
    if (issues.length) return res.status(400).json({ ok: false, issues });

    try {
      const store = await readStore(storePath);
      store[tenant] = store[tenant] || {};
      store[tenant][symbol] = store[tenant][symbol] || {};
      const cfg = store[tenant][symbol].config || {};

      // Capacity check: only count sleeves CURRENTLY holding contracts
      // (ARMED_SELL) toward the budget. ARMED_BUY sleeves already sold —
      // their rebuys will consume position later. Otherwise a freshly bought
      // batch of contracts appears "committed" the moment a sleeve rotates
      // into sold-and-waiting state.
      const primaryQty = Number(cfg.swing_qty ?? 0);
      const core = Number(cfg.core_qty ?? 0);
      const snap = store[tenant][symbol].snapshot || {};
      const stateBlock = store[tenant][symbol].state || {};
      const sleeveStates = stateBlock.sleeves || {};
      const pos = Number(snap.position_qty ?? 0);
      const activeSleeveQty = sleeves.reduce((n, s) => {
        const st = sleeveStates[s.id]?.state || 'ARMED_SELL';
        return n + (st === 'ARMED_SELL' ? Number(s.qty) : 0);
      }, 0);
      const budget = pos - core;
      if (activeSleeveQty + primaryQty > budget) {
        return res.status(400).json({
          ok: false,
          error: `active sleeves (ARMED_SELL) sum to ${activeSleeveQty} + primary ${primaryQty} = ${activeSleeveQty + primaryQty}, exceeds available ${budget} (position ${pos} - core ${core}). Buy more contracts or reduce sleeve qtys.`,
        });
      }

      cfg.sleeves = sleeves;
      store[tenant][symbol].config = cfg;
      await writeStoreAtomic(storePath, store);
      res.json({ ok: true });
    } catch (err) {
      res.status(500).json({ ok: false, error: String(err) });
    }
  });

  app.get('/api/positions', requireAuth, async (req, res) => {
    // Read lots straight from the snapshot (paper broker writes them there
    // every ~5s). No enrichment here — the snapshot already carries mark and
    // per-lot unrealized P/L, so the dashboard just needs to render.
    try {
      const store = await readStore(storePath);
      const view = {};
      for (const [tenant, symbols] of Object.entries(store)) {
        view[tenant] = {};
        for (const [symbol, block] of Object.entries(symbols || {})) {
          if (symbol.startsWith('__')) continue;  // skip sentinels (kill switch)
          const snap = block.snapshot || {};
          view[tenant][symbol] = {
            position_qty: snap.position_qty || 0,
            position_avg_entry: snap.position_avg_entry || 0,
            last_mark: snap.last_mark || 0,
            lots: snap.lots || [],
          };
        }
      }
      res.json({ positions: view, generated_at: new Date().toISOString() });
    } catch (err) {
      res.status(500).json({ error: String(err) });
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
  // Use a PID- and timestamp-suffixed tmp so we don't collide with the Python
  // bot's tmp file when both processes write concurrently. Without this,
  // whichever renames first wins and the other gets ENOENT on rename.
  await fs.mkdir(path.dirname(storePath), { recursive: true });
  const tmp = `${storePath}.tmp-${process.pid}-${Date.now()}`;
  await fs.writeFile(tmp, JSON.stringify(data, null, 2));
  await fs.rename(tmp, storePath);
}

const PYTHON_BIN = process.env.SWING_PYTHON_BIN ||
  path.resolve(__dirname, '..', '.venv', 'bin', 'python');
const BACKTEST_SCRIPT = path.resolve(__dirname, '..', 'scripts', 'run_backtest.py');

function runPythonBacktest(payload) {
  return new Promise((resolve, reject) => {
    const projectRoot = path.resolve(__dirname, '..');
    const proc = spawn(PYTHON_BIN, [BACKTEST_SCRIPT], {
      cwd: projectRoot,
      env: { ...process.env, PYTHONPATH: projectRoot },
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
