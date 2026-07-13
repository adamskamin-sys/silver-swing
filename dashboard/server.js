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
import { createClient } from 'redis';
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
const REDIS_URL = process.env.REDIS_URL || null;
const REDIS_STORE_KEY = 'silver-swing:store';
const REDIS_TRADES_KEY = 'silver-swing:trades';
const REDIS_SCANNER_KEY = 'silver-swing:scanner';
const REDIS_TWITTER_LOG_KEY = 'silver-swing:twitter-signals';

if (!DASHBOARD_PASSWORD) {
  console.warn('WARNING: DASHBOARD_PASSWORD not set. Login is disabled — dev mode only.');
}

// Shared Redis client (lazy). Same instance reused across requests.
let _redis = null;
async function getRedis() {
  if (!REDIS_URL) return null;
  if (_redis) return _redis;
  _redis = createClient({ url: REDIS_URL });
  _redis.on('error', (err) => console.error('redis error:', err));
  await _redis.connect();
  return _redis;
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

  // --- global stop-loss toggle (Adam's "pause SL before market open" button) ---
  // Writes a control scope (__stop_loss_disabled__) on the given tenant that
  // the bot reads before firing ANY stop-loss trigger. Sleeve stop-loss config
  // stays intact — this just gates the firing globally.
  app.get('/api/stop-loss/status', requireAuth, async (req, res) => {
    const tenant = String(req.query.tenant || '').trim();
    if (!tenant) return res.status(400).json({ error: 'tenant required' });
    try {
      const store = await readStore(storePath);
      const cfg = store?.[tenant]?.['__stop_loss_disabled__']?.config || {};
      res.json({ ok: true, disabled: !!cfg.disabled, reason: cfg.reason || null });
    } catch (err) {
      res.status(500).json({ error: String(err) });
    }
  });

  app.post('/api/stop-loss/toggle', requireAuth, async (req, res) => {
    const { tenant, disabled, reason } = req.body || {};
    if (!tenant) return res.status(400).json({ error: 'tenant required' });
    if (typeof disabled !== 'boolean') return res.status(400).json({ error: 'disabled (boolean) required' });
    try {
      const store = await readStore(storePath);
      store[tenant] = store[tenant] || {};
      store[tenant]['__stop_loss_disabled__'] = store[tenant]['__stop_loss_disabled__'] || {};
      store[tenant]['__stop_loss_disabled__'].config = {
        disabled: disabled,
        reason: disabled ? (reason || 'toggled from dashboard') : null,
        ts: Date.now() / 1000,
      };
      await writeStoreAtomic(storePath, store);
      res.json({ ok: true, disabled: disabled });
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

  // --- manual market/limit order intent (dashboard queues, bot executes) ---
  app.post('/api/manual-trade', requireAuth, async (req, res) => {
    const { tenant, symbol, side, qty, confirm, order_type, limit_price } = req.body || {};
    if (!tenant || !symbol) return res.status(400).json({ ok: false, error: 'tenant and symbol required' });
    if (confirm !== 'YES') return res.status(400).json({ ok: false, error: 'confirm must be "YES"' });
    const s = String(side || '').toUpperCase();
    if (!['BUY', 'SELL'].includes(s)) return res.status(400).json({ ok: false, error: 'side must be BUY or SELL' });
    const q = Number(qty);
    if (!Number.isFinite(q) || q < 1 || q > 100 || q !== Math.floor(q)) {
      return res.status(400).json({ ok: false, error: 'qty must be a whole number 1–100' });
    }
    const ot = String(order_type || 'market').toLowerCase();
    if (!['market', 'limit'].includes(ot)) {
      return res.status(400).json({ ok: false, error: 'order_type must be market or limit' });
    }
    let lp = null;
    if (ot === 'limit') {
      lp = Number(limit_price);
      if (!Number.isFinite(lp) || lp <= 0) {
        return res.status(400).json({ ok: false, error: 'limit_price must be > 0 for limit orders' });
      }
    }
    try {
      const store = await readStore(storePath);
      store[tenant] = store[tenant] || {};
      store[tenant][symbol] = store[tenant][symbol] || {};
      // Core-floor check is now a WARNING only for manual trades — shorting
      // is a supported action, so refusing the sell would silently block a
      // legitimate short entry. The bot's per-strategy floor still protects
      // AUTOMATED sells (sleeves/primary); this endpoint is only reached
      // when the user explicitly clicked Buy/Sell.
      if (s === 'SELL') {
        const snap = store[tenant][symbol].snapshot || {};
        const cfg = store[tenant][symbol].config || {};
        const pos = Number(snap.position_qty ?? 0);
        const core = Number(cfg.core_qty ?? 0);
        if (pos > core && pos - q < core) {
          console.warn(`[manual-trade] sell ${q} takes ${tenant}/${symbol} below core ${core} (from ${pos}). Allowed — user asked explicitly.`);
        }
      }
      const snap = store[tenant][symbol].snapshot || {};
      store[tenant][symbol].intent = {
        side: s, qty: q,
        order_type: ot,
        limit_price: lp,
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

  // --- backtest ---
  // In prod (Redis wired): push job onto a Redis queue, poll for the paper
  // worker's response. Keeps Coinbase creds on the one service that needs
  // them. In local dev (no Redis): fall back to spawning the Python script
  // directly so `npm run dev` still works.
  app.post('/api/backtest', requireAuth, async (req, res) => {
    const payload = req.body || {};
    if (!payload.symbol) return res.status(400).json({ ok: false, error: 'symbol required' });
    try {
      const r = await getRedis();
      const result = r
        ? await runBacktestViaRedis(r, payload)
        : await runPythonBacktest(payload);
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
    const { tenant, symbol, sleeve_id, halt } = req.body || {};
    if (!tenant || !symbol) return res.status(400).json({ ok: false, error: 'tenant and symbol required' });
    try {
      const store = await readStore(storePath);
      store[tenant] = store[tenant] || {};
      store[tenant][symbol] = store[tenant][symbol] || {};
      store[tenant][symbol].cancel_intent = {
        requested_ts: Date.now() / 1000,
        sleeve_id: sleeve_id || null,  // null = primary
        halt: !!halt,                  // true → also halt the state machine
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
      // Per-sleeve accumulation validation. Only enforced when enabled.
      if (s.accumulate_enabled) {
        const maxQ = Number(s.max_qty);
        if (!Number.isFinite(maxQ) || maxQ < q || maxQ !== Math.floor(maxQ))
          issues.push({ field: `${s.id}.max_qty`, message: `max_qty must be an integer >= current qty (${q})` });
        const buf = Number(s.scale_up_buffer_mult);
        if (!Number.isFinite(buf) || buf < 1.0)
          issues.push({ field: `${s.id}.scale_up_buffer_mult`, message: 'scale_up_buffer_mult must be >= 1.0' });
      }
      // Per-sleeve stop-loss validation. Only enforced when enabled.
      if (s.stop_loss_enabled) {
        const stopPx = Number(s.stop_loss_px);
        if (!Number.isFinite(stopPx) || stopPx <= 0)
          issues.push({ field: `${s.id}.stop_loss_px`, message: 'stop_loss_px must be > 0' });
        else if (Number.isFinite(buy) && stopPx >= buy)
          issues.push({ field: `${s.id}.stop_loss_px`, message: `stop_loss_px (${stopPx}) must be < buy_px (${buy}); otherwise it fires as soon as you're armed` });
        const mode = String(s.stop_loss_qty_mode || 'all').toLowerCase();
        if (!['all', 'original', 'custom'].includes(mode))
          issues.push({ field: `${s.id}.stop_loss_qty_mode`, message: `stop_loss_qty_mode must be all|original|custom, got ${mode}` });
        if (mode === 'custom') {
          const cq = Number(s.stop_loss_qty_custom);
          if (!Number.isFinite(cq) || cq < 1 || cq !== Math.floor(cq))
            issues.push({ field: `${s.id}.stop_loss_qty_custom`, message: 'stop_loss_qty_custom must be integer >= 1' });
        }
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
      // Live tenant is a portfolio mirror, not a swing bot — no "protected
      // core" to defend. Old configs still have core_qty=10 stuck in them,
      // which causes the capacity check to reject every sleeve. Force 0.
      const isLive = String(tenant).toLowerCase().includes('live');
      const core = isLive ? 0 : Number(cfg.core_qty ?? 0);
      const snap = store[tenant][symbol].snapshot || {};
      const stateBlock = store[tenant][symbol].state || {};
      const sleeveStates = stateBlock.sleeves || {};
      let pos = Number(snap.position_qty ?? 0);
      // Live tenant runs read-only — snapshot.position_qty is 0 because no
      // strategy engine writes it. Real position lives in the __portfolio__
      // snap we sync from Coinbase. Look it up and prefer whichever is larger
      // so paper (snap has value) also works.
      if (isLive && pos === 0) {
        const pfSnap = store[tenant]?.__portfolio__?.config;
        const posRow = (pfSnap?.derivatives || []).find(d => d.product_id === symbol);
        if (posRow) pos = Math.abs(Number(posRow.qty)) || 0;
      }
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

  // --- delete one sleeve without re-validating the survivors --------------
  // The regular PUT /api/sleeves re-validates every sleeve in the payload,
  // so if a survivor has a bad stored field (e.g. old max_qty < new qty,
  // stop_px >= buy_px from a pre-migration save) the whole delete is rejected.
  // This endpoint pulls the current array, removes the target id, writes it
  // back — no validation. Bad survivors keep working; only the target dies.
  app.post('/api/sleeves/delete', requireAuth, async (req, res) => {
    const { tenant, symbol, sleeve_id } = req.body || {};
    if (!tenant || !symbol || !sleeve_id) {
      return res.status(400).json({ ok: false, error: 'tenant, symbol, sleeve_id required' });
    }
    try {
      const store = await readStore(storePath);
      store[tenant] = store[tenant] || {};
      store[tenant][symbol] = store[tenant][symbol] || {};
      const cfg = store[tenant][symbol].config || {};
      const before = (cfg.sleeves || []).length;
      cfg.sleeves = (cfg.sleeves || []).filter(s => s.id !== sleeve_id);
      const removed = before - cfg.sleeves.length;
      if (removed === 0) {
        return res.json({ ok: true, removed: 0, message: 'sleeve not found' });
      }
      store[tenant][symbol].config = cfg;
      await writeStoreAtomic(storePath, store);
      res.json({ ok: true, removed });
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

  app.get('/api/scanner', requireAuth, async (req, res) => {
    const r = await getRedis();
    if (!r) return res.json({ top: [], generated_at: null, note: 'redis not configured' });
    try {
      const raw = await r.get(REDIS_SCANNER_KEY);
      if (!raw) return res.json({ top: [], generated_at: null });
      res.json(JSON.parse(raw));
    } catch (err) {
      res.status(500).json({ error: String(err) });
    }
  });

  // Twitter shadow signal log — read-only. The scanner runs in the bot loop
  // (live_runner calls twitter_scanner.tick every TWITTER_POLL_SECS) and
  // writes entries to REDIS_TWITTER_LOG_KEY as JSON strings. Frontmost entry
  // is the newest. Shadow-mode invariant: `shadow_mode` and
  // `trades_executed: false` are set by the Python side; the dashboard
  // surfaces them so the user always sees "not executing" front-and-center.
  app.get('/api/twitter-signals', requireAuth, async (req, res) => {
    const r = await getRedis();
    if (!r) return res.json({ entries: [], summary: { total_signals: 0, shadow_mode: true } });
    try {
      const raws = await r.lRange(REDIS_TWITTER_LOG_KEY, 0, 199) || [];
      const entries = [];
      for (const raw of raws) {
        try { entries.push(JSON.parse(raw)); } catch { /* skip malformed */ }
      }
      // Aggregate hit rate for the header. Cheap: N ≤ 200.
      const tally = { '1h': { correct: 0, wrong: 0, flat: 0, unknown: 0 },
                      '6h': { correct: 0, wrong: 0, flat: 0, unknown: 0 },
                      '24h': { correct: 0, wrong: 0, flat: 0, unknown: 0 } };
      for (const e of entries) {
        const outs = e.outcomes || {};
        for (const h of ['1h', '6h', '24h']) {
          const res = outs[h];
          if (!res) continue;
          const v = res.verdict || 'unknown';
          if (tally[h][v] !== undefined) tally[h][v]++;
        }
      }
      res.json({
        entries,
        summary: {
          total_signals: entries.length,
          shadow_mode: true,
          by_horizon: tally,
        },
      });
    } catch (err) {
      res.status(500).json({ error: String(err) });
    }
  });

  // Ask the paper worker to run one scan. Sets a Redis flag with a 5-min TTL;
  // the worker checks each loop iteration, runs one scan, then clears the
  // flag. Scanner is on-demand only to save Coinbase API budget.
  app.post('/api/scanner/refresh', requireAuth, async (req, res) => {
    const r = await getRedis();
    if (!r) return res.status(503).json({ ok: false, error: 'redis not configured' });
    try {
      await r.set('silver-swing:scanner:refresh_requested',
                  String(Math.floor(Date.now() / 1000)),
                  'EX', 300);
      // Optional include list: comma-separated product_ids the caller wants
      // to guarantee are in the next scan (e.g., Add Strategy modal for a
      // brand-new product with no existing sleeve). Paper worker unions
      // these into force_include for that scan.
      const include = req.body && req.body.include;
      if (include && typeof include === 'string') {
        // Use a Redis SET so multiple concurrent Scan-Now clicks accumulate
        // instead of overwriting each other. Previous SET-based code caused
        // e.g. "click Scan on NOL, then NER" to drop NOL from the next
        // scanner run.
        for (const pid of include.split(',').map(s => s.trim()).filter(Boolean)) {
          await r.sAdd('silver-swing:scanner:refresh_include_set', pid);
        }
        await r.expire('silver-swing:scanner:refresh_include_set', 300);
      }
      res.json({ ok: true, requested_at: Math.floor(Date.now() / 1000) });
    } catch (err) {
      res.status(500).json({ ok: false, error: String(err) });
    }
  });

  // One-shot buy/sell of any Coinbase futures product straight from the
  // scanner — doesn't require a tracked strategy. Same Redis queue pattern:
  // dashboard queues, paper worker executes via CoinbaseBroker (LIVE) or
  // simulates + logs (PAPER).
  // --- track a new symbol under a tenant. Seeds a default config so the bot's
  // discovery pass finds it and hot-adds a Track. The bot re-scans on an
  // interval (SWING_SYMBOL_DISCOVER_INTERVAL, default 10s) so newly-tracked
  // symbols come online within a few seconds of clicking Track.
  app.post('/api/track-symbol', requireAuth, async (req, res) => {
    const { tenant, symbol } = req.body || {};
    if (!tenant || !symbol) return res.status(400).json({ ok: false, error: 'tenant and symbol required' });
    // Basic symbol shape check — Coinbase futures use PRODUCT-DDMMMYY-CDE.
    // Cheap sanity gate; the bot will fail its feed subscription if wrong.
    if (!/^[A-Z0-9]+-[0-9A-Z]+-[A-Z]+$/i.test(symbol)) {
      return res.status(400).json({ ok: false, error: `symbol shape looks wrong: ${symbol}` });
    }
    try {
      const store = await readStore(storePath);
      store[tenant] = store[tenant] || {};
      if (store[tenant][symbol]) {
        return res.json({ ok: true, already_tracked: true });
      }
      // Seed with core_qty=0 so a new derivative starts with free trading —
      // the user can raise the floor later once they know how they want to
      // manage it. Everything else uses the same empirical SLR defaults;
      // dashboard exposes Settings to tune per-symbol.
      store[tenant][symbol] = {
        config: {
          core_qty: 0, swing_qty: 0, max_swing_qty: 5,
          sell_px: 0, buy_px: 0, contract_size: 50,
          margin_per_contract: 275.0, scale_up_buffer_mult: 1.5,
          fee_per_contract_roundtrip: 4.68,
          abort_below: 0, abort_above: 1e9,
          fee_sanity_multiplier: 2.0,
          sleeves: [],
          tracked_by: 'dashboard',
          tracked_ts: Date.now() / 1000,
        },
      };
      await writeStoreAtomic(storePath, store);
      res.json({ ok: true, symbol });
    } catch (err) {
      res.status(500).json({ ok: false, error: String(err) });
    }
  });

  app.post('/api/scanner-order', requireAuth, async (req, res) => {
    const { product_id, side, qty, mode, confirm, order_type, limit_price } = req.body || {};
    if (!product_id) return res.status(400).json({ ok: false, error: 'product_id required' });
    if (confirm !== 'YES') return res.status(400).json({ ok: false, error: 'confirm must be "YES"' });
    const s = String(side || '').toUpperCase();
    if (!['BUY', 'SELL'].includes(s)) return res.status(400).json({ ok: false, error: 'side must be BUY or SELL' });
    const q = Number(qty);
    if (!Number.isFinite(q) || q < 1 || q > 100 || q !== Math.floor(q)) {
      return res.status(400).json({ ok: false, error: 'qty must be a whole number 1-100' });
    }
    const m = String(mode || 'paper').toLowerCase();
    if (!['paper', 'live', 'lab'].includes(m)) return res.status(400).json({ ok: false, error: 'mode must be paper, lab, or live' });
    const ot = String(order_type || 'market').toLowerCase();
    if (!['market', 'limit'].includes(ot)) {
      return res.status(400).json({ ok: false, error: 'order_type must be market or limit' });
    }
    let lp = null;
    if (ot === 'limit') {
      lp = Number(limit_price);
      if (!Number.isFinite(lp) || lp <= 0) {
        return res.status(400).json({ ok: false, error: 'limit_price must be > 0 for limit orders' });
      }
    }
    const r = await getRedis();
    if (!r) return res.status(503).json({ ok: false, error: 'redis not configured' });
    try {
      const result = await scannerOrderViaRedis(r, {
        product_id, side: s, qty: q, mode: m,
        order_type: ot, limit_price: lp,
      });
      res.json(result);
    } catch (err) {
      res.status(500).json({ ok: false, error: String(err) });
    }
  });

  // Candles for the scanner's chart modal. Same Redis queue pattern as
  // backtest — dashboard queues, paper worker fetches from Coinbase, result
  // is cached ~60s so a re-open doesn't re-hit the API.
  app.get('/api/candles', requireAuth, async (req, res) => {
    const product_id = String(req.query.product_id || '');
    const granularity = String(req.query.granularity || 'FIVE_MINUTE');
    // Sub-day windows use `minutes` (5, 30, 60). Day-scale windows use `days`
    // (1, 7, 30). Frontend passes exactly one; server prefers minutes if set.
    const minutesRaw = req.query.minutes;
    const payload = { product_id, granularity };
    if (minutesRaw != null) {
      payload.minutes = Math.max(1, Math.min(1440, parseInt(minutesRaw, 10)));
    } else {
      payload.days = Math.max(1, Math.min(30, parseInt(req.query.days || '7', 10)));
    }
    if (!product_id) return res.status(400).json({ ok: false, error: 'product_id required' });
    const r = await getRedis();
    if (!r) return res.status(503).json({ ok: false, error: 'redis not configured' });
    try {
      const result = await fetchCandlesViaRedis(r, payload);
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

  // Filtered fills for chart annotation. Returns each sleeve_order_filled
  // (or primary order_filled) event for a specific product, mapped to
  // {ts, side, price, qty}. Side derived from the leg field: ARMED_SELL →
  // SELL fill (we sold), ARMED_BUY → BUY fill (we bought back).
  app.get('/api/fills', requireAuth, async (req, res) => {
    const symbol = String(req.query.symbol || '').trim();
    if (!symbol) return res.status(400).json({ ok: false, error: 'symbol required' });
    // Default deep — cycles from last week must still be reachable. RedisTradeLog
    // caps at 10000 events; fetching all is one LRANGE and ~1 MB of JSON, fine
    // for a chart-open trigger.
    const limit = Math.min(parseInt(req.query.limit || '10000', 10), 10000);
    try {
      const events = await tailJsonl(tradeLogPath, limit);
      const fills = [];
      for (const e of events) {
        if (!e || e.symbol !== symbol) continue;
        const t = e.event_type;
        // sleeve_order_filled: every fill on a sleeve arm.
        if (t === 'sleeve_order_filled') {
          const leg = String(e.leg || '').toUpperCase();
          const side = leg.includes('SELL') ? 'SELL' : (leg.includes('BUY') ? 'BUY' : null);
          if (!side) continue;
          const price = Number(e.average_filled_price);
          if (!Number.isFinite(price) || price <= 0) continue;
          fills.push({ ts: Number(e.ts) || 0, side, price, qty: Number(e.filled_qty) || 0,
                       kind: 'sleeve', sleeve: e.sleeve_name || e.sleeve_id });
          continue;
        }
        // sleeve_stop_loss_triggered: forced SELL from stop-loss.
        if (t === 'sleeve_stop_loss_triggered' || t === 'stop_loss_triggered') {
          const price = Number(e.price);
          if (!Number.isFinite(price) || price <= 0) continue;
          fills.push({ ts: Number(e.ts) || 0, side: 'SELL', price, qty: Number(e.sold) || 0,
                       kind: 'stop_loss', sleeve: e.sleeve_name || e.sleeve_id || null });
          continue;
        }
      }
      // Sort ascending by ts so pairing buy→sell in order works client-side.
      fills.sort((a, b) => a.ts - b.ts);
      // Diagnostics: how many events did we scan, how many symbols were seen,
      // and did any event mention this symbol at all? Helps debug the
      // "no fills for XLP but I know there are cycles" case.
      const symbolsSeen = new Set();
      let anyForSymbol = 0;
      for (const e of events) {
        if (e && e.symbol) symbolsSeen.add(e.symbol);
        if (e && e.symbol === symbol) anyForSymbol++;
      }
      res.json({
        ok: true,
        fills,
        _diag: {
          events_scanned: events.length,
          events_for_symbol: anyForSymbol,
          symbols_in_log: Array.from(symbolsSeen).sort(),
          matched_fill_events: fills.length,
        },
      });
    } catch (err) {
      res.status(500).json({ ok: false, error: String(err) });
    }
  });

  return app;
}

// ---- helpers ----------------------------------------------------------------

async function readStore(storePath) {
  const r = await getRedis();
  if (r) {
    const raw = await r.get(REDIS_STORE_KEY);
    return raw ? JSON.parse(raw) : {};
  }
  try {
    const raw = await fs.readFile(storePath, 'utf-8');
    return JSON.parse(raw);
  } catch (err) {
    if (err.code === 'ENOENT') return {};  // no store yet, empty view
    throw err;
  }
}

async function writeStoreAtomic(storePath, data) {
  const r = await getRedis();
  if (r) {
    await r.set(REDIS_STORE_KEY, JSON.stringify(data));
    return;
  }
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

const BACKTEST_QUEUE_KEY = 'silver-swing:backtest:queue';
const BACKTEST_REQ_PREFIX = 'silver-swing:backtest:req:';
const BACKTEST_RES_PREFIX = 'silver-swing:backtest:res:';
const BACKTEST_MAX_WAIT_MS = 180_000;   // 3 min — 90d @ 1min candles can push toward 2 min
const BACKTEST_KEY_TTL_SECS = 300;

const CANDLES_QUEUE_KEY = 'silver-swing:candles:queue';
const CANDLES_REQ_PREFIX = 'silver-swing:candles:req:';
const CANDLES_RES_PREFIX = 'silver-swing:candles:res:';
const CANDLES_MAX_WAIT_MS = 30_000;     // 30s — candle fetches are usually fast, cache hits <100ms
const CANDLES_KEY_TTL_SECS = 60;

const JOB_POLL_INTERVAL_MS = 300;

async function runBacktestViaRedis(redis, payload) {
  return jobViaRedis(redis, {
    queueKey: BACKTEST_QUEUE_KEY,
    reqPrefix: BACKTEST_REQ_PREFIX,
    resPrefix: BACKTEST_RES_PREFIX,
    reqTtl: BACKTEST_KEY_TTL_SECS,
    maxWaitMs: BACKTEST_MAX_WAIT_MS,
    timeoutMsg: 'backtest timed out after 180s. Check the paper worker logs.',
  }, payload);
}

async function fetchCandlesViaRedis(redis, payload) {
  return jobViaRedis(redis, {
    queueKey: CANDLES_QUEUE_KEY,
    reqPrefix: CANDLES_REQ_PREFIX,
    resPrefix: CANDLES_RES_PREFIX,
    reqTtl: CANDLES_KEY_TTL_SECS,
    maxWaitMs: CANDLES_MAX_WAIT_MS,
    timeoutMsg: 'candles fetch timed out after 30s. Paper worker may be down.',
  }, payload);
}

const SCANNER_ORDER_QUEUE_KEY = 'silver-swing:scanner_order:queue';
const SCANNER_ORDER_REQ_PREFIX = 'silver-swing:scanner_order:req:';
const SCANNER_ORDER_RES_PREFIX = 'silver-swing:scanner_order:res:';
const SCANNER_ORDER_MAX_WAIT_MS = 30_000;
const SCANNER_ORDER_TTL_SECS = 120;

const LIVE_PORTFOLIO_QUEUE_KEY = 'silver-swing:live_portfolio:queue';
const LIVE_PORTFOLIO_REQ_PREFIX = 'silver-swing:live_portfolio:req:';
const LIVE_PORTFOLIO_RES_PREFIX = 'silver-swing:live_portfolio:res:';
const LIVE_PORTFOLIO_MAX_WAIT_MS = 15_000;
const LIVE_PORTFOLIO_TTL_SECS = 60;

async function livePortfolioViaRedis(redis, payload) {
  return jobViaRedis(redis, {
    queueKey: LIVE_PORTFOLIO_QUEUE_KEY,
    reqPrefix: LIVE_PORTFOLIO_REQ_PREFIX,
    resPrefix: LIVE_PORTFOLIO_RES_PREFIX,
    reqTtl: LIVE_PORTFOLIO_TTL_SECS,
    maxWaitMs: LIVE_PORTFOLIO_MAX_WAIT_MS,
    timeoutMsg: 'live portfolio fetch timed out after 15s. Paper worker may be down.',
  }, payload);
}

async function scannerOrderViaRedis(redis, payload) {
  return jobViaRedis(redis, {
    queueKey: SCANNER_ORDER_QUEUE_KEY,
    reqPrefix: SCANNER_ORDER_REQ_PREFIX,
    resPrefix: SCANNER_ORDER_RES_PREFIX,
    reqTtl: SCANNER_ORDER_TTL_SECS,
    maxWaitMs: SCANNER_ORDER_MAX_WAIT_MS,
    timeoutMsg: 'scanner order timed out after 30s. Paper worker may be down — check Render logs.',
  }, payload);
}

async function jobViaRedis(redis, cfg, payload) {
  const jobId = `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
  const reqKey = `${cfg.reqPrefix}${jobId}`;
  const resKey = `${cfg.resPrefix}${jobId}`;
  await redis.set(reqKey, JSON.stringify(payload), { EX: cfg.reqTtl });
  await redis.lPush(cfg.queueKey, jobId);

  const started = Date.now();
  while (Date.now() - started < cfg.maxWaitMs) {
    await new Promise(r => setTimeout(r, JOB_POLL_INTERVAL_MS));
    const raw = await redis.get(resKey);
    if (raw) {
      await redis.del(resKey);
      try {
        return JSON.parse(raw);
      } catch (e) {
        return { ok: false, error: `worker returned unparseable result: ${e.message}` };
      }
    }
  }
  return { ok: false, error: cfg.timeoutMsg };
}

async function tailJsonl(logPath, n) {
  const r = await getRedis();
  if (r) {
    // Python-side pushes via LPUSH (newest at head), we return oldest→newest.
    const raw = await r.lRange(REDIS_TRADES_KEY, 0, n - 1);
    return raw.slice().reverse().map(line => {
      try { return JSON.parse(line); } catch { return null; }
    }).filter(Boolean);
  }
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
    console.log(`  store: ${REDIS_URL ? `redis:${REDIS_STORE_KEY}` : STORE_PATH}`);
    console.log(`  trade log: ${REDIS_URL ? `redis:${REDIS_TRADES_KEY}` : TRADE_LOG_PATH}`);
    console.log(`  auth: ${DASHBOARD_PASSWORD ? 'ENABLED' : 'disabled (dev only)'}`);
  });
}
