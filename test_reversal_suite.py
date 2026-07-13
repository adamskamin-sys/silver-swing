import random
random.seed(3)
import reversal as rv

def candles(closes, half=0.2):
    return [{"ts":i*300,"open":c,"high":c+half,"low":c-half,"close":c} for i,c in enumerate(closes)]

# strong DOWN-trend (drift dominates noise) -> regime=trend, breaks lows, TSM<0
down=[300.0]
for _ in range(320): down.append(down[-1]-1.5+random.gauss(0,0.1))
cfg={"reversal_enabled":True}

# 1) LONG into a confirmed downtrend, regime gate ON -> reverse to SHORT
r = rv.should_reverse(candles(down), "LONG", cfg)
print("1) long+downtrend ->", r["reverse"], r["to_side"], "|", r["reason"][:55], "| regime", r["signals"]["regime"])
assert r["reverse"] and r["to_side"]=="SHORT", r

# 2) disabled toggle -> never reverse
assert rv.should_reverse(candles(down), "LONG", {"reversal_enabled":False})["reverse"] is False
print("2) toggle off -> no reversal (opt-in respected)")

# 3) chop / mean-revert regime -> gate BLOCKS even with a break
mr=[100.0]; x=0.0
for _ in range(320):
    x=-0.6*x+random.gauss(0,1.0); mr.append(100+x)
r3 = rv.should_reverse(candles(mr,half=0.5), "LONG", cfg)
print("3) long+chop      ->", r3["reverse"], "|", r3["reason"][:50])
assert r3["reverse"] is False and "regime" in r3["reason"], r3

# 4) FLAT -> nothing to reverse
assert rv.should_reverse(candles(down), "FLAT", cfg)["reverse"] is False
print("4) flat -> nothing to reverse")

# 5) SHORT into a confirmed UPtrend -> reverse to LONG
up=[100.0]
for _ in range(320): up.append(up[-1]+1.5+random.gauss(0,0.1))
r5 = rv.should_reverse(candles(up), "SHORT", cfg)
print("5) short+uptrend  ->", r5["reverse"], r5["to_side"])
assert r5["reverse"] and r5["to_side"]=="LONG", r5

# 6) telemetry: reversals count + P&L attributed to reversal legs
ev=[
 {"event_type":"position_reversed","sleeve_name":"ModelB"},
 {"event_type":"position_reversed","sleeve_name":"ModelB"},
 {"event_type":"cycle_completed","via_reversal":True,"gross":12.0,"sleeve_name":"ModelB"},
 {"event_type":"cycle_completed","via_reversal":True,"gross":-4.0,"sleeve_name":"ModelB"},
 {"event_type":"cycle_completed","gross":50.0},
]
st=rv.reversal_stats(ev)
print("6) telemetry -> reversals", st["reversals"], "| reversal P&L $%.0f"%st["reversal_pnl"], "|", st["by_source"]["ModelB"])
assert st["reversals"]==2 and st["reversal_pnl"]==8.0, st
print("\nREVERSAL ENGINE PASSED")

# 40 calm bars, then a climax DOWN bar (range + volume spike)
cs=[]
for i in range(40):
    c=100.0
    cs.append({"ts":i,"open":c,"high":c+0.5,"low":c-0.5,"close":c,"volume":100})
# climax liquidation bar: -10%, huge range, 5x volume
cs.append({"ts":40,"open":100,"high":100.5,"low":89,"close":90,"volume":500})
cfg={"cascade_enabled":True}
sig=rv.cascade_signal(cs, ofi=-0.8, cfg=cfg)
print("cascade ->", sig["cascade"], sig["direction"], "|", sig["reason"][:60])
assert sig["cascade"] and sig["direction"]=="DOWN", sig
# LONG caught in the down cascade -> join it, flip SHORT, trigger=cascade
d=rv.decide(cs, "LONG", cfg=cfg, ofi=-0.8)
print("decide  ->", d["reverse"], d["to_side"], "trigger:", d["trigger"])
assert d["reverse"] and d["to_side"]=="SHORT" and d["trigger"]=="cascade", d
# calm market -> no cascade
calm=[{"ts":i,"open":100,"high":100.5,"low":99.5,"close":100,"volume":100} for i in range(45)]
assert rv.cascade_signal(calm, cfg=cfg)["cascade"] is False
print("calm    -> no cascade (no false trigger)")
print("\nCASCADE ENGINE PASSED")
