-- F48 §Idea B — Persist CandleNet TP1/TP2 targets and source magnitudes
-- on the trade row so closed-trade history retains visibility after the
-- ephemeral Redis keys (trade:{id}:tp1 / tp2 / mag1_pct / mag3_pct) are
-- cleared at close time. Blueprint line 1677: "Stored in trade record as
-- tp1_target, tp2_target."
ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS tp1_target  NUMERIC(20,8),
    ADD COLUMN IF NOT EXISTS tp2_target  NUMERIC(20,8),
    ADD COLUMN IF NOT EXISTS mag1_pct    NUMERIC(8,4),
    ADD COLUMN IF NOT EXISTS mag3_pct    NUMERIC(8,4);
