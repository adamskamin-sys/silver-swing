# Two-agent operating agreement — silver-swing

Two Claude agents work on this bot. This file keeps them from conflicting.
It binds the LOCAL agent (which reads AGENTS.md). The CLOUD auditor is read-only
by construction.

## Roles

**LOCAL** — Claude Code on the Mac / Render. The **sole writer**. Owns every
edit, commit, push, deploy, and live-state change (`__reentry_mode__` flips,
`diag_clear_halt.py`, running `diag_*.py`). Has repo + Render + REDIS_URL.

**CLOUD auditor** — the scheduled session. **Read-only.** Cannot commit, deploy,
or flip scopes (no bridge, no Redis). It reviews diffs/logs you paste, interprets
live grep output, and writes the morning health report. If it proposes a change,
it hands LOCAL a patch — it never applies one.

## Hard rules (these are what prevent conflict)

1. **Single writer.** Only LOCAL writes to repo / Render / Redis. Never run two
   writer sessions against the same working tree at once — that's a git race.
   For parallel work use a branch or a `git worktree` per agent.

2. **One re-entry module.** `experts_reentry.py` (wired at swing_leg.py:2994) is
   canonical. Do NOT add a second `expert_reentry.py`. The cloud auditor's safety
   layer — `__reentry_mode__` flag gate, `legacy_fallback` on missing helper /
   exception, the `buy_px < reference` refusal, and `reentry_preflight.py` — is
   merged INTO `experts_reentry.py`, not shipped beside it.

3. **Single source of truth.** All health/audit conclusions read the **Redis prod
   log** (via Render or REDIS_URL). NEVER conclude health from local
   `data/trades.jsonl` — it's stale paper data and will report false-healthy.
   Set `REDIS_URL` (read-only) in BOTH the Mac env and the scheduled-task env.

4. **In-flight work stays on a branch.** While mid-change (e.g. health.py
   instrumentation), commit to a feature branch, not `main`, so a half-written
   file can't collide with a live-money deploy. Merge to `main` only when green:
   `py_compile` clean AND `reentry_preflight.py` prints `PREFLIGHT: PASS`.

5. **Serialize deploys.** One change reaches live at a time. Clearing the halt or
   flipping `__reentry_mode__` happens only AFTER the prior deploy is confirmed on
   the next sell cycle. Don't stack an unconfirmed live-money change under new work.

6. **Handoff log.** Append one line to `COORDINATION_LOG.md` per material action
   (commit sha, deploy, scope flip, halt clear) so the other agent has ground
   truth instead of guessing.

## Cheat-sheet — who does what

| Action | Owner |
|---|---|
| Edit / commit / push / deploy | LOCAL |
| Flip `__reentry_mode__` / clear halt | LOCAL |
| Run `diag_*.py` on Render | LOCAL (paste output to CLOUD for interpretation) |
| Morning health report / drift interpretation / patch authoring | CLOUD |
| Final go/no-go decision | Adam |

## Current state (update as it changes)
- `experts_reentry.py` is the single re-entry module (commit a599a71, wired 2994).
- No `expert_reentry.py` in the tree — keep it that way (rule 2).
- New re-entry protections pushed; take effect on the NEXT sell fill after redeploy.
- Portfolio halt cleared 10:52. `__reentry_mode__` per-tenant state: <fill in>.
- REDIS_URL: set on Render; NOT yet on the Mac / scheduled-task env (rule 3 gap).
