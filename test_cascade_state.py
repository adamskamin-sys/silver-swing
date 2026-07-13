import cascade_state as cs

# 1) crash -> dead-cat BOUNCE (price up, VPIN still toxic) -> don't re-enter, 2nd leg
crash = [{"price":100-2*i, "vpin":0.4+0.05*i, "vol":2.0, "ofi":-0.7} for i in range(10)]  # -> vpin ~0.85, price 100->82
bounce = [{"price":82+0.4*i, "vpin":0.70, "vol":1.8, "ofi":-0.4} for i in range(8)]        # price up, flow still toxic
a = cs.assess(crash+bounce)
print("bounce seq ->", a["phase"], "| reentry_ok", a["reentry_ok"], "| 2nd-leg", a["second_leg_risk"])
assert a["phase"]=="bounce" and a["reentry_ok"] is False and a["second_leg_risk"] is True, a

# 2) crash -> genuine RECOVERY (vpin subsides, vol contracts) -> all-clear
recover = [{"price":82+0.2*i, "vpin":0.30, "vol":0.5, "ofi":0.0} for i in range(10)]
b = cs.assess(crash+recover)
print("recover seq->", b["phase"], "| reentry_ok", b["reentry_ok"], "| vol_ratio", b["vol_ratio"], "| calm_bars", b["calm_bars"])
assert b["reentry_ok"] is True and b["phase"] in ("exhaustion","calm"), b

# 3) mid-crash -> crashing, never re-enter
mid = [{"price":100-2*i, "vpin":0.4+0.06*i, "vol":2.2, "ofi":-0.8} for i in range(12)]  # ends ~0.7-0.9
mid[-1]["vpin"]=0.9
c = cs.assess(mid)
print("mid-crash  ->", c["phase"], "| reentry_ok", c["reentry_ok"])
assert c["phase"]=="crashing" and c["reentry_ok"] is False, c
print("\nCASCADE-STATE PASSED")
