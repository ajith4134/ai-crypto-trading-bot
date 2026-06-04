-- Migration 017: Backfill entry_overrides for the 9 pre-cont-11 experimentals (2026-05-21)
--
-- The cont. 11 fix made F36's LLM emit typed entry_overrides for NEW research
-- strategies, but the 9 existing experimentals (from cont. 2's research run)
-- had NULL entry_overrides → still used global defaults at entry.
--
-- This migration hand-classifies each strategy from its entry_conditions text
-- and assigns deliberately varied typed overrides so the F8 UCB1 bandit has
-- real variance across the experimental pool to rank by.
--
-- Idempotent: only updates rows where entry_overrides IS NULL, so re-running
-- after a real F36 LLM-driven override won't overwrite it.
--
-- Rule 4: these values are hand-picked from text inspection, NOT LLM-derived.
-- An LLM-driven backfill (re-run F36 per strategy) would be more authentic
-- but burns provider tokens and varies on retry. Hand-picking gives
-- deterministic, audited differentiation across the pool.

-- 1. MA crossover + oversold RSI + Bollinger squeeze → mean-reversion bottom-fish.
--    Tolerate turbulent (oversold often produced by turbulence). Mid strength.
UPDATE strategies SET entry_overrides = jsonb_build_object(
    'min_signal_strength', 30,
    'turbulence_cap',      5.0
) WHERE name = 'research_1779280824' AND entry_overrides IS NULL;

-- 2. Upper BB touch + MACD bullish + RSI<70 → breakout. Skip turbulent.
UPDATE strategies SET entry_overrides = jsonb_build_object(
    'min_signal_strength', 35,
    'regime_whitelist',    jsonb_build_array('bull', 'bear')
) WHERE name = 'research_1779283310' AND entry_overrides IS NULL;

-- 3. Vague "trend alignment + momentum + volatility filter" → unspecific,
--    requires strict strength to compensate. All regimes.
UPDATE strategies SET entry_overrides = jsonb_build_object(
    'min_signal_strength', 50
) WHERE name = 'research_1779289518' AND entry_overrides IS NULL;

-- 4. 20-MA breakout + volume + ATR rising → momentum breakout. Stricter, bull/bear.
UPDATE strategies SET entry_overrides = jsonb_build_object(
    'min_signal_strength', 45,
    'regime_whitelist',    jsonb_build_array('bull', 'bear')
) WHERE name = 'research_1779293872' AND entry_overrides IS NULL;

-- 5. Above 50MA + RSI<70 + BB widening → momentum-trend. Bull/bear.
UPDATE strategies SET entry_overrides = jsonb_build_object(
    'min_signal_strength', 40,
    'regime_whitelist',    jsonb_build_array('bull', 'bear')
) WHERE name = 'research_1779293875' AND entry_overrides IS NULL;

-- 6. RSI oversold + above 200MA + volume + close above BB low → mean-reversion.
--    All regimes (volatility creates entries). Lower threshold.
UPDATE strategies SET entry_overrides = jsonb_build_object(
    'min_signal_strength', 28,
    'turbulence_cap',      6.0
) WHERE name = 'research_1779293893' AND entry_overrides IS NULL;

-- 7. Two MAs + RSI bullish divergence → simple trend follower. Default-ish.
UPDATE strategies SET entry_overrides = jsonb_build_object(
    'min_signal_strength', 33
) WHERE name = 'research_1779300580' AND entry_overrides IS NULL;

-- 8. RSI mid-zone + volume + key-level break → cautious momentum. All regimes.
UPDATE strategies SET entry_overrides = jsonb_build_object(
    'min_signal_strength', 42,
    'turbulence_cap',      3.5
) WHERE name = 'research_1779300581' AND entry_overrides IS NULL;

-- 9. 20-period high break + RSI<70 + MACD on 4h → trend-breakout, longer TF.
--    Stricter strength (4h confirmation is rare = high-conviction). Bull/bear.
UPDATE strategies SET entry_overrides = jsonb_build_object(
    'min_signal_strength', 48,
    'regime_whitelist',    jsonb_build_array('bull', 'bear')
) WHERE name = 'research_1779300582' AND entry_overrides IS NULL;
