-- Migration 015: F35 Fast→Slow memory consolidation (2026-05-21)
--
-- Blueprint F35 ("Dual Memory System — Complementary Learning Systems"):
--   Fast memory = raw individual trades (already exists: Redis sorted set
--     + trades table per-row).
--   Slow memory = consolidated patterns/clusters distilled from many trades
--     (NEW — this table). Pattern == (regime, pair_class, direction,
--     outcome_class) bucket; centroid embedding + running summary stats.
--
-- This table is the destination for the Fast→Slow consolidation job
-- (memory.cognitive.consolidation.consolidate_fast_to_slow). Updates are
-- incremental running averages so old regime knowledge is preserved when
-- new trades arrive — the blueprint's "prevent catastrophic forgetting
-- between market regimes" mechanism.

CREATE TABLE IF NOT EXISTS memory_clusters (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cluster_key         TEXT NOT NULL UNIQUE,    -- e.g. "bull|majors|long|win"
    market_regime       TEXT,
    pair_class          TEXT,                    -- 'majors' (BTC,ETH) or 'alts'
    direction           TEXT,
    outcome_class       TEXT,                    -- 'win' | 'loss' | 'breakeven'
    centroid_embedding  vector(512),
    n_trades            INTEGER NOT NULL DEFAULT 0,
    win_rate            NUMERIC(5,2),
    avg_pnl_usdt        NUMERIC(12,4),
    avg_hold_seconds    INTEGER,
    last_updated        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_memory_clusters_key
    ON memory_clusters (cluster_key);
CREATE INDEX IF NOT EXISTS idx_memory_clusters_regime
    ON memory_clusters (market_regime);
CREATE INDEX IF NOT EXISTS idx_memory_clusters_last_updated
    ON memory_clusters (last_updated DESC);
CREATE INDEX IF NOT EXISTS idx_memory_clusters_embed
    ON memory_clusters USING ivfflat (centroid_embedding vector_cosine_ops)
    WITH (lists = 50);
