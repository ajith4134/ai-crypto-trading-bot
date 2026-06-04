-- Migration 016: F37 Multi-Agent Debate Council — argument persistence (2026-05-20)
--
-- Blueprint Feature 37 (Section 9.1 / NeurIPS 2024 FinCon) specifies a 2-3
-- round debate followed by Verbal Reinforcement: "After every closed trade,
-- the council reviews the outcome and updates its 'systematic investment
-- beliefs' — a set of standing positions that inform future debates."
--
-- Prior to this migration the debate ran round 1 only and produced a single
-- verdict that was persisted on signals.debate_verdict (migration 012). The
-- raw arguments themselves were discarded after the verdict was synthesised,
-- so verbal reinforcement was impossible — you can't update agent-specific
-- beliefs without preserving each agent's claims for retrospective review.
--
-- This table persists every agent's arguments per round so that on trade
-- close the council can compute was_correct per (agent, round) and update
-- the per-agent weight priors in Redis (`debate:agent_weight:{bull,bear,risk}`).

CREATE TABLE IF NOT EXISTS debate_arguments (
    signal_id    UUID         NOT NULL,
    trade_id     UUID         NULL REFERENCES trades(id),  -- filled after open_trade returns; NULL when debate verdict skipped trade
    agent_role   TEXT         NOT NULL,  -- 'bull' | 'bear' | 'risk' | 'moderator'
    round_num    INTEGER      NOT NULL,  -- 1, 2, or 3
    arguments    JSONB        NOT NULL,  -- the raw agent output (argue_for/argue_against, arguments list, etc.)
    score        INTEGER      NULL,      -- confidence (bull) | risk_score (bear) | recommended_size_pct (risk)
    was_correct  BOOLEAN      NULL,      -- back-filled by memory/write.py:write_trade_close
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (signal_id, agent_role, round_num)
);

-- Fast lookup from trade close → all debate arguments for verbal reinforcement.
CREATE INDEX IF NOT EXISTS idx_debate_args_trade
    ON debate_arguments (trade_id)
    WHERE trade_id IS NOT NULL;

-- "Show me Bull's track record" / "Show me Bear's track record" analytics.
CREATE INDEX IF NOT EXISTS idx_debate_args_agent_correct
    ON debate_arguments (agent_role, was_correct)
    WHERE was_correct IS NOT NULL;
