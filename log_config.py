"""Log-noise filter for the Coinbase SDK.

Two failure modes were flooding the trade logs (2026-07-18):

1. Coinbase infrastructure 502s → the SDK logs the ENTIRE Cloudflare HTML
   error page at ERROR level. One 502 becomes 40+ lines of unreadable
   HTML/CSS, drowning the actual signal.

2. WebSocket disconnects → "Connection closed (ERROR): no close frame
   received or sent" spam. The SDK auto-reconnects (retry=True), so the
   disconnect itself is not actionable — but it prints per-attempt.

This module installs a filter on the coinbase / coinbase.RESTClient /
coinbase.WSClient loggers that:
  - Truncates any log message containing HTML markers (<!DOCTYPE, <html)
    to the first 500 chars + a marker. Preserves the fact that an error
    happened AND the beginning (which usually says '502 Bad Gateway').
  - Deduplicates identical messages within DEDUP_WINDOW_SECS — the first
    fires, subsequent occurrences within the window are silenced. On
    window expiry the next occurrence fires again with a suppressed=N tag.

Call `install()` once at process startup (main.py + live_runner.py).
Idempotent — safe to call multiple times.
"""
from __future__ import annotations
import logging
import time


HTML_MARKERS = ("<!DOCTYPE", "<html", "<HTML", "<!doctype")
HTML_TRUNCATE_CHARS = 500
DEDUP_WINDOW_SECS = 60.0


class _NoiseFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        # Map: msg_signature -> (first_seen_ts, suppressed_count)
        self._recent: dict[str, tuple[float, int]] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True  # fail-open: never swallow due to filter crash

        # HTML truncation: replace the message in-place.
        if any(m in msg for m in HTML_MARKERS):
            head = msg[:HTML_TRUNCATE_CHARS]
            record.msg = f"{head} ...[HTML body truncated, {len(msg)} chars total]"
            record.args = None

        # Dedup identical messages within the window.
        now = time.time()
        sig = f"{record.name}:{record.levelno}:{record.getMessage()[:200]}"
        prev = self._recent.get(sig)
        if prev is not None:
            first_ts, suppressed = prev
            if now - first_ts < DEDUP_WINDOW_SECS:
                # Still in window — suppress + count.
                self._recent[sig] = (first_ts, suppressed + 1)
                return False
            # Window expired. Let this one through, tag with suppressed count.
            if suppressed > 0:
                record.msg = f"{record.getMessage()} [prev {suppressed} identical suppressed]"
                record.args = None
            self._recent[sig] = (now, 0)
            return True
        # First occurrence in a while — remember and let through.
        self._recent[sig] = (now, 0)
        # Periodic prune (bounded memory): drop entries whose window is well past.
        if len(self._recent) > 500:
            cutoff = now - DEDUP_WINDOW_SECS * 4
            self._recent = {
                k: v for k, v in self._recent.items() if v[0] > cutoff
            }
        return True


_installed = False
_filter_instance: _NoiseFilter | None = None


def install() -> None:
    """Idempotent — install the filter on Coinbase SDK loggers."""
    global _installed, _filter_instance
    if _installed:
        return
    _filter_instance = _NoiseFilter()
    for name in ("coinbase", "coinbase.RESTClient", "coinbase.WSClient"):
        logging.getLogger(name).addFilter(_filter_instance)
    _installed = True
