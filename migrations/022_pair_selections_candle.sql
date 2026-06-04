-- Migration 022: F50a — add candle_setup_score column to pair_selections (2026-05-26)
--
-- F50 Candle-First Brain §F50a: the scanner now ranks pairs partly by a
-- candle-setup score read from CandleNet 15m+1h forecasts. The F10 weight
-- learner correlates each scanner sub-score with realised forward PnL to
-- update the criterion weights, so it needs a column for the new score.
--
-- Default 50 (neutral) means existing rows written before F50a — and any
-- future row where CandleNet forecasts were not yet published — don't bias
-- the correlation calculation either way.

ALTER TABLE pair_selections
    ADD COLUMN IF NOT EXISTS candle_setup_score NUMERIC(6,2) NOT NULL DEFAULT 50.0;
