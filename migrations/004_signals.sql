-- E-05: signals table
CREATE TABLE IF NOT EXISTS signals (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    generated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    pair                TEXT NOT NULL,
    direction           TEXT NOT NULL CHECK (direction IN ('long', 'short')),
    timeframe           TEXT,
    strategy_id         UUID REFERENCES strategies(id),
    accepted            BOOLEAN NOT NULL,
    rejection_reason    TEXT,
    trade_id            UUID REFERENCES trades(id),
    feature_vector      JSONB,
    signal_strength     NUMERIC(5,2),
    market_regime       TEXT,
    brain_stage         INTEGER NOT NULL
);
