-- E-11: experiments table
CREATE TABLE IF NOT EXISTS experiments (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    experiment_type     TEXT NOT NULL,
    hypothesis          TEXT,
    evidence            JSONB,
    metrics             JSONB,
    decision            TEXT,
    decision_reason     TEXT,
    market_conditions   JSONB,
    trade_count_at_run  INTEGER,
    outcome             TEXT,
    notes               TEXT
);
