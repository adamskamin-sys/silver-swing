# silver-swing — project context for Claude Code

## What this is
An automated **crypto swing-trading system** (Python) trading on **Coinbase**,
deployed on **Render**. It scans markets, generates signals, sizes positions with
risk controls, and can run live or in paper/backtest mode. **This moves real
money — correctness and safety outrank cleverness.**

## Map of the codebase
- main.py (~1k lines) — entrypoint / orchestration.
- swing_leg.py (~2.5k lines) — core strategy logic. **Biggest file; highest
  risk; primary refactor + audit target.**
- scanner.py, scanner_worker.py, twitter_scanner.py — opportunity scanning.
- broker.py, paper_broker.py — live vs simulated order execution.
- backtest.py, backtest_worker.py — historical simulation. **Watch for
  look-ahead bias / data leakage.**
- portfolio_risk.py, correlation.py, safety.py — risk controls. **Guardrails
  live here; a bug here is a money bug.**
- microstructure.py, tape_shadow.py, feed.py — market data / microstructure.
- sleeves.py, strategies.py, presets.py, expert_params.py, expert_tuner.py —
  strategy config & tuning.
- state_store.py — persisted state. live_runner.py — the live loop.
- tests/ — pytest suite. **Run it before trusting any change.**

## Rules for any change
- **Never** place or modify live orders while exploring. Paper/backtest first.
- Preserve and, where possible, strengthen risk controls; never weaken a guard
  to make a test pass.
- Currency math: watch precision/rounding; no floats where it matters.
- Backtest changes must not introduce look-ahead bias.
- Add/adjust tests in tests/ for any logic change.
- Secrets (.coinbase_key.json, .env) are gitignored — keep it that way.

## Where the scouts should look first
Highest stakes: safety.py, portfolio_risk.py, broker.py, live_runner.py,
and the giant swing_leg.py. Biggest debt: splitting swing_leg.py into
cohesive modules with test coverage.

## Top-priority invariants — agents should treat these as blocker findings

These are durable rules learned from real 2026-07 money-losing incidents.
Any agent (risk-officer, problem-scout, execution-analyst, backtest-referee)
reading this codebase should flag violations as blockers.

- **Bot mark ↔ Coinbase mark must always be in sync.** Every $-denominated
  calc AND every price-triggered action (TP, trail, stop, entry, re-entry,
  buy-add) must read from a mark source refreshed within seconds. Drift
  detection must fire an aggressive re-sync (force fetch + WebSocket
  reconnect), NEVER halt trading. Reference: 2026-07-14 PLAT incident
  where a 2.5s stale mark cost a missed take-profit AND missed trail-stop.
- **Every held product gets ticked.** Per-product WebSocket feed + per-
  product SwingTrader. No "primary is special" carve-outs. If a held
  product exists in the __portfolio__ snapshot but has no active trader
  ticking it, that's a blocker. Reference: 2026-07-14 non-primary
  silence bug that killed 4+ hours of stop-loss coverage.
- **Tenant scoping is `-live` shaped.** Every code path that touches the
  live tenant must derive it via the guarded `f"{TENANT}-live" if not
  TENANT.endswith("-live") else TENANT` pattern (or an equivalent hard
  assert). No bare `f"{TENANT}-live"`. Reference: 2026-07-14 refresh-
  loop bug that wrote fresh marks to a phantom `adam-live-live` scope
  for weeks.
- **DryRunBroker must intercept every write path.** Any new order type
  added to CoinbaseBroker requires a matching stub in DryRunBroker,
  or dry-run will silently submit real orders. Reference: 2026-07-14
  problem-scout finding — `place_market` fell through `__getattr__` to
  the real client, so market sells bypassed dry-run entirely.
- **Non-primary boot state normalizer must be log-only.** The primary
  gets HALT-on-drift; non-primary logs drift but does NOT halt (would
  silently freeze a sleeve overnight = the opposite of protective).
- **Never credit stale reconciled fills as fresh.** When creating a
  trader for a product that's been silent for > SWING_STALE_HEARTBEAT_
  HOURS, clear stale `live_order_id`s BEFORE reconcile — else
  `_sleeve_on_fill` will credit months-old FILLED orders as fresh
  cycles, pollute realized_pnl + cycles, and potentially trigger a
  live-crossing expert-reanchor buy.
- **Evicted tracks must have a cooldown.** After a per-product trader
  is evicted for repeated failures, refuse re-creation for
  SWING_EVICT_COOLDOWN_SECS. Prevents infinite create/fail/evict loops
  that would rate-limit-ban us off Coinbase.

## When to spawn agents proactively
- Before shipping any change to live_runner.py, swing_leg.py, broker.py,
  safety.py, portfolio_risk.py, or sleeves.py → run problem-scout.
- After any strategy or parameter change → run backtest-referee.
- On a recurring schedule (see /schedule) → run risk-officer to check
  feed freshness, mark sync, and liquidation headroom across correlated
  clusters. Read-only; it raises risk, never trades.
