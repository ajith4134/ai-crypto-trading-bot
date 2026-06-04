-- Migration 014: F44 Directional Hedge relationship columns (2026-05-20)
--
-- Blueprint F44: when a losing trade's DCA has fired AND price continues
-- another -5% past the DCA trigger, the brain opens a counter-position on the
-- same symbol to profit from confirmed trend continuation. Tracking which
-- trades are hedges of which requires two columns:
--
--   hedge_of_trade_id  — FK to the original (parent) losing trade
--   is_hedge_active    — flag on the PARENT trade so we don't open multiple
--                        hedges for the same losing position
--
-- See risk/hedge.py for the full trigger logic.

ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS hedge_of_trade_id UUID REFERENCES trades(id),
    ADD COLUMN IF NOT EXISTS is_hedge_active   BOOLEAN NOT NULL DEFAULT FALSE;

-- Partial index so the brain's per-tick "do I have a hedge yet?" lookup is fast.
CREATE INDEX IF NOT EXISTS idx_trades_hedge_active
    ON trades (id)
    WHERE is_hedge_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_trades_hedge_of
    ON trades (hedge_of_trade_id)
    WHERE hedge_of_trade_id IS NOT NULL;
