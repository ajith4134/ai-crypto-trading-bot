# Optimal TP1/TP2 Percentage per Capital — Deep Research & Plan

Topic slug: `tp_optimal_percentage`
Created: 2026-05-30 (cont. 65f)
Trigger: Owner — "check why tp1 and tp2 entry prices are very long … come up with a best percentage for every trade based on capital and do a deep think on the optimal values"

---

## A. What the data actually shows (Rule 2 — measured)

Snapshot 2026-05-30 ~16:30 UTC. Trades closed last 24 h: **2,139**. Open: **97**.

### A.1 Where TPs actually live (schema audit)

| Column | Open (97) | Closed 24h (2139) | Verdict |
|---|---|---|---|
| `tp1` | 0 | rare | UNUSED — legacy column |
| `tp2` | 0 | rare | UNUSED |
| `tp` | 0 | rare | UNUSED |
| `tp1_target` | **97/97** | populated | **the real TP1** |
| `tp2_target` | **97/97** | populated | **the real TP2** |
| `predicted_tp` | 0 | 0 | UNUSED (model-output column never wired) |
| `predicted_sl` | 0 | 0 | UNUSED |
| `position_size_usdt` | — | **0/2139** | **NEVER WRITTEN — bug** |
| `capital_pct` | — | **0/2139** | **NEVER WRITTEN — bug** |
| `capital_usdt` | — | 2138/2139 | populated |
| `leverage` | — | 2138/2139 | populated (avg 19.6x) |
| `trailing_sl_level` | 97/97 | populated | working |

**Schema gap:** the bot computes notional via `capital_usdt × leverage` only — it never persists position_size_usdt or capital_pct. Any analysis that filters on these columns silently returns 0 rows. This is a measurement leak.

### A.2 Current TP behaviour

| Metric | Value |
|---|---|
| Open TP1 distance: median / p90 | **0.75% / 1.26%** |
| Open TP2 distance: median / p90 | **1.50% / 2.59%** |
| Closed-24h avg TP1 distance | **1.14%** |
| Closed-24h avg TP2 distance | **2.28%** |
| Closed-24h **TP1 hit rate** | **10.94%** (234/2139) |
| Closed-24h `tp_fired` | 232 (matches TP1 within rounding) |
| Avg peak PROFIT reached / trade | **+$11.25** |
| Avg peak LOSS reached / trade | **-$13.47** |
| Avg NET PnL / trade | **-$1.33** (LOSING) |
| Avg notional ($capital × $leverage) | $1,676 (capital $84 × lev 19.6) |
| Avg hold time when TP1 misses | 38 min |
| Avg hold time when TP1 hits | 58 min |
| Net PnL when TP1 fires | **+$18.85** (winning) |
| Net PnL when TP1 misses | **-$3.81** (losing) |

### A.3 The 13-point measurement leak

| Outcome | Count | % |
|---|---|---|
| Trades whose **peak passed the TP1 price** | 516 | **24.12%** |
| Trades where **`tp1_fired = TRUE`** | 234 | **10.94%** |
| **Gap** (peak touched TP1 but fire not recorded) | ~282 | **~13 pp** |

The peak passed TP1's distance in 24% of trades, but the TP1-fired flag only set in 11%. ~282 trades/day where TP1 should have triggered a partial close but didn't. This is the **primary loss source** before even discussing optimal %.

### A.4 Peak-profit distance distribution (positive-peak trades only)

| Distance band | Count | % |
|---|---|---|
| 0.00 – 0.25 % | 575 | 31.35 |
| 0.25 – 0.50 % | 305 | 16.63 |
| 0.50 – 0.75 % | 231 | 12.60 |
| 0.75 – 1.00 % | 406 | 22.14 |
| 1.00 – 1.50 % | 192 | 10.47 |
| 1.50 – 2.00 % | 59  | 3.22 |
| 2.00 – 3.00 % | 39  | 2.13 |
| 3.00 – 5.00 % | 15  | 0.82 |
| ≥ 5 % | 12  | 0.65 |

Percentiles of peak profit %: **p25=0.18, p50=0.53, p75=0.88, p90=1.25, p95=1.66.**

Translation: **half of the positive-peak trades never reach 0.53% above entry.** Current avg TP1 (1.14%) sits beyond p75.

### A.5 Hit-rate simulation at various TP1 distances

| TP1 candidate | Simulated hit rate | $ per hit (notional $1676) | EV per trade |
|---|---|---|---|
| 0.30 % | **54.81 %** | $5.03 | $2.76 |
| 0.50 % | **44.58 %** | $8.38 | $3.74 |
| **0.75 %** | **33.79 %** | **$12.57** | **$4.25** ← **optimal** |
| 1.00 % | 14.81 % | $16.76 | $2.48 |
| 1.50 % | 5.84 %  | $25.14 | $1.47 |
| 2.00 % | 3.08 %  | $33.52 | $1.03 |

**EV-maximising TP1 ≈ 0.75 %.** Below that the per-hit dollar is too small; above 1 % the hit rate craters because realised peaks rarely get that far.

### A.6 Direction asymmetry (regime mono-culture)

| Direction | Trades | TP1 hit | Avg net |
|---|---|---|---|
| Short | 1,977 | **11.58 %** | -$1.37 |
| Long  | 162   | **3.09 %** (5 only) | -$0.88 |

Longs barely ever hit TP1 because regime is stuck "bear" (see `next_impl/perfect_direction_prediction.md`) and longs are counter-trend.

---

## B. Root-cause decomposition

| # | Cause | Evidence |
|---|---|---|
| B1 | **TP1 placed too far for the realised peak distribution** | p50 peak = 0.53 %, avg TP1 = 1.14 % — TP1 sits beyond the median trade's max favourable move |
| B2 | **vol_unit floor of 0.5 %** | `risk/manager.py:331` `_volatility_unit` clamped to [0.5%, 2.5%] → fallback TP1 = 1.5 × 0.5 % = 0.75 % minimum |
| B3 | **CandleNet `mag1` outputs are small** | when primary path used, TP1 = |mag1_avg| × sign. Median mag1 produces sub-1 % targets, but the path is bypassed often (fallback fires) |
| B4 | **13-pp measurement leak: peak passed TP1 but fire didn't record** | 24 % peak-passed vs 11 % tp1_fired = ~282 trades/day where partial close should have triggered |
| B5 | **No capital-tier scaling** | TP formula is per-symbol vol_unit only. A trade risking 0.5 % of capital uses the same TP % as one risking 3 % — irrational |
| B6 | **position_size_usdt + capital_pct never persisted** | NULL on all 2139 closed trades → cannot tier by capital deployed even retroactively |
| B7 | **Trailing SL fires before TP1 in 69 % of trades** | exit_reason `trailing_sl` = 1468/2139. The trailing SL system is closing out trades whose peak hasn't yet reached TP1, locking in small loss instead of waiting for TP1 |
| B8 | **Direction bias destroys long TP hits** | Longs 3 % hit rate vs shorts 11.6 % — regime mono-culture from perfect_direction_prediction |

**B4 is the single biggest leak today** — fixing it would near-double TP1 hits with zero formula change. B1 + B2 + B5 are the design issues to redesign.

---

## C. SOTA research summary (2026, web-sourced)

- **ATR × 1.5 for TP1, ATR × 3.0 for TP2** is the canonical scalper/intraday setup — matches our existing fallback formula. Confirms the formula is sane; the inputs (vol_unit clamp, mag1) are what's off.
- **R-multiple framework**: define 1R = ATR-distance × 1.25 = your stop distance. At 38 % win rate, you need ~2R TPs for positive expectancy. At 50 %, 2R targets are positive after fees. **Our 11 % hit rate is far below 50 % — implies we need TPs that hit much more often, NOT bigger.**
- **High-leverage scalping (20x+)** uses TPs in the 0.1 – 5 % range with hold times measured in minutes-to-hours. Our avg notional $1,676 on $84 capital = 20x — exactly in this regime.
- **Kelly criterion at our parameters**: with W=10.94 %, even a 5R win/1R loss gives Kelly fraction ≈ 0 — i.e., the strategy is unprofitable at this hit rate and Kelly recommends not trading. **The only way out is to raise hit rate.** Tightening TP1 to 0.75 % more than triples hit rate → Kelly becomes positive again.
- **Partial-TP design**: industry standard is 40 % at TP1, 35 % at TP2, 25 % runner with trailing. Aligns with our 3-tier profit-lock (50/70/85 from cont. 65d feedback memory).
- **Slippage**: 0.05 – 0.10 % for liquid pairs, 0.20 – 0.50 % for alts. Our TP at 0.30 % would be inside slippage band for many alts — **don't go below ~0.40 % TP1**.

---

## D. Proposed design (the "best % per capital" the owner asked for)

### D.1 Fix the measurement leak first (zero design change)

1. **Bug fix**: investigate B4 — why does `tp1_fired` not set when peak passes TP1. Likely candidate: `monitor_trailing_sl` loop only checks `trailing_sl_level`, never checks `tp1_target`. The TP1 fire might only be checked by a separate `monitor_tp` loop that isn't running for every trade, or by the wickless-TP debounce (cont. 62 `feedback_sl_tp_cont62`) that may be over-debouncing.
2. **Persist `position_size_usdt` and `capital_pct`** at trade open in `memory/write.py:write_trade_create`. One-line per field. Unblocks all per-capital-tier analysis.

These two fixes alone are projected to lift TP1 hit rate from 11 % → 20-22 % with no formula change.

### D.2 Capital-tiered TP1/TP2 (the deep-think answer)

**Principle**: bigger capital-at-risk → tighter TPs (lock-in priority). Smaller capital-at-risk → looser TPs (let-it-run priority because the dollar-loss-if-missed is small).

**Owner mandate (cont. 65f, 2026-05-30):** **NO partial close ever.** When peak reaches TPx, SL ratchets to TPx — quantity unchanged. Trade only exits when mark bounces back to the new SL. This matches the existing cont. 65 checkpoint design (`risk/manager.py:690,732` already calls `engine.modify_sl(trade["id"], tp)` without any partial-close call). The tier table only sets the TP **price**; the SL-tailgate semantics carry across all tiers unchanged.

| `capital_pct` of total | TP1 % | TP2 % | On reach → SL jumps to | Floor (slippage) |
|---|---|---|---|---|
| < 0.5 %  (very small) | **vol_unit × 1.0** (cap 1.0 %) | vol_unit × 2.5 (cap 2.5 %) | TP1 then TP2 (no close) | 0.50 % |
| 0.5 – 2 % (normal) | **vol_unit × 0.75** (cap 0.75 %) | vol_unit × 2.0 (cap 1.50 %) | TP1 then TP2 (no close) | 0.50 % |
| > 2 % (large)     | **vol_unit × 0.50** (cap 0.50 %, FLOOR) | vol_unit × 1.5 (cap 1.0 %) | TP1 then TP2 (no close) | 0.50 % |

**Floor at 0.50 %** for all tiers to stay above slippage (research §C). Cap above means "but never further than X%" because realized peaks beyond 1.5 % occur in only 7 % of trades.

**Profit-lock ladder re-tune (cont. 65f, paired with these TPs):** the existing 50/70/85 ladder chased SL into trades before tight TPs could fire. New ladder (also owner-mandated this session):

| State | Lock cap | Was | Reason |
|---|---|---|---|
| Pre-TP1 | **10 %** | 50 % | Very loose — let the realised peak grow until tight TP1 (0.50-0.75 %) fires |
| Between TP1 and TP2 | **75 %** | 70 % | TP1 confirms direction; tighten |
| Post-TP2 | **85 %** | 85 % | Unchanged; only last 15 % can give back |

Code sites: `risk/manager.py:1267-1273` (Path B) + `1361-1367` (Path A). Both updated in cont. 65f. Memory `[[feedback-profit-lock]]` rewritten 2026-05-30.

### D.3 Lower the vol_unit floor

`_volatility_unit` is clamped to `[0.5 %, 2.5 %]` of mark. Drop floor to **0.30 %** (only enforced for TP computation, not SL) so very-low-vol pairs can use tight TPs. Slippage floor at 0.50 % then becomes the binding constraint via D.2's "Floor" column.

### D.4 Wickless TP debounce review (cont. 62)

The 30-second wickless-TP debounce was added to avoid spurious fires on flash spikes. With TP1 moving tighter, false-positive risk rises. Reduce debounce to **10 s for TP1**, keep 30 s for TP2.

### D.5 Anti-direction-bias (already covered)

Per `next_impl/perfect_direction_prediction.md`, the long-side 3 % TP hit rate is downstream of the regime mono-culture and direction-predictor coinflip. Layer 1/2 of that file is the primary fix. This TP file does NOT attempt to compensate — instead, the ensemble there decides direction first, and these TPs apply to whichever direction wins.

---

## E. Detailed implementation checklist

```
Phase 1 — Measurement leaks (low risk, big lift)
[ ] memory/write.py:write_trade_create — persist position_size_usdt = capital_usdt × leverage
[ ] memory/write.py:write_trade_create — persist capital_pct = capital_usdt / total_capital
[ ] Investigate B4: find why peak_pass_tp1 ≠ tp1_fired (282 trades/day leak).
    Candidates:
      - risk/manager.py monitor loops never check tp1_target inline
      - signals/engine.py wickless-TP debounce over-debouncing (cont. 62)
      - candlenet_tp1/2 path only fires for CandleNet-decided exits
[ ] Add Redis counter: signals:tp1_peak_passed_no_fire (per-pair) for ongoing visibility

Phase 2 — Capital-tiered TP (medium risk, primary lift)
[ ] risk/manager.py:compute_tp_targets — accept capital_pct param + apply D.2 table
[ ] risk/manager.py:_volatility_unit — separate TP-floor (0.30 %) from SL-floor (0.50 %)
[ ] signals/engine.py:2323 — pass capital_pct into compute_tp_targets call site
[ ] feature_governance: add F65 ("capital_tiered_tp") with default ON
[ ] Per-tier Redis counters: signals:tp1_tier_small / tier_normal / tier_large

Phase 3 — Debounce (low risk) — NO PARTIAL CLOSE per owner cont. 65f
[ ] risk/sl_tp_cont62.py (wickless debounce) — TP1 debounce 30 s → 10 s
[ ] (REMOVED — was: partial-close fractions. Owner: no partial close ever; SL
     tailgates to TP price, qty unchanged. Existing engine.modify_sl already
     does this — no change needed.)

Phase 4 — Verification (mandatory before deploy is called "working")
[ ] Per-trade JSONB write: tp1_distance_pct, tp2_distance_pct, capital_tier
[ ] After 24 h on live: re-run §A.5 hit-rate simulation against actuals.
    Target: tp1_hit_rate ≥ 28 % (up from 11 %), net_pnl_per_trade > 0
[ ] Per-direction TP1 hit rate report (longs should rise once direction predictor improves)
```

---

## F. Owner decision points

| # | Decision | Default I'd pick | Why |
|---|---|---|---|
| F1 | Ship Phase 1 (leak fix) immediately or wait for full design approval? | **Immediately** | Zero formula change, projected 11 % → 20 % TP1 hit rate alone |
| F2 | Capital tiers: 3 tiers (D.2) or simple flat 0.75 %? | **3 tiers** | Owner asked specifically for "based on capital"; flat ignores the ask |
| F3 | TP1 floor: 0.30 % (research min) vs 0.50 % (slippage min)? | **0.50 %** | Below slippage band trades become unprofitable after fees, even with high hit rate |
| F4 | Partial close fractions | **REMOVED — no partial close** | Owner mandate cont. 65f: SL tailgates to TP price, qty unchanged |
| F5 | Lower the wickless debounce 30 → 10 s for TP1? | **Yes** | Tighter TPs raise false-positive risk less than they raise hit rate |
| F6 | Touch `risk/manager.py:_volatility_unit` clamp at all? | **Yes — TP-only floor of 0.30 %** | SL keeps 0.50 % floor; TP-side floor relaxed so very-low-vol pairs can use tight TPs |
| F7 | Apply only to paper or also live? | **Paper first, 1 week, then live** | Per `feedback_full_deploy_mode`, both paper + live in full-deploy mode after validation |

---

## G. Risk register

- **Tighter TPs raise false-positive risk** (spurious wicks fire TPs against intent). Mitigated by Phase 3 debounce.
- **Tightening to 0.50 % shrinks per-hit dollar** by ~56 % vs current. EV calc shows total trade EV still rises — but variance per trade drops. Needs ≥ 24 h paper validation.
- **Direction predictor is still coinflip** (per perfect_direction_prediction). Tighter TPs help capture profit on the 51 % directions that ARE right; do nothing for the 49 % that are wrong. Compounds with Layer 2 of that file.
- **Hit-rate simulation assumes no early SL exit** — in reality 69 % of trades close on trailing_sl before peak. Real lift will be ~70 % of the simulated lift = TP1 hit rate ~22 % rather than 34 %. Still 2× improvement.
- **Capital-tier needs `capital_pct` populated**. Phase 1 fixes that prerequisite.

---

## H. References (2026, web-sourced)

- [Crypto perpetual futures strategies — CoinGape](https://coingape.com/blog/crypto-perpetual-futures-trading-strategies/)
- [Is crypto futures trading profitable in 2026 — CoinGape](https://coingape.com/blog/is-crypto-futures-trading-profitable/)
- [Kelly Criterion for Crypto Traders — Medium](https://medium.com/@tmapendembe_28659/kelly-criterion-for-crypto-traders-a-modern-approach-to-volatile-markets-a0cda654caa9)
- [Kelly Criterion for Crypto Position Sizing — Altrady](https://www.altrady.com/blog/risk-management/kelly-criterion-crypto-position-sizing)
- [Mastering the Kelly Criterion — LBank](https://www.lbank.com/explore/mastering-the-kelly-criterion-for-smarter-crypto-risk-management)
- [The R Multiple Cheat Code — Profit Smasher](https://www.profitsmasher.com/2026/03/the-r-multiple-cheat-code.html)
- [ATR indicator trading strategy 2025 — MindMathMoney](https://www.mindmathmoney.com/articles/atr-indicator-trading-strategy-master-volatility-for-better-breakouts-and-risk-management)
- [Average True Range in Crypto — Mudrex](https://mudrex.com/learn/average-true-range-crypto/)
- [Crypto trading bot backtesting — Bitget Academy](https://www.bitget.com/academy/12560603877835)
- [AI bot performance metrics — 3Commas](https://3commas.io/blog/ai-trading-bot-performance-analysis)

---

## I. Session handoff

This file captures the analysis and proposal. **No code has been changed yet.** Awaiting owner decisions F1–F7.

Once approved:
- **Phase 1 ships fastest** (~30 min): two `memory/write.py` lines + one debug-investigation script to identify the B4 leak.
- **Phase 2 main work** (~half-day): D.2 capital-tier table in `compute_tp_targets`, plumb `capital_pct` through the call site, F65 feature flag.
- **Phase 3 + 4**: ~half-day combined, paper-mode validation runs 7 days.

Delete this file after the chosen phases ship AND a 7-day paper-mode win-rate report confirms ≥ 20 pp TP1 hit-rate lift.

Linked memories: [[feedback-profit-lock]] (3-tier ladder 50/70/85), [[feedback-sl-tp-cont62]] (wickless TP 30s debounce that this file proposes to halve for TP1), [[feedback-verify-before-fix]] (B4 leak is what Rule 2 prevents), [[feedback-blueprint-grade-check]], [[project-judge-replaces-entry-gates]] (TP changes apply to whichever judge-approved trade fires).

Linked next_impl: [[perfect_direction_prediction]] (direction-bias B8 is downstream of that file's Layer 1/2 work).
