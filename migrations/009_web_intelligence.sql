-- E-10: web_intelligence table
CREATE TABLE IF NOT EXISTS web_intelligence (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source              TEXT NOT NULL,
    source_url          TEXT,
    raw_content         TEXT,
    signal_type         TEXT CHECK (signal_type IN ('news', 'idea', 'strategy', 'risk')),
    pairs_affected      JSONB DEFAULT '[]'::jsonb,
    sentiment           TEXT CHECK (sentiment IN ('bullish', 'bearish', 'neutral')),
    confidence          NUMERIC(5,2),
    summary             TEXT,
    credibility_score   NUMERIC(5,4) DEFAULT 0.5,
    brain_weight        NUMERIC(5,4) DEFAULT 0.5,
    brain_action        TEXT,
    outcome_tracked     BOOLEAN DEFAULT FALSE,
    outcome_correct     BOOLEAN,
    claimed_win_rate    NUMERIC(5,2),
    actual_win_rate     NUMERIC(5,2)
);
