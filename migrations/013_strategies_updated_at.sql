-- Migration 013: Add strategies.updated_at column (2026-05-20)
--
-- strategy/save.py:39 and strategy/lifecycle.py:99 both reference
-- updated_at = NOW() in UPDATE/UPSERT statements. The column was never
-- created. Every research-engine strategy creation (F36) and every
-- lifecycle status transition (F8) failed with:
--   ERROR: column "updated_at" of relation "strategies" does not exist
--
-- Adds the column with DEFAULT NOW() so existing rows are stamped.

ALTER TABLE strategies
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
