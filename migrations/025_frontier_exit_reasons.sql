-- 025_frontier_exit_reasons.sql
-- cont. 60 (2026-05-28) — Add frontier exit_reason values for the 13
-- new SOTA exit techniques. Each can fire a force_close with a unique
-- reason string. Keeping legacy values + adding new.

ALTER TABLE trades DROP CONSTRAINT IF EXISTS trades_exit_reason_check;

ALTER TABLE trades ADD CONSTRAINT trades_exit_reason_check
    CHECK (exit_reason IS NULL OR exit_reason = ANY (ARRAY[
        -- legacy (kept for compat)
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
        -- cont. 59
        'mtf_15m_reversal_confirmed',
        'candlenet_tp1_partial',
        'candlenet_tp2',
        'time_barrier_max_hold',
        -- cont. 60 frontier exits
        'filtered_obi_severe_flip',
        'funding_premium_crowded_long',
        'funding_premium_crowded_short',
        'bocpd_changepoint_detected',
        'gnn_contagion_systemic_risk',
        'llm_council_exit',
        'ppo_policy_exit',
        'deep_hedging_exit'
    ]::text[]));
