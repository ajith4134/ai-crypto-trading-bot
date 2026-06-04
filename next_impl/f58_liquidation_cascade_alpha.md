# F58 — Liquidation Cascade Alpha

**Status:** SHIPPED cont. 56 (2026-05-28).

## Implementation summary
- `data/liquidation_levels.py` (~290 LoC). Native providers: Coinglass (if
  `COINGLASS_API_KEY` set) → Coinalyze (if `COINALYZE_API_KEY` set). Free
  tier endpoints, ~5-10 req/min.
- **Proxy fallback** (no API key required): synthesises a 2-cluster heatmap
  from funding_rate sign × open-interest-proxy × 3×ATR distance. Crude but
  better than nothing; flagged via `{pair}:liq_source=proxy`.
- **Cascade logic**: when one side's notional ≥ 2× the other AND price within
  1.5 % of that cluster → `cascade_direction` ∈ {long, short, neutral} +
  `cascade_prob` ∈ [0, 1].
- **Bonus**: ±18 (native source, direction matches) / ±12 (proxy, halved) /
  0 (neutral or insufficient prob). Cold-start: no liq data → 0.
- **Deadlock detector**: auto-disable when reject rate > 85 % over 30+
  calls (`liq:disabled`).
- Beat task: `liquidation-levels-refresh` (every 5 min) in `celery_app.py`.
- Consumer in `signals/engine.py` as `cascade_bonus` parallel to F50g.

## Rule 4 — what's intentionally simplified
- 2-cluster synthetic proxy heatmap; real heatmaps have 20+ clusters.
  Expect proxy Sharpe to be ~0.5-0.7 of native.
- Alts without Coinglass coverage silently use proxy; no per-pair quality
  tier yet.
- Add `COINGLASS_API_KEY` env var to upgrade weight from ±12 (proxy) to
  ±18 (native) — automatic, no redeploy needed.

## Session handoff
- Liq updated_at — `redis-cli get liq:updated_at`
- Per-pair source — `redis-cli get BTCUSDT:liq_source`
- Disabled flag — `redis-cli get liq:disabled`
- Governance — `redis-cli get feature:F58:contribution`

File cleared after Rule-2 verification post-implementation.
