-- Migration 014: F12 Mismatch Decoder table (2026-05-21)
--
-- Blueprint F12 ("Mismatch Decoder — When Predictions Are Wrong"):
-- pair every high-potential losing trade with a contemporaneous
-- low-potential winning trade so the LLM can decode the cause of the
-- mispricing. Output is stored per (loser, winner) pair.
--
-- Companion to F9: the counterfactuals table already has miss_decode_reason
-- and miss_decoded columns; this migration only adds the F12-side table.

CREATE TABLE IF NOT EXISTS mismatches (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    loser_trade_id       UUID NOT NULL REFERENCES trades(id),
    winner_trade_id      UUID NOT NULL REFERENCES trades(id),
    period_start         TIMESTAMPTZ NOT NULL,
    period_end           TIMESTAMPTZ NOT NULL,
    loser_potential      NUMERIC(5,2),
    winner_potential     NUMERIC(5,2),
    loser_pnl_usdt       NUMERIC(12,4),
    winner_pnl_usdt      NUMERIC(12,4),
    decode_reason        TEXT,
    decoded_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (loser_trade_id, winner_trade_id)
);

CREATE INDEX IF NOT EXISTS idx_mismatches_decoded_at ON mismatches (decoded_at DESC);
CREATE INDEX IF NOT EXISTS idx_mismatches_period      ON mismatches (period_start, period_end);
