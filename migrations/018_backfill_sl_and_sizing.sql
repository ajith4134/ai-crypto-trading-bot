-- Migration 018: F8 Router — backfill trailing_sl_params + position_sizing_rules (2026-05-21)
--
-- Companion to migrations 016 (entry_overrides) and 017 (experimental backfill).
-- The router added per-strategy SL + capital sizing in cont. 13; this migration
-- populates the two existing JSONB columns so every strategy has deliberately
-- varied values for the bandit to rank.
--
-- Idempotent: only updates where the target column IS NULL. Re-running won't
-- overwrite later LLM-emitted values when F36 starts emitting these too.
--
-- Schemas:
--   trailing_sl_params:
--     initial_atr_mult   : ATR multiplier for initial SL (default 2.5)
--     initial_min_pct    : minimum SL distance as fraction of mark (default 0.03)
--     trailing_dist_pct  : trailing distance after profit (default 0.02)
--
--   position_sizing_rules:
--     capital_pct_mult   : multiplier on default capital, clipped to [0.25, 2.0]
--                          at the consumer. 1.0 = no change.

-- ── Active strategies ────────────────────────────────────────────────────
-- stage1_ofi_momentum — momentum, give winners room to run
UPDATE strategies SET trailing_sl_params = jsonb_build_object(
    'initial_atr_mult', 3.0, 'initial_min_pct', 0.04, 'trailing_dist_pct', 0.025
) WHERE name = 'stage1_ofi_momentum' AND trailing_sl_params IS NULL;
UPDATE strategies SET position_sizing_rules = jsonb_build_object(
    'capital_pct_mult', 1.0
) WHERE name = 'stage1_ofi_momentum' AND position_sizing_rules IS NULL;

-- stage2_sentiment_ofi — sentiment-driven, default room, full capital
UPDATE strategies SET trailing_sl_params = jsonb_build_object(
    'initial_atr_mult', 2.5, 'initial_min_pct', 0.03, 'trailing_dist_pct', 0.02
) WHERE name = 'stage2_sentiment_ofi' AND trailing_sl_params IS NULL;
UPDATE strategies SET position_sizing_rules = jsonb_build_object(
    'capital_pct_mult', 1.0
) WHERE name = 'stage2_sentiment_ofi' AND position_sizing_rules IS NULL;

-- ── Experimental strategies ──────────────────────────────────────────────
-- Spread across the {tight-SL+small-capital, default, wide-SL+larger-capital}
-- band so the bandit sees full per-strategy variance.

-- research_1779280824 (mean-reversion bottom-fish) — tight trailing, half capital
UPDATE strategies SET trailing_sl_params = jsonb_build_object(
    'initial_atr_mult', 2.0, 'initial_min_pct', 0.025, 'trailing_dist_pct', 0.015
) WHERE name = 'research_1779280824' AND trailing_sl_params IS NULL;
UPDATE strategies SET position_sizing_rules = jsonb_build_object('capital_pct_mult', 0.5)
  WHERE name = 'research_1779280824' AND position_sizing_rules IS NULL;

-- research_1779283310 (BB breakout) — wide trailing (let breakouts run), 0.75x capital
UPDATE strategies SET trailing_sl_params = jsonb_build_object(
    'initial_atr_mult', 3.5, 'initial_min_pct', 0.04, 'trailing_dist_pct', 0.03
) WHERE name = 'research_1779283310' AND trailing_sl_params IS NULL;
UPDATE strategies SET position_sizing_rules = jsonb_build_object('capital_pct_mult', 0.75)
  WHERE name = 'research_1779283310' AND position_sizing_rules IS NULL;

-- research_1779289518 (vague trend) — defaults, half capital (unproven)
UPDATE strategies SET trailing_sl_params = jsonb_build_object(
    'initial_atr_mult', 2.5, 'initial_min_pct', 0.03, 'trailing_dist_pct', 0.02
) WHERE name = 'research_1779289518' AND trailing_sl_params IS NULL;
UPDATE strategies SET position_sizing_rules = jsonb_build_object('capital_pct_mult', 0.5)
  WHERE name = 'research_1779289518' AND position_sizing_rules IS NULL;

-- research_1779293872 (MA breakout + volume) — wide trailing, 0.8x capital
UPDATE strategies SET trailing_sl_params = jsonb_build_object(
    'initial_atr_mult', 3.0, 'initial_min_pct', 0.035, 'trailing_dist_pct', 0.025
) WHERE name = 'research_1779293872' AND trailing_sl_params IS NULL;
UPDATE strategies SET position_sizing_rules = jsonb_build_object('capital_pct_mult', 0.8)
  WHERE name = 'research_1779293872' AND position_sizing_rules IS NULL;

-- research_1779293875 (above-50MA trend) — slightly wider, 0.75x capital
UPDATE strategies SET trailing_sl_params = jsonb_build_object(
    'initial_atr_mult', 2.8, 'initial_min_pct', 0.03, 'trailing_dist_pct', 0.022
) WHERE name = 'research_1779293875' AND trailing_sl_params IS NULL;
UPDATE strategies SET position_sizing_rules = jsonb_build_object('capital_pct_mult', 0.75)
  WHERE name = 'research_1779293875' AND position_sizing_rules IS NULL;

-- research_1779293893 (RSI oversold bottom-fish) — tight stops, 0.5x capital
UPDATE strategies SET trailing_sl_params = jsonb_build_object(
    'initial_atr_mult', 1.8, 'initial_min_pct', 0.022, 'trailing_dist_pct', 0.013
) WHERE name = 'research_1779293893' AND trailing_sl_params IS NULL;
UPDATE strategies SET position_sizing_rules = jsonb_build_object('capital_pct_mult', 0.5)
  WHERE name = 'research_1779293893' AND position_sizing_rules IS NULL;

-- research_1779300580 (two MA + bullish RSI divergence) — defaults, 0.6x capital
UPDATE strategies SET trailing_sl_params = jsonb_build_object(
    'initial_atr_mult', 2.5, 'initial_min_pct', 0.03, 'trailing_dist_pct', 0.02
) WHERE name = 'research_1779300580' AND trailing_sl_params IS NULL;
UPDATE strategies SET position_sizing_rules = jsonb_build_object('capital_pct_mult', 0.6)
  WHERE name = 'research_1779300580' AND position_sizing_rules IS NULL;

-- research_1779300581 (cautious momentum, mid-RSI) — defaults, 0.75x capital
UPDATE strategies SET trailing_sl_params = jsonb_build_object(
    'initial_atr_mult', 2.5, 'initial_min_pct', 0.03, 'trailing_dist_pct', 0.02
) WHERE name = 'research_1779300581' AND trailing_sl_params IS NULL;
UPDATE strategies SET position_sizing_rules = jsonb_build_object('capital_pct_mult', 0.75)
  WHERE name = 'research_1779300581' AND position_sizing_rules IS NULL;

-- research_1779300582 (4h breakout, high conviction) — wide stops, 1.2x capital
UPDATE strategies SET trailing_sl_params = jsonb_build_object(
    'initial_atr_mult', 3.5, 'initial_min_pct', 0.045, 'trailing_dist_pct', 0.03
) WHERE name = 'research_1779300582' AND trailing_sl_params IS NULL;
UPDATE strategies SET position_sizing_rules = jsonb_build_object('capital_pct_mult', 1.2)
  WHERE name = 'research_1779300582' AND position_sizing_rules IS NULL;
