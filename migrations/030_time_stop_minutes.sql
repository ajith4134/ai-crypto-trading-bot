-- 030_time_stop_minutes.sql
-- cont. 66 (2026-06-01) — Hard minute-granularity time-stop (scalping/HFT regime).
-- Extends the trades_exit_reason_check allow-list with one new reason:
--   time_stop_minutes — fires regardless of PnL/TP1 once bot:max_hold_minutes elapses
-- Source: risk/manager.py:monitor_trailing_sl minute time-stop block.

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
        'deep_hedging_exit',
        -- cont. 62b dead-trade time-exit
        'dead_trade_time_exit',
        'dead_trade_force_close_max_age',
        -- cont. 66 hard minute time-stop
        'time_stop_minutes'
    ]::text[]));
