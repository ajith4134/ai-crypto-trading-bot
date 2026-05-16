-- E-03: trades table with pgvector embedding column
CREATE TABLE IF NOT EXISTS trades (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pair                    TEXT NOT NULL,
    direction               TEXT NOT NULL CHECK (direction IN ('long', 'short')),
    strategy_id             UUID REFERENCES strategies(id),
    brain_stage             INTEGER NOT NULL,
    is_paper                BOOLEAN NOT NULL DEFAULT TRUE,
    status                  TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),

    -- Entry
    entry_price             NUMERIC(20,8) NOT NULL,
    entry_time              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    quantity                NUMERIC(20,8) NOT NULL,
    capital_usdt            NUMERIC(12,4) NOT NULL,
    leverage                INTEGER NOT NULL,
    timeframe               TEXT,
    market_regime           TEXT,

    -- Trailing SL
    trailing_sl_level       NUMERIC(20,8),

    -- DCA
    dca_status              JSONB DEFAULT '{"round_1_triggered": false, "round_2_triggered": false}'::jsonb,
    dca1_price              NUMERIC(20,8),
    dca2_price              NUMERIC(20,8),
    average_entry           NUMERIC(20,8),

    -- Peak tracking
    peak_pnl_usdt           NUMERIC(12,4),
    peak_loss_usdt          NUMERIC(12,4),

    -- Exit
    exit_price              NUMERIC(20,8),
    exit_time               TIMESTAMPTZ,
    exit_reason             TEXT CHECK (exit_reason IN ('trailing_sl', 'manual', NULL)),
    hold_time_seconds       INTEGER,
    final_pnl_usdt          NUMERIC(12,4),
    fees_usdt               NUMERIC(12,4),
    net_pnl_usdt            NUMERIC(12,4),

    -- Analysis
    failure_type            TEXT CHECK (failure_type IN ('direction', 'signal', NULL)),
    counterfactual_result   JSONB,
    trade_quality_score     NUMERIC(5,2),

    -- Brain influence tracking
    brain_influenced        BOOLEAN DEFAULT FALSE,
    intervention_count      INTEGER DEFAULT 0,
    brain_actions           JSONB DEFAULT '[]'::jsonb,

    -- Trade scoring
    trade_potential_score   NUMERIC(5,2),
    direction_confidence    NUMERIC(5,2),

    -- Feature vector at entry
    feature_vector          JSONB,

    -- pgvector embedding for similarity search
    embedding               vector(512)
);

-- E-04: indexes on trades
CREATE INDEX IF NOT EXISTS idx_trades_pair        ON trades(pair);
CREATE INDEX IF NOT EXISTS idx_trades_status      ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_regime      ON trades(market_regime);
CREATE INDEX IF NOT EXISTS idx_trades_strategy    ON trades(strategy_id);
CREATE INDEX IF NOT EXISTS idx_trades_is_paper    ON trades(is_paper);
CREATE INDEX IF NOT EXISTS idx_trades_embedding   ON trades USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
