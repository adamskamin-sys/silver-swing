"""
Alerting — push notifications for events that can't wait for a dashboard poll
(spec §9B). Wired into swing_leg.py halts and used by the heartbeat watcher.

The bot logs everything (TradeLog); this is for the subset that needs to
reach the human's phone NOW: any HALT, a heartbeat miss, a margin-call
warning, the daily-loss circuit breaker tripping.

Backends (all satisfy the Notifier Protocol):
  LogNotifier      — stdout + optional file. Always works, zero config. Default.
  TelegramNotifier — Telegram Bot API. Needs bot token + chat ID env vars.
  MultiNotifier    — fan-out to a list of notifiers. Use this to send to log
                     AND telegram simultaneously.

Twilio SMS is intentionally omitted for now — Telegram covers the "phone-
reachable while asleep" use case with less setup and no per-message cost.
"""

from __future__ import annotations

import os
import sys
import time
from enum import Enum
from typing import Optional, Protocol

import requests


class Priority(str, Enum):
    INFO = "info"        # heartbeat, scale-up, cycle complete
    WARN = "warn"        # reconcile mismatch, fee-gate widened
    CRIT = "crit"        # HALT, margin call, kill switch trip


class Notifier(Protocol):
    def send(self, subject: str, body: str, priority: Priority = Priority.INFO) -> None: ...


class LogNotifier:
    """Prints to stdout with a priority tag. Optionally tees to a file for
    async review. Always safe; no external dependencies."""

    def __init__(self, path: Optional[str] = None, stream=None):
        self.path = path
        self.stream = stream if stream is not None else sys.stdout

    def send(self, subject: str, body: str, priority: Priority = Priority.INFO) -> None:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        line = f"[{ts}] [{priority.value.upper()}] {subject}: {body}"
        try:
            print(line, file=self.stream, flush=True)
        except Exception:
            pass
        if self.path:
            try:
                with open(self.path, "a") as f:
                    f.write(line + "\n")
            except Exception:
                pass


class TelegramNotifier:
    """Telegram Bot API push. Requires:
      TELEGRAM_BOT_TOKEN — from @BotFather
      TELEGRAM_CHAT_ID   — your account's chat ID (get from @userinfobot)

    Fails quietly on network error — an alerting outage must NOT crash the
    bot. A local LogNotifier alongside catches whatever this misses.
    """

    API = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None,
                 timeout: float = 5.0):
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self.timeout = timeout

    def _emoji(self, priority: Priority) -> str:
        return {"info": "ℹ️", "warn": "⚠️", "crit": "🚨"}.get(priority.value, "")

    def send(self, subject: str, body: str, priority: Priority = Priority.INFO) -> None:
        if not self.token or not self.chat_id:
            return  # not configured; silently skip
        text = f"{self._emoji(priority)} *{subject}*\n{body}"
        try:
            requests.post(
                self.API.format(token=self.token),
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=self.timeout,
            )
        except Exception:
            pass  # never let alerting take down the bot


class MultiNotifier:
    """Fan-out. Failure of one child doesn't affect the others."""

    def __init__(self, *notifiers: Notifier):
        self.notifiers = list(notifiers)

    def send(self, subject: str, body: str, priority: Priority = Priority.INFO) -> None:
        for n in self.notifiers:
            try:
                n.send(subject, body, priority)
            except Exception:
                pass


def default_notifier() -> Notifier:
    """Build the default stack from env: LogNotifier always, TelegramNotifier
    if TELEGRAM_BOT_TOKEN is set. Callers can override with a custom stack."""
    log = LogNotifier()
    if os.getenv("TELEGRAM_BOT_TOKEN"):
        return MultiNotifier(log, TelegramNotifier())
    return log
