# PLAN — Remove paper + lab tenants (WS3, Option B)

**Status:** DRAFT — awaiting Adam's answers to the two blocking questions in §3 before any code is written.
**Branch:** `feat/remove-paper-lab`
**Author:** LOCAL (Claude Code)
**Reviewer:** CLOUD auditor
**Decision authority:** Adam

---

## 1. Goal & motivation

Delete the paper and lab tenants entirely — Adam picked Option B over the UI-only Option A.

**Why now:** Paper + lab tabs clutter the dashboard, the paper tenant was the accidental second writer that caused the 2026-07-14 duplicate-orders incident, and Adam wants a single-tenant single-purpose bot.

**What "delete" means concretely:**
- Suspend `silver-swing-bot-paper` Render service permanently (already suspended 14:20)
- Purge `adam-paper` and `adam-lab` scopes from Redis
- Remove `paper_broker.py` from the codebase
- Refactor `_derive_live_tenant("adam-paper") → "adam-live"` — live tenant must not need paper as its source
- Remove UI Paper + Lab tabs from the dashboard hamburger menu
- Remove lab seed sleeve code (`_seed_lab_comparison_sleeves`, `_default_lab_config`, `_fixup_lab_config`)
- Move backtest / champion-challenger / expert-tuner / tune_reentry_thresholds to a paper-free path (see §3)

**What "delete" does NOT mean:**
- Deleting Coinbase positions (they live on Coinbase, unaffected)
- Deleting sleeve state on `adam-live` (must be preserved through migration)
- Deleting live-order history (untouched)

---

## 2. Scope audit — what touches paper/lab today

### Code files that import or reference paper/lab
| File | What it uses | Refactor scope |
|---|---|---|
| `main.py` | `TENANT` env default, `_derive_live_tenant("adam-paper")`, `_default_paper_config`, `_default_lab_config`, `_is_lab_tenant`, `_seed_lab_comparison_sleeves`, `_fixup_lab_config`, `paper_broker` import | Substantial — main.py is paper-first architecturally |
| `live_runner.py` | `TENANT` env default = "adam" | Small — rename or unset default |
| `paper_broker.py` | Entire file (paper broker implementation) | DELETE |
| `broker.py` | Only Coinbase; no paper refs | Untouched |
| `backtest.py` | Uses paper broker for simulation | Refactor to use `paper_broker`-like sim locally (see §3) or delete |
| `backtest_worker.py` | Backtest orchestration | Same |
| `champion_challenger.py` | Uses paper for walk-forward | Same |
| `expert_tuner.py` | Uses paper for grid-search sim | Same |
| `run_go_live_check.py` | Uses paper for gauntlet | Same |
| `scripts/run_backtest.py` | Uses paper | Same |
| `scripts/migrate_sleeve_safety_2026_07_11.py` | Old migration touching paper scopes | DELETE (one-off, already ran) |
| `dashboard/server.js` | Filters modes by tenant name, may reject lab | Refactor filter logic |
| `dashboard/public/app.js` | Paper + Lab hamburger tabs, mode-menu items | Delete tabs |
| `dashboard/public/index.html` | If paper/lab mode HTML → delete | Small |
| `run_champion_challenger.py` | Uses paper tenant for evaluation | See §3 |
| `tune_reentry_thresholds.py` (yesterday) | Uses paper-like simulation | See §3 |
| `feed.py` | Might have paper feed fallback | Verify + remove if present |
| `state_store.py` | No hard-coded paper refs; scope-agnostic | Untouched |

### State store scopes to purge
- All `adam-paper/*` scope keys
- All `adam-lab/*` scope keys
- Keep all `adam-live/*` untouched

### Tests
- `tests/test_sleeve_spawn.py` — already failing pre-WS3 (unrelated); may break more with paper removal
- Any test that instantiates `PaperBroker` — refactor or delete

---

## 3. AUDITOR BLOCKERS — Adam must answer before code phase

### Q1: Where do tuning multipliers come from post-removal?

`expert_tuner.py` (Layer-2 auto-tuner) currently runs against paper tenant historical data to grid-search ATR multipliers, then caches results to `__tuned_params__`. `expert_guard.py` verifies live config matches `expert_params × tuned multipliers`. Deleting paper breaks the auto-tune path.

**Options (pick one):**

**A. Freeze current tuned params, disable auto-tuner.** Van Tharp: "stop tuning once you have working numbers." Take a snapshot of today's `__tuned_params__`, promote to durable defaults in `expert_params.py`. Auto-tuner becomes read-only historical audit. Simplest. Loses adaptive re-tuning.

**B. Rewrite `expert_tuner` to use downloaded Coinbase candles offline.** No tenant needed — pulls historical bars via `broker.list_candles(product_id, ...)`. Runs on a schedule (or manually), writes tuned params. Preserves auto-tune. Medium refactor (~200 lines).

**C. Run tuner against `adam-live` historical data.** Same paper-tenant grid-search logic, but pointed at the live tenant's own trade log + snapshot cache. Preserves the pattern. Cheapest refactor (~50 lines). Risk: tuner reads and writes on the same tenant that's actively trading — could confuse if tuner's writes race with the bot.

**Recommendation:** **A first, then B if we want adaptive tuning later.** Van Tharp is right that over-tuning is worse than under-tuning; freezing at current values eliminates a subtle failure mode and gives a clean deletion.

---

### Q2: How are signals validated without backtest / champion-challenger?

`backtest.py`, `champion_challenger.py`, `run_go_live_check.py`, and `tune_reentry_thresholds.py` (yesterday) all use paper tenant as their simulation substrate. Without them, we have no way to answer "would this new threshold have made money on the last 30 days?" before deploying to live.

**Options (pick one):**

**A. Rewrite `backtest.py` to use downloaded Coinbase candles + a lightweight in-memory `SimBroker` (not a tenant).** No `adam-paper` scope needed. `SimBroker` is a slimmed-down `paper_broker` that never touches Redis — pure Python simulation. Preserves the full pre-deploy validation loop. Medium refactor (~300 lines). Recommended.

**B. Accept no pre-deploy validation.** Rely on shadow mode (twitter_scanner, tape_shadow, avg_down_signal) + gradual rollout + Redis dedup + fail-closed defaults. New thresholds go live directly with a small-qty cap for the first N cycles. Cheapest — no code. Highest risk — the failure modes we caught this week (buy-above-last-sale, portfolio-halt lockout, multi-writer) all came from bugs that a backtest would have caught before deployment.

**C. Move to a differently-named "sim" tenant (e.g., `adam-sim`).** Basically undoes the deletion of paper conceptually, just renames it. Not really deleting paper. Auditor's exact concern.

**Recommendation:** **A.** The backtest infrastructure IS what makes live trading safe. Losing it because of a name collision with "paper" would be trading a safety net for cosmetics. Rename `paper_broker.py` → `sim_broker.py`, strip the tenant-writing paths, use as backtest engine only.

---

### Recommended combination

**Q1 → Option A (freeze tuned params, disable auto-tuner)**
**Q2 → Option A (`sim_broker.py` for backtest, no tenant)**

Both preserve safety infrastructure. Both eliminate paper as a tenant/mode concept. Delta from Adam's original ask: `paper_broker.py` code survives (renamed `sim_broker.py`, tenant-write paths stripped). If Adam wants literal "no paper_broker code at all," we accept losing the backtest engine — that's the raw B path but with maximal risk.

---

## 4. Migration plan (only starts after §3 answered)

### Phase 0: Prep (docs + safeties, no live changes)
- [ ] Adam signs off on §3 answers.
- [ ] Snapshot every Redis scope: `adam-live/*`, `adam-paper/*`, `adam-lab/*` → JSON backup in `backups/pre-remove-paper-lab-<timestamp>.json`.
- [ ] Cancel all open orders on Coinbase (Adam, dashboard).
- [ ] Suspend `silver-swing-bot-live` service briefly during cutover (~5 min planned downtime).

### Phase 1: Code changes (feature branch `feat/remove-paper-lab`)
- [ ] Rename `paper_broker.py` → `sim_broker.py`, strip `_write_paper_state` / `_read_paper_state` paths (leave pure-Python simulator).
- [ ] Refactor `backtest.py`, `backtest_worker.py`, `champion_challenger.py`, `expert_tuner.py`, `run_go_live_check.py`, `tune_reentry_thresholds.py`, `scripts/run_backtest.py`, `run_champion_challenger.py` to import `SimBroker` from `sim_broker` (no tenant needed).
- [ ] Refactor `main.py` `_derive_live_tenant` — accept `SWING_TENANT=adam-live` directly, don't require `adam-paper` input.
- [ ] Delete `_default_paper_config`, `_default_lab_config`, `_is_lab_tenant`, `_seed_lab_comparison_sleeves`, `_fixup_lab_config`, related lab code paths.
- [ ] Update `live_runner.py` TENANT default from "adam" to "adam-live" (or make required, no default).
- [ ] Delete UI Paper + Lab tabs from `dashboard/public/app.js` hamburger menu.
- [ ] Update `dashboard/server.js` mode-filter logic to drop paper + lab cases.
- [ ] Update tests: fix or delete anything referencing paper/lab tenants specifically.
- [ ] Delete `scripts/migrate_sleeve_safety_2026_07_11.py` (one-off, already ran).

### Phase 2: Env config changes (Render)
- [ ] `silver-swing-bot-live`: `SWING_TENANT=adam-live` (already set — verified 2026-07-14).
- [ ] Delete `silver-swing-bot-paper` service entirely (currently suspended).
- [ ] Update `silver-swing-dashboard` if it filters paper/lab modes.

### Phase 3: Migration script (Redis scope purge)
- [ ] Write `migrate_remove_paper_lab.py` — reads backup, deletes `adam-paper/*` and `adam-lab/*` keys from Redis, verifies `adam-live/*` intact. `--dry-run` first, `--confirm` to execute.
- [ ] Adam reviews dry-run output.
- [ ] Adam runs `--confirm`.

### Phase 4: Deploy + verify
- [ ] Merge `feat/remove-paper-lab` → main → push.
- [ ] Render auto-deploys `silver-swing-bot-live` + dashboard.
- [ ] Adam re-arms any sleeves that need re-attaching (sleeve configs preserved via backup; state per §3 answer).
- [ ] Verify live continues to cycle correctly for 24 hours.

### Phase 5: Cleanup
- [ ] Remove backup JSON after 30 days if stable.
- [ ] Update AGENTS.md to remove references to paper/lab.

---

## 5. Rollback plan

If ANY phase fails or live starts behaving badly:

- **Phase 1-2 rollback:** `git revert <merge-sha> && git push` — reverts code. Render auto-redeploys. `silver-swing-bot-paper` service must be resumed via dashboard (was only suspended, not deleted).
- **Phase 3 rollback:** Migration script writes `adam-paper` and `adam-lab` scopes back from the JSON backup. `--restore` flag on the same script.
- **Phase 4 rollback:** No new state to roll back; just revert code + restore scopes.

Backup JSON at `backups/pre-remove-paper-lab-<timestamp>.json` is the source of truth for restoration.

---

## 6. Manual actions Adam has to do

Cannot be automated. Adam performs:

1. Approve §3 answers.
2. Cancel all open Coinbase orders before cutover (~5).
3. Approve backup JSON contents (dry-run print).
4. Approve migration `--dry-run` output.
5. Run migration `--confirm`.
6. Re-arm sleeves post-cutover (any sleeves that lost cycle-history state per §3 answer).
7. Watch live for 24h post-cutover, ready to invoke rollback.

---

## 7. Sleeve-state preservation

The state that MUST survive migration (Adam explicitly asked about this):

**Per adam-live sleeve:**
- Sleeve config (id, name, qty, sell_px, buy_px, spread, stop_loss_px, ratchet params, expert flags)
- Sleeve state (cycles, realized_pnl, own_avg_entry, live_order_id, stop_loss_hwm, recent_cycle_pnls)
- Cost basis attribution

**Preservation approach:** the migration script touches ONLY `adam-paper` and `adam-lab` scopes. `adam-live` is not modified. Sleeve state survives untouched. Adam does NOT need to re-arm sleeves if backup + migration are clean.

**Contingency:** if migration accidentally touches `adam-live` (bug), the JSON backup contains everything needed to restore.

---

## 8. What's out of scope

- Renaming `adam-live` to `adam` (bigger change, no benefit)
- Deleting sleeve concept
- Deleting the champion-challenger promotion pattern (staying, just moves off paper tenant)
- Deleting existing trade log history

---

## 9. Estimated effort

- **Adam's blocker answers (§3):** minutes
- **Backup + migration script:** 1-2 hours to write, 30 min to run + verify
- **Code refactor (`paper_broker` → `sim_broker`, main.py, live_runner.py, dashboard):** 4-6 hours
- **Testing / verification:** 1-2 hours
- **Deploy + 24h observation:** 1 day elapsed

Total: 1-2 dev days for the code, 1 day observation.

---

## 10. Sign-off

- [ ] Adam approves §3 Q1 answer: ___
- [ ] Adam approves §3 Q2 answer: ___
- [ ] CLOUD auditor reviews this plan and approves migration approach.
- [ ] Adam grants explicit go for Phase 1 (code changes).
- [ ] Adam grants explicit go for Phase 3 (--confirm on migration).

Only ONE checkbox at a time — serialize per AGENTS.md rule 5.
