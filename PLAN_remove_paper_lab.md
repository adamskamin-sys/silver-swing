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

### Auditor refinements (2026-07-14 15:15 review)

**Q1 clarifications** (before freezing):
- **Which baseline?** "Frozen last-tuner-output" ≠ "canon published values". These are different things. Choose deliberately:
  - **Baseline B1**: freeze whatever is currently in `__tuned_params__` (last tuner run's house snapshot). Preserves whatever adaptive tuning caught. Risk: if the last tuner run overfit, we freeze overfit.
  - **Baseline B2**: revert to `expert_params.py` published canon (Le Beau chandelier, Van Tharp 0.5R, etc.) — the reference values before any Layer-2 tuning. Loses adaptive gains, gains publish-worthy defensibility.
  - **Baseline B3**: hand-pick per product from a tuner report reviewed by Adam. Most work, most defensible.
  - LOCAL default recommendation: **B2** for cleanest reasoning (canon values are the audit trail); can rerun tuner offline via sim_broker later if adaptive tuning wanted back.
- **One-way door.** Deleting `expert_tuner.py` infrastructure means auto-tuning can only be restored by re-authoring it. Accept explicitly.
- **Audit staleness check.** `expert_guard.py` (or the daily audit's "tuning stale >2d" check) will false-alarm forever once the tuner is off by design. Must be removed or replaced with a "tuner intentionally frozen" mode.

**Q2 merge gate** (before sim_broker code lands):
- **Test contract**: `tests/test_sim_broker_isolation.py` MUST include tests asserting `SimBroker` CANNOT:
  1. Place a live order (mock Coinbase client, assert no `place_limit` / `place_market` calls)
  2. Write to any live-tenant scope in Redis (mock store, assert no `put_state`/`put_config` calls where tenant contains "live")
  3. Reach `_derive_live_tenant` via any import path (static check: `grep -r "derive_live_tenant" sim_broker.py` returns empty)
  Even given live env vars (`SWING_TENANT=adam-live`, `SWING_LIVE_CONFIRM=I_UNDERSTAND`), these paths must be dead code.
- **Auditor's rationale**: "Stripped the paths" is not evidence. Prove it. This is exactly the footgun that caused the incident.

### Auditor refinement (2026-07-14 15:50 — second review)

**Broker fate — pick ONE, doc was ambiguous.** LOCAL said three different things across the plan: "delete paper_broker.py entirely" (§1), "rename → sim_broker.py, strip tenant-write paths" (Phase 1), "lightweight in-memory SimBroker" (Q2 recommendation). These are different approaches with different risk profiles. Adam picks:

- **B1 — Rename-and-strip**: keep `paper_broker.py` file, rename to `sim_broker.py`, delete the tenant-write / Coinbase-touching code paths inside. Risk: RESIDUAL LIVE PATH could survive if we miss a code path. Merge-gate test mitigates.
- **B2 — Delete-and-write-fresh**: delete `paper_broker.py`, write a brand-new `sim_broker.py` from scratch — pure Python fill simulator with a well-defined interface (no legacy paths). Risk: NEW SIM BUGS could produce misleading backtest results. Same merge-gate test contract.

Both paths require the Q2 merge-gate test contract above. LOCAL recommendation: **B2** (delete-and-fresh). A fresh ~200-line module we own is easier to audit than a stripped-down ~500-line legacy file where "we removed the live path" is a claim without proof at the code level.

**Q1 sharpened — Adam picks the actual mechanism, not just "approve freeze."** Auditor: the word "freeze" was too loose. The real choice is:

- **Q1-Frozen**: freeze the tuning baseline (either B1 last-tuner-output, B2 canon values, or B3 hand-picked per §3 Q1 above). `expert_tuner.py` refactored to NOT run automatically; can still be invoked manually as a one-shot report. Kills adaptive tuning permanently unless re-authored.
- **Q1-Offline**: refactor `expert_tuner.py` to run on downloaded Coinbase candles (no tenant needed). Preserves adaptive tuning. Aligns with "everything expert-driven" — tuner keeps producing new numbers on a schedule, `__tuned_params__` scope stays live, tuner runs against real market data instead of a paper-tenant simulation.

`expert_tuner.py` is refactored in EITHER path — not deleted (auditor correction; the plan earlier said "delete tuner infra"). LOCAL recommendation update per auditor's steer: **Q1-Offline**. Preserves the adaptive-tuning premise of the codebase; if Van Tharp's "stop tuning" wisdom is preferred later, Adam can flip a `TUNER_DISABLED=1` env var without a code change. Cheaper to have and not use than the reverse.

**Audit tuning-freshness check must match the chosen Q1 path:**
- If **Q1-Frozen**: DROP the "tuning stale >2d" check from the daily audit (it will false-alarm forever by design).
- If **Q1-Offline**: repoint the check at the offline tuner's `tuned_at` timestamp (not the live-tenant tuner's cache).

**Goal A/B sequencing (auditor emphasis): Goal A ships FIRST as its own change, on a separate day from Goal B.** Multi-writer safety hardening (§8b Goal A) is not gated on Goal B. Do it now, deploy independently, verify. Goal B is a separate deliberate cutover — auditor: "not the same day."

---

## 4. Migration plan (only starts after §3 answered)

### Phase 0: Prep (docs + safeties, no live changes)
- [ ] Adam signs off on §3 answers.
- [ ] Snapshot every Redis scope: `adam-live/*`, `adam-paper/*`, `adam-lab/*` → JSON backup in `backups/pre-remove-paper-lab-<timestamp>.json`.
- [ ] **Test-restore the backup** (auditor requirement 2026-07-14 15:15). Run migration `--restore` on a scratch Redis key or a local file store; verify all keys re-materialize with identical content. Rollback is only valid if the restore path is proven.
- [ ] **Confirm removing `adam-paper` has NO cascade to `adam-live`** (auditor). Static analysis: verify no runtime code path reads `adam-paper` state and writes to `adam-live` scope. Test: delete `adam-paper` scopes in a scratch store, run one live tick, assert live behavior unchanged.
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

## 8b. STRATEGIC DECISION — split the ask (auditor 2026-07-14 15:15)

Auditor raised a sharp question Adam should answer BEFORE any code:

**Two independent goals are getting merged in this plan.** They're separable:

### Goal A — "Harden the `_derive_live_tenant` footgun" (SAFETY)
- **What**: prevent the multi-writer bug from ever recurring architecturally, regardless of paper/lab existence.
- **How**: add explicit config (e.g. `SWING_TENANT_ROLE=writer|readonly`, or refuse to run if `_derive_live_tenant(SWING_TENANT)` collides with another running service's declared tenant) + startup assertion + Redis dedup lock (already shipped `95ec8de`).
- **Cost**: ~50 lines, one branch, ships in an hour.
- **Blast radius**: near-zero. Adds a guard, doesn't change existing semantics.
- **Reversible**: fully. Config toggle.

### Goal B — "Remove paper/lab" (SIMPLIFICATION)
- **What**: eliminate paper + lab tenants + concepts entirely per this plan.
- **How**: as documented in §4.
- **Cost**: 1-2 dev days + 1 day observation.
- **Blast radius**: high — architectural change on live-money infrastructure.
- **Reversible**: expensive (rollback via git + Redis restore).

### Do you need both?

- **If your reason for wanting B was safety** — Goal A gets you 90% there for 5% of the risk. The current setup (paper suspended, live-writer isolated, dedup lock live) is already safe post-suspension. Add Goal A on top and multi-writer is architecturally impossible, not just conventionally avoided.
- **If your reason for wanting B was simplification** — the tabs, the tenants, the concept — then B is the answer. Goal A doesn't touch what you see in the dashboard.
- **If both** — do Goal A first (SAFETY, this week), then B when there's a clear-day window (SIMPLIFICATION, next week or beyond).

**Auditor's timing**: "land WS3 LAST, after dedup-lock + health have baked and the bot is confirmed stable post-incident. No same-day rush on the highest-risk change."

**Adam's call**: which of these three?
- **(1) Goal A only** — ship safety fix now; leave paper/lab in place; revisit B later or never.
- **(2) Goal B only** — full removal per §4, wait until dedup + health have baked ~24-48h.
- **(3) Both, sequenced** — Goal A now; Goal B in a scheduled cutover next week.

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
