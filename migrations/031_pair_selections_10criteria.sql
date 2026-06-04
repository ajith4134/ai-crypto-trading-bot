-- cont. 69 (2026-06-02): extend the F10 criteria-weight learner to all 10
-- selection criteria. The scanner already computes adx_trend/open_interest/rsi/
-- ema_distance sub-scores (scanner/main.py sub_scores_by_sym) but pair_selections
-- only had columns for the original 6, so record_pair_selection dropped them and
-- the learner (ml/criteria_weights.py) could never correlate them with PnL → it
-- kept rewriting brain:feature_weights with only 6 keys, zeroing rsi/ema/adx/oi
-- weights in scanner/main.py compute_composite. These 4 columns close that gap.
ALTER TABLE pair_selections
  ADD COLUMN IF NOT EXISTS adx_trend_score     numeric DEFAULT 50,
  ADD COLUMN IF NOT EXISTS open_interest_score numeric DEFAULT 50,
  ADD COLUMN IF NOT EXISTS rsi_score           numeric DEFAULT 50,
  ADD COLUMN IF NOT EXISTS ema_distance_score  numeric DEFAULT 50;
