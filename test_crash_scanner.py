import crash_guard as cg
cfg = {"guard_enabled": True}
# toxic DOWN cascade while LONG -> FLATTEN (defensive, get out now)
ms_down = {"vpin": 0.85, "trade_ofi_60s": -0.82, "obi": -0.7, "kyle_lambda": 9.0,
           "kyle_lambda_baseline": 2.0, "aggressor_run": 7}
rets = [0.0]*18 + [-0.001, -0.03]   # a jump down on the last bar
a = cg.crash_assessment(ms_down, rets, "LONG", cfg)
print("LONG + down cascade ->", a["severity"], a["direction"], a["action"], "| fired", len(a["fired"]))
assert a["severity"]=="crash" and a["direction"]=="DOWN" and a["action"]=="FLATTEN", a

# same but OFFENSIVE flip enabled -> FLATTEN_AND_FLIP to SHORT
a2 = cg.crash_assessment(ms_down, rets, "LONG", {**cfg, "flip_enabled": True})
print("  + flip enabled     ->", a2["action"], "flip_to", a2["flip_to"])
assert a2["action"]=="FLATTEN_AND_FLIP" and a2["flip_to"]=="SHORT", a2

# calm book -> HOLD, no false crash
ms_calm = {"vpin": 0.3, "trade_ofi_60s": 0.05, "obi": 0.1, "kyle_lambda": 2.1, "kyle_lambda_baseline": 2.0, "aggressor_run": 1}
calm = cg.crash_assessment(ms_calm, [0.0005]*20, "LONG", cfg)
print("calm market         ->", calm["severity"], calm["action"])
assert calm["severity"]=="none" and calm["action"]=="HOLD", calm

# guard disabled -> always HOLD (opt-in respected)
assert cg.crash_assessment(ms_down, rets, "LONG", {"guard_enabled": False})["action"]=="HOLD"
print("guard off           -> HOLD (opt-in respected)")

# down cascade but we're SHORT (with it) -> not against us -> HOLD (don't exit a winning short)
a3 = cg.crash_assessment(ms_down, rets, "SHORT", cfg)
print("SHORT + down cascade -> action", a3["action"], "(riding it, no exit)")
assert a3["action"]=="HOLD", a3
print("\nCRASH GUARD PASSED")

import scanner_signals as ss

def cndl(closes, half=0.2, vol=100):
    return [{"ts":i*300,"open":c,"high":c+half,"low":c-half,"close":c,"volume":vol} for i,c in enumerate(closes)]

# clean uptrend -> TREND-ENTER
up=[100.0]
for _ in range(320): up.append(up[-1]+0.5+random.gauss(0,0.05))
r1=ss.entry_assessment(cndl(up))
print("trend candidate  ->", r1["recommendation"], f"(ER {r1['efficiency_ratio']}, q {r1['entry_quality']})")
assert r1["recommendation"]=="TREND-ENTER", r1

# ranging / mean-revert -> SWING-OK
mr=[100.0]; x=0.0
for _ in range(320):
    x=-0.6*x+random.gauss(0,1.0); mr.append(100+x)
r2=ss.entry_assessment(cndl(mr,half=0.5))
print("ranging candidate->", r2["recommendation"], f"(regime {r2['regime']})")
assert r2["recommendation"]=="SWING-OK", r2

# toxic flow (high VPIN) -> AVOID
r3=ss.entry_assessment(cndl([100.0]*45), ms={"vpin":0.85})
print("toxic candidate  ->", r3["recommendation"], "-", r3["reason"][:40])
assert r3["recommendation"]=="AVOID" and r3["toxic"], r3

# live down-cascade -> CASCADE-SHORT
casc=[{"ts":i,"open":100,"high":100.5,"low":99.5,"close":100,"volume":100} for i in range(40)]
casc.append({"ts":40,"open":100,"high":100.5,"low":89,"close":90,"volume":500})
r4=ss.entry_assessment(casc, ofi=-0.8)
print("cascade candidate->", r4["recommendation"], f"(dir {r4['direction']})")
assert r4["recommendation"]=="CASCADE-SHORT" and r4["direction"]=="DOWN", r4

# ranking: best-to-enter first, AVOID last
ranked=ss.rank_candidates([
  {"symbol":"TREND","candles":cndl(up)},
  {"symbol":"TOXIC","candles":cndl([100.0]*45),"ms":{"vpin":0.9}},
  {"symbol":"CASCADE","candles":casc,"ofi":-0.8},
])
print("ranked:", [(r["symbol"], r["recommendation"], r["entry_quality"]) for r in ranked])
assert ranked[0]["symbol"] in ("TREND","CASCADE") and ranked[-1]["symbol"]=="TOXIC", ranked
print("\nSCANNER SIGNALS PASSED")
