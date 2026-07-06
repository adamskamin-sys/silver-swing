# Swing Trading Bot + Dashboard — Build Spec (v1, living draft)

This is the marching-orders document for building the swing-trading system. Hand it to
Claude Code to build against the real repo. It captures every design decision made so far.
Sections marked **[OPEN]** need a value or decision from Adam before that part goes live.

The guiding principle throughout: **the machine executes the trader's decisions safely — it
does not make market predictions.** Anything described as "recommended" is a deterministic
computation from indicator rules, not a forecast. The human approves every re-anchor and
strategy change.

---

## 0. Architecture at a glance

Three separate processes, sharing exactly one datastore:

```
  DASHBOARD (Express, private)  --writes CONFIG-->  [ SHARED STORE ]  <--reads/writes STATE--  BOT (worker)
        ^                                          (Render Key Value                                  |
        |                                            or Postgres)                                     |
        +--------------- reads STATE for display ----------+                                          |
                                                                          BOT is the ONLY holder of --+
                                                                          exchange API keys, and the
                                                                          ONLY thing that places orders.
```

- **Config** (levels, sizes, toggles, presets) is written by the dashboard, read by the bot every loop.
- **State** (position, current leg, P&L, swing size, high-water marks) is written by the bot, read by the dashboard.
- The bot re-reads config at the top of each cycle, so dashboard changes take effect on the
  next loop — **live, no redeploy.**
- The dashboard **never** places orders and **never** holds exchange keys. Worst case if the
  web layer is compromised: an attacker edits numbers (bounded by server-side validation and
  the hard core floor), not a direct pipe to the account.

---

## 1. Exchange access [OPEN — first decision, gates the Broker adapter]

Determine how the SLVR contracts are actually reached:

- **Path A — Coinbase Advanced Trade API:** clean REST + WebSocket, official Python SDK, one
  CDP key. Applies if trading US perpetual-style or international silver perps. Preferred if available.
- **Path B — Coinbase Derivatives Exchange via FCM/broker:** the classic dated/nano contracts.
  Access is through a CFTC-regulated FCM (StoneX, Marex, Dorman, Advantage, etc.) using FIX/SBE/UDP,
  or whatever API that broker exposes. Requires a broker-specific adapter.

**Action:** identify the exact contract ticker and the FCM. That determines which `Broker`
adapter gets built.

**Security (non-negotiable):** the trading API key must be scoped **trade-only, no withdrawal
permission.** Keys live only in the bot process's environment variables, never in the repo,
never in the dashboard.

---

## 2. Core bot: single-active-leg state machine

The foundation (already prototyped in `swing_leg.py`).

- **Only ONE order is ever live on the exchange at a time.** The dormant leg lives in state,
  not on the book. This structurally makes it impossible for the rebuy to fill before the sell.
- **Fills are confirmed by order status, never by price.** Price touching a level ≠ a fill.
- **Full fills only** flip the state. Partial fills keep the current leg live.
- **Persisted state + `reconcile()` on startup:** on restart, trust the book, not memory —
  read actual open orders and position and recover the correct leg before acting.

State machine:
```
ARMED_SELL --(sell leg fills)--> ARMED_BUY --(buy leg fills)--> ARMED_SELL ...
                                     (HALTED is reachable from either via the risk governor)
```

### 2A. Pre-trade fee gate [CHECK THE REAL FEE BEFORE FIRING]

Before any queued order is placed, the bot verifies the trade still clears net *at the real fee* —
not the assumed one. This is a pre-trade gate, so a bad-fee trade is caught **before** you're in it,
not reconciled after.

**Sequence at execution:**
1. Trade is queued (e.g. sell `swing_qty` at the computed level).
2. Pull the **actual fee** this trade will incur — from the venue's current fee-tier/rate endpoint
   (and the live spread from the book). Fees are computed as `per_contract_fee × current swing_qty`,
   never a fixed total (see §3A — size grows, so fees grow with it).
3. Recompute **net after that real fee** and compare to the slider target (§5A).
4. Branch:
   - **Net still clears** → fire the order.
   - **Net no longer clears** → **auto-adjust the level to preserve net** (widen the gap so the net
     target still holds), then fire at the adjusted level. Take-home is always protected. Accepted
     tradeoff: a wider level is one price must actually reach, so some adjusted trades won't fill —
     preferred over taking a trade that erodes net.
   - **Sanity ceiling (guardrail on the auto-adjust)** → if preserving net would require widening
     **beyond an abnormal threshold** (fee/spread blowout, data glitch, liquidity event — not normal
     drift), **HALT and alert** instead of chasing the level somewhere absurd. In normal operation
     this ceiling is never hit; it only trips on genuinely broken conditions so "always auto-adjust"
     can't be turned against you by bad data. **[OPEN: set the abnormal-widen threshold.]**
5. **Post-fill reconciliation:** compare the *actual* fee reported on the fill against the assumption.
   Persistent mismatch (e.g. a silent tier change) → flag it so the config baseline gets corrected.

**Capability requirement [HARD]:** this depends on being able to see/derive a queued order's fee
**before** submitting. Advanced Trade exposes fee tiers + per-fill fees cleanly; an FCM route may
report fees differently. Confirming this capability is part of resolving the access path (§1).

---

## 3. Two-bucket position model + protected core

- **`core_qty`** — the hard floor, never sold (default 10). 
- **`swing_qty`** — the playable size the bot actively trades (start 2 = 12 held − 10 core).
- **Invariant enforced before EVERY sell:** `position − swing_qty ≥ core_qty`. If a sell would
  breach the floor (from a manual trade, an odd partial, anything), the bot **HALTs** instead of
  selling into the core. This is checked against the *actual* live position, not assumed state.

### 3A. Contract specs + count control [PER-INSTRUMENT, USER-OWNED]

The user is in charge of **two distinct things** — don't conflate them:

1. **How many contracts to trade** — `swing_qty` (the playable size) and `core_qty` (the floor).
   Fully user-set, live-editable, and independent in paper vs. live. This is the "I want to swing
   2 / 3 / 5 contracts" control.
2. **The contract's own specs** — a per-instrument config block that *defines what a swing is worth*.
   Everything downstream (the §5A slider math, the fee model, all P&L) reads from this block, never
   from a hardcoded assumption:
   - `contract_size` — units per contract (e.g. 50 oz for SLVR). **A $1 move × 50 oz = $50/contract.**
   - `tick_size` — minimum price increment.
   - `tick_value` — cash value of one tick (= `tick_size × contract_size`).
   - `margin_per_contract` — maintenance margin (from FCM).
   - `commission_per_contract` + typical `spread` — the cost inputs.

**Why this matters:** a "$1 swing" is only $50/contract *because* silver is 50 oz. Trade a mini, a
different metal, or a crypto perp and `contract_size` / `tick_value` change — so the slider, the
minimum-viable-swing guard, and the P&L must all compute from the spec block, not assume silver's 50.
Getting the spec block right is what makes the take-home number honest across instruments.
**[OPEN: real contract specs per instrument — pull from the venue/FCM contract specification.]**

---

## 4. Profit-funded scale-up (2 → 3 → 4 …)

Adam's rule: **never add the next contract until banked profit covers it.**

- Growth happens **at the BUY leg only** — when funded, the bot buys one *more* than it last
  sold (sold 2 → rebuy 3), lifting the swing high-water mark (10↔12 becomes 10↔13) while the
  floor stays nailed at `core_qty`. Growing on the sell side would momentarily dip below the
  floor, so it is never done that way.
- **Gate:** add a contract only when net banked profit beyond already-committed margin covers
  `margin_per_contract × scale_up_buffer_mult`.
  - `scale_up_buffer_mult = 1.0` means "add once profit literally covers one contract's margin."
    Adam wants roughly this; a value >1.0 adds a cushion. **[OPEN: confirm value]**
  - "Covers a contract" = the **margin** to hold it (small), NOT the full notional (~$3,250 at 65×50). **[OPEN: real `margin_per_contract` from FCM]**
- `reserved_margin` prevents spending the same profit twice — once a contract's margin is
  committed, that money no longer counts toward funding the next one.
- **Net, not gross:** realized P&L must subtract fees. **[OPEN: real `fee_per_contract_roundtrip`]**
- **Later:** fractional / 0.5–1.0 point swings. Flag when building: at tighter ranges fees become
  a large fraction of each cycle, so the fee number stops being a rounding error.

---

## 5. Exit mode: fixed-limit vs trailing-stop (ratchet) [TOGGLEABLE, per instrument]

Solves the "sold at 65, woke up to silver at 80" failure mode. `exit_mode` is a per-instrument toggle.

- **`fixed_limit`** — the classic swing. Resting sell-limit at `sell_px` (e.g. 65), rebuy at
  `buy_px` (e.g. 63). Correct while the instrument is ranging.
- **`trailing_stop`** — arms at the trigger (65) but does **not** sell; instead trails a stop
  below price that ratchets up as price rises, and only fires when price falls back through it.
  Rides a breakout up toward 80 and sells on the roll-over instead of capping out at 65.
  - Trail distance is configurable: fixed amount (e.g. 0.20) or **ATR-based** so it widens in
    high volatility and tightens in calm. **[OPEN: default trail distance / ATR multiple]**
  - **High-water mark MUST be persisted state** — a restart mid-trail must remember how high it
    trailed, or it forgets and mis-places the stop.
  - **[OPEN: does the FCM support a native trailing-stop order?]** If yes, use it. If no, the bot
    synthesizes it (track high-water, move a plain stop tick by tick) — more moving parts.

**Honest tradeoff to keep in the UI copy:** a trail gives back profit by design (you always
sell below the peak by ~the trail distance). Too tight → stopped out by noise and you miss the
run; too wide → give back a lot. No perfect number; it's a tuned tradeoff and differs by regime.

### 5A. Target-profit slider [PRIMARY DRIVER — set take-home, not raw levels]

The user should drive the swing from a single slider: **"how much do I want to net per swing"** —
not by hand-setting `buy_px`/`sell_px`. The slider expresses **net profit after fees**, and the
system works backward to place the real orders. Set it to $0.50 and that's $0.50 in pocket, not a
$0.50 gap that fees then eat.

**The back-calculation (this is the whole point):**
- Slider value = target **net** profit per swing (per contract, or total — user's choice).
- The system adds the **round-trip commission + current spread** (and funding if held through a
  window) back on top of the target.
- That sum becomes the actual price gap placed between buy and sell.
- Example: $0.50 net target + $0.30 round-trip costs → the bot must capture **$0.80** of price
  movement to hand you $0.50. The slider hides that; you think take-home, the bot places gross.

**Cost-gated minimum (can't set a losing target):**
- The slider's low end is **gated by the real fee + spread numbers** — it will not let you drag the
  target below what costs allow. If round-trip cost is $0.30, a $0.05 net target is impossible
  (you'd need to capture $0.35 for a nickel, with bad fill odds), so the slider stops above that and
  shows *why*. You literally cannot arm a target that mathematically loses to fees.
- This is the "minimum-viable-swing guard": smallest swing width that still clears costs by a margin
  the user sets. Refuse to arm anything tighter.

**Applies to BOTH exit modes:**
- **Fixed-limit:** `sell_px − buy_px` = target + fees + spread. Direct.
- **Trailing/ratchet:** there's no fixed sell price, so the net target becomes a **minimum lock-in**
  — the trail may not fire for a gain smaller than (target + fees). It rides *higher* than target on
  a breakout, but never trail-stops you out for less than the fee-adjusted minimum. Same slider,
  same "net after fees" promise, applied as a floor on the trailing exit.

**Live inputs:** the back-calculation needs the real per-instrument commission and a live/typical
spread reading (spread pulled from the order book; commission from config). Recompute whenever the
slider moves or the spread shifts materially. Validate in paper first — drag to $0.50, watch the
fees-paid line and the fill rate, and see whether tight swings actually clear before real money.
**[OPEN: real commission + typical spread per instrument — same values the paper/fee model needs.]**

### 5B. Trail-distance modes [MANUAL + ENSEMBLE-COMPUTED]

How wide the trailing stop sits. Same pattern as the strategy selector: the books give a computed
starting number, the user can always override. Three modes:

- **Manual** — user types the number (fixed cents, or an ATR multiple they pick). Full control.
- **ATR-based (recommended default)** — `trail = ATR × multiplier`, computed live. This is the sane
  default because it adapts to the instrument and the moment — wider when jumpy, tighter when calm —
  so you're not re-guessing a fixed number every week. Starting range ~1.5–2.5× ATR (tighter for
  scalps, wider for trend-riding). **[OPEN: default ATR multiplier per asset class.]**
- **Structure-based (Carter)** — trail sits just under the most recent swing low / pivot rather than
  a fixed distance, so the stop rests under real support. Needs pivot detection; choppier to build.

**Author mapping (the "recommended" trail is assembled from the ensemble):**
- **Width ← Williams + ATR.** Volatility-breakout thinking is built on the day's range; ATR is that
  idea as a distance. Williams+ATR set *how wide*.
- **Anchor ← Carter.** Trail beneath structure/pivots, not an arbitrary offset. Carter sets *where*.
- **Cap ← Jim Paul.** Paul doesn't set the trail — he caps its give-back. The `abort_below` governor
  is the hard "this far and no further" so a loosening trail can't turn a winner into a real loss.
- **Appropriateness ← Kleinman/structure gate.** Decides *whether* to trail at all (breakout worth
  riding) vs. use a fixed exit (range) — not the distance itself.

**Ties into the slider (§5A):** whatever the trail is set to, it still respects the fee-adjusted
minimum lock-in — the trail may not fire for a gain smaller than (net target + fees). Width and
lock-in are separate constraints; both apply.

**Honest caveat (UI copy):** none of these predict. ATR is how much the instrument *has been*
moving, not how much it *will*. A recommended trail is a disciplined starting point from the books'
logic, not a guarantee — paper-test how often it stops you out early vs. rides the move before live.

---

## 6. Regime switching / re-anchor

A range scalper's one way to fail is a trend. When the trailing exit fires, the bot decides what
the rebuy should be, based on **where it actually filled**:

- Filled near the old level (≈65) → range intact → rebuy at old `buy_px` (63).
- Filled far above (≈79) → the range is dead → **re-anchor**: recompute new buy/sell levels
  around the new whole number price is now orbiting. The old 63 is stale and must not be used.

Ensemble roles in the re-anchor decision:
- **Williams** — volatility-breakout trigger: "the 63–65 range broke, act."
- **Kleinman** — structure/seasonality gate: don't chase the first tick over; confirm a new level
  is actually establishing before re-anchoring (avoids fakeouts). **[OPEN: confirm author/book name — possibly Bernstein (seasonality) or Ross (price patterns)]**
- **Carter** — psychological/round-number levels and pivots as the anchor references; scale out
  of exits rather than all-or-nothing.
- **Jim Paul** — risk governor: re-anchoring may only ever chase a *confirmed* level; it must
  never turn "I sold too early" into "I'll buy it all back higher and hope." `abort_below` /
  `abort_above` cap the downside if the new level read is wrong.

**Whole-number clustering** is the anchoring reference (real microstructure effect — liquidity
piles at round numbers). Treat as a strong tendency, not a law: silver can knife straight through
a level with no pause.

---

## 7. Strategy selector [NEW]

A layer on top that chooses which strategy/parameters are in force, per instrument.

- **Recommended mode** — the ensemble computes *candidate* parameters from its rules and surfaces
  them as suggestions for the user to approve:
  - breakout level (Williams volatility-breakout formula),
  - trail width (ATR),
  - re-anchor target (recent whole-number cluster).
  - **These are deterministic rule outputs, NOT predictions.** The dashboard presents them; the
    human approves. No auto-apply of a re-anchor without confirmation.
- **Manual mode** — user overrides any/all levels directly.
- **Risk dial (presets)** — conservative / moderate / aggressive, each bundling the risk-facing
  knobs together: trail width, `scale_up_buffer_mult`, `max_swing_qty`, `abort_below` / `abort_above`.
  Pick a preset as a starting point, then hand-tune individual values. **[OPEN: define what each
  preset actually sets]**

---

## 7A. Strategy explainer panel + annotated chart [NEW]

The selector (§7) must not be a bare list of names. Before committing to any strategy, the user
opens an **explainer panel** — a review step that sits *between* browsing and committing. Purpose:
let the user make an informed pick without reading the source books, while never committing capital
to a strategy they can't explain to themselves.

**Flow (deliberate, never one-tap):**
`Browse strategies → "Explain this one" (opens panel) → Back or Select → separate Confirm step to arm.`
No strategy ever goes live from a single tap — same discipline as the HALT-clear confirm.

### Every explainer uses the SAME six-part shape (compare like-for-like)

1. **One-line summary** — what it does in plain language.
2. **The expert & the idea** — who it's from and what they're known for (the credibility to weigh).
   This part is factual and transfers. Present it plainly.
3. **Best in / worst in** — the regime where it prints vs. where it bleeds. The single most useful
   line for a fast decision.
4. **What it does to your money** — mechanically: where it enters, exits, scales, what it gives back.
   Tied to *this* instrument, the core floor, and the account governor — not abstract.
5. **The tradeoff / what to watch** — the honest catch.
6. **Current recommended parameters (if any)** — the rule-computed candidate levels for this
   instrument right now, **clearly flagged as computed suggestions, not predictions.**

> Forcing function: if a strategy can't be written in this six-part shape, it isn't well-defined
> enough to trade. The panel doubles as a definition gate.

### Annotated chart inside each panel [core requirement]

Each explainer includes a **chart that draws the strategy's own logic on top of price**, so the
user can *see* the behavior, not just read it. Two precise meanings:

- **Annotated** = the chart overlays the strategy's decisions on the price line — trigger level,
  rebuy level, swing band, trailing stop stepping up, the entry/exit markers, the core-floor lane —
  each with a short labeled callout. NOT a generic price chart; the decisions made visible.
- **Real-time** =
  - In the **live** view (a strategy already armed): stream the same price feed the bot uses, and
    have the annotations track the bot's **real** state — actual armed orders, actual fills, actual
    position — updating on each tick.
  - In the **preview** view (before commit): run the selected strategy against live/recent data in a
    **sandbox** so the user sees how it *would* behave. Must be clearly labeled "preview — no live
    orders." A preview must never place an order.

**What the chart must render per strategy:**
- price line + whole-number gridlines (the anchoring reference);
- entry/exit markers with plain labels ("sell @65", "rebuy @63", "trailing exit ≈79");
- the active exit mechanism drawn explicitly — a fixed sell line, OR the trailing stop as a stepped
  line ratcheting under price;
- a **position lane** beneath the price panel showing the core floor as an untouchable shaded band
  and the swing sleeve oscillating above it — so "never below core" is visible, not just asserted;
- for the recommended mode, the rule-computed candidate levels drawn as dashed guides, labeled as
  suggestions.

**Implementation notes:** price feed comes from the exchange/FCM WebSocket (same source the bot
uses); the sandbox/preview replays recent candles through the same strategy code the live bot runs
(don't fork the logic — the preview must be the real strategy or it's lying). Charting lib is the
front-end's choice (e.g. lightweight-charts / Chart.js) — keep annotations as a data layer on top so
they can be driven by either live bot state or sandbox output through one interface.

### Written explainer copy (drop-in starting text for Code)

**Fixed-limit swing**
1. Sells a fixed slice at a set high, rebuys the same slice at a set low, repeat.
2. Range-scalping in the spirit of Carter's use of round-number levels as reference points.
3. Best in a sideways, range-bound market that keeps bouncing between two levels. Worst in a trend —
   it sells at the top of its range and gets left behind if price keeps running.
4. Sells `swing_qty` at `sell_px`, rebuys `swing_qty` at `buy_px`; core never sold; profit funds
   growth per §4. Oscillates position between floor and floor+swing.
5. Tradeoff: it will always sell too early into a real breakout. Tighter ranges also let fees eat a
   bigger share of each cycle.
6. Recommended `sell_px`/`buy_px` = nearest whole-number range price is currently orbiting.

**Trailing / ratchet swing**
1. Arms at the high trigger but doesn't sell — trails a stop under price that ratchets up, and only
   sells when price falls back through it.
2. Volatility-breakout thinking (Williams) for detecting the run, plus a trailing exit to ride it.
3. Best in a market that can break out and trend (won't cap you at 65 while silver goes to 80).
   Worst in a choppy range — normal wiggle trips the trail and stops you out early.
4. On trigger, arms a trailing stop at distance `trail` (fixed or ATR); rides the move; on the
   pullback fill it re-anchors the rebuy to the new level (§6). Core untouched throughout.
5. Tradeoff: gives back the trail distance off every top by design. Too tight → stopped by noise;
   too wide → give back a lot. No perfect number; regime-dependent.
6. Recommended `trail` = an ATR multiple of recent range; re-anchor target = new whole-number cluster.

**Recommended / ensemble mode**
1. The system watches the regime and proposes the fitting mode + levels for you to approve.
2. Williams (breakout trigger) + Kleinman-slot structure gate + Carter levels + Paul risk bounds.
3. Best when you don't want to babysit the regime call. Worst when the market is genuinely
   undecided — the gate may sit on its hands, which is correct but can feel like inaction.
4. Runs fixed-swing while ranging; on a confirmed breakout, switches to trailing and re-anchors.
   Every switch is surfaced for approval; nothing auto-arms.
5. Tradeoff: a recommendation is computed from rules, not a forecast. It can be confidently wrong
   about whether a breakout is real. The approval step is where your read lives.
6. Recommended params = the live computed candidates, always shown before anything arms.

---

## 8. Multi-instrument [ARCHITECT NOW, even if only silver runs first]

Retrofitting this later is a rewrite; building it in now is mostly a mental shift.

- The bot becomes a **manager running N independent `SwingTrader` instances**, one per instrument,
  each with its own config and state, **namespaced by symbol** (`SLVR`, `GOLD`, `CL`, …). No more
  single `swing_state.json`; state and config are keyed per symbol.
- Everything in §§2–7 is already per-instance and travels cleanly.
- **The strategy is a template, not a universal law.** The whole-number tight-range scalp works
  because *silver* has been ranging. Another instrument may trend or whipsaw. Each instrument needs
  its own levels and its own human read. The machine will happily run a bad range-scalp on the
  wrong instrument — it can't tell you the instrument's a poor fit.

### 8A. Asset-class grouping + per-class defaults

Coinbase's derivatives cover crypto as well as metals/commodities, so instruments are grouped by
**asset class** (metals, crypto, energy, equity perps) into dashboard tabs/sections. This is mostly
a UX grouping — the swing logic, core floor, trailing exit, and explainer chart all travel
unchanged — but each class carries **default config** because the instruments genuinely behave
differently:

- **Crypto perps (BTC, ETH, …):**
  - Trade **24/7** — no market close. The time-of-day/liquidity guards still apply (thin windows
    exist) but there's no session boundary to key off.
  - **Much higher volatility** than silver — a fixed trail sane for metals is far too tight for BTC.
    This is why `trail` must be **ATR-based per instrument**, not one global number.
  - **Funding rates** hit P&L more often — the funding-aware P&L accounting (see safety layer) is
    load-bearing here, not optional.
  - **Weaker whole-number clustering** — BTC doesn't respect round numbers the way silver has;
    it clusters at different psychological levels (large round increments, prior highs). The
    anchor-detection must be **per-instrument**, not "assume the nearest whole number."
- **Metals / commodities (SLVR, GOLD, CL):** session hours, dated-contract roll handling, tighter
  ranges, stronger whole-number behavior (current default assumptions).

Per-class defaults are a starting template the user can override per instrument. Adding a class is
config, not code.

---

## 9. Account-level margin governor [REQUIRED once >1 instrument can trade]

The one thing multi-instrument genuinely adds beyond namespacing.

- Each instance reasons about its own floor/margin in isolation, but all instances draw on **one
  account's margin pool.** Three bots each independently scaling up can collectively march you
  toward a margin call no single instance can see.
- Build an **account-level governor** above all instances that tracks total margin committed across
  every symbol and can **veto any scale-up** even when the individual instance says it's funded.
  This is the portfolio-level Jim Paul. Not optional.

---

## 9A. Multi-tenant-ready architecture [ARCHITECT NOW, RUN SINGLE-TENANT FIRST]

Adam may sell this as software (like Pourly). Selling it reshapes the architecture, so build it
**tenant-ready** now — but **deploy single-tenant (just Adam) first**, prove the strategy makes
money over real time, and get legal review before onboarding anyone else.

**What "tenant-ready" means concretely (cheap now, a rewrite later):**
- **Namespace everything by `tenant_id`** from day one — config, state, trade logs, keys. Every
  store key and every query is scoped by tenant. This is the Pourly pattern (one codebase, many
  isolated customers). Retrofitting isolation into a single-user tool is a full rewrite; adding a
  `tenant_id` column/prefix now is nearly free.
- **Hard isolation:** no tenant can ever read, see, or affect another tenant's config, state,
  position, keys, or history. This is a security invariant, not a feature — enforce it at the data
  layer, not just the UI.
- **The account-level margin governor (§9) is per-tenant** — each tenant has their own account and
  their own margin pool; the governor must never aggregate across tenants.

**Key custody is a different risk universe once others' keys are involved:**
- Holding *your own* API key: if it leaks, it's your problem, and it's scoped trade-only anyway.
- Holding *many users'* keys: a leak is a company-ending, likely-regulated event. If this goes
  multi-tenant, key custody becomes a central design concern — encryption at rest, strict access
  control, per-tenant scoping, and ideally a design where the platform holds as little key material
  as possible. Do not hand-roll this casually.

**Regulatory checkpoint [NOT LEGAL ADVICE — get a lawyer]:**
- Selling automated-trading software to the public carries real regulatory weight. Depending on
  structure (giving trading advice? managing accounts? just providing a tool?), it can touch
  CFTC/NFA territory around commodity trading advisors and trade-decision software for others.
- The single-user version for Adam has **none** of this weight. The sellable version has a lot.
- **Action:** before selling to anyone, consult a securities/commodities attorney about how to
  structure it. This is a "before you sell" gate, not a "figure it out later" item.

**Bottom line:** tenant-namespace the data now so the option stays open; ship just-Adam first;
don't sell until a lawyer has reviewed the structure and the key-custody design is real.

---

## 10. Dashboard

- **Host:** Express app (same stack as Off Premise) on a **subdomain** of smearthequeer.com
  (e.g. `dash.smearthequeer.com`). Leave the apparel storefront on the apex — preserves the
  brand's SEO/history and keeps a private financial tool off the public-facing URL.
- **Auth on the API, not just the page.** Password-gating the page is theater if the `/api/*`
  routes that read position and write config are reachable without auth. Server-side sessions;
  every trading route rejects any request without a valid session. Single strong login (one user).
- **Server-side sanity bounds on every config write:** reject structurally insane values before
  they reach the store — `core_qty` can't go to 0, `swing_qty ≤ max_swing_qty`, `buy_px < sell_px`,
  trail width within range, etc. The dashboard is convenience; it must not be able to instruct the
  bot to do something reckless.
- **HALT-clear is deliberate:** re-arming after a governor halt sits behind a confirm step, so a
  stray tap can't restart the bot into the exact condition that just tripped it.
- **Per-instrument cards:** each shows read-only status (position, current leg, `swing_qty`,
  realized P&L, cycles, big red HALTED banner if tripped) and editable config (floor, swing size,
  levels, margin, fees, exit mode toggle, strategy/preset, abort bounds). Plus a control to add a
  new instrument.

---

## 9B. Safety & observability layer [BUILD BEFORE STRATEGY CLEVERNESS]

Most of these are invisible until the exact moment they cost money. Build the rails and the
visibility first; the strategy is the fun part, but this is what decides whether the thing makes
money or quietly loses it while you sleep.

- **Kill switch** — one dashboard control (and phone-reachable) that immediately **halts all arming
  across every instrument and tenant-scope**. Distinct from a per-instrument HALT; this is the
  "freeze everything now" button. Build it first.
- **Alerting / notifications** — push to SMS or a Telegram/Discord bot on: any HALT, any fill, a
  heartbeat miss, a margin-call warning, and the daily-loss breaker tripping. This is what lets you
  not babysit the dashboard — the whole point.
- **Heartbeat / dead-man's switch** — the bot writes a timestamp every loop; a separate watcher (or
  uptime service) alerts if it goes stale. A silent death mid-swing is the orphaned-position
  nightmare; without a heartbeat you find out hours late. Single most-forgotten safeguard.
- **Reconciliation on startup AND on a timer** — actively compare bot-believed position/orders
  against exchange truth every N minutes; **HALT on any mismatch.** Drift between state and truth is
  how quiet disasters happen.
- **Idempotency keys on every order** — a unique client-order-ID per placement so a network retry
  can't double-submit.
- **Structured trade log / journal** — every order, fill, halt, re-anchor, scale-up written to a
  table with timestamps. For taxes (futures reporting), for debugging "why did it do that at 2am,"
  and for measuring whether the strategy actually makes money. Automated Stack Tracker.
- **Exchange-outage / partial-connectivity behavior** — define explicitly what happens when the feed
  drops mid-trail or an order times out. Default: **HALT and alert.** Undefined behavior here is
  where real losses live.
- **Time-of-day / liquidity guards** — don't arm new legs when the spread is wider than X (thin
  overnight hours give garbage fills). Behaves differently per asset class (crypto is 24/7).
- **Max-daily-loss circuit breaker** — account-level (and per-tenant) rule that halts everything if
  realized loss crosses a daily threshold. Pure Jim Paul; the backstop for a wrong-read day.
- **Roll handling** — dated contracts expire; near expiry the position must roll to the next month
  (Adam's history shows orphaned brackets after rolls). Explicit roll logic required, or the bot
  does something dumb near expiry. Less relevant on perpetual-style contracts.
- **Funding-rate accounting** — on perpetual-style/crypto perps, funding accrues while holding and
  must be reflected in P&L or realized numbers drift from reality.

---

## 9C. Backtest engine [ONE ENGINE, VIEWABLE FROM ANYWHERE]

There is **one** backtest engine. It runs the **same strategy code through the same `PaperBroker`
and fee model** (§10A) over historical data — the only thing that changes is where you view it from.
This reuse is what makes it trustworthy: the backtest IS the real strategy, not a lookalike.

**Two doorways into the same engine:**
- **From the paper section** — validate and tune a strategy against history at high speed.
- **From the real-money section** — a **"preview before you arm"** button on each instrument: run
  this exact strategy over the chosen window of real data and show the equity curve, win rate, and
  fees *before* committing live. Confidence from data, not from a guess.

**User-chosen time window:**
- The user picks the backtest length: preset windows (last 30 / 90 / 180 days) or a custom
  start/end range. Available from **both** doorways — including the real-money "preview before you
  arm" view.
- Data availability bounds the window (can't test further back than history exists for the
  instrument); surface that limit rather than silently truncating.

**Single-strategy mode:** run one strategy over the chosen window (validate / tune / preview).

**Compare-all mode [ranked leaderboard]:**
- Run **every** strategy over the chosen window on the chosen instrument and **rank them side by
  side**, so the user can see which approach fit that instrument and window best.
- **Fair-comparison rule (enforce):** every strategy in a ranking runs over the *identical* window,
  *identical* data, and *identical* fee model. Otherwise you're comparing an easy stretch against a
  hard one and the ranking is meaningless.
- **Rank on risk-adjusted terms, not raw profit.** The highest-return strategy is often the one
  taking the most risk. The leaderboard shows return **next to** max drawdown, win rate, and fees —
  "best" must mean "best for how much pain," not just the biggest number. Provide a sort control
  (by return, by drawdown, by win rate, by return/drawdown ratio).
- Available from both the paper section and the real-money "preview before you arm" view.

**What it must show (never a single number):**
- equity curve, realized/unrealized P&L, **fees broken out**, win rate, max drawdown, cycle count;
- results **across multiple market regimes** — a ranging stretch, a trending stretch, a choppy
  stretch — shown separately, not blended into one figure.

**The overfitting + window trap [write this into the UI, prominently]:**
A backtest shows how a strategy *would have* done in the **past**, not how it will do next. A
strategy tuned until it looks flawless on one historical window is often just memorizing that
window — brilliant in backtest, falls apart live. And in compare-all mode, **whichever strategy
wins won that specific slice of history** — pick a flattering window and you'll fool yourself. The
defense: don't trust a single-window winner. A strategy earns trust when it ranks well across a
ranging month, a trending month, AND a choppy month — offer that multi-regime comparison, not just
one range. Rank #1 on all three = real; wins only on last month = probably just fit to last month.
Trust an **ugly-but-positive result that holds across regimes** over a perfect result on one
cherry-picked stretch. The backtest is a filter for "obviously broken," not a promise of "will work."

---

## 10A. Paper trading mode [BUILD EARLY — validate before risking a dollar]

A full paper-trading sandbox: "deposit" a virtual balance, run the real strategy, and see whether
it makes money *after costs* — with zero exchange exposure. This is the gate before any live capital.

### The paper broker

- A `PaperBroker` implementing the **same `Broker` interface** as the live adapter. The bot runs the
  **exact same strategy code** — only fills are simulated, nothing reaches the exchange. This
  identity is the whole point: a forked paper path would validate a different bot than you'd deploy.
- Selected by a config flag (`mode: paper | live`), fully isolated from live config and state.
- **Paper config is independent of live config.** The user can set any `core_qty` / `swing_qty` /
  levels in paper (e.g. stress-test 5 contracts or an 8 core) without touching the live 12/10 setup.
  Nothing done in the sandbox can reach or affect the live bot.

### Virtual account with a real starting balance

- User **deposits** a set virtual amount. The paper account tracks it as a real constraint:
  - starting balance, current balance, **margin in use**, **free margin**,
  - realized P&L, unrealized P&L,
  - **total fees paid, broken out as its own line** (the single most useful number here),
  - win rate, cycles completed, max drawdown, equity curve over time.
- **Margin-call simulation:** the paper account can hit a margin call exactly as the live one would.
  If a run would have blown up, paper shows that instead of pretending balance was infinite. The
  deposited amount is a real limit, not decoration.

### Fee & cost modeling [DO NOT SKIP ANY — omission makes paper P&L lie upward]

Every fill runs through the same cost model the live account faces:
- **Commission** — per-contract fee on every fill.
- **Spread** — fills are NOT at mid: a buy pays the ask, a sell hits the bid. That gap is a real
  per-cycle cost and on a 2-point swing it's a meaningful slice.
- **Funding** — for perpetual-style / crypto perps, funding accrues while a position is held; a
  paper run holding through funding windows must debit it.
- **Slippage estimate** — in fast markets fills land worse than the requested price; model it as an
  estimate so paper isn't unrealistically clean.
- **[OPEN: real commission, typical spread, and funding values per instrument from the venue/FCM.]**

### Two run speeds (same `PaperBroker` underneath)

- **Live paper** — runs forward on the real streaming feed. Honest but slow (days to accumulate
  cycles). Use to confirm behavior in current conditions.
- **Backtest replay** — runs the shared backtest engine (§9C) over historical candles at high speed,
  so a full season of cycles takes seconds. Use to tune parameters (e.g. is `scale_up_buffer_mult` =
  1.0 vs 1.5 better) against data instead of a guess. Same engine the real-money "preview before you
  arm" button uses.

### Honest limits (write into the UI)

Paper trading proves the **logic**; it does not prove the **fills**. Real exchanges deliver slippage
and occasional non-fills a simulator can only estimate. Sequence: **paper until the equity curve
convinces you → a small live run at minimal real size → then scale.** Never treat a good paper
result as proof the live fills will match.

---

## 11. Deployment

- Bot runs as a **paid Render background worker** (NOT free tier — free spins down after 15 min
  and has a 750-hr cap; either silently kills a trading bot). ~$7/mo Starter compute.
- **State lives in Render Key Value or Postgres, not the local filesystem** (Render's default FS is
  ephemeral and wiped on every redeploy — it would erase `realized_pnl`, `swing_qty`, high-water
  marks). This is the load-bearing `StateStore` swap; build it first.
- Bot process and dashboard web process are separate services sharing only the store.

---

## 12. Build order (suggested)

1. **`StateStore` abstraction** backed by Render Key Value/Postgres (config + state, namespaced by
   symbol). Everything else hangs off this — do it first.
2. **`Broker` adapter** for the resolved access path (§1).
3. **`PaperBroker`** (§10A) — same interface, simulated fills, full fee model. Build alongside the
   real adapter so you can validate everything in paper before any live capital.
4. **Safety & observability layer** (§9B) — kill switch, heartbeat, alerting, reconciliation,
   trade log, daily-loss breaker. Build the rails before the strategy cleverness.
5. **Backtest engine** (§9C) — one engine, viewable from both paper and real-money sections.
6. **Express dashboard**: subdomain + server-side session auth on all `/api/*` routes.
7. **Read-only status view** — watch the bot before it can be changed from the UI.
8. **Editable config** with server-side sanity-bound validation.
9. **Bot feature modules:** exit-mode toggle (trailing/ratchet + high-water persistence) →
   re-anchor/breakout detection → strategy selector + presets.
10. **Strategy explainer panel + annotated chart** (§7A): six-part copy, annotated price chart with
   position lane, live vs. sandbox-preview modes.
11. **Multi-instrument manager** + **account-level margin governor**.

---

## Open decisions checklist (fill before going live)

- [ ] Access path A vs B; exact contract ticker + FCM (§1)
- [ ] `margin_per_contract` — real number from FCM (§4)
- [ ] `fee_per_contract_roundtrip` — real number (§4)
- [ ] `scale_up_buffer_mult` — confirm ~1.0 (§4)
- [ ] Default trail distance / ATR multiple (§5)
- [ ] FCM native trailing-stop support? (§5)
- [ ] Risk preset definitions — conservative/moderate/aggressive values (§7)
- [ ] "Kleinman" author/book name confirmation (§6)
- [ ] Paper-mode cost inputs — real commission, typical spread, funding per instrument (§10A)
- [ ] Contract specs per instrument — contract_size, tick_size, tick_value, margin (§3A)
- [ ] Default ATR multiplier per asset class for the trail (§5B)
- [ ] Pre-trade fee visibility on a queued order — confirm the API exposes it (§1, §2A)
- [ ] Abnormal-widen threshold for the fee-gate sanity ceiling (§2A)

---

## Standing principle

The core position is protected by design; the swing sleeve is not, and it grows as it profits —
so its risk grows too. The trailing exit, the re-anchor, and the presets are all tools to execute
*your* read of the market. Whether a given level is a new range or a fakeout is the one thing the
system can't decide for you. It stays a human call — the machine's job is to execute it without
ever breaching the floor or the account margin governor.
