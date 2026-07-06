/**
 * dashboard/public/app.js — reads and (safely) writes state via /api/*.
 *
 * Flow:
 *   check session → login OR dashboard → poll /api/status + /api/trades every 5s
 *   → group by asset class (§8A) → render per-instrument cards with snapshot data
 *   → HALT banner overlays when anything is halted
 *   → kill switch button opens confirm modal → POST /api/kill-switch/activate
 *   → edit config button opens editable form → PUT /api/config with server-side validation
 *   → strategy explainer button opens six-part panel (§7A) → separate confirm to select
 *   → backtest button opens window/mode picker → POST /api/backtest → leaderboard
 */

const POLL_MS = 5000;

// ---- DOM refs ------------------------------------------------------------

const loginView = document.getElementById('login-view');
const dashboardView = document.getElementById('dashboard-view');
const loginForm = document.getElementById('login-form');
const loginError = document.getElementById('login-error');
const passwordInput = document.getElementById('password');
const logoutBtn = document.getElementById('logout-btn');
const lastUpdated = document.getElementById('last-updated');
const cardsEl = document.getElementById('instrument-cards');
const tradeLogEl = document.getElementById('trade-log');
const haltBanner = document.getElementById('halt-banner');
const killBanner = document.getElementById('kill-banner');
const killBtn = document.getElementById('kill-switch-btn');
const assetTabs = document.getElementById('asset-tabs');

const configModal = document.getElementById('config-modal');
const configForm = document.getElementById('config-form');
const configErrors = document.getElementById('config-errors');
const configSaveBtn = document.getElementById('config-save');
const configTitle = document.getElementById('config-modal-title');

const killModal = document.getElementById('kill-modal');
const killModalTitle = document.getElementById('kill-modal-title');
const killModalBody = document.getElementById('kill-modal-body');
const killReason = document.getElementById('kill-reason');
const killConfirm = document.getElementById('kill-confirm');

const strategyModal = document.getElementById('strategy-modal');
const strategyBody = document.getElementById('strategy-body');
const strategyTitle = document.getElementById('strategy-modal-title');
const strategySelect = document.getElementById('strategy-select');

const backtestModal = document.getElementById('backtest-modal');
const backtestForm = document.getElementById('backtest-form');
const backtestResult = document.getElementById('backtest-result');

// ---- state ---------------------------------------------------------------

let pollHandle = null;
let currentStore = {};          // last full /api/status
let activeAssetClass = null;    // sidebar-tab filter
let configEditContext = null;   // {tenant, symbol} while modal open
let killContext = null;         // {tenant, mode: 'activate'|'clear'} while modal open
let strategyContext = null;     // {tenant, symbol, name} while modal open
let backtestContext = null;

// ---- fetch helpers -------------------------------------------------------

async function fetchJson(url, opts = {}) {
  const res = await fetch(url, { credentials: 'same-origin', ...opts });
  if (res.status === 401) return { _unauthorized: true };
  return { _status: res.status, ...(await res.json().catch(() => ({}))) };
}

async function postJson(url, body) {
  return fetchJson(url, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
}

async function putJson(url, body) {
  return fetchJson(url, {
    method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
}

// ---- session -------------------------------------------------------------

async function checkSession() { return await fetchJson('/api/session'); }

async function tryLogin(password) {
  return await postJson('/login', { password });
}

async function logout() {
  await postJson('/logout');
  showLogin();
}

function showLogin() {
  if (pollHandle) { clearInterval(pollHandle); pollHandle = null; }
  loginView.hidden = false;
  dashboardView.hidden = true;
  logoutBtn.hidden = true;
  killBtn.hidden = true;
  passwordInput.focus();
}

function showDashboard(authRequired) {
  loginView.hidden = true;
  dashboardView.hidden = false;
  logoutBtn.hidden = !authRequired;
  killBtn.hidden = false;
  refreshOnce();
  pollHandle = setInterval(refreshOnce, POLL_MS);
}

// ---- formatting ---------------------------------------------------------

function fmtMoney(n, opts = {}) {
  if (n === null || n === undefined) return '—';
  const v = Number(n);
  if (Number.isNaN(v)) return '—';
  return v.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: opts.decimals ?? 2, minimumFractionDigits: opts.decimals ?? 2 });
}

function fmtNum(n, decimals = 2) {
  if (n === null || n === undefined) return '—';
  const v = Number(n);
  return Number.isNaN(v) ? '—' : v.toFixed(decimals);
}

function fmtHeartbeat(ts) {
  if (!ts) return 'never';
  const age = Date.now() / 1000 - ts;
  if (age < 60) return `${age.toFixed(0)}s ago`;
  if (age < 3600) return `${(age / 60).toFixed(0)}m ago`;
  return `${(age / 3600).toFixed(1)}h ago`;
}

function classForValue(n) {
  const v = Number(n);
  if (Number.isNaN(v) || v === 0) return 'dim';
  return v > 0 ? 'pos' : 'neg';
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, ch => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]
  ));
}

// ---- asset class inference ----------------------------------------------

function assetClassOf(symbol) {
  // SLR = silver (CDE), GC = gold, CL = crude, etc.  Crypto perps look like BTC-PERP-INTX.
  if (/^(SLR|SIL|GC|GOLD|PA|PL|HG|COPPER)/.test(symbol)) return 'metals';
  if (/^(CL|NG|BZ|RB|HO)/.test(symbol)) return 'energy';
  if (/-PERP-/.test(symbol) || /^(BTC|ETH|SOL|BCH|LTC|XRP)-/.test(symbol)) return 'crypto';
  if (/^(ES|NQ|YM|RTY)/.test(symbol)) return 'equity';
  return 'other';
}

function iconForAssetClass(c) {
  return { metals: '⚪', energy: '⛽', crypto: '₿', equity: '📈', other: '📊' }[c] || '📊';
}

// ---- HALT + kill banners ------------------------------------------------

function renderBanners(store) {
  const haltedInstruments = [];
  let killActive = null;

  for (const [tenant, symbols] of Object.entries(store)) {
    for (const [symbol, block] of Object.entries(symbols || {})) {
      if (symbol === '__account_kill_switch__') {
        const c = block.config || {};
        if (c.active) killActive = { tenant, reason: c.reason, ts: c.activated_ts };
        continue;
      }
      const s = block.state || {};
      if (s.state === 'HALTED') haltedInstruments.push({ tenant, symbol });
    }
  }

  if (haltedInstruments.length > 0) {
    haltBanner.hidden = false;
    haltBanner.innerHTML = `⚠ HALTED — ${haltedInstruments.map(h => `${escapeHtml(h.tenant)}/${escapeHtml(h.symbol)}`).join(', ')}. Review the trade log for the reason. Clearing HALT requires a config change AND a deliberate re-arm.`;
  } else {
    haltBanner.hidden = true;
  }

  if (killActive) {
    killBanner.hidden = false;
    const reason = killActive.reason ? ` — ${escapeHtml(killActive.reason)}` : '';
    killBanner.innerHTML = `⏸ KILL SWITCH ACTIVE for ${escapeHtml(killActive.tenant)}${reason}. Bot will not arm new legs.`;
    killBtn.textContent = 'RESUME';
    killBtn.dataset.mode = 'clear';
  } else {
    killBanner.hidden = true;
    killBtn.textContent = 'PAUSE ALL';
    killBtn.dataset.mode = 'activate';
  }
}

// ---- asset-class tabs ---------------------------------------------------

function renderAssetTabs(store) {
  const counts = {};
  for (const [_, symbols] of Object.entries(store)) {
    for (const symbol of Object.keys(symbols || {})) {
      if (symbol === '__account_kill_switch__') continue;
      const c = assetClassOf(symbol);
      counts[c] = (counts[c] || 0) + 1;
    }
  }
  const classes = Object.keys(counts).sort();

  assetTabs.innerHTML = '';
  if (classes.length <= 1) return;  // no need for tabs if only one class

  const allBtn = document.createElement('button');
  allBtn.className = 'tab' + (activeAssetClass === null ? ' active' : '');
  allBtn.innerHTML = `all <span class="tab-count">${Object.values(counts).reduce((a, b) => a + b, 0)}</span>`;
  allBtn.onclick = () => { activeAssetClass = null; refreshOnce(); };
  assetTabs.appendChild(allBtn);

  for (const c of classes) {
    const btn = document.createElement('button');
    btn.className = 'tab' + (activeAssetClass === c ? ' active' : '');
    btn.innerHTML = `${iconForAssetClass(c)} ${c} <span class="tab-count">${counts[c]}</span>`;
    btn.onclick = () => { activeAssetClass = c; refreshOnce(); };
    assetTabs.appendChild(btn);
  }
}

// ---- position lane -------------------------------------------------------

function renderPositionLane(state, config, snapshot) {
  const posQty = snapshot?.position_qty ?? state?.swing_qty ?? 0;
  const core = config?.core_qty ?? 0;
  const swingHeld = Math.max(0, posQty - core);
  const swingArmed = state?.swing_qty ?? 0;
  return `
    <div class="position-lane">
      <div class="position-lane-row">
        <span class="lane-swing">◆ swing sleeve: ${swingHeld} held / ${swingArmed} armed</span>
        <span class="lane-core">◼ core floor: ${core} (never sold)</span>
      </div>
      <div class="position-lane-row" style="margin-top:4px;color:var(--muted);font-size:11px;">
        <span>total held: ${posQty}</span>
        <span>avg entry: ${fmtNum(snapshot?.position_avg_entry, 3)}</span>
      </div>
    </div>
  `;
}

// ---- margin bar ---------------------------------------------------------

function renderMarginBar(snapshot) {
  if (!snapshot) return '';
  const used = Number(snapshot.margin_used ?? snapshot.initial_margin ?? 0);
  const equity = Number(snapshot.equity ?? 0);
  if (!equity) return '';
  const ratio = Math.min(1, used / equity);
  const pct = (ratio * 100).toFixed(1);
  const cls = ratio > 0.75 ? 'crit' : ratio > 0.5 ? 'warn' : '';
  return `
    <div class="field">
      <span class="field-label">margin usage</span>
      <span class="field-value ${cls === 'crit' ? 'neg' : ''}">${pct}%</span>
      <div class="margin-bar"><div class="margin-bar-fill ${cls}" style="width:${pct}%"></div></div>
    </div>
  `;
}

// ---- cards --------------------------------------------------------------

function renderCard(tenant, symbol, { config, state, snapshot }) {
  const s = state || {};
  const c = config || {};
  const snap = snapshot || {};
  const halted = s.state === 'HALTED';
  const legPill = { ARMED_SELL: 'armed-sell', ARMED_BUY: 'armed-buy', HALTED: 'halted' }[s.state] || '';
  const modeLabel = snap.mode === 'live' ? 'LIVE' : snap.mode === 'paper' ? 'PAPER' : '';

  const el = document.createElement('article');
  el.className = 'card' + (halted ? ' halted' : '');
  el.innerHTML = `
    <h2>
      <span>${escapeHtml(tenant)} / ${escapeHtml(symbol)} ${modeLabel ? `<span class="pill dim">${modeLabel}</span>` : ''}</span>
      <span class="card-actions">
        <span class="pill ${legPill}">${escapeHtml(s.state || 'unknown')}</span>
        <button data-action="edit" data-tenant="${escapeHtml(tenant)}" data-symbol="${escapeHtml(symbol)}">config</button>
        <button data-action="explain" data-tenant="${escapeHtml(tenant)}" data-symbol="${escapeHtml(symbol)}" data-name="${escapeHtml(c.exit_mode || 'fixed_limit')}">strategy</button>
        <button data-action="backtest" data-tenant="${escapeHtml(tenant)}" data-symbol="${escapeHtml(symbol)}">backtest</button>
      </span>
    </h2>

    <div class="card-section">
      <h3>account</h3>
      <div class="grid">
        <div class="field"><span class="field-label">equity</span><span class="field-value">${fmtMoney(snap.equity)}</span></div>
        <div class="field"><span class="field-label">unrealized P&amp;L</span><span class="field-value ${classForValue(snap.unrealized_pnl)}">${fmtMoney(snap.unrealized_pnl)}</span></div>
        <div class="field"><span class="field-label">realized P&amp;L</span><span class="field-value ${classForValue(s.realized_pnl ?? snap.realized_pnl)}">${fmtMoney(s.realized_pnl ?? snap.realized_pnl)}</span></div>
        <div class="field"><span class="field-label">fees paid</span><span class="field-value dim">${fmtMoney(snap.fees_paid ?? 0)}</span></div>
        <div class="field"><span class="field-label">buying power</span><span class="field-value">${fmtMoney(snap.futures_buying_power ?? snap.available_margin)}</span></div>
        <div class="field"><span class="field-label">liq buffer</span><span class="field-value dim">${fmtMoney(snap.liquidation_buffer)}</span></div>
        ${renderMarginBar(snap)}
        <div class="field"><span class="field-label">max drawdown</span><span class="field-value ${classForValue(-(snap.max_drawdown ?? 0))}">${fmtMoney(snap.max_drawdown ?? 0)}</span></div>
      </div>
    </div>

    ${renderPositionLane(s, c, snap)}

    <div class="card-section">
      <h3>strategy</h3>
      <div class="grid">
        <div class="field"><span class="field-label">exit mode</span><span class="field-value dim">${escapeHtml(c.exit_mode || 'fixed_limit')}</span></div>
        <div class="field"><span class="field-label">sell / buy</span><span class="field-value">${fmtNum(c.sell_px, 3)} / ${fmtNum(c.buy_px, 3)}</span></div>
        <div class="field"><span class="field-label">abort ↓ / ↑</span><span class="field-value dim">${fmtNum(c.abort_below, 2)} / ${fmtNum(c.abort_above, 2)}</span></div>
        <div class="field"><span class="field-label">mark</span><span class="field-value">${fmtNum(snap.last_mark, 3)}</span></div>
        <div class="field"><span class="field-label">bid / ask</span><span class="field-value dim">${fmtNum(snap.best_bid, 3)} / ${fmtNum(snap.best_ask, 3)}</span></div>
        <div class="field"><span class="field-label">cycles</span><span class="field-value">${s.cycles ?? 0}</span></div>
      </div>
    </div>

    ${renderMiniChart(symbol, c, snap)}

    <div class="card-section">
      <h3>runtime</h3>
      <div class="grid">
        <div class="field"><span class="field-label">live order</span><span class="field-value dim">${escapeHtml((s.live_order_id || '—').slice(0, 24))}</span></div>
        <div class="field"><span class="field-label">last heartbeat</span><span class="field-value dim">${fmtHeartbeat(s.last_heartbeat_ts)}</span></div>
        <div class="field"><span class="field-label">reserved margin</span><span class="field-value dim">${fmtMoney(s.reserved_margin)}</span></div>
      </div>
    </div>
  `;
  return el;
}

// ---- mini SVG chart ------------------------------------------------------

/** Simple SVG chart showing the price zone (buy/sell/abort levels) around
 *  the current mark. Not a full annotated chart with time axis — for that
 *  the caller should open the backtest modal to see history. This is a
 *  quick visual "am I in the zone?" glance. */
function renderMiniChart(symbol, config, snapshot) {
  const mark = Number(snapshot?.last_mark);
  const buy = Number(config?.buy_px);
  const sell = Number(config?.sell_px);
  const abortBelow = Number(config?.abort_below);
  const abortAbove = Number(config?.abort_above);
  if (!isFinite(mark) || !isFinite(buy) || !isFinite(sell)) return '';

  const low = Math.min(abortBelow || buy - 1, mark - 1);
  const high = Math.max(abortAbove || sell + 1, mark + 1);
  const range = high - low;
  const scale = v => 300 - ((v - low) / range) * 280 - 10;
  const w = 100;

  const markY = scale(mark);
  const sellY = scale(sell);
  const buyY = scale(buy);
  const abortBelowY = isFinite(abortBelow) ? scale(abortBelow) : 300;
  const abortAboveY = isFinite(abortAbove) ? scale(abortAbove) : 0;

  return `
    <svg viewBox="0 0 ${w} 300" width="100%" height="120" preserveAspectRatio="none" style="background:var(--panel-2);border-radius:4px;margin:8px 0;">
      <rect x="0" y="0" width="${w}" height="${abortAboveY}" fill="rgba(244,63,94,0.05)" />
      <rect x="0" y="${abortBelowY}" width="${w}" height="${300 - abortBelowY}" fill="rgba(244,63,94,0.05)" />

      <line x1="0" y1="${sellY}" x2="${w}" y2="${sellY}" stroke="#4ade80" stroke-width="1" stroke-dasharray="4,2" />
      <text x="2" y="${sellY - 3}" fill="#4ade80" font-size="8" font-family="monospace">sell ${sell.toFixed(3)}</text>

      <line x1="0" y1="${buyY}" x2="${w}" y2="${buyY}" stroke="#60a5fa" stroke-width="1" stroke-dasharray="4,2" />
      <text x="2" y="${buyY + 10}" fill="#60a5fa" font-size="8" font-family="monospace">buy ${buy.toFixed(3)}</text>

      <line x1="0" y1="${markY}" x2="${w}" y2="${markY}" stroke="#e6ecf3" stroke-width="1.5" />
      <text x="70" y="${markY - 3}" fill="#e6ecf3" font-size="8" font-family="monospace">mark ${mark.toFixed(3)}</text>

      ${isFinite(abortBelow) ? `<line x1="0" y1="${abortBelowY}" x2="${w}" y2="${abortBelowY}" stroke="#f43f5e" stroke-width="1" opacity="0.5" /><text x="2" y="${abortBelowY - 3}" fill="#f43f5e" font-size="8" font-family="monospace">abort↓ ${abortBelow.toFixed(2)}</text>` : ''}
      ${isFinite(abortAbove) ? `<line x1="0" y1="${abortAboveY}" x2="${w}" y2="${abortAboveY}" stroke="#f43f5e" stroke-width="1" opacity="0.5" /><text x="2" y="${abortAboveY + 10}" fill="#f43f5e" font-size="8" font-family="monospace">abort↑ ${abortAbove.toFixed(2)}</text>` : ''}
    </svg>
  `;
}

// ---- trades log ---------------------------------------------------------

function priorityClass(eventType) {
  if (eventType === 'halt' || eventType === 'reconcile_halt' || eventType === 'fee_gate_halt') return 'crit';
  if (eventType === 'kill_switch_pause' || eventType === 'cancel_failed' || eventType === 'fee_gate_preview_failed') return 'warn';
  return '';
}

function renderTradeEvent(ev) {
  const li = document.createElement('li');
  li.className = priorityClass(ev.event_type);
  const ts = new Date((ev.ts || 0) * 1000).toISOString().slice(11, 19);
  const detail = { ...ev };
  delete detail.ts; delete detail.event_type; delete detail.tenant; delete detail.symbol;
  li.innerHTML = `
    <span class="event-ts">${ts}</span>
    <span class="event-type">${escapeHtml(ev.event_type || '?')}${ev.symbol ? ` <span style="color:var(--muted)">${escapeHtml(ev.symbol)}</span>` : ''}</span>
    <span class="event-detail">${escapeHtml(JSON.stringify(detail).slice(0, 400))}</span>
  `;
  return li;
}

// ---- refresh loop -------------------------------------------------------

async function refreshOnce() {
  const [status, trades] = await Promise.all([
    fetchJson('/api/status'),
    fetchJson('/api/trades?n=60'),
  ]);
  if (status._unauthorized || trades._unauthorized) { showLogin(); return; }
  currentStore = status.store || {};
  renderBanners(currentStore);
  renderAssetTabs(currentStore);

  cardsEl.innerHTML = '';
  const tenants = Object.keys(currentStore).sort();
  let anyRendered = false;
  for (const tenant of tenants) {
    const symbols = Object.keys(currentStore[tenant] || {}).sort();
    for (const symbol of symbols) {
      if (symbol === '__account_kill_switch__') continue;
      if (activeAssetClass && assetClassOf(symbol) !== activeAssetClass) continue;
      cardsEl.appendChild(renderCard(tenant, symbol, currentStore[tenant][symbol]));
      anyRendered = true;
    }
  }
  if (!anyRendered) {
    cardsEl.innerHTML = '<div class="field-value dim">no state yet — has the bot run?</div>';
  }

  tradeLogEl.innerHTML = '';
  for (const ev of (trades.events || []).slice().reverse()) {
    tradeLogEl.appendChild(renderTradeEvent(ev));
  }

  lastUpdated.textContent = `updated ${new Date().toLocaleTimeString()}`;
}

// ---- CONFIG editor -------------------------------------------------------

const CONFIG_FIELD_SPEC = [
  ['core_qty', 'core qty (floor)', 'number', { step: 1, min: 1 }],
  ['swing_qty', 'swing qty', 'number', { step: 1, min: 1 }],
  ['max_swing_qty', 'max swing qty', 'number', { step: 1, min: 1 }],
  ['sell_px', 'sell price', 'number', { step: 0.005 }],
  ['buy_px', 'buy price', 'number', { step: 0.005 }],
  ['abort_below', 'abort below', 'number', { step: 0.01 }],
  ['abort_above', 'abort above', 'number', { step: 0.01 }],
  ['exit_mode', 'exit mode', 'select', { options: ['fixed_limit', 'trailing_stop'] }],
  ['trail_trigger', 'trail trigger', 'number', { step: 0.005 }],
  ['trail_distance', 'trail distance', 'number', { step: 0.005 }],
  ['reanchor_threshold', 're-anchor threshold', 'number', { step: 0.1 }],
  ['contract_size', 'contract size', 'number', { step: 1 }],
  ['margin_per_contract', 'margin/contract', 'number', { step: 1 }],
  ['fee_per_contract_roundtrip', 'fee per roundtrip', 'number', { step: 0.01 }],
  ['scale_up_buffer_mult', 'scale-up buffer ×', 'number', { step: 0.1, min: 1 }],
  ['fee_sanity_multiplier', 'fee sanity ×', 'number', { step: 0.1, min: 1 }],
];

function openConfigEditor(tenant, symbol) {
  const cfg = currentStore[tenant]?.[symbol]?.config || {};
  configEditContext = { tenant, symbol };
  configTitle.textContent = `edit config — ${tenant} / ${symbol}`;
  configForm.innerHTML = '';
  configErrors.innerHTML = '';
  for (const [key, label, type, opts] of CONFIG_FIELD_SPEC) {
    const wrap = document.createElement('label');
    wrap.innerHTML = `<span>${label}</span>`;
    let input;
    if (type === 'select') {
      input = document.createElement('select');
      input.name = key;
      for (const o of opts.options) {
        const opt = document.createElement('option');
        opt.value = o; opt.textContent = o;
        if (String(cfg[key] || '') === o) opt.selected = true;
        input.appendChild(opt);
      }
    } else {
      input = document.createElement('input');
      input.type = type;
      input.name = key;
      input.value = cfg[key] ?? '';
      if (opts?.step != null) input.step = opts.step;
      if (opts?.min != null) input.min = opts.min;
    }
    wrap.appendChild(input);
    configForm.appendChild(wrap);
  }
  configModal.hidden = false;
}

async function saveConfig() {
  if (!configEditContext) return;
  const cfg = {};
  for (const [key, , type] of CONFIG_FIELD_SPEC) {
    const input = configForm.querySelector(`[name="${key}"]`);
    if (!input) continue;
    let val = input.value;
    if (val === '' || val === null || val === undefined) continue;
    if (type === 'number') val = Number(val);
    cfg[key] = val;
  }
  const res = await putJson('/api/config', {
    tenant: configEditContext.tenant,
    symbol: configEditContext.symbol,
    config: cfg,
  });
  if (res.ok) {
    configModal.hidden = true;
    refreshOnce();
  } else {
    configErrors.innerHTML = (res.issues || [{ message: res.error || 'save failed' }])
      .map(i => `<div class="issue-item"><b>${escapeHtml(i.field || '')}</b> ${escapeHtml(i.message || '')}</div>`)
      .join('');
  }
}

// ---- kill switch --------------------------------------------------------

function openKillModal(tenant, mode) {
  killContext = { tenant, mode };
  if (mode === 'activate') {
    killModalTitle.textContent = 'pause all trading?';
    killModalBody.innerHTML = 'This freezes arming across every instrument for this tenant. Existing positions are not closed; existing orders on the exchange are NOT cancelled. Only new legs are blocked until you resume.';
    killConfirm.textContent = 'CONFIRM PAUSE';
    killReason.hidden = false;
  } else {
    killModalTitle.textContent = 'resume trading?';
    killModalBody.innerHTML = `Kill switch will be cleared. Any HALTED instrument is separately still halted — clearing kill does NOT auto-arm those.`;
    killConfirm.textContent = 'CONFIRM RESUME';
    killReason.hidden = true;
  }
  killReason.value = '';
  killModal.hidden = false;
}

async function confirmKill() {
  if (!killContext) return;
  const url = killContext.mode === 'activate'
    ? '/api/kill-switch/activate'
    : '/api/kill-switch/clear';
  const body = killContext.mode === 'activate'
    ? { tenant: killContext.tenant, confirm: 'YES', reason: killReason.value || 'triggered from dashboard' }
    : { tenant: killContext.tenant, confirm: 'YES', cleared_by: 'dashboard' };
  const res = await postJson(url, body);
  if (res.ok) {
    killModal.hidden = true;
    refreshOnce();
  } else {
    alert('failed: ' + (res.error || 'unknown'));
  }
}

// ---- strategy explainer --------------------------------------------------

const STRATEGY_COPY = {
  fixed_limit: {
    title: 'fixed-limit swing',
    summary: 'Sells a fixed slice at a set high, rebuys the same slice at a set low, repeat.',
    expert: 'Range-scalping in the spirit of Carter\'s use of round-number levels as reference points.',
    regime: {
      best: 'a sideways, range-bound market that keeps bouncing between two levels.',
      worst: 'a trend — it sells at the top of its range and gets left behind if price keeps running.',
    },
    mechanics: 'Sells swing_qty at sell_px, rebuys swing_qty at buy_px; core never sold; profit funds growth (§4). Position oscillates between floor and floor+swing.',
    tradeoff: 'It will always sell too early into a real breakout. Tighter ranges also let fees eat a bigger share of each cycle.',
    recommended: 'sell_px / buy_px = nearest whole-number range price is currently orbiting.',
  },
  trailing_stop: {
    title: 'trailing / ratchet swing',
    summary: 'Arms at the high trigger but doesn\'t sell — trails a stop under price that ratchets up, and only sells when price falls back through it.',
    expert: 'Volatility-breakout thinking (Williams) for detecting the run, plus a trailing exit to ride it.',
    regime: {
      best: 'a market that can break out and trend (won\'t cap you at 65 while silver goes to 80).',
      worst: 'a choppy range — normal wiggle trips the trail and stops you out early.',
    },
    mechanics: 'On trigger, arms a trailing stop at distance trail; rides the move; on the pullback fill it re-anchors the rebuy to the new level (§6). Core untouched throughout.',
    tradeoff: 'Gives back the trail distance off every top by design. Too tight → stopped by noise; too wide → give back a lot. No perfect number; regime-dependent.',
    recommended: 'trail = an ATR multiple of recent range; re-anchor target = new whole-number cluster.',
  },
};

function openStrategyExplainer(tenant, symbol, name) {
  strategyContext = { tenant, symbol, name };
  const copy = STRATEGY_COPY[name] || {
    title: name, summary: 'no explainer copy yet for this strategy',
    expert: '', regime: { best: '', worst: '' }, mechanics: '', tradeoff: '', recommended: '',
  };
  strategyTitle.textContent = `${copy.title} — ${tenant} / ${symbol}`;
  strategyBody.innerHTML = `
    <div class="strategy-section"><h3>1. one-line summary</h3><p>${escapeHtml(copy.summary)}</p></div>
    <div class="strategy-section"><h3>2. the expert &amp; the idea</h3><p>${escapeHtml(copy.expert)}</p></div>
    <div class="strategy-section"><h3>3. best in / worst in</h3><p><b>Best:</b> ${escapeHtml(copy.regime.best)}<br><b>Worst:</b> ${escapeHtml(copy.regime.worst)}</p></div>
    <div class="strategy-section"><h3>4. what it does to your money</h3><p>${escapeHtml(copy.mechanics)}</p></div>
    <div class="strategy-section"><h3>5. the tradeoff / what to watch</h3><p>${escapeHtml(copy.tradeoff)}</p></div>
    <div class="strategy-section"><h3>6. current recommended parameters</h3><p>${escapeHtml(copy.recommended)} <em>(computed suggestion, not a prediction)</em></p></div>
  `;
  strategyModal.hidden = false;
}

async function selectStrategyFromModal() {
  if (!strategyContext) return;
  // Second-step confirm: change exit_mode via config PUT
  const { tenant, symbol, name } = strategyContext;
  const current = currentStore[tenant]?.[symbol]?.config || {};
  const cfg = { ...current, exit_mode: name };
  const res = await putJson('/api/config', { tenant, symbol, config: cfg });
  if (res.ok) {
    strategyModal.hidden = true;
    refreshOnce();
  } else {
    alert('config update failed: ' + JSON.stringify(res.issues || res.error));
  }
}

// ---- backtest -----------------------------------------------------------

function openBacktest(tenant, symbol) {
  backtestContext = { tenant, symbol };
  backtestResult.innerHTML = '';
  backtestModal.hidden = false;
}

async function runBacktest(e) {
  e.preventDefault();
  if (!backtestContext) return;
  const days = document.getElementById('bt-window').value;
  const gran = document.getElementById('bt-gran').value;
  const mode = document.getElementById('bt-mode').value;

  backtestResult.innerHTML = '<div class="field-value dim">running… (this fetches candles from Coinbase and can take up to ~20s)</div>';
  const res = await postJson('/api/backtest', {
    tenant: backtestContext.tenant,
    symbol: backtestContext.symbol,
    days: Number(days), granularity: gran, mode,
  });

  if (res._unauthorized) { showLogin(); return; }
  if (!res.ok) {
    backtestResult.innerHTML = `<div class="error">${escapeHtml(res.error || 'backtest failed')}</div>`;
    return;
  }

  if (mode === 'compare_all') {
    backtestResult.innerHTML = renderLeaderboard(res.results);
  } else {
    backtestResult.innerHTML = renderBacktestSummary(res.result);
  }
}

function renderBacktestSummary(r) {
  if (!r) return '<div class="dim">no result</div>';
  return `
    <table class="leaderboard">
      <tr><td>starting balance</td><td>${fmtMoney(r.starting_balance)}</td></tr>
      <tr><td>final equity</td><td>${fmtMoney(r.final_equity)}</td></tr>
      <tr><td>total return</td><td class="${classForValue(r.total_return)}">${fmtMoney(r.total_return)} (${fmtNum(r.total_return_pct, 2)}%)</td></tr>
      <tr><td>realized P&amp;L</td><td class="${classForValue(r.realized_pnl)}">${fmtMoney(r.realized_pnl)}</td></tr>
      <tr><td>unrealized P&amp;L</td><td class="${classForValue(r.unrealized_pnl)}">${fmtMoney(r.unrealized_pnl)}</td></tr>
      <tr><td>fees paid</td><td>${fmtMoney(r.fees_paid)}</td></tr>
      <tr><td>max drawdown</td><td class="neg">${fmtMoney(r.max_drawdown)} (${fmtNum(r.max_drawdown_pct, 2)}%)</td></tr>
      <tr><td>cycles</td><td>${r.cycles}</td></tr>
      <tr><td>fills</td><td>${r.fills}</td></tr>
      <tr><td>halted?</td><td>${r.halted ? 'yes — ' + escapeHtml(r.halt_reason || '') : 'no'}</td></tr>
    </table>
  `;
}

function renderLeaderboard(results) {
  if (!results?.length) return '<div class="dim">no results</div>';
  const rows = results.map(r => `
    <tr>
      <td>${escapeHtml(r.strategy)}</td>
      <td class="${classForValue(r.total_return)}">${fmtMoney(r.total_return)}</td>
      <td>${fmtNum(r.total_return_pct, 2)}%</td>
      <td class="neg">${fmtMoney(r.max_drawdown)}</td>
      <td>${r.cycles}</td>
      <td>${r.fills}</td>
      <td>${r.halted ? '⚠' : '✓'}</td>
    </tr>
  `).join('');
  return `
    <table class="leaderboard">
      <thead>
        <tr>
          <th>strategy</th><th>return</th><th>return %</th><th>max dd</th>
          <th>cycles</th><th>fills</th><th>ok</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="overfit-warning" style="margin-top:16px">
      Ranked on this specific window. Whichever strategy wins here won THIS
      slice of history. Try 3+ windows spanning different regimes before
      trusting the ranking.
    </div>
  `;
}

// ---- delegated events ---------------------------------------------------

document.addEventListener('click', (e) => {
  const btn = e.target.closest('button');
  if (!btn) return;
  if (btn.dataset.close !== undefined) {
    btn.closest('.modal').hidden = true;
    return;
  }
  const action = btn.dataset.action;
  const { tenant, symbol, name } = btn.dataset;
  if (action === 'edit') openConfigEditor(tenant, symbol);
  else if (action === 'explain') openStrategyExplainer(tenant, symbol, name);
  else if (action === 'backtest') openBacktest(tenant, symbol);
});

killBtn.addEventListener('click', () => {
  const mode = killBtn.dataset.mode || 'activate';
  const tenant = Object.keys(currentStore)[0] || 'adam';
  openKillModal(tenant, mode);
});
killConfirm.addEventListener('click', confirmKill);
configSaveBtn.addEventListener('click', saveConfig);
strategySelect.addEventListener('click', selectStrategyFromModal);
backtestForm.addEventListener('submit', runBacktest);

loginForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  loginError.textContent = '';
  const result = await tryLogin(passwordInput.value);
  if (result.ok) {
    passwordInput.value = '';
    showDashboard(true);
  } else {
    loginError.textContent = result.error || 'login failed';
  }
});
logoutBtn.addEventListener('click', logout);

// ---- boot ---------------------------------------------------------------

(async () => {
  const sess = await checkSession();
  if (!sess.auth_required || sess.authed) {
    showDashboard(sess.auth_required);
  } else {
    showLogin();
  }
})();
