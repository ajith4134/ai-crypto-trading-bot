# Seed Gene-Pool: Crypto-Native Strategy Archetypes

## Family Definition

The **crypto-native family** comprises trading strategies whose alpha source does **not** exist in
TradFi: they exploit mechanisms unique to crypto market microstructure - perpetual swap funding
mechanics, public on-chain transaction graphs, exchange-published liquidation tape, leverage-driven
liquidation cascades, stablecoin issuance plumbing, validator/mempool MEV, and the bi-modal
CEX/DEX market structure. These are the bedrock alpha streams a perp bot should mine first
because (a) data is cheap and continuous, (b) signals tend to be high-frequency-friendly, and
(c) they directly map to the producers the bot already operates: funding rate, premium index,
Coinglass liquidations (F58), exchange netflow (F52), MVRV proxies, perpetual basis, DVOL,
CryptoBERT/FinBERT sentiment, and the regime HMM.

## Catalog Format

`name | mechanism (1 sentence) | required_data | complexity | crypto-perp_fit (1-5)`

## Sub-Family A: Funding-Rate Strategies (15)

1. `funding_fade_extreme_long | Short perp when 8h funding > +0.05% (90th pct) on the premise that overheated longs precede liquidation-driven dips | funding_rate, OI | low | 5`
2. `funding_fade_extreme_short | Long perp when 8h funding < -0.03% (10th pct) because crowded shorts cap downside and prefigure a squeeze | funding_rate, OI | low | 5`
3. `funding_momentum_directional | Go long while funding stays positive and rising (sentiment confirmation), exit when funding rolls over | funding_rate (z-score+slope) | low | 4`
4. `funding_zero_cross_reversal | Trade direction of the funding-rate sign-flip as a regime-change marker | funding_rate | low | 4`
5. `funding_carry_perp_spot | Long spot + short perp (or vice versa) to harvest funding while hedged delta-zero | funding_rate, spot, perp | med | 5`
6. `cash_and_carry_basis | Long spot + short dated future, lock the annualized basis to expiry | spot, dated_futures, days_to_exp | low | 3`
7. `reverse_carry_backwardation | Short spot (borrow) + long dated future when basis is negative | spot, dated_futures, borrow_rate | med | 3`
8. `funding_dispersion_xexch | Long perp on exchange with most-negative funding, short perp on exchange with most-positive funding, delta-neutral | funding_rate per venue | high | 5`
9. `funding_weighted_oi_signal | Weight funding by OI to detect when "real money" is paying for leverage vs marginal positioning | funding_rate, OI | med | 5`
10. `funding_volatility_breakout | Trade direction when funding rate's rolling std collapses then expands (volatility-of-funding regime change) | funding_rate | med | 4`
11. `funding_acceleration | Entry on 2nd-derivative of funding (rate-of-change of slope) to front-run momentum chasers | funding_rate | med | 4`
12. `funding_premium_divergence | When funding rises but premium index stagnates, fade the funding (artificial pressure) | funding_rate, premium_index | med | 5`
13. `funding_skew_convexity | Build payoff convexity: small short when funding > 1 stdev, scale up at >2 stdev | funding_rate (z-score) | med | 5`
14. `funding_persistence_trend | Hold direction while funding stays one sign for N hours (sticky regime exploitation) | funding_rate (run-length) | low | 4`
15. `funding_calendar_perp | Long perp + short dated future when perp funding > implied basis of dated | funding_rate, dated_basis | high | 3`

## Sub-Family B: Basis / Term Structure (10)

16. `perp_quarterly_spread | Spread trade between perp and quarterly future when calendar basis dislocates | perp, quarterly | med | 3`
17. `term_structure_steepener | Long front + short back when curve is too flat (contango compression) | multi-expiry futures | high | 2`
18. `term_structure_flattener | Opposite: short front + long back when curve too steep | multi-expiry futures | high | 2`
19. `basis_zscore_revert | Mean-revert perp-spot basis when |z| > 2 (1m bars, 60-bar window) | basis (perp-spot), z-score | low | 5`
20. `basis_breakout_follow | Follow basis when it breaks above N-hour range (institutional demand for leverage) | basis | low | 5`
21. `dvol_term_structure_regime | Long vega when term structure inverts (front-month DVOL > 90-day) | DVOL term structure | high | 3`
22. `realized_implied_spread | Sell DVOL when implied > realized by >5 vol points (variance carry) | DVOL, realized vol | high | 2`
23. `mark_spot_premium_fade | Fade premium index when |premium| > 0.3% within 5 minutes | premium_index | low | 5`
24. `mark_spot_premium_follow | Follow premium index breakout above 60-min high (aggressive perp bid) | premium_index | low | 4`
25. `synthetic_yield_curve | Build implied "yield curve" across maturities and trade convexity dislocations | funding + dated basis | high | 3`

## Sub-Family C: Liquidation Alpha (15)

26. `liq_cascade_predict_long | Enter short when long-side liq map cluster sits 1-2% below price AND OI is at percentile-rank 90 | liq_heatmap, OI, price | high | 5`
27. `liq_cascade_predict_short | Mirror: long when short cluster sits 1-2% above and shorts are crowded | liq_heatmap, OI | high | 5`
28. `post_liq_reversion | Buy after >$50M long liquidations printed in <5 min (over-extension bounce) | liquidation tape | low | 5`
29. `post_liq_reversion_short | Symmetric: short after a massive short-liq spike | liquidation tape | low | 5`
30. `liq_magnet_follow | Go with price toward nearest large liq cluster (magnetism trade) | liq_heatmap | med | 5`
31. `liq_magnet_fade | Fade price at the cluster wick after liquidations clear (post-grab reversal) | liq_heatmap, candle wicks | med | 5`
32. `liq_cluster_breakout | When price breaches a major cluster, trade momentum continuation as cascade unfolds | liq_heatmap, tick data | high | 5`
33. `dark_pool_liq_retracement | When OI drops > 10% in 30 min but spot unchanged, fade the next move (forced unwinds finished) | OI, price | med | 4`
34. `liq_heatmap_double_cluster | Bracket trade between two equidistant clusters above/below | liq_heatmap | med | 4`
35. `liq_drying_signal | Liquidation print rate collapses to <10% of weekly avg → low-vol regime, sell straddle | liquidation tape | high | 3`
36. `oct10_cascade_archetype | Massive cross-asset liq event (>$5B day) → 24h aggressive long after first stabilisation candle | liq tape, BTC.D, OI | med | 5`
37. `liq_imbalance_directional | Trade in the direction of the smaller liq side (sticky shorts vs sticky longs) | liq tape per side | low | 5`
38. `liq_rate_acceleration | Enter on a >3 stdev spike in liq-per-second (front-run continuation) | liquidation tape (per-second) | high | 4`
39. `funding_aligned_liq_fade | Fade liq cascades that align with extreme funding (true exhaustion) but follow those that contradict funding (continuation) | funding, liq tape | med | 5`
40. `liq_volume_profile_node | Use historical liq volume by price as support/resistance nodes | liq tape (historical) | med | 4`

## Sub-Family D: Open Interest Mechanics (10)

41. `oi_price_divergence | Fade trends when price makes new high but OI diverges down (rally led by shorts covering) | OI, price | low | 5`
42. `oi_up_price_down_short_build | Short continuation when both OI rises and price falls (fresh shorts adding) | OI, price | low | 5`
43. `oi_up_price_up_strong_long | Long continuation when OI and price both rise (fresh longs, strong trend) | OI, price | low | 5`
44. `oi_down_price_down_long_unwind | Look for bounce when OI and price both fall (longs flushed, sellers exhausted) | OI, price | low | 5`
45. `oi_exhaustion_percentile | Exit / fade when OI hits 95th-percentile of trailing 30d | OI (rolling pct) | low | 5`
46. `oi_roc_momentum | Long when 1h OI rate-of-change > 3 stdev (institutional position build) | OI (delta) | low | 4`
47. `oi_collapse_relief | Bounce trade when OI drops >15% in 4h (forced deleveraging complete) | OI | low | 5`
48. `oi_weighted_funding_composite | Composite signal = funding × OI percentile (true cost of crowded positioning) | funding, OI | med | 5`
49. `oi_dispersion_xexch | When one exchange's OI dominates (Bybit > Binance shift), fade the dominant-side bias | OI per venue | high | 4`
50. `oi_per_market_cap_ratio | High OI/MC ratio = over-leveraged regime → reduce position size and fade extremes | OI, market cap | low | 4`

## Sub-Family E: On-Chain Market-Cycle Metrics (12)

51. `mvrv_z_extreme_short | Short bias when MVRV-Z > 7 (cyclical top) | MVRV z-score | med | 4`
52. `mvrv_z_extreme_long | Aggressive long when MVRV-Z < 0.5 (cyclical bottom accumulation) | MVRV z-score | med | 4`
53. `nupl_euphoria_fade | Fade when NUPL > 0.75 (euphoria zone, distribution risk) | NUPL | med | 4`
54. `nupl_capitulation_long | Long when NUPL < 0 (capitulation, unrealised losses dominate) | NUPL | med | 4`
55. `sopr_cross_one | Trade direction off SOPR crossing 1.0 from below (long) or above (short) | SOPR | med | 3`
56. `lth_sth_sopr_divergence | LTH-SOPR rising while STH-SOPR falling = bullish (smart money distributing to speculators is bearish - invert) | LTH-SOPR, STH-SOPR | high | 3`
57. `dormancy_spike_distribution | When dormancy spikes, old coins moving → distribution risk → short bias | dormancy metric | high | 3`
58. `coin_days_destroyed_signal | High CDD relative to 30d MA = whales moving, fade rallies | CDD | high | 3`
59. `supply_on_exchange_trend | Falling exchange supply = bullish accumulation, long bias | exchange BTC balance | med | 4`
60. `miner_outflow_pressure | Spike in miner-to-exchange flow = sell pressure, short bias | miner outflow | med | 3`
61. `hodl_wave_compression | When 1y+ HODL waves compress (old coins moving), tactical short | HODL waves | high | 2`
62. `realized_price_floor | Long when spot < realized price (historical cycle bottom proxy) | realized price | low | 3`

## Sub-Family F: Exchange Netflow & Stablecoin Plumbing (12)

63. `exchange_netflow_inflow_fade | Short on >3 stdev exchange-inflow spike (sell pressure incoming) - matches F52 producer | exchange netflow | low | 5`
64. `exchange_netflow_outflow_long | Long on persistent multi-day outflow (cold-storage accumulation) | exchange netflow | low | 5`
65. `stablecoin_supply_ratio_low | Low SSR (lots of stable dry powder vs BTC mcap) → long bias | SSR | low | 4`
66. `stablecoin_mint_event | Long crypto risk-on after >$250M USDC/USDT mint to authorized treasury | mint/burn tape | med | 5`
67. `stablecoin_burn_event | Short bias after large stable burn (capital exiting risk) | mint/burn tape | med | 4`
68. `stable_exchange_balance_rising | Rising stablecoin exchange balance = ammo on exchange → bullish setup | stable exchange flow | med | 5`
69. `usdt_usdc_dominance_rotation | Trade rotation between USDT and USDC dominance as risk/regulatory proxy | stable mcap split | med | 3`
70. `stable_depeg_arb | Mean-revert stable when |price - 1.0| > 0.5% via spot-perp or curve pools | stable spot prices | high | 3`
71. `stable_depeg_contagion_short | Short risk crypto when major stable de-pegs > 1% (flight-to-safety risk-off) | stable peg | med | 5`
72. `dex_cex_stable_migration | Migration of stables from CEX to DEX = DeFi season; long alts/ETH | per-venue stable balances | high | 3`
73. `circle_attestation_signal | Trade reaction to monthly Circle attestation (composition + reserve quality) | attestation reports | med | 2`
74. `tether_print_directional | Long BTC after large USDT print waves (historical correlation) | USDT supply | low | 4`

## Sub-Family G: Whale & Cohort Tracking (8)

75. `whale_alert_cluster_directional | Trade direction of clustered whale moves (>3 alerts/hour same direction) | whale-alert feed | med | 4`
76. `top100_holder_cohort_shift | Long when top-100 wallet collective balance grows >0.5% in 7d | rich-list snapshots | high | 4`
77. `otc_desk_inflow | Bullish when known OTC desk wallets receive large inflows from miners | labelled wallets | high | 3`
78. `nansen_smart_money_follow | Mirror Nansen "smart money" cohort net buys on majors | Nansen labels | high | 3`
79. `whale_to_exchange_short | Short on large whale → exchange deposit (incoming sell) | wallet labels, transfer feed | med | 5`
80. `whale_from_exchange_long | Long on large whale → cold-wallet withdrawal (accumulation) | wallet labels, transfer feed | med | 5`
81. `whale_persistence_score | Score whales by hit-rate, weight signals by historical accuracy | whale feed + label history | high | 4`
82. `whale_silence_anomaly | Long when whale activity collapses to <30d-min (low distribution risk) | whale feed | med | 3`

## Sub-Family H: BTC Dominance & Rotation (6)

83. `btcd_breakdown_alt_long | Long alt-perps when BTC.D breaks 60→below trend (alt season ignition) | BTC.D | low | 4`
84. `btcd_breakout_alt_short | Short alts when BTC.D breaks out of range up (capital flight to BTC) | BTC.D | low | 4`
85. `eth_btc_ratio_regime | Use ETH/BTC > 200d MA as alt-season "ON" filter; gates alt long signals | ETH/BTC | low | 4`
86. `alt_season_index_filter | Activate alt-perp long strategies only when Altcoin Season Index > 75 | alt season idx | low | 3`
87. `rotation_sequence_cohort | Rotate BTC → ETH → large alts → mid → small as dominance ladders | mcap cohort indices | high | 3`
88. `btcd_mean_revert | Mean-revert BTC.D when 2 stdev above/below 90d mean | BTC.D | low | 3`

## Sub-Family I: Macro-Crypto & ETF Flow (8)

89. `spot_etf_inflow_long | Long BTC the session after >$500M aggregate spot-ETF inflow | ETF flows | low | 5`
90. `spot_etf_outflow_short | Short BTC after >$300M outflow day | ETF flows | low | 5`
91. `etf_flow_streak_momentum | Multi-day streak signal (5+ consecutive inflow days = trend ON) | ETF flows | low | 4`
92. `etf_flow_divergence | Short when BTC rallies but ETF flows are net-out (rally lacks institutional bid) | ETF flows + price | med | 5`
93. `eth_etf_relative_flow | ETH/BTC long when ETH-ETF flows outpace BTC-ETF flows | ETF flows per asset | med | 4`
94. `halving_cycle_positioning | Increase BTC perp long allocation in 12-18m post-halving window | halving epoch | low | 2`
95. `rainbow_band_overlay | Reduce risk when price enters top rainbow band; size up at bottom band | rainbow chart band | low | 2`
96. `stock_to_flow_deviation | Trade deviation from S2F model when |residual| > 2 stdev | S2F model line | med | 2`

## Sub-Family J: MEV-Adjacent & Microstructure (8)

97. `gas_spike_avoid | Suppress entries during gas-fee spike percentile > 95 (signals MEV risk / volatile period) | mempool gas | med | 3`
98. `mempool_directional_imbalance | Trade direction of pending-tx flow imbalance (DEX → CEX arb pressure) | mempool feed | high | 3`
99. `sandwich_aware_entry | Use private RPC or batch auctions for entries; avoid placing market orders during high-MEV windows | mempool + private relay | high | 3`
100. `dex_cex_price_gap_arb | Arb price gap between Uniswap/AMM and CEX spot, hedge via perp | DEX quotes, CEX prices | high | 4`
101. `cex_dex_liquidity_funnel | Trade direction of stable-flow imbalance between CEX deposit and DEX pool activity | DEX/CEX flows | high | 3`
102. `twap_detection_follow | Detect institutional TWAP via persistent same-side prints at regular intervals; ride direction | tick-level trades | high | 4`
103. `vwap_anchor_revert | Mean-revert price to session VWAP when deviation > 2 stdev with no news catalyst | tick trades, VWAP | low | 5`
104. `iceberg_order_signal | Long when persistent passive bid re-loads at one level (iceberg accumulation) | LOB level-1/2 | high | 4`

## Sub-Family K: ML-Augmented Crypto-Native Composites (10)

105. `gnn_onchain_clustering | Graph Neural Net on tx-graph to score address clusters; trade when "smart" cluster net buys | tx graph + GNN | high | 3`
106. `sentiment_netflow_composite | Composite Z(cryptobert_sentiment) + Z(exchange_netflow_inverse) > 2 → long | sentiment, netflow | med | 5`
107. `regime_conditioned_onchain | Use HMM regime to gate which on-chain signal fires (e.g. MVRV-revert only in range regime) | HMM state, on-chain | high | 4`
108. `funding_sentiment_disagreement | Long when funding negative but sentiment positive (contrarian alignment) | funding, sentiment | med | 5`
109. `cryptobert_extreme_fade | Fade when CryptoBERT sentiment in 99th percentile (euphoria) | CryptoBERT score | med | 5`
110. `finbert_news_event_burst | Trade direction of immediate news burst when FinBERT polarity > 0.8 and headline volume spikes | FinBERT, news count | med | 4`
111. `fear_greed_extreme_revert | Long when F&G < 20, scale-out when > 80 | F&G index | low | 4`
112. `composite_liq_funding_oi_rl | RL agent inputs (funding, OI, liq tape, netflow) → discretionary directional bet | RL stack | high | 5`
113. `multi_factor_onchain_rank | Rank assets by composite z-score across MVRV/SOPR/NUPL/netflow; long top-decile | on-chain panel | high | 3`
114. `regime_aware_carry | Switch between funding-carry (low-vol regime) and basis-reversion (high-vol regime) by HMM state | HMM, funding, basis | high | 5`

## Sub-Family L: Cross-Signal & Convexity (6)

115. `funding_premium_oi_triple_align | Enter only when funding, premium, and OI all 3 confirm same direction (high-conviction filter) | funding, premium, OI | med | 5`
116. `convex_funding_skew_payoff | Build asymmetric payoff that pays when funding mean-reverts violently (long vol-of-funding) | funding, options | high | 4`
117. `cross_asset_basis_convergence | Long BTC basis / short ETH basis when ETH basis > BTC by historical-extreme spread | BTC + ETH basis | high | 3`
118. `liq_funding_contradiction | When liq cascade direction contradicts funding direction → it's exhaustion, follow the cascade | liq, funding | med | 5`
119. `netflow_basis_composite | Long when netflow outflowing AND basis cheap (accumulation + discounted leverage) | netflow, basis | med | 5`
120. `whale_funding_alignment | Whale deposits to exchange + funding extreme positive → high-conviction short | whale flow, funding | high | 5`

---

## Producer Mapping (which bot producers feed which sub-family)

| Bot producer | Feeds sub-families |
|---|---|
| funding rate (per pair) | A, D, G, K, L |
| premium index (mark-spot) | B, L |
| Coinglass liquidations (F58) | C, L |
| exchange netflow (F52) | F, G, K, L |
| MVRV proxies | E, K |
| perpetual basis | B, F, K, L |
| DVOL | B, K |
| CryptoBERT / FinBERT sentiment | K |
| regime HMM | K, all (as gating) |

## Notes for Gene-Pool Seeding

- All 120 specs are designed to be expressible as DSL formulas / signal-graph nodes — keep
  parameters (z-thresholds, percentile ranks, window sizes) as evolvable genes.
- Complexity "low" = single-producer rule, suitable for first-generation seed alleles.
- Complexity "med/high" = composites; seed at lower frequency to preserve population diversity.
- crypto-perp_fit=5 strategies are the highest-priority seeds for a perp-only bot.

