# CandleNet 1m/5m/15m/30m Entry Gate

**Created:** 2026-05-30 22:25 UTC (cont. 65i)
**Updated:** 2026-05-31 cont. 65j — B.4 executed, B.1 re-triggered, B.3 queued behind training.
**Status:** EXECUTING — owner approved §B.3 path (wait for 30m/1h training then enable PCG).
**Owner mandate (cont. 65, restated 2026-05-30):**
> "no as the candel chart prediction before entry — we need the candlechart prediction of 1m/5m/15m and 30m for entry and direction. Until it gives values, trades should not open."

Verified with Rule 2 (live code + Redis state read).

---

## A. Current state — what's actually wired

### Two separate gates exist; only one is currently active

| Gate | Defined in | Source | State now | Effect |
|---|---|---|---|---|
| **PCG** (Pre-Open Candle Gate) | `signals/pre_open_candle_gate.py` | reads `{pair}:{tf}:candle_forecast` keys from CandleNet | `pcg:enabled` not set → **DORMANT** | none |
| **Phase C XGB gate** | `signals/engine.py:1047-1115` | reads `predictions:{pair}` hash (written by `prediction/refresh_loop` running XGB) | `prediction:gate_enabled = "1"` → **ACTIVE** | rejecting 325 signals in 20 min as `prediction_not_ready` / `prediction_low_confidence_0.59` |

### CandleNet coverage right now

| TF | Model file | Forecast keys in Redis | Ready? |
|---|---|---|---|
| 1m | `candlenet_1m.pth` (May 27) | 100/100 pairs | ✅ |
| 5m | `candlenet_5m.pth` (May 27) | 100/100 | ✅ |
| 15m | `candlenet_15m.pth` (May 27) | 100/100 | ✅ |
| **30m** | **MISSING** | **0/100** | ❌ |
| **1h** | **MISSING** | **0/100** | ❌ |

### Why trades aren't opening

Phase C XGB gate is the silent killer. It's not the CandleNet gate the owner wants. The XGB predictor:
- only covers ~30 pairs (top-N scanner subset, per `prediction:refresh_top_n` in `prediction/refresh_loop.py`)
- the other 70 pairs get `prediction_not_ready` instantly
- the 30 covered hit a 0.60 conformal confidence floor; the model produces 0.59 → silent reject by 0.01

This is **not what the mandate asks for**. The mandate asks for the CandleNet gate.

## B. Path to the mandate (4 steps, in order)

### B.1 — Train the missing CandleNet models (30m required, 1h optional)

```python
# Trigger from celery_worker_candlenet:
from celery_app import retrain_candlenet_30m, retrain_candlenet_1h
retrain_candlenet_30m.delay()
retrain_candlenet_1h.delay()
```

Both tasks already exist (added in cont. 65e). Each takes ~5-15 min. Outputs:
- `/opt/trading-bot/models/candlenet_30m.pth`
- `/opt/trading-bot/models/candlenet_1h.pth`

Verification:
- `ls /app/models/candlenet_{30m,1h}.pth`
- `redis-cli --scan --pattern '*:30m:candle_forecast' | wc -l` should converge to ~100 within 60s of the next `candlenet_infer_all` beat tick

### B.2 — Set PCG required_tfs to the 4 TFs

```bash
docker exec trading-bot-redis-1 redis-cli SET pcg:required_tfs "1m,5m,15m,30m"
```

Default is `5m,15m`. Setting to all four enforces the mandate. Add 1h later if/when its model trains and the operator wants more conservatism.

### B.3 — Enable PCG (master switch)

```bash
docker exec trading-bot-redis-1 redis-cli SET pcg:enabled 1
```

PCG behavior after enable, per `signals/pre_open_candle_gate.py`:
- For every signal, check `{pair}:{tf}:candle_forecast` exists for ALL of `1m,5m,15m,30m`.
- If any missing → "wait" (caller skips this tick, retries next brain cycle).
- After `pcg:max_wait_seconds` (default 30s) → "drop" with reason `pcg_dropped_timeout`.
- If all present → strict cascade vote; needs `cascade_confidence >= pcg:min_cascade_conf` (default 50) AND non-veto direction.

This is exactly the "hold and wait until it gives the direction long or short" behavior in the mandate.

### B.4 — Disable the XGB Phase C gate (it's redundant + currently miscovered)

```bash
docker exec trading-bot-redis-1 redis-cli SET prediction:gate_enabled 0
```

Rationale: CandleNet PCG enforces the same intent (require model agreement before entry) with broader coverage (100 pairs vs 30) and a more meaningful signal (per-TF candle direction, not XGB conformal). Running both stacks rejections multiplicatively and there's no design reason to.

> **Alternative**: keep Phase C as a soft secondary, lower its threshold to 0.50 so it stops false-rejecting at 0.59. But cleaner to disable while PCG owns the entry gate.

## C. Order-of-operations matters

Wrong order = downtime extension or accidental gate-double-fire. Correct sequence:

1. **B.1 first** — trigger 30m + 1h training, WAIT for `.pth` files to land + first `candlenet_infer_all` cycle to populate Redis (~20 min total).
2. **Verify coverage**: `*:30m:candle_forecast = 100`.
3. **B.2** — set `pcg:required_tfs`.
4. **B.3** — flip `pcg:enabled = 1`.
5. **B.4** — flip `prediction:gate_enabled = 0`.
6. Watch first cycle: `pcg:ready_first_tick` counter climbing, `prediction:gate:reject:*` counters frozen.

If we do B.3/B.4 BEFORE B.1 completes, every signal gets dropped on the 30s PCG wait timeout — trades stay frozen.

## D. Expected post-deploy state

- 100/100 pairs eligible for PCG check (vs 30/100 today on Phase C).
- Per cycle: each signal either ready-now (4 TFs all populated and cascade votes one direction with conf ≥ 50), waits up to 30s, or drops with a visible reason.
- All rejection paths observable per `feedback_silent_rejection` (PCG counters already wired, lines 38-49 of `pre_open_candle_gate.py`).
- Trade open rate recovers gradually as PCG ready_first_tick climbs.

## E. Open questions for the owner

1. **Disable Phase C entirely (recommended) or run both in parallel?** Recommendation: disable. Running both = multiplicative rejection with no design rationale.
2. **`pcg:min_cascade_conf` floor** — default 50. Tighter (60-70) = fewer false positives but fewer trades. Loose (40) = more trades, more noise. Recommend 50 for first deploy, tune after a week of data.
3. **`pcg:max_wait_seconds`** — default 30. Per the mandate "hold and wait until it gives", maybe extend to 60-120s? Or keep 30 with the understanding that signals that don't resolve quickly were probably weak.
4. **1h required, or 1m+5m+15m+30m only?** Mandate explicitly names 1m/5m/15m/30m. 1h not required — but adds robustness. Recommend: not required initially; add after 1h model has 1 week of forecasts.

## F. Checklist

- [x] Owner sign-off on steps B.1–B.4 + the four open questions above (cont. 65j)
- [x] Trigger `retrain_candlenet_30m.delay()` and `retrain_candlenet_1h.delay()` (cont. 65j; cont. 65f's trigger never produced .pth)
- [ ] Wait for `.pth` files + Redis coverage = 100/100 for 30m (in flight, background poller running)
- [x] `redis-cli SET pcg:required_tfs "1m,5m,15m,30m"` (already set pre-session)
- [ ] `redis-cli SET pcg:enabled 1` (gated on training landing)
- [x] `redis-cli SET prediction:gate_enabled 0` (cont. 65j — counters confirmed frozen)
- [ ] Verify counters: `pcg:ready_first_tick` climbing, `prediction:gate:reject:*` frozen (XGB side done; PCG side gated on enable)
- [ ] After 24h: review `pcg:dropped:no_forecast` per pair — if certain pairs always time out, exclude them from active scanner or retrain
- [x] PROGRESS.md cont. 65j entry (this session)

## G. What's NOT in this PR (intentionally)

- **CandleNet model upgrade / retrain pipeline** — separate concern (`f50e_continuous_candle_training.md`)
- **SL/TP placement caps** — separate redesign in `sl_tp_placement_redesign.md` (cont. 65h draft)
- **The XGB predictor itself** — keeping it available (refresh loop still runs) so if PCG ever needs a fallback or A/B comparison, the data exists
- **Phase A predict-all expansion** — that's the longer roadmap in `predict_all_before_open.md`
