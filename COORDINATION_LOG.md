# COORDINATION_LOG.md

Handoff log between LOCAL (Claude Code) and CLOUD auditor, per AGENTS.md rule 6.
One line per material action (commit sha, deploy, scope flip, halt clear).
Newest at bottom. Format: `YYYY-MM-DD HH:MM  ACTOR  ACTION  DETAIL`.

## Legend
- **LOCAL** = Claude Code (this repo). Sole writer.
- **CLOUD** = read-only auditor session.
- **ADAM**  = manual user action.

## Log

2026-07-13 07:15  LOCAL  commit  c275dcb — Revert net for today's promotions (backups + surgical restore script)
2026-07-13 07:47  LOCAL  commit  b9c5be3 — Post-reversal UI: reversal-armed badge, colored SIDE, faster refresh
2026-07-13 07:54  LOCAL  commit  b80931e — Stop-loss re-enable safety + ENTRY column reflects actual basis
2026-07-13 08:18  LOCAL  commit  9adb10c — Average-down green light (notification-only, never executes)
2026-07-13 08:41  LOCAL  commit  9711727 — Sleeve UNREALIZED always shows MTM (removes cycles>0 gate)
2026-07-13 08:46  LOCAL  commit  d14e67b — Sleeve UNREALIZED basis: own_avg_entry → position avg, never entry_mark
2026-07-13 09:11  LOCAL  commit  c5a1b9b — Avg-down signal: per-product 🟢/🟡 badge + tooltip names the contract
2026-07-13 09:15  LOCAL  commit  9be2d3a — Avg-down badge: match by symbol only, not tenant
2026-07-13 10:18  LOCAL  commit  a8bb301 — diag_oil_skips.py: name the guard blocking a sleeve's re-entry
2026-07-13 10:22  LOCAL  commit  666fab1 — diag_oil_skips.py: substring match + list-all-symbols on no match
2026-07-13 10:26  LOCAL  commit  fe87e50 — diag_sleeve_state.py: find where OIL sleeves actually live
2026-07-13 10:30  LOCAL  commit  327192e — diag_sleeve_state.py: enumerate ALL tenants, not just three
2026-07-13 10:31  LOCAL  commit  e8beb18 — diag_grep_events.py: server-side grep of the Redis prod trade log
2026-07-13 10:50  LOCAL  commit  fe2f53a — Portfolio-halt: fix small-peak explosion + raise noise floor + clear script
2026-07-13 10:52  ADAM   action  ran `python3 diag_clear_halt.py` on Render → adam-live portfolio halt CLEARED. peak_pnl reset from -82.72 to current -206.81
2026-07-13 11:01  LOCAL  commit  a599a71 — Expert-driven re-entry: buy_px never above the last sale (ehlers/elder/connors/vince + orchestrator wired swing_leg.py:2994)
2026-07-13 11:08  LOCAL  commit  3273f67 — Cockpit: add 24h realized tile
2026-07-13 11:20  LOCAL  commit  6d45bd5 — Reanchor clamp: never place buy above the sleeve's last sell fill (all 3 cascade sites)
2026-07-13 11:27  LOCAL  commit  ba73713 — Per-product re-entry thresholds — plumbing for expert-chain tuning
2026-07-13 11:34  LOCAL  commit  26bba25 — Per-product re-entry threshold tuner + manual-review promote gate
2026-07-13 11:35  LOCAL  commit  b43e452 — Hamburger menu: preserve open state across 2s refresh renders
2026-07-13 11:41  LOCAL  commit  28fcfb0 — Signals tab: name the product, don't just count it
2026-07-13 11:54  LOCAL  commit  b4b69f2 — diag_refresh_portfolio.py: force a __portfolio__ snapshot pull now
2026-07-13 12:00  LOCAL  commit  9fa304c — Portfolio snapshot: staleness detection + chip + validator hint

2026-07-14 13:34  LOCAL  commit  a88a499 — Add AGENTS.md — two-agent operating agreement (NOT YET PUSHED — awaiting Adam's `git push`)
2026-07-14 13:38  LOCAL  commit  bc7d93d — Bootstrap COORDINATION_LOG.md per AGENTS.md rule 6 (NOT YET PUSHED)
2026-07-14 14:00  LOCAL  commit  a1453ac — Fix 'snap Infinityh' chip when portfolio snapshot has no _refresh_ts (NOT YET PUSHED)
2026-07-14 14:00  ADAM   action  killed local PID 14498 (Python main.py running since 2026-07-06). No launchd/crontab auto-respawn found. Local writer path definitively closed.
2026-07-14 14:15  DIAG   finding  Confirmed multi-writer at RENDER level: silver-swing-bot-paper (SWING_TENANT=adam-paper, SWING_MODE=paper, SWING_LIVE_ENGINE=1, SWING_LIVE_CONFIRM=I_UNDERSTAND) was deriving adam-live via `_derive_live_tenant` and running live engine on it — in parallel with silver-swing-bot-live (SWING_TENANT=adam-live, SWING_MODE=live). Two Render services × one adam-live scope = duplicate orders.
2026-07-14 14:20  ADAM   action  SUSPENDED silver-swing-bot-paper service on Render. Live writer path now: silver-swing-bot-live only. Multi-writer bug closed.
2026-07-14 14:20  OPEN   pending  Cancel duplicate orders on Coinbase: SLVR 27 AUG 26 (2 @ $65.25, one of two), OIL 20 JUL 26 (Buy 5 @ $74.76 stale), OIL 20 JUL 26 (Buy 1 @ $74.75 stale). Also: Adam step 1 (REDIS_URL on Mac) still pending. Also: "B authorized" sentence still pending.
2026-07-14 14:24  ADAM   action  CONFIRMED — duplicate orders cancelled on Coinbase, no new duplicates observed post-suspension. Multi-writer bug fully resolved and verified in practice.
2026-07-14 14:33  LOCAL  push    Pushed 5 commits (a88a499..836702b) to origin/main. Cloud auditor can now read AGENTS.md + COORDINATION_LOG.md on GitHub.
2026-07-14 14:37  CLOUD  review  Reviewed WS1 (redis-dedup-lock) design. Endorsed dedup-first ordering. Two blocking notes: (1) SETNX must be ON TOP OF the 2423 in-process guard, not replace it; (2) fail-closed on Redis unavailable. Also WS3: PLAN doc must answer where multipliers come from post-tuner-removal AND how signals are validated without backtest/CC before code lands. Merge order: dedup → health → paper-removal.
2026-07-14 14:45  LOCAL  commit  95ec8de — WS1 dedup lock (arm_dedup.py + swing_leg.py:2801 + 11 tests). Both auditor notes addressed. Sent to auditor for review.
2026-07-14 14:50  ADAM   go      Approved WS1 merge. Merged feat/redis-dedup-lock → main (fast-forward) and pushed. Starting WS2 (silent-failure instrumentation) on feat/health-and-safety.
2026-07-14 15:00  LOCAL  commit  305aa63 — WS2 silent-failure instrumentation (health.py + 10 sites in live_runner.py + main.py + 9 tests, all never-raise verified). Sent to auditor for review.
2026-07-14 15:05  ADAM   go      Approved WS2 merge. Merged feat/health-and-safety → main (fast-forward) and pushed. Starting WS3 PLAN doc on feat/remove-paper-lab (docs only; code phase blocked until Adam signs off on the plan's answers to auditor's two questions).

## Open items

- **Adam step 0** (LIVE-MONEY): cancel duplicate SLVR sell on Coinbase; verify Render Service → Instances == 1, kill extras; `ps aux | grep -E "main.py|live_runner"` on Mac, kill any stray runner.
- **Adam step 1**: set `REDIS_URL` (read-only) on the Mac shell so LOCAL is not blind on paper JSON.
- **LOCAL step 3** (BLOCKED until multi-writer settled): merge safety layer into `experts_reentry.py` — flag gate + `legacy_fallback` + preflight PASS/FAIL. Keep kill switch during swap.
- **LOCAL step 4** (BLOCKED until step 3 in place): resume silent-failure instrumentation on the feature branch.
- **ROBUSTNESS** (after step 0 stops the bleeding): Redis-backed dedup guard (SETNX lock keyed on tenant+symbol+side or on live_order_id) so it holds across processes.
