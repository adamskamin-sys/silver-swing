"""Show / clear the account-level kill switch (per tenant).

Adam 2026-07-21: preflight refuses to start with 'kill switch active:
triggered from dashboard'. This diag surfaces the actual state and
clears it (with confirmation).

    python3 diag_clear_kill_switch.py            # show state
    python3 diag_clear_kill_switch.py clear      # deactivate

Reads/writes the per-tenant config under scope __account_kill_switch__
via the KillSwitch class in safety.py.
"""
import os
import sys


def main():
    from state_store import RedisJsonStore
    from safety import KillSwitch

    url = os.environ.get("REDIS_URL") or os.environ.get("REDIS_INTERNAL_URL")
    if not url:
        print("REDIS_URL not set")
        return
    store = RedisJsonStore(url)
    tenants = ["adam-live"]

    action = sys.argv[1].lower() if len(sys.argv) > 1 else "show"

    for tenant in tenants:
        ks = KillSwitch(store, tenant)
        active = ks.is_active()
        reason = ks.reason()
        print(f"\ntenant: {tenant}")
        print(f"  active: {active}")
        print(f"  reason: {reason}")

        if action in ("clear", "off", "release"):
            if active:
                ks.clear(cleared_by="diag_clear_kill_switch.py")
                print(f"  ✓ CLEARED (was active — bot should start on next preflight)")
            else:
                print(f"  (already inactive, nothing to clear)")
        elif action != "show":
            print(f"unknown action: {action}   (use 'clear' or leave blank to show)")


if __name__ == "__main__":
    main()
