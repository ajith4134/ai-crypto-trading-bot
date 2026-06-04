-- Migration 012: F37 Multi-Agent Debate Council verdict persistence (2026-05-20)
--
-- signals/engine.py:342 writes signal["debate_verdict"] but write_signal()
-- never persisted it, and the column didn't exist. Result: every debate
-- verdict at brain_stage >= 3 was silently dropped, breaking counterfactual
-- comparison of debate decisions against shadow outcomes (blueprint F37).
--
-- Adds the column. write_signal() is updated separately in memory/write.py
-- to actually pass the value through.

ALTER TABLE signals
    ADD COLUMN IF NOT EXISTS debate_verdict TEXT;  -- 'skip'|'skip_risk'|'reduced_allocation'|'exploratory'|'full_allocation'|NULL

-- Optional index for "find all skipped-by-debate signals last 7d" analytics
CREATE INDEX IF NOT EXISTS idx_signals_debate_verdict
    ON signals (debate_verdict)
    WHERE debate_verdict IS NOT NULL;
