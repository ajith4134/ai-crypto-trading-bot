-- Migration 021: Backfill dca_rules for seed strategies (2026-05-23)
--
-- The two active seed strategies (stage1_ofi_momentum, stage2_sentiment_ofi)
-- were created without dca_rules populated. F8 router resolved their DCA to
-- __NULL__ in Redis, forcing the bot to fall back to config defaults — and
-- when the brain attributed a trade to one of these IDs, the per-strategy
-- override path was effectively dead. Only the experimental mean_reversion
-- strategy (1927356f) had complete router fields.
--
-- This backfill restores parity. Round-1/Round-2 values pulled from the
-- config defaults (-20/-40) so behaviour matches the pre-router default
-- exactly; the bandit will evolve them once it has signal.
--
-- Idempotent: only updates where dca_rules IS NULL.

UPDATE strategies SET dca_rules = jsonb_build_object(
    'round_1_pct', -20.0, 'round_2_pct', -40.0
) WHERE name = 'stage1_ofi_momentum' AND status = 'active' AND dca_rules IS NULL;

UPDATE strategies SET dca_rules = jsonb_build_object(
    'round_1_pct', -20.0, 'round_2_pct', -40.0
) WHERE name = 'stage2_sentiment_ofi' AND status = 'active' AND dca_rules IS NULL;
