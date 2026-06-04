-- DRAFT — not yet applied. Review before copying to migrations/027_predict_all_schema.sql.
--
-- Phase A of the predict-all-before-open architecture:
--   1. Collapse tp1/tp2 → single tp (backfilled; old columns kept for 1 release).
--   2. New `predictions` table — relational store of every prediction made,
--      regardless of whether a trade materialized. Enables prediction-hit-rate
--      measurement (predictions_made vs trades_opened_from_predictions).
--   3. New `pattern_effectiveness_registry` — per-(cluster × regime × direction)
--      stats. Updated by a celery beat task after each trade closes.
--   4. New tracking columns on `trades` — predicted vs actual comparisons.
--
-- See /opt/trading-bot/next_impl/predict_all_before_open.md §4 (Phase A).
-- See memory: [[project_predict_all_before_open]].
--
-- Reversible: all changes are ADD COLUMN IF NOT EXISTS / CREATE TABLE IF NOT EXISTS.
-- Migration 028 (separate, after 1 release): DROP COLUMN tp1, tp1_fired, tp1_target, tp2, tp2_target.

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Single TP columns on trades (backfilled from tp1).
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE trades
  ADD COLUMN IF NOT EXISTS tp        numeric(18,8),
  ADD COLUMN IF NOT EXISTS tp_fired  boolean DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS tp_target numeric(18,8);

-- Backfill from tp1. tp2 is not collapsed in — under the predict-all-with-
-- trailing-runner design (user pick 2026-05-29), tp1 IS the single TP target;
-- tp2 was the runner level which now falls to the existing profit-lock ratchet.
UPDATE trades
   SET tp = tp1, tp_fired = tp1_fired, tp_target = tp1_target
 WHERE tp IS NULL AND tp1 IS NOT NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Prediction-vs-actual tracking on trades.
-- ─────────────────────────────────────────────────────────────────────────────
-- direction_confidence (existing) becomes the raw model probability;
-- conformal_confidence (new) becomes the calibrated coverage-bound value.
-- hold_time_seconds (existing) IS actual_hold_seconds;
-- predicted_hold_seconds (new) is the comparison target.

ALTER TABLE trades
  ADD COLUMN IF NOT EXISTS prediction_id          uuid,
  ADD COLUMN IF NOT EXISTS pattern_cluster_id     integer,
  ADD COLUMN IF NOT EXISTS predicted_direction    text,
  ADD COLUMN IF NOT EXISTS predicted_entry        numeric(18,8),
  ADD COLUMN IF NOT EXISTS predicted_sl           numeric(18,8),
  ADD COLUMN IF NOT EXISTS predicted_tp           numeric(18,8),
  ADD COLUMN IF NOT EXISTS predicted_hold_seconds integer,
  ADD COLUMN IF NOT EXISTS conformal_confidence   numeric(5,4),
  ADD COLUMN IF NOT EXISTS predicted_rr           numeric(8,4),
  ADD COLUMN IF NOT EXISTS actual_rr              numeric(8,4),
  ADD COLUMN IF NOT EXISTS prediction_success     boolean;

ALTER TABLE trades
  DROP CONSTRAINT IF EXISTS trades_predicted_direction_check;
ALTER TABLE trades
  ADD CONSTRAINT trades_predicted_direction_check CHECK (
    predicted_direction IS NULL OR predicted_direction IN ('long','short')
  );

ALTER TABLE trades
  DROP CONSTRAINT IF EXISTS trades_conformal_confidence_range;
ALTER TABLE trades
  ADD CONSTRAINT trades_conformal_confidence_range CHECK (
    conformal_confidence IS NULL OR
    (conformal_confidence >= 0 AND conformal_confidence <= 1)
  );

CREATE INDEX IF NOT EXISTS idx_trades_prediction_id
  ON trades (prediction_id) WHERE prediction_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_trades_pattern_cluster_regime
  ON trades (pattern_cluster_id, market_regime) WHERE pattern_cluster_id IS NOT NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. predictions table — relational store of every prediction made.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS predictions (
  id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  symbol                 text NOT NULL,
  scan_cycle_id          uuid,                 -- nullable; ties to scanner cycle
  tf_set                 text NOT NULL,        -- e.g. '1m,5m,15m,1h'
  predicted_direction    text NOT NULL CHECK (predicted_direction IN ('long','short')),
  predicted_entry        numeric(18,8) NOT NULL,
  predicted_sl           numeric(18,8) NOT NULL,
  predicted_tp           numeric(18,8) NOT NULL,
  predicted_hold_seconds integer NOT NULL,
  conformal_confidence   numeric(5,4) NOT NULL CHECK (conformal_confidence BETWEEN 0 AND 1),
  predicted_rr_p25       numeric(8,4),
  predicted_rr_p50       numeric(8,4) NOT NULL,
  predicted_rr_p75       numeric(8,4),
  pattern_cluster_id     integer,
  market_regime          text,
  model_version          text NOT NULL,
  created_at             timestamptz NOT NULL DEFAULT NOW(),
  expires_at             timestamptz NOT NULL,  -- predictions are stale after this
  -- Outcome columns — populated when the prediction is consumed.
  trade_id               uuid REFERENCES trades(id),
  consumed_at            timestamptz,           -- when signal-side picked this up
  rejected_reason        text                   -- e.g. 'low_confidence', 'low_rr', 'no_signal'
);

CREATE INDEX IF NOT EXISTS idx_predictions_symbol_created
  ON predictions (symbol, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_predictions_trade_id
  ON predictions (trade_id) WHERE trade_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_predictions_cluster_regime
  ON predictions (pattern_cluster_id, market_regime)
  WHERE pattern_cluster_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_predictions_expires_at
  ON predictions (expires_at);  -- cleanup job will use this

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. pattern_effectiveness_registry — per-(cluster × regime × direction) stats.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS pattern_effectiveness_registry (
  pattern_cluster_id integer NOT NULL,
  market_regime      text NOT NULL,
  direction          text NOT NULL CHECK (direction IN ('long','short')),
  n_trades           integer NOT NULL DEFAULT 0,
  n_wins             integer NOT NULL DEFAULT 0,
  avg_rr             numeric(8,4),
  avg_hold_seconds   integer,
  net_pnl_usdt       numeric(14,4) NOT NULL DEFAULT 0,
  -- Calibration tracking — populated by drift monitor.
  calibration_ece    numeric(6,4),            -- expected calibration error
  n_samples_for_ece  integer NOT NULL DEFAULT 0,
  drift_flagged      boolean NOT NULL DEFAULT FALSE,
  last_updated       timestamptz NOT NULL DEFAULT NOW(),
  PRIMARY KEY (pattern_cluster_id, market_regime, direction)
);

CREATE INDEX IF NOT EXISTS idx_per_winrate
  ON pattern_effectiveness_registry ((n_wins::float / NULLIF(n_trades,0)))
  WHERE n_trades >= 10;
CREATE INDEX IF NOT EXISTS idx_per_drift_flagged
  ON pattern_effectiveness_registry (drift_flagged) WHERE drift_flagged;

-- Convenience view — top-performing patterns with at least 20 trades.
CREATE OR REPLACE VIEW v_pattern_effectiveness_ranked AS
  SELECT
    pattern_cluster_id,
    market_regime,
    direction,
    n_trades,
    n_wins,
    ROUND(100.0 * n_wins::numeric / NULLIF(n_trades,0), 2) AS win_rate_pct,
    avg_rr,
    avg_hold_seconds,
    net_pnl_usdt,
    calibration_ece,
    drift_flagged
  FROM pattern_effectiveness_registry
  WHERE n_trades >= 20
  ORDER BY net_pnl_usdt DESC;

COMMIT;

-- ─────────────────────────────────────────────────────────────────────────────
-- ROLLBACK (apply manually if needed):
--   BEGIN;
--   DROP VIEW IF EXISTS v_pattern_effectiveness_ranked;
--   DROP TABLE IF EXISTS pattern_effectiveness_registry;
--   DROP TABLE IF EXISTS predictions;
--   DROP INDEX IF EXISTS idx_trades_prediction_id;
--   DROP INDEX IF EXISTS idx_trades_pattern_cluster_regime;
--   ALTER TABLE trades
--     DROP CONSTRAINT IF EXISTS trades_predicted_direction_check,
--     DROP CONSTRAINT IF EXISTS trades_conformal_confidence_range,
--     DROP COLUMN IF EXISTS prediction_id,
--     DROP COLUMN IF EXISTS pattern_cluster_id,
--     DROP COLUMN IF EXISTS predicted_direction,
--     DROP COLUMN IF EXISTS predicted_entry,
--     DROP COLUMN IF EXISTS predicted_sl,
--     DROP COLUMN IF EXISTS predicted_tp,
--     DROP COLUMN IF EXISTS predicted_hold_seconds,
--     DROP COLUMN IF EXISTS conformal_confidence,
--     DROP COLUMN IF EXISTS predicted_rr,
--     DROP COLUMN IF EXISTS actual_rr,
--     DROP COLUMN IF EXISTS prediction_success,
--     DROP COLUMN IF EXISTS tp,
--     DROP COLUMN IF EXISTS tp_fired,
--     DROP COLUMN IF EXISTS tp_target;
--   COMMIT;
