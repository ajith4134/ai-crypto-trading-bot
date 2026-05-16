-- E-09: feature_governance table
CREATE TABLE IF NOT EXISTS feature_governance (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    feature_id                  TEXT NOT NULL UNIQUE,
    feature_name                TEXT NOT NULL,
    status                      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'probation', 'turned_off', 'dormant')),
    current_weight              NUMERIC(5,4) DEFAULT 1.0,
    activation_phase            INTEGER NOT NULL DEFAULT 0,
    failure_mode                TEXT,
    decode_reason               TEXT,
    evidence                    JSONB,
    flagged_at                  TIMESTAMPTZ,
    turned_off_at               TIMESTAMPTZ,
    next_reeval_at_trades       INTEGER,
    probation_trade_start       INTEGER,
    confirmed_at                TIMESTAMPTZ,
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
