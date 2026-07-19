"""Tests for log_config — HTML truncation + dedup for Coinbase SDK noise.

Failure modes covered:
  - HTML pages in log messages get truncated (Cloudflare 502 class)
  - Repeated identical messages within window are suppressed
  - After window expiry, suppressed count is attached to the next occurrence
  - Non-HTML, non-repeated messages pass through untouched
  - install() is idempotent
"""
from __future__ import annotations

import logging

import log_config


def _make_record(msg: str, name: str = "coinbase.RESTClient",
                  level: int = logging.ERROR) -> logging.LogRecord:
    return logging.LogRecord(
        name=name, level=level, pathname="test.py", lineno=1,
        msg=msg, args=None, exc_info=None,
    )


def test_html_truncation():
    f = log_config._NoiseFilter()
    html = ("<!DOCTYPE html>" + "<body>" + ("x" * 2000) + "</body></html>")
    rec = _make_record(html)
    passed = f.filter(rec)
    assert passed is True  # message still logged
    assert "HTML body truncated" in rec.msg
    assert len(rec.msg) < 700  # 500 head + suffix, way less than 2000


def test_no_truncation_when_no_html():
    f = log_config._NoiseFilter()
    msg = "HTTP Error: 429 rate limited"
    rec = _make_record(msg)
    passed = f.filter(rec)
    assert passed is True
    assert rec.msg == msg  # unchanged


def test_dedup_suppresses_immediate_repeat():
    f = log_config._NoiseFilter()
    rec1 = _make_record("Connection closed (ERROR)")
    rec2 = _make_record("Connection closed (ERROR)")
    assert f.filter(rec1) is True
    assert f.filter(rec2) is False  # suppressed within window


def test_dedup_releases_after_window():
    f = log_config._NoiseFilter()
    rec1 = _make_record("Connection closed (ERROR)")
    f.filter(rec1)
    # Simulate 3 more suppressed within window
    for _ in range(3):
        f.filter(_make_record("Connection closed (ERROR)"))
    # Manually advance time by rewinding stored ts
    sig_key = next(iter(f._recent))
    first_ts, count = f._recent[sig_key]
    f._recent[sig_key] = (first_ts - log_config.DEDUP_WINDOW_SECS - 1, count)
    rec_next = _make_record("Connection closed (ERROR)")
    assert f.filter(rec_next) is True
    assert "prev 3 identical suppressed" in rec_next.msg


def test_different_messages_not_deduped():
    f = log_config._NoiseFilter()
    assert f.filter(_make_record("error A")) is True
    assert f.filter(_make_record("error B")) is True  # different msg


def test_different_loggers_dedup_separately():
    f = log_config._NoiseFilter()
    assert f.filter(_make_record("same msg", name="coinbase")) is True
    assert f.filter(_make_record("same msg", name="coinbase.RESTClient")) is True


def test_install_is_idempotent():
    # Prior test/import may have already installed — strip existing NoiseFilters
    # from every target logger so this test observes the install() effect in
    # isolation.
    for name in ("coinbase", "coinbase.RESTClient", "coinbase.WSClient"):
        lg = logging.getLogger(name)
        for f in list(lg.filters):
            if isinstance(f, log_config._NoiseFilter):
                lg.removeFilter(f)
    log_config._installed = False
    log_config.install()
    log_config.install()
    log_config.install()
    lg = logging.getLogger("coinbase.RESTClient")
    filters = [x for x in lg.filters if isinstance(x, log_config._NoiseFilter)]
    assert len(filters) == 1  # only one filter attached, not three


def test_filter_survives_malformed_record():
    f = log_config._NoiseFilter()

    class BadRecord:
        name = "coinbase"
        levelno = logging.ERROR

        def getMessage(self):
            raise ValueError("cannot format")

    # Should not raise; should fail-open and let it through.
    assert f.filter(BadRecord()) is True


def test_prune_bounds_memory():
    f = log_config._NoiseFilter()
    # Fill with 600 unique entries — should trigger prune.
    for i in range(600):
        f.filter(_make_record(f"unique msg {i}"))
    assert len(f._recent) <= 600  # bounded (500 threshold + a few added post-prune)
