# F57 — TLOB Level-2 Order-Book Transformer

**Status:** Tier B — DEFERRED (design only this session, cont. 55).
**Research:** arXiv:2502.15757 "TLOB: A Transformer-based Limit Order Book
Predictor" — +3.7 F1 over DeepLOB on raw L2 LOB; especially strong on
volatile regimes.

## Why deferred (HARD)

1. **Data**: L2 LOB is not currently captured. Binance futures depth WS
   gives 5/10/20-level updates at ~100Hz — that's a significant new data
   pipeline (~5 MB/min per pair). Storage and rolling-buffer engineering
   alone is a session.
2. **Compute**: TLOB is a 6-layer transformer; CPU inference ~30ms/pair —
   total budget at 30 active pairs = ~1s per decide cycle. Profiling-needed
   before commit.
3. **Training**: needs labeled LOB events. Self-supervised pretraining like
   F48§MAE is the right approach but adds another pretrainer step.

## When to implement

After F52+F53+F54 prove out AND we have a clear directional-edge bottleneck
that the current OFI+VPIN+microstructure stack isn't capturing. TLOB
specifically targets short-horizon (5-30s) direction; for the bot's
1m-decide cadence, the marginal gain may be smaller than the paper headline.

## Design sketch

- `data/lob_capture.py` — WS subscription to depth20 stream; ring buffer
  in Redis (capped 10K events per pair).
- `ml/tlob.py` — TLOB architecture (transformer encoder, classification head).
- Pretrainer step 15 — self-supervised LOB MAE warm-start.
- Bonus in signals/engine.py: **±10**.
- Inference cache: 1s TTL since LOB updates are sub-second.

## Honest concern

The "+3.7 F1" headline is on equity-LOB data. Crypto perpetual LOB has
different microstructure (smaller tick size, less HFT presence on some
exchanges, funding-rate squeeze patterns). Expect ~50% degradation in
translation — TLOB on crypto is still likely a positive contribution but
not guaranteed.

## Session handoff

Not implemented yet. Open when re-opening the topic.
