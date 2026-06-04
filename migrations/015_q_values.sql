-- Migration 015: F35 MemRL — real Q-learning table (2026-05-20)
--
-- Blueprint Feature 35 (Section 8.7 / arXiv:2601.03192) specifies "Phase 2 —
-- Quality Re-ranking: Re-rank filtered memories by Q-value — which of these
-- similar situations actually led to profitable decisions?"
--
-- Until now memory/cognitive/memrl.py:_phase2_quality_rerank simply sorted
-- candidate trades by raw net_pnl_usdt DESC. That's not a Q-value — it's a
-- single observed return for that exact trade. A high-PnL outlier in a
-- generally-losing setup would still rank first.
--
-- This table stores learned Q-values per (state_bucket, action) updated
-- via constant-α Monte Carlo from every closed paper trade:
--     Q(s,a) ← Q(s,a) + α · (r - Q(s,a))    α = 0.1
-- where r = clip(net_pnl_usdt / 50, -1, +1)
--
-- See memory/cognitive/q_learning.py for the bucketing scheme + update logic
-- and memory/cognitive/memrl.py:_phase2_quality_rerank for the consumer side.

CREATE TABLE IF NOT EXISTS q_values (
    state_bucket  TEXT             NOT NULL,
    action        TEXT             NOT NULL,
    q_value       DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    n_samples     INTEGER          NOT NULL DEFAULT 0,
    last_updated  TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    PRIMARY KEY (state_bucket, action)
);

-- Index for retrieval-side JOINs from _phase2_quality_rerank — looks up Q for
-- a batch of candidate trades' (state_bucket, action) pairs in one query.
CREATE INDEX IF NOT EXISTS idx_q_values_bucket
    ON q_values (state_bucket);
