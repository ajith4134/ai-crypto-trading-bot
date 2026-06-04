# F52 — Exchange Net-Flow Directional Gate

**Status:** Tier S — implementing this session (cont. 55).
**Numbering note:** Original synthesis called this "F51b". F51b was already taken
by the Funding Rate Extremes Gate (cont. 51), so renumbered to F52.

## Research basis

- Coin Metrics 2025 "On-Chain Signal Quality" report: exchange net-flow > 2σ
  predicts directional move with **72% accuracy** as a regime gate.
- Glassnode 2024-2026 backtests: net inflows to exchanges precede sell pressure
  (median lead 4-12h); net outflows precede accumulation rallies.
- CryptoQuant inflow-mean-ratio (IFR > 2) flagged March/Aug 2025 BTC tops
  within 6h.

## Why the bot needs this

Audit of current direction-prediction stack (F13 + F19 + F20 + F48 + F50c +
F50g + F24M + multi-TF cascade) shows **zero on-chain signal**. Every
forecaster reads OHLCV / LOB / microstructure derived from exchange-internal
data. Exchange net-flow is the single largest blind spot — it sees coins
*entering or leaving* the exchanges, which directly proxies forthcoming sell
or buy pressure.

## Design

### Producer — `data/onchain_netflow.py` (new)

Polls one of (in priority order, fall through on failure / no key):
1. CryptoQuant free-tier (`https://api.cryptoquant.com/v1/btc/exchange-flows/inflow`)
2. Glassnode free metrics (`https://api.glassnode.com/v1/metrics/transactions/transfers_volume_to_exchanges_sum`)
3. Coinglass exchange balance (`https://open-api-v3.coinglass.com/api/exchange-balance-list`)
4. CoinMetrics community (`https://community-api.coinmetrics.io/v4/timeseries/asset-metrics`)

All four have free public endpoints today (May 2026) — no key required for
basic queries; rate-limited at ~10/min.

For ALT pairs without dedicated chain coverage (per-exchange net-flow): proxy
via the exchange's own deposit/withdraw aggregate or fall back to the BTC
beta-correlated signal. Honest scope: BTC and ETH get true per-asset signal;
remaining alts get a regime-shared signal.

### Redis keys (`redis_keys.py` additions)

```
EXCHANGE_NETFLOW_RAW      = "{pair}:exchange_netflow_raw"      # str (float, USD)
EXCHANGE_NETFLOW_Z        = "{pair}:exchange_netflow_z"        # str (float, z-score)
EXCHANGE_NETFLOW_REGIME   = "{pair}:exchange_netflow_regime"   # str: inflow | outflow | neutral
NETFLOW_UPDATED_AT        = "netflow:updated_at"               # str (epoch)
NETFLOW_DEADLOCK_DISABLED = "netflow:disabled"                 # str ("1" = kill switch)
NETFLOW_REJECT_COUNT      = "netflow:reject_count"             # str (int)
NETFLOW_CALL_COUNT        = "netflow:call_count"               # str (int)
```

### z-score computation

30-day rolling window per pair (720 hourly samples). z > 2 = strong inflow
(bearish), z < -2 = strong outflow (bullish), |z| < 0.5 = neutral. Stored
alongside raw because raw flows differ by 4+ orders of magnitude across pairs.

### Consumer — wired in `signals/engine.py` near line 390

Small additive bonus (±8) parallel to `foundation_bonus`:

```python
netflow_bonus = 0
try:
    _nf_z = float(r.get(f"{pair}:exchange_netflow_z") or 0.0)
    if _nf_z < -1.5:      # strong outflow (accumulation)
        netflow_bonus = 8 if direction == "long" else -8
    elif _nf_z > 1.5:     # strong inflow (sell pressure)
        netflow_bonus = 8 if direction == "short" else -8
except Exception:
    pass
```

Same scale as F50g foundation forecast — it's a directional confluence vote,
not a primary driver. Cold-start safe: missing Redis key → bonus=0.

### Kill switch + deadlock detector

Per memory rule [[rl-deadlock-detector]]: if `netflow:reject_count` /
`netflow:call_count` > 0.8 over 50+ calls, set `netflow:disabled=1`. Consumer
short-circuits to 0 when disabled.

### Celery beat

New entry in `celery_app.py`:
```python
"netflow_refresh": {
    "task": "data.onchain_netflow.refresh_all",
    "schedule": 300.0,  # every 5 minutes
}
```

5-min cadence is appropriate — exchange flows are slow-moving relative to
1m candles, and we're rate-limited at ~10 requests/min across free providers.

### F30 governance

Self-register at module load:
```python
register("F52", "Exchange Net-Flow Directional Gate", activation_phase=0)
```

### Config (`config.yaml`)

```yaml
netflow:
  enabled: true
  providers: [cryptoquant, glassnode, coinglass, coinmetrics]
  refresh_interval_s: 300
  rolling_window_hours: 720
  z_threshold_strong: 1.5
  z_threshold_extreme: 2.5
  bonus_strong: 8
  bonus_extreme: 12
```

## Implementation checklist (this session)

- [ ] `data/onchain_netflow.py` — provider polling + z-score
- [ ] `redis_keys.py` — 7 new keys
- [ ] `signals/engine.py` — netflow_bonus wired
- [ ] `feature_governance/bootstrap.py` — F52 entry
- [ ] `celery_app.py` — beat schedule
- [ ] `config.yaml` — netflow block
- [ ] `PROGRESS.md` cont. 55 — files modified table
- [ ] Dockerfile rebuild + container restart verified

## Cold-start behaviour

Until the first successful provider poll lands data in Redis, every pair
returns `netflow_bonus = 0`. Bot continues to function identically to
pre-F52 behaviour. **No regression risk.** Full benefit kicks in
automatically once the first 30-day z-score window fills (~30 days of
celery-beat runs, or one historical backfill query at startup).

## Honest scope (Rule 4)

- BTC, ETH, SOL, BNB, XRP: true per-asset net-flow signal.
- Remaining alts in the ACTIVE_PAIRS set: get a beta-correlated signal
  derived from BTC net-flow z × pair's 30d correlation to BTC. Marked in
  Redis as `{pair}:exchange_netflow_proxy=1` so the consumer halves the
  bonus weight (±4 instead of ±8) for proxy-derived pairs.
- 4h macro veto NOT implemented (matches F50b deferral pattern).
- Backfill of 30-day history at first run uses Glassnode community endpoint
  (free, 365d limit); failure → start with empty z-window and let it fill
  organically over 30 days.

## Session handoff

If implementation runs past session end:
- Files created — check `data/onchain_netflow.py`
- Redis keys produced — `redis-cli keys "*netflow*"`
- Consumer hook — `grep -n netflow_bonus signals/engine.py`
- Governance — `redis-cli get feature:F52:contribution`
- Health — `redis-cli get netflow:updated_at`

File cleared after Rule-2 verification post-implementation.
