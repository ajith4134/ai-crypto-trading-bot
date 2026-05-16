-- Migration 011: Add missing columns found in blueprint vs disk audit (2026-05-16)
-- Adds columns specified in BOT_BLUEPRINT.md Section 13 that were omitted from
-- the original migrations. All use ADD COLUMN IF NOT EXISTS — safe to re-run.

-- ─── trades (Blueprint Section 13.1) ────────────────────────────────────────
-- Blueprint has: capital_pct, position_size_usdt, signals_at_entry, created_at
-- Disk has:      capital_usdt, quantity, feature_vector (equivalents — aliases added below)

ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS capital_pct          NUMERIC(6,2),      -- % of total capital at entry
    ADD COLUMN IF NOT EXISTS position_size_usdt   NUMERIC(20,4),     -- notional value (leverage × capital)
    ADD COLUMN IF NOT EXISTS signals_at_entry     JSONB,             -- alias for feature_vector (blueprint name)
    ADD COLUMN IF NOT EXISTS created_at           TIMESTAMPTZ DEFAULT NOW();

-- Back-fill position_size_usdt from existing rows (capital_usdt × leverage)
UPDATE trades
    SET position_size_usdt = capital_usdt * leverage
    WHERE position_size_usdt IS NULL AND capital_usdt IS NOT NULL AND leverage IS NOT NULL;

-- ─── signals (Blueprint Section 13.2) ───────────────────────────────────────
-- Blueprint has: potential_score, direction_confidence, is_paper
-- Disk missing these three columns

ALTER TABLE signals
    ADD COLUMN IF NOT EXISTS potential_score      NUMERIC(5,2),      -- 0-100 trade potential at signal time
    ADD COLUMN IF NOT EXISTS direction_confidence NUMERIC(5,2),      -- 0-100 directional conviction
    ADD COLUMN IF NOT EXISTS is_paper             BOOLEAN NOT NULL DEFAULT TRUE;

-- ─── counterfactuals (Blueprint Section 13.3) ───────────────────────────────
-- Blueprint has: pair, direction, signal_time, miss_decoded
-- Disk has tracking_window_start (≈ signal_time) but missing pair, direction, miss_decoded

ALTER TABLE counterfactuals
    ADD COLUMN IF NOT EXISTS pair                 TEXT,              -- trading pair e.g. 'BTCUSDT'
    ADD COLUMN IF NOT EXISTS direction            TEXT CHECK (direction IN ('long', 'short', NULL)),
    ADD COLUMN IF NOT EXISTS signal_time          TIMESTAMPTZ,       -- exact signal generation time
    ADD COLUMN IF NOT EXISTS miss_decoded         BOOLEAN DEFAULT FALSE;

-- Back-fill signal_time from tracking_window_start (same value)
UPDATE counterfactuals
    SET signal_time = tracking_window_start
    WHERE signal_time IS NULL AND tracking_window_start IS NOT NULL;

-- ─── brain_state (Blueprint Section 13.5) ───────────────────────────────────
-- Blueprint has extra metrics columns and recorded_at that our single-row design omitted

ALTER TABLE brain_state
    ADD COLUMN IF NOT EXISTS total_closed_trades  INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS overall_win_rate     NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS directional_accuracy NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS rolling_sharpe       NUMERIC(8,4),
    ADD COLUMN IF NOT EXISTS model_checkpoint_path TEXT,
    ADD COLUMN IF NOT EXISTS recorded_at          TIMESTAMPTZ DEFAULT NOW();

-- ─── pairs (Blueprint Section 13.6) ─────────────────────────────────────────
-- Blueprint has: scanned_at, strategy_win_rate_score (vs win_rate_score),
--               historical_pnl_score (vs pnl_score), total_pnl_usdt (vs total_net_pnl_usdt),
--               win_rate, trade_count, directional_accuracy

ALTER TABLE pairs
    ADD COLUMN IF NOT EXISTS scanned_at              TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS strategy_win_rate_score NUMERIC(5,2),   -- blueprint name for win_rate_score
    ADD COLUMN IF NOT EXISTS historical_pnl_score    NUMERIC(5,2),   -- blueprint name for pnl_score
    ADD COLUMN IF NOT EXISTS total_pnl_usdt          NUMERIC(20,4) DEFAULT 0, -- blueprint name for total_net_pnl_usdt
    ADD COLUMN IF NOT EXISTS win_rate                NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS trade_count             INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS directional_accuracy    NUMERIC(5,2);

-- Back-fill aliased columns from existing data
UPDATE pairs SET
    scanned_at              = last_scanned_at,
    strategy_win_rate_score = win_rate_score,
    historical_pnl_score    = pnl_score,
    total_pnl_usdt          = total_net_pnl_usdt
WHERE scanned_at IS NULL;

-- ─── feature_governance (Blueprint Section 13.7) ────────────────────────────
-- Blueprint uses: action_time, action, contribution_score, performance_before,
--                performance_after, regime columns (different naming from our impl)

ALTER TABLE feature_governance
    ADD COLUMN IF NOT EXISTS action_time          TIMESTAMPTZ DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS action               TEXT,              -- 'flagged'|'turned_off'|'probation'|'restored'
    ADD COLUMN IF NOT EXISTS contribution_score   NUMERIC(8,4),
    ADD COLUMN IF NOT EXISTS performance_before   NUMERIC(8,4),     -- win rate / Sharpe before action
    ADD COLUMN IF NOT EXISTS performance_after    NUMERIC(8,4),     -- measured after 50-100 trades
    ADD COLUMN IF NOT EXISTS regime               TEXT;             -- if regime-specific deactivation

-- ─── web_intelligence (Blueprint Section 13.8) ──────────────────────────────
-- Blueprint uses source_name (we have source), outcome_measured (we have outcome_tracked)
-- Add blueprint-named aliases

ALTER TABLE web_intelligence
    ADD COLUMN IF NOT EXISTS source_name          TEXT,              -- blueprint name for source column
    ADD COLUMN IF NOT EXISTS outcome_measured      BOOLEAN DEFAULT FALSE; -- blueprint name for outcome_tracked

-- Back-fill
UPDATE web_intelligence SET
    source_name     = source,
    outcome_measured = outcome_tracked
WHERE source_name IS NULL;

-- ─── experiments (Blueprint Section 13.9) ───────────────────────────────────
-- Blueprint has: started_at, completed_at, strategy_a_id, strategy_b_id,
--               result, metric_a, metric_b, metric_type, action_taken

ALTER TABLE experiments
    ADD COLUMN IF NOT EXISTS started_at           TIMESTAMPTZ DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS completed_at         TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS strategy_a_id        UUID REFERENCES strategies(id),
    ADD COLUMN IF NOT EXISTS strategy_b_id        UUID REFERENCES strategies(id),
    ADD COLUMN IF NOT EXISTS result               TEXT CHECK (result IN ('a_wins','b_wins','inconclusive', NULL)),
    ADD COLUMN IF NOT EXISTS metric_a             NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS metric_b             NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS metric_type          TEXT,             -- 'sharpe'|'win_rate'|'pnl'
    ADD COLUMN IF NOT EXISTS action_taken         TEXT;
