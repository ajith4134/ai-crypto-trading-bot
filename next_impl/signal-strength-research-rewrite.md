# Signal Strength Formula Rewrite — Research-Backed
## Date: 2026-06-07 | Author: Claude Code (Session)
## Target file: signals/engine.py (bind-mounted, restart only)

---

## Why the bot is unprofitable — root causes in the formula

From audit (2026-06-06):
- bear+short = -$3,507 on 4,173 trades ← formula ALLOWS these by scoring regime_score=20
- bull+short = +$1,098 (profitable! but formula scores regime_score=0, under-signals these)
- Stage 1 (simple) = +$6.15/trade vs Stage 4 (complex) = +$0.19/trade → COMPLEXITY HURTS

The signal formula has 6 identifiable bugs:
1. **Regime scoring is backwards for crypto**: bear+short scored 20 (too high) and bull+short scored 0 (too low). Empirical reality: bear+short is the #1 loss source, not because it's counter-trend but because crypto bear bounces destroy leveraged shorts.
2. **TFT ×5000 scaling bug**: `|tft_bias| × 5000` means a 0.02% forecast bias scores 100. Any bias > 0.02% hits the 100 cap and loses all gradient information.
3. **Sentiment ignores direction**: `|sentiment - 0.5| × 200` uses absolute deviation — a 0.9 bullish reading scores the same on a long OR short trade. Bearish sentiment SHOULD help a short trade, not penalize it.
4. **OFI and VPIN double-count order flow**: They measure the same underlying phenomenon (directional order imbalance). Combined weight 0.25+0.10=0.35 of the total double-counts this factor.
5. **CandleNet bonus→score mapping wastes probability info**: `50 + bonus×3` from discrete {+25, +10, 0, -15} votes throws away the continuous dir3 probability that the model actually outputs.
6. **hist_acc is a circular self-reference**: Past win rate reflects the bot's own behavior under prior faulty formulas, not market edge. Shrinkage toward 50% is more honest.

---

## Research sources integrated

- Cont, Cucuringu, Zhang (2023) — multi-level OFI via PCA, z-score normalization
- Easley, Lopez de Prado, O'Hara (2012) — VPIN as toxicity indicator (directional via net flow)
- Grinold (1994) — alpha = IC × vol × score_z; IC-weighted combination
- AlphaForge (arXiv:2406.18394, 2024) — ICIR weighting for signal combination
- Guo et al. (2017) — temperature scaling for neural network calibration
- arXiv:2601.06084 — funding rate mechanics in crypto perps
- THEORY vs EMPIRICAL note: research recommends direction-conditioned regime scoring (bear+short = high). Overridden by crypto empirical data showing bear+short systematically destroys leveraged shorts. Crypto-specific adjustment applied.

---

## Change 1: OFI Score (lines 697–701 in engine.py)

### Current
```python
if _new_ewma > 0:
    _ofi_mag_score = min(100.0, (_ofi_abs / _new_ewma) * 50.0)
else:
    _ofi_mag_score = 50.0
ofi_score = round(_ofi_mag_score * _ofi_align, 2)
```

### New — rolling z-score (Cont et al. normalized)
Add `{pair}:ofi_abs_std_ewma` Redis key alongside existing `{pair}:ofi_abs_ewma`.
Replace `magnitude × alignment_binary` with directional z-score:
```python
_ofi_std_key = f"{pair}:ofi_abs_std_ewma"
try:
    _prev_std_raw = r.get(_ofi_std_key)
    _prev_std = float(_prev_std_raw) if _prev_std_raw else _ofi_abs
    _variance_new = 0.1 * (_ofi_abs - _new_ewma) ** 2 + 0.9 * (_prev_std ** 2)
    _ofi_std_new = max(1e-10, _variance_new ** 0.5)
    r.setex(_ofi_std_key, 3600, _ofi_std_new)
except Exception:
    _ofi_std_new = _ofi_abs + 1e-10
# Directional z-score: positive when flow matches direction, negative when against
_ofi_z = (_ofi_abs - _new_ewma) / max(1e-10, _ofi_std_new)  # magnitude z-score
_ofi_dir_z = min(3.0, max(-3.0, _ofi_z)) * (1.0 if _ofi_align == 1.0 else -1.0 if _ofi_align == 0.0 else 0.0)
ofi_score = round(max(0.0, min(100.0, 50.0 + _ofi_dir_z * 15.0)), 2)
# Interpretation: 3σ aligned → score 95, on-mean → 50, 3σ against → score 5
```

---

## Change 2: VPIN Score (lines 756–759 in engine.py)

### Current
```python
vpin_score = min(100.0, (vpin / _v_new) * 50.0)
```

### New — toxicity-percentile (VPIN is confidence multiplier, not directional signal)
VPIN measures informed-trader activity. High VPIN amplifies the OTHER signals (OFI, CandleNet).
It should center at 50 (not reward/penalize direction on its own).
```python
# VPIN as informed-flow magnitude relative to pair baseline
# High VPIN = informed traders active = other signals are more reliable
# Center at 50: at baseline VPIN → 50; 2× baseline → ~75; 0.5× baseline → ~25
if _v_new > 0:
    _vpin_ratio = vpin / _v_new  # 1.0 = baseline, >1 = elevated
    vpin_score = round(max(0.0, min(100.0, 50.0 * _vpin_ratio)), 2)
else:
    vpin_score = 50.0
# Note: VPIN does NOT get direction-multiplied (it is regime-agnostic toxicity).
# Its weight is reduced and merged into flow_composite (see weight table).
```

---

## Change 3: Regime Score (lines 710–718 in engine.py) — MOST CRITICAL

### Current (BROKEN)
```python
if regime == "bull" and direction == "long":
    regime_score = 100.0
elif regime == "bear" and direction == "short":
    regime_score = float(_r_bs.get("signals:bear_short_regime_score") or 20.0)
elif regime in ("unknown", "turbulent"):
    regime_score = 50.0
else:
    regime_score = 0.0
```

### Why it is broken
- bear+short = 20 → still high enough to pass → 4,173 trades at -$3,507
- bull+short = 0 → profitable +$1,098 pair but under-signalled
- turbulent+long = -$185 (loss) but scores 50 — should be penalized
- turbulent+short = +$796 (good!) but scores 50 — same as above

### New — empirically calibrated for crypto perps
```python
# Regime × direction scoring: calibrated from 10,641-trade audit (2026-06-06).
# THEORY (research) says bear+short should score high (regime-aligned).
# CRYPTO EMPIRICAL REALITY: crypto bear bounces destroy leveraged shorts.
#   bear+short = -$3,507 on 4,173 trades; bull+short = +$1,098 on 2,372 trades.
# OVERRIDE: bear+short scored 0 (below minimum signal_strength threshold).
# Turbulent split: short+turbulent=+$796 (good), long+turbulent=-$185 (bad).
_REGIME_SCORES = {
    ("bull",       "long"):   88.0,  # primary edge: regime + direction + trend all aligned
    ("bull",       "short"):  60.0,  # counter-trend but empirically profitable; allow with good OFI
    ("bear",       "long"):   40.0,  # mean-reversion opportunity; cautious but allow
    ("bear",       "short"):   0.0,  # HARD ZERO: crypto bear squeezes destroy leveraged shorts
    ("turbulent",  "short"):  55.0,  # empirically +$796; volatility helps short entries
    ("turbulent",  "long"):   25.0,  # empirically -$185; turbulence favors short, not long
    ("unknown",    "long"):   42.0,  # slight caution in unknown regime
    ("unknown",    "short"):  42.0,  # symmetric for unknown
}
regime_score = _REGIME_SCORES.get((regime, direction), 40.0)
# Redis override still available for specific cells, e.g.:
#   redis-cli SET signals:regime_score:bear:short 0
_redis_override_key = f"signals:regime_score:{regime}:{direction}"
try:
    _r_ovrd = r.get(_redis_override_key)
    if _r_ovrd is not None:
        regime_score = float(_r_ovrd)
except Exception:
    pass
```

---

## Change 4: TFT Score (lines 720–726 in engine.py)

### Current (SCALING BUG)
```python
_tft_mag = min(100.0, abs(tft_bias) * 5000)  # ← 0.0002 bias = 100; scaling is broken
```

### New — EWMA std z-score (Cont et al. approach for signal normalization)
```python
if tft_bias != 0:
    _tft_std_key = f"{pair}:tft_bias_std_ewma"
    _tft_abs = abs(tft_bias)
    try:
        _tft_std_raw = r.get(_tft_std_key)
        _tft_std = float(_tft_std_raw) if _tft_std_raw else _tft_abs
        _tft_std_new = max(1e-8, 0.1 * _tft_abs + 0.9 * _tft_std)
        r.setex(_tft_std_key, 7200, _tft_std_new)
    except Exception:
        _tft_std_new = max(1e-8, _tft_abs)
    _tft_mag_z = min(3.0, _tft_abs / _tft_std_new)  # z-score, capped at 3σ
    _agrees = (direction == "long" and tft_bias > 0) or \
              (direction == "short" and tft_bias < 0)
    if _agrees:
        tft_score = round(min(100.0, 50.0 + _tft_mag_z * 16.7), 2)  # 3σ → 100
    else:
        tft_score = round(max(0.0, 50.0 - _tft_mag_z * 12.0), 2)   # 3σ against → 14
else:
    tft_score = 50.0
```

---

## Change 5: CandleNet Score (lines 736–741 in engine.py)

### Current (WASTES PROBABILITY OUTPUT)
```python
if candlenet_bonus > 0:
    candlenet_score = min(100.0, 50.0 + candlenet_bonus * 3)
elif candlenet_bonus < 0:
    candlenet_score = max(0.0, 50.0 + candlenet_bonus * 3)
else:
    candlenet_score = 50.0
```

### New — temperature-calibrated probability (Guo et al. 2017)
`_forecasts` dict already populated in the candlenet_bonus section above this.
Use `dir3` (3-candle ahead) probabilities directly — same signal that determines candlenet_bonus:
```python
# Use actual model probabilities instead of discrete vote → score mapping.
# dir3 = P(candle 3 ahead is bullish); temperature T=2.0 reduces overconfidence.
# (Guo et al. 2017: modern deep networks are overconfident; T scaling fixes this.)
_cn_aligned_probs = []
if '_forecasts' in dir():
    for _fc_v in _forecasts.values():
        _d3 = _fc_v.get("dir3", _fc_v.get("dir1"))
        if _d3 is not None:
            _prob = float(_d3)
            _aligned = _prob if direction == "long" else (1.0 - _prob)
            _cn_aligned_probs.append(_aligned)

if _cn_aligned_probs:
    import math as _math
    _cn_avg = sum(_cn_aligned_probs) / len(_cn_aligned_probs)
    # Temperature scaling T=2.0: logit / T → sigmoid
    _cn_logit = _math.log(max(1e-6, _cn_avg) / max(1e-6, 1.0 - _cn_avg))
    _cn_cal = 1.0 / (1.0 + _math.exp(-_cn_logit / 2.0))
    candlenet_score = round(max(0.0, min(100.0, _cn_cal * 100.0)), 2)
else:
    # Fallback to bonus mapping if forecasts missing
    candlenet_score = round(max(0.0, min(100.0, 50.0 + candlenet_bonus * 2.5)), 2)
```

---

## Change 6: Sentiment Score (line 705 in engine.py)

### Current (IGNORES DIRECTION)
```python
sent_score = round(min(100, abs(sentiment - 0.5) * 200), 2)
```

### New — direction-aligned (logical fix)
```python
# Sentiment ∈ [0,1]: 1.0 = max bullish, 0.0 = max bearish, 0.5 = neutral.
# For long trade: bullish sentiment is good → aligned = sentiment directly.
# For short trade: bearish sentiment is good → aligned = 1 - sentiment.
_sent_aligned = sentiment if direction == "long" else (1.0 - sentiment)
sent_score = round(max(0.0, min(100.0, _sent_aligned * 100.0)), 2)
# Neutral (0.5) → 50 for both directions. 1.0 bullish on a long → 100. On a short → 0.
```

---

## Change 7: hist_acc Score (line 743 in engine.py)

### Current (CIRCULAR SELF-REFERENCE)
```python
hist_score = _safe_dir_accuracy(r, pair, default=50.0)
```

### New — Bayesian Beta-Binomial shrinkage (James-Stein toward prior)
```python
# Beta-Binomial conjugate shrinkage toward 50% win rate.
# Prior pseudo-count k0=20 (= 20 imaginary trades at 50% win rate).
# Without trades: score = 50 (uninformed prior).
# With N=20 trades at 60% win: score = (12 + 10)/(20 + 20) × 100 = 55.
# With N=200 trades at 60% win: score = (120 + 10)/(200 + 20) × 100 = 59.
# Prevents overfit to small samples; converges to true win rate for large N.
try:
    _dir_acc_raw = r.get(f"{pair}:dir_accuracy")
    _dir_count_raw = r.get(f"{pair}:dir_count")
    _obs_rate = float(_dir_acc_raw) if _dir_acc_raw else 0.5
    _n = max(0, int(_dir_count_raw)) if _dir_count_raw else 0
    _k0 = 20  # prior pseudo-count
    _n_wins = round(_obs_rate * _n)
    _bayes_rate = (_n_wins + _k0 * 0.5) / (_n + _k0)
    _credibility = _n / (_n + _k0)  # 0 when no data → stays at 50
    hist_score = round(50.0 + (_bayes_rate - 0.5) * 100.0 * _credibility, 2)
    hist_score = max(0.0, min(100.0, hist_score))
except Exception:
    hist_score = 50.0
```

---

## Change 8: PatchTST Score (lines 729–734 in engine.py)

### Current
```python
if patchtst_bonus > 0:
    patchtst_score = min(100.0, 50.0 + patchtst_bonus * 10)  # +5 → 100 immediately
elif patchtst_bonus < 0:
    patchtst_score = max(0.0, 50.0 + patchtst_bonus * 10)    # -3 → 20
else:
    patchtst_score = 50.0
```

### New — change_pct z-score via EWMA std
`change_pct` is already available from the patchtst block above. Use it directly instead
of the discrete {+5/-3/0} bonus:
```python
# Use the continuous change_pct from PatchTST rather than discrete bonus.
# Normalize by EWMA std so 1σ predicted move = meaningful score deviation from 50.
try:
    _ptst_std_key = f"{pair}:ptst_chg_std_ewma"
    _ptst_abs = abs(change_pct) if 'change_pct' in dir() else 0.0
    if _ptst_abs > 0:
        _ptst_std_raw = r.get(_ptst_std_key)
        _ptst_std = float(_ptst_std_raw) if _ptst_std_raw else _ptst_abs
        _ptst_std_new = max(1e-8, 0.1 * _ptst_abs + 0.9 * _ptst_std)
        r.setex(_ptst_std_key, 7200, _ptst_std_new)
        _ptst_z = min(3.0, _ptst_abs / _ptst_std_new)
        _ptst_agrees = (direction == "long" and change_pct > 0) or \
                       (direction == "short" and change_pct < 0)
        if _ptst_agrees:
            patchtst_score = round(min(100.0, 50.0 + _ptst_z * 16.7), 2)
        else:
            patchtst_score = round(max(0.0, 50.0 - _ptst_z * 12.0), 2)
    else:
        patchtst_score = 50.0
except Exception:
    # Fallback to old bonus mapping
    if patchtst_bonus > 0:
        patchtst_score = min(100.0, 50.0 + patchtst_bonus * 6)
    elif patchtst_bonus < 0:
        patchtst_score = max(0.0, 50.0 + patchtst_bonus * 6)
    else:
        patchtst_score = 50.0
```

---

## Change 9: Weight Table (lines 767–787 in engine.py)

### Current weights (total = 1.08 normalized)
```
ofi:       0.25   vpin:       0.10   regime: 0.20
tft:       0.15   patchtst:   0.08   candlenet: 0.12
hist_acc:  0.13   sentiment:  0.05
```

### New weights — merge OFI+VPIN to prevent double-count (total = 1.00 flat)
```python
_components = [
    # "flow_composite" — merged OFI+VPIN (same underlying factor, prevent double-count)
    # OFI gets 0.25 (primary directional signal), VPIN drops to 0.08 (confidence modifier)
    # Effective: same 0.33 total flow weight but OFI leads
    ("ofi",        ofi_score,       0.25),  # keep lead weight
    ("regime",     regime_score,    0.22),  # up from 0.20: formula now correct
    ("tft",        tft_score,       0.15),  # keep
    ("candlenet",  candlenet_score, 0.12),  # keep
    ("hist_acc",   hist_score,      0.08),  # DOWN from 0.13: Bayesian shrinkage reduces noise
    ("patchtst",   patchtst_score,  0.08),  # keep
    ("vpin",       vpin_score,      0.06),  # DOWN from 0.10: toxicity modifier not direction signal
    ("sentiment",  sent_score,      0.04),  # keep (direction-aligned now, still low weight)
]
# Total: 1.00 exact — division still uses _total_w for safety
```

---

## What does NOT change

- `direction_conf` calculation: untouched (separate path)
- `candlenet_trend_ceil`: untouched (still uses bonus votes for ceilings)
- `candlenet_bonus`: untouched (votes still used for trend_ceil and exhaustion override)
- All gates and filters: untouched (PCG, microstructure veto, idiosyncratic gate, etc.)
- The `direction_confidence` computation (lines 806–818): untouched
- Stage 1 path (line 79): untouched (uses simple ofi_strength)

---

## Expected impact

| Change | Mechanism | Expected PnL impact |
|--------|-----------|---------------------|
| Regime score: bear+short → 0 | Hard-blocks a class losing $3,507 | +$1,000–$3,500 |
| Regime score: bull+short → 60 | Properly signals profitable class | +$200–$500 |
| TFT z-score fix | Restores gradient info for predictions | +$100–$300 |
| Sentiment direction-align | Stops rewarding wrong direction trades | +$50–$200 |
| OFI z-score | Cross-pair consistency, fewer false signals | uncertain |
| Bayesian hist_acc | Reduces noise/overfit | neutral–small |
| CandleNet temperature | Better calibrated probability scores | +$50–$200 |

Primary risk: bear+short = 0 means fewer trades → less data accumulation. Mitigated because
remaining trades should be higher quality.

---

## Deployment

1. Edit signals/engine.py (bind-mounted — no image rebuild needed)
2. `docker-compose restart brain`
3. Monitor for 100 trades before assessing: `redis-cli HGETALL brain:stats`
4. Watch bear+short counter: `redis-cli GET brain:pnl:bear:short`
5. Shadow Mode gate: 50 shadow cycles before evaluating overall PnL (Rule 14)

---

## Deferred to Phase 2 (require data accumulation first)

- ICIR-weighted combination: needs 90 days of per-signal IC tracking
- Full HMM regime probability: needs retraining with new formula
- Full VPIN volume-bucket approach (Easley 2012 canonical): needs raw tick data
- Funding rate as scored component: partially in code as gate, leave as gate
- Temperature calibration tuning: need 500+ trades to estimate T properly
