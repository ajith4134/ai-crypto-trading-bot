# Seed Archetypes — Mean-Reversion / Stat-Arb / Pairs Family

**Purpose:** Canonical archetype catalog for the autonomous strategy-research engine's seed gene-pool.
**Family scope:** Mean-reversion, statistical arbitrage, and pairs/basket trading — all variants where the alpha hypothesis is "an extension from equilibrium will revert."
**Replaces:** `mean_reversion_strict` placeholder.
**Target use:** Each row is a *distinct mechanism* (not a param tweak). The GA / RL outer loop will mutate parameters, timeframes, gates, and ML wrappers per archetype.

**Data bus available (referenced by short code):**
- `OHLCV-MTF` — multi-timeframe candles (1m/5m/15m/1h/4h/1d)
- `OB` — order book L2 (depth, OBI), `CVD`, `OFI`, `MICRO` (microprice)
- `FUND` — funding rate, `PREM` — premium index, `BASIS` — perp-spot basis
- `ONCH` — on-chain (exchange netflow, MVRV, SOPR)
- `SENT` — sentiment, `LIQ` — liquidation feed
- `VOL` — DVOL/ATR/realized vol surface
- `REG` — HMM regime label, `HURST` — rolling Hurst
- `ML` — GNN / TFT / PatchTST / Mamba / CandleNet / BOCPD / Conformal

**Schema:** `name | mechanism | required_data | complexity | crypto-perp_fit (1-5)`

---

## Sub-family A — Bollinger / Volatility-Band Reversion

1. `bb_outer_touch_revert | Fade close that pierces outer Bollinger band (20,2) back toward MA20 | OHLCV-MTF | low | 4`
2. `bb_percent_b_extreme | Enter when %B<0 or %B>1, exit on %B=0.5 | OHLCV-MTF | low | 4`
3. `bb_squeeze_release_fade | After Keltner-inside-BB squeeze, fade the first impulse exit | OHLCV-MTF | med | 3`
4. `bb_walk_exhaustion | Detect "walking the band" with declining body size, fade the final touch | OHLCV-MTF | med | 3`
5. `bb_double_bottom_inside | Bollinger M-top / W-bottom variant (Bollinger's own pattern) | OHLCV-MTF | med | 3`
6. `bb_width_percentile_mean | Long vol-of-vol reversion when BB width is at >95th pct historical | OHLCV-MTF,VOL | med | 3`
7. `keltner_outside_revert | Fade close outside Keltner (ATR-based) channel | OHLCV-MTF | low | 4`
8. `donchian_extreme_fade | Fade fresh N-period Donchian extreme when ADX<20 | OHLCV-MTF | low | 3`
9. `bb_vwap_confluence_fade | Fade BB outer touch only if also >2σ from session VWAP | OHLCV-MTF | med | 4`
10. `bb_ml_residual_revert | Fade BB extreme only when CandleNet residual exceeds conformal band | OHLCV-MTF,ML | high | 4`

## Sub-family B — Oscillator-Extreme Reversion

11. `rsi_oversold_overbought | Classic RSI<30 long / RSI>70 short, exit at 50 | OHLCV-MTF | low | 4`
12. `rsi_regular_divergence | Price new low + RSI higher low → long (and inverse) | OHLCV-MTF | med | 4`
13. `rsi_hidden_divergence | Continuation-style hidden divergence on pullback inside trend | OHLCV-MTF,REG | med | 3`
14. `rsi_dual_tf_alignment | 1h RSI extreme + 5m RSI cross-back trigger | OHLCV-MTF | low | 4`
15. `stoch_kd_cross_oversold | Stochastic %K crosses %D below 20 (and above 80) | OHLCV-MTF | low | 3`
16. `stoch_rsi_extreme | StochRSI flush to 0/100 then cross | OHLCV-MTF | low | 3`
17. `mfi_extreme_revert | Money Flow Index <20 / >80 with volume confirmation | OHLCV-MTF | low | 3`
18. `williams_r_extreme | %R below -80 / above -20 fade | OHLCV-MTF | low | 3`
19. `cci_outlier_fade | CCI > +200 short / < -200 long | OHLCV-MTF | low | 3`
20. `demarker_extreme | DeMarker <0.3 / >0.7 mean revert | OHLCV-MTF | low | 2`
21. `ultimate_oscillator_revert | UO triple-timeframe extreme reading fade | OHLCV-MTF | low | 3`
22. `awesome_oscillator_zero_cross | AO twin-peaks distant from zero → fade | OHLCV-MTF | low | 2`
23. `composite_oscillator_score | Weighted composite of RSI+Stoch+MFI+%R+%B → fade at >2σ | OHLCV-MTF | med | 4`
24. `rsi_bb_confluence | RSI extreme AND price outside BB outer — only fire when both | OHLCV-MTF | low | 4`
25. `tsi_extreme_fade | True Strength Index extreme + signal-line cross | OHLCV-MTF | low | 3`

## Sub-family C — Z-Score / Statistical Distance Reversion

26. `zscore_price_revert | Z-score of close vs N-period mean; fade |z|>2, exit at z≈0 | OHLCV-MTF | low | 4`
27. `zscore_log_return_revert | Z-score of log-returns; fade single-bar shock | OHLCV-MTF | low | 4`
28. `zscore_rolling_dynamic | EWMA-weighted z-score with adaptive window via HURST | OHLCV-MTF,HURST | med | 4`
29. `zscore_volatility_revert | Z-score of realized-vol vs vol-cone; fade vol spikes | VOL | med | 3`
30. `zscore_spread_pair | Z-score of cointegrated pair spread → classic stat-arb entry | OHLCV-MTF | med | 4`
31. `zscore_basket_residual | Z-score of multi-asset basket residual after regression | OHLCV-MTF | high | 3`
32. `mahalanobis_distance_fade | Multi-feature Mahalanobis distance outlier → mean revert | OHLCV-MTF,ML | high | 3`
33. `winsorized_outlier_fade | Robust z-score with MAD instead of std; fade tails | OHLCV-MTF | low | 4`
34. `cumret_zscore_revert | Z-score of N-bar cumulative return; fade momentum extremes | OHLCV-MTF | low | 4`

## Sub-family D — OU / Vasicek / Continuous-Time Reversion

35. `ou_calibrated_revert | Fit OU(θ,μ,σ) on residual; enter when deviation > κ·σ_eq, size by speed | OHLCV-MTF | high | 4`
36. `ou_half_life_filter | Trade only when estimated half-life ∈ [holding-period limits] | OHLCV-MTF | high | 4`
37. `ou_optimal_stopping | Bertola/Leung-Li optimal entry/exit thresholds from OU params | OHLCV-MTF | high | 3`
38. `vasicek_rate_revert | Vasicek-style reversion model applied to funding/basis | FUND,BASIS | high | 4`
39. `cir_positive_revert | Cox-Ingersoll-Ross variant for non-negative series (vol, funding |abs|) | VOL,FUND | high | 3`
40. `ou_jump_diffusion | OU + Poisson jumps; ignore jumps, fade diffusion only | OHLCV-MTF | high | 3`
41. `penalized_ou_portfolio | L1-penalized OU-likelihood basket construction (mean-reverting weights) | OHLCV-MTF | high | 3`

## Sub-family E — Cointegration Pairs Trading

42. `eg_pair_zscore | Engle-Granger 2-step on log-prices; trade residual z-score | OHLCV-MTF | med | 4`
43. `johansen_multi_pair | Johansen VECM over ≥3 assets; trade leading eigenvector spread | OHLCV-MTF | high | 4`
44. `kalman_dynamic_hedge | Kalman filter on β_t; trade time-varying spread | OHLCV-MTF | high | 4`
45. `partial_cointegration | Clegg-Krauss partial-cointegration spread trading | OHLCV-MTF | high | 3`
46. `phillips_ouliaris_pair | P-O test gated pair selection, then z-score trade | OHLCV-MTF | high | 3`
47. `rolling_cointegration_refit | Refit cointegration each window; auto-deregister broken pairs | OHLCV-MTF | high | 4`
48. `copula_pair_misprice | Copula-based mispricing index (Liew-Wu) instead of z-score | OHLCV-MTF | high | 3`
49. `distance_pair_classic | Gatev distance-method nearest-pair selection + threshold | OHLCV-MTF | med | 3`
50. `cluster_then_pair | DBSCAN/affinity-cluster crypto universe, then pair within cluster | OHLCV-MTF | high | 4`

## Sub-family F — Factor / PCA / Residual Stat-Arb

51. `avellaneda_pca_residual | Avellaneda-Lee: regress on top-K PCs, OU-model residual, fade | OHLCV-MTF | high | 4`
52. `sector_neutral_residual | Sector-neutral (L1/L2/DeFi/MEME) residual reversion | OHLCV-MTF | high | 4`
53. `btc_beta_neutral_resid | Hedge each alt's BTC-β, fade residual deviation | OHLCV-MTF | med | 5`
54. `eth_beta_neutral_resid | Same vs ETH for L2/DeFi correlation cluster | OHLCV-MTF | med | 4`
55. `dual_factor_neutral | Neutralize BTC and ETH simultaneously, fade residual | OHLCV-MTF | high | 4`
56. `autoencoder_residual_revert | Sparse autoencoder reconstruction error → fade large residuals | OHLCV-MTF,ML | high | 4`
57. `pca_idio_vol_targeted | Vol-target the idio residual before fading (Sharpe-stable sizing) | OHLCV-MTF,VOL | high | 4`
58. `gnn_cross_section_residual | GNN-implied fair value across the asset graph; fade residual | OHLCV-MTF,ML | high | 4`

## Sub-family G — Cross-Venue / Spread / Triangular Arbitrage

59. `cross_exchange_spot_spread | Fade venue-to-venue spread when |Δ| > 2× fee+slippage | OHLCV-MTF | med | 5`
60. `cross_exchange_perp_spread | Same coin perp across Binance/Bybit/OKX; fade spread divergence | OHLCV-MTF | med | 5`
61. `triangular_arb_loop | BTC→ETH→USDT→BTC closed-loop instant arb on same venue | OHLCV-MTF,OB | high | 4`
62. `cross_quote_arb | BTC/USDT vs BTC/USDC vs BTC/BUSD implied-rate fade | OHLCV-MTF | med | 4`
63. `stable_depeg_revert | USDT/USDC/DAI off-peg fade to 1.00 | OHLCV-MTF | low | 4`
64. `cex_dex_basis_revert | CEX vs DEX (Uniswap) price gap fade (gas-aware) | OHLCV-MTF | high | 3`
65. `quanto_funding_arb | Inverse-perp vs linear-perp funding diff fade | FUND | high | 3`
66. `inter_exchange_funding_div | Funding rate divergence across exchanges → cash-and-carry rotation | FUND | med | 4`

## Sub-family H — Crypto-Specific Derivative Reversion

67. `funding_extreme_fade | Fade direction of perp when |funding| > N-day 95th pct | FUND | low | 5`
68. `premium_index_z_fade | Z-score of premium index; fade extremes | PREM | med | 5`
69. `basis_revert_mean | Perp-spot basis reverts to per-coin mean within hours; fade gap | BASIS | med | 5`
70. `funding_predictive_revert | Predicted next-funding (cumulative premium) extreme → pre-fade | FUND,PREM | med | 4`
71. `liquidation_cascade_fade | Detect cascading LIQ feed; enter contra after volume+OBI confirm | LIQ,OB | high | 5`
72. `long_short_ratio_extreme | Exchange-reported L/S ratio extreme → contrarian | SENT | low | 4`
73. `oi_spike_no_price_fade | Open-interest jump without price follow-through → fade direction | OHLCV-MTF | med | 4`
74. `cvd_divergence_revert | Price up + CVD down (or inverse) → fade move | OB | med | 5`
75. `obi_extreme_micro_revert | Microprice deviation from mid > θ → 30-sec mean-revert scalp | OB,MICRO | high | 4`
76. `ofi_pulse_fade | Order-flow-imbalance pulse fades after liquidity refill (~secs) | OB | high | 4`
77. `taker_buy_ratio_extreme | Taker buy/sell volume ratio extreme over 5m → fade | OB | low | 4`
78. `iv_rv_spread_revert | DVOL implied vs realized spread reverts; fade IV spikes | VOL | high | 3`
79. `funding_basis_combo_fade | Combined funding-z + basis-z dual-signal contrarian | FUND,BASIS | med | 5`
80. `liquidation_heatmap_magnet | Price overshoots liq cluster then reverts to magnet zone | LIQ | high | 4`

## Sub-family I — Level / Anchor Reversion

81. `vwap_revert_session | Fade price >2σ from session VWAP back to VWAP | OHLCV-MTF | low | 4`
82. `vwap_anchored_revert | Anchored VWAP from swing-high/low — fade deviations | OHLCV-MTF | med | 4`
83. `prior_day_hl_fade | Fade extension beyond prior-day high/low back to PDH/PDL | OHLCV-MTF | low | 4`
84. `weekly_pivot_revert | Classic R1/S1/R2/S2 pivot fade with confluence | OHLCV-MTF | low | 3`
85. `round_number_magnet | Fade overshoot of psychological round levels (10k, 50k, etc.) | OHLCV-MTF | low | 3`
86. `prior_swing_revert | Fade re-test failure of prior swing high/low (false breakout) | OHLCV-MTF | med | 4`
87. `volume_profile_poc_revert | Fade price >1.5σ from session POC back to POC | OHLCV-MTF | med | 4`
88. `value_area_revert | Mean-revert from VAH/VAL back into value area | OHLCV-MTF | med | 4`
89. `liquidity_void_fill | Fade move that leaves an FVG / liquidity void → revert to fill | OHLCV-MTF | med | 4`

## Sub-family J — Time-of-Day / Calendar Reversion

90. `intraday_open_revert | Fade first-N-min opening drive after RTH/UTC session open | OHLCV-MTF | low | 3`
91. `overnight_gap_fade | Fade weekend / Sun-open gap toward Friday close | OHLCV-MTF | low | 4`
92. `funding_window_revert | Fade pre-funding-window squeeze, exit post-settlement | FUND | med | 5`
93. `monthly_expiry_fade | CME/options-expiry overshoot fade | OHLCV-MTF | med | 3`
94. `weekend_volume_revert | Low-weekend-vol drift fade back to Friday VWAP | OHLCV-MTF | low | 3`
95. `seasonal_dow_revert | Day-of-week conditional reversion (e.g. Monday counter-trend) | OHLCV-MTF | low | 2`

## Sub-family K — Regime / Adaptive / ML-Augmented Reversion

96. `hurst_gated_revert | Enable reversion stack only when rolling Hurst < 0.45 | OHLCV-MTF,HURST | med | 5`
97. `hmm_regime_revert | Trade reversion only in HMM "range" state | OHLCV-MTF,REG | med | 5`
98. `adx_filter_revert | ADX<20 unlocks reversion; ADX>25 disables it | OHLCV-MTF | low | 4`
99. `vol_regime_scaled_revert | Scale entry threshold by ATR-percentile to keep edge in high-vol | OHLCV-MTF,VOL | med | 4`
100. `bocpd_confirmed_revert | Enter reversion only after BOCPD signals "no new regime" | OHLCV-MTF,ML | high | 5`
101. `conformal_band_revert | Fade only when price exits PatchTST conformal prediction interval | OHLCV-MTF,ML | high | 5`
102. `tft_residual_revert | Fade TFT forecast residual when |actual−pred|/σ > θ | OHLCV-MTF,ML | high | 5`
103. `mamba_short_horizon_revert | Mamba 1m forecast contra-signal entry, exit at forecast mean | OHLCV-MTF,ML | high | 4`
104. `gnn_neighbor_lead_lag | GNN says neighbor moved first; fade our laggard when gap > θ | OHLCV-MTF,ML | high | 4`
105. `candlenet_pattern_revert | CandleNet labels exhaustion pattern → contra entry | OHLCV-MTF,ML | high | 4`
106. `wavelet_denoised_revert | Wavelet-denoised series z-score fade (noise-stripped) | OHLCV-MTF | high | 3`
107. `kalman_smoothed_revert | Kalman-smoothed local-level model; fade smoothed-vs-raw gap | OHLCV-MTF | high | 4`
108. `rl_meanrev_policy | Discrete-action RL trained only on range-regime data | OHLCV-MTF,REG,ML | high | 4`

## Sub-family L — Sentiment / On-Chain Reversion

109. `sentiment_extreme_fade | Sentiment score >95th / <5th pct → contrarian crypto fade | SENT | low | 4`
110. `funding_sentiment_combo | Funding-z + sentiment-z dual extreme → high-conviction contra | FUND,SENT | med | 5`
111. `exchange_netflow_revert | Large netflow spike without price reaction → mean-revert next 24h | ONCH | med | 3`
112. `mvrv_zscore_revert | MVRV-Z extreme on swing-TF gates contra trades | ONCH | med | 3`
113. `social_volume_climax | Social-volume blow-off + price extension → fade | SENT | low | 3`

---

## Notes for the outer-loop optimizer

- **Variants are not new archetypes:** changing 20→14 RSI period, or BB(2σ)→BB(2.5σ), counts as a parameter mutation, not a new gene.
- **Cross-family composites** (e.g. RSI+BB+VWAP triple-confluence) are encoded as a single archetype only when the *mechanism of confluence* is the contribution; otherwise treat as a gating mutation.
- **Crypto-perp fit scoring** rationale: 5 = uses perp-only data (funding, basis, OI, liq); 4 = leverages 24/7 deep crypto liquidity and microstructure; 3 = transferable from equity but works; 2 = weak / legacy.
- **Top-tier seeds (recommended bootstrap set):** #51, #67, #68, #71, #74, #79, #96, #100, #101 — these are the crypto-perp-native or ML-confirmed archetypes most likely to survive walk-forward.
