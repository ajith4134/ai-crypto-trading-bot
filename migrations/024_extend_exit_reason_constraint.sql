-- 024_extend_exit_reason_constraint.sql
-- cont. 59 (2026-05-28) — Add new exit_reason values used by code paths
-- that have been live for multiple sessions but never passed DB validation:
--   * mtf_15m_reversal_confirmed   (cont. 53, Path F — 15m MTF reversal force-close)
--   * candlenet_tp1_partial        (cont. 47, F48 §Idea B — TP1 partial close)
--   * candlenet_tp2                (cont. 47, F48 §Idea B — TP2 full close)
--   * time_barrier_max_hold        (cont. 59 — Lopez de Prado Triple Barrier)
--
-- Symptom this fixes: trades stuck "in-progress" because close_trade INSERTs
-- failed the CHECK constraint; signal generation eventually halted because
-- the slot-recovery never completed for those phantom-open trades.

ALTER TABLE trades DROP CONSTRAINT IF EXISTS trades_exit_reason_check;

ALTER TABLE trades ADD CONSTRAINT trades_exit_reason_check
    CHECK (exit_reason IS NULL OR exit_reason = ANY (ARRAY[
        'trailing_sl',
        'manual',
        'manual_close_all',
        'take_profit',
        'regime_flip_exit',
        'funding_burn',
        'liquidation',
        'dca_kill',
        'timeout',
        'hedge_close',
        'mtf_15m_reversal_confirmed',
        'candlenet_tp1_partial',
        'candlenet_tp2',
        'time_barrier_max_hold'
    ]::text[]));
