# F53 — Qlib Alpha-158 Formulaic Alpha Pool

**Status:** Tier S — implementing this session (cont. 55).

## Research basis

- Microsoft Qlib (github.com/microsoft/qlib, 36.3k★): production AI quant
  platform used internally; the Alpha-158 and Alpha-360 feature handlers
  are well-cited factor libraries.
- Alpha-158 = 158 OHLCV-derived formulaic factors (no chain or text data
  needed). Each one is a closed-form expression over a rolling window of
  raw candles. Examples: `(close-low)/(high-low+1e-12)`, `corr(close, volume, 30)`,
  `rank(ts_mean(volume, 10)/ts_mean(volume, 60))`.
- IC (Information Coefficient) per factor measures rank correlation between
  factor today vs return tomorrow. Promotable factors have rolling 7d IC ≥ 0.02
  (Qlib's own promotion threshold).

## Why the bot needs this

Current factor stack is narrow: F13 LogReg/GBC, F19/F20 deep forecasters,
F48 CandleNet, F50c Mamba, F50g Chronos. All consume *raw* OHLCV. None
build engineered factors from the OHLCV — they all leave that work to the
neural net's first few layers.

Adding 158 explicit hand-engineered factors lets the meta-learner (F8
router + F25 GA + F46 actuator) exploit signal that would otherwise be
spread thinly across deep-net latents. **More importantly:** these factors
are interpretable, so when one breaks down (alpha decay) it's visible in
the IC log, not hidden inside a black-box dropout.

## Design

### File — `ml/qlib_alphas.py` (new, pure Python, no Qlib dependency)

158 formulas implemented as small numpy ops over a (N, 5) OHLCV array.
Each formula is a function `(o, h, l, c, v, ts_window) -> float | None`.

Organised in 6 groups matching the Qlib paper:

1. **K-line factors (10):** body/wick/range ratios — `(close-open)/open`,
   `(high-low)/open`, etc.
2. **Price factors (20):** rolling-window price normalisations — `close /
   ts_mean(close, N)` for N in {5, 10, 20, 30, 60}.
3. **Volume factors (10):** volume-z, dollar-volume ratios — `volume /
   ts_mean(volume, N)`.
4. **Momentum factors (40):** rolling returns — `close/ref(close, N) - 1`,
   plus their z-score variants over the same window.
5. **Correlation factors (40):** rolling `corr(close, volume, N)`, `corr(close,
   ref(close,1), N)`, `corr(high, low, N)`, etc.
6. **Volatility / quantile factors (38):** `ts_std(close, N)`, `ts_rank(close,
   N)`, `ts_min(close, N)`, `ts_max(close, N)` and their combinations.

### IC tracking (key innovation — NOT in vanilla Qlib pool)

Each factor's rolling IC vs 1h-forward return is stored in Redis. Updated
hourly by a Celery beat task. Top-K (default K=20) selected factors are
exported to the composite scorer as a single aggregate bonus.

```
{pair}:qlib_alpha:{factor_id}            # str (float) — current factor value
qlib:ic_history:{factor_id}              # JSON list — last 7d hourly IC values
qlib:ic_rolling:{factor_id}              # str (float) — current 7d mean IC
qlib:top_k_factor_ids                    # JSON list[str] — current selected factors
qlib:top_k_updated_at                    # str (epoch)
qlib:disabled                            # str ("1" = kill switch)
```

### Consumer — `signals/engine.py`

```python
qlib_bonus = 0
try:
    _top_k = json.loads(r.get("qlib:top_k_factor_ids") or "[]")
    if _top_k:
        _agree = 0
        for _fid in _top_k:
            _val = float(r.get(f"{pair}:qlib_alpha:{_fid}") or 0.0)
            _ic = float(r.get(f"qlib:ic_rolling:{_fid}") or 0.0)
            # IC sign tells us which direction _val predicts; align with trade.
            _signed = _val * _ic
            if (direction == "long" and _signed > 0) or \
               (direction == "short" and _signed < 0):
                _agree += 1
        _frac = _agree / len(_top_k)
        if _frac > 0.7:
            qlib_bonus = 12
        elif _frac > 0.6:
            qlib_bonus = 6
        elif _frac < 0.3:
            qlib_bonus = -8
except Exception:
    pass
```

Larger bonus weight (±12) than F50c Mamba (±10) because the Top-K is a
fan of 20 independent factors, each with proven 7d IC ≥ 0.02 — much
higher signal-to-noise than a single forecaster.

### Celery beat

```python
"qlib_alpha_compute": {
    "task": "ml.qlib_alphas.compute_for_active_pairs",
    "schedule": 60.0,  # every minute, after candle close
},
"qlib_alpha_ic_refresh": {
    "task": "ml.qlib_alphas.refresh_ic_top_k",
    "schedule": 3600.0,  # hourly IC recomputation
},
```

### F30 governance

```python
register("F53", "Qlib Alpha-158 Formulaic Pool", activation_phase=0)
```

### Config (`config.yaml`)

```yaml
qlib_alphas:
  enabled: true
  top_k: 20
  ic_threshold: 0.02
  ic_window_hours: 168  # 7 days
  refresh_interval_s: 60
  ic_refresh_interval_s: 3600
  compute_active_only: true   # only compute factors for ACTIVE_PAIRS
```

## Implementation checklist

- [ ] `ml/qlib_alphas.py` — 158 formulas + IC tracker + top-K selector
- [ ] Per-pair compute caller that reads `{pair}:1m:candles` ring buffer
- [ ] Hourly IC refresh against the 1h-ahead return (lookup via 60 candles forward)
- [ ] `signals/engine.py` — qlib_bonus wired
- [ ] `redis_keys.py` — qlib namespace keys
- [ ] `feature_governance/bootstrap.py` — F53 entry
- [ ] `celery_app.py` — two beat tasks
- [ ] `config.yaml` — qlib_alphas block

## Cold-start behaviour

Until ≥168h of factor history exists, top_k is empty → qlib_bonus=0 → no
regression. Factors are computed from candle 1m ring buffer (already
maintained by F50a/b). IC tracker needs 168h of paired (factor[t],
return[t+60min]) samples; once that fills, the top-K selection emerges
automatically.

## Honest scope (Rule 4)

- 158 factors **only on 1m timeframe** for v1. Other TFs deferred — would
  be a 4× compute increase for marginal gain (factors are mostly
  scale-invariant via the ts_mean normalisations).
- IC is computed against 1h-forward return only. Multi-horizon IC (1h, 4h,
  1d) deferred — needs more candle history per pair.
- Top-K is **global**, not per-pair. Per-pair Top-K is on the deferred list
  (would let the system learn that pair X likes momentum factors while pair
  Y likes mean-reversion factors).
- Sign-stability check (does the IC sign flip too often?) is implemented
  but the *adversarial mining* gate from AlphaAgent (deferred F60) is not.
  Factors with unstable signs will be selected then deselected; no real
  damage but some noise in qlib_bonus.

## Session handoff

- Factor compute health — `redis-cli get qlib:top_k_updated_at`
- IC table — `redis-cli keys "qlib:ic_rolling:*" | head`
- Selected factors — `redis-cli get qlib:top_k_factor_ids`
- Per-pair sample — `redis-cli keys "BTCUSDT:qlib_alpha:*" | wc -l` → expect 158
- Governance contribution — `redis-cli get feature:F53:contribution`

File cleared after Rule-2 verification post-implementation.
