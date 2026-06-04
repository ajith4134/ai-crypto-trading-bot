-- 032_launch_pad.sql
-- cont. 70 (2026-06-03) — Launch-Pad: a 10-deep "on-deck" buffer of pre-qualified
-- symbols that is the SOLE funnel for opening trades (owner mandate, see
-- /opt/trading-bot/next_impl/launch_pad_10.md). Ships behind launchpad:enabled=0
-- (shadow-only) until reviewed, so this migration has ZERO behavioural effect on
-- its own — it only adds storage.
--
-- Two tables:
--   launch_pad          — N live slots (default 10), the current buffer occupants.
--                         Each slot shadow-tracks PnL / peak-profit / peak-loss(MAE)
--                         from the price at which the symbol entered the table.
--   launch_pad_history  — one row per buffer EXIT (fired/displaced/expired/flipped/
--                         cooldown). Records the 3 movement metrics AS AT ENTRY plus
--                         the realised shadow outcome, so we can study which movement
--                         metric (candlenet / predicted / realized) is most reliable
--                         BEFORE trusting any one of them (owner Q4).
--
-- Owner (write): signals/launch_pad/{maintainer,shadow,qualify}.py
-- Readers: signals/engine.py (P5 open hook), dashboard/api.py (P6 panel).

BEGIN;

CREATE TABLE IF NOT EXISTS launch_pad (
    slot              smallint    PRIMARY KEY,                       -- 1..depth (stable slot id; occupant rotates)
    symbol            text,                                          -- NULL when slot empty
    direction         text        CHECK (direction IS NULL OR direction IN ('long','short')),
    table_entry_price numeric,                                       -- price when this symbol entered the slot
    table_entry_ts    timestamptz DEFAULT now(),
    last_mark         numeric,                                       -- most recent mark used for shadow pnl
    shadow_pnl_pct    numeric     DEFAULT 0,                         -- current shadow PnL % (dir-adjusted)
    peak_profit_pct   numeric     DEFAULT 0,                         -- max favourable excursion (>= 0)
    peak_loss_pct     numeric     DEFAULT 0,                         -- max adverse excursion / MAE (<= 0)
    mv_candlenet      numeric,                                       -- movement col 1: |dir3-0.5| * magnitude
    mv_predicted      numeric,                                       -- movement col 2: predict-all expected move %
    mv_realized       numeric,                                       -- movement col 3: recent return / ATR
    qualified         boolean     DEFAULT false,                     -- passed the "open-green" gate this tick
    flips_count       smallint    DEFAULT 0,                         -- direction flips so far (cap via launchpad:flip_cap)
    state             text        DEFAULT 'empty'
                                  CHECK (state IN ('empty','scouting','staged','confirmed_green',
                                                   'fired','cooldown','flipped')),
    regime            text,
    ttl_expires_at    timestamptz,                                   -- when this candidate goes stale
    updated_at        timestamptz DEFAULT now()
);

-- Seed the fixed slots empty (default depth 10). Maintainer fills them.
INSERT INTO launch_pad (slot)
SELECT gs FROM generate_series(1, 10) AS gs
ON CONFLICT (slot) DO NOTHING;

CREATE TABLE IF NOT EXISTS launch_pad_history (
    id                bigserial   PRIMARY KEY,
    symbol            text        NOT NULL,
    direction         text,
    table_entry_price numeric,
    table_entry_ts    timestamptz,
    exit_ts           timestamptz DEFAULT now(),
    exit_reason       text        CHECK (exit_reason IS NULL OR exit_reason IN
                                  ('fired','displaced','expired','flipped','cooldown')),
    shadow_pnl_pct    numeric,                                       -- final shadow PnL at exit
    peak_profit_pct   numeric,
    peak_loss_pct     numeric,
    mv_candlenet      numeric,                                       -- the 3 movement metrics AS AT ENTRY
    mv_predicted      numeric,
    mv_realized       numeric,
    regime            text,
    flips_count       smallint,
    trade_id          bigint                                         -- set in P5 if this became a real trade (join realised PnL)
);

CREATE INDEX IF NOT EXISTS idx_launch_pad_history_symbol  ON launch_pad_history (symbol);
CREATE INDEX IF NOT EXISTS idx_launch_pad_history_exit_ts ON launch_pad_history (exit_ts);
CREATE INDEX IF NOT EXISTS idx_launch_pad_history_reason  ON launch_pad_history (exit_reason);

COMMIT;

-- ============================================================================
-- ROLLBACK (manual):
--   BEGIN;
--   DROP TABLE IF EXISTS launch_pad_history;
--   DROP TABLE IF EXISTS launch_pad;
--   COMMIT;
-- And delete the launchpad:* Redis keys (registry in redis_keys.py).
-- ============================================================================
