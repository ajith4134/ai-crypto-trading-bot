# Seed Archetypes: Trend-Following, Momentum & Breakout Family

## Purpose
Canonical archetype seed pool for the autonomous strategy-research engine. Each entry is a deduplicated *idea*, not a parameterisation — variants of the same mechanism (e.g. EMA-20/50 vs EMA-10/30) collapse to one row. The research engine is expected to grid-search params, regime-gate, and recombine these.

## Family Scope
This file covers the **trend / momentum / breakout** super-family only. Mean-reversion, market-making, stat-arb, options-vol, and event-driven are tracked in separate seed files.

## Sub-families (catalog organisation)
1. **Cross-sectional momentum** (rank-based, relative-strength)
2. **Time-series momentum / absolute momentum** (own-asset trend)
3. **Moving-average systems** (cross, slope, envelope)
4. **Channel / volatility breakout** (Donchian, Keltner, Bollinger, NR, ORB)
5. **Trend-strength / directional indicators** (ADX, SAR, Ichimoku, Supertrend)
6. **Volume-confirmed trend** (OBV, CMF, VWAP, A/D)
7. **Multi-timeframe / composite trend** (Elder triple-screen, stage-analysis, scorers)
8. **Pullback / continuation entries** (Holy Grail, ABC, 2B, Wolfe)
9. **Crypto-native trend** (funding, basis, dominance, halving, liquidations, on-chain)
10. **Adaptive / regime-aware trend** (vol-target, Hurst-gated, HMM-conditioned, Kelly-sized)
11. **ML / DL augmented trend** (PatchTST, TFT, GNN, Mamba, CandleNet, ensemble)

## Schema
`# | name | mechanism | required_data | complexity | crypto-perp_fit (1-5)`

Complexity: L=low, M=medium, H=high. Fit: 1=poor, 5=excellent on crypto perps.

---

## 1. Cross-Sectional Momentum

1 | xsec_momentum_topk | Rank universe by N-period return, long top-K / short bottom-K rebalanced periodically (Jegadeesh-Titman) | OHLCV multi-asset | L | 4
2 | dual_momentum_antonacci | Pick top relative-momentum asset only if its absolute 12m return > risk-free, else flat / cash | OHLCV multi-asset | L | 4
3 | residual_momentum | Rank assets on momentum of return *residuals* after stripping market+sector beta, long winners / short losers | OHLCV + factor model | M | 3
4 | risk_adjusted_momentum_sharpe | Rank on past Sharpe (mean/vol) rather than raw return; long top / short bottom | OHLCV multi-asset | L | 4
5 | intermediate_momentum_novy_marx | Use return from t-12 to t-7 months (excluding recent 6m) to avoid short-term reversal contamination | OHLCV multi-asset | L | 3
6 | industry_sector_momentum | Compute momentum at sector/category level (L1s, DeFi, memes, AI tokens) and rotate into top sector basket | OHLCV + sector tags | M | 5
7 | factor_momentum_ehsani | Rank style factors (value, quality, low-vol) by trailing returns, tilt portfolio to winning factor | OHLCV + factor returns | H | 2
8 | xsec_volatility_scaled_momentum | Inverse-vol-weight the cross-sectional long/short book so each leg contributes equal risk | OHLCV + realized vol | M | 4
9 | beta_neutral_momentum | Long top momentum, short bottom momentum, then beta-hedge with BTC index futures | OHLCV + BTC beta | M | 4
10 | xsec_funding_momentum | Rank perps by funding-rate trend (slope of funding); long compressing-funding alts, short blowing-out funding | Funding rate multi-asset | M | 5

## 2. Time-Series / Absolute Momentum

11 | tsm_12_1 | Long if past 12-1m return > 0, short if < 0 (Moskowitz-Ooi-Pedersen canonical) | OHLCV daily | L | 4
12 | tsm_short_horizon | Same as TSM but on 1-30 day lookback for intraday/swing crypto | OHLCV 1h/4h | L | 5
13 | sign_of_return_voting | Ensemble vote across N lookbacks (1w/1m/3m/6m); position = avg sign | OHLCV | L | 4
14 | roc_threshold | Trade when rate-of-change exceeds an ATR-scaled or percentile threshold | OHLCV + ATR | L | 4
15 | regression_slope_trend | Fit OLS line on log-price over rolling window, trade sign of slope filtered by R² | OHLCV | L | 4
16 | mansfield_relative_strength | Trade asset's relative strength vs benchmark (BTC, total market cap), enter on RS new high | OHLCV + benchmark | L | 4

## 3. Moving-Average Systems

17 | sma_dual_cross | Classic two-SMA cross (fast above slow = long) | OHLCV | L | 3
18 | ema_dual_cross | Same as SMA but exponential weighting | OHLCV | L | 3
19 | triple_ma_alignment | Long only when fast > mid > slow MA stacked correctly (3-MA ribbon) | OHLCV | L | 4
20 | ma_slope_trend | Trade sign of MA's first derivative rather than crosses (slope > 0 long) | OHLCV | L | 4
21 | price_above_ma_filter | Pure binary regime filter: only allow longs when price > Nth MA | OHLCV | L | 4
22 | guppy_mma_ribbon | Two ribbons of 6 short EMAs + 6 long EMAs; trade when fast ribbon decisively crosses slow ribbon | OHLCV | M | 4
23 | hull_ma_trend | HMA reduces lag via weighted/sqrt smoothing; trade HMA slope flips | OHLCV | L | 4
24 | kama_adaptive_trend | Kaufman AMA adapts smoothing to efficiency ratio; trade KAMA direction | OHLCV | M | 4
25 | vidya_chande_trend | Variable Index Dynamic Average uses CMO-driven adaptive alpha; trade slope | OHLCV | M | 3
26 | frama_fractal_ma | Fractal Adaptive MA scales alpha to fractal dimension; trade cross/slope | OHLCV | M | 3
27 | tema_dema_trend | Triple/Double EMA reduces lag, trade cross or slope flips | OHLCV | L | 4
28 | t3_tillson_trend | T3 = chained EMAs with volume factor; smoother trend signal with low lag | OHLCV | M | 3
29 | alma_gaussian_trend | Arnaud Legoux MA with Gaussian filter shifted toward recent prices, trade slope | OHLCV | L | 4
30 | zlema_zero_lag_trend | Zero-Lag EMA subtracts lag term; trade cross with slow EMA | OHLCV | L | 4
31 | jurik_jma_trend | Jurik MA proprietary smoothing; trade direction flips (very low lag) | OHLCV | M | 3
32 | mcginley_dynamic | Self-adjusting MA that speeds up/slows down based on price velocity; trade slope | OHLCV | M | 3

## 4. Channel & Volatility Breakout

33 | donchian_breakout_classic | Buy N-bar high / sell N-bar low (Richard Donchian 4-week rule, Turtle seed) | OHLCV | L | 5
34 | turtle_system | Donchian 20-entry / 10-exit with ATR position-sizing and pyramiding (Dennis/Eckhardt) | OHLCV + ATR | M | 5
35 | keltner_channel_breakout | Enter on close beyond EMA ± multiple × ATR envelope | OHLCV + ATR | L | 5
36 | bollinger_band_breakout | Enter on close outside upper/lower σ-band of price (volatility expansion) | OHLCV | L | 4
37 | bollinger_keltner_squeeze_ttm | Squeeze when BB is inside KC; fire when BB expands out (TTM Squeeze) | OHLCV + ATR | M | 5
38 | nr4_nr7_breakout | Narrowest-range 4 or 7 bar setup → trade breakout of NR-bar high/low (Crabel) | OHLCV | L | 5
39 | inside_day_breakout | Trade breakout of an inside-bar's high/low | OHLCV | L | 4
40 | opening_range_breakout_orb | Mark high/low of first N minutes (5/15/30/60), trade break of that range | OHLCV intraday | L | 5
41 | session_breakout_london_ny | ORB anchored to specific session open (London 08:00 GMT / NY 13:30 GMT) | OHLCV intraday + session clock | L | 4
42 | asian_range_breakout | Trade break of Asian-session high/low at London open | OHLCV intraday + session clock | L | 4
43 | weekly_monthly_breakout | Breakout of prior week's or month's high/low (longer-horizon Donchian) | OHLCV daily | L | 4
44 | darvas_box_breakout | Stack of consolidation boxes built from new 52w highs; buy each top, trail stop to box base | OHLCV daily | L | 3
45 | atr_channel_breakout | Trade close beyond N×ATR distance from prior close (pure volatility breakout) | OHLCV + ATR | L | 5
46 | starc_band_breakout | Stoller Average Range Channel breakout; SMA ± multiple × ATR with different multiplier scheme | OHLCV + ATR | L | 4
47 | momentum_pinball_raschke | Connors-Raschke ROC + first-hour-range setup for next-day directional pop | OHLCV intraday | M | 3
48 | range_expansion_index_tirone | Trade breakouts only after a range-expansion-index spike (signals volatility regime change) | OHLCV | M | 4

## 5. Trend-Strength & Directional Indicators

49 | adx_dmi_trend | Trade DI+/DI- cross only when ADX > threshold (e.g. 25); skip in chop | OHLCV | L | 4
50 | aroon_cross | Long when Aroon Up > Aroon Down and Up > 70; short symmetric | OHLCV | L | 3
51 | vortex_cross | Long on +VI / -VI cross with positive trend confirmation | OHLCV | L | 4
52 | supertrend_flip | ATR-based trend line that flips sides; trade each flip as new direction | OHLCV + ATR | L | 5
53 | parabolic_sar_flip | Trade SAR dot flip from above-price to below-price (and vice versa) | OHLCV | L | 3
54 | ichimoku_kumo_breakout | Long when price closes above Kumo cloud with Tenkan>Kijun and Chikou confirmation | OHLCV | M | 4
55 | trix_zero_cross | Triple-smoothed momentum oscillator; trade zero-line cross / signal-line cross | OHLCV | L | 3
56 | chande_momentum_cmo_trend | Use CMO sign with abs(CMO)>threshold as trend filter | OHLCV | L | 3
57 | qqe_smoothed_rsi_trend | QQE smooths RSI with double-EMA + ATR bands; trade band cross | OHLCV | M | 3
58 | schaff_trend_cycle | MACD wrapped in a stochastic for fast/cyclic trend signal; trade cross of 25/75 | OHLCV | M | 4
59 | macd_signal_cross | Classic MACD line cross of signal line as trend trigger | OHLCV | L | 4
60 | macd_zero_line_cross | Trade MACD line crossing zero (slower but cleaner) | OHLCV | L | 4
61 | macd_histogram_slope | Trade sign change of MACD histogram (earliest momentum-of-momentum signal) | OHLCV | L | 3
62 | tsi_trend | True Strength Index double-smoothed momentum; trade signal-line cross | OHLCV | L | 3
63 | kst_know_sure_thing | Pring's weighted sum of 4 smoothed ROCs; trade signal-line cross | OHLCV | M | 3
64 | coppock_curve_long_only | Long when Coppock crosses up from negative; long-cycle bull-market re-entry signal | OHLCV daily | L | 3
65 | cci_zero_line_trend | Trade CCI crossing ±100 as trend continuation (not reversal) | OHLCV | L | 3

## 6. Volume-Confirmed Trend

66 | obv_trend_confirm | Long only when OBV is making new highs in sync with price | OHLCV | L | 4
67 | accumulation_distribution_line | Trade AD-line breakouts and divergences against price | OHLCV | L | 3
68 | chaikin_money_flow_trend | Long when CMF > 0 and price above MA (volume-weighted trend filter) | OHLCV | L | 4
69 | vwap_trend_session | Long above session VWAP, short below; trade VWAP reclaims as entries | OHLCV intraday | L | 5
70 | anchored_vwap_trend | Anchor VWAP to swing high/low/event; trade reactions / reclaims of anchor | OHLCV + anchor events | M | 5
71 | volume_weighted_macd | MACD computed on volume-weighted prices for trend confirmation | OHLCV | M | 3
72 | klinger_volume_oscillator | KVO signal-line cross to confirm long-term volume trend | OHLCV | M | 2
73 | force_index_elder | EMA of (price-change × volume); trade zero-line cross as trend trigger | OHLCV | L | 3
74 | cvd_divergence_trend | Use cumulative volume delta direction to confirm or fade price trend | Trades / aggregated CVD | M | 5
75 | obi_pressure_trend | Persistent order-book imbalance (bid>ask by threshold) entries in trend direction | L2 order book | M | 5
76 | microprice_drift_trend | Trade slow drift of microprice vs midprice as directional pressure indicator | L2 order book | H | 5

## 7. Multi-Timeframe / Composite Trend

77 | elder_triple_screen | Higher-TF trend + intermediate momentum oscillator + lower-TF entry trigger | OHLCV multi-TF | M | 4
78 | mtf_ma_stack_confirm | Require MA alignment on 3+ timeframes (e.g. 4h + 1h + 15m) before entry | OHLCV multi-TF | L | 5
79 | weinstein_stage_analysis | Trade only Stage-2 (price > flat-turning 30wk MA + volume expansion) | OHLCV weekly + volume | M | 3
80 | minervini_sepa_template | Stage-2 with 8-point template: price > 50/150/200 MA stack, within 25% of 52w high, RS rank top | OHLCV + RS rank | M | 3
81 | wyckoff_markup_phase | Identify accumulation → spring → markup; long on sign-of-strength after spring | OHLCV + volume structure | H | 3
82 | trend_strength_composite_scorer | Z-score blend of ADX + slope + R² + above-MA + OBV → trade only top-decile scores | OHLCV multi-indicator | M | 4
83 | renko_atr_trend | Use ATR-sized Renko bricks; trade run of N consecutive same-color bricks | OHLCV + ATR | L | 4
84 | heikin_ashi_trend | Long while HA candles stay bullish (no upper wick); exit on first opposite HA close | OHLCV | L | 4
85 | range_bar_breakout_trend | Use fixed-range bars instead of time bars; trade N-bar breakouts | Trade ticks | M | 3
86 | market_profile_value_area_trend | Long when price accepts above prior VAH (Value Area High) on follow-through | OHLCV + volume profile | M | 3
87 | poc_migration_trend | Trade direction of POC (Point of Control) drift across sessions/days | Volume profile | M | 3

## 8. Pullback & Continuation Entries

88 | holy_grail_raschke | ADX(14) > 30 + pullback to 20-EMA → enter on break of trigger-bar high | OHLCV + ADX | L | 4
89 | abc_correction_pullback | Wait for 38.2-62% Fib pullback inside trend, enter on break of correction high | OHLCV + Fib | M | 3
90 | two_b_test_failure | Failed retest of prior swing high/low in trend direction → continuation entry | OHLCV swings | M | 3
91 | wolfe_wave_continuation | Wedge with 5-point structure; enter on break toward EPA target line | OHLCV swings | H | 2
92 | fib_ema_confluence_pullback | Long pullback when Fib retracement aligns with 20/50 EMA in established trend | OHLCV + Fib + EMA | L | 4
93 | flag_pennant_continuation | Trade breakout of bull/bear flag or pennant in trend direction | OHLCV pattern | M | 4
94 | cup_and_handle_breakout | Long break of handle rim in cup-and-handle pattern (O'Neil) | OHLCV daily | M | 3
95 | three_pushes_continuation | Trade pullback after 3 momentum pushes in trend (continuation, not exhaustion) | OHLCV swings | M | 3
96 | gartley_butterfly_with_trend | Harmonic pattern entries filtered to direction of higher-TF trend | OHLCV + harmonic detection | H | 2

## 9. Crypto-Native Trend

97 | funding_aligned_momentum | Long-trend signals only when funding < cap and same-sign (avoid extreme-funding tops) | Funding + OHLCV | M | 5
98 | funding_flip_continuation | Funding flipping from negative to positive (or vice versa) as confirmation of new trend leg | Funding history | M | 5
99 | basis_premium_trend | Trade direction of futures-spot premium index slope (CME/Deribit basis carry) | Premium index, spot, futures | M | 4
100 | btc_dominance_rotation | Rotate between BTC and alt-basket based on BTC.D regime (above 200d MA → BTC, below → alts) | BTC.D index + alt baskets | M | 5
101 | eth_btc_ratio_trend | Long ETH/short BTC perp when ETHBTC trends up across MA stack, reverse on flip | OHLCV ETHBTC | L | 5
102 | alt_season_index_trigger | Activate alt-momentum sleeve only when Altcoin Season Index > 75 | Alt season index | L | 5
103 | halving_cycle_phase_overlay | Use post-halving month-count as regime gate (markup vs distribution) on trend signals | Halving date + price | L | 3
104 | mvrv_zscore_regime_trend | Only run long trend in MVRV-Z accumulation zone, short in distribution zone | MVRV-Z on-chain | M | 4
105 | nupl_regime_trend | NUPL phase (hope/optimism/greed/euphoria) as trend-aggression scaler | NUPL on-chain | M | 4
106 | exchange_netflow_trend | Sustained negative exchange netflow (coins leaving exchanges) as bullish trend filter | On-chain netflow | M | 4
107 | liquidation_cascade_continuation | Front-run liquidation cluster: enter trend direction *into* cluster, exit at flush | Coinglass liq map + OI | H | 5
108 | open_interest_expansion_trend | Long trend continuations only when OI expands with price (new money confirming trend) | OI series + price | L | 5
109 | long_short_ratio_contrarian_trend | Fade extreme long/short ratio in direction of trend (crowded shorts = squeeze fuel) | LS-ratio per exchange | M | 4
110 | sentiment_score_aligned_trend | Take trend signal only when CryptoBERT/FinBERT sentiment z-score agrees in sign | News + price | M | 4

## 10. Adaptive / Regime-Aware Trend

111 | vol_target_trend_overlay | Scale any trend signal by (target_vol / realized_vol) for constant risk contribution | Realized vol + signal | L | 5
112 | atr_position_sized_trend | Position size = risk$ / (k × ATR); equalises $-risk per trade across volatility regimes | ATR + signal | L | 5
113 | half_kelly_trend_sizer | Position size = 0.5 × Kelly fraction derived from rolling win-rate and payoff | Trade history | M | 4
114 | hurst_gated_trend | Run trend strategies only when rolling Hurst exponent > 0.55 (persistent regime) | Price history (Hurst calc) | M | 4
115 | hmm_regime_conditioned_trend | Trade trend in bull/calm HMM state, flat or hedged in turbulent state | HMM regime tags | H | 4
116 | atr_trailing_stop_chandelier | Chandelier exit: trailing stop = highest-high − k×ATR; pure trend-rider exit module | OHLCV + ATR | L | 5
117 | parabolic_acceleration_exit | Tighten stops parabolically with trend age to lock gains in late-cycle moves | OHLCV + trade age | M | 4
118 | regime_switch_trend_mr | Trend logic when ADX>25 or Hurst>0.55, mean-reversion logic otherwise | OHLCV + regime classifier | M | 4
119 | dvol_aware_trend | Scale trend exposure down when DVOL (BTC vol index) > rolling 80th percentile | DVOL + signal | M | 4
120 | turbulence_index_filter | Mahalanobis-distance turbulence filter; pause trend entries when above threshold | Multi-asset returns | H | 3

## 11. ML / DL Augmented Trend

121 | patchtst_direction_trend | PatchTST forecast sign → trade direction, scaled by forecast confidence | OHLCV + PatchTST | H | 4
122 | tft_regime_conditioned_trend | TFT with regime as static covariate; trade horizon-specific directional prediction | OHLCV + regime + TFT | H | 4
123 | mamba_state_space_trend | Mamba/SSM directional forecast on long-context OHLCV+orderflow inputs | Multi-modal + Mamba | H | 4
124 | gnn_cross_asset_momentum | Graph network on asset-correlation graph; nodes' updated embeddings rank momentum (network momentum) | Multi-asset OHLCV + GNN | H | 4
125 | candlenet_pattern_trend | CandleNet (CNN over candle images) emits trend-continuation probability | OHLCV candle images | H | 3
126 | xgboost_trend_classifier | Gradient-boosted classifier over technical+orderflow features predicts next-bar direction | Tabular features + XGB | M | 4
127 | lstm_momentum_signal | LSTM over multivariate sequence outputs scaled position size (Dynamic Momentum Learning) | OHLCV + LSTM | M | 3
128 | rl_trend_position_sizer | RL agent sizes a base trend signal (PPO/DQN) using reward = risk-adj PnL | Signal + RL env | H | 4
129 | conformal_calibrated_trend | Wrap any trend predictor in conformal prediction; trade only when interval excludes zero | Base model + conformal | M | 5
130 | ensemble_trend_blender | Stack/average sign of N trend strategies (MA, ADX, breakout, ML) with risk-parity weights | All trend signals | M | 5

---

## Notes for the Research Engine
- **Param search** is the engine's job: each row is one *idea*, not one *configuration*.
- **Sub-family tags** above should be persisted as labels so the engine can constrain crossover/mutation within or across sub-families.
- **Crypto-perp fit** scores are heuristic priors, not gospel — let backtests override.
- **Dedup boundary**: two strategies are the same if they would degenerate to the same signal under a change of MA length / lookback. Different *mechanisms* (slope vs cross vs envelope) count separately.
- **Composability**: rows in §10 (adaptive) are explicitly *overlays* — the engine should be allowed to wrap any §1-§9 base with any §10 overlay.
