"""Auditor 2026-07-14 must-verify #3 for core_qty:

Confirm core_qty is CONFIG/INTENT — set by the user via the dashboard or
config seed — and NEVER a drifting runtime value written by trader code.
If runtime code wrote core_qty at runtime, reconciliation's
'expected_position = core_qty + swing_qty' math would mask real position
mismatches (bot silently 'agrees with itself' about a drifting core).
"""
from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).parent.parent


def _source_files():
    """Runtime .py sources (exclude tests, venv, scripts, and validator)."""
    for p in REPO.rglob("*.py"):
        s = str(p)
        if "/.venv/" in s or "/tests/" in s or "/scripts/" in s:
            continue
        yield p


# The forbidden patterns — code that would mutate cfg.core_qty at runtime.
# Each is a regex over source text. If any match in any runtime .py file,
# core_qty has become a runtime-writable value and the reconciliation
# expected_position math would start masking real drift.
_FORBIDDEN = [
    # cfg.core_qty = X  or  self.cfg.core_qty = X  (attribute assignment)
    (r'\bcfg\.core_qty\s*=', "cfg.core_qty assigned at runtime"),
    (r'\bself\.cfg\.core_qty\s*=', "self.cfg.core_qty assigned at runtime"),
    # dict-style writes to a config dict's core_qty key
    (r'\["core_qty"\]\s*=', 'dict["core_qty"] = ... assignment at runtime'),
    (r"\['core_qty'\]\s*=", "dict['core_qty'] = ... assignment at runtime"),
]


def test_core_qty_never_written_at_runtime():
    """No runtime source file mutates core_qty on the LIVE path. It comes
    only from user intent (dashboard PUT /api/config, config seed,
    defaults) — the reconciliation monitor's core+swing sum stays
    trustworthy on adam-live."""
    hits: list[str] = []
    for path in _source_files():
        text = path.read_text()
        rel = str(path.relative_to(REPO))
        # Files that legitimately WRITE core_qty (config sources, not runtime).
        if rel in ("config_validator.py",     # clamp_to_bounds; not called at runtime
                   "expert_tuner.py"):        # DEFAULT_CFG dict literal, not runtime write
            continue
        # Lab tenant fixup is deprecated (WS3 plan will delete lab entirely).
        # Skip writes inside _fixup_lab_config — they never fire on adam-live.
        # TODO(WS3-phase5): delete _fixup_lab_config and remove this exclusion.
        lab_fixup_span = _find_function_span(text, "_fixup_lab_config")

        for pat, label in _FORBIDDEN:
            for m in re.finditer(pat, text):
                line_no = text.count("\n", 0, m.start()) + 1
                if lab_fixup_span and lab_fixup_span[0] <= line_no <= lab_fixup_span[1]:
                    continue  # lab-deprecated
                hits.append(f"{rel}:{line_no} — {label}")
    assert not hits, (
        "core_qty is being written by runtime code — this breaks the "
        "reconciliation expected_position invariant.\n\n"
        + "\n".join(hits)
    )


def _find_function_span(text: str, name: str) -> tuple[int, int] | None:
    """Return (start_line, end_line) of a top-level `def name(...)` block.
    Simple heuristic: from the def line to the next non-blank line at column 0
    that isn't part of this function's body."""
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines, start=1):
        if ln.startswith(f"def {name}("):
            start = i
            break
    if start is None:
        return None
    # Walk forward until we hit a line that starts at column 0 with 'def '
    # or 'class ' — that's the end of the previous function.
    for j in range(start, len(lines)):
        if j == start - 1:
            continue
        ln = lines[j]
        if (ln.startswith("def ") or ln.startswith("class ")) and j + 1 != start:
            return (start, j)
    return (start, len(lines))


def test_clamp_to_bounds_not_called_at_runtime():
    """config_validator.clamp_to_bounds() COULD write core_qty=0 if given
    a negative value — but it's only used by tests/defaults, never at
    runtime. Regression guard: assert no runtime .py imports or calls it."""
    for path in _source_files():
        text = path.read_text()
        rel = str(path.relative_to(REPO))
        if rel == "config_validator.py":
            continue
        assert "clamp_to_bounds" not in text, (
            f"{rel} references clamp_to_bounds — if you're calling it at "
            f"runtime, it would rewrite core_qty. Move to a test-only path.")


def test_reconciliation_reads_core_qty_from_config_scope():
    """live_runner's reconciliation-monitor call site must read core_qty
    from the CONFIG scope, not from state — otherwise a runtime state.core_qty
    drift would flow through as 'expected'."""
    src = (REPO / "live_runner.py").read_text()
    # The reconciliation loop reads config for core_qty
    assert 'cfg.get("core_qty")' in src or "cfg['core_qty']" in src, (
        "live_runner reconciliation must read core_qty from cfg (config), "
        "not from state — else drift becomes silently 'expected'.")
