-- Cont. 40 (2026-05-22): multi-label failure subtype, blueprint §F13.
--
-- The original schema constrained exit_reason and failure_type to a tiny enum:
--   exit_reason  IN ('trailing_sl', 'manual', NULL)
--   failure_type IN ('direction', 'signal', NULL)
--
-- In practice the counterfactual logic at execution/paper.py:73-75 collapses
-- to 98.4% "direction" — the Direction Prediction Model has been retraining
-- on a degenerate label corpus. Blueprint §F13 was updated cont. 40 to add a
-- multi-label diagnosis_tags concept that preserves the binary failure_type
-- (as the canonical learning-signal router) and adds structured granularity.

BEGIN;

-- 1. Widen exit_reason to cover real close paths.
ALTER TABLE trades DROP CONSTRAINT IF EXISTS trades_exit_reason_check;
ALTER TABLE trades ADD CONSTRAINT trades_exit_reason_check
    CHECK (exit_reason IN (
        'trailing_sl',
        'manual',
        'manual_close_all',
        'take_profit',
        'regime_flip_exit',
        'funding_burn',
        'liquidation',
        'dca_kill',
        'timeout',
        'hedge_close'
    ) OR exit_reason IS NULL);

-- 2. failure_type stays as the binary router but allow a third bucket for
-- "win" labelling so downstream queries are explicit instead of NULL-checking.
-- Keep NULL allowed so existing rows aren't forced to fill.
ALTER TABLE trades DROP CONSTRAINT IF EXISTS trades_failure_type_check;
ALTER TABLE trades ADD CONSTRAINT trades_failure_type_check
    CHECK (failure_type IN ('direction', 'signal', 'win', 'unclassified')
           OR failure_type IS NULL);

-- 3. New multi-label tag column. JSONB array of strings; empty array == no
-- tags. NOT NULL with default '[]'::jsonb so query writers never have to
-- COALESCE.
ALTER TABLE trades ADD COLUMN IF NOT EXISTS diagnosis_tags JSONB NOT NULL DEFAULT '[]'::jsonb;

-- 4. Index for tag-filtered queries (Direction Model training corpus, dashboard).
CREATE INDEX IF NOT EXISTS idx_trades_diagnosis_tags
    ON trades USING gin (diagnosis_tags);

-- 5. Index for failure_type filter (used by Direction Model retrain pipeline).
CREATE INDEX IF NOT EXISTS idx_trades_failure_type
    ON trades (failure_type)
    WHERE failure_type IS NOT NULL;

COMMIT;
