"""Read Coinbase's real-time rate-limit budget headers.

Coinbase's REST responses include headers that tell you your actual
remaining budget (not the documented ceiling — the real number their
throttle is enforcing right now):

    X-RateLimit-Limit       — max requests per window
    X-RateLimit-Remaining   — how many you have left in current window
    X-RateLimit-Reset       — when the window resets (unix ts or seconds)
    Retry-After             — seconds to wait if you're throttled (429s)

This diag hits each endpoint type ONCE, prints the raw headers, and
tells you your real rate budget for THIS account (not the docs).

Usage (Render silver-swing-bot-live shell):
    python3 diag_coinbase_ratelimit_headers.py

Read-only. Single GET per endpoint. Zero write ops.
"""
from __future__ import annotations
import os
import sys
import time


PROBE_PRODUCT = "SLR-27AUG26-CDE"


def _extract_headers(resp) -> dict:
    """Coinbase SDK wraps responses in different shapes depending on version.
    Try multiple attribute paths to find the underlying HTTP headers.
    Returns a dict of relevant rate-limit headers, or an empty dict if
    we can't find them."""
    candidates = [
        # Path 1: response has .headers directly
        lambda r: getattr(r, "headers", None),
        # Path 2: response has ._response with .headers
        lambda r: getattr(getattr(r, "_response", None), "headers", None),
        # Path 3: response has ._raw or .raw with .headers
        lambda r: getattr(getattr(r, "_raw", None), "headers", None),
        lambda r: getattr(getattr(r, "raw", None), "headers", None),
        # Path 4: response is a dict, headers key
        lambda r: r.get("headers") if isinstance(r, dict) else None,
    ]
    for c in candidates:
        try:
            h = c(resp)
            if h:
                if hasattr(h, "items"):
                    return dict(h.items())
                if isinstance(h, dict):
                    return dict(h)
        except Exception:
            continue
    return {}


def _monkey_patch_client_to_capture_headers(client) -> list:
    """The Coinbase SDK might not expose raw HTTP headers on its response
    objects. Monkey-patch the underlying HTTP session (if urllib3 or
    requests-based) to capture headers of the last N responses.

    Returns a shared list that will be populated with dicts of headers
    from each subsequent response."""
    captured = []
    # Try common SDK internals — the SDK wraps requests.Session
    session = None
    for attr in ("_session", "session", "_http", "http"):
        if hasattr(client, attr):
            session = getattr(client, attr)
            break
    if session is None:
        # Try one level deeper
        for attr in ("api", "_api", "transport"):
            inner = getattr(client, attr, None)
            if inner is not None:
                for sub in ("_session", "session"):
                    if hasattr(inner, sub):
                        session = getattr(inner, sub)
                        break
    if session is None or not hasattr(session, "request"):
        return captured
    original_request = session.request

    def _wrapped(*args, **kwargs):
        resp = original_request(*args, **kwargs)
        try:
            captured.append({
                "url": getattr(resp, "url", ""),
                "status_code": getattr(resp, "status_code", None),
                "headers": dict(getattr(resp, "headers", {}) or {}),
            })
        except Exception:
            pass
        return resp

    session.request = _wrapped
    return captured


def main() -> None:
    print("=" * 70)
    print("COINBASE ADVANCED TRADE — REAL-TIME RATE LIMIT HEADERS")
    print("=" * 70)
    print()

    try:
        from broker import CoinbaseBroker, BrokerConfig
        broker = CoinbaseBroker(BrokerConfig(product_id=PROBE_PRODUCT))
    except Exception as e:
        print(f"BROKER INIT FAILED: {type(e).__name__}: {e}")
        sys.exit(1)

    # Monkey-patch to capture headers from the underlying HTTP session
    captured = _monkey_patch_client_to_capture_headers(broker.client)
    if not captured:
        print("WARN: could not attach header capture to Coinbase SDK's HTTP session.")
        print("      Headers won't be visible below — will still make the calls.")
        print()

    # Fire one request per endpoint type
    endpoints = [
        ("get_product (public)",
         lambda: broker.client.get_product(PROBE_PRODUCT)),
        ("get_candles (public)",
         lambda: broker.client.get_candles(
             product_id=PROBE_PRODUCT,
             start=str(int(time.time()) - 300),
             end=str(int(time.time())),
             granularity="ONE_MINUTE",
         )),
        ("get_accounts (private)",
         lambda: broker.client.get_accounts(limit=1)),
    ]

    for label, fn in endpoints:
        print(f"--- {label} ---")
        t0 = time.perf_counter()
        try:
            fn()
            elapsed = (time.perf_counter() - t0) * 1000
            print(f"  status:  OK ({elapsed:.0f}ms)")
        except Exception as e:
            elapsed = (time.perf_counter() - t0) * 1000
            print(f"  status:  FAILED ({elapsed:.0f}ms): {type(e).__name__}: {e}")
        # Show the LATEST captured response's headers (should be the one from above)
        if captured:
            last = captured[-1]
            print(f"  url:     {last.get('url', '')}")
            print(f"  http:    {last.get('status_code')}")
            headers = last.get("headers", {})
            # Filter to only rate-limit-relevant headers (case-insensitive)
            relevant = {}
            for k, v in headers.items():
                kl = k.lower()
                if any(needle in kl for needle in
                       ("ratelimit", "rate-limit", "retry-after",
                        "x-cb-", "x-cursor", "x-request-id")):
                    relevant[k] = v
            if relevant:
                for k, v in sorted(relevant.items()):
                    print(f"  {k}: {v}")
            else:
                print("  (no rate-limit headers in response — Coinbase may not")
                print("   expose them for this endpoint on your tier)")
        print()

    print("=" * 70)
    print("INTERPRETATION")
    print("=" * 70)
    print("If you see X-RateLimit-Limit and X-RateLimit-Remaining above,")
    print("those tell you your ACTUAL rate budget for that endpoint. Compare")
    print("Limit to Remaining to see how close you are to throttling.")
    print()
    print("If NO rate-limit headers are visible, Coinbase either:")
    print("  a) Doesn't return them for retail accounts (only for institutional)")
    print("  b) Returns them under a non-standard header name (check output above)")
    print("  c) The SDK is stripping them before we can see them")
    print()
    print("In cases (a) and (c), we know the LIMITS from the docs but not the")
    print("real-time budget. The RateLimitController (Commit B) will still work")
    print("— it just tracks OUR outgoing rate rather than reading Coinbase's")
    print("view of it.")


if __name__ == "__main__":
    main()
