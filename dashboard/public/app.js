/**
 * dashboard/public/app.js — reads-only status polling.
 *
 * Two modes:
 *   - Login required (DASHBOARD_PASSWORD set on server) → show login → session → polling
 *   - Dev mode (no password)                             → straight to polling
 *
 * Polls /api/status and /api/trades every 5s. If the server returns 401,
 * drop back to the login screen — a session may have expired.
 */

const POLL_MS = 5000;

const loginView = document.getElementById('login-view');
const dashboardView = document.getElementById('dashboard-view');
const loginForm = document.getElementById('login-form');
const loginError = document.getElementById('login-error');
const passwordInput = document.getElementById('password');
const logoutBtn = document.getElementById('logout-btn');
const lastUpdated = document.getElementById('last-updated');
const cardsEl = document.getElementById('instrument-cards');
const tradeLogEl = document.getElementById('trade-log');

let pollHandle = null;

async function fetchJson(url, opts) {
  const res = await fetch(url, { credentials: 'same-origin', ...opts });
  if (res.status === 401) return { _unauthorized: true };
  return await res.json();
}

async function checkSession() {
  const s = await fetchJson('/api/session');
  return s;
}

async function tryLogin(password) {
  const res = await fetch('/login', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({ password }),
  });
  return await res.json();
}

async function logout() {
  await fetch('/logout', { method: 'POST', credentials: 'same-origin' });
  showLogin();
}

function showLogin() {
  if (pollHandle) { clearInterval(pollHandle); pollHandle = null; }
  loginView.hidden = false;
  dashboardView.hidden = true;
  logoutBtn.hidden = true;
  passwordInput.focus();
}

function showDashboard(authRequired) {
  loginView.hidden = true;
  dashboardView.hidden = false;
  logoutBtn.hidden = !authRequired;
  refreshOnce();
  pollHandle = setInterval(refreshOnce, POLL_MS);
}

// ---- rendering ------------------------------------------------------------

function fmtMoney(n) {
  if (n === null || n === undefined) return '—';
  const v = Number(n);
  return v.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 });
}

function fmtNum(n, decimals = 2) {
  if (n === null || n === undefined) return '—';
  return Number(n).toFixed(decimals);
}

function classForValue(n) {
  const v = Number(n);
  if (Number.isNaN(v) || v === 0) return 'dim';
  return v > 0 ? 'pos' : 'neg';
}

function renderCard(tenant, symbol, { config, state }) {
  const s = state || {};
  const c = config || {};
  const halted = s.state === 'HALTED';
  const legPill = {
    ARMED_SELL: 'armed-sell',
    ARMED_BUY: 'armed-buy',
    HALTED: 'halted',
  }[s.state] || '';

  const el = document.createElement('article');
  el.className = 'card' + (halted ? ' halted' : '');
  el.innerHTML = `
    <h2>
      <span>${escapeHtml(tenant)} / ${escapeHtml(symbol)}</span>
      <span class="pill ${legPill}">${escapeHtml(s.state || 'unknown')}</span>
    </h2>
    <div class="grid">
      <div class="field"><span class="field-label">swing qty</span><span class="field-value">${s.swing_qty ?? '—'} / max ${c.max_swing_qty ?? '—'}</span></div>
      <div class="field"><span class="field-label">core floor</span><span class="field-value">${c.core_qty ?? '—'}</span></div>
      <div class="field"><span class="field-label">cycles</span><span class="field-value">${s.cycles ?? 0}</span></div>
      <div class="field"><span class="field-label">exit mode</span><span class="field-value dim">${escapeHtml(c.exit_mode || 'fixed_limit')}</span></div>
      <div class="field"><span class="field-label">sell / buy</span><span class="field-value">${fmtNum(c.sell_px, 3)} / ${fmtNum(c.buy_px, 3)}</span></div>
      <div class="field"><span class="field-label">abort ↓ / ↑</span><span class="field-value dim">${fmtNum(c.abort_below, 2)} / ${fmtNum(c.abort_above, 2)}</span></div>
      <div class="field"><span class="field-label">realized P&amp;L</span><span class="field-value ${classForValue(s.realized_pnl)}">${fmtMoney(s.realized_pnl)}</span></div>
      <div class="field"><span class="field-label">reserved margin</span><span class="field-value dim">${fmtMoney(s.reserved_margin)}</span></div>
      <div class="field"><span class="field-label">live order</span><span class="field-value dim">${escapeHtml(s.live_order_id || '—')}</span></div>
      <div class="field"><span class="field-label">last heartbeat</span><span class="field-value dim">${fmtHeartbeat(s.last_heartbeat_ts)}</span></div>
    </div>
  `;
  return el;
}

function fmtHeartbeat(ts) {
  if (!ts) return 'never';
  const age = Date.now() / 1000 - ts;
  if (age < 60) return `${age.toFixed(0)}s ago`;
  if (age < 3600) return `${(age / 60).toFixed(0)}m ago`;
  return `${(age / 3600).toFixed(1)}h ago`;
}

function priorityClass(eventType) {
  if (eventType === 'halt' || eventType === 'reconcile_halt' || eventType === 'fee_gate_halt') return 'crit';
  if (eventType === 'kill_switch_pause' || eventType === 'cancel_failed') return 'warn';
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
    <span class="event-type">${escapeHtml(ev.event_type || '?')}</span>
    <span class="event-detail">${escapeHtml(JSON.stringify(detail))}</span>
  `;
  return li;
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, ch => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]
  ));
}

// ---- polling --------------------------------------------------------------

async function refreshOnce() {
  const [status, trades] = await Promise.all([
    fetchJson('/api/status'),
    fetchJson('/api/trades?n=30'),
  ]);
  if (status._unauthorized || trades._unauthorized) {
    showLogin();
    return;
  }

  cardsEl.innerHTML = '';
  const store = status.store || {};
  const tenants = Object.keys(store).sort();
  if (tenants.length === 0) {
    cardsEl.innerHTML = '<div class="field-value dim">no state yet — has the bot run?</div>';
  } else {
    for (const tenant of tenants) {
      const symbols = Object.keys(store[tenant] || {}).sort();
      for (const symbol of symbols) {
        cardsEl.appendChild(renderCard(tenant, symbol, store[tenant][symbol]));
      }
    }
  }

  tradeLogEl.innerHTML = '';
  const events = (trades.events || []).slice().reverse();
  for (const ev of events) {
    tradeLogEl.appendChild(renderTradeEvent(ev));
  }

  lastUpdated.textContent = `updated ${new Date().toLocaleTimeString()}`;
}

// ---- events --------------------------------------------------------------

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

// ---- bootstrap -----------------------------------------------------------

(async () => {
  const sess = await checkSession();
  if (!sess.auth_required || sess.authed) {
    showDashboard(sess.auth_required);
  } else {
    showLogin();
  }
})();
