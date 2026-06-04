# Seed Archetypes: Volatility, Vol-of-Vol, Options-Derived & Vol-Regime Family

## Purpose
Seed gene-pool for the autonomous strategy-research engine. This file catalogs canonical strategies from the **volatility complex** that can be applied to **crypto perpetual futures** either as:
- **DIRECTIONAL** entries (the vol-signal implies a direction in the underlying perp), or
- **FILTER / SIZING / EXIT** overlays (the vol-signal gates or scales another strategy).

Crypto perps do not trade options natively, but the bot has access to:
- Deribit IV surface (ATM IV, 25Δ skew, 25Δ butterfly, term structure)
- DVOL index + decile
- Premium index, funding rate, mark-index basis
- Multi-TF ATR, BOCPD changepoint, HMM regime, Mamba quantile bands, conformal residual bands, GARCH scaffolding
- Open interest, GEX (computable from Deribit chain), vanna/charm proxies

## Sub-Family Index
1. **Vol Breakout / Squeeze** (compression-to-expansion)
2. **Vol Forecasting** (GARCH/HAR family, sizing & timing)
3. **IV-Level Regime Filters** (DVOL, IV rank/percentile)
4. **Skew & Risk-Reversal Signals** (directional bias from option surface)
5. **Term Structure** (front vs back IV, contango/backwardation roll)
6. **Variance Risk Premium** (IV vs RV divergence, vol carry)
7. **Dealer-Flow / Greeks Exposure** (GEX, vanna, charm, pin levels)
8. **Vol-of-Vol & Higher Moments** (kurtosis, butterfly, jump premium)
9. **Regime Detection & Changepoint** (HMM, BOCPD, conformal, Mamba bands)
10. **Vol-Targeting & Risk Overlays** (sizing, tail hedge, vol-pumping)

## Spec Format
`name | mechanism | required_data | complexity | crypto-perp_fit (1-5)`

---

## 1. Vol Breakout / Squeeze (compression → expansion)

1. `BB_KC_Squeeze | Long/short when Bollinger Bands exit Keltner Channel after BB-inside-KC compression. | OHLCV | low | 5`
2. `NR4_Breakout | Enter on break of NR4 candle's high/low (narrowest range in 4 bars). | OHLCV | low | 5`
3. `NR7_Breakout | Same as NR4 but 7-bar narrowest range; classical Crabel pattern. | OHLCV | low | 5`
4. `NR7ID | NR7 + inside-day double compression; tighter trigger, higher follow-through. | OHLCV | low | 4`
5. `ATR_Compression_Breakout | ATR(n) drops below z-score -1.5 then expands; trade direction of expansion bar. | ATR multi-TF | low | 5`
6. `Chaikin_Vol_Expansion | Chaikin Volatility (EMA of H-L range) crosses +threshold; trade momentum direction. | OHLCV | low | 4`
7. `BB_Width_Percentile_Squeeze | Enter when BB-width percentile < 5 over 120 bars, on break direction. | OHLCV | low | 5`
8. `Donchian_PostSqueeze | Donchian channel width < 20th pct then break; trade breakout. | OHLCV | low | 4`
9. `Vol_Regime_Shift_Breakout | DVOL decile jumps >=2 in a bar; trade in direction of accompanying perp tick. | DVOL decile | med | 4`
10. `ATR_Multi_TF_Alignment | All three ATR TFs (5m,1h,4h) expand simultaneously; trade dominant trend. | ATR multi-TF | low | 4`
11. `Inside_Bar_VolSqueeze | Inside-bar inside an NR7; break of NR7 high/low. | OHLCV | low | 4`
12. `Realized_Vol_Quantile_Breakout | 30-bar RV in bottom 10th pct; trade break of last 20-bar range. | OHLCV, RV | low | 5`

## 2. Vol Forecasting (GARCH / HAR family)

13. `GARCH11_Vol_Forecast_Sizing | Scale position by 1/sigma_hat from GARCH(1,1) one-step forecast. | OHLCV, GARCH scaffold | med | 5`
14. `EGARCH_Asymmetric_Sizing | EGARCH(1,1) captures leverage effect; size down on negative-shock-implied vol rise. | OHLCV | med | 4`
15. `GJR_GARCH_Filter | GJR-GARCH asymmetric forecast > threshold → block new longs (downside-vol regime). | OHLCV | med | 4`
16. `HAR_RV_Forecast | Corsi HAR-RV (daily, weekly, monthly RV lags) forecast → scale position. | 5m bars, RV | med | 5`
17. `HAR_RV_J_Jump_Filter | HAR with jump term (RV - bipower variation) > threshold → reduce size / skip. | high-freq returns | high | 4`
18. `HAR_CJ_Continuous_Jump | Split RV into continuous + jump components; trade only in continuous-dominated regimes. | high-freq returns | high | 3`
19. `Realized_GARCH_Hybrid | Realized GARCH using RV as exogenous; tighter forecast → sharper sizing. | OHLCV, RV | high | 3`
20. `Bipower_Variation_JumpFlag | BPV vs RV divergence flags jump; skip entry within N bars after jump. | high-freq returns | med | 4`
21. `Two_Scale_RV_Estimator | TSRV noise/jump-robust RV (10-30s sampling) feeds GARCH; cleaner forecast. | tick data | high | 3`
22. `MedRV_MinRV_JumpRobust | Median/min RV (Andersen-Dobrev-Schaumburg) for jump-robust vol regime classification. | high-freq returns | high | 3`
23. `Vol_Forecast_vs_IV_Divergence | If GARCH RV-forecast > Deribit ATM IV → buy perp (long vol via direction). | GARCH + DVOL/IV | med | 4`
24. `Vol_Forecast_Decay_Reversion | When forecast vol > spot vol by >2σ, expect reversion; size down longs. | GARCH | med | 3`
25. `Stochastic_Vol_Heston_Filter | Heston-style latent vol Kalman estimate; gate entries on low-state. | OHLCV, Kalman | high | 2`

## 3. IV-Level Regime Filters (DVOL, IV rank/percentile)

26. `DVOL_Decile_Filter_TrendBias | In DVOL decile <=3, run trend-follow; in decile >=8, switch to mean-revert. | DVOL decile | low | 5`
27. `IV_Rank_FadeHigh | IV rank > 70 → fade extension moves on perp (sell into rallies, buy dips). | DVOL or IV rank | low | 5`
28. `IV_Rank_TrendLow | IV rank < 30 → trend-follow breakouts; vol cheap, big moves likely undertraded. | DVOL or IV rank | low | 5`
29. `IV_Percentile_DualGate | Require IVR>30 AND IVP>50 to enable mean-reversion module. | DVOL series | low | 5`
30. `DVOL_Z_MeanReversion | DVOL z-score > +2 → fade next perp pop (vol-driven exhaustion). | DVOL series | low | 4`
31. `DVOL_Spike_LongUnderlying | DVOL jumps >20% intraday on price drop → buy perp (panic-bottom proxy). | DVOL ticks | med | 4`
32. `DVOL_Compression_BreakoutHunt | DVOL in bottom decile >= 5 days → arm breakout module. | DVOL daily | low | 5`
33. `IV_Regime_Position_Cap | Hard-cap leverage by IV regime tier (low/med/high IV → 5x/3x/1x). | DVOL | low | 5`

## 4. Skew & Risk-Reversal Signals (directional bias)

34. `RR25D_Bullish_Skew_Long | 25Δ RR (call-IV - put-IV) flips positive → long perp (call demand). | Deribit chain | med | 5`
35. `RR25D_Bearish_Skew_Short | RR25 sharply negative + still falling → short perp continuation. | Deribit chain | med | 5`
36. `RR25D_Extreme_Fade | RR25 z-score < -2 (puts very rich) → contrarian long perp. | Deribit chain | med | 5`
37. `Skew_Slope_Steepening_Short | Put-side skew steepens vs ATM > threshold → short perp (downside hedging surge). | IV surface | med | 4`
38. `Skew_Slope_Flattening_Long | Put skew flattens fast → fear unwinding, long perp. | IV surface | med | 4`
39. `Skew_vs_PriceDivergence | Price up, RR25 down (more put bid) → short perp on divergence. | IV surface + price | med | 5`
40. `Smile_Asymmetry_Index | Right-wing - left-wing IV slope; positive → call demand, long bias. | IV chain | med | 4`
41. `Skew_Regime_HMM | HMM on RR25 + ATM IV state vector; trade direction of regime transition. | IV chain, HMM | high | 3`
42. `RR_Funding_Divergence | RR25 bullish but funding negative → squeeze long entry. | IV + funding | med | 5`
43. `RR_FundingRate_Confluence | RR25 + funding same sign + extreme → trade against the crowd (squeeze trade). | IV + funding | med | 5`

## 5. Term Structure (contango / backwardation)

44. `IV_Term_Contango_TrendLong | Front IV < back IV (contango) + bullish micro-trend → long perp. | Term structure | med | 4`
45. `IV_Term_Backwardation_Fade | Front IV > back IV (backwardation = stress) → fade rallies / reduce longs. | Term structure | med | 5`
46. `IV_Term_Backwardation_LongBounce | Backwardation flips after spike → buy panic-bottom (VIX-curve analog). | Term structure | med | 5`
47. `IV_Slope_ZScore_Filter | Term-slope z-score gate: only trend when |z|<1. | Term structure | low | 4`
48. `Calendar_Vol_Spread_Signal | Front-back IV spread mean-reversion proxy → directional perp tilt. | Term structure | med | 3`
49. `DVOL_Futures_Curve_Roll | Sign of DVOL futures roll yield → trend (positive roll → short-vol regime → trend longs). | DVOL futures | med | 4`
50. `Vol_Curve_Inversion_Crash | Term inversion >5d → enter tail-hedge mode (cut leverage, widen stops). | Term structure | med | 5`
51. `Forward_Vol_Implied | Compute implied forward IV between two expiries; extremes → directional fade. | IV surface | high | 3`

## 6. Variance Risk Premium (IV vs RV)

52. `VRP_Long_ShortVol_Direction | IV - RV > +10pts (rich) → trend-with-perp (vol-sellers absorb dips). | IV + RV | med | 4`
53. `VRP_Inversion_LongUnderlying | RV > IV (negative VRP) → vol underpriced, expect explosive move; momentum entry. | IV + RV | med | 5`
54. `VRP_ZScore_Mean_Revert | VRP z-score extreme → expect convergence; fade implied. | IV + RV | med | 3`
55. `VRP_Carry_Sizing | Position size scaled by current VRP (rich vol → bigger trend size). | IV + RV | med | 4`
56. `RealizedVol_vs_GARCH_Spread | Spot RV - GARCH forecast > threshold → vol shock, reduce exposure. | RV + GARCH | med | 4`
57. `Implied_vs_Realized_Convergence | When IV converges down to RV → exit short-vol regime trades. | IV + RV | med | 3`
58. `Vol_Carry_Roll_Sign | Sign of (RV - IV) used as binary regime → trend filter on perps. | IV + RV | low | 4`

## 7. Dealer-Flow / Greeks Exposure (computed from Deribit chain)

59. `GEX_Positive_PinMode | Aggregate dealer GEX > 0 → mean-revert intraday around large-OI strike. | Deribit chain | high | 4`
60. `GEX_Negative_TrendAccel | Dealer GEX < 0 → momentum amplification; trade breakouts aggressively. | Deribit chain | high | 5`
61. `Gamma_Flip_Cross | Spot crosses gamma-zero level → regime change trade (direction of cross). | Deribit chain | high | 5`
62. `Vanna_Rally_LongUnderlying | IV drops + positive vanna exposure → dealers buy → long perp. | Deribit chain | high | 4`
63. `Charm_OpenDrift | Large positive charm into expiry weekend → trade morning drift. | Deribit chain | high | 3`
64. `Max_Pain_Drift | Spot drifts toward max-pain strike on expiry day; trade toward level. | Deribit chain | med | 4`
65. `Largest_OI_Pin_Magnet | Trade reversion to dense OI cluster strike when GEX>0. | Deribit chain | med | 4`
66. `Dealer_Hedge_Flow_Long | Compute estimated dealer delta change from IV move; trade with implied hedge flow. | Deribit chain | high | 3`
67. `Vol_Trigger_OptionsExpiry | Heightened vol window 24h pre-expiry → cut leverage, then trade post-expiry breakout. | Expiry calendar | low | 5`

## 8. Vol-of-Vol & Higher Moments

68. `VolOfVol_Spike_Fade | Realized vol of DVOL > +2σ → expect vol-vol mean reversion; fade directional perp extension. | DVOL series | med | 4`
69. `VolOfVol_Compression_Breakout | Vol-of-vol in bottom decile → arm breakout module (vol regime change imminent). | DVOL series | med | 4`
70. `Butterfly25D_Kurtosis_High | 25Δ butterfly IV spikes → fat tails priced in, fade extreme moves. | IV chain | med | 4`
71. `Butterfly_Compression_TailRisk | Butterfly compresses → tails underpriced; arm tail-hedge & breakout entries. | IV chain | med | 4`
72. `Jump_Risk_Premium_Long | Jump component of HAR-J > threshold → directional momentum, jump-trend. | high-freq returns | high | 3`
73. `Skewness_3rdMoment_Filter | Rolling 3rd-moment of returns < -threshold → skip longs (left-skew regime). | OHLCV | med | 3`
74. `Kurtosis_4thMoment_Sizing | Down-size by 1/kurtosis when fat-tail regime detected. | OHLCV | med | 3`
75. `Crash_Risk_Premium_Index | CRP = OTM-put IV - ATM IV; extreme → trade contra (panic-bottom long). | IV chain | med | 4`

## 9. Regime Detection & Changepoint

76. `HMM_3State_Regime_Trend | HMM(bull/bear/turbulent) state = bull → enable trend longs; turbulent → disable. | HMM regime | med | 5`
77. `HMM_TransitionProb_Entry | Enter on rising P(bull|state) > 0.7 with momentum confirmation. | HMM regime | med | 4`
78. `HMM_TurbulentExit | Force-exit all positions on transition into turbulent state. | HMM regime | low | 5`
79. `BOCPD_Regime_Reset | Changepoint posterior > 0.5 → flatten, re-evaluate; new regime trade. | BOCPD | med | 5`
80. `BOCPD_Hazard_Tuned_RegimeBreak | Tuned hazard rate, trade direction of post-changepoint drift. | BOCPD | high | 4`
81. `Conformal_Band_Breakout | Price exits upper 90% conformal band → momentum long; lower → short. | Conformal residuals | med | 5`
82. `Conformal_Band_MeanRevert | Price pokes outside 95% band then re-enters → fade (mean-revert). | Conformal residuals | med | 4`
83. `Conformal_BandWidth_Sizing | Position size inversely proportional to band width (narrower → bigger). | Conformal residuals | med | 5`
84. `Mamba_Quantile_Q90_Breakout | Price clears Mamba Q90 forecast → momentum long with Mamba target. | Mamba quantiles | high | 5`
85. `Mamba_Quantile_Q10_Bounce | Price tags Q10 quantile → buy bounce to median. | Mamba quantiles | high | 4`
86. `Mamba_BandWidth_VolRegime | Width of (Q90-Q10) as forecast vol proxy; gate strategies by it. | Mamba quantiles | med | 5`
87. `Mamba_vs_Realized_Divergence | Realized move outside Mamba band 2x → regime break, flip bias. | Mamba quantiles | high | 4`
88. `Composite_Regime_VPIN_DVOL | VPIN > 0.7 + DVOL z>1 → cut size; toxic-flow + vol regime composite. | VPIN + DVOL | high | 4`
89. `VPIN_Vol_Spike_Predictor | VPIN rises before vol → preempt by widening stops / cutting leverage. | order flow | high | 4`

## 10. Vol-Targeting, Carry & Overlays

90. `Vol_Target_Sizing | size = target_vol / realized_vol; constant-vol overlay on any base strategy. | RV | low | 5`
91. `Inverse_ATR_Sizing | Size = risk_$ / (k * ATR); classical Turtle-style. | ATR | low | 5`
92. `Vol_Parity_Pair_Sizing | Equal vol contribution across pairs in a basket of perps. | RV per asset | med | 4`
93. `Vol_Carry_Contango_ShortVol | DVOL futures contango → bias short-vol stance (favor mean-revert / trend in calm). | DVOL futures | med | 4`
94. `Vol_Carry_Backwardation_LongVol | DVOL backwardation → bias long-vol (favor breakout, reduce mean-revert). | DVOL futures | med | 4`
95. `Tail_Hedge_OverlayDecile | When DVOL decile>=9 cut leverage by 50%, widen stops by 1.5x. | DVOL decile | low | 5`
96. `Crash_Avoidance_RegimeKill | HMM=turbulent OR backwardation+VRP-negative → halt new entries. | HMM + term | low | 5`
97. `Shannon_Vol_Pumping_Rebalance | Fixed-weight rebalance between perp and stable; harvests vol drag. | balances | low | 3`
98. `Vol_Pumping_Pair_Rebalance | Same as 97 but two anti-correlated perps (e.g. BTC vs alt). | pair balances | med | 3`
99. `Vol_Adaptive_Stop | Stop = entry - k*ATR with k scaled by IV percentile. | ATR + IVP | low | 5`
100. `Vol_Adaptive_TP_Ladder | TP rungs sized in ATR units, multiplier rises with IV regime. | ATR + IVP | low | 5`
101. `Trailing_Stop_Vol_Aware | Trailing stop distance = max(ATR*k, 1σ Mamba band). | ATR + Mamba | med | 5`
102. `Premium_Index_Mean_Revert | Perp premium z>+2 → fade long; z<-2 → fade short (basis snap). | premium index | low | 5`
103. `Funding_Skew_Confluence_Long | Funding deeply negative + RR25 turning up → squeeze long. | funding + RR25 | med | 5`
104. `Funding_Skew_Confluence_Short | Funding deeply positive + RR25 turning down → blow-off short. | funding + RR25 | med | 5`
105. `Realized_Variance_Regime_KillSwitch | RV > 99th pct → flatten and pause N hours. | RV | low | 5`
106. `Vol_of_Funding_Regime | Vol-of-funding spike → unstable basis, reduce leverage. | funding series | low | 4`
107. `Cross_Asset_Vol_Spillover | BTC DVOL spike → preempt ETH perp position cuts. | multi-asset DVOL | med | 4`
108. `Vol_Smile_Shift_Translation | Smile minimum strike shifts right/left → directional bias on perp. | IV surface | high | 3`

---

## Notes on Mapping to Bot Data

- **DVOL + decile** → families 3, 5, 7 (carry), 9
- **ATR multi-TF** → 1, 10 (sizing), exit ladders
- **BOCPD** → 9 (regime resets)
- **HMM regime** → 9 (state filters / kill switches)
- **Mamba quantiles** → 9 (band trades), 10 (adaptive stops)
- **Conformal residuals** → 9 (band breakout / mean-revert)
- **GARCH scaffold** → 2 (forecast), 6 (VRP)
- **Premium index + funding** → 4/10 (skew-funding confluence)
- **Deribit IV surface** (requires connector) → 3, 4, 5, 6, 7, 8

## Deduplication Notes
- IV rank vs IV percentile collapsed into one dual-gate strategy (#29) plus the standalone fade/trend pair (#27/28).
- HAR variants (RV, RV-J, CJ) kept separate as they consume distinct data complexity.
- BB squeeze, BBW percentile, Donchian are kept as distinct triggers (different math).
- Vol-target sizing and ATR sizing kept separate (different denominators).
- Gamma-flip / GEX-positive-pin / Vanna rally are NOT collapsed — each has independent mechanism.
