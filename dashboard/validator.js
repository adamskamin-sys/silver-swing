/**
 * Server-side JS mirror of config_validator.py. Two implementations of the
 * same rules is a maintenance burden — but the alternative (calling Python
 * from Node for every write) adds a fragile cross-language dependency to the
 * critical path.
 *
 * Test discipline: any rule change must land in BOTH files. There's a shared
 * test fixture (../tests/test_config_validator.py) that documents the rules
 * in prose; keep this file in sync with it.
 */

const VALID_EXIT_MODES = new Set(['fixed_limit', 'trailing_stop']);

function num(cfg, key, issues, required = true) {
  const v = cfg[key];
  if (v === null || v === undefined || v === '') {
    if (required) issues.push({ field: key, message: `${key} is required` });
    return null;
  }
  const n = Number(v);
  if (Number.isNaN(n)) {
    issues.push({ field: key, message: `${key} must be a number, got ${JSON.stringify(v)}` });
    return null;
  }
  return n;
}

function int(cfg, key, issues, required = true) {
  const v = num(cfg, key, issues, required);
  if (v === null) return null;
  if (!Number.isInteger(v)) {
    issues.push({ field: key, message: `${key} must be a whole number, got ${v}` });
    return null;
  }
  return v;
}

export function validateConfig(cfg = {}) {
  const issues = [];

  const core_qty = int(cfg, 'core_qty', issues);
  const swing_qty = int(cfg, 'swing_qty', issues);
  const max_swing_qty = int(cfg, 'max_swing_qty', issues);
  const sell_px = num(cfg, 'sell_px', issues);
  const buy_px = num(cfg, 'buy_px', issues);
  const abort_below = num(cfg, 'abort_below', issues);
  const abort_above = num(cfg, 'abort_above', issues);
  const margin_per_contract = num(cfg, 'margin_per_contract', issues);
  const fee_rt = num(cfg, 'fee_per_contract_roundtrip', issues);
  const scale_up_mult = num(cfg, 'scale_up_buffer_mult', issues);
  const contract_size = num(cfg, 'contract_size', issues);
  const fee_sanity = num(cfg, 'fee_sanity_multiplier', issues, false);

  const exit_mode = cfg.exit_mode || 'fixed_limit';
  if (!VALID_EXIT_MODES.has(exit_mode)) {
    issues.push({
      field: 'exit_mode',
      message: `exit_mode must be one of ${[...VALID_EXIT_MODES].join(', ')}, got ${JSON.stringify(exit_mode)}`,
    });
  }

  if (core_qty !== null && core_qty <= 0)
    issues.push({ field: 'core_qty', message: 'core_qty must be > 0 (that\'s the whole floor)' });
  if (swing_qty !== null && swing_qty < 1)
    issues.push({ field: 'swing_qty', message: 'swing_qty must be >= 1' });
  if (max_swing_qty !== null && max_swing_qty < 1)
    issues.push({ field: 'max_swing_qty', message: 'max_swing_qty must be >= 1' });
  if (swing_qty !== null && max_swing_qty !== null && swing_qty > max_swing_qty)
    issues.push({ field: 'swing_qty', message: `swing_qty (${swing_qty}) must be <= max_swing_qty (${max_swing_qty})` });
  if (buy_px !== null && sell_px !== null && buy_px >= sell_px)
    issues.push({ field: 'buy_px', message: `buy_px (${buy_px}) must be < sell_px (${sell_px}); otherwise the swing loses money` });
  if (abort_below !== null && abort_above !== null && abort_below >= abort_above)
    issues.push({ field: 'abort_below', message: `abort_below (${abort_below}) must be < abort_above (${abort_above})` });
  if (abort_below !== null && buy_px !== null && abort_below >= buy_px)
    issues.push({ field: 'abort_below', message: `abort_below (${abort_below}) must be < buy_px (${buy_px}); the governor sits outside the range` });
  if (sell_px !== null && abort_above !== null && sell_px >= abort_above)
    issues.push({ field: 'abort_above', message: `abort_above (${abort_above}) must be > sell_px (${sell_px}); the governor sits outside the range` });
  if (margin_per_contract !== null && margin_per_contract <= 0)
    issues.push({ field: 'margin_per_contract', message: 'margin_per_contract must be > 0' });
  if (fee_rt !== null && fee_rt < 0)
    issues.push({ field: 'fee_per_contract_roundtrip', message: 'fees cannot be negative' });
  if (scale_up_mult !== null && scale_up_mult < 1.0)
    issues.push({ field: 'scale_up_buffer_mult', message: 'scale_up_buffer_mult must be >= 1.0 (below 1 means scaling on debt)' });
  if (contract_size !== null && contract_size <= 0)
    issues.push({ field: 'contract_size', message: 'contract_size must be > 0' });
  if (fee_sanity !== null && fee_sanity < 1.0)
    issues.push({ field: 'fee_sanity_multiplier', message: 'fee_sanity_multiplier must be >= 1.0 (below 1 means the gate always trips)' });

  if (exit_mode === 'trailing_stop') {
    const trail_distance = num(cfg, 'trail_distance', issues);
    const trail_trigger = num(cfg, 'trail_trigger', issues);
    if (trail_distance !== null && trail_distance <= 0)
      issues.push({ field: 'trail_distance', message: 'trail_distance must be > 0' });
    if (trail_trigger !== null && trail_trigger <= 0)
      issues.push({ field: 'trail_trigger', message: 'trail_trigger must be > 0' });
    if (trail_trigger !== null && sell_px !== null && trail_trigger < sell_px)
      issues.push({ field: 'trail_trigger', message: `trail_trigger (${trail_trigger}) should be >= sell_px (${sell_px})` });
  }

  const reanchor_threshold = num(cfg, 'reanchor_threshold', issues, false);
  if (reanchor_threshold !== null && reanchor_threshold < 0)
    issues.push({ field: 'reanchor_threshold', message: 'reanchor_threshold cannot be negative' });

  return { ok: issues.length === 0, issues };
}
