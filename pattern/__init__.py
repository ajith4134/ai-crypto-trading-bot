"""Pattern clustering + effectiveness registry (cont. 55).

Plan: /opt/trading-bot/next_impl/predict_all_before_open.md
Live-data-only constraint per user 2026-05-29: embeddings ALWAYS come from
live exchange candle data via ml.candlenet.run_inference at signal-fire time,
NEVER from closed-trade snapshots.
"""
