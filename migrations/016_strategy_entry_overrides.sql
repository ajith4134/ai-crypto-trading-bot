-- Migration 016: F8 Strategy Router — typed per-strategy entry overrides (2026-05-21)
--
-- Companion to the cont. 8 DCA router. The router now also routes the
-- signal-acceptance gate (accept_or_reject) when the strategy has typed
-- overrides. Each key is optional — missing keys mean "use the global
-- default / GA-evolved value".
--
-- Supported keys (more can be added without migration):
--   min_signal_strength  : float, overrides accept_or_reject min_strength gate
--   turbulence_cap       : float, overrides accept_or_reject turbulence ceiling
--   regime_whitelist     : list[str], reject signal if market_regime not in list
--
-- The 2 active strategies (stage1_ofi_momentum, stage2_sentiment_ofi) are
-- backfilled with deliberately different overrides so the bandit has real
-- variation to rank. Experimental strategies (research_*) stay NULL — they
-- already differ via dca_rules (cont. 8) and would need F36 to emit typed
-- entry parameters too, which is a separate enhancement.

ALTER TABLE strategies
    ADD COLUMN IF NOT EXISTS entry_overrides JSONB;

-- Backfill: stage1_ofi_momentum — momentum-only, skip turbulent regime
UPDATE strategies SET entry_overrides = jsonb_build_object(
    'min_signal_strength', 25,
    'regime_whitelist',    jsonb_build_array('bull', 'bear')
) WHERE name = 'stage1_ofi_momentum' AND status = 'active'
  AND entry_overrides IS NULL;

-- Backfill: stage2_sentiment_ofi — stricter strength gate, higher turb tolerance
UPDATE strategies SET entry_overrides = jsonb_build_object(
    'min_signal_strength', 40,
    'turbulence_cap',      4.0
) WHERE name = 'stage2_sentiment_ofi' AND status = 'active'
  AND entry_overrides IS NULL;
