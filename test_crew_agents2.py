import random
random.seed(1)

# ---- TCA ----
import tca
events = [
  {"event_type":"order_placed","order_id":"1","side":"BUY","price":100.0,"qty":2,"post_only":True},
  {"event_type":"order_filled","order_id":"1","average_filled_price":100.3,"filled_qty":2,"ts":10},  # paid up 0.3
  {"event_type":"order_placed","order_id":"2","side":"SELL","price":105.0,"qty":2,"post_only":False},
  {"event_type":"order_filled","order_id":"2","average_filled_price":104.5,"filled_qty":2,"ts":20},  # sold low 0.5
]
fills = tca.fills_from_events(events)
rep = tca.analyze(fills, mark_lookup=lambda ts: 99.0 if ts==10 else 106.0, contract_size=1)  # both moved against us
assert rep["n_fills"]==2 and rep["slippage_mean"]>0, rep
print("TCA        ->", rep["verdict"], "| adverse rate", rep["adverse_selection_rate"], "| flags", len(rep["flags"]))

# ---- alpha_decay ----
import alpha_decay as ad
dead = ad.edge_health([-1,-2,1,-3,-1,2,-2,-1,-2,-1]*3, backtest_expectancy=5.0, min_samples=20)
healthy = ad.edge_health([6,5,7,6,5,6,7,5,6,6]*3, backtest_expectancy=5.0, backtest_sharpe=0.5, min_samples=20)
assert dead["verdict"]=="DEAD" and healthy["verdict"]=="HEALTHY", (dead["verdict"], healthy["verdict"])
print("alpha_decay-> dead:", dead["verdict"], "| healthy:", healthy["verdict"])

# ---- regime ----
import regime
trend=[100.0]
for _ in range(320): trend.append(trend[-1] + 0.2 + random.gauss(0,0.05))
mr=[100.0]; x=0.0
for _ in range(320):
    x = -0.6*x + random.gauss(0,1.0); mr.append(100+x)
rt = regime.classify_regime([{"close":p} for p in trend])
rm = regime.classify_regime([{"close":p} for p in mr])
print("regime     -> trend series:", rt["regime"], f"(H={rt['hurst']})", "| MR series:", rm["regime"], f"(H={rm['hurst']})")
assert rt["hurst"]>0.5 and rt["regime"]=="trend", rt
assert rm["hurst"]<0.5 and rm["regime"] in ("mean_revert","chop"), rm

# ---- stress_test ----
import stress_test as st
base=[{"ts":i*300,"open":100,"high":101,"low":99,"close":100} for i in range(5)]
class R:
    def __init__(s,ret,halted=False): s.total_return=ret; s.max_drawdown=abs(ret); s.halted=halted; s.halt_reason=None
def run_fn(cfg,candles):
    drop=candles[-1]["close"]-candles[len(base)-1]["close"] if len(candles)>len(base) else 0
    return R(drop*10, halted=False)  # negative return, not halted -> blowup
srep = st.stress_report({}, run_fn, base, drop_pct=0.30)
print("stress     ->", srep["verdict"][:60], "| worst", srep["worst_scenario"])
assert srep["blowups"], srep

# ---- margin_sentinel ----
import margin_sentinel as ms
pos=[{"symbol":"SLR-27AUG26-CDE","side":"BUY","qty":10,"avg_entry":100,"mark":94,"contract_size":1,"margin_per_contract":10}]
mrep=ms.margin_report(pos, balance=200.0, warn_distance_pct=15.0)
print("margin     ->", mrep["verdict"], "| nearest-to-liq", mrep["nearest_distance_to_liq_pct"],"% | alerts", len(mrep["alerts"]))
assert any(a["severity"]=="critical" for a in mrep["alerts"]), mrep

# ---- data_quality ----
import data_quality as dq
good=[{"ts":i*300,"open":100,"high":101,"low":99,"close":100+ (i%3)} for i in range(50)]
bad =[{"ts":0,"open":10,"high":9,"low":11,"close":10}]  # high<low crossed
gq=dq.check_candles(good); bq=dq.check_candles(bad)
print("data_qual  -> good:", gq["verdict"][:20], "| bad:", bq["verdict"][:35])
assert gq["ok"] and not bq["ok"], (gq["ok"], bq["ok"])

print("\nALL SIX AGENT MODULES PASSED")
