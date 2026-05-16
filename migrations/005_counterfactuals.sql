-- E-06: counterfactuals table
CREATE TABLE IF NOT EXISTS counterfactuals (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    signal_id               UUID NOT NULL REFERENCES signals(id),
    tracking_window_start   TIMESTAMPTZ NOT NULL,
    tracking_window_end     TIMESTAMPTZ NOT NULL,  -- signal_time + 72h
    peak_profit_pct         NUMERIC(8,4),
    peak_loss_pct           NUMERIC(8,4),
    trailing_sl_exit_pct    NUMERIC(8,4),
    would_have_won          BOOLEAN,
    miss_decode_reason      TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
