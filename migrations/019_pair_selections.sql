-- Migration 019: F10 Brain-learned criteria weights — pair_selections audit log (2026-05-21)
--
-- Blueprint Feature 10 ("Brain-Learned Criteria Weights"):
--   "The Brain does not use fixed weights for the scoring criteria. It experiments
--    with different weight combinations, measures which combination produces the
--    most profitable pair selection, and continuously optimises the weights."
--
-- The scanner already READS weights from Redis key BRAIN_FEATURE_WEIGHTS with
-- config fallback. Cont. 18 adds the WRITER side: this table stores the per-pair
-- sub-scores at scan time so the learner can later JOIN against realized PnL on
-- those pairs and update the weights to correlate with outcomes.

CREATE TABLE IF NOT EXISTS pair_selections (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol             TEXT NOT NULL,
    scan_ts            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    volume_score       NUMERIC(6,2) NOT NULL,
    volatility_score   NUMERIC(6,2) NOT NULL,
    spread_score       NUMERIC(6,2) NOT NULL,
    winrate_score      NUMERIC(6,2) NOT NULL,
    pnl_score          NUMERIC(6,2) NOT NULL,
    composite_score    NUMERIC(6,2) NOT NULL,
    selected_as_active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_pair_selections_symbol_ts
    ON pair_selections (symbol, scan_ts DESC);
CREATE INDEX IF NOT EXISTS idx_pair_selections_scan_ts
    ON pair_selections (scan_ts DESC);
