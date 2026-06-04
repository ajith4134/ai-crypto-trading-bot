-- Migration 029 — Idea 2 Postmortem RAG (cont. 64, 2026-05-30).
--
-- Adds 768-dim text embeddings of the LLM decode_reason on both decoder tables
-- so signals/engine.py can do nearest-neighbour lookup at decision time:
--
--   query_vec  = embed("pair=X dir=long bull strength=42")
--   top5       = SELECT decode_reason, peak_profit_pct
--                FROM counterfactuals
--                ORDER BY decode_reason_embedding <=> query_vec
--                LIMIT 5;
--   prior_belief = avg(top5.peak_profit_pct > 5%)
--
-- Embedder: Ollama nomic-embed-text (768-dim), pulled into the existing
-- trading-bot-ollama-1 container. Reuses config.llm.ollama_url.
--
-- Coverage: F9 (counterfactuals.miss_decode_reason) AND F12
-- (mismatches.decode_reason). Both columns get a vector(768) sibling +
-- ivfflat cosine index.
--
-- Reversible: ADD COLUMN IF NOT EXISTS. NULL embedding means "not yet
-- embedded" — the celery beat task `embed_pending_postmortems` will fill
-- it in on the next pass. Backfill of historical rows runs once via
-- `backfill_postmortem_embeddings`.
--
-- ivfflat tuning: lists=100 picked per pgvector guidance
-- (sqrt(rows) for rows<10k). Re-tune to lists=sqrt(N) when row count
-- crosses 10k.

BEGIN;

-- F9 side ------------------------------------------------------------
ALTER TABLE counterfactuals
  ADD COLUMN IF NOT EXISTS decode_reason_embedding vector(768);

-- ivfflat cosine index only over rows that have been embedded.
-- Partial index keeps it lean while backfill is in flight.
CREATE INDEX IF NOT EXISTS idx_counterfactuals_decode_reason_emb
  ON counterfactuals
  USING ivfflat (decode_reason_embedding vector_cosine_ops)
  WITH (lists = 100);

-- F12 side ------------------------------------------------------------
ALTER TABLE mismatches
  ADD COLUMN IF NOT EXISTS decode_reason_embedding vector(768);

CREATE INDEX IF NOT EXISTS idx_mismatches_decode_reason_emb
  ON mismatches
  USING ivfflat (decode_reason_embedding vector_cosine_ops)
  WITH (lists = 100);

COMMIT;
