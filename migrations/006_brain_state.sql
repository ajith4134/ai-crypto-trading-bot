-- E-07: brain_state table (single row, updated in place)
CREATE TABLE IF NOT EXISTS brain_state (
    id                          INTEGER PRIMARY KEY DEFAULT 1,
    evolution_stage             INTEGER NOT NULL DEFAULT 1,
    paper_closed_trades         INTEGER NOT NULL DEFAULT 0,
    live_closed_trades          INTEGER NOT NULL DEFAULT 0,
    opro_prompt_version         INTEGER NOT NULL DEFAULT 0,
    current_executor_prompt     JSONB DEFAULT '{}'::jsonb,
    feature_weights             JSONB DEFAULT '{}'::jsonb,
    competence_map              JSONB DEFAULT '{}'::jsonb,
    active_feature_flags        JSONB DEFAULT '{}'::jsonb,
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT single_row CHECK (id = 1)
);

-- Insert initial row
INSERT INTO brain_state (id) VALUES (1) ON CONFLICT DO NOTHING;
