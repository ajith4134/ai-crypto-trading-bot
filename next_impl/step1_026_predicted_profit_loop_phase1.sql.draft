-- DRAFT — not yet applied. Review before copying to migrations/026_predicted_profit_loop_phase1.sql.
-- Phase 1 of the predicted-profit loop: structured cause tags on the F9 decoder output,
-- plus placeholder columns on signals for the Step 2 predictor to fill later.
-- See /opt/trading-bot/next_impl/predicted_profit_loop.md §3.1.
--
-- Reversible: every change is ADD COLUMN IF NOT EXISTS + dropable indexes/constraints.
-- No data deletion. Safe to run on live Postgres while bot is running (ALTER TABLE ADD
-- COLUMN is metadata-only when no default is set).

BEGIN;

-- F9 structured tags. Existing miss_decode_reason (free text) preserved as audit.
ALTER TABLE counterfactuals
  ADD COLUMN IF NOT EXISTS miss_tag             text,
  ADD COLUMN IF NOT EXISTS miss_tag_confidence  numeric(5,4),
  ADD COLUMN IF NOT EXISTS miss_tag_evidence    jsonb;

-- Enum constraint. Vocabulary derived from the existing F9 action vocab in actuator.py
-- (signal_too_weak / memrl_rejected) plus the most common rejection_reason families seen
-- in signals.rejection_reason (regime_mismatch, cooldown, sizer_zero, governance_blocked,
-- liquidity_thin) plus a noise bucket for single-sample no-systemic cases.
ALTER TABLE counterfactuals
  DROP CONSTRAINT IF EXISTS counterfactuals_miss_tag_check;
ALTER TABLE counterfactuals
  ADD CONSTRAINT counterfactuals_miss_tag_check CHECK (
    miss_tag IS NULL OR miss_tag IN (
      'signal_too_weak',
      'regime_mismatch',
      'memrl_rejected',
      'cooldown',
      'sizer_zero',
      'governance_blocked',
      'liquidity_thin',
      'noise'
    )
  );

ALTER TABLE counterfactuals
  DROP CONSTRAINT IF EXISTS counterfactuals_miss_tag_confidence_range;
ALTER TABLE counterfactuals
  ADD CONSTRAINT counterfactuals_miss_tag_confidence_range CHECK (
    miss_tag_confidence IS NULL OR
    (miss_tag_confidence >= 0 AND miss_tag_confidence <= 1)
  );

-- Aggregation index for the regret auto-tuner (later step) — cheap partial index.
CREATE INDEX IF NOT EXISTS idx_counterfactuals_miss_tag
  ON counterfactuals (miss_tag) WHERE miss_tag IS NOT NULL;

-- Step 2 predictor's output surface. Populated NULL on existing rows.
-- Step 1 itself does NOT write to these columns — the columns exist now so Step 2
-- can begin writing as soon as it's deployed without another migration.
ALTER TABLE signals
  ADD COLUMN IF NOT EXISTS predicted_peak_profit_pct numeric(8,4),
  ADD COLUMN IF NOT EXISTS predictor_version         text;

CREATE INDEX IF NOT EXISTS idx_signals_predicted_peak
  ON signals (predicted_peak_profit_pct)
  WHERE predicted_peak_profit_pct IS NOT NULL;

COMMIT;

-- Rollback (if needed):
--   BEGIN;
--   ALTER TABLE signals DROP COLUMN IF EXISTS predicted_peak_profit_pct,
--                       DROP COLUMN IF EXISTS predictor_version;
--   ALTER TABLE counterfactuals
--     DROP CONSTRAINT IF EXISTS counterfactuals_miss_tag_check,
--     DROP CONSTRAINT IF EXISTS counterfactuals_miss_tag_confidence_range,
--     DROP COLUMN IF EXISTS miss_tag,
--     DROP COLUMN IF EXISTS miss_tag_confidence,
--     DROP COLUMN IF EXISTS miss_tag_evidence;
--   DROP INDEX IF EXISTS idx_counterfactuals_miss_tag;
--   DROP INDEX IF EXISTS idx_signals_predicted_peak;
--   COMMIT;
