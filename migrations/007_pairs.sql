-- E-08: pairs table
CREATE TABLE IF NOT EXISTS pairs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol              TEXT NOT NULL UNIQUE,
    is_active           BOOLEAN NOT NULL DEFAULT FALSE,
    composite_score     NUMERIC(5,2),
    volume_score        NUMERIC(5,2),
    volatility_score    NUMERIC(5,2),
    spread_score        NUMERIC(5,2),
    win_rate_score      NUMERIC(5,2),
    pnl_score           NUMERIC(5,2),
    total_net_pnl_usdt  NUMERIC(12,4) DEFAULT 0,
    last_scanned_at     TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pairs_symbol    ON pairs(symbol);
CREATE INDEX IF NOT EXISTS idx_pairs_is_active ON pairs(is_active);
