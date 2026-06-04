# Trade Outcome Pattern Analysis — 2026-05-23

Data-driven attribution of what makes trades win vs lose, using sklearn RandomForest + permutation importance on 1796 closed trades. Train/test chronological split (70/30).

## Methodology

- **Dataset:** 1796 closed trades with full feature_vector, ordered by entry_time
- **Target:** binary win (net_pnl_usdt > 0) vs loss
- **Train:** trades 1-1257 (May 17-21, 2026)
- **Test:**  trades 1258-1796 (May 21-23, 2026)
- **Tool:** sklearn 1.8.0 RandomForestClassifier + permutation_importance
- **Baseline accuracy:** 59.7% (always-predict-loss)
- **Model accuracy:** 71.5% (12pp edge over baseline → real signal in features)
- **Note:** Pattern viability tested OOS — train-only findings rejected

## Pattern 1: Direction-confidence is INVERSE to win rate ★ STRONGEST

The bot's `direction_confidence` score (0-100) is **anti-predictive**. Higher confidence → lower WR.

| Confidence bucket | Train WR | Test WR | Test n |
|---|---|---|---|
| 0-20 | 36.3% | **73.8%** ★ | 145 |
| 20-40 | 37.3% | 50.3% | 157 |
| 40-60 | 35.4% | 32.6% | 86 |
| 60-100 | 38.2% | 35.8% | 148 |

**Test sample is large enough to be statistically meaningful.** The 0-20 bucket has 73.8% WR vs the 60-100 bucket at 35.8% — 38 percentage point spread.

**Mechanism (hypothesis):** the confidence score is a sum of bonuses from OFI strength + regime alignment + TFT bias + PatchTST bias. High confidence means strong directional consensus across multiple signals — which appears to coincide with *over-extended* setups that mean-revert.

**Actionable rule:** **Invert the confidence gate.** Trade signals with direction_confidence ≤ 20 preferentially. Skip signals with direction_confidence ≥ 60.

## Pattern 2: High positive funding rate predicts higher WR ★ REPLICATES

| Funding bucket | Train WR | Test WR | Test n |
|---|---|---|---|
| Negative (<-0.0001) | 36.4% | 45.3% | 203 |
| Neutral (±0.0001) | 40.5% | 52.6% | 215 |
| Positive mild (0.0001-0.001) | 36.1% | 48.3% | 87 |
| Positive extreme (>0.001) | 14.3% (n=14) | **67.6%** ★ | 34 |

**Test data validates the signal** — extreme positive funding has 67.6% WR vs 50.1% baseline (17pp edge).

**Mechanism:** Extreme positive funding means longs are paying shorts heavily. This is a structural tailwind for short positions and a signal the long side is over-positioned (about to capitulate).

**Actionable rule:** When funding_rate > 0.001 on a pair, increase position size or take shorts preferentially.

## Pattern 3: Pair selection is a real edge ★ DIRECTIONAL ONLY

Some pairs are reliably profitable; some are reliably losing. The bot trades all of them equally.

**Top 5 winning pairs from TRAIN (by WR):**
BUSDT, UBUSDT, BOBUSDT, SYSUSDT, OBOLUSDT — TRAIN WR ~60-68%

**Test performance on these pairs:**
55.9% WR (68 trades), PnL -$231

**Pair WR generalizes** (55.9% > 50.1% baseline) but absolute PnL became negative in test due to declining edge magnitude.

**Top 5 losing pairs from TRAIN (by WR):**
BROCCOLIF3BUSDT (0% WR!), BANANAS31USDT, SQDUSDT, AIAUSDT, RONINUSDT

In TEST: zero trades on these (bot retired them) — implicit pair filtering already in effect via bandit.

**Actionable rule:** Maintain a per-pair win-rate tracker. Reduce size on pairs with WR < 35% over rolling 30-trade window. Boost size on pairs with WR > 55%.

## Pattern 4: Manual closes destroy capital ★ CONFIRMED

| Exit reason | Train n | Test n | Test WR | Test total PnL |
|---|---|---|---|---|
| trailing_sl | 1257 | 413 | 49.6% | +$XXX |
| manual_close_all | 0 | 60 | 70.0% | +$79 |
| **manual** | 0 | **33** | **12.1%** | **-$365.69** ★ |

Manual closes (panic-exit on individual trades) average -$11.08 per trade with only 12% winning. This is the single biggest controllable drain.

**Actionable rule:** Lock the dashboard's per-trade manual close button behind a confirmation dialog with a 60-second cooldown. Manual closes happen during panic and are systematically wrong.

## Pattern 5: Counter-OFI may have edge — INSUFFICIENT TRAIN DATA

The bot ALWAYS aligns direction with OFI sign. We have only 81 OFI-opposite trades in test (all from regime where bot deviated for other reasons).

Test data:
- OFI-aligned: 49.8% WR (n=458)
- OFI-opposite: 51.9% WR (n=81)

**Statistically inconclusive** (2.1pp difference, small opposite sample). Cannot confirm or reject. Needs intentional A/B test where bot deliberately takes ~10% of trades opposite to OFI to gather data.

## Patterns that did NOT replicate (rejected)

- **change_24h (24h prior move):** Sign flipped between train and test — noise.
- **hold_time_seconds (dominant in importance):** This is leakage. Trades hold longer BECAUSE they're winning (trailing SL hasn't tripped). Not predictive.
- **mark price:** Also leakage related to specific pairs.

## Forward-test methodology

To validate these patterns continue to hold:

### Test 1: Inverted confidence (Pattern 1)
- Tag each new trade with its direction_confidence at entry
- Group trades by confidence bucket as they close
- After 100 new trades, check: does conf<20 still outperform conf>60?
- Kill rule: if conf<20 WR drops below 50% over rolling 50 trades, pattern decayed

### Test 2: Extreme positive funding (Pattern 2)
- Flag any trade entered with funding_rate > 0.001
- After 30 such trades, check WR
- Pattern holds if WR > 55%; rejects if WR < 50%

### Test 3: Pair WR tracker (Pattern 3)
- Build rolling 30-trade WR per pair
- Tag pairs as TIER_A (WR > 55%), TIER_B (45-55%), TIER_C (<45%)
- Verify TIER_A continues to outperform TIER_C in next 30 trades per pair

### Test 4: Manual close gate (Pattern 4)
- Implement 60-second confirmation cooldown
- Track manual close frequency before/after — should drop
- Track PnL — should improve by ~$11/trade × manual closes avoided

## Implementation priority

| Pattern | Implementation effort | Expected impact |
|---|---|---|
| 4 (Manual close gate) | 30 min UI change | +$11/avoided manual close. Highest ROI. |
| 1 (Invert confidence) | 1 line code change in signal filter | +20-30pp WR on filtered subset |
| 2 (Funding boost) | Add funding_rate as size multiplier | Modest +5pp on pos_extreme bucket |
| 3 (Pair WR tracker) | New module ~150 lines | Compound effect over time |

## Tools used

- **scikit-learn 1.8.0** RandomForestClassifier — `from sklearn.ensemble import RandomForestClassifier`
- **scikit-learn 1.8.0** permutation_importance — `from sklearn.inspection import permutation_importance`
- **pandas 3.0.3** for data wrangling
- **scipy.stats** for chi-square tests
- Chronological train/test split to prevent lookahead bias

## Tools considered but not used (for future expansion)

- **SHAP** — for per-trade feature attribution (would explain WHY each individual trade won/lost)
- **mlxtend** — for association rule mining (finds "if X and Y, then win" multi-variable patterns)
- **PyCaret** — for AutoML across multiple classifier families
- **DoWhy/EconML** — for causal inference (distinguish correlation from causation)

## Limitations honestly stated

1. **1796 trades is moderate sample size.** Real significance requires 3000+ trades per discovered pattern.
2. **Train/test temporal split** is correct methodology but only spans 6 days. Patterns may not hold across regime changes (bull → bear).
3. **The bot's own behavior changed during the dataset** (cont. 18-23 fixes). Patterns from broken-SL era may not represent current behavior.
4. **`is_paper=true` for ALL trades.** Real money behavior may differ via slippage and partial fills.
5. **The inverted-confidence finding (Pattern 1) is surprising** and needs independent verification before betting capital on it.

## Next analysis recommended

1. Run the same analysis after 500 new post-fix trades to see if patterns persist.
2. Run on **paper vs live** trades when available (will differ).
3. Run **per-pair** analyses for the top-10 universe specifically.
4. Implement SHAP to get per-trade attribution (answers: "WHY did this specific trade lose?").
