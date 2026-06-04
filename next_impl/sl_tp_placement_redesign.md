# SL / TP1 / TP2 Placement Redesign

**Created:** 2026-05-30 (cont. 65h)
**Status:** IMPLEMENTED cont. 65k (2026-05-31) — all 3 guards live in risk/manager.py +
engine.py call sites; verified bounded (TP1 0.75%/TP2 1.5% @20×, SL ceiling 4% notional,
mag clip 5%). Deployed via brain ./risk mount.
**Trigger:** Owner observation 2026-05-30 21:50 UTC — "SL/TP1/TP2 messed up, no matter how many times you try you can't fix this." Confirmed across open trades (live snapshot in §A).

> Per `feedback_blueprint_first` + `feedback_redesign_when_blueprint_fails`: this is a placement-side redesign, NOT another trailing-side patch. Every previous SL/TP fix (cont. 44, 53, 62, 62d, 64, 65, 65f, 65g) tuned the *trailing* surface (lock fraction, activation gate, ratchet paths). The placement surface — what initial SL and TP1/TP2 values get written at trade open — has never been bounded. This document fixes that.

---

## A. Observed chaos (live snapshot 21:50 UTC, 18 open trades, all 20× lev)

| Pair | TP1 cap% | TP2 cap% | SL cap% | Peak% | Failure mode |
|---|---|---|---|---|---|
| **TAUSDT** ×3 | 24–75 | 47–150 | **−244 to −247** | 5–8 | SL past liquidation; TP1 grotesque |
| **1MBABYDOGEUSDT** ×3 | 16–21 | 31–41 | −48 to −52 | 3–10 | "Normal" |
| **MORPHO/NEWT/KITE** | 15 | 30 | −49 to −50 | 3–9 | Normal |
| **MBOXUSDT** | 33.6 | 67.9 | +2.1 | 21 | TP1 too far; trail correctly armed |
| **ASTERUSDT** ×3 | 17–24 | 33–50 | +2 to +3 | 21–32 | TP1 variable; trail correct |
| **1INCHUSDT** | 15 | 30 | +1.2 | 12 | All correct |
| **ROSEUSDT** | 15 | 30 | +2.5 | 25 | All correct |

**Symptoms:**
1. **TP1 ranges 15 % → 75 % capital** across 20× lev trades — 5× variance.
2. **Initial SL ranges −48 % → −247 % capital** — TAUSDT is past the 100 % liquidation point (SL would never trigger; you'd be force-liq'd first).
3. **TP2 = 2 × TP1 distance** always — amplifies the variance.

## B. Root cause

`risk/manager.py:compute_tp_targets()` and `compute_initial_sl()` pass CandleNet `mag1`/`mag3` straight through with **no upper clamp**:

```python
# compute_tp_targets (line 350-351)
tp1 = entry_price * (1.0 + (abs(mag1_avg) / 100.0) * sign)
tp2 = entry_price * (1.0 + (abs(mag3_avg) / 100.0) * sign)

# compute_initial_sl mag-driven floor (line 167-170)
cn_mag_pct = _candlenet_avg_mag_pct(r, pair)
if cn_mag_pct > 0:
    mag_distance = mark * (cn_mag_pct / 100.0) * 0.5
    atr_distance = max(atr_distance, mag_distance)  # WIDER wins, no ceiling
```

On thin/new pairs (TAUSDT: VPIN=0.001, ATR=0.0004) CandleNet still emits predictions because the model always emits a number. Snapshot shows TAUSDT mags of `−8.7%`, `+16.6%`, `−19%` across TFs — these flow into both SL and TP without sanity.

`apply_capital_sl_floor` only enforces a **lower** bound (50 % capital min loss) — it never caps the upper bound. Same for `compute_tp_targets`.

## C. Proposal — three additive guards

### C.1 — Mag sanity clip (both functions)

Add at the top of `_candlenet_avg_mag_pct` and inside `compute_tp_targets`'s mag aggregation:

```python
_MAG_CLIP_PCT = 5.0     # CandleNet predictions > 5% on a single candle are noise
                        # rationale: BTC's biggest 1m candle in 2025 was 3.1%;
                        # any pair forecasting > 5% per candle is a model artifact
# in the per-TF loop, after parsing `m1`/`m3`:
if abs(m1) > _MAG_CLIP_PCT: continue   # skip this TF's contribution
if abs(m3) > _MAG_CLIP_PCT: continue
```

When all 3 TFs get clipped, `compute_tp_targets` falls through to its existing ATR-multiple fallback (line 326+). `compute_initial_sl` falls through to the vol-only path.

### C.2 — TP1/TP2 distance ceiling (compute_tp_targets)

Per-leverage ceiling table (sized so worst-case TP equals 30 % capital — the practical headroom for a quick exit before mean-reversion):

| Leverage | TP1 max notional | TP2 max notional | (= max % capital) |
|---|---|---|---|
| 1–5× | 5.0 % / 10.0 % | (25 %–50 %) |
| 6–10× | 3.0 % / 6.0 % | (18 %–60 %) |
| 11–20× | 1.5 % / 3.0 % | (30 %–60 %) |
| 21×+ | 1.0 % / 2.0 % | (≥30 %) |

```python
def _tp_distance_cap_notional(leverage: int) -> tuple[float, float]:
    if leverage <= 5:  return (0.05, 0.10)
    if leverage <= 10: return (0.03, 0.06)
    if leverage <= 20: return (0.015, 0.03)
    return (0.01, 0.02)

# After mag-driven tp1/tp2 computation, before return:
tp1_cap, tp2_cap = _tp_distance_cap_notional(leverage)
tp1_dist = abs(tp1 - entry_price) / entry_price
tp2_dist = abs(tp2 - entry_price) / entry_price
if tp1_dist > tp1_cap:
    tp1 = entry_price * (1.0 + tp1_cap * sign)
if tp2_dist > tp2_cap:
    tp2 = entry_price * (1.0 + tp2_cap * sign)
```

Note: `compute_tp_targets` currently doesn't receive `leverage` — needs signature change `(pair, direction, entry_price, leverage)`. Caller is `signals/engine.py:2289` (single site).

### C.3 — Initial SL distance ceiling (apply_capital_sl_ceiling)

Mirror of `apply_capital_sl_floor` but for the upper bound. Capped at **80 % of capital** — beyond that you'd be liquidated before the SL fires (100 % at 20× = 5 % notional move) so any SL wider is meaningless.

```python
def apply_capital_sl_ceiling(raw_sl, mark, direction, capital_usdt, leverage, r=None) -> float:
    """Hard upper bound on SL distance: never let initial SL place beyond
    `ceiling_frac` of capital (default 0.80). At 20× lev this is 4% notional
    — beyond which a 5% adverse move would liquidate the position before
    SL triggers anyway.
    """
    if mark <= 0 or capital_usdt <= 0 or leverage <= 0:
        return raw_sl
    ceiling_frac = float(r.get("risk:capital_sl_ceiling_frac") or 0.80)
    ceiling_frac = max(0.50, min(0.95, ceiling_frac))
    max_pct_notional = ceiling_frac / leverage
    max_distance = mark * max_pct_notional
    if direction == "long":
        ceiling_sl = round(mark - max_distance, 8)
        return max(raw_sl, ceiling_sl)   # NARROWER (closer to mark) wins
    else:
        ceiling_sl = round(mark + max_distance, 8)
        return min(raw_sl, ceiling_sl)
```

Call site: `signals/engine.py:2269`, immediately after `apply_capital_sl_floor`:

```python
initial_sl = apply_capital_sl_floor(initial_sl, mark_for_qty, ...)
initial_sl = apply_capital_sl_ceiling(initial_sl, mark_for_qty, ...)   # NEW
```

## D. Expected post-deploy state

Re-applied to today's snapshot:

| Pair | Before SL% | After SL% | Before TP1% | After TP1% |
|---|---|---|---|---|
| TAUSDT | **−247** | **−80** (ceiling) | **75** | **30** (ceiling) |
| MBOXUSDT | +2.1 | +2.1 (trail; no change) | 33.6 | **30** (ceiling) |
| ASTERUSDT | +2 to +3 | +2 to +3 (trail) | 17–24 | 17–24 (under cap) |
| 1MBABYDOGEUSDT | −48 to −52 | −48 to −52 (under cap) | 16–21 | 16–21 (under cap) |
| 1INCHUSDT | +1.2 | +1.2 (trail) | 15 | 15 (under cap) |

Net: only the actual outliers move; the well-behaved trades are untouched.

## E. Out of scope (intentionally)

- **TP1/TP2 lower bound** — current ATR fallback already provides a sensible floor; not blocking.
- **TP1 ≠ 0.75 % notional target** from `feedback_profit_lock` memory — that's the *empirical EV-maximising* target from cont. 65f, but enforcing it as a hard floor conflicts with this PR's cap-only philosophy. Park for separate decision.
- **CandleNet retraining** — the model produces unreliable mags on thin pairs. That's a deeper fix (training-data filtering) tracked in `f50e_continuous_candle_training.md`. This PR is a runtime safety net, not a CandleNet retrain.
- **Trailing-side behavior** — already fixed in cont. 65g (Paths D/E lock-cap + activation gate). Out of scope here.

## F. Checklist

- [ ] **Owner sign-off** on the three guards (mag clip 5%, TP table, SL ceiling 80%)
- [ ] Add `_MAG_CLIP_PCT` constant + per-TF clip in `_candlenet_avg_mag_pct` and `compute_tp_targets`
- [ ] Add `_tp_distance_cap_notional` helper + clamp in `compute_tp_targets`
- [ ] Change `compute_tp_targets` signature to accept `leverage` (caller: `signals/engine.py:2289`)
- [ ] Add `apply_capital_sl_ceiling` + call site at `signals/engine.py:2269`
- [ ] Backfill existing open trades (one-shot SQL UPDATE clamping SL/TP1/TP2 to the new caps) — OR let natural attrition replace them
- [ ] PROGRESS.md cont. 65h entry
- [ ] Rebuild brain + celery_worker + recreate
- [ ] Verify on next-cycle trades: max TP1 cap% ≤ 30, max |SL cap%| ≤ 80, no TAUSDT-style outliers

## G. Open questions

1. **Backfill or natural attrition?** Existing open trades have broken SL/TP; either UPDATE them in place (risk: SL might be tighter than mark already → would trigger immediate close) or let them close naturally. Recommendation: leave alone, only new trades get the cap.
2. **Per-leverage table** — owner may want different cuts (e.g., disable CandleNet TP entirely on thin-VPIN pairs).
3. **Mag clip threshold** — 5 % is hand-picked from BTC reference. Could be empirical-percentile (e.g. p99 of historical 1m candles) per pair.
