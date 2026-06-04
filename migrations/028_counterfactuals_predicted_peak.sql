-- Migration 028 — cont. 63 (2026-05-29).
--
-- Closes the gap from /opt/trading-bot/next_impl/predicted_profit_loop.md §3.1:
-- the load-bearing numeric `predicted_peak_profit_pct` column on counterfactuals
-- was specified in §3.1 but never landed in migration 026 (which only added the
-- categorical miss_tag fields). Without this column, F9's actuator cannot scale
-- magnitude proportionally to the size of the missed regret — a 5%-miss decode
-- and a 150%-miss decode produce the same ±0.02 Redis delta.
--
-- Reversible: ADD COLUMN IF NOT EXISTS + a CHECK constraint. No data deletion.

BEGIN;

ALTER TABLE counterfactuals
  ADD COLUMN IF NOT EXISTS predicted_peak_profit_pct numeric(8,4);

-- Sanity bound — peak profit pct in this codebase is unitless % and never
-- realistically exceeds ±200 for the 72h tracking window. NULL is permitted
-- (decode hasn't fired yet, or LLM omitted the field).
ALTER TABLE counterfactuals
  DROP CONSTRAINT IF EXISTS counterfactuals_predicted_peak_profit_pct_range;
ALTER TABLE counterfactuals
  ADD CONSTRAINT counterfactuals_predicted_peak_profit_pct_range CHECK (
    predicted_peak_profit_pct IS NULL OR
    (predicted_peak_profit_pct >= -200.0 AND predicted_peak_profit_pct <= 200.0)
  );

CREATE INDEX IF NOT EXISTS idx_counterfactuals_predicted_peak_profit_pct
  ON counterfactuals (predicted_peak_profit_pct)
  WHERE predicted_peak_profit_pct IS NOT NULL;

COMMIT;
