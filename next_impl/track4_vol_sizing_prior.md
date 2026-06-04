# Track 4 — Foundation-model volatility/magnitude prior for SIZING + SL/TP

**Origin:** cont.70e proved per-candle DIRECTION is at an input ceiling (~0.55 AUC; microstructure
added no lift). User chose "Track 4 as sizing/vol prior" — a SOFT foundation-model vol/magnitude
prior feeding SL/TP placement + position sizing, NEVER a direction gate. The edge is in
sizing/exits, not direction.

## AUDIT (cont.70f, 2026-06-03) — verified on disk + in the live worker

**The foundation vol-prior is ~90% already built but DORMANT + MISWIRED:**
- `ml/foundation_forecast.py` (F50g, Chronos-Bolt) EXISTS. `predict()` returns
  `{dir1h, mag1h, q05, q50, q95, anchor, spread, ts}` — i.e. magnitude + a volatility band
  (`spread`=q95-q05). Writes `{pair}:foundation_forecast` TTL 600s.
- Model DOWNLOADED: `models/chronos_bolt_base/` (present since 2026-05-30). Loads in 0.9s in the
  candlenet worker; torch 2.12 + `chronos` pkg present.
- DORMANT: NO producer task anywhere (`grep foundation_forecast celery_app.py` = empty) -> nobody
  calls predict() -> zero `*:foundation_forecast` keys live. Also gated behind F50g governance
  (inactive: governance:F50g empty).
- MISWIRED CONSUMER: signals/engine.py:559-576 reads `load_forecast(pair)` but uses ONLY `dir1h`
  as a ±8 DIRECTION bonus and THROWS AWAY spread/q05/q95/mag1h. That's exactly backwards from the
  Track-4 goal (we want the vol band, not the weak direction signal).
- EXISTING SL/TP magnitude source: risk/manager.py already uses CandleNet `mag1`
  (`{pair}:{tf}:candle_forecast`) for TP placement (TP1 = entry×(1+mag1_avg×sign)). Live across
  1m/5m/15m/30m/1h. NOTHING feeds a vol band into SIZING.

**Version bugs in foundation_forecast.py (must fix on revive):**
1. Calls `pipeline.predict_quantiles(context=..., ...)` — installed chronos needs POSITIONAL
   `inputs` (sig: `predict_quantiles(inputs, prediction_length=, quantile_levels=)`). Currently
   would raise TypeError -> predict() returns None silently (the warning path).
2. Requests `quantile_levels=[0.05,0.5,0.95]` but Chronos-Bolt only trained on [0.1..0.9] -> clamps
   to q10/q90 (warns). Switch to [0.1,0.5,0.9] and rename keys q10/q90 (or keep q05/q95 names but
   document they're really q10/q90).

**Latency reality:** ~7s/pair first call (CPU; prediction_length=60). Likely includes warmup;
needs steady-state re-measure. EITHER WAY -> cadenced BACKGROUND producer over a BOUNDED pair set
(e.g. top-N active), Redis-cached; NEVER on the hot decide path. Kronos-base would be SLOWER than
Chronos-Bolt on CPU -> greenfield Kronos has worse latency, not better.

## DECISION (recommended): REVIVE Chronos, don't greenfield Kronos
f55 blueprint ITSELF sequences Kronos AFTER the Chronos ensemble is measured ("if IC<0.04 ->
Kronos"). We never even ran Chronos. Reviving = ~10x cheaper, uses the downloaded model, and
delivers Track-4's goal now. Kronos deferred to a MEASURED follow-up.

## PLAN
P1. FIX foundation_forecast.py: positional `inputs`; quantile_levels [0.1,0.5,0.9]; re-measure
    steady-state latency. (prediction/ is COPY-baked? NO — ml/ is bind-mounted on candlenet ->
    no rebuild for ml/foundation_forecast.py.)
P2. PRODUCER: new celery beat task `foundation_forecast_task` @ queue=predict_all or microstructure
    (candlenet worker: torch + ml/ + model). Iterate a BOUNDED active-pair set; call predict();
    write {pair}:foundation_forecast. Cadence 5 min. Kill switch + counters. Activate F50g
    governance (register + is_active).
P3. NEW WIRING (the real gap), SOFT + reversible + Redis-tunable, gate-NOT-kill:
    (a) SIZING: a vol-uncertainty size multiplier in signals/engine.py sizing chain (~line 2586,
        after archetype tilt). Wider relative spread (spread/anchor) = more uncertain -> size DOWN;
        tight = size up. Clip e.g. [0.5, 1.25]. Redis vol_prior:size_* keys.
    (b) SL/TP: blend foundation q10/q90 into risk/manager.py placement as an ADDITIONAL source
        alongside CandleNet mag (e.g. SL just beyond q10 for longs / q90 for shorts; TP toward the
        far quantile). Soft blend, not replace.
    Keep/leave the existing ±8 dir bonus as-is (small, not the focus).
P4. MEASURE (reuse cont.70e shadow methodology, adapted to PnL/RR not AUC): forward-compare
    realized RR / SL-hit-rate with vs without the vol-sizing prior before trusting it. Soft the
    whole time. THIS is the f55 gate for whether Kronos is ever worth building.
P5. (DEFERRED) Kronos greenfield — only if Chronos vol prior proves insufficient after measurement.

## DEPLOY
ml/foundation_forecast.py + ml/<new producer logic>: ml/ bind-mounted on candlenet -> no rebuild,
restart candlenet+beat. signals/engine.py: bind-mounted on brain -> restart brain. risk/manager.py:
brain bind-mounts risk/ -> restart brain. celery_app.py bind-mounted -> restart beat+worker.
ALL no-rebuild. HIGH-STAKES (live sizing + SL/TP) -> show diff + approve before applying P3.

## STATUS: P1+P2+P3 SHIPPED + VERIFIED (cont.70f). P4 pending, P5 (Kronos) deferred.
- P1 DONE: foundation_forecast.py fixed (positional inputs, q10/q90, spread_frac; 1h candles,
  CONTEXT 256/min 96, HORIZON 3, TTL 1200; TFT-defer removed). ~300ms/pair steady.
- P2 DONE: run_forecast_sweep + foundation_forecast_task @predict_all, beat @600s. VERIFIED
  sweep_ok=60, 68 keys live. Kill switch foundation:disabled; counters foundation:sweep_*.
- P3 DONE (user-approved both): SIZING mult in engine.py (~2673) + SL FLOOR in risk/manager.py
  (~172). Soft, Redis-tunable (vol_prior:*). VERIFIED firing: size:down_count=1,
  sl_floor_applied_count=1, brain clean. TP blend DEFERRED.
- P4 PENDING: forward-measure realized RR / SL-hit-rate WITH vs WITHOUT vol prior. Needs trades to
  accumulate. Approach: tag trade metadata with the applied size_mult + whether SL floor bound,
  then compare cohorts (or A/B by toggling vol_prior:size_enabled on alternating windows). This is
  the f55 gate for whether Kronos (P5) is worth building.
- P5 DEFERRED: greenfield Kronos — only if Chronos vol prior proves insufficient after P4.

## REVERSIBLE
redis-cli SET vol_prior:size_enabled 0  /  vol_prior:sl_floor_enabled 0  /  foundation:disabled 1
Tunables: vol_prior:size_ref (0.03), size_min (0.5), size_max (1.25), sl_band_k (0.5).
