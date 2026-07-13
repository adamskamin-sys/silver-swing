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
const stopLossBanner = document.getElementById('stop-loss-banner');
const killBtn = document.getElementById('kill-switch-btn');
const resetPaperBtn = document.getElementById('reset-paper-btn');
const stopLossToggleBtn = document.getElementById('stop-loss-toggle-btn');
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
let activeMode = 'live';        // 'paper' | 'live' | 'lab' | 'scanner' — Live is the landing dashboard
let selectedLiveProduct = null; // when set on Live tab, only this product's card renders
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
  stopLossToggleBtn.hidden = false;
  refreshOnce();
  pollHandle = setInterval(refreshOnce, POLL_MS);
}

// Global stop-loss toggle — reads current state from the live tenant's
// __stop_loss_disabled__ control scope and paints the header button.
function refreshStopLossToggleUi() {
  if (!stopLossToggleBtn) return;
  const liveTenant = Object.keys(currentStore || {}).find(t => modeOfTenant(t) === 'live');
  const disabled = !!(liveTenant && currentStore[liveTenant]?.['__stop_loss_disabled__']?.config?.disabled);
  stopLossToggleBtn.textContent = disabled ? 'Stop-loss: OFF' : 'Stop-loss: ON';
  stopLossToggleBtn.classList.toggle('danger', disabled);
  stopLossToggleBtn.classList.toggle('ghost', !disabled);
  stopLossToggleBtn.dataset.currentlyDisabled = disabled ? '1' : '0';
  stopLossToggleBtn.dataset.tenant = liveTenant || '';
  // Persistent banner while global override is in effect — the sleeve modals
  // still show per-sleeve stop_loss.enabled checked (that's saved intent, not
  // current state), which was confusing without this reminder.
  if (stopLossBanner) {
    if (disabled) {
      stopLossBanner.hidden = false;
      stopLossBanner.innerHTML = '🛑 <b>Stop-loss globally OFF</b> — bot is ignoring every stop-loss trigger on live sleeves. Click "Stop-loss: OFF" in the header to flip back ON.';
    } else {
      stopLossBanner.hidden = true;
    }
  }
  // If a sleeve modal is currently open, refresh its inline warning too so
  // Adam sees the override without having to close/reopen.
  const inlineWarn = document.getElementById('sl-stoploss-global-warn');
  if (inlineWarn) inlineWarn.hidden = !disabled;
}

async function toggleStopLoss() {
  const tenant = stopLossToggleBtn.dataset.tenant;
  if (!tenant) return alert('No live tenant found — refresh and try again.');
  const currentlyDisabled = stopLossToggleBtn.dataset.currentlyDisabled === '1';
  const nextDisabled = !currentlyDisabled;
  if (nextDisabled) {
    if (!confirm('Turn STOP-LOSS OFF for all live sleeves?\n\nBot will ignore every stop-loss trigger until you flip this back ON.')) return;
  }
  try {
    const res = await fetchJson('/api/stop-loss/toggle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tenant, disabled: nextDisabled, reason: nextDisabled ? 'dashboard toggle (pre-market whiplash guard)' : undefined }),
    });
    if (res._unauthorized || !res.ok) {
      alert('Toggle failed: ' + (res.error || 'unknown error'));
      return;
    }
    // Optimistic UI — next poll will confirm from currentStore.
    stopLossToggleBtn.dataset.currentlyDisabled = nextDisabled ? '1' : '0';
    stopLossToggleBtn.textContent = nextDisabled ? 'Stop-loss: OFF' : 'Stop-loss: ON';
    stopLossToggleBtn.classList.toggle('danger', nextDisabled);
    stopLossToggleBtn.classList.toggle('ghost', !nextDisabled);
    refreshOnce();
  } catch (err) {
    alert('Toggle failed: ' + String(err));
  }
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
  // Coinbase CFM nano futures: SLR = silver, NOL = nano oil, GC = gold, etc.
  // Traditional futures tickers (CL/NG/BZ) covered too. Crypto perps look
  // like BTC-PERP-INTX.
  if (/^(SLR|SIL|GC|GOLD|PA|PL|HG|COPPER)/.test(symbol)) return 'metals';
  if (/^(NOL|CL|NG|BZ|RB|HO)/.test(symbol)) return 'energy';
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
  NOL: 'NANO CRUDE OIL',
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

// Coinbase's display convention: NOL-20JUL26-CDE → "OIL 20 JUL 26",
// SLR-27AUG26-CDE → "SLVR 27 AUG 26". Their app uses a friendlier prefix
// than the API product_id and a spaced-out date. Matches what the user
// sees in Coinbase so labels don't feel like insider jargon.
const COINBASE_DISPLAY_PREFIX = {
  NOL: 'OIL',
  SLR: 'SLVR',
};
function prettyProductName(symbol) {
  if (!symbol || typeof symbol !== 'string') return symbol || '';
  // Crypto perps and spot don't need reformatting — display as-is.
  if (symbol.includes('-PERP-') || !symbol.includes('-')) return symbol;
  const parts = symbol.split('-');
  if (parts.length < 2) return symbol;
  const prefix = parts[0].toUpperCase();
  const displayPrefix = COINBASE_DISPLAY_PREFIX[prefix] || prefix;
  // Expiration part like '27AUG26' → '27 AUG 26'
  const dateStr = parts[1];
  const m = dateStr.match(/^(\d{1,2})([A-Z]{3})(\d{2,4})$/i);
  const formattedDate = m ? `${m[1]} ${m[2].toUpperCase()} ${m[3]}` : dateStr;
  return `${displayPrefix} ${formattedDate}`;
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
      if (symbol === '__stop_loss_disabled__') continue;
      const s = block.state || {};
      if (s.state === 'HALTED') haltedInstruments.push({ tenant, symbol, mode: modeOfTenant(tenant) });
    }
  }

  // Only show halts for the mode the user is currently viewing. In Live mode
  // Adam doesn't care about paper/lab halts — they're background sandboxes.
  const relevantHalts = haltedInstruments.filter(h => h.mode === activeMode);
  if (relevantHalts.length > 0) {
    haltBanner.hidden = false;
    haltBanner.innerHTML = `⚠ Strategy halted — ${relevantHalts.map(h => `${escapeHtml(h.tenant)}/${escapeHtml(h.symbol)}`).join(', ')}. See the halt reason on the strategy row, fix the underlying issue, then click <b>Resume</b>.`;
  } else {
    haltBanner.hidden = true;
  }

  if (killActive) {
    killBanner.hidden = false;
    const reason = killActive.reason ? ` — ${escapeHtml(killActive.reason)}` : '';
    killBanner.innerHTML = `⏸ Bot paused${reason}. Not arming new orders until you resume.`;
    killBtn.textContent = activeMode === 'live' ? 'Resume LIVE' : 'Resume bot';
    killBtn.className = 'primary';
    killBtn.dataset.mode = 'clear';
  } else {
    killBanner.hidden = true;
    // Live tab: prominent red KILL-ALL. Any other tab: subtle ghost pause.
    if (activeMode === 'live') {
      killBtn.textContent = '⛔ KILL ALL LIVE';
      killBtn.className = 'kill-all-btn';
    } else {
      killBtn.textContent = 'Pause bot';
      killBtn.className = 'ghost';
    }
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
      if (symbol === '__portfolio__') continue;
      if (symbol === '__tuned_params__') continue;
      if (symbol === '__stop_loss_disabled__') continue;
      counts[m] = (counts[m] || 0) + 1;
    }
  }

  // Live is the landing dashboard. Everything else lives behind a hamburger
  // menu so returning users don't land on the paper/lab view they no longer
  // check. The bar now shows: [current-mode badge] ................. [☰ menu]
  modeTabs.innerHTML = '';

  const currentBadge = document.createElement('div');
  currentBadge.className = 'mode-current-badge mode-' + activeMode;
  const modeLabels = { live: 'live · real money', paper: 'paper · simulated', lab: 'lab · $100k sandbox', scanner: 'scanner · derivatives', signals: 'signals · twitter (shadow)' };
  currentBadge.textContent = modeLabels[activeMode] || activeMode;
  modeTabs.appendChild(currentBadge);

  const menuWrap = document.createElement('div');
  menuWrap.className = 'mode-menu-wrap';
  const menuBtn = document.createElement('button');
  menuBtn.className = 'mode-menu-btn';
  menuBtn.setAttribute('aria-label', 'Switch mode');
  menuBtn.innerHTML = '<span></span><span></span><span></span>';
  const menuDrop = document.createElement('div');
  menuDrop.className = 'mode-menu-drop';
  menuDrop.hidden = true;
  const modes = [
    ['live',    'Live',    'real money · your portfolio',  counts.live || 0, 'mode-live'],
    ['paper',   'Paper',   'simulated fills',              counts.paper || 0, 'mode-paper'],
    ['lab',     'Lab',     '$100k sandbox · Models A-E',   counts.lab || 0, 'mode-lab'],
    ['scanner', 'Scanner', 'top derivatives by volatility', 0, 'mode-scanner'],
    ['signals', 'Signals', 'twitter · SHADOW (no trades)',  0, 'mode-signals'],
  ];
  for (const [mode, label, sub, count, cls] of modes) {
    const row = document.createElement('button');
    row.className = 'mode-menu-item ' + cls + (activeMode === mode ? ' active' : '');
    row.innerHTML = `
      <div class="mm-label">${label}${count ? ` <span class="tab-count">${count}</span>` : ''}</div>
      <div class="mm-sub">${sub}</div>
    `;
    row.onclick = () => {
      activeMode = mode;
      menuDrop.hidden = true;
      refreshOnce();
    };
    menuDrop.appendChild(row);
  }
  menuBtn.onclick = (e) => {
    e.stopPropagation();
    menuDrop.hidden = !menuDrop.hidden;
  };
  document.addEventListener('click', (e) => {
    if (!menuWrap.contains(e.target)) menuDrop.hidden = true;
  });
  menuWrap.appendChild(menuBtn);
  menuWrap.appendChild(menuDrop);
  modeTabs.appendChild(menuWrap);
}

function renderAssetTabs(store) {
  const counts = {};
  for (const [_, symbols] of Object.entries(store)) {
    for (const symbol of Object.keys(symbols || {})) {
      if (symbol === '__account_kill_switch__') continue;
      if (symbol === '__portfolio__') continue;
      if (symbol === '__tuned_params__') continue;
      if (symbol === '__stop_loss_disabled__') continue;
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

// Lab comparison view: side-by-side table of Models A–E performance.
// Reads sleeve state from the Lab tenant and lays out every metric that
// matters for A/B/C/D/E head-to-head comparison. Purely additive — cards
// still render below, this is just a summary strip.
function renderLabComparison() {
  const labTenant = Object.keys(currentStore || {}).find(t => modeOfTenant(t) === 'lab');
  if (!labTenant) return '';
  const symbols = Object.keys(currentStore[labTenant] || {}).filter(s => !s.startsWith('__'));
  if (!symbols.length) return '';
  // Aggregate Model sleeves across all Lab symbols (usually just one, but
  // could be multiple if user tracked more instruments in Lab).
  const rows = [];
  for (const symbol of symbols) {
    const block = currentStore[labTenant][symbol] || {};
    const config = block.config || {};
    const state = block.state || {};
    const sleeves = config.sleeves || [];
    const sleeveStates = state.sleeves || {};
    for (const s of sleeves) {
      const name = String(s.name || s.id || '');
      if (!name.startsWith('Model ')) continue;  // only auto-seeded model sleeves
      const ss = sleeveStates[s.id] || {};
      const cycles = Number(ss.cycles) || 0;
      const realized = Number(ss.realized_pnl) || 0;
      const consecutiveStops = Number(ss.consecutive_stops) || 0;
      const stateName = String(ss.state || 'ARMED_SELL');
      const halted = stateName === 'HALTED';
      // Avg $/cycle — realized divided by completed cycles. Meaningful once
      // you have at least a few cycles under each model.
      const avgPerCycle = cycles > 0 ? realized / cycles : 0;
      rows.push({
        name, symbol, cycles, realized, avgPerCycle,
        state: stateName, halted, consecutive_stops: consecutiveStops,
        halt_reason: ss.halt_reason || '',
      });
    }
  }
  if (!rows.length) return '';
  rows.sort((a, b) => String(a.name).localeCompare(String(b.name)));
  const bestRealized = Math.max(...rows.map(r => r.realized));
  const cell = (val, cls = '') => `<td class="${cls}">${val}</td>`;
  const numCell = (val, isWinner = false) => {
    const sign = val >= 0 ? '+' : '';
    const cls = val >= 0 ? 'pos' : 'neg';
    const winner = isWinner && val > 0 ? ' winner' : '';
    return `<td class="mono ${cls}${winner}">${sign}${fmtMoney(val)}</td>`;
  };
  const rowsHtml = rows.map(r => {
    const isWinner = r.realized === bestRealized && rows.length > 1;
    return `
    <tr class="${r.halted ? 'halted' : ''}${isWinner ? ' winner-row' : ''}">
      <td class="model-name"><b>${escapeHtml(r.name)}</b>${r.halted ? `<div class="halt-why">${escapeHtml(r.halt_reason)}</div>` : ''}</td>
      <td class="mono">${r.cycles}</td>
      ${numCell(r.realized, isWinner)}
      ${numCell(r.avgPerCycle)}
      <td class="mono ${r.consecutive_stops >= 2 ? 'neg' : 'dim'}">${r.consecutive_stops}</td>
      <td><span class="status-pill ${(r.state || '').toLowerCase()}">${escapeHtml(prettyState(r.state))}</span></td>
    </tr>`;
  }).join('');
  return `
    <div class="lab-comparison-header">
      <h3 class="section-title">Model Comparison — head-to-head</h3>
      <div class="dim">Comparison of sleeves running side-by-side on the same market data. Winner in green.</div>
    </div>
    <table class="lab-comparison-table">
      <thead>
        <tr>
          <th>Strategy</th>
          <th>Cycles</th>
          <th>Realized</th>
          <th>Avg / cycle</th>
          <th>Consec. stops</th>
          <th>State</th>
        </tr>
      </thead>
      <tbody>${rowsHtml}</tbody>
    </table>`;
}

// Live-tab portfolio view: compact single-screen table of every position.
// Reads the __portfolio__ snapshot the backend writes (main.py:
// _sync_live_portfolio). Falls back to reading the tracked-symbol snapshots
// directly if the sync hasn't populated yet, so you still see something.
// Optionally accepts a specific tenant + mode so paper can share the same
// Portfolio → Open Orders → Add-a-Position layout as live.
// Sum of (mark - own_avg_entry) × contract_size × qty across ALL sleeves on
// this product that CURRENTLY OWN contracts (state=ARMED_SELL). Returns null
// if the product has no sleeve config — signals the caller to fall back to
// position-level unrealized. Adam's ask: portfolio Unrealized column should
// reflect only the contracts a strategy is actively exposing him to, not
// the un-attached "core" contracts that would show up in position-level pnl.
function sleeveOnlyUnrealized(tenant, product, mark, coinbasePnl, positionQty, positionAvg) {
  const block = currentStore?.[tenant]?.[product];
  if (!block) return null;
  const cfg = block.config || {};
  const sleeves = Array.isArray(cfg.sleeves) ? cfg.sleeves : [];
  if (!sleeves.length) return null;
  const contractSize = Number(cfg.contract_size) || 0;
  if (contractSize <= 0 || !(mark > 0)) return null;
  const sleeveStates = (block.state || {}).sleeves || {};
  // Rule (Adam 2026-07-13): portfolio row UNREALIZED must EQUAL the sum of
  // per-sleeve UNREALIZEDs — same formula, same cycles>0 gate. Fresh
  // sleeves (cycles=0) contribute $0 because the pre-attach paper move
  // belongs to the position, not to any sleeve. This keeps row totals in
  // sync with the sleeve-editor drilldown.
  let pnl = 0;
  let qty = 0;
  for (const s of sleeves) {
    const ss = sleeveStates[s.id] || {};
    const sleeveState = String(ss.state || 'ARMED_SELL');
    if (sleeveState !== 'ARMED_SELL') continue;
    const sq = Number(s.qty) || 0;
    if (sq <= 0) continue;
    const cycles = Number(ss.cycles) || 0;
    if (cycles === 0) continue;
    const ownEntry = ss.own_avg_entry != null && ss.own_avg_entry > 0
      ? Number(ss.own_avg_entry) : Number(s.buy_px);
    if (!(ownEntry > 0)) continue;
    pnl += (mark - ownEntry) * contractSize * sq;
    qty += sq;
  }
  return { pnl, qty };
}

function renderLivePortfolio(tenantOverride, modeOverride) {
  const liveTenant = tenantOverride
    || Object.keys(currentStore || {}).find(t => modeOfTenant(t) === 'live');
  if (!liveTenant) return '';
  const isLive = modeOverride ? modeOverride === 'live' : true;
  const tenantBlock = currentStore[liveTenant] || {};
  const snap = tenantBlock['__portfolio__']?.config;

  // Build a single flat row list: [{name, pnl, side, qty, avg, mark, liq}]
  const rows = [];
  let cashTotal = 0, cashPrimary = 0, cashDeriv = 0, cashUsdc = 0;
  if (snap && snap.cash) {
    cashTotal  = Number(snap.cash.total) || 0;
    cashPrimary = Number(snap.cash.primary_usd) || 0;
    cashDeriv  = Number(snap.cash.derivatives_usd) || 0;
    cashUsdc   = Number(snap.cash.usdc) || 0;
    for (const d of snap.derivatives || []) {
      rows.push({
        kind: 'futures', product: d.product_id, side: d.side || 'LONG',
        qty: d.qty, avg: d.avg_entry, mark: d.mark,
        pnl: d.unrealized, liq: d.liquidation_price,
      });
    }
    for (const c of snap.crypto || []) {
      // Filter dust — anything under $1 USD is round-off from prior trades
      // and just clutters the portfolio (Coinbase returns dust as scientific
      // notation like 3.26e-10 BTC which renders as noise).
      if (Number(c.value_usd) < 1) continue;
      // Use product_id (BTC-USD) as the row's product, not the raw currency
      // code (BTC). Chart + get_product endpoints all need the -USD suffix.
      // Display name still shows the pretty currency code.
      rows.push({
        kind: 'spot', product: c.product_id || `${c.currency}-USD`,
        display: c.currency,
        side: '', qty: c.balance,
        avg: 0, mark: c.mark, pnl: 0, liq: 0, value: c.value_usd,
      });
    }
  } else {
    // Fallback: read each tracked live symbol's snapshot directly. No cash
    // breakdown but at least the positions render.
    for (const sym of Object.keys(tenantBlock)) {
      if (sym.startsWith('__')) continue;
      const s = tenantBlock[sym]?.snapshot;
      if (!s) continue;
      const qty = Number(s.position_qty) || 0;
      if (qty === 0) continue;
      rows.push({
        kind: 'futures', product: sym,
        side: qty > 0 ? 'LONG' : 'SHORT',
        qty: Math.abs(qty), avg: Number(s.position_avg_entry) || 0,
        mark: Number(s.last_mark) || 0, pnl: Number(s.unrealized_pnl) || 0,
        liq: Number(s.liquidation_price) || 0,
      });
    }
  }
  // Also surface products where position = 0 but a sleeve is still attached
  // (state = ARMED_BUY, waiting to rebuy after a completed sell cycle).
  // Without this, the sleeve disappears from view and the user thinks the
  // strategy was deleted — but it's actually alive, holding a buy order.
  const seenProducts = new Set(rows.map(r => r.product));
  // Look up recent scanner marks for products missing snapshot.last_mark
  // (position=0 means the primary loop isn't updating the snapshot for
  // this symbol — but the scanner has a fresh price we can borrow).
  const scannerMarks = {};
  const cachedTop = _tradeableCache && _tradeableCache.data && Array.isArray(_tradeableCache.data.top)
    ? _tradeableCache.data.top : [];
  for (const s of cachedTop) {
    if (s && s.product_id) scannerMarks[s.product_id] = Number(s.price) || 0;
  }
  for (const sym of Object.keys(tenantBlock)) {
    if (sym.startsWith('__')) continue;
    if (seenProducts.has(sym)) continue;
    const block = tenantBlock[sym] || {};
    const cfg = block.config || {};
    const sleeves = Array.isArray(cfg.sleeves) ? cfg.sleeves : [];
    if (!sleeves.length) continue;
    // Only futures — spot doesn't run the sleeve engine.
    if (!/^[A-Z0-9]+-[0-9A-Z]+-[A-Z]+$/i.test(sym)) continue;
    const snapRow = block.snapshot || {};
    const mark = Number(snapRow.last_mark) || scannerMarks[sym] || 0;
    rows.push({
      kind: 'futures', product: sym,
      side: 'WAITING',  // rendered as label in the Side column
      qty: 0, avg: 0,
      mark: mark,
      pnl: 0, liq: 0,
      _waiting: true,  // used by row renderer to style differently
    });
  }

  // Respect the asset-class subtab filter (all / metals / energy / crypto /
  // other). Filters rows to only that class; cash stays visible on 'all'.
  const filteredRows = activeAssetClass
    ? rows.filter(r => {
        if (r.kind === 'spot') return activeAssetClass === 'crypto';
        return assetClassOf(r.product) === activeAssetClass;
      })
    : rows;
  const showCash = !activeAssetClass;  // only show cash on 'all'
  const displayCashTotal = showCash ? cashTotal : 0;

  if (!filteredRows.length && !displayCashTotal) {
    return `<div class="pf-hint dim">No ${escapeHtml(activeAssetClass || 'live')} positions right now.</div>`;
  }

  const arrow = (v) => v > 0 ? '↗' : v < 0 ? '↘' : '';
  const cls = (v) => v > 0 ? 'pos' : v < 0 ? 'neg' : '';

  const cashLine = displayCashTotal > 0 ? `
    <div class="pf-cash">
      Cash <b>${fmtMoney(cashTotal)}</b>
      <span class="dim">· Primary USD ${fmtMoney(cashPrimary)}
       · Derivatives USD ${fmtMoney(cashDeriv)}
       · USDC ${fmtMoney(cashUsdc)}</span>
    </div>` : '';

  const rowsHtml = filteredRows.map(r => {
    const sym = escapeHtml(r.product || '');
    const displayName = escapeHtml(r.kind === 'futures'
      ? prettyProductName(r.product || '')
      : (r.display || r.product || ''));
    // Sleeve-only unrealized: for products with strategies attached, ignore
    // the un-attached contracts (e.g., core hold). Adam wants Unrealized to
    // reflect only what the STRATEGY is exposing him to, not the whole
    // position. Falls through to position-level pnl if no sleeves are on
    // this product.
    let effectivePnl = r.pnl;
    let sleeveContractsShown = 0;
    if (r.kind === 'futures') {
      const so = sleeveOnlyUnrealized(
        liveTenant, r.product, Number(r.mark) || 0,
        Number(r.pnl) || 0, Number(r.qty) || 0, Number(r.avg) || 0,
      );
      if (so !== null) {
        effectivePnl = so.pnl;
        sleeveContractsShown = so.qty;
      }
    }
    const dcls = cls(effectivePnl || 0);
    const pnlTooltip = (r.kind === 'futures' && sleeveContractsShown > 0 && sleeveContractsShown !== Number(r.qty))
      ? ` title="Strategy-only unrealized on ${sleeveContractsShown} of ${r.qty} contracts. Position-level was ${arrow(r.pnl)} ${fmtMoney(Math.abs(r.pnl || 0))}."`
      : '';
    const pnlText = r.kind === 'spot'
      ? `<span class="dim">${fmtMoney(r.value || 0)}</span>`
      : `<span class="${dcls}"${pnlTooltip}>${arrow(effectivePnl)} ${fmtMoney(Math.abs(effectivePnl || 0))}</span>`;
    const qtyText = r.kind === 'spot' ? fmtNum(r.qty, 6) : r.qty;
    const avgText = r.avg > 0 ? '$' + fmtPrice(r.avg) : '—';
    const markText = r.mark > 0 ? '$' + fmtPrice(r.mark) : '—';
    const liqText = r.liq > 0 ? '$' + fmtPrice(r.liq) : '—';
    const cycles = r.kind === 'futures' ? totalCyclesForProduct(r.product) : 0;
    const cyclesText = cycles > 0
      ? `<span title="Completed round-trips across all tenants (primary + sleeves)"><b>${cycles}</b></span>`
      : '<span class="dim">0</span>';
    const realized = r.kind === 'futures' ? totalRealizedForProduct(r.product, liveTenant) : 0;
    const realizedCls = realized > 0 ? 'pos' : (realized < 0 ? 'neg' : '');
    const realizedText = realized !== 0
      ? `<span class="${realizedCls}" title="Sum of primary + sleeve realized_pnl across all tenants">${realized > 0 ? '+' : ''}${fmtMoney(realized)}</span>`
      : '<span class="dim">$0</span>';
    // Combined P/L: unrealized (mark-to-market) + realized (closed trades).
    // Adam explicitly asked (2026-07-13): "we were going to make the sum of
    // realized and unrealized based off the number of contracts with
    // strategies not the total number of contracts. oil is wrong it should
    // be around 50". Root cause: this column was using r.pnl (raw
    // Coinbase position pnl, ALL contracts) while the UNREALIZED column
    // to its left already switched to effectivePnl (sleeve-only). The two
    // columns must be internally consistent — use effectivePnl for both.
    const totalPnl = (Number(effectivePnl) || 0) + realized;
    const totalCls = totalPnl > 0 ? 'pos' : (totalPnl < 0 ? 'neg' : '');
    const totalText = totalPnl !== 0
      ? `<span class="${totalCls}" title="Strategy-only unrealized + strategy-only realized. Matches the UNREALIZED column to the left (only counts contracts attached to sleeves).">${arrow(totalPnl)} ${fmtMoney(Math.abs(totalPnl))}</span>`
      : '<span class="dim">$0</span>';
    return `
      <tr class="pf-row" data-action="open-live-strategy"
          data-tenant="${escapeHtml(liveTenant)}" data-symbol="${sym}"
          data-mark="${r.mark || 0}" data-avg="${r.avg || 0}" data-pos-qty="${r.qty || 0}"
          data-side="${escapeHtml(r.side || '')}"
          title="Click to attach a Model / strategy">
        <td><b>${displayName}</b></td>
        <td class="mono">${pnlText}</td>
        <td class="mono dim">${(() => {
          if (r.kind !== 'futures') return '—';
          const cs = Number(currentStore[liveTenant]?.[r.product]?.config?.contract_size) || 0;
          return cs > 0 ? cs.toLocaleString('en-US') : '<span class="dim">—</span>';
        })()}</td>
        <td class="mono dim">${escapeHtml(r.side || '')}</td>
        <td class="mono">${qtyText}</td>
        <td class="mono">${avgText}</td>
        <td class="mono">${markText}</td>
        <td class="mono">${cyclesText}</td>
        <td class="mono">${realizedText}</td>
        <td class="mono">${totalText}</td>
        <td class="mono">${(() => {
          if (r.kind !== 'futures') return '<span class="dim">—</span>';
          const isPerp = /-PERP-/i.test(String(r.product || ''));
          if (!isPerp) return '<span class="dim">—</span>';
          const snap = currentStore[liveTenant]?.[r.product]?.snapshot || {};
          const fr = Number(snap.funding_rate ?? snap.predicted_funding_rate ?? snap.current_funding_rate);
          if (!Number.isFinite(fr)) return '<span class="dim">—</span>';
          // Positive = you PAY (bearish for long holders). Negative = you RECEIVE.
          const pct = (fr * 100).toFixed(4);
          const cls = fr <= -0.0002 ? 'pos' : fr >= 0.0002 ? 'neg' : 'dim';
          const csize = Number(currentStore[liveTenant]?.[r.product]?.config?.contract_size) || 0;
          const notional = csize * Number(r.mark || 0) * Number(r.qty || 0);
          const perPeriodPay = fr * notional;
          const dailyPay = perPeriodPay * 3;  // 3 × 8h periods per day
          const dailyPayStr = notional > 0
            ? `${fr < 0 ? '+' : '-'}$${Math.abs(dailyPay).toFixed(2)}/day`
            : '';
          const tooltip = `Funding = ${pct}% per 8h. ${fr < 0 ? 'Shorts pay longs — you RECEIVE' : fr > 0 ? 'Longs pay shorts — you PAY' : 'Neutral'}. ${dailyPayStr ? `At ${r.qty} contract${r.qty === 1 ? '' : 's'} × $${(csize * Number(r.mark || 0)).toFixed(0)} notional/ct: ${dailyPayStr}` : ''}`;
          return `<span class="${cls}" title="${escapeHtml(tooltip)}">${pct}%${dailyPayStr ? ` <span class="dim">${dailyPayStr}</span>` : ''}</span>`;
        })()}</td>
        <td class="mono dim">${liqText}</td>
      </tr>`;
  }).join('');

  // Column totals for the footer row. Sum P&L (unrealized), Cycles,
  // Realized, and P/L + Realized across every futures row. Skip Qty / Avg
  // / Mark / Liq since those don't sum across products with different
  // contract sizes.
  const totalPnlSum = filteredRows.reduce((a, r) => {
    if (r.kind !== 'futures') return a;
    // Match the row-level display: sleeve-only unrealized if a strategy is
    // attached, else fall through to the raw position pnl.
    const so = sleeveOnlyUnrealized(
      liveTenant, r.product, Number(r.mark) || 0,
      Number(r.pnl) || 0, Number(r.qty) || 0, Number(r.avg) || 0,
    );
    const rowPnl = so !== null ? so.pnl : (Number(r.pnl) || 0);
    return a + rowPnl;
  }, 0);
  const totalCyclesSum = filteredRows.reduce((a, r) =>
    a + (r.kind === 'futures' ? totalCyclesForProduct(r.product) : 0), 0);
  const totalRealizedSum = filteredRows.reduce((a, r) =>
    a + (r.kind === 'futures' ? totalRealizedForProduct(r.product, liveTenant) : 0), 0);
  const grandTotal = totalPnlSum + totalRealizedSum;
  const footClass = (v) => v > 0 ? 'pos' : v < 0 ? 'neg' : 'dim';
  const fmtSigned = (v) => v === 0 ? '$0' : `${v > 0 ? '+' : '-'}${fmtMoney(Math.abs(v))}`;
  const footHtml = filteredRows.some(r => r.kind === 'futures') ? `
    <tfoot>
      <tr class="pf-total-row">
        <td><b>Total</b></td>
        <td class="mono ${footClass(totalPnlSum)}">${fmtSigned(totalPnlSum)}</td>
        <td></td>
        <td></td>
        <td></td>
        <td></td>
        <td></td>
        <td class="mono"><b>${totalCyclesSum}</b></td>
        <td class="mono ${footClass(totalRealizedSum)}">${fmtSigned(totalRealizedSum)}</td>
        <td class="mono ${footClass(grandTotal)}">${fmtSigned(grandTotal)}</td>
        <td></td>
        <td></td>
      </tr>
    </tfoot>` : '';

  return `
    ${cashLine}
    <table class="pf-table-compact">
      <thead><tr>
        <th>Name</th><th title="Unrealized (mark-to-market on open position)">Unrealized</th><th title="Contract size — units of the underlying per contract (e.g., 50 for silver, 2000 for copper, 10 for oil/platinum). Straight from Coinbase spec.">Size</th><th>Side</th><th>Qty</th>
        <th>Avg</th><th>Mark</th><th>Cycles</th><th title="Total closed-trade profit for this product">Realized</th><th title="Unrealized + Realized">Unrealized + Realized</th><th title="Funding rate (crypto perps only). Positive = longs pay shorts (expensive carry). Negative = shorts pay longs (you get paid to hold).">Funding</th><th>Liq</th>
      </tr></thead>
      <tbody>${rowsHtml}</tbody>
      ${footHtml}
    </table>
    <div class="pf-hint dim">Click a row to attach Model B/C/D/E · auto-refreshes every 2 min</div>
    ${renderOpenOrders(liveTenant)}
    ${isLive ? `
    <div id="live-tradeable" class="live-tradeable">
      <div class="live-tradeable-head">Add a position — all tradeable derivatives</div>
      <div class="live-tradeable-body dim">loading…</div>
    </div>` : ''}`;
}

// All attached strategies across every live product, in one flat table so
// the user can see every open order + state without clicking through per-
// product modals. Rows include products where position=0 (waiting to
// rebuy) so nothing hides just because a cycle finished.
function renderOpenOrders(liveTenant) {
  if (!liveTenant) return '';
  const tenantBlock = currentStore[liveTenant] || {};
  const rows = [];
  for (const sym of Object.keys(tenantBlock)) {
    if (sym.startsWith('__')) continue;
    const block = tenantBlock[sym] || {};
    const cfg = block.config || {};
    const sleeves = Array.isArray(cfg.sleeves) ? cfg.sleeves : [];
    if (!sleeves.length) continue;
    const sleeveStates = (block.state && block.state.sleeves) || {};
    const snap = block.snapshot || {};
    const mark = Number(snap.last_mark) || 0;
    for (const s of sleeves) {
      const ss = sleeveStates[s.id] || {};
      const state = String(ss.state || 'ARMED_SELL');
      const halted = state === 'HALTED';
      // Choose target price based on state — SELL leg waits at sell_px,
      // BUY leg waits at buy_px. HALTED shows the last-known side target.
      const isBuyLeg = state === 'ARMED_BUY';
      const target = isBuyLeg ? Number(s.buy_px) : Number(s.sell_px);
      const sideLabel = halted ? 'HALTED' : (isBuyLeg ? 'BUY' : 'SELL');
      const dist = (mark > 0 && target > 0) ? (mark - target) : 0;
      const distText = mark > 0 && target > 0
        ? (Math.abs(dist) < 0.001 ? 'at target' : `${dist > 0 ? '+' : ''}$${fmtPrice(dist)}`)
        : '—';
      const haltReason = halted
        ? (block.state?.halt_reason || ss.halt_reason || 'halted')
        : '';
      rows.push({
        product: sym, name: s.name || s.id, qty: s.qty,
        side: sideLabel, target, mark, dist, distText,
        cycles: Number(ss.cycles) || 0, halted, haltReason,
        sleeveId: s.id,
      });
    }
  }
  if (!rows.length) return '';
  const rowsHtml = rows.map(r => {
    const sideCls = r.halted ? 'neg' : (r.side === 'BUY' ? 'pos' : 'neg');
    return `
      <tr class="pf-row" data-action="open-live-strategy"
          data-tenant="${escapeHtml(liveTenant)}" data-symbol="${escapeHtml(r.product)}"
          data-mark="${r.mark || 0}" data-pos-qty="0" data-side=""
          title="Open the strategy detail modal for ${escapeHtml(prettyProductName(r.product))}">
        <td><b>${escapeHtml(prettyProductName(r.product))}</b></td>
        <td class="mono">${escapeHtml(r.name)}</td>
        <td class="mono">${r.qty}</td>
        <td class="mono ${sideCls}">${r.side}${r.halted && r.haltReason ? `<div class="halt-why-inline">${escapeHtml(r.haltReason)}</div>` : ''}</td>
        <td class="mono">${r.target > 0 ? '$' + fmtPrice(r.target) : '—'}</td>
        <td class="mono dim">${r.mark > 0 ? '$' + fmtPrice(r.mark) : '—'}</td>
        <td class="mono">${r.distText}</td>
        <td class="mono">${r.cycles}</td>
      </tr>`;
  }).join('');
  return `
    <div class="open-orders">
      <div class="open-orders-head">All open orders (${rows.length})</div>
      <table class="open-orders-table">
        <thead><tr>
          <th>Product</th><th>Strategy</th><th>Qty</th><th>Side</th>
          <th>Target</th><th>Mark</th><th>Distance</th><th>Cycles</th>
        </tr></thead>
        <tbody>${rowsHtml}</tbody>
      </table>
    </div>`;
}

// Fetch scanner data and render spread recommendations for the sleeve
// editor. Non-blocking. `opts.onApply(spread)` fires when the user picks
// one — the caller derives buy_px/sell_px from the current anchor.
async function loadSpreadRecommendations(productId, modalEl, opts) {
  const container = modalEl.querySelector('#sleeve-spread-recs');
  const body = container && container.querySelector('.sleeve-spread-recs-body');
  if (!container || !body) return;

  // Trigger a scanner refresh and poll for this product's swing_candidates
  // to land. Bounded ~90s (30 polls × 3s). Used both by the "Scan now" button
  // and (if you want) auto-called on missing data.
  async function triggerScanAndPoll() {
    body.innerHTML = `<span class="dim">scanning ${escapeHtml(productId)}… this takes ~30–60s.</span>`;
    try {
      await fetch('/api/scanner/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ include: productId }),
      });
    } catch { /* ignore — user will see empty state and can retry */ }
    for (let i = 0; i < 30; i++) {
      await new Promise(r => setTimeout(r, 3000));
      try {
        const r2 = await fetch('/api/scanner');
        if (!r2.ok) continue;
        const d2 = await r2.json();
        const rows2 = Array.isArray(d2.top) ? d2.top : [];
        const row2 = rows2.find(x => x.product_id === productId);
        if (row2 && Array.isArray(row2.swing_candidates) && row2.swing_candidates.length && Number(row2.contract_size) > 0) {
          return loadSpreadRecommendations(productId, modalEl, opts);
        }
      } catch { /* keep polling */ }
    }
    body.innerHTML = `<span class="dim">scan did not return spreads for ${escapeHtml(productId)}. Try the Scanner tab or check bot logs.</span>`;
  }

  try {
    const resp = await fetch('/api/scanner');
    if (!resp.ok) return;
    const data = await resp.json();
    const rows = Array.isArray(data.top) ? data.top : [];
    const row = rows.find(r => r.product_id === productId);
    const candidates = row && Array.isArray(row.swing_candidates) ? row.swing_candidates : null;
    const csize = row ? Number(row.contract_size) || 0 : 0;
    if (!row || !candidates || !candidates.length || csize <= 0) {
      container.hidden = false;
      // No cached tiles — auto-kick the scan so every strategy modal ends up
      // with product-specific BEST tiles without the user having to click.
      // triggerScanAndPoll() renders its own "scanning..." state and re-calls
      // this function when data lands.
      triggerScanAndPoll();
      return;
    }
    // Two tiles only, as Adam requested: EXPERT (full stack scoring at real
    // taker fees) and FRONT-RUN (same stack at maker fees, i.e. requires
    // post_only + penny_inside to actually beat the other bots). Anything
    // else was clutter — scanner returns exactly these two now.
    //
    // RECONCILIATION INVARIANT (applies to EVERY product, current + future):
    //   $/day    = c.score * qty
    //   $/week   = $/day * 7
    //   $/month  = $/day * 30
    //   rt/day   = c.score / c.net_per_rt
    //   $/swing  = c.net_per_rt * qty
    // All five displayed numbers derive from exactly two scanner inputs
    // (c.score, c.net_per_rt). Do NOT read c.score_weekly or c.score_monthly
    // here — those are per-horizon raw rates that made $/week diverge from
    // $/day × 7 on hot 7d windows. Adam asked to fix that 12× before I
    // enforced the single-input structure. Per-horizon RT counts still exist
    // in the tooltip via session_weighted_weekly_rt / _monthly_rt for anyone
    // who wants the raw horizon data — they just don't drive the grid.
    const qtyLive = Math.max(1, parseInt(modalEl.querySelector('#sl-qty')?.value, 10) || 1);
    const ranked = [...candidates].sort((a, b) => (b.score || 0) - (a.score || 0));
    const rowsHtml = ranked.map((c) => {
      const spread = Number(c.spread) || 0;
      // dailyPerCt = weighted expected $/day (the scanner's headline number,
      // blends 24h/7d/30d per its horizon weights). ALL projections below
      // derive from this single rate so the tile reconciles: user sees
      // "0.68 rt/day × $18.60/swing" and expects day×7=week, day×30=month.
      // Prior version pulled week/month from the raw per-horizon RT rates,
      // which meant a hot 7d window made week read higher than day×7 —
      // Adam saw $12.22/day, $120.90/week and rightly said the numbers
      // don't add up. Fixed: one rate, multiply out.
      const dailyPerCt = Number(c.score) || 0;
      const netPerCt = Number(c.net_per_rt) || 0;
      const rtPerDay = (netPerCt > 0) ? (dailyPerCt / netPerCt) : 0;
      const weeklyPerCt = dailyPerCt * 7;
      const monthlyPerCt = dailyPerCt * 30;
      // Multiply to per-swing / total qty units so tile ↔ slider reconciles.
      const daily = dailyPerCt * qtyLive;
      const weekly = weeklyPerCt * qtyLive;
      const monthly = monthlyPerCt * qtyLive;
      const net = netPerCt * qtyLive;
      const feeUsed = Number(c.fee_rt_used) || 0;
      const feePerSwing = feeUsed * qtyLive;
      const robustness = Number(c.regime_robustness) || 0;
      const weeklyRtSess = Number(c.session_weighted_weekly_rt) || 0;
      const monthlyRtSess = Number(c.session_weighted_monthly_rt) || 0;
      const rtLabel = rtPerDay >= 1
        ? `${Math.round(rtPerDay)} roundtrips/day`
        : `${rtPerDay.toFixed(2)} roundtrips/day avg`;
      const kind = String(c.tile_label || c.tile_kind || 'EXPERT').toUpperCase();
      const isFrontrun = kind === 'FRONT-RUN' || kind === 'FRONTRUN';
      const badgeClass = isFrontrun ? 'spread-rec-badge frontrun' : 'spread-rec-badge';
      // Adam's rule (2026-07-13): always show contract_size × spread on the
      // tile so contract_size drift is instantly visible. The bot pulls the
      // real value from Coinbase every 6h + startup, but if a value ever
      // drifted (BIT was 0.04 vs 0.01, etc.) the tile's $ projections would
      // silently mis-scale by that factor. Making it visible closes the gap.
      const csizeDisplay = Number(cfg?.contract_size) || 0;
      const grossPerCt = csizeDisplay > 0 ? (spread * csizeDisplay) : 0;
      const csizeNote = csizeDisplay > 0
        ? ` · leverage = ${csizeDisplay.toLocaleString('en-US')} × spread = $${grossPerCt.toFixed(2)} gross/swing`
        : '';
      const subtitle = isFrontrun
        ? `<span class="dim">requires post-only + penny-inside (maker fees $${feePerSwing.toFixed(2)}/swing on ${qtyLive} ct)${csizeNote}</span>`
        : `<span class="dim">full expert stack at real fees ($${feePerSwing.toFixed(2)}/swing on ${qtyLive} ct)${csizeNote}</span>`;
      // Raw per-horizon signals — what the algo actually observed before
      // blending. Surfaced on the tile so the user can judge the pick's
      // confidence, not just the output. The main grid stays reconciled
      // ($/day × 7 = week, $/day × 30 = month projections), this row is
      // the "evidence" the pick was built on.
      const daily24hRt = Number(c.roundtrips) || 0;
      const robustPct = (robustness * 100).toFixed(0);
      // Color-code robustness: green if ≥70% (Turtle-safe), amber 40-70%, red <40%.
      const robustClass = robustness >= 0.7 ? 'pos' : (robustness >= 0.4 ? 'warn' : 'neg');
      const tooltip = `${kind} · robustness ${robustPct}% (min-chunk / avg-chunk over 30d) · `
        + `session-weighted RTs: ${weeklyRtSess.toFixed(1)}/wk · `
        + `${monthlyRtSess.toFixed(1)}/mo · fees $${feePerSwing.toFixed(2)}/swing at ${qtyLive} contracts · `
        + `per-contract net $${netPerCt.toFixed(2)}`;
      return `
        <button type="button" class="spread-rec-tile ${isFrontrun ? 'frontrun' : 'expert'}"
                data-spread="${spread}" data-net="${netPerCt}" title="${tooltip}">
          <div class="spread-rec-head">
            <span class="${badgeClass}">${kind}</span>
            <b style="font-size:1.1em;margin-left:6px">$${fmtNum(spread, 4)}</b>
            <span class="dim">· ${rtLabel} · $${fmtNum(net, 2)} net/swing (${qtyLive} ct)</span>
          </div>
          <div class="spread-rec-sub">${subtitle}</div>
          <div class="spread-rec-grid">
            <div><span class="dim">$/day (wtd)</span> <b class="pos">$${fmtNum(daily, 2)}</b></div>
            <div><span class="dim">$/week (7d)</span> <b class="pos">$${fmtNum(weekly, 2)}</b></div>
            <div><span class="dim">$/month (30d)</span> <b class="pos">$${fmtNum(monthly, 2)}</b></div>
          </div>
          <div class="spread-rec-signals">
            <span class="dim">observed:</span>
            <span title="Roundtrips found in last 24h of candles">${daily24hRt.toFixed(1)} RTs/24h</span>
            <span class="dim">·</span>
            <span title="Session-weighted roundtrips in last 7d (US session full weight, off-hours 0.5)">${weeklyRtSess.toFixed(1)} RTs/7d</span>
            <span class="dim">·</span>
            <span title="Session-weighted roundtrips in last 30d">${monthlyRtSess.toFixed(1)} RTs/30d</span>
            <span class="dim">·</span>
            <span class="${robustClass}" title="Turtle robustness: min-chunk RTs / avg-chunk RTs across 3 chunks of 30d. ≥70% is stable, <40% is a bumpy regime.">${robustPct}% robust</span>
          </div>
        </button>`;
    }).join('');
    container.hidden = false;
    body.innerHTML = rowsHtml;
    body.querySelectorAll('.spread-rec-tile').forEach(btn => {
      btn.onclick = () => {
        const spread = Number(btn.dataset.spread) || 0;
        const net = Number(btn.dataset.net) || 0;
        if (spread > 0 && opts && typeof opts.onApply === 'function') {
          opts.onApply(spread, net);
          // Highlight selection.
          body.querySelectorAll('.spread-rec-tile').forEach(b => b.classList.remove('active'));
          btn.classList.add('active');
        }
      };
    });
    // Auto-apply EXPERT tile on load — uniform across all sleeves (fresh and
    // existing). Adam: 'make this a uniform change across all sleeves and
    // future sleeves.' Reason: the tile numbers being different from the
    // sell/buy/slider values in the form was the recurring 'those numbers
    // don't add up' complaint. Now the tile ALWAYS wins on modal open, so
    // every editor immediately reflects the current expert-stack pick.
    // User can still click FRONT-RUN to switch, or manually edit sell/buy
    // afterward. The auto-apply is silent (no confirmation) since it's
    // effectively saying 'here's what the experts + data say to do right now'.
    const expertBtn = Array.from(body.querySelectorAll('.spread-rec-tile.expert'))[0]
      || body.querySelectorAll('.spread-rec-tile')[0];
    if (expertBtn && opts && typeof opts.onApply === 'function') {
      const spread = Number(expertBtn.dataset.spread) || 0;
      const net = Number(expertBtn.dataset.net) || 0;
      if (spread > 0) {
        opts.onApply(spread, net);
        expertBtn.classList.add('active');
      }
    }
  } catch (err) {
    body.innerHTML = `<span class="dim">could not load recommendations: ${escapeHtml(String(err.message || err))}</span>`;
    container.hidden = false;
  }
}

// Sum of realized P/L for a product across every tenant that runs a
// strategy on it — primary state.realized_pnl + every sleeve's realized_pnl.
// Used by the Live/Paper positions table so the user can see "this product
// has banked $X so far" without opening the drill-down.
function totalRealizedForProduct(productId, tenantFilter) {
  let total = 0;
  const store = currentStore || {};
  for (const tenant of Object.keys(store)) {
    if (tenantFilter && tenant !== tenantFilter) continue;
    const entry = store[tenant] && store[tenant][productId];
    if (!entry) continue;
    total += Number((entry.state && entry.state.realized_pnl) || 0);
    const sleeves = (entry.state && entry.state.sleeves) || {};
    for (const sid of Object.keys(sleeves)) {
      total += Number((sleeves[sid] && sleeves[sid].realized_pnl) || 0);
    }
  }
  return total;
}

// Cycles for a product across every tenant that runs a strategy on it —
// primary strategy + all sleeves. Useful signal: "the bot has round-tripped
// this thing N times somewhere" without pinning to a single tenant.
function totalCyclesForProduct(productId) {
  let total = 0;
  const store = currentStore || {};
  for (const tenant of Object.keys(store)) {
    const entry = store[tenant] && store[tenant][productId];
    if (!entry) continue;
    total += Number((entry.state && entry.state.cycles) || 0);
    const sleeves = (entry.state && entry.state.sleeves) || {};
    for (const sid of Object.keys(sleeves)) {
      total += Number((sleeves[sid] && sleeves[sid].cycles) || 0);
    }
  }
  return total;
}

// Fetch scanner-ranked derivatives once and render them into the Live tab.
// Runs after renderLivePortfolio has injected the placeholder <div>. Cached
// briefly so switching asset-class subtabs doesn't spam Coinbase.
let _tradeableCache = { data: null, ts: 0 };
async function renderLiveTradeable() {
  const container = document.getElementById('live-tradeable');
  if (!container) return;
  const body = container.querySelector('.live-tradeable-body');
  if (!body) return;
  try {
    let data = _tradeableCache.data;
    // Refresh every 60s.
    if (!data || (Date.now() - _tradeableCache.ts) > 60_000) {
      const resp = await fetch('/api/scanner');
      if (!resp.ok) { body.innerHTML = '<span class="dim">could not fetch derivatives</span>'; return; }
      data = await resp.json();
      _tradeableCache = { data, ts: Date.now() };
    }
    const top = Array.isArray(data.top) ? data.top : [];
    if (!top.length) { body.innerHTML = '<span class="dim">no ranking yet</span>'; return; }
    // Filter by active asset class subtab if set.
    const filtered = activeAssetClass
      ? top.filter(r => assetClassOf(r.product_id) === activeAssetClass)
      : top;
    if (!filtered.length) {
      body.innerHTML = `<span class="dim">no ${escapeHtml(activeAssetClass)} derivatives available right now</span>`;
      return;
    }
    body.innerHTML = `
      <table class="live-tradeable-table">
        <thead><tr>
          <th>Product</th><th>Price</th><th>24h High</th><th>24h Low</th><th>Range %</th><th>Cycles</th><th></th>
        </tr></thead>
        <tbody>
          ${filtered.map(r => {
            const cycles = totalCyclesForProduct(r.product_id);
            const cyclesCell = cycles > 0
              ? `<span title="Completed round-trips across all tenants (primary + sleeves)"><b>${cycles}</b></span>`
              : '<span class="dim">0</span>';
            return `
            <tr class="live-tradeable-row" data-product='${encodeURIComponent(JSON.stringify(r))}'>
              <td><b>${escapeHtml(prettyProductName(r.product_id))}</b></td>
              <td class="mono">$${fmtNum(r.price, 4)}</td>
              <td class="mono pos">$${fmtNum(r.high_24h, 4)}</td>
              <td class="mono neg">$${fmtNum(r.low_24h, 4)}</td>
              <td class="mono"><b>${fmtNum(r.vol_pct, 2)}%</b></td>
              <td class="mono">${cyclesCell}</td>
              <td><button class="small primary">Buy / Short →</button></td>
            </tr>
            `;
          }).join('')}
        </tbody>
      </table>
      <div class="pf-hint dim">Click a row to open the chart + place a Buy (long) or Short order</div>`;
    // Wire row clicks — opens scanner-detail modal (has LONG/SHORT selector).
    body.querySelectorAll('tr.live-tradeable-row').forEach(tr => {
      tr.onclick = () => {
        try {
          const row = JSON.parse(decodeURIComponent(tr.dataset.product));
          openScannerDetail(row);
        } catch (e) { /* ignore */ }
      };
    });
  } catch (err) {
    body.innerHTML = `<span class="dim">scanner error: ${escapeHtml(String(err.message || err))}</span>`;
  }
}

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

    <div class="card-body card-body-split">
      <div class="card-main">
        ${renderTargetsRow(c, snap)}
        ${renderSleevesSection(tenant, symbol, c, s, snap)}
        ${renderLotsTable(snap, c, tenant, symbol, s)}
        ${renderRiskStrip(snap)}
        ${renderMicrostructurePanel(snap)}
      </div>
      <aside class="card-sidebar">
        ${renderTradeSidebar(tenant, symbol, s, c, snap)}
        ${renderContractInfo(symbol, c, snap)}
        ${renderPositionBar(s, c, snap)}
      </aside>
    </div>

    <div class="card-actions">
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
  // Fixed limit — also compute the ACTUAL net per cycle after fees so the
  // user can see when a sleeve labeled "$10 net swing" is actually only
  // netting $6 (mismatch between preset label and configured prices).
  const sellPx = Number(staticCfg?.sellPx) || 0;
  const buyPx = Number(staticCfg?.buyPx) || 0;
  const gross = (sellPx - buyPx) * contractSize * qty;
  const netPerCycle = gross - totalFees;
  const netCls = netPerCycle >= 0 ? 'pos' : 'neg';
  return `
    <div class="params-block">
      <div class="params-mode">Fixed limit</div>
      <div class="params-line"><span class="params-label">Sell</span>$${fmtPrice(staticCfg.sellPx)}</div>
      <div class="params-line"><span class="params-label">Buy</span>$${fmtPrice(staticCfg.buyPx)}</div>
      ${qty > 0 && feeRt > 0 ? `<div class="params-line params-locked"><span class="params-label">Net / cycle</span><b class="${netCls}">${netPerCycle >= 0 ? '+' : ''}${fmtMoney(netPerCycle)}</b></div>` : ''}
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
    const sleeveAvgEntry = sleeveUnits.length > 0
      ? sleeveUnits.reduce((n, u) => n + u.entry_price, 0) / sleeveUnits.length
      : 0;
    // Cost-basis hierarchy (most accurate → least). Adam saw $0 unrealized
    // on a sleeve that clearly held ITM contracts and rightly asked to
    // 'make sure the numbers reconcile'. Fallback chain: sleeve's own_avg
    // (bought via its own cycle) → sell_entry_avg (captured at arm time via
    // FIFO) → sleeveAvgEntry (lot allocation) → position avg (inherited
    // contracts pre-any-sleeve-cycle) → buy_px (last resort; the FUTURE
    // rebuy target, not real cost).
    const positionAvg = Number(snapshot?.position_avg_entry) || 0;
    let ownEntry = 0;
    let ownEntrySource = 'none';
    if (ss.own_avg_entry != null && Number(ss.own_avg_entry) > 0) {
      ownEntry = Number(ss.own_avg_entry); ownEntrySource = 'own_avg_entry';
    } else if (ss.sell_entry_avg != null && Number(ss.sell_entry_avg) > 0) {
      ownEntry = Number(ss.sell_entry_avg); ownEntrySource = 'sell_entry_avg';
    } else if (sleeveAvgEntry > 0) {
      ownEntry = sleeveAvgEntry; ownEntrySource = 'lot_alloc';
    } else if (positionAvg > 0) {
      ownEntry = positionAvg; ownEntrySource = 'position_avg';
    } else if (Number(s.buy_px) > 0) {
      ownEntry = Number(s.buy_px); ownEntrySource = 'buy_px_target';
    }
    // Belt-and-suspenders: unrealized MUST be $0 when the sleeve isn't holding
    // contracts. Adam saw +$2.60 unrealized on a PT sleeve in ARMED_BUY (0
    // contracts held, position_qty=0). Root cause: the fallback chain
    // populated ownEntry from s.buy_px (the FUTURE rebuy target — not real
    // cost basis) and the state gate somehow passed. Tighter check now:
    //   - state must be ARMED_SELL (holds contracts)
    //   - ownEntrySource must be a REAL cost basis (not buy_px_target, not none)
    //   - snapshot.position_qty must be > 0 (physically holds contracts)
    //   - cycles > 0 (sleeve has bought contracts via its own state machine).
    //     Adam 2026-07-13: "there is no unrealized in my silver sleeve." A
    //     freshly-attached sleeve inherits its qty from the parent position;
    //     the pre-attach paper move belongs to the position, not the sleeve,
    //     and shows on the position row instead — never double-counted here.
    // Any one of these failing → unrealized is $0.
    const positionQty = Number(snapshot?.position_qty) || 0;
    const isRealCostBasis = ownEntrySource !== 'none' && ownEntrySource !== 'buy_px_target';
    const unreal = (
      ownEntry > 0 && sleeveQty > 0
      && sState === 'ARMED_SELL'
      && isRealCostBasis
      && positionQty > 0
      && cycles > 0
    ) ? (mark - ownEntry) * contractSize * sleeveQty : 0;
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

// Inline trade sidebar — Buy/Sell tab strip, order-type dropdown, quantity
// input, optional limit price, notional preview, single confirm button.
// Everything visible without opening a modal. Coinbase Advanced style.
function renderTradeSidebar(tenant, symbol, state, config, snap) {
  const mark = Number(snap?.last_mark) || 0;
  const bid = Number(snap?.best_bid);
  const ask = Number(snap?.best_ask);
  const pos = Number(snap?.position_qty) || 0;
  const escSym = escapeHtml(symbol);
  const escTen = escapeHtml(tenant);
  const uid = `${tenant}-${symbol}`.replace(/[^a-z0-9]/gi, '-');
  const defaultPx = bid && ask ? ((bid + ask) / 2).toFixed(pricePrecisionFor(mark, config)) : (mark || 0).toFixed(pricePrecisionFor(mark, config));
  return `
    <div class="trade-sidebar" data-tenant="${escTen}" data-symbol="${escSym}" data-uid="${uid}">
      <div class="trade-sidebar-header">
        <div class="trade-sidebar-title">Trade</div>
        <div class="trade-sidebar-price">
          <span class="trade-sidebar-mark">$${fmtPrice(mark, config)}</span>
          ${Number.isFinite(bid) && Number.isFinite(ask) ? `<span class="trade-sidebar-book">bid $${fmtPrice(bid, config)} · ask $${fmtPrice(ask, config)}</span>` : ''}
        </div>
      </div>
      <div class="ts-side-tabs" role="tablist">
        <button class="ts-side-tab ts-buy active" data-side="BUY">Buy / Long</button>
        <button class="ts-side-tab ts-sell" data-side="SELL">Sell / Short</button>
      </div>
      <div class="ts-form">
        <label class="ts-field">
          <span class="ts-field-label">Order type</span>
          <select class="ts-order-type">
            <option value="market" selected>Market</option>
            <option value="limit">Limit</option>
          </select>
        </label>
        <label class="ts-field ts-limit-field" hidden>
          <span class="ts-field-label">Limit price</span>
          <input type="number" class="ts-limit-price" step="${(config?.tick_size || 0.005)}" value="${defaultPx}">
        </label>
        <label class="ts-field">
          <span class="ts-field-label">Contracts</span>
          <input type="number" class="ts-qty" min="1" step="1" value="1">
        </label>
        <div class="ts-preview">
          <div class="ts-preview-line"><span>Notional</span><span class="ts-notional mono">—</span></div>
          <div class="ts-preview-line"><span>Est. fee</span><span class="ts-fee mono">—</span></div>
          <div class="ts-preview-line"><span>Position after</span><span class="ts-after mono">—</span></div>
        </div>
        <div class="ts-warn" hidden></div>
        <button class="ts-submit primary" data-action="ts-submit" data-tenant="${escTen}" data-symbol="${escSym}">Buy 1 contract</button>
      </div>
      <div class="trade-sidebar-hint">
        ${pos === 0
          ? 'You hold <b>0</b> contracts. Buy opens a long; Sell opens a short.'
          : pos > 0
            ? `You hold <b>${pos}</b> long. Sell more than that = short.`
            : `You are <b>SHORT ${Math.abs(pos)}</b>. Buy to cover.`}
      </div>
    </div>`;
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

let scannerTriggerLastAt = 0;

async function triggerScannerRefresh() {
  try {
    await fetch('/api/scanner/refresh', { method: 'POST', credentials: 'same-origin' });
    scannerTriggerLastAt = Date.now();
  } catch (err) {
    console.error('scanner trigger failed', err);
  }
}

async function refreshScanner() {
  // On-demand only: trigger a scan when the tab opens and every ~60s while
  // it stays open. Saves the Coinbase API cost of ~30 candle fetches per
  // scan when nobody's looking at it. Polling every ~5s just re-reads the
  // Redis cache; the trigger is what tells the paper worker to run one.
  if (Date.now() - scannerTriggerLastAt > 60_000) {
    triggerScannerRefresh();
  }
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
      updated.innerHTML = 'no ranking yet — scan requested, results arrive shortly. <button id="scanner-force-refresh" class="small ghost">Refresh now</button>';
      const forceBtn = document.getElementById('scanner-force-refresh');
      if (forceBtn) forceBtn.onclick = () => { triggerScannerRefresh(); refreshScanner(); };
      return;
    }
    if (data.generated_at) {
      const dt = new Date(data.generated_at * 1000);
      updated.innerHTML = `updated ${dt.toLocaleTimeString()} <button id="scanner-force-refresh" class="small ghost">Refresh</button>`;
      const forceBtn = document.getElementById('scanner-force-refresh');
      if (forceBtn) forceBtn.onclick = () => { triggerScannerRefresh(); };
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
      const bestSpread = Number(row.best_spread) || 0;
      const bestRoundtrips = Number(row.best_roundtrips) || 0;
      const bestScore = Number(row.best_score) || 0;
      const netPerRt = Number(row.best_net_per_rt) || 0;
      const spreadCell = bestSpread > 0
        ? `<span title="Best spread × contract_size − fees = $${fmtNum(netPerRt, 2)} net per roundtrip">$${fmtNum(bestSpread, 4)}</span>`
        : '<span class="dim">—</span>';
      const rtCell = bestRoundtrips > 0
        ? `<b>${bestRoundtrips}</b>`
        : '<span class="dim">0</span>';
      const scoreCell = bestScore > 0
        ? `<b class="pos">$${fmtNum(bestScore, 2)}</b>`
        : '<span class="dim">—</span>';
      const weeklyScore = Number(row.weekly_score) || 0;
      const monthlyScore = Number(row.monthly_score) || 0;
      const weeklyRt = Number(row.weekly_roundtrips) || 0;
      const monthlyRt = Number(row.monthly_roundtrips) || 0;
      const weeklyCell = weeklyScore > 0
        ? `<b class="pos" title="${weeklyRt} roundtrips over the last 7 days (1H candles) at best spread">$${fmtNum(weeklyScore, 2)}</b>`
        : '<span class="dim">—</span>';
      const monthlyCell = monthlyScore > 0
        ? `<b class="pos" title="${monthlyRt} roundtrips over the last 30 days (1H candles) at best spread">$${fmtNum(monthlyScore, 2)}</b>`
        : '<span class="dim">—</span>';
      const defaultRt = Number(row.default_roundtrips) || 0;
      const defaultSpread = Number(row.default_spread) || 0;
      const defaultRtCell = defaultRt > 0
        ? `<span title="At spread $${fmtNum(defaultSpread, 4)} (your $10/contract preset)"><b>${defaultRt}</b></span>`
        : '<span class="dim" title="No roundtrips in last 24h at your default $10/contract spread">0</span>';
      const cycles = totalCyclesForProduct(row.product_id);
      const cyclesCell = cycles > 0
        ? `<span title="Completed round-trips across all tenants (primary + sleeves)"><b>${cycles}</b></span>`
        : '<span class="dim">0</span>';
      tr.innerHTML = `
        <td>${i + 1}</td>
        <td class="mono">${escapeHtml(row.product_id)}</td>
        <td class="mono">$${fmtNum(row.price, 4)}</td>
        <td class="mono">${fmtNum(row.vol_pct, 2)}%</td>
        <td class="mono">${spreadCell}</td>
        <td class="mono">${rtCell}</td>
        <td class="mono">${defaultRtCell}</td>
        <td class="mono">${scoreCell}</td>
        <td class="mono">${weeklyCell}</td>
        <td class="mono">${monthlyCell}</td>
        <td class="mono">${cyclesCell}</td>
        <td class="mono dim">${row.volume_24h ? fmtMoney(row.volume_24h) : '—'}</td>
        <td><button class="small primary scanner-buy-btn">Buy / Short →</button></td>
      `;
      tr.onclick = (e) => {
        // Click anywhere on the row (including the button) → open detail
        // modal which has BUY (long) / SHORT (short) side selector.
        if (e.target.closest('button')) e.stopPropagation();
        openScannerDetail(row);
      };
      const buyBtn = tr.querySelector('.scanner-buy-btn');
      if (buyBtn) buyBtn.onclick = (e) => { e.stopPropagation(); openScannerDetail(row); };
      tbody.appendChild(tr);
    });
  } catch (err) {
    console.error('scanner refresh failed', err);
  }
}

// Twitter shadow signal viewer. Reads /api/twitter-signals which returns
// {entries: [...], summary: {total_signals, shadow_mode, by_horizon}}.
// Renders a header with total + per-horizon accuracy tally, then a table
// of every "would-have-decided" entry. Nothing here triggers or references
// trade execution — this is purely a validation harness.
async function refreshSignals() {
  const summaryEl = document.getElementById('signals-summary');
  const tbody = document.querySelector('#signals-table tbody');
  if (!summaryEl || !tbody) return;
  try {
    const r = await fetch('/api/twitter-signals');
    if (!r.ok) {
      summaryEl.textContent = 'signals API unavailable';
      tbody.innerHTML = '';
      return;
    }
    const data = await r.json();
    const entries = Array.isArray(data.entries) ? data.entries : [];
    const summary = data.summary || {};
    const tally = summary.by_horizon || {};
    const horizonCell = (h) => {
      const t = tally[h] || {};
      const c = t.correct || 0, w = t.wrong || 0, f = t.flat || 0, u = t.unknown || 0;
      const n = c + w + f;
      const acc = n > 0 ? Math.round(100 * c / n) : null;
      return `<span class="signals-tally"><b>${h}</b>: `
        + `<span class="pos">${c}✓</span> `
        + `<span class="neg">${w}✗</span> `
        + `<span class="dim">${f} flat · ${u} unk</span>`
        + (acc !== null ? ` · <b>${acc}% accurate</b>` : '')
        + `</span>`;
    };
    summaryEl.innerHTML = `
      <div class="signals-total">${entries.length} shadow signal${entries.length === 1 ? '' : 's'} logged</div>
      <div class="signals-tallies">
        ${horizonCell('1h')} ${horizonCell('6h')} ${horizonCell('24h')}
      </div>
    `;
    tbody.innerHTML = '';
    if (!entries.length) {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td colspan="10" class="dim">no signals yet — the scanner polls every 5 min; check back after the next tick</td>`;
      tbody.appendChild(tr);
      return;
    }
    for (const e of entries) {
      const tr = document.createElement('tr');
      const ts = new Date((Number(e.ts) || 0) * 1000).toLocaleString();
      const src = e.source || '';
      const family = e.family || '';
      const wa = e.would_action || '';
      const waClass = wa === 'EXIT_HINT' ? 'neg'
                    : wa === 'BLOCK_ARM' ? 'warn'
                    : wa === 'ALERT_ARM' ? 'pos' : 'dim';
      const kw = Array.isArray(e.keywords_matched) ? e.keywords_matched.join(', ') : '';
      const prods = Array.isArray(e.products_affected) ? e.products_affected : [];
      const prodDisplay = prods.length ? `${prods.length} product${prods.length === 1 ? '' : 's'}` : '—';
      const outs = e.outcomes || {};
      const outCell = (h) => {
        const o = outs[h];
        if (!o) return '<span class="dim">pending</span>';
        const v = o.verdict || 'unknown';
        const p = o.pct_avg;
        const pctStr = (p !== null && p !== undefined) ? `${p >= 0 ? '+' : ''}${Number(p).toFixed(2)}%` : '—';
        const cls = v === 'correct' ? 'pos' : v === 'wrong' ? 'neg' : 'dim';
        const label = v === 'correct' ? '✓' : v === 'wrong' ? '✗' : v === 'flat' ? '·' : '?';
        return `<span class="${cls}">${label} ${pctStr}</span>`;
      };
      const tweetLink = e.tweet_url
        ? `<a href="${escapeHtml(e.tweet_url)}" target="_blank" rel="noopener" class="dim">view</a>`
        : '<span class="dim">—</span>';
      tr.innerHTML = `
        <td class="dim" title="${escapeHtml(new Date((Number(e.ts) || 0) * 1000).toISOString())}">${escapeHtml(ts)}</td>
        <td>${escapeHtml(src)}</td>
        <td>${escapeHtml(family)}</td>
        <td><span class="signal-would ${waClass}">${escapeHtml(wa)}</span></td>
        <td class="dim" title="${escapeHtml(kw)}">${escapeHtml(kw.slice(0, 40))}${kw.length > 40 ? '…' : ''}</td>
        <td class="dim" title="${escapeHtml(prods.join(', '))}">${prodDisplay}</td>
        <td>${outCell('1h')}</td>
        <td>${outCell('6h')}</td>
        <td>${outCell('24h')}</td>
        <td>${tweetLink}</td>
      `;
      tbody.appendChild(tr);
    }
  } catch (err) {
    console.error('signals refresh failed', err);
    if (summaryEl) summaryEl.textContent = `signals refresh failed: ${err.message}`;
  }
}

async function refreshOnce() {
  const [status, trades] = await Promise.all([
    fetchJson('/api/status'),
    fetchJson('/api/trades?n=60'),
  ]);
  if (status._unauthorized || trades._unauthorized) { showLogin(); return; }
  currentStore = status.store || {};
  refreshStopLossToggleUi();
  renderBanners(currentStore);
  renderModeTabs(currentStore);
  renderAssetTabs(currentStore);

  const scannerSection = document.getElementById('scanner-section');
  const signalsSection = document.getElementById('signals-section');
  const showScanner = activeMode === 'scanner';
  const showSignals = activeMode === 'signals';
  if (scannerSection) scannerSection.hidden = !showScanner;
  if (signalsSection) signalsSection.hidden = !showSignals;
  cardsEl.hidden = showScanner || showSignals;
  document.getElementById('asset-tabs').hidden = showScanner || showSignals;

  if (showScanner) {
    refreshScanner();
    cardsEl.innerHTML = '';
    tradeLogEl.innerHTML = '';
    lastUpdated.textContent = `updated ${new Date().toLocaleTimeString()}`;
    return;
  }
  if (showSignals) {
    refreshSignals();
    cardsEl.innerHTML = '';
    tradeLogEl.innerHTML = '';
    lastUpdated.textContent = `updated ${new Date().toLocaleTimeString()}`;
    return;
  }

  cardsEl.innerHTML = '';
  const tenants = Object.keys(currentStore).sort();
  let anyRendered = false;

  // Lab tab: add a side-by-side comparison panel at the top showing
  // Models A-E performance at a glance, before the individual cards.
  if (activeMode === 'lab') {
    const compHtml = renderLabComparison();
    if (compHtml) {
      const compEl = document.createElement('section');
      compEl.className = 'lab-comparison';
      compEl.innerHTML = compHtml;
      cardsEl.appendChild(compEl);
    }
  }

  // Live tab: render the Coinbase-style portfolio overview (Cash / Derivatives
  // / Crypto sections) at the top, above any individual strategy cards. Reads
  // the __portfolio__ snapshot the paper worker writes to store on live sync.
  if (activeMode === 'live') {
    const pfHtml = renderLivePortfolio();
    if (pfHtml) {
      const pfEl = document.createElement('section');
      pfEl.className = 'live-portfolio';
      pfEl.innerHTML = pfHtml;
      cardsEl.appendChild(pfEl);
      // Fire-and-forget: fetch scanner-ranked derivatives to fill the "Add
      // a position" section below the portfolio table.
      renderLiveTradeable();
    }
  }
  // Paper tab: same layout as Live — positions table + open orders. Reuses
  // the same renderer with the paper tenant so the UX is identical (Adam
  // was constantly switching modes and losing track of where he was
  // because paper looked totally different).
  if (activeMode === 'paper') {
    const paperTenant = Object.keys(currentStore || {}).find(t => modeOfTenant(t) === 'paper');
    if (paperTenant) {
      const pfHtml = renderLivePortfolio(paperTenant, 'paper');
      if (pfHtml) {
        const pfEl = document.createElement('section');
        pfEl.className = 'live-portfolio';
        pfEl.innerHTML = pfHtml;
        cardsEl.appendChild(pfEl);
      }
    }
  }

  for (const tenant of tenants) {
    const m = modeOfTenant(tenant);
    if (activeMode && activeMode !== 'scanner' && m && m !== activeMode) continue;
    const symbols = Object.keys(currentStore[tenant] || {}).sort();
    for (const symbol of symbols) {
      if (symbol === '__account_kill_switch__') continue;
      if (symbol === '__portfolio__') continue;
      if (symbol === '__tuned_params__') continue;
      if (symbol === '__stop_loss_disabled__') continue;
      if (activeAssetClass && assetClassOf(symbol) !== activeAssetClass) continue;
      // Live + Paper tabs: drop the per-symbol cards from the flat render.
      // The portfolio table is the entry point; drilling into a specific
      // product opens the scanner-detail modal (chart + trade + add strategy).
      // No noise from N idle cards below the table.
      if (m === 'live' && activeMode === 'live') continue;
      if (m === 'paper' && activeMode === 'paper') continue;
      cardsEl.appendChild(renderCard(tenant, symbol, currentStore[tenant][symbol]));
      anyRendered = true;
    }
  }
  // Live/Paper special case: if the portfolio table rendered, count that
  // as "rendered" so we don't show the 'no state yet' empty-state.
  if ((activeMode === 'live' || activeMode === 'paper')
      && cardsEl.querySelector('.live-portfolio')) {
    anyRendered = true;
  }
  if (!anyRendered) {
    cardsEl.innerHTML = '<div class="field-value dim">no state yet — has the bot run?</div>';
  }

  tradeLogEl.innerHTML = '';
  for (const ev of (trades.events || []).slice().reverse()) {
    tradeLogEl.appendChild(renderTradeEvent(ev));
  }

  lastUpdated.textContent = `updated ${new Date().toLocaleTimeString()}`;

  // If the scanner detail modal is open, refresh its price bar + sleeves
  // table from the fresh store so mark/unrealized don't lag while the user
  // is watching a drill-down. Chart is not re-drawn (would flap).
  refreshScannerDetailLive();
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
  const isLive = String(tenant || '').endsWith('-live');
  if (mode === 'activate') {
    if (isLive) {
      killModalTitle.textContent = '⛔ KILL ALL LIVE TRADING?';
      killModalBody.innerHTML = '<b>REAL MONEY.</b> This immediately freezes every Live sleeve across every product. New buy/sell legs are BLOCKED until you resume. <b>Existing open orders on Coinbase are NOT cancelled</b> and <b>existing positions are NOT closed</b> — cancel/close those manually on Coinbase if the panic reason requires it.';
      killConfirm.textContent = 'KILL LIVE NOW';
    } else {
      killModalTitle.textContent = 'pause all trading?';
      killModalBody.innerHTML = 'This freezes arming across every instrument for this tenant. Existing positions are not closed; existing orders on the exchange are NOT cancelled. Only new legs are blocked until you resume.';
      killConfirm.textContent = 'CONFIRM PAUSE';
    }
    killReason.hidden = false;
  } else {
    killModalTitle.textContent = isLive ? 'resume LIVE trading?' : 'resume trading?';
    killModalBody.innerHTML = `Kill switch will be cleared. Any HALTED instrument is separately still halted — clearing kill does NOT auto-arm those.`;
    killConfirm.textContent = isLive ? 'RESUME LIVE' : 'CONFIRM RESUME';
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
    const noteRow = r.note ? `
      <tr class="leaderboard-note-row">
        <td></td>
        <td colspan="7" class="dim leaderboard-note">${escapeHtml(r.note)}</td>
      </tr>
    ` : '';
    return `
      <tr class="${winnerCls}">
        <td class="rank-cell">${medal}</td>
        <td class="strategy-cell">${escapeHtml(r.strategy)}${i === 0 && !r.error ? ' <span class="best-tag">BEST</span>' : ''}</td>
        ${err}
      </tr>
      ${noteRow}
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

function openSleeveEditor(tenant, symbol, sleeveId, lotContext = null, portfolioContext = null) {
  const block = currentStore[tenant]?.[symbol] || {};
  const cfg = block.config || {};
  const snap = block.snapshot || {};
  // Live-tenant fallback for mark / avg_entry / qty. Precedence:
  //   1. portfolioContext — data captured at click time from the row we came
  //      from. Most reliable: doesn't depend on the __portfolio__ snapshot
  //      being current in the store.
  //   2. __portfolio__ snapshot in the store (updated every 2 min by sync).
  //   3. Same symbol tracked on another tenant (paper / lab) — shares mark.
  let liveMark = 0, liveAvg = 0, liveQty = 0;
  if (portfolioContext) {
    liveMark = Number(portfolioContext.mark) || 0;
    liveAvg  = Number(portfolioContext.avg) || 0;
    liveQty  = Number(portfolioContext.qty) || 0;
  }
  if (isLiveTenant(tenant) && (!liveMark || !liveAvg || !liveQty)) {
    const pfSnap = currentStore[tenant]?.['__portfolio__']?.config;
    const posRow = (pfSnap?.derivatives || []).find(d => d.product_id === symbol);
    if (posRow) {
      liveMark = liveMark || Number(posRow.mark) || 0;
      liveAvg  = liveAvg  || Number(posRow.avg_entry) || 0;
      liveQty  = liveQty  || Number(posRow.qty) || 0;
    }
  }
  // Cross-tenant mark fallback: same product tracked on paper/lab has a live
  // feed, so its snapshot has last_mark even if this tenant's doesn't.
  if (!liveMark) {
    for (const t of Object.keys(currentStore || {})) {
      if (t === tenant) continue;
      const s = currentStore[t]?.[symbol]?.snapshot;
      if (s && Number(s.last_mark) > 0) { liveMark = Number(s.last_mark); break; }
    }
  }
  const mark = Number(snap.last_mark) || liveMark || 0;
  const posAvgEntry = Number(snap.position_avg_entry) || liveAvg || 0;

  // Expert params per THIS product: ATR from real candles + asset-class
  // multipliers from published trader literature (Layer 1), overridden by
  // any Layer 2 grid-search tuning that ran against this product's own
  // recent history. Silver / oil / BTC all end up with different numbers.
  let expertATR = 0;
  let expertParams = null;
  let tunedParams = null;
  const liveTenantKey = Object.keys(currentStore || {}).find(t => modeOfTenant(t) === 'live');
  if (liveTenantKey) {
    const pfSnap = currentStore[liveTenantKey]?.['__portfolio__']?.config;
    const posRow = (pfSnap?.derivatives || []).find(d => d.product_id === symbol);
    if (posRow) {
      expertATR = Number(posRow.atr) || 0;
      expertParams = posRow.expert_params || null;
    }
    const tuned = currentStore[liveTenantKey]?.['__tuned_params__']?.config;
    if (tuned && tuned[symbol]) tunedParams = tuned[symbol];
  }
  // Merge: Layer 2 (tuned) trail_x_atr overrides Layer 1's multiplier.
  const effectiveMultipliers = expertParams ? { ...expertParams.multipliers } : null;
  if (effectiveMultipliers && tunedParams?.trail_x_atr) {
    effectiveMultipliers.trail_x_atr = tunedParams.trail_x_atr;
  }
  // Recompute dollar values from the (possibly-tuned) multipliers.
  const expertDollars = (effectiveMultipliers && expertATR > 0) ? {
    trail_distance: +(expertATR * effectiveMultipliers.trail_x_atr).toFixed(4),
    stop_loss_distance: +(expertATR * effectiveMultipliers.stop_x_atr).toFixed(4),
    activation_offset: +(expertATR * effectiveMultipliers.activation_offset_x_atr).toFixed(4),
    ratchet_distance: +(expertATR * effectiveMultipliers.ratchet_x_atr).toFixed(4),
    ratchet_activation: +(expertATR * effectiveMultipliers.ratchet_activation_x_atr).toFixed(4),
    reanchor_threshold: +(expertATR * effectiveMultipliers.reanchor_x_atr).toFixed(4),
    // Le Beau entry-filter default — 0.5×ATR bounce required to confirm
    // the fall is over before rebuying (Livermore's pivot rule).
    buy_trail_distance: +(expertATR * (effectiveMultipliers.buy_trail_x_atr || 0.5)).toFixed(4),
  } : null;
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
  // Anchor choices: up to three buttons, showing whichever are distinct enough
  // to be meaningful. In edit mode Adam wants to see BOTH the current market
  // AND his contract avg (so he can retarget the sleeve around whichever one
  // makes sense right now). If the sleeve has a stamped entry_mark from when
  // the strategy was originally attached, expose that too — it's the natural
  // "keep original targets relative to strategy entry" pick.
  const existingSleeveForAnchor = sleeveId ? (cfg.sleeves || []).find(s => s.id === sleeveId) : null;
  const strategyEntryMark = Number(existingSleeveForAnchor?.entry_mark) || 0;
  const strategyEntryTs = Number(existingSleeveForAnchor?.entry_ts) || 0;
  const anchorChoices = [];
  const pushChoice = (label, value) => {
    if (!(value > 0)) return;
    if (anchorChoices.some(c => Math.abs(c.value - value) < 0.001)) return;  // dedupe near-identical
    anchorChoices.push({ label, value });
  };
  if (lotContext) {
    pushChoice('Purchased price', Number(lotContext.entry_price));
    pushChoice('Current market', mark);
  } else if (existingSleeveForAnchor) {
    pushChoice('Current market', mark);
    pushChoice('Your contract avg', posAvgEntry);
    pushChoice('Strategy entry', strategyEntryMark);
  } else {
    pushChoice('Current market', mark);
    pushChoice('Your contract avg', posAvgEntry);
  }
  // Third option — user-entered target price. Rendered with an inline input
  // so the user can pick the exact price they want the strategy anchored on.
  // Initial value = current mark (a sensible default that's guaranteed > 0).
  anchorChoices.push({ label: 'Custom target', value: mark, custom: true });
  // Default anchor priority:
  //   1. Lot context: purchased price (opened from a specific lot).
  //   2. Existing sleeve + underwater: Your Contract Avg. Adam's ask —
  //      "I try to use my contract average because I don't want to make
  //      any sales until I am above water." If posAvgEntry is above mark,
  //      picking the stale sleeve buy_px lands the auto-applied EXPERT
  //      tile's sell target BELOW cost. Default to cost avg so the derived
  //      sell stays above water.
  //   3. Existing sleeve + above water: sleeve's original buy_px (stable
  //      across re-opens once a cycle is profitable).
  //   4. New / no context: Current market.
  let anchor;
  let anchorLabel;
  if (lotContext) {
    anchor = Number(lotContext.entry_price) || mark;
    anchorLabel = 'Purchased price';
  } else if (existingSleeveForAnchor && posAvgEntry > 0 && posAvgEntry > mark) {
    anchor = posAvgEntry;
    anchorLabel = 'Your contract avg';
  } else if (existingSleeveForAnchor) {
    anchor = Number(existingSleeveForAnchor.buy_px) || mark;
    anchorLabel = "Strategy's original entry";
  } else {
    anchor = mark;
    anchorLabel = 'Current market';
  }
  // NO silver defaults. If the bot hasn't synced Coinbase specs for this
  // product yet, contract_size and fee_per_contract_roundtrip are 0 — we
  // MUST refuse to compute spreads against silver-scale fallbacks, because
  // the user's target ($/net swing) would silently be honored at wrong
  // scale and the sleeve would save prices producing a fraction of intent
  // (this exact bug hit NER + NGS: $2 preset → $0.60/$0.90 actual net).
  const contractSize = Number(cfg.contract_size);
  const feeRt = Number(cfg.fee_per_contract_roundtrip);
  const specMissing = !(contractSize > 0) || !(feeRt > 0);
  // Precision for price inputs — tick-driven so low-priced perps (XLP at
  // $0.186 with $0.0001 tick) don't collapse Sell/Buy-back to the same
  // 3-decimal number and swallow the actual spread.
  const pricePrec = pricePrecisionFor(anchor, cfg);
  const tickStep = Number(cfg?.tick_size) > 0 ? Number(cfg.tick_size) : 0.005;
  const sleeves = Array.isArray(cfg.sleeves) ? [...cfg.sleeves] : [];
  const existing = sleeveId ? sleeves.find(s => s.id === sleeveId) : null;

  // Capacity: how many contracts can this sleeve use?
  // Live-tenant: the regular snap.position_qty is 0 (read-only mirror, no
  // strategy engine writing snapshots). Trust liveQty from the portfolio
  // row, which came directly from Coinbase list_futures_positions.
  // Take MAX so a stale 0 in either source doesn't override the good value.
  const rawPosQty = Number(snap.position_qty) || 0;
  const pos = Math.max(rawPosQty, liveQty, 0);
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
  // Live tenant: the position count may be stale in the store (portfolio
  // snap lag). Skip the frontend gate and let the server-side check be the
  // authority — it reads __portfolio__ directly and will reject if there's
  // really no capacity. Prevents false "NO FREE CAPACITY" on Live drills
  // where the qty just hasn't propagated yet.
  const skipCapacityGate = isLiveTenant(tenant);
  const freeCapacity = skipCapacityGate
    ? Math.max(1, pos - core - primary - otherSleeveQty)  // never block on Live
    : Math.max(0, pos - core - primary - otherSleeveQty);
  // When editing an existing sleeve, its own qty is already counted against
  // freeCapacity via the filter above (otherSleeves), so max = freeCapacity + its own qty.
  const maxQty = freeCapacity + (existing ? Number(existing.qty || 0) : 0);
  const atCapacity = !skipCapacityGate && maxQty < 1;

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

  // Preset collapse (2026-07-13): Models C, D, E were removed. Every
  // "differentiating feature" they had (microstructure gates, news
  // blackout, book imbalance, etc.) is now a per-sleeve toggle — so
  // "apply Model C" and "apply Model B + check the microstructure box"
  // produce identical configs. Keeping five near-identical presets
  // just added UX confusion. Model B is now the canonical full-expert-
  // stack preset. Custom stays for manual configs. Model A (bare
  // fixed-limit control) was removed earlier at Adam's request — it
  // sold at target and missed breakouts.
  //
  // Existing sleeves that were created via Model C/D/E preset names
  // are UNAFFECTED — their saved sleeve.name is a string, independent
  // of the preset dropdown, and their configs already have the right
  // toggles saved on them. Only the "apply this preset" dropdown loses
  // the C/D/E options.
  const PRESETS = {
    'Model B — Defensive plus (ratchet + reanchor + volatility re-entry)': {
      // Everything the removed baseline had PLUS: ratcheting stop-loss (preserves gains),
      // auto-reanchor on stalled buy, volatility-contraction re-entry after
      // stop. This is the "expert stack" from the methodology discussion.
      exit_mode: 'hybrid',
      profitDollarsFixed: 10,
      trailDistance: 0.15,
      trailActivationOffset: 0.10,
      hybridDelay: 5,
      accumulate: { enabled: true, buffer_mult: 1.5, max_qty_mult: 2.5 },
      stopLoss: {
        enabled: true, price_below_buy_pct: 0.05, qty_mode: 'all',
        ratchet_enabled: true, ratchet_distance: 1.5, ratchet_activation: 0.5,
        reanchor_on_trigger: false, max_consecutive: 3,
        protect_realized_enabled: true, protect_realized_frac: 0.5,
      },
      reanchorThreshold: 0.75,
      timeReanchorSecs: 3600,        // 60 min: if we've been priced-out this long, walk forward
      volReanchorPercentile: 90,     // if at top 10% of recent bars, market is trending — walk forward
      volReanchorWindow: 60,         // over the last ~60 bars of price history
      reentry: { mode: 'volatility', range_contraction: 0.5, min_wait_secs: 30 },
      postTrailReentry: { mode: 'sequential', stage_b_max_wait_secs: 3600 },
      entryTrendFilter: { enabled: true, sma_window: 20 },
      microstructureGate: true,
      // Maker-only + penny-inside placement — beat other bots on fill priority
      // and slash fee cost. Defaults ON for Model B; opt-out via sleeve editor.
      postOnly: true,
      pennyInside: { enabled: true, max_ticks: 5 },
      // Book-imbalance gate (Chan/Harris): don't arm a leg whose direction
      // fights current top-5 book pressure. Same 65% threshold both ways.
      bookImbalance: { enabled: true, depth: 5, sell_threshold: 0.65, buy_threshold: 0.65 },
      // Trailing buy (Livermore / Turtle / Le Beau) — wait for bounce before
      // rebuying. Distance defaults to Le Beau 0.5×ATR from expert_params;
      // undefined here means "use the expert-derived default". Model B ships
      // it ON as part of the defensive stack.
      buyTrail: { enabled: true },
      // Funding-rate gate (Aksoy-Cheng / Hasbrouck) — crypto perps only.
      // Blocks BUY arms when 8h funding > +0.05% (expensive to hold long).
      // Non-perp products (silver/oil) unaffected — funding_gate_ok_for_buy
      // returns permissive-default when funding data missing.
      fundingGate: { enabled: true, threshold: 0.0005 },
      // Kelly quarter-Kelly sizing (Van Tharp) — no effect until sleeve has
      // 8+ cycles of data. Then sizes cfg.qty × Kelly f* (capped at 1.0
      // via kelly_fraction, never sizes up).
      kelly: { enabled: true, fraction: 0.25, min_cycles: 8 },
      // Adaptive spread (Andersen-Bollerslev) — widens spread up to 2× in
      // high realized-vol regimes. No effect when vol matches baseline.
      adaptiveSpread: { enabled: true, max_multiplier: 2.0, vol_window_secs: 300 },
      // Cross-exchange fair-value gate (Binance reference) — blocks arms
      // when Coinbase price diverges >1% from Binance mid. Only crypto
      // (BTC/ETH/SOL/…); permissive for non-mapped products.
      crossexGate: { enabled: true, max_divergence_pct: 1.0 },
      // Dynamic correlation gate — catches cross-family co-movement
      // (BTC↔gold in macro shocks). Requires 7d+ of snapshots per product;
      // permissive-default until then.
      correlationDynamic: { enabled: true, threshold: 0.6 },
      // ML predictor shadow signal — placeholder linear model until real
      // training data collected. Emits shadow log entries when |score| >
      // 0.3, does NOT gate arms. Purely observational, feeds the Signals
      // tab with ml@PlaceholderLinear entries.
      mlShadow: { enabled: true, threshold: 0.3 },
      // Classic-indicator shadow signals (RSI Wilder / Bollinger / MACD Appel).
      // Emitted at every arm for evaluation vs 1h/6h/24h outcomes.
      // Shadow-only: never gates arms.
      classicIndicatorsShadow: { enabled: true },
      note: 'Full expert stack: hybrid trail + accumulate + ratcheting stop-loss (locks in gains) + protect-half realized (never gives back >50% of booked gains) + trend-gated buys + volatility-contraction re-entry after stop + falling-knife protection on rebuys + funding-aware entries + Kelly-scaled sizing + vol-adaptive spread + cross-exchange fair-value check + dynamic correlation + ML shadow harness. Every feature is internally data-gated: no misfires when history is thin. Van Tharp / Livermore / Turtles / Le Beau / Andersen-Bollerslev / Aksoy-Cheng / Vince.',
    },
    'Custom': {
      exit_mode: 'fixed_limit',
      profitDollarsPerContract: 50,
      trailDistance: 0.10,
      note: 'You set every parameter. Use this when Models A–E don\'t match what you want.',
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
    // Hybrid mode ON by default (Adam 2026-07-13): "just [like] trail
    // stopping so we can maximize profit on breakouts." Hybrid = sell at
    // target (Path A, normal cycle) OR trail on breakout confirmation
    // (Path B, upside capture). Strictly better than pure trailing_stop
    // for cycle-focused strategies — normal cycles hit the target cleanly,
    // breakouts get ridden. See the mode comparison in the exit dropdown.
    exit_mode: 'hybrid',
    reanchor_threshold: 2.0,
    // Full expert stack ON by default for NEW sleeves (Adam: "turn them all
    // on by default"). Every feature is internally data-gated: Kelly waits
    // for 8+ cycles, adaptive spread waits for vol history, dynamic
    // correlation waits for 7d+ of snapshots, funding gate no-ops on non-
    // perps, crossex is permissive without Binance data. Nothing misfires
    // when the sleeve is brand new — they turn on when data arrives.
    // Existing sleeves preserve their saved config (draft = existing branch).
    buy_trail_enabled: true,
    // Expert-canon default when ATR is loaded (Le Beau 0.5×ATR / Kaufman
    // 0.75×ATR). Absolute-last fallback of 0.05 only fires when neither
    // ATR nor tick_size is available — very rare cold-start case.
    buy_trail_distance: (
      expertDollars?.buy_trail_distance
      || (cfg?.tick_size ? Number(cfg.tick_size) * 20 : 0.05)
    ),
    funding_gate_enabled: true,
    funding_gate_threshold: 0.0005,
    kelly_enabled: true,
    kelly_fraction: 0.25,
    kelly_min_cycles: 8,
    adaptive_spread_enabled: true,
    adaptive_spread_max_multiplier: 2.0,
    adaptive_spread_vol_window_secs: 300,
    crossex_gate_enabled: true,
    crossex_max_divergence_pct: 1.0,
    correlation_dynamic_enabled: true,
    correlation_dynamic_threshold: 0.6,
    ml_shadow_enabled: true,
    ml_signal_threshold: 0.3,
    classic_indicators_shadow_enabled: true,
  };

  // Coerce falling-knife protection to ON for EXISTING sleeves too if the
  // field is missing (never saved) OR undefined. Adam's durable rule:
  // "Turn them all on by default." A sleeve that predates the falling-
  // knife feature has draft.buy_trail_enabled === undefined; we treat
  // that as "user hasn't decided, apply the default (ON)". Explicit
  // false stays false so users who deliberately turned it off aren't
  // silently re-enabled.
  if (existing && draft.buy_trail_enabled === undefined) {
    draft.buy_trail_enabled = true;
    if (!draft.buy_trail_distance) {
      draft.buy_trail_distance = expertDollars?.buy_trail_distance
        || (draft.trail_distance ? Number(draft.trail_distance) * 0.5 : 0)
        || (cfg?.tick_size ? Number(cfg.tick_size) * 20 : 0.05);
    }
  }

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
      ${expertParams ? `
        <div class="sleeve-expert">
          <div class="sleeve-expert-head">
            <b>Expert-tuned to ${escapeHtml(symbolFamilyOf(symbol) || symbol)}</b>
            <span class="dim">·</span>
            <span>ATR (14, 5min) <b class="mono">$${fmtPrice(expertATR)}</b></span>
            <span class="dim">·</span>
            <span>Asset class: <b>${escapeHtml(expertParams.asset_class || 'other')}</b></span>
            ${tunedParams?.trail_x_atr ? `
              <span class="dim">·</span>
              <span>Layer 2 tuned: trail <b class="mono">${tunedParams.trail_x_atr}×ATR</b>
                <span class="dim">(from ${tunedParams.days || 30}d history)</span>
              </span>
            ` : `
              <span class="dim">·</span>
              <span class="dim">Layer 2 tuning pending (runs daily)</span>
            `}
          </div>
          <div class="sleeve-expert-formulas">
            Trail <b class="mono">$${fmtPrice(expertDollars?.trail_distance || 0)}</b>
              (${(effectiveMultipliers?.trail_x_atr || 0).toFixed(1)}×ATR, Turtle 2N)
            <span class="dim">·</span>
            Stop <b class="mono">$${fmtPrice(expertDollars?.stop_loss_distance || 0)}</b>
              (${(effectiveMultipliers?.stop_x_atr || 0).toFixed(1)}×ATR, Van Tharp 1R)
            <span class="dim">·</span>
            Ratchet <b class="mono">$${fmtPrice(expertDollars?.ratchet_distance || 0)}</b>
              (${(effectiveMultipliers?.ratchet_x_atr || 0).toFixed(1)}×ATR, Le Beau chandelier)
          </div>
        </div>
      ` : ''}
      <div class="sleeve-anchor ${anchorStale ? 'stale' : ''}">
        <div class="sleeve-anchor-title">Anchor the strategy around</div>
        <div class="sleeve-anchor-toggle" role="tablist">
          ${anchorChoices.map((c, i) => c.custom ? `
            <div class="anchor-choice anchor-choice-custom"
                 data-anchor-idx="${i}" data-anchor-custom="1">
              <span class="anchor-choice-label">${escapeHtml(c.label)}</span>
              <span class="anchor-choice-value">$<input type="number" step="any"
                   class="anchor-custom-input" value="${fmtPrice(c.value)}"></span>
            </div>
          ` : `
            <button type="button" class="anchor-choice ${Math.abs(anchor - c.value) < 0.001 ? 'active' : ''}"
                    data-anchor-idx="${i}" data-anchor="${c.value}">
              <span class="anchor-choice-label">${escapeHtml(c.label)}</span>
              <span class="anchor-choice-value">$${fmtPrice(c.value)}</span>
            </button>
          `).join('')}
        </div>
        ${strategyEntryMark > 0 && existingSleeveForAnchor ? `
          <div class="sleeve-anchor-sub">
            <span class="dim">Strategy originally entered at</span>
            <b class="mono">$${fmtPrice(strategyEntryMark)}</b>
            ${strategyEntryTs > 0 ? `<span class="dim">on ${new Date(strategyEntryTs * 1000).toLocaleString()}</span>` : ''}
          </div>` : ''}
        ${anchorStale ? `
          <div class="sleeve-anchor-sub">
            <span class="stale-warn">Selected anchor is $${fmtPrice(anchorToMarketDist)} away from current market — targets below may be off-market</span>
          </div>` : ''}
      </div>
      ${specMissing ? `
      <div class="sleeve-spec-missing-warn" style="background:rgba(255,159,64,0.12);border:1px solid var(--warn);border-radius:6px;padding:10px 12px;margin:8px 0;color:var(--warn);font-size:13px;">
        <b>Coinbase specs not synced yet for ${escapeHtml(symbol)}.</b>
        <div style="margin-top:4px;color:var(--muted);">
          The bot pulls contract_size and per-fill fees from Coinbase on each product's first tick. Presets and the $/swing slider are disabled until that lands — otherwise silver-scale defaults would leak into the spread math and save prices that produce a fraction of your target (this is what happened to NER + NGS earlier). Close this modal, wait 60s, and reopen.
        </div>
      </div>` : ''}
      <div id="sleeve-spread-recs" class="sleeve-spread-recs" hidden>
        <div class="sleeve-spread-recs-head">
          Recommended spreads
          <span class="dim">— from last scanner run · click one to apply</span>
        </div>
        <div class="sleeve-spread-recs-body dim">loading…</div>
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
          <input type="number" id="sl-sell-target" step="${tickStep}" value="${existing?.sell_px ?? (anchor + 0.5).toFixed(pricePrec)}">
        </label>
        <label>Buy-back target
          <input type="number" id="sl-buy-target" step="${tickStep}" value="${existing?.buy_px ?? (anchor - 0.5).toFixed(pricePrec)}">
        </label>
      </div>

      <!-- Profit target slider — TAKE-HOME (net after fees). Bot back-calcs the gap. -->
      <div class="profit-slider-block" id="sl-fixed-block">
        <div class="profit-slider-header">
          <span class="slider-label">Or drag: <b>net</b> take-home per swing (after fees, all <span id="sl-qty-echo">${startingQty}</span> contracts)</span>
          <span class="slider-value-input-wrap">$<input type="number" id="sl-profit-val" class="slider-value-input" min="10" max="2000" step="1" value="${existingTotalProfit}"></span>
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
        <div class="trail-dollars" id="sl-td-dollars"></div>
        <div class="trail-status" id="sl-td-status"></div>
        <div class="preview-note">
          Trailing stop uses the sell target above as the ARM price — once ${escapeHtml(symbolFamilyOf(symbol) || 'price')} hits it, the trail engages and rides upside. Sells when price pulls back this much from the peak.
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
          Once ${escapeHtml(symbolFamilyOf(symbol) || 'price')} crosses the <b>sell target</b>, we wait this many seconds to see if it pushes through the <b>trail activation price</b>.
          If it does → trailing stop engages and rides the breakout. If it doesn't → we take the swing at market when the delay expires.
        </div>
      </div>

      <div class="sleeve-preview" id="sl-preview">
        <!-- filled by updatePreview() -->
      </div>

      <!-- Trailing buy — wait for a bounce before rebuying instead of
           letting a resting limit catch the falling knife. Expert canon:
           Livermore ("Buy on the pivot"), Turtle Rules (breakout confirm),
           Le Beau (ATR entry filter). Default distance = 0.5×ATR from
           expert_params. -->
      <div class="accumulate-block">
        <label class="accumulate-toggle">
          <input type="checkbox" id="sl-buy-trail" ${draft.buy_trail_enabled ? 'checked' : ''}>
          <b>Wait for bounce before rebuying (don't catch a falling knife)</b>
        </label>
        <div class="accumulate-fields" id="sl-buy-trail-fields" ${draft.buy_trail_enabled ? '' : 'hidden'}>
          <div class="target-inputs">
            <label>Bounce distance ($)
              <input type="number" id="sl-buy-trail-distance" step="0.0001"
                     value="${draft.buy_trail_distance || (
                       // Preferred: pure expert canon from ATR × class multiplier
                       // (Le Beau 0.5× for metals/energy, Kaufman 0.75× for crypto)
                       expertDollars?.buy_trail_distance
                       // Fallback: half of the sleeve's trail_distance. This is
                       // mathematically 0.5×ATR when trail_x_atr=2.0 (the metals/
                       // energy default). Off for crypto (0.625× vs 0.75× canonical).
                       // Adam flagged this in the UI — for full expert canon the
                       // scanner needs to have fetched candles for this product so
                       // expertATR loads. Until then, this fallback is the closest
                       // approximation available client-side.
                       || (draft.trail_distance ? draft.trail_distance * 0.5 : 0)
                       // Last resort: tick_size × 20 (a rough vol proxy) or 0.05.
                       || (cfg?.tick_size ? Number(cfg.tick_size) * 20 : 0.05)
                     )}"
                     title="${expertDollars?.buy_trail_distance
                       ? `Expert-derived: ${(effectiveMultipliers?.buy_trail_x_atr || 0.5).toFixed(2)}×ATR of ${fmtPrice(expertATR)} = ${fmtPrice(expertDollars.buy_trail_distance)} (${expertParams?.asset_class || 'unknown'} class)`
                       : `Fallback (no expert ATR loaded for ${escapeHtml(symbol)}). Half of the sleeve's trail_distance — a rough proxy. Fully-expert value activates once the scanner has candles for this product.`
                     }">
            </label>
          </div>
          <div class="preview-note">
            When mark drops through your buy target, the sleeve tracks the running low
            instead of buying immediately. It only submits the buy after mark bounces
            <b>this much</b> above the local low — confirms the fall is over. Never
            pays more than the original buy target.
            ${expertDollars?.buy_trail_distance ? `
              <b class="pos">Expert-derived from ATR</b>:
              ${(effectiveMultipliers?.buy_trail_x_atr || 0.5).toFixed(2)}×ATR of
              $${fmtPrice(expertATR)} = <b class="mono">$${fmtPrice(expertDollars.buy_trail_distance)}</b>
              (${escapeHtml(expertParams?.asset_class || 'unknown')} — Le Beau /
              ${expertParams?.asset_class === 'crypto' ? 'Kaufman crypto bump' : 'entry filter'}).
            ` : `
              <b class="warn">Approximate</b> — expert ATR isn't loaded for ${escapeHtml(symbol)} yet.
              Current default is half the sleeve's trail_distance (a rough proxy for 0.5×ATR).
              Value auto-refreshes to pure expert canon once the scanner fetches candles for this product.
            `}
            Livermore's "pivot" rule; Turtle breakout confirmation; Le Beau ATR entry filter.
          </div>
        </div>
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
        <div id="sl-stoploss-global-warn" class="halt-banner" style="margin:6px 0;padding:8px 12px;font-size:13px;" ${
          currentStore[Object.keys(currentStore || {}).find(t => modeOfTenant(t) === 'live')]?.['__stop_loss_disabled__']?.config?.disabled ? '' : 'hidden'
        }>🛑 Global stop-loss is currently OFF — the checkbox + trigger below stay saved but the bot will NOT fire this stop until you flip the header button back ON.</div>
        <label class="accumulate-toggle">
          <input type="checkbox" id="sl-stoploss" ${draft.stop_loss_enabled ? 'checked' : ''}>
          <b>Stop-loss (protects during a crash)</b>
        </label>
        <div class="accumulate-fields" id="sl-stoploss-fields" ${draft.stop_loss_enabled ? '' : 'hidden'}>
          <div class="target-inputs">
            <label>Trigger price ($) — sell when ${escapeHtml(symbolFamilyOf(symbol) || 'price')} falls to
              <input type="number" id="sl-stop-px" step="0.01" value="${draft.stop_loss_px || Math.max(0, +(mark * 0.95).toFixed(Math.max(2, mark < 1 ? 4 : 2)))}">
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
            When ${escapeHtml(symbolFamilyOf(symbol) || 'price')} ≤ trigger, this sleeve market-sells the configured qty then halts.
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
  const tdDollarsEl = m.querySelector('#sl-td-dollars');
  const tdStatusEl = m.querySelector('#sl-td-status');
  const sellTargetEl = m.querySelector('#sl-sell-target');
  const buyTargetEl = m.querySelector('#sl-buy-target');
  const trailActivationEl = m.querySelector('#sl-trail-activation');
  const hybridDelayEl = m.querySelector('#sl-hybrid-delay');
  const anchorChoiceBtns = Array.from(m.querySelectorAll('.anchor-choice'));
  const accumulateToggle = m.querySelector('#sl-accumulate');
  const accumulateFields = m.querySelector('#sl-accumulate-fields');
  if (accumulateToggle && accumulateFields) {
    accumulateToggle.addEventListener('change', () => {
      accumulateFields.hidden = !accumulateToggle.checked;
    });
  }
  const buyTrailToggle = m.querySelector('#sl-buy-trail');
  const buyTrailFields = m.querySelector('#sl-buy-trail-fields');
  if (buyTrailToggle && buyTrailFields) {
    buyTrailToggle.addEventListener('change', () => {
      buyTrailFields.hidden = !buyTrailToggle.checked;
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
    let p = PRESETS[name];
    if (!p) return;
    if (specMissing) {
      alert(`Cannot apply preset — Coinbase specs (contract_size, fees) haven't loaded yet for ${symbol}. Wait a few seconds and reopen this modal. The bot fetches specs from Coinbase on each product's first tick.`);
      return;
    }
    exitEl.value = p.exit_mode;
    // Expert-derived per-product values override the preset's silver-tuned
    // defaults when we have ATR for this product. So Model B applied to oil
    // gets oil's trail distance (~$0.84), not silver's ($0.15).
    if (expertDollars) {
      p = {
        ...p,
        trailDistance: expertDollars.trail_distance,
        trailActivationOffset: expertDollars.activation_offset,
        stopLoss: p.stopLoss ? {
          ...p.stopLoss,
          price_below_buy: expertDollars.stop_loss_distance,
          ratchet_distance: expertDollars.ratchet_distance,
          ratchet_activation: expertDollars.ratchet_activation,
        } : p.stopLoss,
        reanchorThreshold: expertDollars.reanchor_threshold,
      };
    }
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
    // Rewrite the preset note to reflect the ACTUAL applied values (which
    // may be expert-tuned per this product), not the hardcoded silver text.
    let liveNote = p.note;
    if (expertDollars) {
      const parts = [`ATR-tuned to this product (${expertParams?.asset_class || 'other'}).`];
      if (p.stopLoss?.enabled ?? p.stopLoss) {
        parts.push(`Stop $${fmtPrice(expertDollars.stop_loss_distance)} below buy (2×ATR, Van Tharp 1R).`);
      }
      if (p.exit_mode === 'trailing_stop' || p.exit_mode === 'hybrid') {
        parts.push(`Trail $${fmtPrice(expertDollars.trail_distance)} (2×ATR, Turtle 2N).`);
      }
      if (p.stopLoss?.ratchet_enabled || p.stopLoss?.ratchet_distance != null) {
        parts.push(`Ratchet $${fmtPrice(expertDollars.ratchet_distance)} (3×ATR, Le Beau chandelier).`);
      }
      liveNote = parts.join(' ') + ' ' + p.note;
    }
    if (p.profitDollarsFixed != null && appliedTarget > p.profitDollarsFixed) {
      presetNoteEl.innerHTML = liveNote + ` <b style="color:var(--warn)">Note: at ${qty} contracts, fees alone are $${feesTotal.toFixed(2)}, so this preset targets $${appliedTarget} net (the floor) not $${p.profitDollarsFixed}.</b>`;
    } else {
      presetNoteEl.textContent = liveNote;
    }
    syncTargetsFromSlider();
    // Now that sell/buy targets are computed, populate the hybrid fields.
    if (p.exit_mode === 'hybrid') {
      if (p.trailActivationOffset != null && trailActivationEl && sellTargetEl) {
        const sellPx = Number(sellTargetEl.value) || 0;
        trailActivationEl.value = (sellPx + p.trailActivationOffset).toFixed(3);
      }
      if (p.hybridDelay != null && hybridDelayEl) {
        hybridDelayEl.value = p.hybridDelay;
      }
    }
    // Accumulate toggle + fields — presets can enable pyramiding.
    if (p.accumulate) {
      const accEl = m.querySelector('#sl-accumulate');
      const maxQtyEl = m.querySelector('#sl-max-qty');
      const bufEl = m.querySelector('#sl-scale-buf');
      const accFields = m.querySelector('#sl-accumulate-fields');
      if (accEl) accEl.checked = !!p.accumulate.enabled;
      if (accFields) accFields.hidden = !p.accumulate.enabled;
      if (maxQtyEl && p.accumulate.max_qty_mult) {
        maxQtyEl.value = Math.max(1, Math.round(qty * p.accumulate.max_qty_mult));
      }
      if (bufEl && p.accumulate.buffer_mult) {
        bufEl.value = p.accumulate.buffer_mult;
      }
    }
    // Trailing-buy (falling-knife) toggle + distance. Sync the checkbox +
    // fields visibility whenever a preset is applied so switching presets
    // mid-edit updates the UI.
    {
      const btEl = m.querySelector('#sl-buy-trail');
      const btDistEl = m.querySelector('#sl-buy-trail-distance');
      const btFields = m.querySelector('#sl-buy-trail-fields');
      if (btEl) btEl.checked = !!draft.buy_trail_enabled;
      if (btFields) btFields.hidden = !draft.buy_trail_enabled;
      if (btDistEl && draft.buy_trail_distance) btDistEl.value = draft.buy_trail_distance;
    }
    // Stop-loss toggle + fields — presets can enable crash protection.
    if (p.stopLoss) {
      const slEl = m.querySelector('#sl-stoploss');
      const slPxEl = m.querySelector('#sl-stop-px');
      const slModeEl = m.querySelector('#sl-stop-mode');
      const slFields = m.querySelector('#sl-stoploss-fields');
      if (slEl) slEl.checked = !!p.stopLoss.enabled;
      if (slFields) slFields.hidden = !p.stopLoss.enabled;
      if (slPxEl && buyTargetEl) {
        const buyPx = Number(buyTargetEl.value) || 0;
        // Product-scale-aware: prefer the percent-based value so a $3 gas
        // contract doesn't inherit silver's $1.50 fixed distance (which
        // would put the stop at $1.50 — 50% below buy — on a natural gas
        // sleeve). Fall back to the legacy fixed dollar field for older
        // preset shapes still in the wild.
        let stopPx = null;
        if (p.stopLoss.price_below_buy_pct != null) {
          stopPx = buyPx * (1 - Number(p.stopLoss.price_below_buy_pct));
        } else if (p.stopLoss.price_below_buy != null) {
          stopPx = buyPx - Number(p.stopLoss.price_below_buy);
        }
        if (stopPx !== null) {
          const decimals = buyPx < 1 ? 4 : 2;
          slPxEl.value = Math.max(0, +stopPx.toFixed(decimals)).toString();
        }
      }
      if (slModeEl && p.stopLoss.qty_mode) {
        slModeEl.value = p.stopLoss.qty_mode;
      }
    }
    // Reanchor threshold — no UI in the sleeve editor, but the save handler
    // reads draft.reanchor_threshold. Mutate draft so it flows through.
    if (p.reanchorThreshold != null) {
      draft.reanchor_threshold = p.reanchorThreshold;
    }
    // Time + volatility reanchor — additional triggers alongside the
    // price-based reanchor_threshold. All three can fire; whichever hits
    // first wins.
    draft.time_reanchor_secs = p.timeReanchorSecs ?? 0;
    draft.vol_reanchor_percentile = p.volReanchorPercentile ?? 0;
    draft.vol_reanchor_window = p.volReanchorWindow ?? 60;
    // Ratcheting stop-loss + re-entry + news blackout + microstructure gate —
    // no editor UI, presets are the primary way to configure. Flow through
    // draft to the save patch. Explicitly write nulls when the preset omits
    // them so previous preset selections don't leak into the next one.
    const sl = p.stopLoss || {};
    draft.stop_loss_ratchet_enabled = !!sl.ratchet_enabled;
    draft.stop_loss_ratchet_distance = sl.ratchet_distance ?? 1.50;
    draft.stop_loss_ratchet_activation = sl.ratchet_activation ?? 0.50;
    draft.stop_loss_reanchor_on_trigger = !!sl.reanchor_on_trigger;
    draft.stop_loss_max_consecutive = sl.max_consecutive ?? 0;
    // Protect-half realized-gains stop: caps loss on this cycle at
    // (realized_pnl × frac). Only meaningful from cycle 2+. Flows through
    // draft — no editor UI, presets are the interface.
    draft.stop_loss_protect_realized_enabled = !!sl.protect_realized_enabled;
    draft.stop_loss_protect_realized_frac = sl.protect_realized_frac ?? 0.5;
    // Trend gate on buy arm: refuses to arm buys when last_price < N-bar SMA
    // (Turtle / Livermore anti-falling-knife rule).
    const etf = p.entryTrendFilter || {};
    draft.entry_trend_filter_enabled = !!etf.enabled;
    draft.entry_trend_sma_window = etf.sma_window ?? 20;
    // Post-trail re-entry (Flavor 3): after a trail-based sell, wait for
    // volatility to contract THEN a new high before re-arming buys. Sequential
    // Kaufman-then-Turtle gating. 'off' = no wait, 'volatility' = Stage A only,
    // 'sequential' = both stages.
    const ptr = p.postTrailReentry || {};
    draft.post_trail_reentry_mode = String(ptr.mode || 'off');
    draft.post_trail_stage_b_max_wait_secs = Number(ptr.stage_b_max_wait_secs) || 3600;
    const re = p.reentry || {};
    draft.reentry_mode = re.mode || 'off';
    draft.reentry_range_contraction = re.range_contraction ?? 0.5;
    draft.reentry_range_window = re.range_window ?? 60;
    draft.reentry_min_wait_secs = re.min_wait_secs ?? 30;
    const nb = p.newsBlackout || {};
    draft.news_blackout_enabled = !!nb.enabled;
    draft.news_blackout_tier = nb.tier ?? 2;
    draft.microstructure_gate_enabled = !!p.microstructureGate;
    // Maker-only + penny-inside — preset ships them ON for Model B+; overrides
    // land here so the sleeve save picks them up alongside the other fields.
    draft.post_only_enabled = !!p.postOnly;
    const pi = p.pennyInside || {};
    draft.penny_inside_enabled = !!pi.enabled;
    draft.penny_inside_max_ticks = pi.max_ticks ?? 5;
    const bi = p.bookImbalance || {};
    draft.book_imbalance_gate_enabled = !!bi.enabled;
    draft.book_imbalance_depth_levels = bi.depth ?? 5;
    draft.book_imbalance_sell_threshold = bi.sell_threshold ?? 0.65;
    draft.book_imbalance_buy_threshold = bi.buy_threshold ?? 0.65;
    // Trailing buy (falling-knife protection). Distance defaults from expert
    // params (0.5×ATR for metals/energy, 0.75×ATR for crypto) unless the
    // preset overrides. Preset can also disable it explicitly.
    const bt = p.buyTrail || {};
    draft.buy_trail_enabled = bt.enabled !== undefined ? !!bt.enabled : true;
    draft.buy_trail_distance = bt.distance != null
      ? Number(bt.distance)
      : (expertDollars?.buy_trail_distance || draft.buy_trail_distance || 0.05);
    // Seven-feature batch (2026-07-13). All internally data-gated so
    // enabling doesn't misfire when history is thin — they no-op until
    // there's real data to act on.
    const fg = p.fundingGate || {};
    draft.funding_gate_enabled = fg.enabled !== undefined ? !!fg.enabled : true;
    draft.funding_gate_threshold = fg.threshold != null ? Number(fg.threshold) : 0.0005;
    const kl = p.kelly || {};
    draft.kelly_enabled = kl.enabled !== undefined ? !!kl.enabled : true;
    draft.kelly_fraction = kl.fraction != null ? Number(kl.fraction) : 0.25;
    draft.kelly_min_cycles = kl.min_cycles != null ? Number(kl.min_cycles) : 8;
    const ad = p.adaptiveSpread || {};
    draft.adaptive_spread_enabled = ad.enabled !== undefined ? !!ad.enabled : true;
    draft.adaptive_spread_max_multiplier = ad.max_multiplier != null ? Number(ad.max_multiplier) : 2.0;
    draft.adaptive_spread_vol_window_secs = ad.vol_window_secs != null ? Number(ad.vol_window_secs) : 300;
    const cx = p.crossexGate || {};
    draft.crossex_gate_enabled = cx.enabled !== undefined ? !!cx.enabled : true;
    draft.crossex_max_divergence_pct = cx.max_divergence_pct != null ? Number(cx.max_divergence_pct) : 1.0;
    const cd = p.correlationDynamic || {};
    draft.correlation_dynamic_enabled = cd.enabled !== undefined ? !!cd.enabled : true;
    draft.correlation_dynamic_threshold = cd.threshold != null ? Number(cd.threshold) : 0.6;
    const ml = p.mlShadow || {};
    draft.ml_shadow_enabled = ml.enabled !== undefined ? !!ml.enabled : true;
    draft.ml_signal_threshold = ml.threshold != null ? Number(ml.threshold) : 0.3;
    const ci = p.classicIndicatorsShadow || {};
    draft.classic_indicators_shadow_enabled = ci.enabled !== undefined ? !!ci.enabled : true;
    applyModeVisibility();
  }

  function syncTargetsFromSlider() {
    // Spec §5A: slider = target NET after fees. Back-calculate the gross gap:
    //   gross_needed = target_net + roundtrip_fees
    //   spread_per_contract = gross_needed / (qty × contract_size)
    // That's the price gap the bot must actually capture to hand you the net.
    if (specMissing) return;  // guard — refuse to compute with silver defaults
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
    // Preserve the current activation-above-sell offset so a user who set a
    // wider offset (e.g. +$0.50) keeps it as sell target moves. Default to
    // $0.10 when the offset is zero or negative (fresh state / preset default).
    const prevSellPx = Number(sellTargetEl.value) || currentAnchor;
    const prevActivation = Number(trailActivationEl?.value) || prevSellPx;
    const activationOffset = Math.max(0.005, prevActivation - prevSellPx || 0.10);
    const newSellPx = currentAnchor + spread / 2;
    sellTargetEl.value = newSellPx.toFixed(pricePrec);
    buyTargetEl.value = (currentAnchor - spread / 2).toFixed(pricePrec);
    // Auto-slide trail activation up with the sell target so the invariant
    // "activation > sell target" always holds — no manual re-edit needed.
    if (trailActivationEl) {
      trailActivationEl.value = (newSellPx + activationOffset).toFixed(pricePrec);
    }
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

    // Below-cost warning. Adam's rule: never sell below cost basis. If the
    // current sell target lands below Your Contract Avg, warn conspicuously
    // — anchor drift or slider drag can silently produce this scenario.
    const costBasis = Number(snap.position_avg_entry) || 0;
    let belowCostBanner = '';
    if (costBasis > 0 && sellPx > 0 && sellPx < costBasis) {
      const lossPerCt = (costBasis - sellPx) * contractSize;
      const totalLoss = lossPerCt * qty;
      belowCostBanner = `
        <div class="preview-below-cost">
          ⚠ SELL TARGET BELOW COST — $${fmtPrice(sellPx)} vs cost $${fmtPrice(costBasis)}.
          If this fires, you lose <b>$${totalLoss.toFixed(2)}</b> on the sale
          ($${lossPerCt.toFixed(2)} × ${qty} contract${qty === 1 ? '' : 's'}).
          Switch anchor to <b>Your contract avg</b> to keep sell above water.
        </div>
      `;
    }

    // Only overwrite the input if the user isn't currently focused in it —
    // otherwise we clobber their typing mid-edit.
    if (document.activeElement !== profitValEl) profitValEl.value = profitEl.value;
    updateFillPct(profitEl);
    updateFillPct(tdSliderEl);
    const tdRaw = Number(tdSliderEl.value);
    tdValEl.textContent = `$${fmtPrice(tdRaw)}`;
    if (tdDollarsEl) {
      // trail_distance × contract_size = $/contract given back on pullback.
      // × qty = total $ this sleeve gives back before the trailing stop fires.
      const perCt = tdRaw * contractSize;
      const total = perCt * qty;
      tdDollarsEl.innerHTML = `
        $${fmtPrice(tdRaw)} × <b>${contractSize}</b> oz/ct = <b>$${perCt.toFixed(2)}</b> per contract
        <span class="dim">·</span>
        <b>$${total.toFixed(2)}</b> total across ${qty} ct
      `;
    }
    if (tdStatusEl && existing) {
      // Read live sleeve state: trail_high_water_price > 0 means the trail
      // has activated and is riding a peak. Fresh sleeves have it at 0.
      const liveSleeveState = currentStore[tenant]?.[symbol]?.state?.sleeves?.[existing.id] || {};
      const peak = Number(liveSleeveState.trail_high_water_price) || 0;
      const sellFires = peak > 0 ? peak - tdRaw : 0;
      if (peak > 0) {
        const distFromPeak = mark > 0 ? peak - mark : 0;
        tdStatusEl.innerHTML = `
          <div class="td-status-on">
            <b>TRAIL ACTIVE</b>
            <span class="dim">·</span>
            Peak <b class="mono">$${fmtPrice(peak)}</b>
            <span class="dim">·</span>
            Sells at <b class="mono">$${fmtPrice(sellFires)}</b>
            ${mark > 0 ? `<span class="dim">·</span> Now $${fmtPrice(mark)} (${distFromPeak >= 0 ? '−' : '+'}$${fmtPrice(Math.abs(distFromPeak))} from peak)` : ''}
          </div>
        `;
      } else {
        // Not activated yet. Show what mark needs to cross to arm the trail.
        const armPrice = exitEl.value === 'hybrid'
          ? Number(m.querySelector('#sl-trail-activation')?.value) || Number(sellTargetEl.value) + 0.10
          : Number(sellTargetEl.value);
        tdStatusEl.innerHTML = `
          <div class="td-status-off">
            <b>Not yet activated</b>
            <span class="dim">·</span>
            Arms once price crosses <b class="mono">$${fmtPrice(armPrice)}</b>
            ${mark > 0 && armPrice > 0 ? `<span class="dim">(${mark >= armPrice ? 'now above' : `$${fmtPrice(armPrice - mark)} away`})</span>` : ''}
          </div>
        `;
      }
    }

    if (mode === 'trailing_stop') {
      const trailDistance = Number(tdSliderEl.value) || 0.1;
      const estGross = Math.max(0, (sellPx - trailDistance - buyPx) * contractSize * qty);
      const estNet = estGross - feesPerSwing;
      previewEl.innerHTML = `
        ${belowCostBanner}
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
        ${belowCostBanner}
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
        ${belowCostBanner}
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
  // Manual net-profit entry — mirrors the slider. Typing directly is the
  // precision path (slider steps in $10); input steps in $1 and accepts any
  // value clamped to the slider's dynamic min/max (min = fees + $1 floor).
  profitValEl.addEventListener('input', () => {
    const raw = Number(profitValEl.value);
    if (!Number.isFinite(raw)) return;
    const min = Number(profitEl.min) || 10;
    const max = Number(profitEl.max) || 2000;
    profitEl.value = Math.max(min, Math.min(max, raw));
    syncTargetsFromSlider();
    updatePreview();
  });
  // On blur, snap the input's visible value back to what actually got applied
  // (clamped + sanitized) so the field never disagrees with the slider.
  profitValEl.addEventListener('blur', () => { profitValEl.value = profitEl.value; });
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
    // Toggle .active across all anchor choice buttons so the user can see
    // which one is in play. All remain visible so the choice is reversible.
    for (const b of anchorChoiceBtns) b.classList.toggle('active', b === activeBtn);
    // Durable rule (Adam 2026-07-13): anchor change must re-apply expert
    // canon so every anchor-relative field (sell/buy around anchor, stop
    // loss = anchor − 1R, trail activation = sell + Le Beau buffer)
    // updates automatically. Never leave a stale value that no longer
    // matches the anchor the user just picked.
    const currentSpread = Number(sellTargetEl.value) - Number(buyTargetEl.value);
    if (currentSpread > 0) {
      applyExpertCanonToForm(currentSpread, newAnchor);
    } else {
      syncTargetsFromSlider();
    }
    updatePreview();
  }
  for (const b of anchorChoiceBtns) {
    if (b.dataset.anchorCustom === '1') {
      // Custom-target choice: clicking anywhere on the tile activates it
      // (using whatever price is currently in the input); typing into the
      // input keeps the tile active and re-syncs targets live.
      const input = b.querySelector('.anchor-custom-input');
      b.addEventListener('click', (e) => {
        if (e.target !== input) input && input.focus();
        const v = Number(input && input.value) || 0;
        if (v > 0) setAnchor(v, b);
      });
      if (input) {
        input.addEventListener('input', () => {
          const v = Number(input.value) || 0;
          if (v > 0) setAnchor(v, b);
        });
      }
    } else {
      b.onclick = () => setAnchor(Number(b.dataset.anchor), b);
    }
  }

  // Initial state.
  // For NEW sleeves: applyPreset seeds the form from the chosen preset.
  // For EXISTING sleeves: preserve the sleeve's actually-saved sell_px/buy_px
  // (already in the inputs via `<input value="${existing.sell_px}">`). Do NOT
  // call syncTargetsFromSlider here — it would derive fresh sell/buy from
  // the anchor + slider position and overwrite what the user saved. Users
  // reported "prices don't update after edit" because of exactly this: the
  // overwrite meant the form saved derived values, not their edits.
  if (!existing) applyPreset(presetEl.value);
  else {
    presetNoteEl.textContent = PRESETS[draft.name]?.note || '';
    // Cost-floor + slider fill + preview refresh WITHOUT touching sell/buy.
    updateFillPct(profitEl);
    updateFillPct(tdSliderEl);
    updatePreview();
  }
  applyModeVisibility();

  // Fetch scanner spread recommendations for this product and render as
  // clickable tiles with daily/weekly/monthly projections. Non-blocking —
  // editor is fully usable while this loads.
  loadSpreadRecommendations(symbol, m, {
    onApply: (spread, netPerRt) => {
      // Durable rule (Adam 2026-07-13): "Everything should auto adjust to
      // whatever tile I choose and what entry price I choose." Tile click
      // must recompute EVERY expert-derived field from expertATR × class
      // multiplier — never leave a stale saved value. This is the single
      // source of truth for the Van Tharp / Livermore / Turtle / Le Beau /
      // Kaufman formulas we ship. Saving them in memory is meaningless if
      // we don't apply them on every user interaction.
      //
      // Anchor choice (currentAnchor) is respected — the user picks WHERE
      // to center the spread (Your Contract Avg to stay above cost, or
      // Current Market to trade around now). Only the SPREAD and ATR-
      // derived params update automatically.
      applyExpertCanonToForm(spread, currentAnchor);
      // Slider snap to tile's expert-computed net × current qty — preserves
      // exact net (0.01 step) so tile ↔ slider reconciles.
      if (Number.isFinite(netPerRt) && netPerRt > 0) {
        const qtyLive = Math.max(1, Number(qtyEl?.value) || 1);
        const totalNet = Number((netPerRt * qtyLive).toFixed(2));
        const displayVal = totalNet.toFixed(2);
        if (totalNet < Number(profitEl.min || 0)) profitEl.min = displayVal;
        if (totalNet < Number(profitValEl.min || 0)) profitValEl.min = displayVal;
        profitEl.step = '0.01';
        profitValEl.step = '0.01';
        profitEl.value = displayVal;
        profitValEl.value = displayVal;
      }
      updatePreview();
    }
  });

  // Expert-canon auto-apply. Called by tile click AND by anchor change.
  // Recomputes every ATR-derived field from expertDollars — the pure
  // expert stack — so the form stays true to the chosen tile + anchor.
  // If expertATR isn't loaded (no ATR for this product yet), only the
  // spread-dependent fields update; ATR-dependent fields keep the last
  // known value and updatePreview() will show the "approximate" warning.
  function applyExpertCanonToForm(spread, anchorPx) {
    // Spread + anchor → sell / buy targets (Van Tharp midpoint math)
    if (spread > 0 && anchorPx > 0) {
      const halfSpread = spread / 2;
      const newSell = Number((anchorPx + halfSpread).toFixed(pricePrec));
      const newBuy = Number((anchorPx - halfSpread).toFixed(pricePrec));
      sellTargetEl.value = newSell;
      buyTargetEl.value = newBuy;
    }
    const currentSell = Number(sellTargetEl.value) || 0;
    // ATR-derived fields — only if we have expert ATR for this product.
    if (expertDollars) {
      // Trail distance (Turtle 2N / Kaufman crypto bump)
      if (tdSliderEl && expertDollars.trail_distance > 0) {
        tdSliderEl.value = expertDollars.trail_distance.toFixed(pricePrec);
        if (tdValEl) tdValEl.textContent = `$${fmtPrice(expertDollars.trail_distance)}`;
      }
      // Trail activation offset (Le Beau breakout buffer) — activation
      // sits above the sell target by activation_offset.
      if (trailActivationEl && expertDollars.activation_offset > 0 && currentSell > 0) {
        const newAct = Number((currentSell + expertDollars.activation_offset).toFixed(pricePrec));
        trailActivationEl.value = newAct;
      }
      // Stop loss (Van Tharp 1R below anchor — the entry price)
      const stopPxEl = m.querySelector('#sl-stop-px');
      if (stopPxEl && expertDollars.stop_loss_distance > 0 && anchorPx > 0) {
        const newStop = Number((anchorPx - expertDollars.stop_loss_distance).toFixed(pricePrec));
        // Only update if stop-loss is enabled (otherwise leave user's
        // manual value alone — they've opted out of the ATR-derived stop).
        if (stopLossToggle && stopLossToggle.checked) {
          stopPxEl.value = newStop;
        }
      }
      // Ratchet distance + activation (Le Beau chandelier + Van Tharp 0.5R)
      // Only affects hidden `draft` fields (no UI element); we mutate
      // draft directly so the save payload picks them up.
      if (expertDollars.ratchet_distance > 0) {
        draft.stop_loss_ratchet_distance = expertDollars.ratchet_distance;
      }
      if (expertDollars.ratchet_activation > 0) {
        draft.stop_loss_ratchet_activation = expertDollars.ratchet_activation;
      }
      // Reanchor threshold (Van Tharp SafeZone)
      if (expertDollars.reanchor_threshold > 0) {
        draft.reanchor_threshold = expertDollars.reanchor_threshold;
      }
      // Buy-trail distance (Le Beau 0.5×ATR / Kaufman 0.75×ATR)
      const btDistEl = m.querySelector('#sl-buy-trail-distance');
      if (btDistEl && expertDollars.buy_trail_distance > 0) {
        btDistEl.value = expertDollars.buy_trail_distance;
      }
      draft.buy_trail_distance = expertDollars.buy_trail_distance || draft.buy_trail_distance;
    }
    // Nudge the slider fills so anything watching them updates.
    if (typeof updateFillPct === 'function') {
      updateFillPct(profitEl);
      updateFillPct(tdSliderEl);
    }
  }

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
    // Entry-basis stamp: unrealized P&L for a sleeve should be measured from
    // when the STRATEGY started tracking, not the position's overall avg entry
    // (which reflects buys that predate the sleeve). Stamp once at create time;
    // preserve on edits so the historical basis doesn't reset when you tweak
    // targets. Existing pre-stamp sleeves get backfilled with current mark.
    const existingEntryMark = existing ? Number(draft.entry_mark) : 0;
    let entryMark = existingEntryMark > 0 ? existingEntryMark : (Number(mark) || 0);
    // Read qty ONCE, coerce blank/invalid to existing.qty (edit) or 1 (new).
    // Users hit "Contracts must be at least 1" when they cleared the input
    // while just trying to change the preset — auto-fill instead of erroring.
    const rawQty = parseInt(qtyEl.value, 10);
    const safeQty = Number.isFinite(rawQty) && rawQty >= 1
      ? rawQty
      : (existing ? Number(existing.qty) || 1 : 1);
    // If qty INCREASED on an existing sleeve, the newly-added contracts enter
    // at the current mark (they weren't part of the original entry). Weighted-
    // average old-qty at old-basis + added-qty at current-mark so the sleeve's
    // unrealized doesn't multiply by qty. If qty decreased, keep basis as-is.
    const oldQty = existing ? (Number(draft.qty) || 0) : 0;
    if (existing && safeQty > oldQty && oldQty > 0 && existingEntryMark > 0 && mark > 0) {
      const added = safeQty - oldQty;
      entryMark = (oldQty * existingEntryMark + added * mark) / safeQty;
    }
    const entryTs = existing && Number(draft.entry_ts) > 0
      ? Number(draft.entry_ts)
      : Math.floor(Date.now() / 1000);
    const patch = {
      id: draft.id,
      name: nameEl.value || draft.id,
      qty: safeQty,
      exit_mode: exitEl.value,
      sell_px: sellPx,
      buy_px: buyPx,
      entry_mark: entryMark,
      entry_ts: entryTs,
      trail_trigger: sellPx,
      trail_distance: usesTrail ? trailDistance : Math.max(0.02, (sellPx - buyPx) / 4),
      // Auto-bump trail_activation above sell_px so the server's invariant
      // (act > sell) always holds. Users hit save failures when they changed
      // the sell target but the activation input was equal or below — this
      // silently lifts it to sell + 0.05 rather than making them re-align.
      trail_activation_px: exitEl.value === 'hybrid'
        ? (trailActivation > sellPx ? trailActivation : sellPx + 0.05)
        : (sellPx + 0.5),
      hybrid_delay_secs: exitEl.value === 'hybrid' ? hybridDelay : 5.0,
      reanchor_threshold: draft.reanchor_threshold,
      time_reanchor_secs: draft.time_reanchor_secs || 0,
      vol_reanchor_percentile: draft.vol_reanchor_percentile || 0,
      vol_reanchor_window: draft.vol_reanchor_window || 60,
      accumulate_enabled: accumulateEnabled,
      max_qty: accumulateEnabled ? parseInt(maxQtyEl?.value || 0, 10) : 0,
      scale_up_buffer_mult: accumulateEnabled ? Number(scaleBufEl?.value || 1.5) : 1.5,
      stop_loss_enabled: stopLossEnabled,
      stop_loss_px: stopLossEnabled ? stopPx : 0,
      stop_loss_qty_mode: stopLossEnabled ? stopMode : 'all',
      stop_loss_qty_custom: stopLossEnabled && stopMode === 'custom'
        ? parseInt(stopQtyEl?.value || 1, 10) : 0,
      // Ratchet + re-entry + microstructure + news blackout come from the
      // draft (populated by applyPreset for Model B). No UI toggle for
      // these yet — presets are the primary interface. Reading from draft
      // lets Model presets flow through the save without adding form fields.
      stop_loss_ratchet_enabled: !!draft.stop_loss_ratchet_enabled,
      stop_loss_ratchet_distance: Number(draft.stop_loss_ratchet_distance) || 1.50,
      stop_loss_ratchet_activation: Number(draft.stop_loss_ratchet_activation) || 0.50,
      stop_loss_reanchor_on_trigger: !!draft.stop_loss_reanchor_on_trigger,
      stop_loss_max_consecutive: parseInt(draft.stop_loss_max_consecutive || 0, 10),
      reentry_mode: String(draft.reentry_mode || 'off'),
      reentry_range_contraction: Number(draft.reentry_range_contraction) || 0.5,
      reentry_range_window: parseInt(draft.reentry_range_window || 60, 10),
      reentry_min_wait_secs: Number(draft.reentry_min_wait_secs) || 30,
      news_blackout_enabled: !!draft.news_blackout_enabled,
      news_blackout_tier: parseInt(draft.news_blackout_tier || 2, 10),
      microstructure_gate_enabled: !!draft.microstructure_gate_enabled,
      stop_loss_protect_realized_enabled: !!draft.stop_loss_protect_realized_enabled,
      stop_loss_protect_realized_frac: Number(draft.stop_loss_protect_realized_frac) || 0.5,
      entry_trend_filter_enabled: !!draft.entry_trend_filter_enabled,
      entry_trend_sma_window: parseInt(draft.entry_trend_sma_window || 20, 10),
      post_trail_reentry_mode: String(draft.post_trail_reentry_mode || 'off'),
      post_trail_stage_b_max_wait_secs: Number(draft.post_trail_stage_b_max_wait_secs) || 3600,
      // Maker-only + penny-inside (see swing_leg._sleeve_arm). Cheaper fees +
      // better queue position vs other bots at the same price level.
      post_only_enabled: !!draft.post_only_enabled,
      penny_inside_enabled: !!draft.penny_inside_enabled,
      penny_inside_max_ticks: parseInt(draft.penny_inside_max_ticks || 5, 10),
      // Book-imbalance gate (Chan/Harris): don't fight the tape.
      book_imbalance_gate_enabled: !!draft.book_imbalance_gate_enabled,
      book_imbalance_depth_levels: parseInt(draft.book_imbalance_depth_levels || 5, 10),
      book_imbalance_sell_threshold: Number(draft.book_imbalance_sell_threshold) || 0.65,
      book_imbalance_buy_threshold: Number(draft.book_imbalance_buy_threshold) || 0.65,
      // Trailing buy (Livermore / Turtle / Le Beau) — don't grab a falling
      // knife. See swing_leg._trailing_buy_ready for the state machine.
      buy_trail_enabled: !!(m.querySelector('#sl-buy-trail')?.checked),
      buy_trail_distance: (m.querySelector('#sl-buy-trail')?.checked
        ? Number(m.querySelector('#sl-buy-trail-distance')?.value || 0)
        : 0),
      // Seven-feature batch — persist whatever the draft holds. These have
      // no UI toggles yet (Model B preset + new-sleeve default sets them
      // ON; existing sleeves preserve saved values). Adding UI toggles is
      // a follow-up build.
      funding_gate_enabled: !!draft.funding_gate_enabled,
      funding_gate_threshold: Number(draft.funding_gate_threshold) || 0.0005,
      kelly_enabled: !!draft.kelly_enabled,
      kelly_fraction: Number(draft.kelly_fraction) || 0.25,
      kelly_min_cycles: parseInt(draft.kelly_min_cycles || 8, 10),
      adaptive_spread_enabled: !!draft.adaptive_spread_enabled,
      adaptive_spread_max_multiplier: Number(draft.adaptive_spread_max_multiplier) || 2.0,
      adaptive_spread_vol_window_secs: Number(draft.adaptive_spread_vol_window_secs) || 300,
      crossex_gate_enabled: !!draft.crossex_gate_enabled,
      crossex_max_divergence_pct: Number(draft.crossex_max_divergence_pct) || 1.0,
      correlation_dynamic_enabled: !!draft.correlation_dynamic_enabled,
      correlation_dynamic_threshold: Number(draft.correlation_dynamic_threshold) || 0.6,
      ml_shadow_enabled: !!draft.ml_shadow_enabled,
      ml_signal_threshold: Number(draft.ml_signal_threshold) || 0.3,
      classic_indicators_shadow_enabled: !!draft.classic_indicators_shadow_enabled,
    };
    if (!(patch.qty >= 1)) { errEl.hidden = false; errEl.innerHTML = 'Contracts must be at least 1'; return; }
    if (!(buyPx < sellPx)) { errEl.hidden = false; errEl.innerHTML = 'Buy target must be below sell target'; return; }
    // Off-market sell guard: refuse to save a sleeve whose sell target is
    // BELOW current mark by more than 2 ticks — that sleeve fires the sell
    // immediately at the wrong side of the intended spread. Confirmable
    // via a second click if the user really means it (e.g., 'sell now
    // regardless' style config).
    const tickForGuard = Number(cfg?.tick_size) || 0.005;
    if (mark > 0 && sellPx < (mark - 2 * tickForGuard) && !m.dataset.confirmOffMarket) {
      errEl.hidden = false;
      errEl.innerHTML = `Sell target $${fmtPrice(sellPx)} is below current market $${fmtPrice(mark)} — sleeve will fire sell immediately at a worse price than intended. Click Save again to confirm.`;
      m.dataset.confirmOffMarket = '1';
      return;
    }
    delete m.dataset.confirmOffMarket;
    if (usesTrail && !(trailDistance > 0)) { errEl.hidden = false; errEl.innerHTML = 'Trail distance must be > 0'; return; }
    if (exitEl.value === 'hybrid') {
      // trail_activation is auto-lifted above sell_px in the patch above, so
      // no need to gate the save on it here. Only guard the delay.
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
    if (res.ok) {
      m.hidden = true;
      showToast(`${existing ? 'updated' : 'attached'} ${patch.name} → ${symbol}`, 'info');
      // Poll for the refreshed store, then reopen the product-detail modal
      // so the user immediately SEES the new sleeve in the "Attached
      // strategies" table. Silent close made it feel like nothing happened.
      await refreshOnce();
      if (portfolioContext && isLiveTenant(tenant)) {
        openScannerDetail({
          product_id: symbol, price: portfolioContext.mark || 0,
          high_24h: portfolioContext.mark || 0,
          low_24h: portfolioContext.mark || 0,
          vol_pct: 0,
          _live_tenant: tenant,
          _live_avg: portfolioContext.avg || 0,
          _live_qty: portfolioContext.qty || 0,
          _live_side: portfolioContext.side || '',
        });
      }
    }
    else {
      errEl.hidden = false;
      errEl.innerHTML = escapeHtml(res.error || 'save failed') +
        (res.issues ? '<ul>' + res.issues.map(i => `<li>${escapeHtml(i.field)}: ${escapeHtml(i.message)}</li>`).join('') + '</ul>' : '');
      showToast(res.error || 'save failed', 'error');
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
  // Dedicated delete endpoint — server just filters out the target id and
  // writes without re-validating survivors. That way a bad stored field on
  // some OTHER sleeve doesn't block this delete.
  const res = await postJson('/api/sleeves/delete', { tenant, symbol, sleeve_id: sleeveId });
  if (res._unauthorized) { showLogin(); return; }
  if (res.ok) { refreshOnce(); showToast('strategy deleted', 'info'); }
  else {
    // Surface the actual reason (server issue list wins over generic error).
    const detail = res.issues ? res.issues.map(i => `${i.field}: ${i.message}`).join('; ') : (res.error || 'delete failed');
    showToast(detail, 'error');
  }
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

  // Shorting enabled: SELL can exceed position — anything past `pos` opens a
  // short. The core-floor still WARNS but doesn't block, since the user
  // explicitly asked for shorting to be an option. Max qty is now the margin
  // ceiling for either side (buys use margin, shorts also use margin).
  const marginCap = Math.floor(availMargin / marginPer);
  const maxTradable = Math.max(1, Math.min(100, marginCap));
  const maxSellUncovered = Math.max(0, pos - core);  // sell within long, respects core

  tradeModalTitle.textContent = `${side === 'BUY' ? 'Buy / Long' : 'Sell / Short'} — ${symbol}`;
  const positionLine = pos > 0
    ? `You hold <b style="color:var(--text)">${pos} contract${pos === 1 ? '' : 's'} LONG</b>` + (core > 0 ? ` · core floor: <b style="color:var(--text)">${core}</b>` : '')
    : pos < 0
      ? `You are <b style="color:var(--text)">SHORT ${Math.abs(pos)} contract${Math.abs(pos) === 1 ? '' : 's'}</b>`
      : `You hold <b style="color:var(--text)">0 contracts</b>`;
  tradeModalBody.innerHTML = `
    <div style="line-height:1.7; color: var(--muted); font-size: 14px;">
      <div>${positionLine}</div>
      <div>${escapeHtml(symbolLabel(symbol))} market now: <b style="color:var(--text)">$${fmtPrice(mark)}</b> — bid $${fmtPrice(snap.best_bid)} / ask $${fmtPrice(snap.best_ask)}</div>
    </div>
  `;

  const max = maxTradable;
  tradeQty.max = max;
  tradeQty.value = Math.max(1, Math.min(1, max) || 1);
  tradeQty.disabled = max < 1;
  if (max < 1) {
    tradeConfirm.disabled = true;
    tradeError.hidden = false;
    tradeError.innerHTML = `<b>Not enough margin.</b> Deposit more or reduce open positions.`;
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

  // Max note — tells the user WHERE the limit comes from, and warns about the
  // long→short boundary if this SELL will breach it.
  const maxNote = document.getElementById('trade-max-note');
  if (side === 'SELL') {
    const partsFor = (qty) => {
      const closeLong = Math.min(qty, Math.max(0, pos));
      const openShort = qty - closeLong;
      return { closeLong, openShort };
    };
    if (pos > 0 && maxSellUncovered > 0) {
      maxNote.innerHTML = `up to ${max} · first ${maxSellUncovered} close long${core > 0 ? ` (respects core ${core})` : ''}, rest open short`;
    } else if (pos > 0) {
      maxNote.innerHTML = `up to ${max} · opens short beyond your ${pos} long${core > 0 ? ` (core floor blocks selling into ${core})` : ''}`;
    } else {
      maxNote.innerHTML = `up to ${max} · opens a new short position`;
    }
  } else {
    maxNote.textContent = `up to ${max} (limited by ~$${availMargin.toLocaleString('en-US', { maximumFractionDigits: 0 })} available margin)`;
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

  // Shorting enabled: newPos < 0 is a valid short position, no longer a
  // refusal. Core-floor breach STILL warns (protects a long-side reserve
  // the user configured) but does not block — the user knows what they're
  // doing when they explicitly click Sell. Errors below are informational
  // gates; only bad-limit blocks the confirm.
  const willBreachCore = side === 'SELL' && pos > core && newPos < core;
  const willOpenShort = side === 'SELL' && newPos < 0 && pos >= 0;
  const willIncreaseShort = side === 'SELL' && pos < 0;
  const badLimit = orderType === 'limit' && !(limitPrice > 0);
  tradeError.hidden = !(badLimit || willBreachCore || willOpenShort || willIncreaseShort);
  tradeConfirm.disabled = qty < 1 || badLimit;
  if (badLimit) {
    tradeError.innerHTML = `<b>Enter a limit price above 0.</b>`;
  } else if (willOpenShort) {
    const shortDepth = Math.abs(newPos);
    tradeError.innerHTML = `<b>Opens SHORT position:</b> you hold ${pos} long, selling ${qty} closes those and opens a ${shortDepth}-contract short. Shorts have unbounded downside — silver rallying means you lose margin.`;
  } else if (willIncreaseShort) {
    tradeError.innerHTML = `<b>Adds to your short:</b> you're already short ${Math.abs(pos)}. This sell takes you to ${Math.abs(newPos)} short.`;
  } else if (willBreachCore) {
    tradeError.innerHTML = `<b>Below core floor:</b> selling ${qty} takes you to ${newPos} contracts, below your configured core floor of ${core}. Proceeding is allowed but violates your protected-core reserve.`;
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
let scannerChartWindow = { days: 1, granularity: 'FIVE_MINUTE' };

const TIMEFRAMES = [
  { label: '5m',  minutes: 5,   granularity: 'ONE_MINUTE' },
  { label: '30m', minutes: 30,  granularity: 'ONE_MINUTE' },
  { label: '1H',  minutes: 60,  granularity: 'FIVE_MINUTE' },
  { label: '1D',  days: 1,      granularity: 'FIVE_MINUTE' },
  { label: '7D',  days: 7,      granularity: 'FIVE_MINUTE' },
  { label: '30D', days: 30,     granularity: 'ONE_HOUR' },
];

function openScannerDetail(row) {
  scannerDetailContext = row;
  scannerChartWindow = { days: 1, granularity: 'FIVE_MINUTE' };
  scannerDetailTitle.textContent = prettyProductName(row.product_id);
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
  // Contract-info strip: shows the specs the trader needs to size a position
  // (tick, contract size, margin, expiration) alongside the price bar. Sourced
  // straight from Coinbase's get_products response via scanner.compute_ranking
  // so nothing extra is fetched when the modal opens.
  const specParts = [];
  if (row.contract_size) specParts.push(`Size <b>${row.contract_size}/ct</b>`);
  if (row.tick_size) specParts.push(`Tick <b>$${fmtNum(row.tick_size, 5)}</b>`);
  if (row.tick_value) specParts.push(`Tick value <b>$${fmtNum(row.tick_value, 3)}</b>`);
  if (row.intraday_margin_rate)
    specParts.push(`Intraday margin <b>${fmtNum(row.intraday_margin_rate * 100, 2)}%</b>`);
  if (row.contract_expiry) {
    const exp = String(row.contract_expiry).slice(0, 10);
    const daysToExp = Math.round((new Date(row.contract_expiry) - Date.now()) / 86400000);
    specParts.push(`Expires <b>${escapeHtml(exp)}</b>${daysToExp >= 0 ? ` <span class="dim">(${daysToExp}d)</span>` : ''}`);
  }
  const specStrip = specParts.length
    ? `<div class="scanner-detail-specs">${specParts.join(' <span class="dim">·</span> ')}</div>`
    : '';

  // When opened from a Live portfolio row, show the user's position + an
  // "Attach strategy" button so they can jump straight into applying a Model.
  const liveStrip = row._live_tenant ? `
    <div class="scanner-detail-live">
      <div class="sdl-hold">Position: <b>${row._live_qty}</b> ${escapeHtml(row._live_side || 'LONG')} · avg <b class="mono">$${fmtNum(row._live_avg, 4)}</b></div>
      <button class="small primary" id="scanner-detail-attach-strategy"
              data-tenant="${escapeHtml(row._live_tenant)}"
              data-symbol="${escapeHtml(row.product_id)}"
              data-mark="${row.price}" data-avg="${row._live_avg}"
              data-pos-qty="${row._live_qty}" data-side="${escapeHtml(row._live_side || '')}">
        + Attach strategy
      </button>
    </div>
  ` : '';

  // Attached strategies for this product — read straight from the store's
  // config.sleeves for (tenant, symbol). Lets you SEE and MANAGE the sleeves
  // you've attached without needing a full strategy card below the portfolio.
  const liveSleeves = row._live_tenant
    ? ((currentStore[row._live_tenant]?.[row.product_id]?.config?.sleeves) || [])
    : [];
  const liveSleeveStates = row._live_tenant
    ? (currentStore[row._live_tenant]?.[row.product_id]?.state?.sleeves || {})
    : {};
  // Contract size + current mark needed to compute per-sleeve unrealized.
  const liveContractSize = row._live_tenant
    ? Number(currentStore[row._live_tenant]?.[row.product_id]?.config?.contract_size) || 50
    : 50;
  const liveMarkForSleeves = Number(row.price) || Number(row._live_avg) || 0;
  const sleevesStrip = liveSleeves.length ? `
    <div class="scanner-detail-sleeves">
      <div class="scanner-detail-sleeves-head">Attached strategies (${liveSleeves.length})</div>
      <table class="scanner-detail-sleeves-table">
        <thead><tr><th>Name</th><th>Contracts</th><th title="Your position's weighted-avg cost for the underlying contracts">Pos Avg</th><th title="Price at which THIS strategy was attached">Entry</th><th>Sell</th><th>Buy</th>
          <th title="Effective stop-loss price. If the ratchet is armed and above the base stop, this shows the ratcheted floor.">Stop Loss</th>
          <th title="Total P&amp;L this cycle if the stop-loss fires now: banked realized + (effective_stop − basis) × contract_size × qty − sell fee.">If Stopped</th>
          <th title="Trail activation. Grey = not yet armed (shows the arm price). Green = armed, showing peak / effective sell floor.">Trail</th>
          <th>Cycles</th><th>Unrealized</th><th>Realized</th><th>State</th><th></th></tr></thead>
        <tbody>
        ${liveSleeves.map(s => {
          const ss = liveSleeveStates[s.id] || {};
          const state = String(ss.state || 'ARMED_SELL');
          const realized = Number(ss.realized_pnl) || 0;
          // Per-sleeve unrealized = P/L since THIS strategy was attached.
          // Prefer own_avg_entry (sleeve has actually traded, so it has a
          // real basis); otherwise use entry_mark (mark at attach time) so
          // multiple sleeves on the same position show DIFFERENT numbers,
          // reflecting each strategy's own attach point. Falling back to
          // pos_avg would make every sleeve identical.
          const cycles = Number(ss.cycles) || 0;
          let unrealized = 0;
          // Rule (Adam 2026-07-13): sleeve UNREALIZED is $0 until cycles > 0.
          // A freshly-attached sleeve inherits its contracts from the parent
          // position; the paper move on those pre-attach lots belongs to the
          // position, not the sleeve — never double-counted on the sleeve row.
          if (state === 'ARMED_SELL' && cycles > 0) {
            const sell = Number(s.sell_px) || 0;
            const buy = Number(s.buy_px) || 0;
            const midpoint = (sell > 0 && buy > 0) ? (sell + buy) / 2 : 0;
            const basis = Number(ss.own_avg_entry)
              || Number(s.entry_mark)
              || midpoint
              || Number(row._live_avg)
              || 0;
            if (basis > 0 && liveMarkForSleeves > 0) {
              unrealized = (liveMarkForSleeves - basis) * liveContractSize * Number(s.qty);
            }
          }
          const entryPx = Number(s.entry_mark) || 0;
          const posAvgPx = Number(row._live_avg) || 0;
          // Effective stop-loss: base is stop_loss_px, but if the ratchet is
          // enabled AND armed (hwm exists), the floor lifts to hwm − distance.
          // Show whichever is higher — that's what the trader will actually
          // fire on. Ratchet+base bracket the same event; showing max means
          // you always see the closer trigger.
          let stopCell = '<span class="dim">off</span>';
          if (s.stop_loss_enabled) {
            const baseStop = Number(s.stop_loss_px) || 0;
            const hwm = Number(ss.stop_loss_hwm) || 0;
            const ratchetDist = Number(s.stop_loss_ratchet_distance) || 0;
            // Ratchet only actually applies once unrealized/contract crosses
            // stop_loss_ratchet_activation — see swing_leg._sleeve_effective_stop.
            // Without this activation check, the dashboard shows a "ratcheted"
            // stop even when the sleeve is underwater and the ratchet doesn't
            // apply — misleading users into thinking the effective stop is
            // higher than it actually is.
            const activation = Number(s.stop_loss_ratchet_activation) || 0;
            const ownAvgEntry = Number(ss.own_avg_entry) || 0;
            const unrealizedPerContract = ownAvgEntry > 0 ? hwm - ownAvgEntry : 0;
            const ratchetArmed = ownAvgEntry > 0 && unrealizedPerContract >= activation;
            const ratchetedFloor = (s.stop_loss_ratchet_enabled && hwm > 0 && ratchetDist > 0 && ratchetArmed)
              ? hwm - ratchetDist
              : 0;
            const effective = Math.max(baseStop, ratchetedFloor);
            if (effective > 0) {
              const ratcheted = ratchetedFloor > baseStop;
              stopCell = ratcheted
                ? `<span class="mono" title="Ratchet armed — floor lifted from base $${fmtPrice(baseStop)} to $${fmtPrice(effective)} (peak $${fmtPrice(hwm)} − $${fmtPrice(ratchetDist)})">$${fmtPrice(effective)} <span class="sl-ratchet-badge">↑</span></span>`
                : `<span class="mono" title="Base stop-loss (ratchet not yet armed)">$${fmtPrice(effective)}</span>`;
            }
          }
          // If-stopped projection: realized (already banked this cycle) plus
          // per-contract stop-out P&L (effective_stop − sleeve basis) × size × qty,
          // minus the sell-side fee. Only shown when the sleeve actually holds
          // contracts (own_avg_entry > 0) — a flat sleeve has nothing to stop
          // out of, so projecting against entry_mark would fabricate a scary
          // loss for a position that doesn't exist.
          let ifStoppedCell = '<span class="dim">—</span>';
          const ownAvg2 = Number(ss.own_avg_entry) || 0;
          if (s.stop_loss_enabled && ownAvg2 > 0) {
            const baseStop2 = Number(s.stop_loss_px) || 0;
            const hwm2 = Number(ss.stop_loss_hwm) || 0;
            const ratchetDist2 = Number(s.stop_loss_ratchet_distance) || 0;
            const activation2 = Number(s.stop_loss_ratchet_activation) || 0;
            const unrl2 = hwm2 - ownAvg2;
            const armed2 = unrl2 >= activation2;
            const floor2 = (s.stop_loss_ratchet_enabled && hwm2 > 0 && ratchetDist2 > 0 && armed2) ? hwm2 - ratchetDist2 : 0;
            const eff2 = Math.max(baseStop2, floor2);
            if (eff2 > 0) {
              const qty2 = Number(s.qty) || 0;
              const cfg2 = currentStore[row._live_tenant]?.[row.product_id]?.config || {};
              const sellFee = Number(cfg2.fee_per_fill_sell) || ((Number(cfg2.fee_per_contract_roundtrip) || 0) / 2);
              const stopPnl = (eff2 - ownAvg2) * liveContractSize * qty2;
              const projected = realized + stopPnl - sellFee * qty2;
              ifStoppedCell = `<span class="mono ${projected >= 0 ? 'pos' : 'neg'}" title="Realized ${fmtMoney(realized)} + stop-out (${fmtPrice(eff2)}−${fmtPrice(ownAvg2)})×${liveContractSize}×${qty2} − sell fee $${(sellFee * qty2).toFixed(2)}">${projected >= 0 ? '+' : ''}${fmtMoney(projected)}</span>`;
            }
          }
          // Trail cell — grey pill with arm price when trail isn't engaged;
          // green pill with peak + effective stop when it is. Same source as
          // the sleeve-editor trail-status block: state.trail_high_water_price
          // > 0 means the trail is armed (state.trail_armed on the primary,
          // but sleeves persist this on trail_high_water_price only).
          let trailCell = '<span class="dim">—</span>';
          const trailModes = s.exit_mode === 'trailing_stop' || s.exit_mode === 'hybrid';
          if (trailModes) {
            const peak = Number(ss.trail_high_water_price) || 0;
            const trailDist = Number(s.trail_distance) || 0;
            const armPx = Number(s.trail_activation_px) || Number(s.sell_px) || 0;
            if (peak > 0 && trailDist > 0) {
              const effectiveStop = peak - trailDist;
              trailCell = `<span class="mono trail-pill-on" title="Trail engaged — peak $${fmtPrice(peak)}, sells on $${fmtPrice(trailDist)} pullback (effective stop $${fmtPrice(effectiveStop)})">$${fmtPrice(effectiveStop)} <span class="trail-peak-note">$${fmtPrice(peak)}</span></span>`;
            } else if (armPx > 0) {
              trailCell = `<span class="mono trail-pill-off" title="Not yet activated — trail arms once price crosses $${fmtPrice(armPx)}">$${fmtPrice(armPx)}</span>`;
            }
          }
          // Halt reason lives on the product-level state (not per-sleeve) when
          // the state machine crashes out (abort_below, reconcile). Show it as
          // a tooltip on the HALTED pill so Adam knows why to click Resume.
          const productHaltReason = state === 'HALTED'
            ? (currentStore[row._live_tenant]?.[row.product_id]?.state?.halt_reason
               || ss.halt_reason || 'halted — check bot log')
            : null;
          const stateCell = productHaltReason
            ? `<span class="status-pill ${state.toLowerCase()}" title="${escapeHtml(productHaltReason)}">${escapeHtml(prettyState(state))}</span><div class="halt-why-inline">${escapeHtml(productHaltReason)}</div>`
            : `<span class="status-pill ${state.toLowerCase()}">${escapeHtml(prettyState(state))}</span>`;
          const resumeBtn = state === 'HALTED'
            ? `<button class="small primary" data-action="resume-live-strategy"
                       data-tenant="${escapeHtml(row._live_tenant)}"
                       data-symbol="${escapeHtml(row.product_id)}">Resume</button>`
            : '';
          return `<tr>
            <td><b>${escapeHtml(s.name || s.id || '')}</b></td>
            <td class="mono">${s.qty}</td>
            <td class="mono dim">${posAvgPx > 0 ? `$${fmtPrice(posAvgPx)}` : '—'}</td>
            <td class="mono">${entryPx > 0 ? `$${fmtPrice(entryPx)}` : '<span class="dim">—</span>'}</td>
            <td class="mono">$${fmtPrice(s.sell_px || 0)}</td>
            <td class="mono">$${fmtPrice(s.buy_px || 0)}</td>
            <td class="mono">${stopCell}</td>
            <td class="mono">${ifStoppedCell}</td>
            <td class="mono">${trailCell}</td>
            <td class="mono">${Number(ss.cycles) || 0}</td>
            <td class="mono ${unrealized >= 0 ? 'pos' : 'neg'}">${unrealized >= 0 ? '+' : ''}${fmtMoney(unrealized)}</td>
            <td class="mono ${realized >= 0 ? 'pos' : 'neg'}">${realized >= 0 ? '+' : ''}${fmtMoney(realized)}</td>
            <td>${stateCell}</td>
            <td class="sleeve-row-actions">
              ${resumeBtn}
              <button class="small" data-action="edit-live-sleeve"
                      data-tenant="${escapeHtml(row._live_tenant)}"
                      data-symbol="${escapeHtml(row.product_id)}"
                      data-sleeve-id="${escapeHtml(s.id)}">Edit</button>
              <button class="small ghost" data-action="delete-sleeve"
                      data-tenant="${escapeHtml(row._live_tenant)}"
                      data-symbol="${escapeHtml(row.product_id)}"
                      data-sleeve-id="${escapeHtml(s.id)}">Remove</button>
            </td>
          </tr>`;
        }).join('')}
        </tbody>
      </table>
    </div>
  ` : '';

  const liveIndicator = row._live_tenant
    ? `<span class="scanner-detail-price-live">LIVE</span>` : '';
  scannerDetailSummary.innerHTML = `
    <div class="scanner-detail-price">
      <span class="mono">$${fmtNum(row.price, 4)}</span>
      ${liveIndicator}
      <span class="scanner-detail-range">24h <span class="pos">$${fmtNum(row.high_24h, 4)}</span> / <span class="neg">$${fmtNum(row.low_24h, 4)}</span></span>
      <span class="scanner-detail-vol"><b>${fmtNum(row.vol_pct, 2)}%</b> range</span>
    </div>
    ${specStrip}
    ${liveStrip}
    ${sleevesStrip}
  `;
  // Wire the attach-strategy button after innerHTML replaces the DOM node.
  const attachBtn = document.getElementById('scanner-detail-attach-strategy');
  if (attachBtn) {
    attachBtn.onclick = () => {
      const ctx = {
        mark: Number(attachBtn.dataset.mark) || 0,
        avg: Number(attachBtn.dataset.avg) || 0,
        qty: Number(attachBtn.dataset.posQty) || 0,
        side: attachBtn.dataset.side || '',
      };
      scannerDetailModal.hidden = true;
      openSleeveEditor(attachBtn.dataset.tenant, attachBtn.dataset.symbol, null, null, ctx);
    };
  }

  // Fetch a fresh mark if the row didn't have one. Happens on WAITING rows
  // (position=0 so the primary loop wasn't updating the snapshot for that
  // symbol). Kick a candles request for 1 bar at 1min granularity — the
  // paper worker fills it via Coinbase's get_candles and Redis pushes back
  // the response. Non-blocking; the modal is fully usable while this loads.
  if (!(Number(row.price) > 0) && row.product_id) {
    fetch(`/api/candles?product_id=${encodeURIComponent(row.product_id)}&granularity=ONE_MINUTE&minutes=5`)
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (!data || !Array.isArray(data.candles) || !data.candles.length) return;
        // Coinbase returns descending; last candle in the sorted list is
        // whatever the response gave us — take max ts for safety.
        const latest = data.candles.reduce((a, b) => (Number(a.ts) || 0) > (Number(b.ts) || 0) ? a : b);
        const fresh = Number(latest.close) || 0;
        if (fresh > 0 && scannerDetailContext && scannerDetailContext.product_id === row.product_id) {
          scannerDetailContext.price = fresh;
          const priceSpan = scannerDetailSummary.querySelector('.scanner-detail-price .mono');
          if (priceSpan) priceSpan.textContent = '$' + fmtNum(fresh, 4);
        }
      })
      .catch(() => { /* silent — modal stays usable */ });
  }

  // Timeframe buttons
  scannerDetailTimeframes.innerHTML = '';
  for (const tf of TIMEFRAMES) {
    const b = document.createElement('button');
    b.type = 'button';
    const isActive = tf.minutes != null
      ? tf.minutes === scannerChartWindow.minutes
      : tf.days === scannerChartWindow.days && scannerChartWindow.minutes == null;
    b.className = 'timeframe-btn' + (isActive ? ' active' : '');
    b.textContent = tf.label;
    b.onclick = () => {
      scannerChartWindow = tf.minutes != null
        ? { minutes: tf.minutes, granularity: tf.granularity }
        : { days: tf.days, granularity: tf.granularity };
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

// Real-time refresh for the drill-down modal. Called from refreshOnce() every
// POLL_MS. Re-reads the latest mark/avg from currentStore for the currently
// open symbol and rewrites just the price bar + sleeves table (not the chart —
// re-fetching candles every 5s would flap the chart and hammer Coinbase).
function refreshScannerDetailLive() {
  if (!scannerDetailModal || scannerDetailModal.hidden) return;
  const ctx = scannerDetailContext;
  if (!ctx || !ctx._live_tenant) return;  // only Live-portfolio rows get live-refreshed
  const tenant = ctx._live_tenant;
  const symbol = ctx.product_id;
  // Fresh mark: prefer __portfolio__ snap (updated by sync every 15s); fall
  // back to same-symbol snapshot on any tenant (paper/lab keep last_mark warm).
  let mark = 0, avg = Number(ctx._live_avg) || 0, qty = Number(ctx._live_qty) || 0;
  let high = Number(ctx.high_24h) || 0, low = Number(ctx.low_24h) || 0;
  const pfSnap = currentStore?.[tenant]?.['__portfolio__']?.config;
  const posRow = (pfSnap?.derivatives || []).find(d => d.product_id === symbol);
  if (posRow) {
    mark = Number(posRow.mark) || 0;
    avg = Number(posRow.avg_entry) || avg;
    qty = Number(posRow.qty) || qty;
  }
  if (!mark) {
    for (const t of Object.keys(currentStore || {})) {
      const s = currentStore[t]?.[symbol]?.snapshot;
      if (s && Number(s.last_mark) > 0) { mark = Number(s.last_mark); break; }
    }
  }
  if (mark > 0) {
    ctx.price = mark;
    ctx._live_avg = avg;
    ctx._live_qty = qty;
  }
  // Rebuild just the price line to avoid re-drawing chart/timeframes.
  const priceEl = scannerDetailSummary.querySelector('.scanner-detail-price');
  if (priceEl) {
    priceEl.innerHTML = `
      <span class="mono">$${fmtNum(mark || ctx.price || 0, 4)}</span>
      <span class="scanner-detail-price-live">LIVE</span>
      <span class="scanner-detail-range">24h <span class="pos">$${fmtNum(high, 4)}</span> / <span class="neg">$${fmtNum(low, 4)}</span></span>
      <span class="scanner-detail-vol"><b>${fmtNum(ctx.vol_pct || 0, 2)}%</b> range</span>
    `;
  }
  // Rebuild the sleeves table so Unrealized reflects the new mark. Same logic
  // as the initial render — but reads from the latest currentStore.
  const sleeves = currentStore?.[tenant]?.[symbol]?.config?.sleeves || [];
  const sleeveStates = currentStore?.[tenant]?.[symbol]?.state?.sleeves || {};
  const contractSize = Number(currentStore?.[tenant]?.[symbol]?.config?.contract_size) || 50;
  const markForSleeves = mark || Number(ctx.price) || 0;
  const tbody = scannerDetailSummary.querySelector('.scanner-detail-sleeves-table tbody');
  if (tbody && sleeves.length) {
    tbody.innerHTML = sleeves.map(s => {
      const ss = sleeveStates[s.id] || {};
      const state = String(ss.state || 'ARMED_SELL');
      const realized = Number(ss.realized_pnl) || 0;
      const cycles = Number(ss.cycles) || 0;
      let unrealized = 0;
      // Rule (Adam 2026-07-13): sleeve UNREALIZED is $0 until the sleeve has
      // completed at least one buy cycle. A freshly-attached sleeve inherits
      // its contracts from the parent position — the paper move on those
      // pre-attach lots belongs to the position, not the sleeve.
      if (state === 'ARMED_SELL' && cycles > 0) {
        const sell = Number(s.sell_px) || 0;
        const buy = Number(s.buy_px) || 0;
        const midpoint = (sell > 0 && buy > 0) ? (sell + buy) / 2 : 0;
        const basis = Number(ss.own_avg_entry)
          || Number(s.entry_mark)
          || midpoint
          || Number(avg)
          || 0;
        if (basis > 0 && markForSleeves > 0) {
          unrealized = (markForSleeves - basis) * contractSize * Number(s.qty);
        }
      }
      const entryPx = Number(s.entry_mark) || 0;
      const posAvgPx = Number(avg) || 0;
      // Stop-loss cell — mirrors the initial-render logic so column stays
      // consistent across ticks. Without this the price-tick rebuild produced
      // one fewer td than the header, shifting every following column left.
      let stopCell = '<span class="dim">off</span>';
      let ifStoppedCell = '<span class="dim">—</span>';
      if (s.stop_loss_enabled) {
        const baseStop = Number(s.stop_loss_px) || 0;
        const hwm = Number(ss.stop_loss_hwm) || 0;
        const ratchetDist = Number(s.stop_loss_ratchet_distance) || 0;
        const activation = Number(s.stop_loss_ratchet_activation) || 0;
        const ownAvgEntry = Number(ss.own_avg_entry) || 0;
        const unrealizedPerContract = ownAvgEntry > 0 ? hwm - ownAvgEntry : 0;
        const ratchetArmed = ownAvgEntry > 0 && unrealizedPerContract >= activation;
        const ratchetedFloor = (s.stop_loss_ratchet_enabled && hwm > 0 && ratchetDist > 0 && ratchetArmed)
          ? hwm - ratchetDist
          : 0;
        const effective = Math.max(baseStop, ratchetedFloor);
        if (effective > 0) {
          const ratcheted = ratchetedFloor > baseStop;
          stopCell = ratcheted
            ? `<span class="mono" title="Ratchet armed — floor lifted from base $${fmtPrice(baseStop)} to $${fmtPrice(effective)} (peak $${fmtPrice(hwm)} − $${fmtPrice(ratchetDist)})">$${fmtPrice(effective)} <span class="sl-ratchet-badge">↑</span></span>`
            : `<span class="mono" title="Base stop-loss (ratchet not yet armed)">$${fmtPrice(effective)}</span>`;
          // Only project IF STOPPED when the sleeve actually holds contracts.
          if (ownAvgEntry > 0) {
            const qtyIf = Number(s.qty) || 0;
            const cfgIf = currentStore?.[tenant]?.[symbol]?.config || {};
            const sellFeeIf = Number(cfgIf.fee_per_fill_sell) || ((Number(cfgIf.fee_per_contract_roundtrip) || 0) / 2);
            const stopPnlIf = (effective - ownAvgEntry) * contractSize * qtyIf;
            const projectedIf = realized + stopPnlIf - sellFeeIf * qtyIf;
            ifStoppedCell = `<span class="mono ${projectedIf >= 0 ? 'pos' : 'neg'}" title="Realized ${fmtMoney(realized)} + stop-out (${fmtPrice(effective)}−${fmtPrice(ownAvgEntry)})×${contractSize}×${qtyIf} − sell fee $${(sellFeeIf * qtyIf).toFixed(2)}">${projectedIf >= 0 ? '+' : ''}${fmtMoney(projectedIf)}</span>`;
          }
        }
      }
      let trailCell = '<span class="dim">—</span>';
      const trailModes = s.exit_mode === 'trailing_stop' || s.exit_mode === 'hybrid';
      if (trailModes) {
        const peak = Number(ss.trail_high_water_price) || 0;
        const trailDist = Number(s.trail_distance) || 0;
        const armPx = Number(s.trail_activation_px) || Number(s.sell_px) || 0;
        if (peak > 0 && trailDist > 0) {
          const effectiveStop = peak - trailDist;
          trailCell = `<span class="mono trail-pill-on" title="Trail engaged — peak $${fmtPrice(peak)}, sells on $${fmtPrice(trailDist)} pullback (effective stop $${fmtPrice(effectiveStop)})">$${fmtPrice(effectiveStop)} <span class="trail-peak-note">$${fmtPrice(peak)}</span></span>`;
        } else if (armPx > 0) {
          trailCell = `<span class="mono trail-pill-off" title="Not yet activated — trail arms once price crosses $${fmtPrice(armPx)}">$${fmtPrice(armPx)}</span>`;
        }
      }
      const productHaltReason = state === 'HALTED'
        ? (currentStore[tenant]?.[symbol]?.state?.halt_reason
           || ss.halt_reason || 'halted — check bot log')
        : null;
      const stateCell = productHaltReason
        ? `<span class="status-pill ${state.toLowerCase()}" title="${escapeHtml(productHaltReason)}">${escapeHtml(prettyState(state))}</span><div class="halt-why-inline">${escapeHtml(productHaltReason)}</div>`
        : `<span class="status-pill ${state.toLowerCase()}">${escapeHtml(prettyState(state))}</span>`;
      const resumeBtn = state === 'HALTED'
        ? `<button class="small primary" data-action="resume-live-strategy"
                   data-tenant="${escapeHtml(tenant)}"
                   data-symbol="${escapeHtml(symbol)}">Resume</button>`
        : '';
      return `<tr>
        <td><b>${escapeHtml(s.name || s.id || '')}</b></td>
        <td class="mono">${s.qty}</td>
        <td class="mono dim">${posAvgPx > 0 ? `$${fmtPrice(posAvgPx)}` : '—'}</td>
        <td class="mono">${entryPx > 0 ? `$${fmtPrice(entryPx)}` : '<span class="dim">—</span>'}</td>
        <td class="mono">$${fmtPrice(s.sell_px || 0)}</td>
        <td class="mono">$${fmtPrice(s.buy_px || 0)}</td>
        <td class="mono">${stopCell}</td>
        <td class="mono">${ifStoppedCell}</td>
        <td class="mono">${trailCell}</td>
        <td class="mono">${Number(ss.cycles) || 0}</td>
        <td class="mono ${unrealized >= 0 ? 'pos' : 'neg'}">${unrealized >= 0 ? '+' : ''}${fmtMoney(unrealized)}</td>
        <td class="mono ${realized >= 0 ? 'pos' : 'neg'}">${realized >= 0 ? '+' : ''}${fmtMoney(realized)}</td>
        <td>${stateCell}</td>
        <td class="sleeve-row-actions">
          ${resumeBtn}
          <button class="small" data-action="edit-live-sleeve"
                  data-tenant="${escapeHtml(tenant)}"
                  data-symbol="${escapeHtml(symbol)}"
                  data-sleeve-id="${escapeHtml(s.id)}">Edit</button>
          <button class="small ghost" data-action="delete-sleeve"
                  data-tenant="${escapeHtml(tenant)}"
                  data-symbol="${escapeHtml(symbol)}"
                  data-sleeve-id="${escapeHtml(s.id)}">Remove</button>
        </td>
      </tr>`;
    }).join('');
  }
  // Also refresh the "Position: N LONG · avg $X" strip since qty/avg may
  // change when the user trades on Coinbase.
  const liveEl = scannerDetailSummary.querySelector('.scanner-detail-live');
  if (liveEl) {
    const btn = liveEl.querySelector('#scanner-detail-attach-strategy');
    const btnHtml = btn ? btn.outerHTML : '';
    liveEl.innerHTML = `
      <div class="sdl-hold">Position: <b>${qty}</b> ${escapeHtml(ctx._live_side || 'LONG')} · avg <b class="mono">$${fmtNum(avg, 4)}</b></div>
      ${btnHtml}
    `;
    // Re-wire the button since we just replaced its DOM.
    const newBtn = liveEl.querySelector('#scanner-detail-attach-strategy');
    if (newBtn) {
      newBtn.dataset.mark = String(mark || ctx.price || 0);
      newBtn.dataset.avg = String(avg);
      newBtn.dataset.posQty = String(qty);
      newBtn.onclick = () => {
        const ck = {
          mark: Number(newBtn.dataset.mark) || 0,
          avg: Number(newBtn.dataset.avg) || 0,
          qty: Number(newBtn.dataset.posQty) || 0,
          side: newBtn.dataset.side || '',
        };
        scannerDetailModal.hidden = true;
        openSleeveEditor(newBtn.dataset.tenant, newBtn.dataset.symbol, null, null, ck);
      };
    }
  }
}

function updateScannerBuyButton() {
  if (!scannerDetailContext) return;
  const symbol = scannerDetailContext.product_id;
  const mode = selectedScannerBuyMode();
  const orderType = selectedScannerOrderType();
  const side = selectedScannerSide();
  const qtyInput = document.getElementById('scanner-buy-qty');
  const qty = Math.max(1, Math.min(100, parseInt(qtyInput?.value || '1', 10) || 1));
  const limitPrice = Number(document.getElementById('scanner-limit-price')?.value) || 0;
  const priceForPreview = orderType === 'limit' && limitPrice > 0
    ? limitPrice
    : Number(scannerDetailContext.price) || 0;

  // Read spec off the row (scanner.compute_ranking populates it).
  const ctx = scannerDetailContext;
  const contractSize = Number(ctx.contract_size) || 50;
  const intradayRate = Number(ctx.intraday_margin_rate) || 0;
  // For a live tenant we can also read the stored margin_per_contract as a
  // fallback if Coinbase didn't return an intraday rate for this product.
  const liveCfg = ctx._live_tenant
    ? (currentStore?.[ctx._live_tenant]?.[ctx.product_id]?.config || {})
    : {};
  const storedMarginPer = Number(liveCfg.margin_per_contract) || 0;
  const notional = priceForPreview * contractSize * qty;
  // Margin required = what actually leaves your account for a futures fill.
  // Prefer the intraday rate from Coinbase; fall back to stored per-contract.
  const marginRequired = intradayRate > 0
    ? notional * intradayRate
    : (storedMarginPer > 0 ? storedMarginPer * qty : 0);
  // Real per-fill fee for THIS product. Scanner attaches fee_per_fill from a
  // live Coinbase preview; tracked-product configs also store the round-trip
  // number. Fall back to silver's $2.34 only if we've genuinely never seen
  // this product before (a stale scanner cache, or bot never previewed it).
  const rowPerFill = Number(ctx.fee_per_fill) || 0;
  const cfgRt = Number(liveCfg.fee_per_contract_roundtrip) || 0;
  const perFillFee = rowPerFill > 0
    ? rowPerFill
    : (cfgRt > 0 ? cfgRt / 2 : 2.34);
  const feeEst = perFillFee * qty;
  const feeIsEstimate = !(rowPerFill > 0 || cfgRt > 0);
  const priceLabel = orderType === 'limit'
    ? (limitPrice > 0 ? `at your limit $${limitPrice.toFixed(3)}` : 'at your limit (enter price)')
    : `at market ~$${(Number(scannerDetailContext.price) || 0).toFixed(3)}`;

  const notionalPerCt = priceForPreview * contractSize;
  const marginPerCt = marginRequired > 0 ? marginRequired / qty : 0;
  const fmt = (n) => n.toLocaleString('en-US', { maximumFractionDigits: 2 });
  // Per-contract cost line (Adam asked for "how much one contract would
  // cost to purchase"). Shows margin/ct if we know it, notional/ct otherwise.
  const perContractLine = marginPerCt > 0
    ? `<b>1 contract costs</b> <b style="color:var(--text)">$${fmt(marginPerCt)}</b> margin <span class="dim">(notional $${fmt(notionalPerCt)})</span>`
    : `<b>1 contract costs</b> <b style="color:var(--text)">$${fmt(notionalPerCt)}</b> notional <span class="dim">(margin unknown for this product)</span>`;
  const feeSuffix = feeIsEstimate ? ' <span class="dim">(silver-tier estimate)</span>' : '';
  const totalLine = marginRequired > 0
    ? `Total margin: <b style="color:var(--text)">$${fmt(marginRequired)}</b> · fee ~$${feeEst.toFixed(2)}${feeSuffix}`
    : `Total notional: <b style="color:var(--text)">$${fmt(notional)}</b> · fee ~$${feeEst.toFixed(2)}${feeSuffix}`;

  const previewEl = document.getElementById('scanner-buy-preview');
  if (previewEl) {
    previewEl.innerHTML = `
      <div>Entering <b style="color:var(--text)">${qty}</b> contract${qty === 1 ? '' : 's'} of <b style="color:var(--text)">${escapeHtml(symbol)}</b> ${priceLabel}</div>
      <div>${perContractLine}</div>
      <div>${totalLine}</div>
    `;
  }

  scannerBuyBtn.textContent = orderType === 'limit'
    ? `enter ${mode} LIMIT · ${qty} ${symbol}`
    : `enter ${mode} MARKET · ${qty} ${symbol}`;
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

function selectedScannerSide() {
  const checked = document.querySelector('input[name="scanner-side"]:checked');
  return checked ? checked.value : 'BUY';
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
  const side = selectedScannerSide();
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
    const verb = side === 'SELL' ? 'SHORT (SELL)' : 'BUY';
    // Recompute cost lines from the same inputs updateScannerBuyButton uses,
    // so the confirm dialog restates the exact $ that live in the preview
    // panel — no chance of the popup and the panel disagreeing.
    const ctx = scannerDetailContext;
    const contractSize = Number(ctx.contract_size) || 50;
    const priceForConfirm = orderType === 'limit' && limitPrice > 0
      ? limitPrice
      : Number(ctx.price) || 0;
    const notional = priceForConfirm * contractSize * qty;
    const intradayRate = Number(ctx.intraday_margin_rate) || 0;
    const liveCfg = ctx._live_tenant
      ? (currentStore?.[ctx._live_tenant]?.[ctx.product_id]?.config || {})
      : {};
    const storedMarginPer = Number(liveCfg.margin_per_contract) || 0;
    const marginRequired = intradayRate > 0
      ? notional * intradayRate
      : (storedMarginPer > 0 ? storedMarginPer * qty : 0);
    const rowPerFill = Number(ctx.fee_per_fill) || 0;
    const cfgRt = Number(liveCfg.fee_per_contract_roundtrip) || 0;
    const perFillFee = rowPerFill > 0 ? rowPerFill : (cfgRt > 0 ? cfgRt / 2 : 2.34);
    const feeEst = perFillFee * qty;
    const feeIsEstimate = !(rowPerFill > 0 || cfgRt > 0);
    const fmt2 = (n) => n.toLocaleString('en-US', { maximumFractionDigits: 2 });
    const costLines = [
      marginRequired > 0
        ? `Margin required: $${fmt2(marginRequired)}`
        : `Notional: $${fmt2(notional)}  (margin unknown)`,
      `Est. fee: ~$${feeEst.toFixed(2)}${feeIsEstimate ? ' (silver-tier estimate)' : ''}`,
      `Notional: $${fmt2(notional)}`,
    ];
    const ok = confirm(
      `REAL MONEY\n\n` +
      `${verb} ${qty} × ${symbol} contract${qty > 1 ? 's' : ''}\n` +
      `${detail}\n\n` +
      costLines.join('\n') + `\n\n` +
      `Proceed?`
    );
    if (!ok) return;
  }
  const originalLabel = scannerBuyBtn.textContent;
  scannerBuyBtn.disabled = true;
  scannerBuyBtn.textContent = 'placing…';
  try {
    const res = await postJson('/api/scanner-order', {
      product_id: symbol, side, qty, mode,
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
  const { days, minutes, granularity } = scannerChartWindow;
  scannerDetailChart.innerHTML = '<div class="chart-status">loading candles…</div>';
  try {
    const rangeParam = minutes != null ? `minutes=${minutes}` : `days=${days}`;
    // Fetch candles + fills in parallel — fills are annotated on the chart
    // so Adam can see where the strategy actually bought/sold and connect
    // each completed cycle with a line.
    const [candleResp, fillsResp] = await Promise.all([
      fetch(`/api/candles?product_id=${encodeURIComponent(product_id)}&${rangeParam}&granularity=${granularity}`, { credentials: 'same-origin' }),
      // limit=10000 matches the RedisTradeLog cap so cycles from earlier in
      // the day/week still make it onto the chart. 500 was silently dropping
      // fills whenever there were more than ~500 events since the buy leg.
      fetch(`/api/fills?symbol=${encodeURIComponent(product_id)}&limit=10000`, { credentials: 'same-origin' }),
    ]);
    if (!candleResp.ok) throw new Error(`HTTP ${candleResp.status}`);
    const data = await candleResp.json();
    if (!data.ok) throw new Error(data.error || 'unknown error');
    let fills = [];
    let fillsDiag = null;
    try {
      const fj = await fillsResp.json();
      if (fj && fj.ok && Array.isArray(fj.fills)) {
        fills = fj.fills;
        fillsDiag = fj._diag || null;
      }
    } catch { /* fills are optional — chart still renders without them */ }
    // Pull sleeve targets (sell/buy/stop) for reference lines so Adam can
    // see at a glance where the current strategy would fire.
    const liveTenant = Object.keys(currentStore || {}).find(t => modeOfTenant(t) === 'live');
    const cfg = liveTenant ? (currentStore[liveTenant]?.[product_id]?.config || {}) : {};
    const sleeves = Array.isArray(cfg.sleeves) ? cfg.sleeves : [];
    const targetLines = [];
    for (const s of sleeves) {
      if (Number(s.sell_px) > 0) targetLines.push({ price: Number(s.sell_px), label: `sell (${s.name || s.id})`, color: '#22c55e' });
      if (Number(s.buy_px) > 0) targetLines.push({ price: Number(s.buy_px), label: `buy (${s.name || s.id})`, color: '#3b82f6' });
      if (s.stop_loss_enabled && Number(s.stop_loss_px) > 0) targetLines.push({ price: Number(s.stop_loss_px), label: `stop (${s.name || s.id})`, color: '#ef4444' });
    }
    renderCandleChart(data.candles || [], scannerDetailChart, {
      fills,
      fillsDiag,
      symbol: product_id,
      targetLines,
      tickSize: Number(cfg.tick_size) || 0,
      contractSize: Number(cfg.contract_size) || 0,
    });
  } catch (err) {
    scannerDetailChart.innerHTML = `<div class="chart-status error">chart failed: ${escapeHtml(String(err.message || err))}</div>`;
  }
}

// SVG candlestick chart. Candles are [ts, open, high, low, close].
// No external charting lib — keeps the dashboard tiny and avoids CSP hassle.
//
// opts (all optional):
//   fills:        [{ts, side ('BUY'|'SELL'), price, qty, kind, sleeve}]
//                 → renders arrow markers + connects each BUY→SELL as a
//                   dashed cycle line labeled with the per-contract gain.
//   targetLines:  [{price, label, color}]
//                 → horizontal reference lines for current sell/buy/stop
//                   so Adam sees where the live strategy would fire.
//   tickSize:     product tick_size → drives Y-axis label precision.
//   contractSize: for annotating cycle profit in $ (spread × contract_size).
function renderCandleChart(candles, container, opts = {}) {
  if (!candles.length) {
    container.innerHTML = '<div class="chart-status">no candles in window.</div>';
    return;
  }
  const fills = Array.isArray(opts.fills) ? opts.fills : [];
  const targetLines = Array.isArray(opts.targetLines) ? opts.targetLines : [];
  const tickSize = Number(opts.tickSize) || 0;
  const csize = Number(opts.contractSize) || 0;
  const W = container.clientWidth || 900;
  const H = 480;
  const padL = 80, padR = 20, padT = 20, padB = 40;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;

  // Include fills in Y-axis range so markers are always visible even if
  // they're outside the recent candle range (e.g., an old stop-loss below
  // today's low).
  let lo = Infinity, hi = -Infinity;
  for (const c of candles) {
    if (c[3] < lo) lo = c[3];
    if (c[2] > hi) hi = c[2];
  }
  const firstTs = Number(candles[0][0]) || 0;
  const lastTs = Number(candles[candles.length - 1][0]) || 0;
  for (const f of fills) {
    if (!(Number(f.ts) >= firstTs && Number(f.ts) <= lastTs)) continue;
    if (f.price < lo) lo = f.price;
    if (f.price > hi) hi = f.price;
  }
  for (const t of targetLines) {
    if (t.price < lo) lo = t.price;
    if (t.price > hi) hi = t.price;
  }
  if (lo === hi) { lo -= 0.5; hi += 0.5; }
  const range = hi - lo;
  // Pad top/bottom 5% so nothing sits flush against the axis.
  lo -= range * 0.05;
  hi += range * 0.05;
  const paddedRange = hi - lo;
  const y = (p) => padT + plotH * (1 - (p - lo) / paddedRange);
  const x = (ts) => padL + plotW * ((ts - firstTs) / Math.max(1, lastTs - firstTs));

  // Y-axis precision from tick_size (matches Sell/Buy input display).
  let yDec = 3;
  if (tickSize > 0) {
    const parts = tickSize.toString().split('.');
    yDec = parts.length > 1 ? Math.min(8, parts[1].length) : 0;
  } else if (hi < 1) yDec = 5;
  else if (hi < 10) yDec = 4;

  const n = candles.length;
  const stepW = plotW / n;
  const bodyW = Math.max(2, Math.min(10, stepW * 0.7));

  const parts = [];
  parts.push(`<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="none">`);

  // Y-axis: 7 gridlines with tick-precision labels.
  for (let i = 0; i <= 6; i++) {
    const p = lo + (paddedRange * i / 6);
    const yy = y(p);
    parts.push(`<line x1="${padL}" y1="${yy.toFixed(1)}" x2="${W - padR}" y2="${yy.toFixed(1)}" stroke="#1e2a3a" stroke-width="1" />`);
    parts.push(`<text x="${padL - 8}" y="${(yy + 4).toFixed(1)}" fill="#8a99ac" font-size="12" text-anchor="end" font-family="ui-monospace,monospace">$${p.toFixed(yDec)}</text>`);
  }

  // X-axis: 5 evenly-spaced time labels across the window.
  const xTickCount = 5;
  for (let i = 0; i < xTickCount; i++) {
    const frac = i / (xTickCount - 1);
    const ts = firstTs + (lastTs - firstTs) * frac;
    const xx = padL + plotW * frac;
    const d = new Date(ts * 1000);
    const anchor = i === 0 ? 'start' : (i === xTickCount - 1 ? 'end' : 'middle');
    const dateStr = d.toLocaleDateString([], { month: 'numeric', day: 'numeric' });
    const timeStr = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    parts.push(`<line x1="${xx.toFixed(1)}" y1="${padT}" x2="${xx.toFixed(1)}" y2="${(padT + plotH).toFixed(1)}" stroke="#0f1720" stroke-width="1"/>`);
    parts.push(`<text x="${xx.toFixed(1)}" y="${(H - 20).toFixed(1)}" fill="#8a99ac" font-size="11" text-anchor="${anchor}" font-family="ui-monospace,monospace">${dateStr}</text>`);
    parts.push(`<text x="${xx.toFixed(1)}" y="${(H - 6).toFixed(1)}" fill="#8a99ac" font-size="11" text-anchor="${anchor}" font-family="ui-monospace,monospace">${timeStr}</text>`);
  }

  // Candles.
  for (let i = 0; i < n; i++) {
    const [ts, o, h, l, c] = candles[i];
    const xc = x(Number(ts));
    const up = c >= o;
    const color = up ? '#22c55e' : '#ef4444';
    const yh = y(h), yl = y(l), yo = y(o), yc = y(c);
    const top = Math.min(yo, yc);
    const bh = Math.max(1, Math.abs(yc - yo));
    parts.push(`<line x1="${xc.toFixed(1)}" y1="${yh.toFixed(1)}" x2="${xc.toFixed(1)}" y2="${yl.toFixed(1)}" stroke="${color}" stroke-width="1"/>`);
    parts.push(`<rect x="${(xc - bodyW/2).toFixed(1)}" y="${top.toFixed(1)}" width="${bodyW.toFixed(1)}" height="${bh.toFixed(1)}" fill="${color}"/>`);
  }

  // Target reference lines (current sell / buy / stop from live sleeves).
  for (const t of targetLines) {
    const yy = y(t.price);
    if (yy < padT || yy > padT + plotH) continue;
    parts.push(`<line x1="${padL}" y1="${yy.toFixed(1)}" x2="${W - padR}" y2="${yy.toFixed(1)}" stroke="${t.color}" stroke-width="1" stroke-dasharray="4 3" opacity="0.75"/>`);
    parts.push(`<text x="${(W - padR - 4).toFixed(1)}" y="${(yy - 3).toFixed(1)}" fill="${t.color}" font-size="10" text-anchor="end" font-family="ui-monospace,monospace" opacity="0.9">${escapeHtml(t.label)} $${t.price.toFixed(yDec)}</text>`);
  }

  // Fill markers + cycle-connecting dashed lines.
  // Pair each BUY with the next SELL to form a cycle (walk fills in order).
  const visibleFills = fills.filter(f => Number(f.ts) >= firstTs && Number(f.ts) <= lastTs);
  const cycles = [];
  let openBuy = null;
  for (const f of visibleFills) {
    if (f.side === 'BUY') {
      openBuy = f;  // most recent open buy — replace if two buys in a row
    } else if (f.side === 'SELL' && openBuy) {
      cycles.push({ buy: openBuy, sell: f });
      openBuy = null;
    }
  }
  // Draw cycle lines UNDER markers so triangles pop on top.
  for (const c of cycles) {
    const x1 = x(Number(c.buy.ts));
    const y1 = y(c.buy.price);
    const x2 = x(Number(c.sell.ts));
    const y2 = y(c.sell.price);
    const won = c.sell.price > c.buy.price;
    const lineColor = won ? '#22c55e' : '#ef4444';
    parts.push(`<line x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}" stroke="${lineColor}" stroke-width="1.5" stroke-dasharray="5 3" opacity="0.85"/>`);
    // Label at midpoint: $/contract gain (or loss).
    if (csize > 0) {
      const midX = (x1 + x2) / 2;
      const midY = (y1 + y2) / 2 - 6;
      const gain = (c.sell.price - c.buy.price) * csize;
      const sign = gain >= 0 ? '+' : '';
      parts.push(`<text x="${midX.toFixed(1)}" y="${midY.toFixed(1)}" fill="${lineColor}" font-size="11" text-anchor="middle" font-family="ui-monospace,monospace" font-weight="bold">${sign}$${gain.toFixed(2)}</text>`);
    }
  }
  // Markers.
  for (const f of visibleFills) {
    const xc = x(Number(f.ts));
    const yc = y(f.price);
    if (f.side === 'BUY') {
      // Blue up-triangle below the fill price.
      const tri = `${xc},${(yc + 4).toFixed(1)} ${(xc - 6).toFixed(1)},${(yc + 14).toFixed(1)} ${(xc + 6).toFixed(1)},${(yc + 14).toFixed(1)}`;
      parts.push(`<polygon points="${tri}" fill="#3b82f6" stroke="#dbeafe" stroke-width="1"/>`);
      parts.push(`<title>BUY ${f.qty} @ $${Number(f.price).toFixed(yDec)}</title>`);
    } else {
      // Red down-triangle above the fill price. Stop-loss gets a bolder
      // outline so a forced exit stands out from a target-fired sell.
      const tri = `${xc},${(yc - 4).toFixed(1)} ${(xc - 6).toFixed(1)},${(yc - 14).toFixed(1)} ${(xc + 6).toFixed(1)},${(yc - 14).toFixed(1)}`;
      const stroke = f.kind === 'stop_loss' ? '#facc15' : '#fee2e2';
      const strokeW = f.kind === 'stop_loss' ? 2 : 1;
      parts.push(`<polygon points="${tri}" fill="#ef4444" stroke="${stroke}" stroke-width="${strokeW}"/>`);
    }
  }

  parts.push(`</svg>`);

  // Legend below chart so Adam can decode markers without hovering.
  const cycleWon = cycles.filter(c => c.sell.price > c.buy.price).length;
  const cycleLost = cycles.length - cycleWon;
  // When 0 fills for this symbol, surface the diagnostic block directly on
  // the chart so we can see whether the log has fills under a DIFFERENT
  // symbol (naming mismatch) or genuinely nothing to plot.
  let diagLine = '';
  const diag = opts.fillsDiag;
  const sym = opts.symbol || '(unknown symbol)';
  if (fills.length === 0 && diag) {
    const others = (diag.symbols_in_log || []).filter(s => s !== sym).slice(0, 12);
    const othersStr = others.length ? ` · other symbols in log: <b>${others.map(escapeHtml).join(', ')}</b>` : '';
    diagLine = `
      <div style="padding:8px 12px;font-size:12px;color:#facc15;font-family:ui-monospace,monospace;background:rgba(250,204,21,0.06);border-top:1px solid rgba(250,204,21,0.2);">
        no fills to plot for <b>${escapeHtml(sym)}</b> — scanned ${diag.events_scanned} log events, ${diag.events_for_symbol} tagged with this symbol${othersStr}
      </div>`;
  }
  const legend = `
    <div style="display:flex;gap:16px;flex-wrap:wrap;padding:8px 12px;font-size:12px;color:#8a99ac;font-family:ui-monospace,monospace;">
      <span><span style="color:#3b82f6;">▲</span> BUY fill</span>
      <span><span style="color:#ef4444;">▼</span> SELL fill</span>
      <span><span style="color:#facc15;">◆</span> stop-loss fired</span>
      ${targetLines.length ? '<span style="border-top:1px dashed #8a99ac;padding-top:1px;">current target lines</span>' : ''}
      ${cycles.length ? `<span>· <b style="color:#22c55e;">${cycleWon}</b> winning · <b style="color:#ef4444;">${cycleLost}</b> losing cycle${cycles.length === 1 ? '' : 's'} on chart</span>` : ''}
    </div>`;
  container.innerHTML = parts.join('') + legend + diagLine;
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
  // Live-portfolio derivative row: opens the product-detail modal (reuses
  // the scanner-detail modal) which has the chart + Buy/Sell + Attach
  // strategy button. Portfolio table stays as the entry point; a click
  // drills into ONE product without cluttering the page with cards.
  const pfRow = e.target.closest('tr.pf-row');
  if (pfRow && !e.target.closest('button')) {
    const t = pfRow.dataset.tenant, s = pfRow.dataset.symbol;
    if (t && s) {
      const mark = Number(pfRow.dataset.mark) || 0;
      const avg = Number(pfRow.dataset.avg) || 0;
      const qty = Number(pfRow.dataset.posQty) || 0;
      openScannerDetail({
        product_id: s,
        price: mark,
        high_24h: mark,
        low_24h: mark,
        vol_pct: 0,
        _live_tenant: t,
        _live_avg: avg,
        _live_qty: qty,
        _live_side: pfRow.dataset.side || '',
      });
      return;
    }
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
  else if (action === 'open-live-strategy') openSleeveEditor(tenant, symbol, null);
  else if (action === 'add-sleeve-from-lot') openSleeveEditor(tenant, symbol, null, {
    entry_price: parseFloat(btn.dataset.lotEntry),
    qty: parseInt(btn.dataset.lotQty, 10),
  });
  else if (action === 'edit-sleeve') openSleeveEditor(tenant, symbol, btn.dataset.sleeveId);
  else if (action === 'edit-live-sleeve') {
    // Edit from the Live drill-down: close it, then open the sleeve editor
    // with the portfolio context so save-flow reopens the drill-down.
    const ctx = scannerDetailContext;
    const pf = ctx && ctx._live_tenant ? {
      mark: Number(ctx.price) || 0,
      avg: Number(ctx._live_avg) || 0,
      qty: Number(ctx._live_qty) || 0,
      side: String(ctx._live_side || ''),
    } : null;
    scannerDetailModal.hidden = true;
    openSleeveEditor(tenant, symbol, btn.dataset.sleeveId, null, pf);
  }
  else if (action === 'delete-sleeve') deleteSleeve(tenant, symbol, btn.dataset.sleeveId);
  else if (action === 'resume-live-strategy') {
    // Clear the HALT for this product on the Live tenant. Server writes a
    // resume_intent; the bot picks it up on the next reconcile tick.
    postJson('/api/resume', { tenant, symbol }).then(res => {
      if (res._unauthorized) return showLogin();
      if (res.ok) { showToast('resume queued — next tick', 'info'); refreshOnce(); }
      else showToast(res.error || 'resume failed', 'error');
    });
  }
  else if (action === 'resume') resumeStrategy(tenant, symbol);
  else if (action === 'cancel-order') cancelOrder(tenant, symbol, btn.dataset.sleeveId || null);
  else if (action === 'sell-now') marketSell(tenant, symbol, parseInt(btn.dataset.qty, 10));
  else if (action === 'disable-primary') disablePrimaryStrategy(tenant, symbol);
  else if (action === 'ts-submit') submitSidebarTrade(btn.closest('.trade-sidebar'));
});

// Sidebar inline trade form: recompute preview + submit button label when
// the user changes side/qty/type/limit. Delegated on the cards container so
// it works across every card without re-binding on each render.
document.addEventListener('input', e => {
  const wrap = e.target.closest('.trade-sidebar');
  if (!wrap) return;
  updateSidebarPreview(wrap);
});
document.addEventListener('change', e => {
  const wrap = e.target.closest('.trade-sidebar');
  if (!wrap) return;
  if (e.target.classList.contains('ts-order-type')) {
    const isLimit = e.target.value === 'limit';
    const limitField = wrap.querySelector('.ts-limit-field');
    if (limitField) limitField.hidden = !isLimit;
  }
  updateSidebarPreview(wrap);
});
document.addEventListener('click', e => {
  const tab = e.target.closest('.ts-side-tab');
  if (!tab) return;
  const wrap = tab.closest('.trade-sidebar');
  if (!wrap) return;
  wrap.querySelectorAll('.ts-side-tab').forEach(t => t.classList.remove('active'));
  tab.classList.add('active');
  updateSidebarPreview(wrap);
});

function sidebarSelectedSide(wrap) {
  const active = wrap.querySelector('.ts-side-tab.active');
  return active ? active.dataset.side : 'BUY';
}

function updateSidebarPreview(wrap) {
  const tenant = wrap.dataset.tenant;
  const symbol = wrap.dataset.symbol;
  const cfg = currentStore[tenant]?.[symbol]?.config || {};
  const snap = currentStore[tenant]?.[symbol]?.snapshot || {};
  const side = sidebarSelectedSide(wrap);
  const orderType = wrap.querySelector('.ts-order-type')?.value || 'market';
  const qty = Math.max(1, parseInt(wrap.querySelector('.ts-qty')?.value || '1', 10));
  const mark = Number(snap.last_mark) || 0;
  const limitPx = Number(wrap.querySelector('.ts-limit-price')?.value) || 0;
  const price = orderType === 'limit' ? limitPx : mark;
  const contractSize = Number(cfg.contract_size) || 50;
  const feeRt = Number(cfg.fee_per_contract_roundtrip) || 4.68;
  const halfFee = (feeRt / 2) * qty;
  const notional = price * contractSize * qty;
  const pos = Number(snap.position_qty) || 0;
  const newPos = side === 'BUY' ? pos + qty : pos - qty;
  wrap.querySelector('.ts-notional').textContent = notional > 0 ? '$' + notional.toLocaleString('en-US', {maximumFractionDigits: 2}) : '—';
  wrap.querySelector('.ts-fee').textContent = '$' + halfFee.toFixed(2);
  wrap.querySelector('.ts-after').textContent = `${pos} → ${newPos}`;
  const submit = wrap.querySelector('.ts-submit');
  submit.textContent = `${side === 'BUY' ? 'Buy' : 'Sell'} ${qty} contract${qty === 1 ? '' : 's'}`;
  submit.classList.toggle('primary', side === 'BUY');
  submit.classList.toggle('danger', side === 'SELL');
  const warn = wrap.querySelector('.ts-warn');
  if (side === 'SELL' && newPos < 0) {
    warn.hidden = false;
    warn.innerHTML = pos > 0
      ? `<b>Opens short:</b> closes your ${pos} long, opens ${Math.abs(newPos)}-contract short.`
      : `<b>Opens new short position of ${Math.abs(newPos)}.</b>`;
  } else {
    warn.hidden = true;
  }
}

async function submitSidebarTrade(wrap) {
  const tenant = wrap.dataset.tenant;
  const symbol = wrap.dataset.symbol;
  const side = sidebarSelectedSide(wrap);
  const orderType = wrap.querySelector('.ts-order-type')?.value || 'market';
  const qty = Math.max(1, parseInt(wrap.querySelector('.ts-qty')?.value || '1', 10));
  const limitPrice = Number(wrap.querySelector('.ts-limit-price')?.value) || 0;
  if (orderType === 'limit' && !(limitPrice > 0)) {
    showToast('enter a limit price above 0', 'error');
    return;
  }
  const snap = currentStore[tenant]?.[symbol]?.snapshot || {};
  const pos = Number(snap.position_qty) || 0;
  if (side === 'SELL' && (pos - qty) < 0) {
    const shortDepth = qty - Math.max(0, pos);
    if (!confirm(`This opens a ${shortDepth}-contract SHORT position. Silver rallying = margin loss. Continue?`)) return;
  }
  if (isLiveTenant(tenant)) {
    const priceLine = orderType === 'limit' ? `at LIMIT $${limitPrice.toFixed(3)}` : `at MARKET`;
    const ok = await confirmLive({
      title: `${side} ${qty} ${symbol} — real money`,
      body: `<b>${side} ${qty}</b> contract${qty === 1 ? '' : 's'} of <b>${escapeHtml(symbol)}</b> ${priceLine} on Coinbase.`,
    });
    if (!ok) return;
  }
  const res = await postJson('/api/manual-trade', {
    tenant, symbol, side, qty, order_type: orderType,
    limit_price: orderType === 'limit' ? limitPrice : null,
    confirm: 'YES',
  });
  if (res._unauthorized) { showLogin(); return; }
  if (res.ok) { showToast(`${side.toLowerCase()} queued`, 'info'); refreshOnce(); }
  else showToast(res.error || 'trade failed', 'error');
}

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
  if (qty < 1) { showToast('qty must be at least 1', 'error'); return; }
  // Shorting enabled: selling MORE than you hold takes you into a short
  // position on Coinbase CFM. Warn explicitly so accidental oversells don't
  // silently open shorts, but don't block — Adam asked for the option.
  if (qty > pos) {
    const shortDepth = qty - pos;
    const msg = pos > 0
      ? `Sell ${qty} — closes your ${pos} long AND opens ${shortDepth}-contract short. Continue?`
      : `You hold 0 contracts. Sell ${qty} opens a NEW ${qty}-contract short. Continue?`;
    if (!confirm(msg)) return;
  }
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
stopLossToggleBtn.addEventListener('click', toggleStopLoss);

// ---- boot ---------------------------------------------------------------

(async () => {
  const sess = await checkSession();
  if (!sess.auth_required || sess.authed) {
    showDashboard(sess.auth_required);
  } else {
    showLogin();
  }
})();
