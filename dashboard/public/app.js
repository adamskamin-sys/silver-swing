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
const resetPaperBtn = document.getElementById('reset-paper-btn');
const assetTabs = document.getElementById('asset-tabs');
const modeTabs = document.getElementById('mode-tabs');

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

const tradeModal = document.getElementById('trade-modal');
const tradeModalTitle = document.getElementById('trade-modal-title');
const tradeModalBody = document.getElementById('trade-modal-body');
const tradeQty = document.getElementById('trade-qty');
const tradePreview = document.getElementById('trade-preview');
const tradeError = document.getElementById('trade-error');
const tradeConfirm = document.getElementById('trade-confirm');

// ---- state ---------------------------------------------------------------

let pollHandle = null;
let currentStore = {};          // last full /api/status
let activeAssetClass = null;    // sidebar-tab filter
let activeMode = 'paper';       // 'paper' | 'live' | null(all) — top-level mode tab
let configEditContext = null;   // {tenant, symbol} while modal open
let killContext = null;         // {tenant, mode: 'activate'|'clear'} while modal open
let strategyContext = null;     // {tenant, symbol, name} while modal open
let backtestContext = null;
let tradeContext = null;

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

function hideAllModals() {
  for (const m of document.querySelectorAll('.modal')) m.hidden = true;
}

function showLogin() {
  if (pollHandle) { clearInterval(pollHandle); pollHandle = null; }
  hideAllModals();  // don't leave modals hanging over the login screen
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
  resetPaperBtn.hidden = false;  // paper mode only — hidden in live mode by check below
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

// Pick a sensible decimal precision for a product's price. Micro-priced perps
// (PEPE at $0.00001) need 6-8 decimals; silver at $60 only needs 3. Prefers
// the product's tick_size when present in config; otherwise infers from
// magnitude so we never truncate meaningful digits.
function pricePrecisionFor(price, config) {
  const tick = Number(config?.tick_size) || 0;
  if (tick > 0) {
    const parts = tick.toString().split('.');
    if (parts.length > 1) return Math.min(8, parts[1].length);
    return 0;
  }
  const p = Math.abs(Number(price) || 0);
  if (p === 0) return 3;
  if (p >= 1000) return 2;
  if (p >= 10) return 3;
  if (p >= 1) return 4;
  if (p >= 0.01) return 5;
  if (p >= 0.0001) return 6;
  return 8;
}

// Format a price with dynamic precision. Config is optional — when missing,
// falls back to magnitude inference so this can be dropped in anywhere.
function fmtPrice(price, config) {
  return fmtNum(price, pricePrecisionFor(price, config));
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

// Human-readable label for a product_id. SLR-27AUG26-CDE → "SILVER (SLR)",
// AVE-20DEC30-CDE → "AVALANCHE (AVE)", BTC-PERP-INTX → "BITCOIN (BTC)". The
// display in the price bar was previously hardcoded to "SILVER (SLR)" so ANY
// tracked symbol would falsely read as silver — including AVE at $91, which is
// how this bug was found. Falls back to the raw family code if we don't have
// a friendly name yet.
const SYMBOL_FAMILY_NAMES = {
  SLR: 'SILVER', SIL: 'SILVER',
  GC: 'GOLD', GOLD: 'GOLD',
  PA: 'PALLADIUM', PL: 'PLATINUM',
  HG: 'COPPER', COPPER: 'COPPER',
  CL: 'CRUDE OIL', NG: 'NATURAL GAS', BZ: 'BRENT',
  BTC: 'BITCOIN', ETH: 'ETHEREUM', SOL: 'SOLANA', LTC: 'LITECOIN',
  XRP: 'RIPPLE', BCH: 'BITCOIN CASH', AVE: 'AVALANCHE', DOGE: 'DOGECOIN',
  LINK: 'CHAINLINK', UNI: 'UNISWAP', MATIC: 'POLYGON',
  ES: 'S&P 500', NQ: 'NASDAQ', YM: 'DOW', RTY: 'RUSSELL',
};
function symbolFamilyOf(symbol) {
  if (!symbol) return '';
  if (symbol.includes('-PERP-')) return symbol.split('-PERP-')[0];
  return symbol.split('-')[0] || '';
}
function symbolLabel(symbol) {
  const fam = symbolFamilyOf(symbol);
  const friendly = SYMBOL_FAMILY_NAMES[fam.toUpperCase()];
  return friendly ? `${friendly} (${fam})` : fam || symbol;
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
    haltBanner.innerHTML = `⚠ Strategy halted — ${haltedInstruments.map(h => `${escapeHtml(h.tenant)}/${escapeHtml(h.symbol)}`).join(', ')}. See the halt reason on the strategy row, fix the underlying issue, then click <b>Resume</b>.`;
  } else {
    haltBanner.hidden = true;
  }

  if (killActive) {
    killBanner.hidden = false;
    const reason = killActive.reason ? ` — ${escapeHtml(killActive.reason)}` : '';
    killBanner.innerHTML = `⏸ Bot paused${reason}. Not arming new orders until you resume.`;
    killBtn.textContent = 'Resume bot';
    killBtn.className = 'primary';
    killBtn.dataset.mode = 'clear';
  } else {
    killBanner.hidden = true;
    killBtn.textContent = 'Pause bot';
    killBtn.className = 'ghost';
    killBtn.dataset.mode = 'activate';
  }
}

// ---- asset-class tabs ---------------------------------------------------

function modeOfTenant(tenant) {
  // Tenant naming: `adam-paper`, `adam-live`, `adam-lab`. Lab is a dedicated
  // $100k learning sandbox for theory-based strategies — same paper broker
  // and feed, isolated from the primary paper tenant so experiments don't
  // pollute your main account.
  const t = String(tenant || '').toLowerCase();
  if (t.includes('lab')) return 'lab';
  if (t.includes('live')) return 'live';
  if (t.includes('paper')) return 'paper';
  return null;
}

function isLiveTenant(tenant) {
  return modeOfTenant(tenant) === 'live';
}

// Red-bordered "LIVE — REAL MONEY" confirmation. Returns a Promise resolving
// true (proceed) or false (cancel). Requires ticking a checkbox before the
// confirm button enables — an extra deliberate step so a rage-click can't
// blow past the safety net.
function confirmLive({ title = 'Confirm live action', body = 'This will place a real order on Coinbase.' } = {}) {
  return new Promise((resolve) => {
    const modal = document.getElementById('live-confirm-modal');
    const titleEl = document.getElementById('live-confirm-title');
    const bodyEl = document.getElementById('live-confirm-body');
    const checkEl = document.getElementById('live-confirm-check');
    const okBtn = document.getElementById('live-confirm-ok');
    const cancelBtn = document.getElementById('live-confirm-cancel');
    titleEl.textContent = title;
    bodyEl.innerHTML = body;
    checkEl.checked = false;
    okBtn.disabled = true;
    modal.hidden = false;
    const onCheck = () => { okBtn.disabled = !checkEl.checked; };
    const cleanup = () => {
      checkEl.removeEventListener('change', onCheck);
      okBtn.onclick = null;
      cancelBtn.onclick = null;
      modal.hidden = true;
    };
    checkEl.addEventListener('change', onCheck);
    okBtn.onclick = () => { cleanup(); resolve(true); };
    cancelBtn.onclick = () => { cleanup(); resolve(false); };
  });
}

function renderModeTabs(store) {
  const counts = { paper: 0, live: 0, lab: 0 };
  for (const [tenant, symbols] of Object.entries(store)) {
    const m = modeOfTenant(tenant);
    if (!m) continue;
    for (const symbol of Object.keys(symbols || {})) {
      if (symbol === '__account_kill_switch__') continue;
      counts[m] = (counts[m] || 0) + 1;
    }
  }
  modeTabs.innerHTML = '';

  const mk = (label, mode, badgeClass, badge = '') => {
    const b = document.createElement('button');
    b.className = 'tab mode-tab' + (activeMode === mode ? ' active' : '')
      + (badgeClass ? ' ' + badgeClass : '');
    b.innerHTML = `${label}${badge ? ` <span class="tab-count">${badge}</span>` : ''}`;
    b.onclick = () => { activeMode = mode; refreshOnce(); };
    return b;
  };
  modeTabs.appendChild(mk('paper', 'paper', 'mode-paper', counts.paper || 0));
  modeTabs.appendChild(mk('live', 'live', 'mode-live', counts.live || 0));
  modeTabs.appendChild(mk('lab', 'lab', 'mode-lab', counts.lab || 0));
  modeTabs.appendChild(mk('scanner', 'scanner', 'mode-scanner'));
}

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
        ${core > 0 ? `<span class="lane-core">◼ core floor: ${core} (never sold)</span>` : `<span class="lane-core">◼ no core floor · free trading</span>`}
      </div>
      <div class="position-lane-row" style="margin-top:4px;color:var(--muted);font-size:11px;">
        <span>total held: ${posQty}</span>
        <span>avg entry: ${fmtPrice(snapshot?.position_avg_entry)}</span>
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
  const modeLabel = snap.mode === 'live' ? 'LIVE' :
                    snap.mode === 'dry_run' ? 'DRY-RUN' :
                    snap.mode === 'paper' ? 'PAPER' : '';
  // Card-level pill: aggregate across primary + all sleeves and pick the most
  // meaningful active state. Ordering: HALTED > sell-capable > waiting-to-buy
  // (already sold, cycling) > idle (nothing possible). This way if the primary
  // is off and both sleeves are ARMED_BUY, the hero shows "Waiting for buy"
  // instead of a misleading "IDLE" — sleeves are actively cycling.
  const primaryQty = Number(c.swing_qty) || 0;
  const posQty = Number(snap.position_qty) || 0;
  const sleeves = Array.isArray(c.sleeves) ? c.sleeves : [];
  const sleeveStates = s.sleeves || {};
  const primaryActive = primaryQty > 0 && s.state !== 'HALTED';
  const primaryCanSell = primaryActive && s.state === 'ARMED_SELL' && posQty >= primaryQty;
  const primaryWaitingBuy = primaryActive && s.state === 'ARMED_BUY';
  const anySleeveHalted = sleeves.some(sc => (sleeveStates[sc.id]?.state) === 'HALTED');
  const anySleeveCanSell = sleeves.some(sc => {
    const ss = sleeveStates[sc.id] || {};
    return (ss.state || 'ARMED_SELL') === 'ARMED_SELL' && posQty >= Number(sc.qty || 0);
  });
  const anySleeveArmedBuy = sleeves.some(sc => (sleeveStates[sc.id]?.state) === 'ARMED_BUY');
  let displayState;
  if (halted || (primaryQty === 0 && sleeves.length && sleeves.every(sc => (sleeveStates[sc.id]?.state) === 'HALTED'))) {
    displayState = 'HALTED';
  } else if (primaryCanSell || anySleeveCanSell) {
    displayState = 'ARMED_SELL';
  } else if (primaryWaitingBuy || anySleeveArmedBuy) {
    displayState = 'ARMED_BUY';
  } else {
    displayState = 'IDLE';
  }
  const stateKey = (displayState || 'unknown').toLowerCase();

  // "Cycles complete" in the hero must include sleeve cycles too. Primary
  // cycles is the legacy single-strategy counter; if the user runs only
  // sleeves, that stays at 0 while real trading happens. Sum everything.
  const sleeveCyclesTotal = Object.values(sleeveStates).reduce(
    (n, ss) => n + (Number(ss?.cycles) || 0), 0);
  const totalCycles = (Number(s.cycles) || 0) + sleeveCyclesTotal;
  const sleeveRealizedTotal = Object.values(sleeveStates).reduce(
    (n, ss) => n + (Number(ss?.realized_pnl) || 0), 0);
  const totalRealized = (Number(s.realized_pnl) || 0) + sleeveRealizedTotal;

  const equity = snap.equity;
  const unrealized = snap.unrealized_pnl;
  // Sum realized across primary + sleeves so the hero reflects ALL trading,
  // not just the primary strategy's (which is 0 when only sleeves are running).
  const realized = totalRealized;
  const totalPnl = (Number(unrealized) || 0) + (Number(realized) || 0);

  const el = document.createElement('article');
  el.className = 'card' + (halted ? ' halted' : '');
  el.innerHTML = `
    <div class="card-hero">
      <div class="hero-top">
        <div>
          <h2 class="hero-symbol">${escapeHtml(symbol)} ${modeLabel ? `<span class="hero-mode">${modeLabel}</span>` : ''}</h2>
          <div class="hero-tenant">${escapeHtml(tenant)}</div>
        </div>
        <span class="status-pill ${stateKey}">${escapeHtml(prettyState(displayState))}</span>
      </div>
      <div class="hero-numbers">
        <div class="hero-metric">
          <span class="hero-label">Total value</span>
          <span class="hero-value">${fmtMoney(equity)}</span>
          <span class="hero-value-sub">${(Number(equity) > 100000 ? '+' : '')}${fmtMoney(Number(equity || 0) - 100000)} vs deposit</span>
        </div>
        <div class="hero-metric">
          <span class="hero-label">Today's P&amp;L</span>
          <span class="hero-value small ${classForValue(totalPnl)}">${totalPnl >= 0 ? '+' : ''}${fmtMoney(totalPnl)}</span>
          <span class="hero-value-sub">${fmtMoney(unrealized)} unrealized · ${fmtMoney(realized)} banked</span>
        </div>
        <div class="hero-metric">
          <span class="hero-label">Cycles complete</span>
          <span class="hero-value small">${totalCycles}</span>
          <span class="hero-value-sub">${fmtMoney(snap.fees_paid ?? 0)} paid in fees</span>
        </div>
      </div>
    </div>

    <div class="card-body">
      ${renderTargetsRow(c, snap)}
      ${renderContractInfo(symbol, c, snap)}
      ${renderPositionBar(s, c, snap)}
      ${renderLotsTable(snap, c, tenant, symbol, s)}
      ${renderRiskStrip(snap)}
      ${renderMicrostructurePanel(snap)}
      ${renderSleevesSection(tenant, symbol, c, s, snap)}
    </div>

    <div class="card-actions">
      <button class="primary" data-action="trade" data-tenant="${escapeHtml(tenant)}" data-symbol="${escapeHtml(symbol)}" data-side="BUY">Buy at market</button>
      <button class="danger" data-action="trade" data-tenant="${escapeHtml(tenant)}" data-symbol="${escapeHtml(symbol)}" data-side="SELL">Sell at market</button>
      <button data-action="backtest" data-tenant="${escapeHtml(tenant)}" data-symbol="${escapeHtml(symbol)}">Backtest</button>
      <button data-action="explain" data-tenant="${escapeHtml(tenant)}" data-symbol="${escapeHtml(symbol)}" data-name="${escapeHtml(c.exit_mode || 'fixed_limit')}">Strategy</button>
      <button class="ghost" data-action="edit" data-tenant="${escapeHtml(tenant)}" data-symbol="${escapeHtml(symbol)}">Settings</button>
    </div>

    <button class="details-toggle" data-action="toggle-details" data-target="details-${tenant}-${symbol}">More details</button>
    <div class="details-content" id="details-${tenant}-${symbol}" hidden>
      <div class="card-row">
        <div class="metric"><span class="metric-label">Margin used</span><span class="metric-value">${fmtNum(marginPct(snap), 1)}%</span></div>
        <div class="metric"><span class="metric-label">Buying power</span><span class="metric-value">${fmtMoney(snap.futures_buying_power ?? snap.available_margin)}</span></div>
        <div class="metric"><span class="metric-label">Max drawdown</span><span class="metric-value neg">${fmtMoney(snap.max_drawdown ?? 0)}</span></div>
        <div class="metric"><span class="metric-label">Liq buffer</span><span class="metric-value dim">${fmtMoney(snap.liquidation_buffer)}</span></div>
      </div>
      <div class="card-row">
        <div class="metric"><span class="metric-label">Live order</span><span class="metric-value dim">${escapeHtml((s.live_order_id || '—').slice(0, 12))}${s.live_order_id ? '…' : ''}</span></div>
        <div class="metric"><span class="metric-label">Heartbeat</span><span class="metric-value dim">${fmtHeartbeat(s.last_heartbeat_ts)}</span></div>
        <div class="metric"><span class="metric-label">Reserved margin</span><span class="metric-value dim">${fmtMoney(s.reserved_margin)}</span></div>
      </div>
    </div>
  `;
  return el;
}

function prettyState(state) {
  if (!state) return 'Unknown';
  if (state === 'ARMED_SELL') return 'Waiting for sell';
  if (state === 'ARMED_BUY') return 'Waiting for buy';
  if (state === 'HALTED') return 'Halted';
  if (state === 'IDLE') return 'Idle';
  return state;
}

function marginPct(snap) {
  const used = Number(snap.margin_used ?? snap.initial_margin ?? 0);
  const equity = Number(snap.equity ?? 0);
  if (!equity) return 0;
  return Math.min(100, (used / equity) * 100);
}

function renderLotsTable(snapshot, config, tenant, symbol, state) {
  const lots = Array.isArray(snapshot?.lots) ? snapshot.lots : [];
  const mark = Number(snapshot?.last_mark) || 0;
  const contractSize = Number(config?.contract_size) || 50;
  const currentLotContext = { tenant: tenant || '', symbol: symbol || '' };
  // Map sleeve ids → display names so lot rows can show a friendly strategy label
  const sleeveNamesById = {};
  for (const s of (config?.sleeves || [])) {
    sleeveNamesById[s.id] = s.name || s.id;
  }
  // FIFO allocation gives us "which strategy will sell this lot" even for
  // manual buys with no strategy_id tag on them.
  const primaryQty = Number(config?.swing_qty) || 0;
  const sleeves = Array.isArray(config?.sleeves) ? config.sleeves : [];
  const allocation = allocateLotsToStrategies(lots, primaryQty, sleeves, state?.sleeves || {});
  const ownerLabel = (owner) => owner === '__primary' ? 'Primary' : (sleeveNamesById[owner] || owner);

  if (lots.length === 0) {
    // Fallback: single aggregate line when the running bot hasn't yet
    // written per-lot data (older snapshot or non-paper broker).
    const pos = Number(snapshot?.position_qty) || 0;
    const avg = Number(snapshot?.position_avg_entry) || 0;
    if (pos === 0) {
      return `
        <section class="positions-section empty">
          <h3 class="section-title">Open positions</h3>
          <div class="positions-empty">You hold no contracts right now.</div>
        </section>
      `;
    }
    const unreal = mark && avg ? (mark - avg) * contractSize * pos : 0;
    const dist = mark && avg ? mark - avg : 0;
    return `
      <section class="positions-section">
        <div class="section-title-row">
          <h3 class="section-title">Open positions <span class="section-count">${pos} contracts</span></h3>
          <div class="section-title-pnl ${classForValue(unreal)}">${unreal >= 0 ? '+' : ''}${fmtMoney(unreal)} unrealized</div>
        </div>
        <div class="positions-summary">
          <div class="summary-cell">
            <span class="summary-label">Avg entry</span>
            <span class="summary-value">$${fmtPrice(avg)}</span>
          </div>
          <div class="summary-cell">
            <span class="summary-label">Current mark</span>
            <span class="summary-value">$${fmtPrice(mark)}</span>
          </div>
          <div class="summary-cell">
            <span class="summary-label">vs avg entry</span>
            <span class="summary-value ${classForValue(dist)}">${dist >= 0 ? '+' : ''}$${fmtPrice(dist)} / contract</span>
          </div>
        </div>
        <div class="positions-empty">Aggregate view — per-lot history begins with your next fill.</div>
      </section>
    `;
  }

  // Sort newest first, sum totals, compute weighted avg entry across lots.
  const sorted = [...lots].sort((a, b) => (b.entry_ts || 0) - (a.entry_ts || 0));
  const totalQty = sorted.reduce((n, l) => n + (Number(l.qty) || 0), 0);
  const totalUnreal = sorted.reduce((n, l) => n + (Number(l.unrealized_pnl) || 0), 0);
  const weightedCost = sorted.reduce((n, l) => n + (Number(l.qty) || 0) * (Number(l.entry_price) || 0), 0);
  const avgEntry = totalQty > 0 ? weightedCost / totalQty : 0;
  const distFromAvg = mark && avgEntry ? mark - avgEntry : 0;

  const rows = sorted.map(lot => {
    const qty = Number(lot.qty) || 0;
    const entry = Number(lot.entry_price) || 0;
    const unreal = Number(lot.unrealized_pnl) || 0;
    const perContract = qty > 0 ? unreal / qty : 0;
    const src = String(lot.source || 'unknown');
    const age = lotAge(lot.entry_ts);
    return `
      <tr class="lot-row">
        <td class="lot-qty"><b>${qty}</b></td>
        <td class="lot-entry">$${fmtPrice(entry)}</td>
        <td class="lot-mark">$${fmtPrice(mark)}</td>
        <td class="lot-pnl ${classForValue(unreal)}">
          <div>${unreal >= 0 ? '+' : ''}${fmtMoney(unreal)}</div>
          <div class="lot-pnl-sub">${perContract >= 0 ? '+' : ''}${fmtMoney(perContract)} / ea</div>
        </td>
        <td class="lot-src"><span class="lot-source-badge src-${escapeHtml(src)}">${escapeHtml(src)}</span></td>
        <td class="lot-strategy">${
          (() => {
            const owners = allocation.byLotOwners[lot.id];
            if (!owners) return '<span class="lot-strategy-none">—</span>';
            const entries = Object.entries(owners);
            if (entries.length === 1) {
              const [owner, n] = entries[0];
              const label = ownerLabel(owner);
              return `<span class="lot-strategy-tag">${escapeHtml(label)}${n < qty ? ` (${n}/${qty})` : ''}</span>`;
            }
            // Split across multiple strategies
            return entries.map(([owner, n]) =>
              `<span class="lot-strategy-tag">${escapeHtml(ownerLabel(owner))} ${n}</span>`
            ).join(' ');
          })()
        }</td>
        <td class="lot-age">${age}</td>
        <td class="lot-actions">
          ${(() => {
            // Only offer "+ Strategy" for lots that AREN'T already fully committed
            // to a running sleeve. Otherwise a click double-counts contracts and
            // triggers the capacity error, which is confusing.
            const owners = allocation.byLotOwners[lot.id];
            const assigned = owners ? Object.values(owners).reduce((a, b) => a + b, 0) : 0;
            const free = qty - assigned;
            if (free <= 0) {
              return `<span class="lot-assigned-badge" title="This lot is already committed to a strategy — add a new sleeve from the ‘+ add strategy’ button after buying more contracts">assigned</span>`;
            }
            return `<button class="small primary"
              data-action="add-sleeve-from-lot"
              data-tenant="${escapeHtml(currentLotContext.tenant)}"
              data-symbol="${escapeHtml(currentLotContext.symbol)}"
              data-lot-qty="${free}"
              data-lot-entry="${entry}"
              title="Add a strategy anchored to this lot's entry price (${free} contract${free === 1 ? '' : 's'} free)">+ Strategy</button>`;
          })()}
        </td>
      </tr>
    `;
  }).join('');

  return `
    <section class="positions-section">
      <div class="section-title-row">
        <h3 class="section-title">Open positions <span class="section-count">${sorted.length} lot${sorted.length === 1 ? '' : 's'} · ${totalQty} contracts</span></h3>
        <div class="section-title-pnl ${classForValue(totalUnreal)}">${totalUnreal >= 0 ? '+' : ''}${fmtMoney(totalUnreal)} unrealized</div>
      </div>
      <div class="positions-summary">
        <div class="summary-cell">
          <span class="summary-label">Avg entry</span>
          <span class="summary-value">$${fmtPrice(avgEntry)}</span>
        </div>
        <div class="summary-cell">
          <span class="summary-label">Current mark</span>
          <span class="summary-value">$${fmtPrice(mark)}</span>
        </div>
        <div class="summary-cell">
          <span class="summary-label">vs avg entry</span>
          <span class="summary-value ${classForValue(distFromAvg)}">${distFromAvg >= 0 ? '+' : ''}$${fmtPrice(distFromAvg)} / contract</span>
        </div>
      </div>
      <table class="positions-table">
        <thead>
          <tr>
            <th>Qty</th>
            <th>Bought at</th>
            <th>Now</th>
            <th>Unrealized</th>
            <th>Source</th>
            <th>Strategy</th>
            <th>Age</th>
            <th></th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </section>
  `;
}

function renderMicrostructurePanel(snapshot) {
  const ms = snapshot?.microstructure;
  if (!ms) return '';
  const cells = [];
  const cell = (label, val, sub, cls = '') =>
    `<div class="ms-cell ${cls}">
       <span class="ms-label">${label}</span>
       <span class="ms-value">${val}</span>
       ${sub ? `<span class="ms-sub">${sub}</span>` : ''}
     </div>`;

  if ('spread_median' in ms) {
    const s = ms.spread_median;
    cells.push(cell('Effective spread',
      s == null ? '—' : `$${Number(s).toFixed(4)}`,
      `band k=${ms.spread_k}`));
  }
  if ('autocorr_lag1' in ms) {
    const a = ms.autocorr_lag1;
    const bad = a != null && a > (ms.autocorr_max ?? 0);
    cells.push(cell('Return autocorr (lag 1)',
      a == null ? '—' : a.toFixed(3),
      bad ? `paused > ${ms.autocorr_max}` : `≤ ${ms.autocorr_max}, OK`,
      bad ? 'bad' : ''));
  }
  if ('obi' in ms) {
    const o = ms.obi;
    const th = ms.obi_threshold;
    const bad = o != null && Math.abs(o) > th;
    cells.push(cell('Order-book imbalance',
      o == null ? '—' : o.toFixed(3),
      bad ? `|OBI| > ${th}` : `|OBI| ≤ ${th}, OK`,
      bad ? 'warn' : ''));
  }
  if ('vpin' in ms) {
    const v = ms.vpin;
    const bad = v != null && v > ms.vpin_max;
    cells.push(cell('VPIN (toxicity)',
      v == null ? '—' : v.toFixed(3),
      bad ? `paused > ${ms.vpin_max}` : `≤ ${ms.vpin_max}, OK`,
      bad ? 'bad' : ''));
  }
  if ('kyle_lambda' in ms) {
    const l = ms.kyle_lambda;
    const scale = ms.size_scale;
    const bad = scale != null && scale < 1.0;
    cells.push(cell('Kyle λ (impact)',
      l == null ? '—' : Number(l).toExponential(2),
      bad ? `size ×${scale?.toFixed(2)}` : `full size`,
      bad ? 'warn' : ''));
  }

  const pc = ms.pause_counts || {};
  const totalPauses = (pc.autocorr || 0) + (pc.vpin || 0) + (pc.obi_buy || 0) + (pc.obi_sell || 0);
  const summary = `
    <div class="ms-summary">
      <span>arm attempts: <b>${ms.arm_attempts ?? 0}</b></span>
      <span>total pauses: <b>${totalPauses}</b></span>
      <span class="dim">autocorr ${pc.autocorr || 0} · vpin ${pc.vpin || 0} · obi buy ${pc.obi_buy || 0} · obi sell ${pc.obi_sell || 0} · size tapers ${ms.size_taper_count || 0}</span>
    </div>
  `;

  return `
    <section class="ms-panel">
      <h3 class="ms-heading">Microstructure signals</h3>
      <div class="ms-grid">${cells.join('')}</div>
      ${summary}
    </section>
  `;
}

function renderRiskStrip(snapshot) {
  const pos = Number(snapshot?.position_qty) || 0;
  const mark = Number(snapshot?.last_mark) || 0;
  const marginUsed = Number(snapshot?.margin_used) || 0;
  const marginAvail = Number(snapshot?.available_margin) || 0;
  const marginPer = Number(snapshot?.margin_per_contract) || 0;
  const liq = snapshot?.liquidation_price;
  const equity = Number(snapshot?.equity) || 0;
  const usePct = equity > 0 ? Math.min(100, (marginUsed / equity) * 100) : 0;

  let liqCell;
  if (pos === 0) {
    liqCell = `
      <div class="risk-cell">
        <span class="risk-label">Liquidation price</span>
        <span class="risk-value dim">—</span>
        <span class="risk-sub">flat — no directional risk</span>
      </div>
    `;
  } else if (liq == null) {
    liqCell = `
      <div class="risk-cell">
        <span class="risk-label">Liquidation price</span>
        <span class="risk-value">safe</span>
        <span class="risk-sub">account can absorb full drop to $0</span>
      </div>
    `;
  } else {
    const dist = mark - Number(liq);
    const distPct = mark > 0 ? Math.abs(dist / mark) * 100 : 0;
    const cls = distPct < 10 ? 'bad' : distPct < 25 ? 'warn' : '';
    liqCell = `
      <div class="risk-cell">
        <span class="risk-label">Liquidation price</span>
        <span class="risk-value ${cls}">$${fmtPrice(liq)}</span>
        <span class="risk-sub">$${fmtPrice(Math.abs(dist))} away · ${distPct.toFixed(1)}% cushion</span>
      </div>
    `;
  }

  const useCls = usePct > 75 ? 'bad' : usePct > 50 ? 'warn' : '';
  return `
    <div class="risk-strip">
      <div class="risk-cell">
        <span class="risk-label">Margin used</span>
        <span class="risk-value ${useCls}">${fmtMoney(marginUsed)}</span>
        <span class="risk-sub">${usePct.toFixed(1)}% of equity · $${fmtNum(marginPer, 0)}/contract</span>
      </div>
      <div class="risk-cell">
        <span class="risk-label">Available margin</span>
        <span class="risk-value">${fmtMoney(marginAvail)}</span>
        <span class="risk-sub">room for ${marginPer > 0 ? Math.floor(marginAvail / marginPer) : 0} more contracts</span>
      </div>
      ${liqCell}
      <div class="risk-cell">
        <span class="risk-label">Total equity</span>
        <span class="risk-value">${fmtMoney(equity)}</span>
        <span class="risk-sub">balance + unrealized</span>
      </div>
    </div>
  `;
}

function allocateLotsToStrategies(lots, primaryQty, sleeves, sleeveStates = {}) {
  // Break every lot into 1-contract units and hand them out FIFO so each
  // strategy knows the ACTUAL cost basis of contracts it will sell. Units
  // explicitly tagged with a strategy_id go to that sleeve first; whatever's
  // left goes to primary, then to each sleeve in listed order.
  // Sleeves in ARMED_BUY (already sold, waiting to rebuy) claim NO contracts —
  // their tagged orphans from prior cycles go into the unassigned pool for
  // ARMED_SELL sleeves to consume. Only strategies actively holding a position
  // should show ownership.
  // Also returns byLotOwners: { lot_id: [{owner, qty}] } so the lot table
  // can show which strategy each lot is committed to.
  const sorted = [...(lots || [])].sort((a, b) => (a.entry_ts || 0) - (b.entry_ts || 0));
  const units = [];
  for (const lot of sorted) {
    const n = Number(lot.qty) || 0;
    for (let i = 0; i < n; i++) {
      units.push({
        entry_price: Number(lot.entry_price) || 0,
        strategy_id: lot.strategy_id || null,
        lot_id: lot.id,
      });
    }
  }
  const bySleeve = {};
  for (const s of sleeves) bySleeve[s.id] = [];
  const unassigned = [];
  // Sleeves get their tagged units first, but ONLY up to their configured qty,
  // AND only if they're in a state where they hold contracts (ARMED_SELL).
  // Excess tagged units (from earlier cycles) drop into the unassigned pool
  // so other sleeves / primary can claim them. This prevents "size drift" AND
  // stops ARMED_BUY sleeves from showing phantom unrealized gains on orphan lots.
  const qtyById = {};
  const holdsContracts = {};
  for (const s of sleeves) {
    qtyById[s.id] = Number(s.qty) || 0;
    const st = (sleeveStates[s.id]?.state) || 'ARMED_SELL';
    holdsContracts[s.id] = st === 'ARMED_SELL';  // only sell-armed sleeves own contracts
  }
  for (const u of units) {
    if (u.strategy_id && bySleeve[u.strategy_id] !== undefined
        && holdsContracts[u.strategy_id]
        && bySleeve[u.strategy_id].length < qtyById[u.strategy_id]) {
      bySleeve[u.strategy_id].push(u);
    } else {
      unassigned.push(u);
    }
  }
  let idx = 0;
  const takeFor = (n) => {
    const out = [];
    while (out.length < n && idx < unassigned.length) out.push(unassigned[idx++]);
    return out;
  };
  const primary = takeFor(primaryQty);
  for (const s of sleeves) {
    if (!holdsContracts[s.id]) continue;  // ARMED_BUY sleeves don't take from the pool
    const need = (Number(s.qty) || 0) - bySleeve[s.id].length;
    if (need > 0) bySleeve[s.id].push(...takeFor(need));
  }
  // Roll up per-lot ownership counts so the lot table can label each lot with
  // the strategy that will sell it. Accumulate as {owner → qty} to handle
  // splits when one lot is shared between primary + sleeves.
  const byLotOwners = {};
  const addOwn = (owner, unit) => {
    if (!byLotOwners[unit.lot_id]) byLotOwners[unit.lot_id] = {};
    byLotOwners[unit.lot_id][owner] = (byLotOwners[unit.lot_id][owner] || 0) + 1;
  };
  for (const u of primary) addOwn('__primary', u);
  for (const s of sleeves) for (const u of bySleeve[s.id]) addOwn(s.id, u);
  return { primary, bySleeve, byLotOwners };
}

function sumUnitsUnrealized(units, mark, contractSize) {
  if (!units || !units.length || !mark) return 0;
  let sum = 0;
  for (const u of units) sum += (mark - u.entry_price) * contractSize;
  return sum;
}

function fmtTrailingParams(exitMode, live, staticCfg) {
  // exitMode: "trailing_stop" | "fixed_limit" | "hybrid" | ...
  // live: { armed, hwm, distance, hybridTriggeredTs, hybridDelaySecs, activationPx } for runtime state
  // staticCfg: { trigger, distance, sellPx, buyPx, activationPx, hybridDelay, mark, avgEntry, qty, contractSize, feeRt }
  // The `mark` field lets us project the "if trail engaged NOW, stop would be
  // here" line so the user sees the trail stop track price in real time even
  // before activation crosses. avgEntry/qty/contractSize/feeRt let us derive
  // "Locked profit" — the guaranteed net if the trail fires right now.
  const mark = Number(staticCfg?.mark) || 0;
  const dist = Number(staticCfg?.distance) || 0;
  const projectedStop = mark > 0 && dist > 0 ? mark - dist : null;
  const avgEntry = Number(staticCfg?.avgEntry) || 0;
  const qty = Number(staticCfg?.qty) || 0;
  const contractSize = Number(staticCfg?.contractSize) || 50;
  const feeRt = Number(staticCfg?.feeRt) || 0;
  const totalFees = feeRt * qty;
  // Locked profit at a given stop price = (stop - avg_entry) × size × qty − round-trip fees.
  // Positive = we guaranteed a gain the moment we fire; negative = trail would
  // sell for a loss (relative to entry). If we don't have a cost basis yet
  // (no allocated contracts) or no armed/projected stop, we return null.
  const lockedAt = (stopPrice) => {
    if (!stopPrice || !avgEntry || qty <= 0) return null;
    return (stopPrice - avgEntry) * contractSize * qty - totalFees;
  };
  const lockedArmed = live && live.armed && live.hwm
    ? lockedAt(Number(live.hwm) - Number(live.distance || 0)) : null;
  const lockedProjected = projectedStop !== null ? lockedAt(projectedStop) : null;
  const fmtLockedLine = (val, label = 'Locked') => {
    if (val === null || !isFinite(val)) return '';
    const cls = val >= 0 ? 'pos' : 'neg';
    return `<div class="params-line params-locked"><span class="params-label">${label}</span><b class="${cls}">${val >= 0 ? '+' : ''}${fmtMoney(val)}</b></div>`;
  };
  if (exitMode === 'trailing_stop') {
    if (live && live.armed && live.hwm) {
      const stop = Number(live.hwm) - Number(live.distance || 0);
      return `
        <div class="params-block trail-armed">
          <div class="params-mode"><span class="dot dot-live"></span>Trailing <em>(armed)</em></div>
          <div class="params-line"><span class="params-label">Stop</span><b>$${fmtPrice(stop)}</b></div>
          <div class="params-line"><span class="params-label">HWM</span>$${fmtPrice(live.hwm)}</div>
          <div class="params-line"><span class="params-label">Buy back</span>$${fmtPrice(staticCfg.buyPx)}</div>
          ${fmtLockedLine(lockedArmed, 'Locked in profit')}
          <div class="params-line params-sub">Rises with price · sells at market on pullback</div>
        </div>`;
    }
    return `
      <div class="params-block">
        <div class="params-mode">Trailing stop</div>
        <div class="params-line"><span class="params-label">Trigger</span>$${fmtPrice(staticCfg.trigger)}</div>
        <div class="params-line"><span class="params-label">Buy back</span>$${fmtPrice(staticCfg.buyPx)}</div>
        <div class="params-line"><span class="params-label">Distance</span>$${fmtPrice(staticCfg.distance)}</div>
        ${projectedStop !== null ? `<div class="params-line params-projected"><span class="params-label">If armed now</span><b class="dim-b">$${fmtPrice(projectedStop)}</b></div>` : ''}
        ${fmtLockedLine(lockedProjected, 'If armed: locked')}
        <div class="params-line params-sub">Waits for trigger, then trails</div>
      </div>`;
  }
  if (exitMode === 'hybrid') {
    // Stage 3: trail engaged (breakout confirmed)
    if (live && live.armed && live.hwm) {
      const stop = Number(live.hwm) - Number(live.distance || 0);
      return `
        <div class="params-block trail-armed">
          <div class="params-mode"><span class="dot dot-live"></span>Hybrid → Trailing <em>(breakout)</em></div>
          <div class="params-line"><span class="params-label">Stop</span><b>$${fmtPrice(stop)}</b></div>
          <div class="params-line"><span class="params-label">HWM</span>$${fmtPrice(live.hwm)}</div>
          <div class="params-line"><span class="params-label">Buy back</span>$${fmtPrice(staticCfg.buyPx)}</div>
          ${fmtLockedLine(lockedArmed, 'Locked in profit')}
          <div class="params-line params-sub">Rode past activation · trailing on breakout</div>
        </div>`;
    }
    // Stage 2: sell triggered, in delay window
    if (live && live.hybridTriggeredTs) {
      const elapsed = Math.max(0, (Date.now() / 1000) - Number(live.hybridTriggeredTs));
      const remaining = Math.max(0, Number(live.hybridDelaySecs || 5) - elapsed);
      return `
        <div class="params-block trail-armed">
          <div class="params-mode"><span class="dot dot-live"></span>Hybrid <em>(watching)</em></div>
          <div class="params-line"><span class="params-label">Target hit</span>$${fmtPrice(staticCfg.sellPx)}</div>
          <div class="params-line"><span class="params-label">Watch until</span><b>$${fmtPrice(staticCfg.activationPx)}</b></div>
          <div class="params-line"><span class="params-label">Remaining</span>${remaining.toFixed(1)}s</div>
          <div class="params-line"><span class="params-label">Buy back</span>$${fmtPrice(staticCfg.buyPx)}</div>
          ${projectedStop !== null ? `<div class="params-line params-projected"><span class="params-label">If trail now</span><b class="dim-b">$${fmtPrice(projectedStop)}</b></div>` : ''}
          ${fmtLockedLine(lockedProjected, 'If trail: locked')}
          <div class="params-line params-sub">Cross activation → trail · else sell at market</div>
        </div>`;
    }
    // Stage 1: idle, waiting for sell target
    return `
      <div class="params-block">
        <div class="params-mode">Hybrid</div>
        <div class="params-line"><span class="params-label">Sell target</span>$${fmtPrice(staticCfg.sellPx)}</div>
        <div class="params-line"><span class="params-label">Buy back</span>$${fmtPrice(staticCfg.buyPx)}</div>
        <div class="params-line"><span class="params-label">Activation</span>$${fmtPrice(staticCfg.activationPx)}</div>
        <div class="params-line"><span class="params-label">Delay</span>${fmtNum(staticCfg.hybridDelay || 5, 0)}s</div>
        <div class="params-line"><span class="params-label">Trail dist</span>$${fmtPrice(staticCfg.distance)}</div>
        ${projectedStop !== null ? `<div class="params-line params-projected"><span class="params-label">If trail now</span><b class="dim-b">$${fmtPrice(projectedStop)}</b></div>` : ''}
        ${fmtLockedLine(lockedProjected, 'If trail: locked')}
      </div>`;
  }
  return `
    <div class="params-block">
      <div class="params-mode">Fixed limit</div>
      <div class="params-line"><span class="params-label">Sell</span>$${fmtPrice(staticCfg.sellPx)}</div>
      <div class="params-line"><span class="params-label">Buy</span>$${fmtPrice(staticCfg.buyPx)}</div>
    </div>`;
}

function renderSleevesSection(tenant, symbol, config, state, snapshot) {
  const primaryQty = Number(config?.swing_qty) || 0;
  const core = Number(config?.core_qty) || 0;
  const pos = Number(snapshot?.position_qty) || 0;
  const sleeves = Array.isArray(config?.sleeves) ? config.sleeves : [];
  const sleeveStates = state?.sleeves || {};
  const sleeveQtySum = sleeves.reduce((n, s) => n + (Number(s.qty) || 0), 0);
  const budget = pos - core;
  const used = primaryQty + sleeveQtySum;
  const remaining = budget - used;

  // Allocate lots FIFO across primary + sleeves so each strategy's unrealized
  // reflects the ACTUAL price paid for the contracts it will sell, not its
  // configured buy_px target. Passing sleeveStates so ARMED_BUY sleeves
  // (already sold, waiting to rebuy) correctly claim zero.
  const allocation = allocateLotsToStrategies(snapshot?.lots || [], primaryQty, sleeves, sleeveStates);

  const primaryHalted = state?.state === 'HALTED';
  const primaryStateLabel = prettyState(state?.state);
  const cyclesTotal = Number(state?.cycles) || 0;
  const realizedTotal = Number(state?.realized_pnl) || 0;

  const anyHaltedSleeves = Object.values(sleeveStates).filter(ss => ss?.state === 'HALTED').length;
  const primaryActive = primaryQty > 0;
  const running = (primaryActive ? 1 : 0) + sleeves.length
    - (primaryActive && primaryHalted ? 1 : 0) - anyHaltedSleeves;
  const haltedCount = (primaryActive && primaryHalted ? 1 : 0) + anyHaltedSleeves;
  const totalStrategies = (primaryActive ? 1 : 0) + sleeves.length;

  const resumeBtn = (t, sym) =>
    `<button class="small primary" data-action="resume" data-tenant="${escapeHtml(t)}" data-symbol="${escapeHtml(sym)}">Resume</button>`;
  const cancelBtn = (t, sym, sid, enabled) =>
    `<button class="small ghost" ${enabled ? '' : 'disabled'} data-action="cancel-order" data-tenant="${escapeHtml(t)}" data-symbol="${escapeHtml(sym)}"${sid ? ` data-sleeve-id="${escapeHtml(sid)}"` : ''} title="${enabled ? 'Pause this strategy — cancels the pending order and halts the state machine so it does not immediately re-arm. Click Resume to bring it back.' : 'Strategy has no pending order'}">Pause strategy</button>`;
  const sellNowBtn = (t, sym, qty, enabled) => {
    // Only render the button when a sell is actually possible. If the strategy
    // is waiting to buy or has no contracts, the button would be a no-op — don't
    // clutter the row with disabled controls.
    if (!enabled) return '';
    return `<button class="small danger" data-action="sell-now" data-tenant="${escapeHtml(t)}" data-symbol="${escapeHtml(sym)}" data-qty="${qty}" title="Market-sell ${qty} contract${qty === 1 ? '' : 's'} now">Sell ${qty} now</button>`;
  };

  const primaryHasOrder = !!state?.live_order_id;
  // Sell-now only makes sense when the strategy is in ARMED_SELL AND actually
  // has contracts to sell. When ARMED_BUY (already sold, waiting to rebuy) or
  // HALTED, there's nothing to sell — hide the button entirely.
  const primaryCanSellNow = state?.state === 'ARMED_SELL' && pos >= primaryQty && primaryQty > 0;
  // Hide the primary row entirely when the user has disabled it (swing_qty=0).
  // The bot's primary state machine still runs but does nothing — the UI just
  // stops showing an irrelevant row.
  const primaryEnabled = primaryQty > 0;

  const primaryHint = primaryHalted && state?.halt_reason
    ? `<span class="halt-why">Halted: ${escapeHtml(state.halt_reason)}</span>`
    : (pos < primaryQty
        ? `<span class="idle-why">Idle — needs ${primaryQty} contracts (you have ${pos})</span>`
        : 'From your main config — edit in Settings');

  const primaryMark = Number(snapshot?.last_mark) || 0;
  const primaryCS = Number(config?.contract_size) || 50;
  // Unrealized = sum over ACTUAL lot entries the primary owns (via FIFO allocation).
  // Always shown as a number (even $0.00) so the user knows the field is live.
  const primaryUnreal = sumUnitsUnrealized(allocation.primary, primaryMark, primaryCS);

  const primaryAvgEntry = allocation.primary.length > 0
    ? allocation.primary.reduce((n, u) => n + u.entry_price, 0) / allocation.primary.length
    : 0;
  const primaryParamsHtml = fmtTrailingParams(
    config?.exit_mode || 'fixed_limit',
    { armed: !!state?.trail_armed, hwm: state?.trail_high_water_price, distance: config?.trail_distance,
      hybridTriggeredTs: state?.hybrid_sell_triggered_ts, hybridDelaySecs: config?.hybrid_delay_secs,
      activationPx: config?.trail_activation_px },
    { trigger: config?.trail_trigger, distance: config?.trail_distance,
      sellPx: config?.sell_px, buyPx: config?.buy_px,
      activationPx: config?.trail_activation_px, hybridDelay: config?.hybrid_delay_secs,
      mark: snapshot?.last_mark,
      avgEntry: primaryAvgEntry, qty: allocation.primary.length,
      contractSize: primaryCS, feeRt: config?.fee_per_contract_roundtrip }
  );

  const primaryRow = primaryEnabled ? `
    <tr class="sleeve-row primary ${primaryHalted ? 'halted' : ''}">
      <td class="sleeve-name" data-label="Strategy"><b>Primary</b><div class="sleeve-hint">${primaryHint}</div></td>
      <td class="sleeve-qty" data-label="Contracts">${primaryQty}</td>
      <td class="sleeve-params" data-label="Params">${primaryParamsHtml}</td>
      <td class="sleeve-status" data-label="Status"><span class="status-pill ${(state?.state || '').toLowerCase()}">${escapeHtml(primaryStateLabel)}</span></td>
      <td class="sleeve-cycles" data-label="Cycles">${cyclesTotal}</td>
      <td class="sleeve-unrealized ${classForValue(primaryUnreal)}" data-label="Unrealized">${primaryUnreal >= 0 ? '+' : ''}${fmtMoney(primaryUnreal)}</td>
      <td class="sleeve-realized ${classForValue(realizedTotal)}" data-label="Realized">${realizedTotal >= 0 ? '+' : ''}${fmtMoney(realizedTotal)}</td>
      <td class="sleeve-actions" data-label="Actions">
        ${primaryHalted ? resumeBtn(tenant, symbol) : ''}
        ${cancelBtn(tenant, symbol, null, primaryHasOrder)}
        ${sellNowBtn(tenant, symbol, primaryQty, primaryCanSellNow)}
        <button class="small ghost" data-action="disable-primary" data-tenant="${escapeHtml(tenant)}" data-symbol="${escapeHtml(symbol)}" title="Stop the Primary strategy (set swing_qty=0). Any live order cancels next tick.">Stop strategy</button>
        <button class="small ghost" data-action="edit" data-tenant="${escapeHtml(tenant)}" data-symbol="${escapeHtml(symbol)}">Edit</button>
      </td>
    </tr>
  ` : '';

  const mark = Number(snapshot?.last_mark) || 0;
  const contractSize = Number(config?.contract_size) || 50;

  const sleeveRows = sleeves.map(s => {
    const ss = sleeveStates[s.id] || {};
    const sState = String(ss.state || 'ARMED_SELL');
    const cycles = Number(ss.cycles) || 0;
    const realized = Number(ss.realized_pnl) || 0;
    const hasOrder = !!ss.live_order_id;
    const sleeveHalted = sState === 'HALTED';
    const sleeveQty = Number(s.qty) || 0;
    // Sell-now: only when the sleeve is ARMED_SELL with enough contracts to
    // actually sell. Hide in ARMED_BUY (nothing to sell — already sold) and
    // HALTED. To take a strategy out of rotation, use the ✕ (Stop strategy).
    const canSellNow = sState === 'ARMED_SELL' && pos >= sleeveQty && sleeveQty > 0;

    // Per-sleeve unrealized reflects ONLY what THIS sleeve has traded — the
    // paper gain on contracts it bought via its own state machine (own_avg_entry).
    // Newly-created sleeves and ARMED_BUY sleeves show $0 here because they
    // haven't earned any move that belongs to them yet. Inherited paper gains
    // on pre-existing lots stay at the top-level unrealized on the position row,
    // so they're not double-counted per strategy. Fallback: if the sleeve is
    // ARMED_SELL with contracts but own_avg_entry is missing (legacy state
    // from before this field was tracked), use the sleeve's buy_px — a limit
    // buy fills exactly at buy_px so it's the closest we can reconstruct.
    const sleeveUnits = allocation.bySleeve[s.id] || [];
    const ownEntry = ss.own_avg_entry != null ? Number(ss.own_avg_entry) : Number(s.buy_px);
    const unreal = (ownEntry > 0 && sleeveQty > 0 && sState === 'ARMED_SELL')
      ? (mark - ownEntry) * contractSize * sleeveQty
      : 0;

    const sleeveAvgEntry = sleeveUnits.length > 0
      ? sleeveUnits.reduce((n, u) => n + u.entry_price, 0) / sleeveUnits.length
      : 0;
    const paramsHtml = fmtTrailingParams(
      s.exit_mode || 'fixed_limit',
      { armed: !!ss.trail_armed, hwm: ss.trail_high_water_price, distance: s.trail_distance,
        hybridTriggeredTs: ss.hybrid_sell_triggered_ts, hybridDelaySecs: s.hybrid_delay_secs,
        activationPx: s.trail_activation_px },
      { trigger: s.trail_trigger, distance: s.trail_distance,
        sellPx: s.sell_px, buyPx: s.buy_px,
        activationPx: s.trail_activation_px, hybridDelay: s.hybrid_delay_secs,
        mark: snapshot?.last_mark,
        avgEntry: sleeveAvgEntry, qty: sleeveUnits.length,
        contractSize: contractSize, feeRt: config?.fee_per_contract_roundtrip }
    );

    return `
      <tr class="sleeve-row ${sleeveHalted ? 'halted' : ''}" data-sleeve-id="${escapeHtml(s.id)}">
        <td class="sleeve-name" data-label="Strategy">
          <b>${escapeHtml(s.name || s.id)}</b>
          ${sleeveHalted && ss.halt_reason ? `<div class="sleeve-hint"><span class="halt-why">${escapeHtml(ss.halt_reason)}</span></div>` : ''}
        </td>
        <td class="sleeve-qty" data-label="Contracts">${sleeveQty}</td>
        <td class="sleeve-params" data-label="Params">${paramsHtml}</td>
        <td class="sleeve-status" data-label="Status"><span class="status-pill ${sState.toLowerCase()}">${escapeHtml(prettyState(sState))}</span></td>
        <td class="sleeve-cycles" data-label="Cycles">${cycles}</td>
        <td class="sleeve-unrealized ${classForValue(unreal)}" data-label="Unrealized">${unreal >= 0 ? '+' : ''}${fmtMoney(unreal)}</td>
        <td class="sleeve-realized ${classForValue(realized)}" data-label="Realized">${realized >= 0 ? '+' : ''}${fmtMoney(realized)}</td>
        <td class="sleeve-actions" data-label="Actions">
          ${sleeveHalted ? resumeBtn(tenant, symbol) : ''}
          ${cancelBtn(tenant, symbol, s.id, hasOrder)}
          ${sellNowBtn(tenant, symbol, sleeveQty, canSellNow)}
          <button class="small ghost" data-action="edit-sleeve" data-tenant="${escapeHtml(tenant)}" data-symbol="${escapeHtml(symbol)}" data-sleeve-id="${escapeHtml(s.id)}">Edit</button>
          <button class="small ghost" data-action="delete-sleeve" data-tenant="${escapeHtml(tenant)}" data-symbol="${escapeHtml(symbol)}" data-sleeve-id="${escapeHtml(s.id)}" title="Stop strategy — cancels any pending order and removes it from rotation">Stop strategy</button>
        </td>
      </tr>
    `;
  }).join('');

  let budgetLine;
  if (budget <= 0 && used > 0) {
    budgetLine = `<span class="budget-bad">You hold 0 contracts — strategies need contracts to trade. Buy some or lower their sizes.</span>`;
  } else if (remaining < 0) {
    budgetLine = `<span class="budget-bad">Strategies want ${used} contracts but you only have ${budget} available. Reduce sizes or buy ${-remaining} more.</span>`;
  } else {
    budgetLine = `<span class="budget-ok">${used} / ${budget} contracts assigned${remaining > 0 ? ` · ${remaining} unassigned` : ''}</span>`;
  }

  const countLabel = haltedCount > 0
    ? `${Math.max(0, running)} running · ${haltedCount} halted`
    : `${Math.max(0, running)} running`;

  // Silver price ticker on its own row between the section title and the
  // strategy table — a full-width, prominent strip so the mark is impossible
  // to miss while comparing strategies against current price.
  const mkt = Number(snapshot?.last_mark) || 0;
  const bid = Number(snapshot?.best_bid);
  const ask = Number(snapshot?.best_ask);
  const priceBar = mkt > 0 ? `
    <div class="section-price-bar">
      <div class="section-price-side">
        <span class="section-price-label">${escapeHtml(symbolLabel(symbol))}</span>
      </div>
      <div class="section-price-mark-wrap">
        <span class="section-price-mark">$${fmtPrice(mkt)}</span>
      </div>
      <div class="section-price-side right">
        ${Number.isFinite(bid) && Number.isFinite(ask)
          ? `<span class="section-price-book">bid $${fmtPrice(bid)} &nbsp;·&nbsp; ask $${fmtPrice(ask)}</span>`
          : ''}
      </div>
    </div>` : '';

  return `
    <section class="sleeves-section">
      <div class="section-title-row">
        <h3 class="section-title">Strategies <span class="section-count">${countLabel} · ${budgetLine}</span></h3>
        <button class="small primary" data-action="add-sleeve" data-tenant="${escapeHtml(tenant)}" data-symbol="${escapeHtml(symbol)}">+ add strategy</button>
      </div>
      ${priceBar}
      <div class="sleeves-table-wrap">
        <table class="sleeves-table">
          <colgroup>
            <col class="col-strategy">
            <col class="col-contracts">
            <col class="col-params">
            <col class="col-status">
            <col class="col-cycles">
            <col class="col-unrealized">
            <col class="col-realized">
            <col class="col-actions">
          </colgroup>
          <thead>
            <tr>
              <th>Strategy</th>
              <th>Contracts</th>
              <th>Params</th>
              <th>Status</th>
              <th>Cycles</th>
              <th>Unrealized</th>
              <th>Realized</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            ${primaryRow}
            ${sleeveRows}
          </tbody>
        </table>
      </div>
    </section>
  `;
}

function lotAge(ts) {
  if (!ts) return '—';
  const age = Date.now() / 1000 - Number(ts);
  if (age < 60) return `${age.toFixed(0)}s`;
  if (age < 3600) return `${(age / 60).toFixed(0)}m`;
  if (age < 86400) return `${(age / 3600).toFixed(1)}h`;
  return `${(age / 86400).toFixed(1)}d`;
}

// Contract-info strip: the spec sheet for a tracked product. Coinbase shows
// this on their derivatives page (contract size, tick, margin, expiry) — we
// mirror it here so users don't have to leave the dashboard to look it up.
function renderContractInfo(symbol, config, snapshot) {
  const contractSize = Number(config?.contract_size) || 50;
  const tickSize = Number(config?.tick_size) || 0;
  const tickValue = tickSize > 0 ? tickSize * contractSize : null;
  const margin = Number(config?.margin_per_contract) || 0;
  const fee = Number(config?.fee_per_contract_roundtrip) || 0;
  const expiry = config?.contract_expiry || snapshot?.contract_expiry || null;
  const family = symbolFamilyOf(symbol);
  const cell = (label, val) => `
    <div class="contract-info-cell">
      <div class="contract-info-label">${escapeHtml(label)}</div>
      <div class="contract-info-value">${val}</div>
    </div>`;
  let expiryHtml = '—';
  if (expiry) {
    try {
      const d = new Date(expiry);
      const days = Math.round((d - Date.now()) / 86400000);
      expiryHtml = `${d.toISOString().slice(0, 10)} <span class="dim">(${days}d)</span>`;
    } catch { expiryHtml = escapeHtml(String(expiry)); }
  }
  return `
    <div class="contract-info-strip">
      <div class="contract-info-title">Contract specs</div>
      <div class="contract-info-grid">
        ${cell('Product', escapeHtml(symbol))}
        ${cell('Family', escapeHtml(family))}
        ${cell('Contract size', contractSize.toLocaleString('en-US') + ' /ct')}
        ${cell('Tick size', tickSize > 0 ? '$' + fmtPrice(tickSize, config) : '—')}
        ${cell('Tick value', tickValue !== null ? '$' + tickValue.toFixed(2) : '—')}
        ${cell('Margin / contract', margin > 0 ? '$' + margin.toLocaleString('en-US', {maximumFractionDigits: 2}) : '—')}
        ${cell('Fee / round-trip', fee > 0 ? '$' + fee.toFixed(2) : '—')}
        ${cell('Expiration', expiryHtml)}
      </div>
    </div>`;
}

function renderTargetsRow(config, snapshot) {
  // Symbol-level market data only. Per-strategy targets (buy/sell prices)
  // now live INSIDE each strategy row so sleeves with different params
  // aren't misrepresented by a single primary-derived targets row.
  const mark = Number(snapshot?.last_mark);
  if (!isFinite(mark)) return '';
  const dayHigh = Number(snapshot?.day_high);
  const dayLow = Number(snapshot?.day_low);
  const rangeHtml = (isFinite(dayHigh) && isFinite(dayLow) && dayHigh > 0 && dayLow > 0)
    ? `<div class="mark-range">
         <span class="mark-range-item"><span class="mark-range-label">Day high</span><span class="mark-range-val pos">$${fmtPrice(dayHigh)}</span></span>
         <span class="mark-range-item"><span class="mark-range-label">Day low</span><span class="mark-range-val neg">$${fmtPrice(dayLow)}</span></span>
       </div>` : '';
  const label = symbolLabel(snapshot?.product_id || '') + ' market';
  return `
    <div class="mark-row">
      <div class="mark-label">${escapeHtml(label)}</div>
      <div class="mark-value">$${fmtPrice(mark)}</div>
      <div class="mark-sub">bid $${fmtPrice(snapshot.best_bid)} · ask $${fmtPrice(snapshot.best_ask)}</div>
      ${rangeHtml}
    </div>
  `;
}

function renderPositionBar(state, config, snapshot) {
  // Old core/swing visualization was primary-strategy-specific and misleading
  // when core=0 or sleeves manage the contracts. Removed. Position info is
  // shown in the Open positions section (which is per-lot) and in the Risk
  // strip (margin usage). Nothing symbol-level to show here anymore.
  return '';
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

async function refreshScanner() {
  try {
    const resp = await fetch('/api/scanner');
    if (!resp.ok) return;
    const data = await resp.json();
    const tbody = document.querySelector('#scanner-table tbody');
    const updated = document.getElementById('scanner-updated');
    if (!tbody) return;
    tbody.innerHTML = '';
    const top = Array.isArray(data.top) ? data.top : [];
    if (top.length === 0) {
      updated.textContent = 'no ranking yet — the paper bot writes one every ~60 seconds.';
      return;
    }
    if (data.generated_at) {
      const dt = new Date(data.generated_at * 1000);
      updated.textContent = `updated ${dt.toLocaleTimeString()}`;
    }
    top.forEach((row, i) => {
      const tr = document.createElement('tr');
      tr.className = 'scanner-row';
      tr.dataset.product = row.product_id;
      tr.dataset.price = String(row.price);
      tr.dataset.high = String(row.high_24h);
      tr.dataset.low = String(row.low_24h);
      tr.dataset.volPct = String(row.vol_pct);
      tr.dataset.volume = String(row.volume_24h || 0);
      tr.innerHTML = `
        <td>${i + 1}</td>
        <td class="mono">${escapeHtml(row.product_id)}</td>
        <td class="mono">$${fmtNum(row.price, 4)}</td>
        <td class="mono pos">$${fmtNum(row.high_24h, 4)}</td>
        <td class="mono neg">$${fmtNum(row.low_24h, 4)}</td>
        <td class="mono"><b>${fmtNum(row.vol_pct, 2)}%</b></td>
        <td class="mono dim">${row.volume_24h ? fmtMoney(row.volume_24h) : '—'}</td>
      `;
      tr.onclick = () => openScannerDetail(row);
      tbody.appendChild(tr);
    });
  } catch (err) {
    console.error('scanner refresh failed', err);
  }
}

async function refreshOnce() {
  const [status, trades] = await Promise.all([
    fetchJson('/api/status'),
    fetchJson('/api/trades?n=60'),
  ]);
  if (status._unauthorized || trades._unauthorized) { showLogin(); return; }
  currentStore = status.store || {};
  renderBanners(currentStore);
  renderModeTabs(currentStore);
  renderAssetTabs(currentStore);

  const scannerSection = document.getElementById('scanner-section');
  const showScanner = activeMode === 'scanner';
  if (scannerSection) scannerSection.hidden = !showScanner;
  cardsEl.hidden = showScanner;
  document.getElementById('asset-tabs').hidden = showScanner;

  if (showScanner) {
    refreshScanner();
    cardsEl.innerHTML = '';
    tradeLogEl.innerHTML = '';
    lastUpdated.textContent = `updated ${new Date().toLocaleTimeString()}`;
    return;
  }

  cardsEl.innerHTML = '';
  const tenants = Object.keys(currentStore).sort();
  let anyRendered = false;
  for (const tenant of tenants) {
    const m = modeOfTenant(tenant);
    if (activeMode && activeMode !== 'scanner' && m && m !== activeMode) continue;
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

const CONFIG_SECTIONS = [
  {
    title: 'Position size',
    fields: [
      ['core_qty', 'Core floor (0 = no floor, free trading)', 'number', { step: 1, min: 0 }],
      ['swing_qty', 'Swing size (contracts to trade)', 'number', { step: 1, min: 1 }],
      ['max_swing_qty', 'Max swing size', 'number', { step: 1, min: 1 }],
    ],
  },
  {
    title: 'Price targets',
    fields: [
      ['sell_px', 'Sell at ($)', 'number', { step: 0.005 }],
      ['buy_px', 'Buy back at ($)', 'number', { step: 0.005 }],
      ['abort_below', 'Halt if price falls below ($)', 'number', { step: 0.01 }],
      ['abort_above', 'Halt if price runs above ($)', 'number', { step: 0.01 }],
    ],
  },
  {
    title: 'Exit strategy',
    fields: [
      ['exit_mode', 'Mode', 'select', { options: ['fixed_limit', 'trailing_stop'] }],
      ['trail_trigger', 'Trail trigger price ($)', 'number', { step: 0.005 }],
      ['trail_distance', 'Trail distance ($)', 'number', { step: 0.005 }],
      ['reanchor_threshold', 'Re-anchor threshold ($)', 'number', { step: 0.1 }],
    ],
  },
  {
    title: 'Stop-loss (protects during a crash)',
    fields: [
      ['stop_loss_enabled', 'Enable stop-loss', 'checkbox', {}],
      ['stop_loss_px', 'Trigger price — sell when price falls to ($)', 'number', { step: 0.01 }],
      ['stop_loss_qty_mode', 'Sell how many', 'select', {
        options: ['all', 'original', 'custom'],
        labels: {
          all: 'all (flatten everything above core)',
          original: 'only the original swing size (let accumulated ride)',
          custom: 'custom number',
        },
      }],
      ['stop_loss_qty_custom', 'Custom qty (only if mode=custom)', 'number', { step: 1, min: 1 }],
    ],
  },
  {
    title: 'Costs & margin  (advanced)',
    advanced: true,
    fields: [
      ['contract_size', 'Contract size (oz)', 'number', { step: 1 }],
      ['margin_per_contract', 'Margin per contract ($)', 'number', { step: 1 }],
      ['fee_per_contract_roundtrip', 'Fee per roundtrip ($)', 'number', { step: 0.01 }],
      ['scale_up_buffer_mult', 'Scale-up buffer ×', 'number', { step: 0.1, min: 1 }],
      ['fee_sanity_multiplier', 'Fee sanity ×', 'number', { step: 0.1, min: 1 }],
    ],
  },
];

const PRESET_META = {
  swing_10_net: { name: '$10 net swing', desc: '2 contracts, ~$0.20 spread anchored to current silver. Nets ~$10 per completed cycle after fees.' },
  conservative: { name: 'Conservative', desc: 'Small size, wide abort bracket. Range-scalp only.' },
  moderate: { name: 'Moderate', desc: 'Adam\'s current setup. 2-point range.' },
  aggressive: { name: 'Aggressive', desc: 'Trailing-first, tight trail. Rides breakouts.' },
};

const PRESETS = {
  conservative: {
    core_qty: 10, swing_qty: 2, max_swing_qty: 3,
    sell_px: 65.0, buy_px: 63.0, abort_below: 58.0, abort_above: 72.0,
    exit_mode: 'fixed_limit', trail_trigger: 65.0, trail_distance: 0.25, reanchor_threshold: 2.0,
    contract_size: 50, margin_per_contract: 275.0, fee_per_contract_roundtrip: 4.68,
    scale_up_buffer_mult: 2.0, fee_sanity_multiplier: 2.0,
  },
  moderate: {
    core_qty: 10, swing_qty: 2, max_swing_qty: 5,
    sell_px: 65.0, buy_px: 63.0, abort_below: 60.0, abort_above: 70.0,
    exit_mode: 'fixed_limit', trail_trigger: 65.0, trail_distance: 0.20, reanchor_threshold: 2.0,
    contract_size: 50, margin_per_contract: 275.0, fee_per_contract_roundtrip: 4.68,
    scale_up_buffer_mult: 1.5, fee_sanity_multiplier: 2.0,
  },
  aggressive: {
    core_qty: 10, swing_qty: 2, max_swing_qty: 8,
    sell_px: 65.0, buy_px: 63.0, abort_below: 61.0, abort_above: 80.0,
    exit_mode: 'trailing_stop', trail_trigger: 65.0, trail_distance: 0.15, reanchor_threshold: 2.0,
    contract_size: 50, margin_per_contract: 275.0, fee_per_contract_roundtrip: 4.68,
    scale_up_buffer_mult: 1.0, fee_sanity_multiplier: 2.0,
  },
};

function fieldMatchesPreset(cfg, preset) {
  const keys = ['core_qty', 'max_swing_qty', 'abort_below', 'abort_above', 'exit_mode', 'trail_distance', 'scale_up_buffer_mult'];
  return keys.every(k => String(cfg[k]) === String(preset[k]));
}

function detectActivePreset(cfg) {
  for (const [name, preset] of Object.entries(PRESETS)) {
    if (fieldMatchesPreset(cfg, preset)) return name;
  }
  return null;
}

function openConfigEditor(tenant, symbol) {
  const cfg = currentStore[tenant]?.[symbol]?.config || {};
  configEditContext = { tenant, symbol };
  configTitle.textContent = `Settings — ${symbol}`;
  configForm.innerHTML = '';
  configErrors.innerHTML = '';

  // Presets row at top
  const activePreset = detectActivePreset(cfg);
  const presetsWrap = document.createElement('div');
  presetsWrap.className = 'config-section';
  presetsWrap.innerHTML = `<h3>Start from a preset</h3>`;
  const presetGrid = document.createElement('div');
  presetGrid.className = 'preset-grid';
  for (const [key, meta] of Object.entries(PRESET_META)) {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'preset-card' + (activePreset === key ? ' active' : '');
    card.dataset.preset = key;
    card.innerHTML = `<div class="preset-name">${meta.name}</div><div class="preset-desc">${meta.desc}</div>`;
    card.onclick = () => applyPreset(key);
    presetGrid.appendChild(card);
  }
  presetsWrap.appendChild(presetGrid);
  configForm.appendChild(presetsWrap);

  // Sections
  for (const section of CONFIG_SECTIONS) {
    const sec = document.createElement('div');
    sec.className = 'config-section';
    sec.innerHTML = `<h3>${section.title}</h3>`;
    if (section.advanced) sec.dataset.advanced = 'true';
    if (section.advanced) sec.hidden = true;
    const fields = document.createElement('div');
    fields.className = 'config-fields';
    for (const [key, label, type, opts] of section.fields) {
      const wrap = document.createElement('label');
      const span = document.createElement('span');
      span.textContent = label;
      wrap.appendChild(span);
      let input;
      if (type === 'select') {
        input = document.createElement('select');
        input.name = key;
        for (const o of opts.options) {
          const opt = document.createElement('option');
          opt.value = o;
          opt.textContent = (opts.labels && opts.labels[o]) || o;
          if (String(cfg[key] || '') === o) opt.selected = true;
          input.appendChild(opt);
        }
      } else if (type === 'checkbox') {
        input = document.createElement('input');
        input.type = 'checkbox';
        input.name = key;
        input.checked = !!cfg[key];
        wrap.classList.add('checkbox-row');
      } else {
        input = document.createElement('input');
        input.type = type;
        input.name = key;
        input.value = cfg[key] ?? '';
        if (opts?.step != null) input.step = opts.step;
        if (opts?.min != null) input.min = opts.min;
      }
      wrap.appendChild(input);
      fields.appendChild(wrap);
    }
    sec.appendChild(fields);
    configForm.appendChild(sec);
  }

  // Advanced toggle button
  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'advanced-toggle';
  toggle.textContent = 'Show advanced (fees, margin, contract size)';
  toggle.onclick = () => {
    const advanced = configForm.querySelectorAll('[data-advanced="true"]');
    let anyHidden = false;
    advanced.forEach(a => { if (a.hidden) anyHidden = true; });
    advanced.forEach(a => { a.hidden = !anyHidden ? true : false; });
    toggle.textContent = anyHidden ? 'Hide advanced' : 'Show advanced (fees, margin, contract size)';
  };
  configForm.appendChild(toggle);

  configModal.hidden = false;
}

function applyPreset(name) {
  let preset = PRESETS[name];
  // The $10-net-swing preset anchors buy/sell around the current silver mark
  // rather than hardcoded 63/65, so the config still makes sense whether
  // silver is $58 or $72. Math: $10 net for 2 contracts needs ~$0.20 spread
  // after ~$9.36 in roundtrip fees.
  if (name === 'swing_10_net') {
    const ctx = configEditContext || {};
    const snap = currentStore?.[ctx.tenant]?.[ctx.symbol]?.snapshot || {};
    const mark = Number(snap.last_mark) || 62.50;
    const buy = round3(mark - 0.10);
    const sell = round3(mark + 0.10);
    preset = {
      core_qty: 0, swing_qty: 2, max_swing_qty: 2,
      sell_px: sell, buy_px: buy,
      abort_below: round3(mark - 2.50),
      abort_above: round3(mark + 2.50),
      exit_mode: 'fixed_limit',
      trail_trigger: sell, trail_distance: 0.20, reanchor_threshold: 2.0,
      contract_size: 50, margin_per_contract: 275.0,
      fee_per_contract_roundtrip: 4.68,
      scale_up_buffer_mult: 1.5, fee_sanity_multiplier: 2.0,
    };
  }
  if (!preset) return;
  for (const [key, val] of Object.entries(preset)) {
    const input = configForm.querySelector(`[name="${key}"]`);
    if (!input) continue;
    input.value = val;
  }
  // Re-mark active preset
  configForm.querySelectorAll('.preset-card').forEach(c => {
    c.classList.toggle('active', c.dataset.preset === name);
  });
}

function round3(n) { return Math.round(n * 1000) / 1000; }

async function saveConfig() {
  if (!configEditContext) return;
  if (isLiveTenant(configEditContext.tenant)) {
    const ok = await confirmLive({
      title: `Save live config — ${configEditContext.symbol}`,
      body: 'You are editing the <b>LIVE</b> tenant\'s config. Changes take effect on the bot\'s next tick and drive real market orders (sell targets, buy-backs, stop-loss, size). Double-check every number before saving.',
    });
    if (!ok) return;
  }
  const cfg = {};
  for (const section of CONFIG_SECTIONS) {
    for (const [key, , type] of section.fields) {
      const input = configForm.querySelector(`[name="${key}"]`);
      if (!input) continue;
      if (type === 'checkbox') {
        cfg[key] = !!input.checked;
        continue;
      }
      let val = input.value;
      if (val === '' || val === null || val === undefined) continue;
      if (type === 'number') val = Number(val);
      cfg[key] = val;
    }
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
    backtestResult.innerHTML = renderLeaderboard(res.results, res.applied_cfg);
  } else {
    backtestResult.innerHTML = renderBacktestSummary(res.result, res.applied_cfg);
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

function renderLeaderboard(results, appliedCfg) {
  if (!results?.length) return '<div class="dim">no results</div>';
  // Rank by total_return (highest first). Errored runs go last.
  const ranked = results.slice().sort((a, b) => {
    const ea = a.error != null;
    const eb = b.error != null;
    if (ea && !eb) return 1;
    if (eb && !ea) return -1;
    return (Number(b.total_return) || 0) - (Number(a.total_return) || 0);
  });

  // Price range info (all strategies see the same window, so any result has it)
  const first = ranked.find(r => r.price_min != null) || {};
  const priceInfo = first.price_min != null ? `
    <div class="backtest-price-range">
      window price range: <b>$${fmtPrice(first.price_min)}</b>
       – <b>$${fmtPrice(first.price_max)}</b>
       · candles: ${first.candle_count}
       · start $${fmtPrice(first.price_start)} → end $${fmtPrice(first.price_end)}
    </div>
  ` : '';
  const fitInfo = (appliedCfg && appliedCfg.auto_fit) ? `
    <div class="backtest-auto-fit">
      Auto-fit thresholds to this window:
      <b>buy $${fmtPrice(appliedCfg.buy_px)}</b> ·
      <b>sell $${fmtPrice(appliedCfg.sell_px)}</b> ·
      abort below $${fmtPrice(appliedCfg.abort_below)} / above $${fmtPrice(appliedCfg.abort_above)}.
      Ranking below reflects strategy MECHANICS, not whether a specific price target was hit.
    </div>
  ` : '';

  const rows = ranked.map((r, i) => {
    const winnerCls = i === 0 && !r.error ? 'winner' : '';
    const err = r.error ? `<td colspan="6" class="neg">error: ${escapeHtml(r.error)}</td>` : `
      <td class="${classForValue(r.total_return)}">${fmtMoney(r.total_return)}</td>
      <td>${fmtNum(r.total_return_pct, 2)}%</td>
      <td class="neg">${fmtMoney(r.max_drawdown)}</td>
      <td>${r.cycles}</td>
      <td>${r.fills}</td>
      <td>${r.halted ? '⚠' : '✓'}</td>
    `;
    const medal = i === 0 ? '🥇' : i === 1 ? '🥈' : i === 2 ? '🥉' : `${i + 1}`;
    return `
      <tr class="${winnerCls}">
        <td class="rank-cell">${medal}</td>
        <td class="strategy-cell">${escapeHtml(r.strategy)}${i === 0 && !r.error ? ' <span class="best-tag">BEST</span>' : ''}</td>
        ${err}
      </tr>
    `;
  }).join('');

  const zeroFills = ranked.every(r => (r.fills || 0) === 0 && !r.error);
  const zeroNote = zeroFills ? `
    <div class="backtest-empty-note">
      No strategy fired in this window. Price stayed inside your bounds — try a
      wider window, tighter <code>buy_px</code> / <code>sell_px</code>, or a
      finer granularity (hourly instead of daily) for more resolution.
    </div>
  ` : '';

  return `
    ${priceInfo}
    ${fitInfo}
    <table class="leaderboard">
      <thead>
        <tr>
          <th>rank</th><th>strategy</th><th>return</th><th>return %</th>
          <th>max dd</th><th>cycles</th><th>fills</th><th>ok</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
    ${zeroNote}
    <div class="overfit-warning" style="margin-top:16px">
      Ranked on this specific window. Whichever strategy wins here won THIS
      slice of history. Try 3+ windows spanning different regimes before
      trusting the ranking.
    </div>
  `;
}

// ---- sleeves editor -----------------------------------------------------

function openSleeveEditor(tenant, symbol, sleeveId, lotContext = null) {
  const block = currentStore[tenant]?.[symbol] || {};
  const cfg = block.config || {};
  const snap = block.snapshot || {};
  const mark = Number(snap.last_mark) || 0;
  const posAvgEntry = Number(snap.position_avg_entry) || 0;
  // Priority order for the anchor (the price the swing is centered on):
  //   1. Lot's entry price if opened from a specific lot ("+ Strategy" per lot)
  //   2. Existing sleeve's buy_px (preserve on edit)
  //   3. Current mark for a general "+ add strategy" — reflects RIGHT NOW,
  //      not a stale blended cost basis that could include ancient positions.
  // Two anchor choices are now offered as a toggle in the modal so the user
  // can flip between "purchased at" and "current market" without losing the
  // other. anchor = the default (initially selected) value; anchorAlt = the
  // OTHER option shown next to it. When neither purchased-at nor
  // strategy's-original is applicable, only Current market is offered.
  let anchor;
  let anchorLabel;
  let anchorAlt = null;
  let anchorAltLabel = null;
  if (lotContext) {
    anchor = Number(lotContext.entry_price) || mark;
    anchorLabel = "Purchased price";
    anchorAlt = mark;
    anchorAltLabel = 'Current market';
  } else if (sleeveId) {
    const existingSleeve = (cfg.sleeves || []).find(s => s.id === sleeveId);
    anchor = existingSleeve ? Number(existingSleeve.buy_px) || mark : mark;
    anchorLabel = "Strategy's original entry";
    anchorAlt = mark;
    anchorAltLabel = 'Current market';
  } else if (posAvgEntry > 0 && Math.abs(posAvgEntry - mark) > 0.001) {
    // For fresh strategies with an existing position, offer the position's
    // weighted-avg entry price as the "purchased at" alternative. Lets the
    // user swing around what they actually paid instead of forcing the anchor
    // to today's mark.
    anchor = mark;
    anchorLabel = 'Current market';
    anchorAlt = posAvgEntry;
    anchorAltLabel = 'Your avg purchase price';
  } else {
    anchor = mark;
    anchorLabel = 'Current market';
  }
  const contractSize = Number(cfg.contract_size) || 50;
  const feeRt = Number(cfg.fee_per_contract_roundtrip) || 4.68;
  const sleeves = Array.isArray(cfg.sleeves) ? [...cfg.sleeves] : [];
  const existing = sleeveId ? sleeves.find(s => s.id === sleeveId) : null;

  // Capacity: how many contracts can this sleeve use?
  const pos = Number(snap.position_qty ?? 0);
  const core = Number(cfg.core_qty ?? 0);
  const primary = Number(cfg.swing_qty ?? 0);
  const otherSleeves = sleeves.filter(s => s.id !== sleeveId);
  // Only count sleeves that CURRENTLY own contracts (ARMED_SELL). Sleeves in
  // ARMED_BUY have already sold their portion — they'll consume capacity when
  // they rebuy, but that's a future event, not a current claim. This lets a
  // fresh manual buy actually free up room for a new strategy.
  const sleeveStates = snap.state?.sleeves || (currentStore[tenant]?.[symbol]?.state?.sleeves) || {};
  const otherSleeveQty = otherSleeves.reduce((n, s) => {
    const st = sleeveStates[s.id]?.state || 'ARMED_SELL';
    return n + (st === 'ARMED_SELL' ? (Number(s.qty) || 0) : 0);
  }, 0);
  // Free capacity for THIS sleeve. If everything's already assigned, freeCapacity
  // is 0 — the modal will show a warning and disable the qty input instead of
  // pretending the user can add "1 more".
  const freeCapacity = Math.max(0, pos - core - primary - otherSleeveQty);
  // When editing an existing sleeve, its own qty is already counted against
  // freeCapacity via the filter above (otherSleeves), so max = freeCapacity + its own qty.
  const maxQty = freeCapacity + (existing ? Number(existing.qty || 0) : 0);
  const atCapacity = maxQty < 1;

  // Slider is TOTAL NET profit (after fees) for the strategy (all contracts, one
  // swing). Per spec §5A: you set take-home, the bot places gross = target + fees.
  const startingQty = Math.min(lotContext ? lotContext.qty : (existing?.qty || 1), maxQty);
  const defaultTotalProfit = 50 * startingQty;
  const existingTotalProfit = existing
    ? Math.max(10, Math.round(
        (existing.sell_px - existing.buy_px) * contractSize * existing.qty
        - feeRt * existing.qty
      ))
    : defaultTotalProfit;

  // The 4 canonical swing-trading authors from the build spec (§5, §6). Each
  // preset packages the exit mode + profit target + trail distance the way
  // that author's method suggests. "Custom" lets the user configure freely.
  const PRESETS = {
    'Jim Paul': {
      // What I Learned Losing a Million Dollars — RISK-FIRST. Small, defensible
      // range. Fixed limit with tight profit target so you're never far offside.
      exit_mode: 'fixed_limit',
      profitDollarsPerContract: 25,   // $0.50 spread on a 50oz contract
      trailDistance: 0.05,
      note: 'Risk-first fixed limit. Tight range, small targets — Paul cared about not blowing up more than winning big.',
    },
    'Livermore': {
      // Reminiscences of a Stock Operator — PIVOT + PYRAMID. Wider swings,
      // fixed limit at whole-number pivots. Big spread, meaningful targets.
      exit_mode: 'fixed_limit',
      profitDollarsPerContract: 100,  // $2 spread
      trailDistance: 0.20,
      note: 'Pivot-based fixed limit. Wider swings around whole-number levels — Livermore traded pivot points, not noise.',
    },
    'Carter': {
      // Mastering the Trade — TRAILING/BREAKOUT. Arms above pivot, rides trend,
      // exits on structure break. Real trailing stop with medium distance.
      exit_mode: 'trailing_stop',
      profitDollarsPerContract: 50,   // arm at anchor + $1
      trailDistance: 0.15,
      note: "Trailing stop that rides trend. Arms on breakout, rides upside, exits when structure breaks — Carter's bread and butter.",
    },
    'Elder': {
      // Trading for a Living — TRIPLE SCREEN. Medium fixed limit with wider
      // range to give the setup room to work.
      exit_mode: 'fixed_limit',
      profitDollarsPerContract: 60,   // $1.20 spread
      trailDistance: 0.10,
      note: 'Fixed limit with room to breathe. Elder wanted setups to have time to develop — medium targets, patient exits.',
    },
    '$5 net swing': {
      // FIXED target: always nets $5 total per cycle regardless of qty. Spread
      // auto-adjusts based on contract count. profitDollarsFixed overrides the
      // per-contract math in applyPreset — see comment there.
      exit_mode: 'fixed_limit',
      profitDollarsFixed: 5,
      trailDistance: 0.10,
      note: '$5 net per cycle. Spread auto-adjusts to your qty so this preset ALWAYS nets $5 after fees.',
    },
    '$10 net swing': {
      exit_mode: 'fixed_limit',
      profitDollarsFixed: 10,
      trailDistance: 0.10,
      note: '$10 net per cycle. Spread auto-adjusts to your qty so this preset ALWAYS nets $10 after fees.',
    },
    '$25 net swing': {
      exit_mode: 'fixed_limit',
      profitDollarsFixed: 25,
      trailDistance: 0.15,
      note: '$25 net per cycle. Spread auto-adjusts to your qty so this preset ALWAYS nets $25 after fees.',
    },
    '$50 net swing': {
      exit_mode: 'fixed_limit',
      profitDollarsFixed: 50,
      trailDistance: 0.20,
      note: '$50 net per cycle. Spread auto-adjusts to your qty so this preset ALWAYS nets $50 after fees.',
    },
    '$100 net swing': {
      exit_mode: 'fixed_limit',
      profitDollarsFixed: 100,
      trailDistance: 0.25,
      note: '$100 net per cycle. Spread auto-adjusts to your qty so this preset ALWAYS nets $100 after fees.',
    },
    'Custom': {
      exit_mode: 'fixed_limit',
      profitDollarsPerContract: 50,
      trailDistance: 0.10,
      note: 'You set every parameter. Use this when the presets don\'t match what you want.',
    },
  };
  const nextAuthorName = () => {
    const used = new Set(sleeves.map(s => s.name));
    for (const n of Object.keys(PRESETS)) if (!used.has(n) && n !== 'Custom') return n;
    return 'Custom';
  };

  const draft = existing || {
    id: `s${Date.now().toString(36)}`,
    name: nextAuthorName(),
    qty: 1,
    exit_mode: 'fixed_limit',
    reanchor_threshold: 2.0,
  };

  // Distance from anchor to current mark — surface a warning when the anchor
  // (typically a lot's old entry price) is far from where the market is now.
  const anchorToMarketDist = anchor && mark ? Math.abs(mark - anchor) : 0;
  const anchorStale = anchorToMarketDist > 0.5;

  let m = document.getElementById('sleeve-modal');
  if (!m) {
    m = document.createElement('div');
    m.id = 'sleeve-modal';
    m.className = 'modal';
    document.body.appendChild(m);
  }
  m.innerHTML = `
    <div class="modal-panel">
      <div class="modal-header">
        <h2>${existing ? 'Edit strategy' : 'Add strategy'}</h2>
        <button class="modal-close" data-close>✕</button>
      </div>
      <div class="sleeve-anchor ${anchorStale ? 'stale' : ''}">
        <div class="sleeve-anchor-title">Anchor the strategy around</div>
        <div class="sleeve-anchor-toggle" role="tablist">
          ${anchorAlt !== null ? `
            <button type="button" class="anchor-choice ${anchor === anchorAlt ? 'active' : ''}" id="sl-anchor-alt"
                    data-anchor="${anchorAlt}">
              <span class="anchor-choice-label">${anchorAltLabel}</span>
              <span class="anchor-choice-value">$${fmtPrice(anchorAlt)}</span>
            </button>
          ` : ''}
          <button type="button" class="anchor-choice ${anchor === mark ? 'active' : ''}" id="sl-anchor-market"
                  data-anchor="${mark}">
            <span class="anchor-choice-label">Current market</span>
            <span class="anchor-choice-value">$${fmtPrice(mark)}</span>
          </button>
        </div>
        ${anchorStale ? `
          <div class="sleeve-anchor-sub">
            <span class="stale-warn">Selected anchor is $${fmtPrice(anchorToMarketDist)} away from current market — targets below may be off-market</span>
          </div>` : ''}
      </div>
      <div class="sleeve-form">
        <label>Preset (author)
          <select id="sl-preset">
            ${Object.keys(PRESETS).map(name =>
              `<option value="${escapeHtml(name)}" ${draft.name === name ? 'selected' : ''}>${escapeHtml(name)}</option>`
            ).join('')}
          </select>
        </label>
        <label>Name<input type="text" id="sl-name" value="${escapeHtml(draft.name)}"></label>
        <label>Contracts ${atCapacity
          ? '<span class="capacity-warn">(no free capacity — buy more or reduce another strategy)</span>'
          : `(max ${maxQty})`}
          <input type="number" id="sl-qty" min="1" max="${Math.max(1, maxQty)}" step="1" ${atCapacity ? 'disabled' : ''} value="${Math.min(lotContext ? lotContext.qty : draft.qty, Math.max(1, maxQty))}">
        </label>
        <label>Strategy type
          <select id="sl-exit">
            <option value="fixed_limit" ${draft.exit_mode === 'fixed_limit' ? 'selected' : ''}>Fixed limit (sell high, buy low)</option>
            <option value="trailing_stop" ${draft.exit_mode === 'trailing_stop' ? 'selected' : ''}>Trailing stop (ride the trend)</option>
            <option value="hybrid" ${draft.exit_mode === 'hybrid' ? 'selected' : ''}>Hybrid (take the swing, or trail a breakout)</option>
          </select>
        </label>
      </div>

      <div class="preset-note" id="sl-preset-note"></div>

      <!-- Explicit sell/buy target inputs — always visible so you can override the slider -->
      <div class="target-inputs">
        <label>Sell target
          <input type="number" id="sl-sell-target" step="0.005" value="${existing?.sell_px ?? (anchor + 0.5).toFixed(3)}">
        </label>
        <label>Buy-back target
          <input type="number" id="sl-buy-target" step="0.005" value="${existing?.buy_px ?? (anchor - 0.5).toFixed(3)}">
        </label>
      </div>

      <!-- Profit target slider — TAKE-HOME (net after fees). Bot back-calcs the gap. -->
      <div class="profit-slider-block" id="sl-fixed-block">
        <div class="profit-slider-header">
          <span class="slider-label">Or drag: <b>net</b> take-home per swing (after fees, all <span id="sl-qty-echo">${startingQty}</span> contracts)</span>
          <span class="slider-value" id="sl-profit-val">$${existingTotalProfit}</span>
        </div>
        <input type="range" id="sl-profit" min="10" max="2000" step="10" value="${existingTotalProfit}" class="profit-slider">
        <div class="profit-slider-ticks">
          <span>$10</span><span>$500</span><span>$1,000</span><span>$2,000</span>
        </div>
        <div class="cost-floor-note" id="sl-cost-floor"></div>
      </div>

      <!-- Trailing-only add-on: trail distance below the high water -->
      <div class="profit-slider-block" id="sl-trail-block" hidden>
        <div class="profit-slider-header">
          <span class="slider-label">Trail distance (pullback before selling)</span>
          <span class="slider-value" id="sl-td-val">$0.100</span>
        </div>
        <input type="range" id="sl-td-slider" min="0.010" max="1.000" step="0.005" value="${existing?.trail_distance || 0.100}" class="profit-slider">
        <div class="profit-slider-ticks">
          <span>$0.01</span><span>$0.25</span><span>$0.50</span><span>$1.00</span>
        </div>
        <div class="preview-note">
          Trailing stop uses the sell target above as the ARM price — once silver hits it, the trail engages and rides upside. Sells when price pulls back this much from the peak.
        </div>
      </div>

      <!-- Hybrid-only add-on: activation price + delay window -->
      <div class="profit-slider-block" id="sl-hybrid-block" hidden>
        <div class="target-inputs">
          <label>Trail activation price
            <input type="number" id="sl-trail-activation" step="0.005" value="${existing?.trail_activation_px ?? (anchor + 0.75).toFixed(3)}">
          </label>
          <label>Delay window (seconds)
            <input type="number" id="sl-hybrid-delay" step="1" min="1" max="60" value="${existing?.hybrid_delay_secs ?? 5}">
          </label>
        </div>
        <div class="preview-note">
          Once silver crosses the <b>sell target</b>, we wait this many seconds to see if it pushes through the <b>trail activation price</b>.
          If it does → trailing stop engages and rides the breakout. If it doesn't → we take the swing at market when the delay expires.
        </div>
      </div>

      <div class="sleeve-preview" id="sl-preview">
        <!-- filled by updatePreview() -->
      </div>

      <!-- Per-sleeve accumulation. Independent of the primary's scale-up so
           each sleeve compounds its own realized P&L into more contracts. -->
      <div class="accumulate-block">
        <label class="accumulate-toggle">
          <input type="checkbox" id="sl-accumulate" ${draft.accumulate_enabled ? 'checked' : ''}>
          <b>Accumulate profits into more contracts</b>
        </label>
        <div class="accumulate-fields" id="sl-accumulate-fields" ${draft.accumulate_enabled ? '' : 'hidden'}>
          <div class="target-inputs">
            <label>Max contracts
              <input type="number" id="sl-max-qty" min="1" step="1" value="${draft.max_qty || (draft.qty || 1) * 5}">
            </label>
            <label>Add-one buffer ×
              <input type="number" id="sl-scale-buf" min="1" step="0.1" value="${draft.scale_up_buffer_mult || 1.5}">
            </label>
          </div>
          <div class="preview-note">
            When banked profit ≥ <b>margin/contract × buffer</b> (default $275 × 1.5 = $412),
            this sleeve grows by 1 contract. Repeats until max is reached. Set buffer to
            1.0 to add sooner ($275), 2.0 for a safer $550 cushion.
          </div>
        </div>
      </div>

      <!-- Per-sleeve stop-loss. Fires independently — only this sleeve halts. -->
      <div class="accumulate-block">
        <label class="accumulate-toggle">
          <input type="checkbox" id="sl-stoploss" ${draft.stop_loss_enabled ? 'checked' : ''}>
          <b>Stop-loss (protects during a crash)</b>
        </label>
        <div class="accumulate-fields" id="sl-stoploss-fields" ${draft.stop_loss_enabled ? '' : 'hidden'}>
          <div class="target-inputs">
            <label>Trigger price ($) — sell when silver falls to
              <input type="number" id="sl-stop-px" step="0.01" value="${draft.stop_loss_px || Math.max(0, +(mark - 2).toFixed(2))}">
            </label>
            <label>Sell how many
              <select id="sl-stop-mode">
                <option value="all" ${draft.stop_loss_qty_mode === 'all' ? 'selected' : ''}>all this sleeve's contracts</option>
                <option value="original" ${draft.stop_loss_qty_mode === 'original' ? 'selected' : ''}>only the current qty (let accumulated ride)</option>
                <option value="custom" ${draft.stop_loss_qty_mode === 'custom' ? 'selected' : ''}>custom number</option>
              </select>
            </label>
          </div>
          <div class="target-inputs" id="sl-stop-custom-row" ${draft.stop_loss_qty_mode === 'custom' ? '' : 'hidden'}>
            <label>Custom sell qty
              <input type="number" id="sl-stop-qty" min="1" step="1" value="${draft.stop_loss_qty_custom || 1}">
            </label>
          </div>
          <div class="preview-note">
            When silver ≤ trigger, this sleeve market-sells the configured qty then halts.
            Core floor is always respected. Only THIS sleeve is affected — other sleeves keep trading.
          </div>
        </div>
      </div>

      <div id="sleeve-error" class="issues" hidden></div>
      <div class="modal-footer">
        <button data-close>cancel</button>
        <button class="primary" id="sleeve-save-btn">save</button>
      </div>
    </div>
  `;
  m.hidden = false;

  const presetEl = m.querySelector('#sl-preset');
  const presetNoteEl = m.querySelector('#sl-preset-note');
  const nameEl = m.querySelector('#sl-name');
  const profitEl = m.querySelector('#sl-profit');
  const profitValEl = m.querySelector('#sl-profit-val');
  const previewEl = m.querySelector('#sl-preview');
  const qtyEl = m.querySelector('#sl-qty');
  const qtyEchoEl = m.querySelector('#sl-qty-echo');
  const exitEl = m.querySelector('#sl-exit');
  const fixedBlock = m.querySelector('#sl-fixed-block');
  const trailBlock = m.querySelector('#sl-trail-block');
  const hybridBlock = m.querySelector('#sl-hybrid-block');
  const tdSliderEl = m.querySelector('#sl-td-slider');
  const tdValEl = m.querySelector('#sl-td-val');
  const sellTargetEl = m.querySelector('#sl-sell-target');
  const buyTargetEl = m.querySelector('#sl-buy-target');
  const trailActivationEl = m.querySelector('#sl-trail-activation');
  const hybridDelayEl = m.querySelector('#sl-hybrid-delay');
  const anchorMarketBtn = m.querySelector('#sl-anchor-market');
  const anchorAltBtn = m.querySelector('#sl-anchor-alt');
  const accumulateToggle = m.querySelector('#sl-accumulate');
  const accumulateFields = m.querySelector('#sl-accumulate-fields');
  if (accumulateToggle && accumulateFields) {
    accumulateToggle.addEventListener('change', () => {
      accumulateFields.hidden = !accumulateToggle.checked;
    });
  }
  const stopLossToggle = m.querySelector('#sl-stoploss');
  const stopLossFields = m.querySelector('#sl-stoploss-fields');
  const stopModeEl = m.querySelector('#sl-stop-mode');
  const stopCustomRow = m.querySelector('#sl-stop-custom-row');
  if (stopLossToggle && stopLossFields) {
    stopLossToggle.addEventListener('change', () => {
      stopLossFields.hidden = !stopLossToggle.checked;
    });
  }
  if (stopModeEl && stopCustomRow) {
    stopModeEl.addEventListener('change', () => {
      stopCustomRow.hidden = stopModeEl.value !== 'custom';
    });
  }

  let currentAnchor = anchor;  // mutable so "use market instead" can update it

  function applyPreset(name) {
    const p = PRESETS[name];
    if (!p) return;
    exitEl.value = p.exit_mode;
    const qty = Math.max(1, Number(qtyEl.value) || 1);
    // Two preset flavors:
    //  - profitDollarsFixed:  net-per-cycle stays constant regardless of qty
    //    (spread auto-adjusts so "$10 net swing" ALWAYS nets $10)
    //  - profitDollarsPerContract:  scales with qty ($5/contract × 3 = $15)
    //    Used by named-trader presets where per-contract is the canonical unit.
    // Cost floor still applies: can't set net below fees + $1.
    const feesTotal = feeRt * qty;
    const costFloor = Math.ceil(feesTotal + 1);
    const targetProfit = p.profitDollarsFixed != null
      ? p.profitDollarsFixed
      : p.profitDollarsPerContract * qty;
    const appliedTarget = Math.min(2000, Math.max(costFloor, targetProfit));
    profitEl.value = appliedTarget;
    tdSliderEl.value = p.trailDistance;
    // If the fixed target got bumped up by the cost floor (e.g. "$5 net swing"
    // at qty=3 where fees alone exceed $5), surface that truthfully in the
    // note — silent clamping was the previous bug where users thought they
    // were nettting $5 but actually netting $16.
    if (p.profitDollarsFixed != null && appliedTarget > p.profitDollarsFixed) {
      presetNoteEl.innerHTML = p.note + ` <b style="color:var(--warn)">Note: at ${qty} contracts, fees alone are $${feesTotal.toFixed(2)}, so this preset targets $${appliedTarget} net (the floor) not $${p.profitDollarsFixed}.</b>`;
    } else {
      presetNoteEl.textContent = p.note;
    }
    syncTargetsFromSlider();
    applyModeVisibility();
  }

  function syncTargetsFromSlider() {
    // Spec §5A: slider = target NET after fees. Back-calculate the gross gap:
    //   gross_needed = target_net + roundtrip_fees
    //   spread_per_contract = gross_needed / (qty × contract_size)
    // That's the price gap the bot must actually capture to hand you the net.
    const qty = Math.max(1, Number(qtyEl.value) || 1);
    const feesTotal = feeRt * qty;
    // Cost-gated MINIMUM (spec §5A): can't set a net target that's lower than
    // fees alone — you'd need to capture more than the target just to pay them.
    // Clamp slider min to fees+$1 so any target guarantees at least $1 net.
    const costFloor = Math.ceil(feesTotal + 1);
    if (Number(profitEl.min) !== costFloor) profitEl.min = costFloor;
    if (Number(profitEl.value) < costFloor) profitEl.value = costFloor;
    const costFloorEl = m.querySelector('#sl-cost-floor');
    if (costFloorEl) costFloorEl.innerHTML = `Minimum: <b>$${costFloor}</b> — round-trip fees on ${qty} contract${qty === 1 ? '' : 's'} are $${feesTotal.toFixed(2)}. Lower nets would be eaten by fees.`;
    const targetNet = Number(profitEl.value);
    const grossNeeded = targetNet + feesTotal;
    const spread = grossNeeded / (qty * contractSize);
    sellTargetEl.value = (currentAnchor + spread / 2).toFixed(3);
    buyTargetEl.value = (currentAnchor - spread / 2).toFixed(3);
  }

  function updateFillPct(el) {
    if (!el) return;
    const min = Number(el.min), max = Number(el.max), val = Number(el.value);
    const pct = ((val - min) / (max - min)) * 100;
    el.style.background = `linear-gradient(to right, var(--accent) 0%, var(--accent) ${pct}%, var(--panel-3) ${pct}%, var(--panel-3) 100%)`;
  }

  function updatePreview() {
    const qty = Math.max(1, Number(qtyEl.value) || 1);
    if (qtyEchoEl) qtyEchoEl.textContent = qty;
    const feesPerSwing = feeRt * qty;
    const mode = exitEl.value;
    const sellPx = Number(sellTargetEl.value) || 0;
    const buyPx = Number(buyTargetEl.value) || 0;
    const grossPerSwing = (sellPx - buyPx) * contractSize * qty;
    const netPerSwing = grossPerSwing - feesPerSwing;

    profitValEl.textContent = `$${profitEl.value}`;
    updateFillPct(profitEl);
    updateFillPct(tdSliderEl);
    tdValEl.textContent = `$${fmtPrice(Number(tdSliderEl.value))}`;

    if (mode === 'trailing_stop') {
      const trailDistance = Number(tdSliderEl.value) || 0.1;
      const estGross = Math.max(0, (sellPx - trailDistance - buyPx) * contractSize * qty);
      const estNet = estGross - feesPerSwing;
      previewEl.innerHTML = `
        <div class="preview-row">
          <div><span class="preview-label">Buy back at</span><span class="preview-num buy">$${fmtPrice(buyPx)}</span></div>
          <div><span class="preview-label">Now</span><span class="preview-num">$${fmtPrice(mark)}</span></div>
          <div><span class="preview-label">Arms at</span><span class="preview-num sell">$${fmtPrice(sellPx)}</span></div>
        </div>
        <div class="preview-econ">
          <span>Trail distance: <b>$${fmtPrice(trailDistance)}</b></span>
          <span>Est min sell: <b>$${fmtPrice(sellPx - trailDistance)}</b></span>
          <span>Est gross (min): <b>$${estGross.toFixed(2)}</b></span>
          <span>Fees: <b>−$${feesPerSwing.toFixed(2)}</b></span>
          <span>Est net (min): <b class="${estNet > 0 ? 'pos' : 'neg'}">${estNet >= 0 ? '+' : ''}$${estNet.toFixed(2)}</b></span>
        </div>
        <div class="preview-note">Trailing rides upside — profit is variable. Preview shows worst case (trail fires at arm price).</div>
      `;
    } else if (mode === 'hybrid') {
      const activation = Number(trailActivationEl?.value) || sellPx;
      const delay = Number(hybridDelayEl?.value) || 5;
      const trailDistance = Number(tdSliderEl.value) || 0.1;
      // Path A: delay expires without breakout — market-sell at sell_px area.
      const perContract = qty > 0 ? grossPerSwing / qty : 0;
      // Path B: activation crossed — trail engages. Worst case: trail fires
      // at activation minus trail distance.
      const estGrossTrailWorst = Math.max(0, (activation - trailDistance - buyPx) * contractSize * qty);
      const estNetTrailWorst = estGrossTrailWorst - feesPerSwing;
      previewEl.innerHTML = `
        <div class="preview-row">
          <div><span class="preview-label">Buy back at</span><span class="preview-num buy">$${fmtPrice(buyPx)}</span></div>
          <div><span class="preview-label">Sell target</span><span class="preview-num sell">$${fmtPrice(sellPx)}</span></div>
          <div><span class="preview-label">Activation</span><span class="preview-num">$${fmtPrice(activation)}</span></div>
        </div>
        <div class="preview-econ">
          <span>Delay: <b>${delay}s</b></span>
          <span>Trail distance: <b>$${fmtPrice(trailDistance)}</b></span>
        </div>
        <div class="preview-econ">
          <span><b>Path A</b> (no breakout): sell at target → net <b class="${netPerSwing > 0 ? 'pos' : 'neg'}">${netPerSwing >= 0 ? '+' : ''}$${netPerSwing.toFixed(2)}</b></span>
          <span><b>Path B</b> (breakout, worst): trail sells at $${fmtPrice(activation - trailDistance)} → net <b class="${estNetTrailWorst > 0 ? 'pos' : 'neg'}">${estNetTrailWorst >= 0 ? '+' : ''}$${estNetTrailWorst.toFixed(2)}</b></span>
        </div>
        <div class="preview-note">Activation must be above the sell target. Path B is a floor — real breakouts can ride much higher before the trail fires.</div>
      `;
    } else {
      const perContract = qty > 0 ? grossPerSwing / qty : 0;
      previewEl.innerHTML = `
        <div class="preview-row">
          <div><span class="preview-label">Buy back at</span><span class="preview-num buy">$${fmtPrice(buyPx)}</span></div>
          <div><span class="preview-label">Now</span><span class="preview-num">$${fmtPrice(mark)}</span></div>
          <div><span class="preview-label">Sell at</span><span class="preview-num sell">$${fmtPrice(sellPx)}</span></div>
        </div>
        <div class="preview-econ">
          <span>Swing width: <b>$${fmtPrice(sellPx - buyPx)}</b></span>
          <span>Per contract: <b>$${perContract.toFixed(2)}</b></span>
          <span>Gross/swing: <b>$${grossPerSwing.toFixed(2)}</b></span>
          <span>Fees: <b>−$${feesPerSwing.toFixed(2)}</b></span>
          <span>Net/swing: <b class="${netPerSwing > 0 ? 'pos' : 'neg'}">${netPerSwing >= 0 ? '+' : ''}$${netPerSwing.toFixed(2)}</b></span>
        </div>
      `;
    }
  }

  const applyModeVisibility = () => {
    const mode = exitEl.value;
    // Trail distance slider is used by BOTH trailing_stop and hybrid modes.
    trailBlock.hidden = !(mode === 'trailing_stop' || mode === 'hybrid');
    hybridBlock.hidden = mode !== 'hybrid';
    updatePreview();
  };

  // Wire events
  presetEl.addEventListener('change', () => { nameEl.value = presetEl.value; applyPreset(presetEl.value); });
  nameEl.addEventListener('input', () => {
    // If user types over the name, they're overriding — mark preset as Custom
    if (presetEl.value !== 'Custom' && nameEl.value !== presetEl.value) presetEl.value = 'Custom';
  });
  profitEl.addEventListener('input', () => { syncTargetsFromSlider(); updatePreview(); });
  qtyEl.addEventListener('input', () => {
    // Re-apply the preset when qty changes so per-contract-scaled presets
    // recompute their target (e.g., Paul preset at 1c = $25 net, at 3c = $75
    // net). Fixed-target presets are safe under this too — they just re-clamp
    // against the new cost floor.
    if (presetEl.value && presetEl.value !== 'Custom') {
      applyPreset(presetEl.value);
    } else {
      syncTargetsFromSlider();
    }
    updatePreview();
  });
  exitEl.addEventListener('change', applyModeVisibility);
  tdSliderEl.addEventListener('input', updatePreview);
  sellTargetEl.addEventListener('input', updatePreview);
  buyTargetEl.addEventListener('input', updatePreview);
  if (trailActivationEl) trailActivationEl.addEventListener('input', updatePreview);
  if (hybridDelayEl) hybridDelayEl.addEventListener('input', updatePreview);
  function setAnchor(newAnchor, activeBtn) {
    currentAnchor = newAnchor;
    // Toggle the .active class on the two choice buttons so the user can
    // always see which anchor is in play. Both buttons remain visible so the
    // choice is reversible.
    if (anchorMarketBtn) anchorMarketBtn.classList.toggle('active', activeBtn === anchorMarketBtn);
    if (anchorAltBtn) anchorAltBtn.classList.toggle('active', activeBtn === anchorAltBtn);
    syncTargetsFromSlider();
    updatePreview();
  }
  if (anchorMarketBtn) anchorMarketBtn.onclick = () => setAnchor(mark, anchorMarketBtn);
  if (anchorAltBtn) anchorAltBtn.onclick = () =>
    setAnchor(Number(anchorAltBtn.dataset.anchor), anchorAltBtn);

  // Initial state
  if (!existing) applyPreset(presetEl.value);
  else { presetNoteEl.textContent = PRESETS[draft.name]?.note || ''; syncTargetsFromSlider(); }
  applyModeVisibility();

  m.querySelector('#sleeve-save-btn').onclick = async () => {
    const errEl = m.querySelector('#sleeve-error');
    errEl.hidden = true;
    const sellPx = Number(sellTargetEl.value);
    const buyPx = Number(buyTargetEl.value);
    const trailDistance = Number(tdSliderEl.value);
    const trailActivation = Number(trailActivationEl?.value);
    const hybridDelay = Number(hybridDelayEl?.value);
    const usesTrail = exitEl.value === 'trailing_stop' || exitEl.value === 'hybrid';
    const accumulateEl = m.querySelector('#sl-accumulate');
    const maxQtyEl = m.querySelector('#sl-max-qty');
    const scaleBufEl = m.querySelector('#sl-scale-buf');
    const accumulateEnabled = !!(accumulateEl && accumulateEl.checked);
    const stopPxEl = m.querySelector('#sl-stop-px');
    const stopQtyEl = m.querySelector('#sl-stop-qty');
    const stopLossEnabled = !!(stopLossToggle && stopLossToggle.checked);
    const stopMode = stopModeEl?.value || 'all';
    const stopPx = Number(stopPxEl?.value || 0);
    const patch = {
      id: draft.id,
      name: nameEl.value || draft.id,
      qty: parseInt(qtyEl.value, 10),
      exit_mode: exitEl.value,
      sell_px: sellPx,
      buy_px: buyPx,
      trail_trigger: sellPx,
      trail_distance: usesTrail ? trailDistance : Math.max(0.02, (sellPx - buyPx) / 4),
      trail_activation_px: exitEl.value === 'hybrid' ? trailActivation : (sellPx + 0.5),
      hybrid_delay_secs: exitEl.value === 'hybrid' ? hybridDelay : 5.0,
      reanchor_threshold: draft.reanchor_threshold,
      accumulate_enabled: accumulateEnabled,
      max_qty: accumulateEnabled ? parseInt(maxQtyEl?.value || 0, 10) : 0,
      scale_up_buffer_mult: accumulateEnabled ? Number(scaleBufEl?.value || 1.5) : 1.5,
      stop_loss_enabled: stopLossEnabled,
      stop_loss_px: stopLossEnabled ? stopPx : 0,
      stop_loss_qty_mode: stopLossEnabled ? stopMode : 'all',
      stop_loss_qty_custom: stopLossEnabled && stopMode === 'custom'
        ? parseInt(stopQtyEl?.value || 1, 10) : 0,
    };
    if (!(patch.qty >= 1)) { errEl.hidden = false; errEl.innerHTML = 'Contracts must be at least 1'; return; }
    if (!(buyPx < sellPx)) { errEl.hidden = false; errEl.innerHTML = 'Buy target must be below sell target'; return; }
    if (usesTrail && !(trailDistance > 0)) { errEl.hidden = false; errEl.innerHTML = 'Trail distance must be > 0'; return; }
    if (exitEl.value === 'hybrid') {
      if (!(trailActivation > sellPx)) { errEl.hidden = false; errEl.innerHTML = 'Trail activation must be above the sell target'; return; }
      if (!(hybridDelay >= 1)) { errEl.hidden = false; errEl.innerHTML = 'Delay must be at least 1 second'; return; }
    }
    if (stopLossEnabled) {
      if (!(stopPx > 0)) { errEl.hidden = false; errEl.innerHTML = 'Stop-loss trigger price must be > 0'; return; }
      if (!(stopPx < buyPx)) { errEl.hidden = false; errEl.innerHTML = 'Stop-loss trigger must be below the buy-back target'; return; }
      if (stopMode === 'custom' && !(parseInt(stopQtyEl?.value || 0, 10) >= 1)) {
        errEl.hidden = false; errEl.innerHTML = 'Custom stop-loss qty must be at least 1'; return;
      }
    }

    const next = existing
      ? sleeves.map(s => s.id === draft.id ? patch : s)
      : [...sleeves, patch];
    if (isLiveTenant(tenant)) {
      const ok = await confirmLive({
        title: `${existing ? 'Save' : 'Add'} live strategy — ${symbol}`,
        body: `<b>${existing ? 'Update' : 'Add'} strategy</b> "${escapeHtml(patch.name)}" on <b>${escapeHtml(symbol)}</b> (LIVE).<br><br>` +
              `<b>Contracts:</b> ${patch.qty}<br>` +
              `<b>Sell:</b> $${patch.sell_px.toFixed(3)} &nbsp;&nbsp; <b>Buy back:</b> $${patch.buy_px.toFixed(3)}<br>` +
              `<b>Exit mode:</b> ${patch.exit_mode}<br><br>` +
              'Once saved, the bot will place real orders on the next tick.',
      });
      if (!ok) return;
    }
    const res = await putJson('/api/sleeves', { tenant, symbol, sleeves: next });
    if (res._unauthorized) { showLogin(); return; }
    if (res.ok) { m.hidden = true; refreshOnce(); }
    else {
      errEl.hidden = false;
      errEl.innerHTML = escapeHtml(res.error || 'save failed') +
        (res.issues ? '<ul>' + res.issues.map(i => `<li>${escapeHtml(i.field)}: ${escapeHtml(i.message)}</li>`).join('') + '</ul>' : '');
    }
  };
}

async function deleteSleeve(tenant, symbol, sleeveId) {
  if (isLiveTenant(tenant)) {
    const ok = await confirmLive({
      title: `Delete live strategy ${sleeveId}`,
      body: `Removes strategy <b>${escapeHtml(sleeveId)}</b> from rotation on <b>${escapeHtml(symbol)}</b>.<br><br>` +
            'Its pending order (if any) will be cancelled on the next tick. Contracts it held stay in your position — they just have no strategy managing them anymore.',
    });
    if (!ok) return;
  } else {
    if (!confirm(`Delete strategy ${sleeveId}? Any live order it holds will be cancelled next tick.`)) return;
  }
  const block = currentStore[tenant]?.[symbol] || {};
  const sleeves = (block.config?.sleeves || []).filter(s => s.id !== sleeveId);
  const res = await putJson('/api/sleeves', { tenant, symbol, sleeves });
  if (res._unauthorized) { showLogin(); return; }
  if (res.ok) { refreshOnce(); showToast('strategy deleted', 'info'); }
  else showToast(res.error || 'delete failed', 'error');
}

// ---- manual trade -------------------------------------------------------

function openTradeModal(tenant, symbol, side) {
  tradeContext = { tenant, symbol, side };
  const snap = currentStore[tenant]?.[symbol]?.snapshot || {};
  const cfg = currentStore[tenant]?.[symbol]?.config || {};
  const mark = Number(snap.last_mark) || 0;
  const pos = Number(snap.position_qty) || 0;
  const core = Number(cfg.core_qty) || 0;
  const availMargin = Number(snap.available_margin) || 0;
  const marginPer = Number(cfg.margin_per_contract) || 275;

  // Reset order type to Market on every open, and seed the limit price field
  // with a reasonable default (best bid/ask for the side, or the mark).
  const marketRadio = document.querySelector('input[name="trade-order-type"][value="market"]');
  if (marketRadio) marketRadio.checked = true;
  const limitRow = document.getElementById('trade-limit-row');
  if (limitRow) limitRow.hidden = true;
  const limitInput = document.getElementById('trade-limit-price');
  if (limitInput) {
    const defaultPx = side === 'BUY'
      ? Number(snap.best_bid) || mark
      : Number(snap.best_ask) || mark;
    limitInput.value = defaultPx ? defaultPx.toFixed(3) : '';
  }

  const maxSell = Math.max(0, pos - core);            // core floor guard (0 = full pos)
  const marginCap = Math.floor(availMargin / marginPer);
  const maxBuy = Math.max(1, Math.min(100, marginCap)); // capped by margin, 100 hard limit

  tradeModalTitle.textContent = `${side === 'BUY' ? 'Buy' : 'Sell'} contracts at market — ${symbol}`;
  const holdLine = core > 0
    ? `You hold <b style="color:var(--text)">${pos} contracts</b> · core floor is <b style="color:var(--text)">${core}</b> (never sold)`
    : `You hold <b style="color:var(--text)">${pos} contracts</b> · no floor — every contract is tradeable`;
  tradeModalBody.innerHTML = `
    <div style="line-height:1.7; color: var(--muted); font-size: 14px;">
      <div>${holdLine}</div>
      <div>${escapeHtml(symbolLabel(symbol))} market now: <b style="color:var(--text)">$${fmtPrice(mark)}</b> — bid $${fmtPrice(snap.best_bid)} / ask $${fmtPrice(snap.best_ask)}</div>
    </div>
  `;

  const max = side === 'SELL' ? maxSell : maxBuy;
  tradeQty.max = max;
  tradeQty.value = Math.max(1, Math.min(1, max) || 1);
  tradeQty.disabled = max < 1;
  if (max < 1) {
    tradeConfirm.disabled = true;
    tradeError.hidden = false;
    tradeError.innerHTML = side === 'SELL'
      ? `<b>Nothing to sell.</b> You hold 0 contracts.`
      : `<b>Not enough margin.</b> Deposit more or reduce open positions.`;
  }

  // Quick-select chips: 1, 2, half, max
  const quickEl = document.getElementById('trade-qty-quick');
  quickEl.innerHTML = '';
  const chips = uniqueSortedQtys([1, 2, Math.max(1, Math.floor(max / 2)), max]).filter(q => q >= 1 && q <= max);
  for (const q of chips) {
    const label = q === max && q > 1 ? `all (${q})` : String(q);
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'qty-chip';
    chip.textContent = label;
    chip.onclick = () => { tradeQty.value = q; updateTradePreview(); markActiveChip(quickEl, q); };
    quickEl.appendChild(chip);
  }
  markActiveChip(quickEl, Number(tradeQty.value));

  // Max note
  const maxNote = document.getElementById('trade-max-note');
  if (side === 'SELL') {
    if (maxSell > 0) {
      maxNote.textContent = core > 0
        ? `up to ${maxSell} (protects core floor of ${core})`
        : `up to ${maxSell} (your full position)`;
    } else {
      maxNote.textContent = core > 0
        ? `nothing to sell — position ${pos} = core ${core}`
        : `nothing to sell — position is 0`;
    }
  } else {
    maxNote.textContent = `up to ${maxBuy} (limited by ~$${availMargin.toLocaleString('en-US', { maximumFractionDigits: 0 })} available margin)`;
  }

  tradeError.hidden = true;
  updateTradePreview();
  tradeModal.hidden = false;
  setTimeout(() => { tradeQty.focus(); tradeQty.select(); }, 50);
}

function uniqueSortedQtys(arr) {
  return [...new Set(arr.filter(n => Number.isFinite(n) && n > 0).map(n => Math.floor(n)))].sort((a, b) => a - b);
}

function markActiveChip(container, qty) {
  container.querySelectorAll('.qty-chip').forEach(c => {
    const val = parseInt(c.textContent, 10);
    c.classList.toggle('active', val === qty);
  });
}

function updateTradePreview() {
  if (!tradeContext) return;
  const { tenant, symbol, side } = tradeContext;
  const snap = currentStore[tenant]?.[symbol]?.snapshot || {};
  const cfg = currentStore[tenant]?.[symbol]?.config || {};
  const mark = Number(snap.last_mark) || 0;
  const pos = Number(snap.position_qty) || 0;
  const core = Number(cfg.core_qty) || 0;
  const contractSize = Number(cfg.contract_size) || 50;
  const fee = Number(cfg.fee_per_contract_roundtrip) / 2 || 2.34;
  const qty = Number(tradeQty.value) || 0;

  const orderType = document.querySelector('input[name="trade-order-type"]:checked')?.value || 'market';
  const limitInput = document.getElementById('trade-limit-price');
  const limitPrice = Number(limitInput?.value) || 0;

  // Notional uses the LIMIT price when placing a limit order, otherwise the mark.
  // A limit order at your set price is exactly what you'd pay — the mark is
  // only relevant for market orders that fill immediately.
  const priceForNotional = orderType === 'limit' && limitPrice > 0 ? limitPrice : mark;
  const newPos = side === 'BUY' ? pos + qty : pos - qty;
  const notional = priceForNotional * contractSize * qty;
  const feeCost = fee * qty;

  const wouldBreach = side === 'SELL' && newPos < core;
  const wouldGoNegative = side === 'SELL' && newPos < 0;
  const badLimit = orderType === 'limit' && !(limitPrice > 0);
  tradeError.hidden = !(wouldBreach || wouldGoNegative || badLimit);
  tradeConfirm.disabled = wouldBreach || wouldGoNegative || qty < 1 || badLimit;
  if (wouldGoNegative) {
    tradeError.innerHTML = `<b>Refused:</b> you only hold ${pos} contracts — can't sell ${qty}.`;
  } else if (wouldBreach) {
    tradeError.innerHTML = `<b>Refused:</b> selling ${qty} would take you to ${newPos} contracts, below your core floor of ${core}. Lower your core floor to 0 for free trading, or sell fewer.`;
  } else if (badLimit) {
    tradeError.innerHTML = `<b>Enter a limit price above 0.</b>`;
  }

  // Hint on the limit input: how far from the current mark this price sits.
  const hintEl = document.getElementById('trade-limit-hint');
  if (hintEl) {
    if (orderType === 'limit' && limitPrice > 0 && mark > 0) {
      const diff = limitPrice - mark;
      const sign = diff >= 0 ? '+' : '−';
      hintEl.textContent = `${sign}$${Math.abs(diff).toFixed(3)} vs mark $${mark.toFixed(3)}`;
    } else {
      hintEl.textContent = '';
    }
  }

  const priceLabel = orderType === 'limit'
    ? `at your limit of $${limitPrice.toFixed(3)}`
    : `at current mark $${mark.toFixed(3)}`;

  tradePreview.innerHTML = `
    <div>Position after: <b style="color:var(--text)">${pos} → ${newPos}</b> contracts</div>
    <div>Total ${priceLabel}: <b style="color:var(--text)">$${notional.toLocaleString('en-US', { maximumFractionDigits: 2 })}</b></div>
    <div>Estimated fee: $${feeCost.toFixed(2)}</div>
    ${orderType === 'limit' ? '<div style="color:var(--muted)">Order sits open until price reaches your limit or you cancel it.</div>' : ''}
  `;
}

async function submitTrade() {
  if (!tradeContext) return;
  const { tenant, symbol, side } = tradeContext;
  const qty = Number(tradeQty.value);
  const orderType = document.querySelector('input[name="trade-order-type"]:checked')?.value || 'market';
  const limitPrice = Number(document.getElementById('trade-limit-price')?.value) || 0;
  if (orderType === 'limit' && !(limitPrice > 0)) {
    tradeError.hidden = false;
    tradeError.innerHTML = '<b>Enter a limit price above 0.</b>';
    return;
  }
  if (isLiveTenant(tenant)) {
    const priceLine = orderType === 'limit'
      ? `at LIMIT $${limitPrice.toFixed(3)}`
      : `at MARKET`;
    const ok = await confirmLive({
      title: `${side} ${qty} ${symbol} — real money`,
      body: `<b>${side} ${qty}</b> contract${qty === 1 ? '' : 's'} of <b>${escapeHtml(symbol)}</b> ${priceLine} on Coinbase.<br><br>` +
            (orderType === 'limit'
              ? `Sits open until filled or cancelled. This is not paper — real cash moves out of your account when it fills.`
              : `Fills immediately at current ${side === 'BUY' ? 'ask' : 'bid'}. This is not paper — real cash moves out of your account.`),
    });
    if (!ok) return;
  }
  const res = await postJson('/api/manual-trade', {
    tenant, symbol, side, qty, order_type: orderType,
    limit_price: orderType === 'limit' ? limitPrice : null,
    confirm: 'YES',
  });
  if (res.ok) {
    tradeModal.hidden = true;
    refreshOnce();
  } else {
    tradeError.hidden = false;
    tradeError.innerHTML = `<b>Failed:</b> ${escapeHtml(res.error || 'unknown')}`;
  }
}

tradeQty.addEventListener('input', () => {
  updateTradePreview();
  markActiveChip(document.getElementById('trade-qty-quick'), Number(tradeQty.value));
});
tradeConfirm.addEventListener('click', submitTrade);

// Order-type toggle + limit price live-update the preview and show/hide the
// limit row. Wired once on load — the modal keeps the same DOM across opens.
document.querySelectorAll('input[name="trade-order-type"]').forEach(el => {
  el.addEventListener('change', () => {
    const isLimit = document.querySelector('input[name="trade-order-type"]:checked')?.value === 'limit';
    const row = document.getElementById('trade-limit-row');
    if (row) row.hidden = !isLimit;
    updateTradePreview();
  });
});
document.getElementById('trade-limit-price')?.addEventListener('input', updateTradePreview);

// ---- scanner detail: chart + purchase ----------------------------------

const scannerDetailModal = document.getElementById('scanner-detail-modal');
const scannerDetailTitle = document.getElementById('scanner-detail-title');
const scannerDetailSummary = document.getElementById('scanner-detail-summary');
const scannerDetailTimeframes = document.getElementById('scanner-detail-timeframes');
const scannerDetailChart = document.getElementById('scanner-detail-chart');
const scannerDetailWarning = document.getElementById('scanner-detail-warning');
const scannerBuyBtn = document.getElementById('scanner-buy-btn');

// {product_id, price, high_24h, low_24h, vol_pct, volume_24h}
let scannerDetailContext = null;
// {days, granularity}
let scannerChartWindow = { days: 7, granularity: 'FIVE_MINUTE' };

const TIMEFRAMES = [
  { label: '1D', days: 1,  granularity: 'FIVE_MINUTE' },
  { label: '7D', days: 7,  granularity: 'FIVE_MINUTE' },
  { label: '30D', days: 30, granularity: 'ONE_HOUR' },
];

function openScannerDetail(row) {
  scannerDetailContext = row;
  scannerChartWindow = { days: 7, granularity: 'FIVE_MINUTE' };
  scannerDetailTitle.textContent = row.product_id;
  // Default the mode chooser to whatever tab the user's on if it's meaningful,
  // else fall back to paper (safer default for accidental clicks).
  const defaultMode = ['live', 'lab', 'paper'].includes(activeMode) ? activeMode : 'paper';
  document.querySelectorAll('input[name="scanner-buy-mode"]').forEach(r => {
    r.checked = (r.value === defaultMode);
    r.onchange = () => updateScannerBuyButton();
  });
  const qtyInput = document.getElementById('scanner-buy-qty');
  if (qtyInput) {
    qtyInput.value = 1;
    qtyInput.oninput = () => updateScannerBuyButton();
  }
  scannerDetailSummary.innerHTML = `
    <div class="scanner-detail-price">
      <span class="mono">$${fmtNum(row.price, 4)}</span>
      <span class="scanner-detail-range"><span class="pos">$${fmtNum(row.high_24h, 4)}</span> / <span class="neg">$${fmtNum(row.low_24h, 4)}</span></span>
      <span class="scanner-detail-vol">24h range <b>${fmtNum(row.vol_pct, 2)}%</b></span>
    </div>
  `;

  // Timeframe buttons
  scannerDetailTimeframes.innerHTML = '';
  for (const tf of TIMEFRAMES) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'timeframe-btn' + (tf.days === scannerChartWindow.days ? ' active' : '');
    b.textContent = tf.label;
    b.onclick = () => {
      scannerChartWindow = { days: tf.days, granularity: tf.granularity };
      scannerDetailTimeframes.querySelectorAll('.timeframe-btn').forEach(x => x.classList.remove('active'));
      b.classList.add('active');
      loadScannerChart();
    };
    scannerDetailTimeframes.appendChild(b);
  }

  // Purchase button behavior depends on whether the active tenant tracks this symbol
  updateScannerBuyButton();

  scannerDetailModal.hidden = false;
  loadScannerChart();
}

function updateScannerBuyButton() {
  if (!scannerDetailContext) return;
  const symbol = scannerDetailContext.product_id;
  const mode = selectedScannerBuyMode();
  const orderType = selectedScannerOrderType();
  const qtyInput = document.getElementById('scanner-buy-qty');
  const qty = Math.max(1, Math.min(100, parseInt(qtyInput?.value || '1', 10) || 1));
  const limitPrice = Number(document.getElementById('scanner-limit-price')?.value) || 0;
  const priceForPreview = orderType === 'limit' && limitPrice > 0
    ? limitPrice
    : Number(scannerDetailContext.price) || 0;

  // Contract size defaults to 50 (silver/mini futures); would ideally read
  // from the symbol's spec — good enough for a preview line.
  const contractSize = 50;
  const notional = priceForPreview * contractSize * qty;
  const feeEst = 2.34 * qty;
  const priceLabel = orderType === 'limit'
    ? (limitPrice > 0 ? `at your limit $${limitPrice.toFixed(3)}` : 'at your limit (enter price)')
    : `at market ~$${(Number(scannerDetailContext.price) || 0).toFixed(3)}`;

  const previewEl = document.getElementById('scanner-buy-preview');
  if (previewEl) {
    previewEl.innerHTML = `
      <div>Buying <b style="color:var(--text)">${qty}</b> contract${qty === 1 ? '' : 's'} of <b style="color:var(--text)">${escapeHtml(symbol)}</b> ${priceLabel}</div>
      <div>Total: <b style="color:var(--text)">$${notional.toLocaleString('en-US', { maximumFractionDigits: 2 })}</b> · fee ~$${feeEst.toFixed(2)}</div>
    `;
  }

  scannerBuyBtn.textContent = orderType === 'limit'
    ? `place ${mode} LIMIT for ${qty} ${symbol}`
    : `place ${mode} MARKET for ${qty} ${symbol}`;
  scannerBuyBtn.disabled = orderType === 'limit' && !(limitPrice > 0);

  if (mode === 'live') {
    scannerDetailWarning.hidden = false;
    scannerDetailWarning.innerHTML = `
      <b>Live</b> = real money. Real cash moves ${orderType === 'limit' ? 'when the limit fills' : 'immediately'} on Coinbase.
      No strategy will manage this position after — you'll need to close it manually on Coinbase or add a strategy for
      <b>${escapeHtml(symbol)}</b> later.
    `;
  } else {
    scannerDetailWarning.hidden = false;
    scannerDetailWarning.innerHTML = `
      <b>Paper</b> mode logs a simulated fill. It doesn't persist a position
      unless <b>${escapeHtml(symbol)}</b> is added as a tracked symbol first
      (needs a running strategy to accumulate P&L over time).
    `;
  }
}

function selectedScannerOrderType() {
  const checked = document.querySelector('input[name="scanner-order-type"]:checked');
  return checked ? checked.value : 'market';
}

function selectedScannerBuyMode() {
  const checked = document.querySelector('input[name="scanner-buy-mode"]:checked');
  return checked ? checked.value : 'paper';
}

function tenantForMode(mode) {
  const target = ['live', 'paper', 'lab'].includes(mode) ? mode : 'paper';
  for (const t of Object.keys(currentStore || {})) {
    if (modeOfTenant(t) === target) return t;
  }
  return null;
}

scannerBuyBtn.addEventListener('click', async () => {
  if (scannerBuyBtn.disabled || !scannerDetailContext) return;
  const symbol = scannerDetailContext.product_id;
  const mode = selectedScannerBuyMode();
  const orderType = selectedScannerOrderType();
  const qtyInput = document.getElementById('scanner-buy-qty');
  const qty = Math.max(1, Math.min(100, parseInt(qtyInput?.value || '1', 10) || 1));
  const limitPrice = Number(document.getElementById('scanner-limit-price')?.value) || 0;
  if (orderType === 'limit' && !(limitPrice > 0)) {
    showToast('enter a limit price above 0', 'error');
    return;
  }
  if (mode === 'live') {
    const detail = orderType === 'limit'
      ? `LIMIT at $${limitPrice.toFixed(3)}`
      : 'MARKET at current ask';
    const ok = confirm(`REAL MONEY: place a ${detail} BUY of ${qty} ${symbol} contract${qty > 1 ? 's' : ''} on Coinbase?`);
    if (!ok) return;
  }
  const originalLabel = scannerBuyBtn.textContent;
  scannerBuyBtn.disabled = true;
  scannerBuyBtn.textContent = 'placing…';
  try {
    const res = await postJson('/api/scanner-order', {
      product_id: symbol, side: 'BUY', qty, mode,
      order_type: orderType,
      limit_price: orderType === 'limit' ? limitPrice : null,
      confirm: 'YES',
    });
    if (res._unauthorized) { showLogin(); return; }
    if (res.ok) {
      showToast(res.message || `${mode} ${symbol} order placed`, 'info');
      scannerDetailModal.hidden = true;
    } else {
      showToast(res.error || 'scanner order failed', 'error');
      scannerBuyBtn.disabled = false;
      scannerBuyBtn.textContent = originalLabel;
    }
  } catch (err) {
    showToast(String(err.message || err), 'error');
    scannerBuyBtn.disabled = false;
    scannerBuyBtn.textContent = originalLabel;
  }
});

// Track a scanner-picked symbol so the paper bot starts a Track for it and
// persists a real position when the user buys. Also usable stand-alone: if
// the user just wants the symbol on their dashboard without buying, this puts
// it in the tracked set and adds a card.
const scannerTrackBtn = document.getElementById('scanner-track-btn');
if (scannerTrackBtn) {
  scannerTrackBtn.addEventListener('click', async () => {
    if (!scannerDetailContext) return;
    const symbol = scannerDetailContext.product_id;
    const mode = selectedScannerBuyMode();
    const tenant = tenantForMode(mode);
    if (!tenant) { showToast(`no ${mode} tenant found`, 'error'); return; }
    scannerTrackBtn.disabled = true;
    const originalLabel = scannerTrackBtn.textContent;
    scannerTrackBtn.textContent = 'tracking…';
    try {
      const res = await postJson('/api/track-symbol', { tenant, symbol });
      if (res._unauthorized) { showLogin(); return; }
      if (res.ok) {
        showToast(res.already_tracked
          ? `${symbol} already tracked`
          : `now tracking ${symbol} — the bot will pick it up on next scan (~10s)`,
          'info');
        refreshOnce();
      } else {
        showToast(res.error || 'track failed', 'error');
      }
    } catch (err) {
      showToast(String(err.message || err), 'error');
    } finally {
      scannerTrackBtn.disabled = false;
      scannerTrackBtn.textContent = originalLabel;
    }
  });
}

// Scanner order-type radios: toggle limit-price visibility and refresh preview.
document.querySelectorAll('input[name="scanner-order-type"]').forEach(el => {
  el.addEventListener('change', () => {
    const isLimit = selectedScannerOrderType() === 'limit';
    const row = document.getElementById('scanner-limit-row');
    if (row) row.hidden = !isLimit;
    // Seed limit-price field on first switch so the user has a sensible starting value.
    const limitInput = document.getElementById('scanner-limit-price');
    if (isLimit && limitInput && !limitInput.value && scannerDetailContext?.price) {
      limitInput.value = Number(scannerDetailContext.price).toFixed(3);
    }
    updateScannerBuyButton();
  });
});
document.getElementById('scanner-limit-price')?.addEventListener('input', updateScannerBuyButton);

async function loadScannerChart() {
  if (!scannerDetailContext) return;
  const { product_id } = scannerDetailContext;
  const { days, granularity } = scannerChartWindow;
  scannerDetailChart.innerHTML = '<div class="chart-status">loading candles…</div>';
  try {
    const resp = await fetch(`/api/candles?product_id=${encodeURIComponent(product_id)}&days=${days}&granularity=${granularity}`, { credentials: 'same-origin' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    if (!data.ok) throw new Error(data.error || 'unknown error');
    renderCandleChart(data.candles || [], scannerDetailChart);
  } catch (err) {
    scannerDetailChart.innerHTML = `<div class="chart-status error">chart failed: ${escapeHtml(String(err.message || err))}</div>`;
  }
}

// Compact SVG candlestick chart. Candles are [ts, open, high, low, close].
// No external charting lib — keeps the dashboard tiny and avoids CSP hassle.
function renderCandleChart(candles, container) {
  if (!candles.length) {
    container.innerHTML = '<div class="chart-status">no candles in window.</div>';
    return;
  }
  const W = container.clientWidth || 800;
  const H = 340;
  const padL = 60, padR = 12, padT = 14, padB = 28;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;

  let lo = Infinity, hi = -Infinity;
  for (const c of candles) {
    if (c[3] < lo) lo = c[3];
    if (c[2] > hi) hi = c[2];
  }
  if (lo === hi) { lo -= 0.5; hi += 0.5; }
  const range = hi - lo;
  const y = (p) => padT + plotH * (1 - (p - lo) / range);

  const n = candles.length;
  const stepW = plotW / n;
  const bodyW = Math.max(1, Math.min(6, stepW * 0.7));

  const parts = [];
  parts.push(`<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="none">`);
  // Y-axis gridlines and price labels — 5 ticks across the range
  for (let i = 0; i <= 4; i++) {
    const p = lo + (range * i / 4);
    const yy = y(p);
    parts.push(`<line x1="${padL}" y1="${yy.toFixed(1)}" x2="${W - padR}" y2="${yy.toFixed(1)}" stroke="#1e2a3a" stroke-width="1" />`);
    parts.push(`<text x="${padL - 6}" y="${yy.toFixed(1) + 4}" fill="#7a8899" font-size="10" text-anchor="end" font-family="ui-monospace,monospace">$${p.toFixed(3)}</text>`);
  }
  // X-axis: first and last timestamps
  const first = new Date(candles[0][0] * 1000);
  const last = new Date(candles[n - 1][0] * 1000);
  parts.push(`<text x="${padL}" y="${H - 8}" fill="#7a8899" font-size="10" font-family="ui-monospace,monospace">${first.toLocaleDateString()} ${first.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}</text>`);
  parts.push(`<text x="${W - padR}" y="${H - 8}" fill="#7a8899" font-size="10" text-anchor="end" font-family="ui-monospace,monospace">${last.toLocaleDateString()} ${last.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}</text>`);

  for (let i = 0; i < n; i++) {
    const [, o, h, l, c] = candles[i];
    const x = padL + i * stepW + stepW / 2;
    const up = c >= o;
    const color = up ? '#22c55e' : '#ef4444';
    const yh = y(h), yl = y(l), yo = y(o), yc = y(c);
    const top = Math.min(yo, yc);
    const bh = Math.max(1, Math.abs(yc - yo));
    parts.push(`<line x1="${x.toFixed(1)}" y1="${yh.toFixed(1)}" x2="${x.toFixed(1)}" y2="${yl.toFixed(1)}" stroke="${color}" stroke-width="1"/>`);
    parts.push(`<rect x="${(x - bodyW/2).toFixed(1)}" y="${top.toFixed(1)}" width="${bodyW.toFixed(1)}" height="${bh.toFixed(1)}" fill="${color}"/>`);
  }
  parts.push(`</svg>`);
  container.innerHTML = parts.join('');
}

// ---- delegated events ---------------------------------------------------

// Escape closes any open modal. Also clicking the dark backdrop outside
// the panel closes it — matches web-app expectations.
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  const openModals = document.querySelectorAll('.modal:not([hidden])');
  openModals.forEach(m => { m.hidden = true; });
});
document.addEventListener('click', (e) => {
  // Backdrop click: if the click hit the .modal container itself (not a
  // descendant like the panel or a button inside it), close.
  if (e.target.classList && e.target.classList.contains('modal')) {
    e.target.hidden = true;
    return;
  }
  const btn = e.target.closest('button');
  if (!btn) return;
  if (btn.dataset.close !== undefined) {
    btn.closest('.modal').hidden = true;
    return;
  }
  const action = btn.dataset.action;
  const { tenant, symbol, name, target, side } = btn.dataset;
  if (action === 'edit') openConfigEditor(tenant, symbol);
  else if (action === 'explain') openStrategyExplainer(tenant, symbol, name);
  else if (action === 'backtest') openBacktest(tenant, symbol);
  else if (action === 'trade') openTradeModal(tenant, symbol, side);
  else if (action === 'toggle-details') {
    const el = document.getElementById(target);
    if (el) {
      el.hidden = !el.hidden;
      btn.textContent = el.hidden ? 'More details' : 'Hide details';
    }
  }
  else if (action === 'add-sleeve') openSleeveEditor(tenant, symbol, null);
  else if (action === 'add-sleeve-from-lot') openSleeveEditor(tenant, symbol, null, {
    entry_price: parseFloat(btn.dataset.lotEntry),
    qty: parseInt(btn.dataset.lotQty, 10),
  });
  else if (action === 'edit-sleeve') openSleeveEditor(tenant, symbol, btn.dataset.sleeveId);
  else if (action === 'delete-sleeve') deleteSleeve(tenant, symbol, btn.dataset.sleeveId);
  else if (action === 'resume') resumeStrategy(tenant, symbol);
  else if (action === 'cancel-order') cancelOrder(tenant, symbol, btn.dataset.sleeveId || null);
  else if (action === 'sell-now') marketSell(tenant, symbol, parseInt(btn.dataset.qty, 10));
  else if (action === 'disable-primary') disablePrimaryStrategy(tenant, symbol);
});

async function disablePrimaryStrategy(tenant, symbol) {
  if (!confirm('Turn off the Primary strategy? Its swing_qty goes to 0. Only sleeves you explicitly add will run. Re-enable by editing Settings.')) return;
  const cfg = { ...(currentStore[tenant]?.[symbol]?.config || {}) };
  cfg.swing_qty = 0;
  const res = await putJson('/api/config', { tenant, symbol, config: cfg });
  if (res._unauthorized) { showLogin(); return; }
  if (res.ok) { showToast('primary strategy turned off', 'info'); refreshOnce(); }
  else showToast(res.error || res.issues?.[0]?.message || 'save failed', 'error');
}

async function cancelOrder(tenant, symbol, sleeveId) {
  if (isLiveTenant(tenant)) {
    const ok = await confirmLive({
      title: `Pause strategy — ${symbol}`,
      body: 'Cancels the pending order on Coinbase AND halts the strategy so it stops re-arming. Your contracts stay in the position — nothing sells or buys until you click Resume.',
    });
    if (!ok) return;
  }
  // halt:true → the bot cancels the pending order AND sets the state to
  // HALTED so the strategy stops re-arming on the next tick. Otherwise the
  // strategy would immediately place a new limit order — which was the
  // original bug that made this button feel broken.
  const res = await postJson('/api/cancel-order', { tenant, symbol, sleeve_id: sleeveId, halt: true });
  if (res._unauthorized) { showLogin(); return; }
  if (res.ok) { showToast('order cancel queued', 'info'); refreshOnce(); }
  else showToast(res.error || 'cancel failed', 'error');
}

async function marketSell(tenant, symbol, qty) {
  const snap = currentStore[tenant]?.[symbol]?.snapshot || {};
  const pos = Number(snap.position_qty) || 0;
  if (qty < 1 || qty > pos) { showToast(`can't sell ${qty} — you hold ${pos}`, 'error'); return; }
  if (isLiveTenant(tenant)) {
    const ok = await confirmLive({
      title: `Sell ${qty} ${symbol} at market — real money`,
      body: `<b>Market SELL ${qty}</b> contract${qty === 1 ? '' : 's'} of <b>${escapeHtml(symbol)}</b> on Coinbase.<br><br>` +
            `Fills at the current bid. Closes ${qty} of your ${pos} held contracts. Cash lands in your futures wallet.`,
    });
    if (!ok) return;
  }
  const res = await postJson('/api/manual-trade', {
    tenant, symbol, side: 'SELL', qty, confirm: 'YES',
  });
  if (res._unauthorized) { showLogin(); return; }
  if (res.ok) { showToast(`market sell ${qty} queued`, 'info'); refreshOnce(); }
  else showToast(res.error || 'sell failed', 'error');
}

async function resetPaperTrading() {
  // Reset targets the tenant that matches the CURRENT tab. Clicking 'Reset'
  // while on the Lab tab wipes Lab, not Paper — silently wiping the wrong
  // account was the previous bug. Live tenants are never reset from here
  // (the server also refuses live-tenant resets as a second guard).
  const targetMode = (activeMode === 'lab' || activeMode === 'paper') ? activeMode : 'paper';
  const targetTenant = Object.keys(currentStore).find(t => modeOfTenant(t) === targetMode);
  if (!targetTenant) {
    showToast(`no ${targetMode} tenant found`, 'error');
    return;
  }
  const symbols = Object.keys(currentStore[targetTenant] || {}).filter(s => !s.startsWith('__'));
  if (!symbols.length) { showToast('no symbol to reset', 'error'); return; }
  const label = targetMode === 'lab' ? 'Lab' : 'Paper';
  const msg = `Wipe ${label} trading state for ${targetTenant}/${symbols.join(', ')}? Balance goes back to $100k, position → 0, all lots + strategies reset. Live account is untouched.`;
  if (!confirm(msg)) return;
  let anyFailed = false;
  for (const symbol of symbols) {
    const res = await postJson('/api/reset-paper', { tenant: targetTenant, symbol, confirm: 'YES', starting_balance: 100000 });
    if (res._unauthorized) { showLogin(); return; }
    if (!res.ok) {
      anyFailed = true;
      showToast(res.error || `reset failed for ${symbol}`, 'error');
    }
  }
  if (!anyFailed) {
    showToast(`${label} reset queued — bot picks it up within 5s`, 'info');
    setTimeout(refreshOnce, 3000);
  }
}

async function resumeStrategy(tenant, symbol) {
  const res = await postJson('/api/resume', { tenant, symbol });
  if (res._unauthorized) { showLogin(); return; }
  if (res.ok) refreshOnce();
  else showToast(res.error || 'resume failed', 'error');
}

function showToast(msg, kind = 'info') {
  const el = document.createElement('div');
  el.className = `toast toast-${kind}`;
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.classList.add('visible'), 10);
  setTimeout(() => {
    el.classList.remove('visible');
    setTimeout(() => el.remove(), 300);
  }, 3500);
}

killBtn.addEventListener('click', () => {
  const mode = killBtn.dataset.mode || 'activate';
  // Pause the tenant that matches the currently-viewed tab. Falling back to
  // the alphabetically-first tenant would silently kill the wrong side when
  // multiple tenants exist (live sorts before paper alphabetically).
  const targetMode = ['live', 'paper', 'lab'].includes(activeMode) ? activeMode : 'paper';
  const tenant = Object.keys(currentStore).find(t => modeOfTenant(t) === targetMode)
    || Object.keys(currentStore)[0]
    || 'adam';
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
resetPaperBtn.addEventListener('click', resetPaperTrading);

// ---- boot ---------------------------------------------------------------

(async () => {
  const sess = await checkSession();
  if (!sess.auth_required || sess.authed) {
    showDashboard(sess.auth_required);
  } else {
    showLogin();
  }
})();
