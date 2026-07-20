# silver-swing constitution

The rules Adam and Claude have agreed on. Every rule below is a
binding constraint; violating any of them is a bug that must be
fixed. Rule #1 is the umbrella — all narrower rules serve it.

---

## §1 — The Biggest Rule

**Don't lose money AND take profit at its best.**

Every trading decision, code change, and design choice must serve
these two goals. If a rule below appears to conflict with §1 in
some edge case, §1 wins and the narrower rule needs a caveat.

---

## §2 — How Claude works with Adam

### §2.1 Do what Adam asked. Nothing more, nothing less.
No silent substitutions. No "I noticed X while I was in there" bundled
changes. If a related-but-unrequested improvement is worth doing, ASK
first ("noticed X — want me to change it?").

### §2.2 Never ask Adam to do what Claude can do itself.
If the task can be executed via Bash, Edit, Write, Grep, Read, or
Agent — Claude does it. If a code change removes the need for the
ask entirely (auto-heal, auto-detect) — ship that instead. Only ask
for actions Claude physically cannot perform (IP-locked prod access,
interactive login, eyes-on-pixels). State the physical constraint
explicitly when asking.

### §2.3 Save every correction + preference to memory the same turn.
Durable memories in
`/Users/adamkamin/.claude/projects/-Users-adamkamin-silver-swing/memory/`.
Do not wait for Adam to repeat himself.

### §2.4 Prove work is done; do not claim it.
Every "done" claim needs checkable evidence: commit SHA on
`origin/main`, `git diff` output, `grep` result, dashboard screenshot,
or a specific line reference. "Committed and shipped" that isn't
committed and shipped is a lie, not a mistake.

### §2.5 Cite sources + confirm before automating ANY new trade
decision.
Entry, re-entry, exit, wait, stop, trail, spread, regime
classification: cite the paper/method, propose the design, WAIT for
Adam's "go" before writing code. Mechanism changes that enforce an
already-made decision (e.g. market-sell to execute an already-set
trail level) are NOT new decisions and do not need the cite-and-wait.

### §2.6 Cure the disease, not the symptom.
Trace to origin. Ship the root fix first. Reactive safety nets are
added AFTER and ALONGSIDE the root fix, never instead of it. Every
manual diag command Adam has to type is a signal the auto-path is
broken.

### §2.6.1 State-mutating auto-code must be idempotent.
Any code that credits cycles, adjusts realized_pnl, clears
own_avg_entry, or writes ANY "we did this trade" state on a
periodic trigger (per-tick, per-poll) must have one of: a hard
"already done" check keyed on the specific event (oid, fill_id,
timestamp), a self-clearing state flag that flips inside the
mutation, or a rate limit. Without one of these, the reload-on-tick
pattern turns 5 minutes of missed detection into 60 iterations of
fake data. CHN 2026-07-19 turned $0 realized into +$555 fake
profit in 5 minutes because of exactly this bug.

### §2.7 Follow all of Adam's instructions as given.
Do not filter, do not paraphrase, do not "improve." His words are the
spec. Supersedes any narrower design preference elsewhere in memory.

---

## §3 — Trading rules (what the bot must do)

### §3.1 Bot mark ↔ Coinbase mark must always be in sync.
Every $-denominated calc AND every price-triggered action reads from
a mark refreshed within seconds. On drift → force refresh + WS
reconnect, keep trying, NEVER halt. Applies to all products, all
trigger types.

### §3.2 Every held product gets ticked.
Per-product WS feed + per-product SwingTrader. No "primary is
special" carve-outs. If a held product exists in `__portfolio__` but
no active trader ticks it, that is a blocker.

### §3.3 Once a sleeve owns a position, exit params are FROZEN.
`sell_px`, `trail_distance`, `stop_loss_px` are set at arm time and
do NOT change per-tick during a hold. No expert override, no
adaptive widening or tightening. Experts decide the NEXT re-entry,
not the current exit.

### §3.4 Trail-stop arms at buy fill; ratchets UP only; never sells
below own_avg on the take-profit path.
- On buy fill: `trail_armed=True`, `hwm=own_avg`.
- Trail floor: `max(own_avg + fee_safety, sell_px_if_hit,
  hwm − trail_distance)`.
- Ratchet-up-only. Never lower the exchange stop.

### §3.5 Trail exit must never fall below the original SELL target.
Checkpoint-then-ratchet: once profit-locked at sell_px, trail can
only tighten (fire at HIGHER price) as HWM rises. Never drifts back
below the goal.

### §3.6 Resting ratchet-stop must NEVER leave a held position
unprotected.
HARD invariant. Post-place verify, gap watchdog every tick, critical
alert on divergence, fallback wider stop on place failure. If
`target > mark` on a trail stage, market-sell now (Cartea/Jaimungal
ch.10 stop-triggered execution).

### §3.7 No net-loss cycles from take-profit fires.
Every reanchored `sell_px` must clear `buy_px + fees + safety_margin`
via the fee-floor clamp in `_reanchor_sleeve`. Only `stop_loss` exits
may close red.

### §3.8 No shorting on adam-live.
Every SELL must reduce a held long, never exceed it. $/day objective
is LONG-side only. Orphan resting SELLs are the failure mode — cancel
via `diag_find_orphan_order.py` + `diag_cancel_orphan_order.py`.

### §3.9 Scale up only off REALIZED gains.
Auto-accumulation triggers on realized cycles, never on unrealized
MTM. Van Tharp / Turtle rule.

### §3.10 Optimize realized $/day = cycles × $/cycle.
Tiebreaker toward MORE cycles. Applies to stop-limit gap, buy/sell
offset, trail distance, scanner ranking.

### §3.11 No ghost sleeves — bot self-heals.
Any sleeve state that disagrees with exchange truth (position 0 with
own_avg set, or vice versa) must auto-recover on the next tick. No
manual diag_clear as a required step. Manual diags are audit tools,
never operational tools.

### §3.12 Tenant scoping is `-live` shaped.
Every path that touches the live tenant derives it via the guarded
`f"{TENANT}-live" if not TENANT.endswith("-live") else TENANT`
pattern. No bare `f"{TENANT}-live"`.

### §3.13 Validate every product_id against Coinbase before use.
Scanner arms, manual add, sleeve create: call `contract_spec` and
refuse the save if the product isn't tradeable.

### §3.14 Always verify contract_size against Coinbase.
Every $-denominated projection must use Coinbase-verified
contract_size, not cached values.

---

## §4 — Safety guardrails (never do this)

- Never delete production data without explicit confirmation.
- Never bypass guards Adam built (kill switches, preflight validators,
  `--no-verify`).
- Never place or modify live orders while exploring.
- Never push to main without verifying the code parses (`py_compile`
  / `node --check`) and tests pass where relevant.
- Never make claims about Anthropic refund/credit policy — API
  credits are non-refundable.
- Never commit files that likely contain secrets (`.env`,
  `.coinbase_key.json`, `credentials.json`).

---

## §5 — Working with the codebase

### §5.1 Render shell diagnostic format is a HARD rule.
Adam's shell mangles multi-line paste. Only pattern that works:
Write `diag_X.py` → `py_compile` → commit + push → wait 30-60s for
auto-deploy → Adam types `python3 diag_X.py`. No heredocs, no
`python3 -c`, no exceptions.

### §5.2 Syntax-check every code change before committing.
Mandatory `node --check` / `python -m py_compile`, especially after
bulk regex edits.

### §5.3 Ship it all = commit + push + deploy authorization.
When Adam says "ship it," it's a batch approval for the tested
changes on the table.

### §5.4 Dashboard numbers must sync to Coinbase within seconds.
5s periodic refresh + on-fill immediate refresh. Every
$-denominated display traces to a fresh `__portfolio__` snapshot.

---

## §6 — Meta

### §6.1 Every rule in this document is binding.
No exceptions for "just this once," efficiency, or "it would be
better if." If a rule feels wrong in the moment, propose an
amendment to Adam — do not violate it silently.

### §6.2 This document is the source of truth.
When a memory file, CLAUDE.md snippet, or comment in code disagrees
with this constitution, this constitution wins. Update the other
place to match.

### §6.3 Amendments require Adam.
Any addition, removal, or change to a rule needs Adam's explicit
"yes" — same standard as automating a trade decision.

### §6.4 The memory index (`MEMORY.md`) links to detailed rules.
This constitution is the summary; individual `feedback_*.md` and
`project_*.md` files in memory carry the full history + rationale.
Read those when you need to know "why" a rule exists, not just
"what" it says.
