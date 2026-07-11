"""Dump sleeve_state_reset_intent for every live sleeve. Used to verify
whether the migration wrote the intent AND whether the bot consumed it.

Usage:
    python3 scripts/dump_intent.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from state_store import make_store  # noqa: E402


def main() -> int:
    store = make_store("./data")
    tenant = "adam-live"
    symbols = [s for s in store.list_symbols(tenant) if not s.startswith("__")]
    print(f"tenant: {tenant}")
    print(f"backend: {'Redis' if os.getenv('REDIS_URL') else 'local JSON'}")
    print("-" * 60)
    any_intent = False
    for sym in symbols:
        intent = store._get_scope(tenant, sym, "sleeve_state_reset_intent")
        if intent:
            print(f"{sym}: intent = {intent!r}")
            any_intent = True
    if not any_intent:
        print("No sleeve_state_reset_intent found on any live sleeve.")
        print("Meaning: migration either didn't write one, OR bot already consumed it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
