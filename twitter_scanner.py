"""Twitter/X shadow signal scanner.

Adam's ask (2026-07-13): "give it a try but don't execute any trades with it.
I want to see if it works first. Build it but then give me a message anytime
you would have made a decision and then evaluate the outcome for me."

SHADOW MODE — HARD GUARANTEE
============================
This module NEVER calls broker.place_limit() or any order-placing API.
The only thing it does is:
  1. Poll a set of Twitter accounts (via Nitter RSS — free, no key)
  2. Score matched tweets against a keyword/family taxonomy
  3. Write a "would-have-done-X" shadow decision to Redis
  4. Score outcome at 1h/6h/24h by reading each product's snapshot mark

The constant `EXECUTE_TRADES = False` at the top of this file is a load-bearing
invariant. If anyone flips it to True, a live_runner integration test in
tests/test_twitter_shadow_only.py fails. Do NOT flip it without an explicit
ask from Adam. This module is a validation harness, not a trading path.

Data source
-----------
Nitter RSS is used because:
  - Free (Twitter's API v2 is $200/mo Basic or $5000/mo Pro)
  - No API keys, works from any container
  - RSS feed per account is stable

Nitter instances die often, so we ship a multi-instance failover list. If
all instances fail on a poll, we log and continue — no crashes.

Watchlist
---------
Curated ~15 accounts covering the product families the bot trades. See
WATCHLIST below. Adding a new account is a single-line edit.

Signal taxonomy
---------------
Two orthogonal axes:
  - direction: bullish | bearish
  - severity:  catalyst (specific event) | sentiment (general tone)

The `would_action` produced is one of:
  - BLOCK_ARM      — bearish catalyst affects a family → we'd have gated BUYs
  - ALERT_ARM      — bullish catalyst → informational, no gate (arms unchanged)
  - EXIT_HINT      — very bearish (crash / hack / SEC lawsuit) → we'd have
                     tightened trails / considered exits (still informational)

Outcome evaluation
------------------
For each signal, we snapshot the mark of every affected product at signal
time, then at 1h/6h/24h later record the pct change. A signal is "correct"
if the sign of the move matches the predicted direction. The dashboard
shows the running accuracy so Adam can decide whether to promote out of
shadow mode.
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from typing import Optional

import requests


# =============================================================================
# HARD GUARANTEE — do not flip this without an explicit ask from Adam.
# =============================================================================
EXECUTE_TRADES = False


# Multi-instance Nitter RSS list. As instances die, add new ones from
# https://github.com/zedeus/nitter/wiki/Instances. Order = try-order.
NITTER_INSTANCES = [
    "https://nitter.privacydev.net",
    "https://xcancel.com",
    "https://nitter.poast.org",
    "https://nitter.net",
]


# Curated watchlist. Each entry maps a Twitter handle to the product families
# the account has commentary-value on. Multiple families = we log the signal
# against every family listed. Keep this list small and high-signal; noise
# accounts (memecoin shillers, generic finance twitter) hurt the harness.
WATCHLIST: list[dict] = [
    # Crypto majors + perps — high-signal accounts that move markets
    {"handle": "cz_binance",       "families": ["crypto_major", "crypto_perp"]},
    {"handle": "VitalikButerin",   "families": ["crypto_major"]},
    {"handle": "saylor",           "families": ["crypto_major"]},
    {"handle": "APompliano",       "families": ["crypto_major", "crypto_perp"]},
    {"handle": "DocumentingBTC",   "families": ["crypto_major"]},
    # Metals — silver / gold / copper / platinum commentary
    {"handle": "PeterSchiff",      "families": ["metals"]},
    {"handle": "KingWorldNews",    "families": ["metals"]},
    {"handle": "SilverPriceX",     "families": ["metals"]},
    # Energy — oil / natgas
    {"handle": "JavierBlas",       "families": ["energy"]},
    {"handle": "EIAgov",           "families": ["energy"]},
    {"handle": "OilSheppard",      "families": ["energy"]},
    # Macro (moves everything — mapped to all families)
    {"handle": "federalreserve",   "families": ["metals", "crypto_major", "crypto_perp", "energy"]},
    {"handle": "LizAnnSonders",    "families": ["metals", "crypto_major", "energy"]},
    {"handle": "zerohedge",        "families": ["metals", "crypto_major", "crypto_perp", "energy"]},
]


# Keyword taxonomy. Weights are additive per match; a signal fires above
# `signal_threshold` (default 2). Case-insensitive whole-word match.
#
# Design principle: catalyst keywords (SEC / FOMC / hack) score higher
# than sentiment words (rally / crash) because catalysts are actionable
# events, sentiment is background noise.
BEARISH_CATALYSTS = {
    "hack": 3, "hacked": 3, "exploit": 3, "drained": 3, "rug": 3,
    "sec lawsuit": 3, "sec charges": 3, "sec suing": 3, "sec sues": 3,
    "delisting": 3, "delisted": 3,
    "bankruptcy": 3, "insolvent": 3, "chapter 11": 3,
    "opec cut": 2,  # oil bearish? actually bullish for oil, gets remapped by family below
    "recession": 2, "hard landing": 2,
    "liquidations": 2, "cascade": 2, "capitulation": 2,
    "war escalation": 2, "escalation": 1,
}
BEARISH_SENTIMENT = {
    "crash": 2, "crashing": 2, "plunge": 2, "plunging": 2,
    "dump": 2, "dumping": 2, "sell-off": 2, "selloff": 2,
    "bear market": 2, "bearish": 1, "downtrend": 1,
    "fall": 1, "falling": 1, "decline": 1, "drop": 1, "dropping": 1,
}
BULLISH_CATALYSTS = {
    "etf approved": 3, "sec approves": 3, "approval": 2,
    "adoption": 2, "buyback": 2,
    "rate cut": 3, "dovish": 2,
    "halving": 3,
    "opec production cut": 3,  # bullish for oil
}
BULLISH_SENTIMENT = {
    "rally": 2, "rallying": 2, "breakout": 2, "surge": 2, "surging": 2,
    "moon": 1, "bullish": 1, "uptrend": 1, "ath": 2, "all-time high": 2,
    "pump": 1, "melt-up": 2,
}


# Special-case family remapping. Some keywords are directionally-inverted
# for specific families:
#   "opec cut" (production cut) is BULLISH for oil/energy despite the word "cut"
#   "recession" is generally bearish for risk but can be bullish for gold
FAMILY_KEYWORD_OVERRIDES: dict[tuple[str, str], str] = {
    ("energy", "opec cut"): "bullish",
    ("energy", "opec production cut"): "bullish",
    ("metals", "recession"): "bullish",   # safe haven bid on gold/silver
}


TWEET_ID_TTL_SECS = 7 * 24 * 3600  # dedupe window: don't process same tweet twice within a week


def _score_text(text: str, family: str) -> tuple[str, int, list[str]]:
    """Return (direction, score, matched_keywords) for a tweet against a family.
    direction is 'bullish' | 'bearish' | 'neutral'."""
    if not text:
        return ("neutral", 0, [])
    lower = text.lower()
    bull_score = 0
    bear_score = 0
    matched: list[str] = []
    for kw, w in BEARISH_CATALYSTS.items():
        if kw in lower:
            override = FAMILY_KEYWORD_OVERRIDES.get((family, kw))
            if override == "bullish":
                bull_score += w
            else:
                bear_score += w
            matched.append(kw)
    for kw, w in BEARISH_SENTIMENT.items():
        if re.search(rf"\b{re.escape(kw)}\b", lower):
            bear_score += w
            matched.append(kw)
    for kw, w in BULLISH_CATALYSTS.items():
        if kw in lower:
            bull_score += w
            matched.append(kw)
    for kw, w in BULLISH_SENTIMENT.items():
        if re.search(rf"\b{re.escape(kw)}\b", lower):
            bull_score += w
            matched.append(kw)
    if bull_score == bear_score:
        return ("neutral", 0, matched)
    if bear_score > bull_score:
        return ("bearish", bear_score - bull_score, matched)
    return ("bullish", bull_score - bear_score, matched)


def _would_action(direction: str, score: int) -> str:
    """Map (direction, score) → would_action label.
    Score thresholds picked conservatively — we'd rather miss a signal than
    flood the log with noise. A score of 3+ is a clear catalyst (SEC / hack /
    ETF approval); 2 is strong sentiment; 1 is background chatter (ignored)."""
    if score < 2:
        return "IGNORE"
    if direction == "bearish":
        if score >= 3:
            return "EXIT_HINT"   # tight trail / consider exit
        return "BLOCK_ARM"       # gate new BUYs
    if direction == "bullish":
        return "ALERT_ARM"       # informational; no gate
    return "IGNORE"


def _fetch_rss(handle: str, timeout: float = 8.0) -> Optional[str]:
    """Try each Nitter instance in order; return the first successful RSS body."""
    for inst in NITTER_INSTANCES:
        url = f"{inst}/{handle}/rss"
        try:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": "silver-swing/1.0"})
            if r.status_code == 200 and "<rss" in r.text[:2000]:
                return r.text
        except Exception:
            continue
    return None


_RSS_ITEM_RE = re.compile(r"<item\b.*?</item>", re.DOTALL | re.IGNORECASE)
_RSS_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_RSS_LINK_RE = re.compile(r"<link>(.*?)</link>", re.DOTALL | re.IGNORECASE)
_RSS_PUBDATE_RE = re.compile(r"<pubDate>(.*?)</pubDate>", re.DOTALL | re.IGNORECASE)


def _parse_rss(body: str) -> list[dict]:
    """Very small RSS parser. Full feedparser is overkill for our needs and
    isn't in requirements.txt; the Nitter feed shape is stable enough to
    regex. Returns list of {title, link, pubDate}."""
    out = []
    for item in _RSS_ITEM_RE.findall(body):
        title = _RSS_TITLE_RE.search(item)
        link = _RSS_LINK_RE.search(item)
        pub = _RSS_PUBDATE_RE.search(item)
        if not title:
            continue
        raw = title.group(1).strip()
        # Nitter wraps tweet text in CDATA and HTML-escapes ampersands
        raw = raw.replace("<![CDATA[", "").replace("]]>", "")
        raw = raw.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
        out.append({
            "text": raw,
            "url": link.group(1).strip() if link else "",
            "pubDate": pub.group(1).strip() if pub else "",
        })
    return out


def _tweet_id(handle: str, item: dict) -> str:
    """Stable dedupe id — hash of handle + url + first 100 chars of text."""
    h = hashlib.sha256()
    h.update(handle.encode("utf-8"))
    h.update(b"|")
    h.update(item.get("url", "").encode("utf-8"))
    h.update(b"|")
    h.update((item.get("text", "") or "")[:100].encode("utf-8"))
    return h.hexdigest()[:16]


def _redis_client_from_store(store):
    """Best-effort extraction of the redis client from RedisJsonStore.
    Returns None for JsonFileStateStore (local dev); the caller falls back
    to a state-store scope in that case."""
    return getattr(store, "_r", None)


LOG_KEY = "silver-swing:twitter-signals"
DEDUPE_KEY = "silver-swing:twitter-seen"
MAX_LOG_ENTRIES = 500


def _seen_ids(store) -> set[str]:
    """Load the dedupe set. Uses a Redis set when available for O(1)
    membership; falls back to a state-store scope otherwise."""
    r = _redis_client_from_store(store)
    if r:
        try:
            return set(r.smembers(DEDUPE_KEY))
        except Exception:
            return set()
    # Fallback: json file backend — store dedupe as a config-scope list
    try:
        blob = store.get_config("__twitter__", "__dedupe__") or {}
        return set(blob.get("seen", []))
    except Exception:
        return set()


def _mark_seen(store, tid: str) -> None:
    r = _redis_client_from_store(store)
    if r:
        try:
            r.sadd(DEDUPE_KEY, tid)
            r.expire(DEDUPE_KEY, TWEET_ID_TTL_SECS)
            return
        except Exception:
            pass
    try:
        blob = store.get_config("__twitter__", "__dedupe__") or {"seen": []}
        seen = list(blob.get("seen", []))
        if tid not in seen:
            seen.append(tid)
            # Trim to last 5000 so this doesn't grow unbounded
            if len(seen) > 5000:
                seen = seen[-5000:]
        store.put_config("__twitter__", "__dedupe__", {"seen": seen})
    except Exception:
        pass


def _append_log(store, entry: dict) -> None:
    """Append a signal-decision entry to the log. Keeps last MAX_LOG_ENTRIES."""
    r = _redis_client_from_store(store)
    import json
    if r:
        try:
            r.lpush(LOG_KEY, json.dumps(entry))
            r.ltrim(LOG_KEY, 0, MAX_LOG_ENTRIES - 1)
            return
        except Exception:
            pass
    # Fallback path — use config scope on the store
    try:
        blob = store.get_config("__twitter__", "__log__") or {"entries": []}
        entries = list(blob.get("entries", []))
        entries.insert(0, entry)
        entries = entries[:MAX_LOG_ENTRIES]
        store.put_config("__twitter__", "__log__", {"entries": entries})
    except Exception:
        pass


def read_log(store, limit: int = 200) -> list[dict]:
    """Read the shadow signal log for the dashboard."""
    import json
    r = _redis_client_from_store(store)
    if r:
        try:
            raws = r.lrange(LOG_KEY, 0, limit - 1) or []
            return [json.loads(x) for x in raws]
        except Exception:
            return []
    try:
        blob = store.get_config("__twitter__", "__log__") or {"entries": []}
        return list(blob.get("entries", []))[:limit]
    except Exception:
        return []


def _update_log_entry(store, entry_id: str, updates: dict) -> None:
    """Read-modify-write helper for outcome updates. O(N) on log size but
    N ≤ 500 so this is fine at our scale."""
    import json
    r = _redis_client_from_store(store)
    if r:
        try:
            raws = r.lrange(LOG_KEY, 0, MAX_LOG_ENTRIES - 1) or []
            new_raws = []
            for x in raws:
                d = json.loads(x)
                if d.get("id") == entry_id:
                    d.update(updates)
                new_raws.append(json.dumps(d))
            if new_raws:
                r.delete(LOG_KEY)
                r.rpush(LOG_KEY, *new_raws)
            return
        except Exception:
            pass
    try:
        blob = store.get_config("__twitter__", "__log__") or {"entries": []}
        entries = list(blob.get("entries", []))
        for e in entries:
            if e.get("id") == entry_id:
                e.update(updates)
                break
        store.put_config("__twitter__", "__log__", {"entries": entries})
    except Exception:
        pass


def _products_in_family(store, tenant: str, family: str) -> list[str]:
    """Return tracked symbols matching a family from correlation.CORRELATION_FAMILIES.
    Cross-tenant awareness — if the tenant has no products, tries the first
    tenant that does. This is a shadow tool, so tenant strictness isn't needed."""
    from correlation import _family_of
    for candidate_tenant in (tenant, *(t for t in store.list_tenants() if t != tenant)):
        syms = [s for s in store.list_symbols(candidate_tenant)
                if not s.startswith("__") and _family_of(s) == family]
        if syms:
            return syms
    return []


def _baseline_marks(store, symbols: list[str], tenant: str) -> dict[str, float]:
    """Snapshot mark for every symbol at signal time. Cross-tenant fallback
    same as _products_in_family."""
    marks: dict[str, float] = {}
    for candidate_tenant in (tenant, *(t for t in store.list_tenants() if t != tenant)):
        remaining = [s for s in symbols if s not in marks]
        for s in remaining:
            try:
                snap = store.get_snapshot(candidate_tenant, s) or {}
                m = float(snap.get("last_mark") or 0)
                if m > 0:
                    marks[s] = m
            except Exception:
                continue
        if len(marks) == len(symbols):
            break
    return marks


OUTCOME_HORIZONS_SECS = {
    "1h": 3600,
    "6h": 6 * 3600,
    "24h": 24 * 3600,
}


def tick(store, tenant: str = "__default__", handles_limit: Optional[int] = None) -> dict:
    """One poll cycle. Fetches each handle's RSS, detects signals, writes any
    new ones to the log, then evaluates outcomes for older signals.

    Returns a small telemetry dict: {handles_polled, tweets_scanned,
    signals_new, outcomes_updated}.
    Meant to be called every ~5 min from live_runner.
    """
    handles_polled = 0
    tweets_scanned = 0
    signals_new = 0
    seen = _seen_ids(store)
    for entry in (WATCHLIST if handles_limit is None else WATCHLIST[:handles_limit]):
        handle = entry["handle"]
        families = entry["families"]
        body = _fetch_rss(handle)
        handles_polled += 1
        if not body:
            continue
        items = _parse_rss(body)
        for it in items:
            tid = _tweet_id(handle, it)
            if tid in seen:
                continue
            tweets_scanned += 1
            # Score against each family the account covers; take the strongest
            best = None
            for fam in families:
                dirn, sc, matched = _score_text(it["text"], fam)
                action = _would_action(dirn, sc)
                if action == "IGNORE":
                    continue
                cand = {"family": fam, "direction": dirn, "score": sc,
                        "matched": matched, "would_action": action}
                if best is None or sc > best["score"]:
                    best = cand
            # Mark seen even if IGNORE — so we don't rescore next tick
            _mark_seen(store, tid)
            if best is None:
                continue
            products = _products_in_family(store, tenant, best["family"])
            baselines = _baseline_marks(store, products, tenant)
            record = {
                "id": str(uuid.uuid4()),
                "ts": time.time(),
                "source": f"twitter@{handle}",
                "tweet_url": it.get("url", ""),
                "tweet_text": it.get("text", "")[:400],
                "family": best["family"],
                "direction": best["direction"],
                "score": best["score"],
                "keywords_matched": best["matched"],
                "would_action": best["would_action"],
                "products_affected": products,
                "baseline_marks": baselines,
                "outcomes": {h: None for h in OUTCOME_HORIZONS_SECS},
                "shadow_mode": True,
                "trades_executed": False,
            }
            _append_log(store, record)
            signals_new += 1

    outcomes_updated = _evaluate_outcomes(store, tenant)
    return {
        "handles_polled": handles_polled,
        "tweets_scanned": tweets_scanned,
        "signals_new": signals_new,
        "outcomes_updated": outcomes_updated,
    }


def _evaluate_outcomes(store, tenant: str) -> int:
    """Walk the log, update outcomes for any entry whose horizon has elapsed."""
    entries = read_log(store, limit=MAX_LOG_ENTRIES)
    now = time.time()
    updated = 0
    for e in entries:
        eid = e.get("id")
        if not eid:
            continue
        outs = dict(e.get("outcomes") or {})
        products = e.get("products_affected") or []
        baselines = e.get("baseline_marks") or {}
        elapsed = now - float(e.get("ts") or now)
        any_change = False
        for horizon, secs in OUTCOME_HORIZONS_SECS.items():
            if outs.get(horizon) is not None:
                continue  # already recorded
            if elapsed < secs:
                continue  # not yet due
            # Read current marks and compute pct move on each product
            current = _baseline_marks(store, products, tenant)
            moves = {}
            avg_pct = 0.0
            n = 0
            for p in products:
                b = float(baselines.get(p) or 0)
                c = float(current.get(p) or 0)
                if b > 0 and c > 0:
                    pct = (c - b) / b * 100.0
                    moves[p] = round(pct, 3)
                    avg_pct += pct
                    n += 1
            if n == 0:
                # No usable price data; mark as unknown so we stop retrying
                outs[horizon] = {"pct_avg": None, "moves": {}, "verdict": "unknown"}
            else:
                avg_pct /= n
                # Correctness: signal direction should match sign of avg move.
                # "bullish" predicted → up move; "bearish" → down move.
                direction = e.get("direction")
                if abs(avg_pct) < 0.1:
                    verdict = "flat"
                elif direction == "bullish":
                    verdict = "correct" if avg_pct > 0 else "wrong"
                elif direction == "bearish":
                    verdict = "correct" if avg_pct < 0 else "wrong"
                else:
                    verdict = "n/a"
                outs[horizon] = {"pct_avg": round(avg_pct, 3), "moves": moves, "verdict": verdict}
            any_change = True
        if any_change:
            _update_log_entry(store, eid, {"outcomes": outs})
            updated += 1
    return updated


def accuracy_summary(store) -> dict:
    """Aggregate hit rate across the log for the dashboard header."""
    entries = read_log(store, limit=MAX_LOG_ENTRIES)
    tally = {h: {"correct": 0, "wrong": 0, "flat": 0, "unknown": 0}
             for h in OUTCOME_HORIZONS_SECS}
    for e in entries:
        outs = e.get("outcomes") or {}
        for h, res in outs.items():
            if not res:
                continue
            v = res.get("verdict") or "unknown"
            if v in tally.get(h, {}):
                tally[h][v] += 1
    return {
        "total_signals": len(entries),
        "shadow_mode": True,
        "by_horizon": tally,
    }
