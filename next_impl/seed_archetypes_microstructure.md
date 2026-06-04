# Seed Archetypes — Market Microstructure / Order-Flow / LOB Family

## Purpose
Seed gene-pool for the autonomous strategy-research engine, restricted to the **market microstructure, order-flow, and limit-order-book (LOB)** family.
All entries assume crypto perpetual futures execution (Binance/Bybit/OKX/Hyperliquid-class venues) with sub-second L1+L5 book updates, public trade prints, funding/premium feeds, and the bot's existing microstructure producers (OBI raw + EMA, CVD, OFI, microprice, VPIN, Hawkes λ, MM-Hawkes spoof score, premium index, BOCPD per-pair, large-trade >2σ flags).

## Sub-families covered
1. **OFI / book-imbalance signals** (classical, depth-weighted, multi-level, hidden-flow)
2. **CVD / delta-flow signals** (divergence, absorption, exhaustion, sessional)
3. **VPIN / toxicity signals** (entries, exits, regime gates)
4. **Microprice / queue-position alpha** (Stoikov microprice, queue-reactive, fill-prob)
5. **Trade-size / cluster / aggressor signals** (block, iceberg, sweep, sweep-of-sweeps)
6. **Manipulation detection & counter-trade** (spoof, layering, quote-stuffing, MM-Hawkes)
7. **Hawkes self/cross-excitation entries** (lambda spikes, cross-side excitation, decay-fade)
8. **LOB-shape / depth-pressure / slope** (lopsidedness, wall fade, vacuum break)
9. **Latency arb / cross-venue lead-lag** (Binance leads, perp-spot, CEX-DEX)
10. **MM-derivative directional** (Avellaneda-Stoikov skew sign, inventory-bet)
11. **Funding / basis / premium** (premium index, funding-rate arb, perp-spot basis)
12. **Liquidation-cascade prediction** (depth-depletion, liquidation-map, OI flush)
13. **Tape / footprint / volume-profile** (delta clusters, stacked imbalance, POC/VAH/VAL)
14. **BOCPD changepoint entries** (per-pair, per-feature, regime-switch)
15. **Deep-LOB neural signals** (DeepLOB, TLOB, MLPLOB, hybrid CNN-Transformer)
16. **Stop-hunt / liquidity-grab fades** (sweep-then-fail, swing-liquidity reclaim)
17. **Auction / session-open** (call auction, first-N-minute fade/momo)
18. **Cancel-rate / order-age / flicker** (cancel storm, quote-life filter)

## Spec format
`id | name | mechanism (1 sentence) | required_data | complexity (low/med/high) | crypto-perp_fit (1-5)`

---

## 1. OFI / Book-Imbalance signals
1 | classical_OFI_cont | Trade in direction of Cont-Kukanov L1 OFI when |OFI| crosses z-score threshold, exit on mean reversion. | L1 quote events | low | 5
2 | depth_weighted_OFI | Weight OFI contributions by inverse distance from mid across L1-L5 to capture deeper pressure. | L5 quote events | med | 5
3 | multilevel_OFI_MLOFI | PCA/linear combine OFI from each of top-K levels into integrated OFI factor per Xu-Bagrov. | L5 quote events | med | 5
4 | hidden_liquidity_OFI | Trade-implied OFI minus visible-OFI residual = hidden-flow proxy; ride residual direction. | L1 quotes + prints | med | 4
5 | OFI_meanrev_thin_book | When depth < q20 and OFI sign flips, take counter-trade (linear impact is too large to sustain). | L5 + depth | med | 4
6 | OFI_normalized_by_depth | Predict short-horizon return via OFI / market_depth (slope ∝ 1/depth). | L5 quotes | low | 5
7 | cross_pair_OFI_lead | BTC perp OFI leads alt-perp OFI by ~200-800ms; trade alt in BTC-OFI direction. | Multi-pair L1 | med | 5
8 | OFI_decay_halflife_filter | Use only OFI from last τ ms where τ fitted per pair; filters stale flow. | L1 events + tick clock | low | 4
9 | OBI_EMA_crossover | Long when fast-EMA OBI > slow-EMA OBI and both > 0; exit on cross-back. | L1 OBI series | low | 5
10 | OBI_extreme_fade | At |OBI| > 0.85 with stalled microprice, fade — usually spoof or wall. | OBI + microprice | med | 4
11 | stacked_OFI_levels | Require OFI of same sign at L1, L2, L3 simultaneously; rare but high-signal. | L5 events | med | 4
12 | cross_impact_OFI | Use cross-impact matrix (Cont 2023) — pair-X OFI explains pair-Y returns. | Multi-pair L1 | high | 4

## 2. CVD / Delta-flow signals
13 | CVD_breakout | Long when CVD breaks above N-bar high with price confirmation. | Trade prints | low | 5
14 | CVD_price_bullish_divergence | Long when price makes lower-low but CVD makes higher-low (selling exhausted). | Prints + price | low | 5
15 | CVD_price_bearish_divergence | Short on price HH vs CVD LH (buyer exhaustion). | Prints + price | low | 5
16 | CVD_absorption_continuation | Aggressive sells with price holding flat at support → reversal long. | Prints + price levels | med | 5
17 | CVD_absorption_at_VWAP | Specialised absorption setup anchored to session VWAP. | Prints + VWAP | med | 5
18 | CVD_session_reset_breakout | Reset CVD at funding interval, trade direction of intraday-CVD breakout. | Prints + funding clock | low | 5
19 | CVD_delta_exhaustion | Late-trend CVD slope inversion + RSI extreme → fade. | Prints + RSI | low | 4
20 | spot_vs_perp_CVD_divergence | Perp CVD ↑ while spot CVD ↓ → perp-led squeeze candidate to fade. | Two-venue prints | med | 5
21 | CVD_aggressor_ratio | Ratio aggr-buy/aggr-sell over rolling N; trade when crosses 60/40. | Prints | low | 5
22 | per_size_bucket_CVD | Split CVD into small/med/large trade buckets; trade large-bucket CVD only. | Tagged prints | med | 5
23 | CVD_z_score_meanrev | Fade CVD z > 3 in ranging regimes (low ADX). | Prints + ADX | low | 4

## 3. VPIN / toxicity signals
24 | VPIN_spike_exit | Force-close longs when VPIN > 0.7 (toxic flow incoming). | VPIN | low | 5
25 | VPIN_low_entry_gate | Only allow new entries when VPIN < 0.4 (clean tape). | VPIN | low | 5
26 | VPIN_regime_classifier | High-VPIN → momentum mode; low-VPIN → mean-rev mode router. | VPIN + meta-router | med | 5
27 | VPIN_volatility_predict | Use VPIN as leading vol input to size positions inversely. | VPIN + ATR | med | 5
28 | VPIN_bucket_imbalance_dir | Direction of VPIN bucket imbalance (signed) used as 5-30s alpha. | Signed VPIN | med | 4
29 | VPIN_crash_anticipation | VPIN > 0.8 + funding spike → buy puts/short bias for flash-crash window. | VPIN + funding | high | 5
30 | dVPIN_dt_filter | Trade only when d(VPIN)/dt < 0 (toxicity receding). | VPIN slope | low | 4

## 4. Microprice / queue-position alpha
31 | microprice_gradient | Long when (microprice − mid) > θ ticks for N consecutive ticks. | L1 sizes | low | 5
32 | microprice_meanrev_to_mid | Fade microprice extremes back toward mid in thick books. | L1 + depth filter | med | 4
33 | adaptive_microprice_stoikov | Use Stoikov G/B matrices fitted per-pair for unbiased fair price. | L1 + offline calibration | high | 4
34 | microprice_slope_momentum | Microprice acceleration > θ → momentum continuation entry. | Microprice series | low | 5
35 | queue_position_value | Estimate fill-prob & post-fill return; place passive only when EV positive. | L1 + book sims | high | 3
36 | fill_prob_vs_payoff_MM | Market-maker's-dilemma gate: skip levels where fill-prob × payoff < cost. | L1 + fee table | high | 3
37 | queue_jump_post_only | Post-only directly inside spread when microprice signals favourable side. | L1 + post-only | med | 4

## 5. Trade-size / cluster / aggressor
38 | block_trade_follow | Enter same-side after >5σ aggressor print that does not reverse within K ticks. | Tagged prints | low | 5
39 | block_trade_fade | Fade >5σ aggressor print that fails to push price (absorption). | Prints + price | low | 5
40 | iceberg_detection_buy | Repeated refill at same bid level → infer iceberg buyer, ride long. | L1 refill tracker | med | 5
41 | iceberg_completion_continuation | Enter long once detected iceberg buy fully consumed and book bounces. | Refill tracker + price | med | 5
42 | sweep_to_liquidity_continuation | Aggressive multi-level sweep through prior swing → continuation. | Prints + swing map | med | 5
43 | sweep_failure_fade | Sweep prints that immediately retrace within N ms → fade. | Prints + tick clock | med | 5
44 | hidden_print_premium | Off-book block print (dark / RFQ on derivs) treated as informed; trade direction. | Venue-specific block feed | high | 3
45 | trade_size_cluster_alpha | Cluster trades by size; entries only when "informed" cluster firing. | Prints + clustering | high | 4
46 | invariance_size_normalized | Normalise trade sizes by Kyle-invariance scaling per regime. | Prints + vol | high | 3
47 | sweep_print_density | Trades/sec acceleration > 4× baseline = breakout regime entry. | Tick clock | low | 5

## 6. Manipulation detection & counter-trade
48 | MM_hawkes_spoof_fade | When MM-Hawkes spoof score high and OBI flips, fade the artificial OBI. | MM-Hawkes + OBI | med | 5
49 | layering_detector_counter | Detect stacked-then-cancelled levels → counter-trade the implied direction. | L5 events | high | 4
50 | quote_stuffing_gate | Cancel-to-trade ratio > θ in <1s → veto all entries for cooldown window. | L5 events | med | 5
51 | OTR_spike_pause | Order-to-trade ratio per-venue spike → reduce size globally. | Venue counters | med | 4
52 | momentum_ignition_fade | Burst of small aggressors triggering no follow-through → fade ignition. | Prints | med | 4
53 | wash_trade_filter | Filter prints with self-cross / immediate offset patterns out of CVD. | Tagged prints | high | 3

## 7. Hawkes self/cross-excitation entries
54 | hawkes_lambda_spike_ride | Enter on λ_buy spike sustained over K windows (informed cascade). | Hawkes λ | low | 5
55 | hawkes_cross_excitation | λ_buy spike in BTC → trade alt-perp same side (cross-asset Hawkes). | Multi-pair Hawkes | med | 5
56 | hawkes_decay_fade | After λ peaks and decays past 50%, fade exhausted flow. | Hawkes λ derivative | med | 4
57 | hawkes_branching_ratio | If branching ratio η > 0.9, market is endogenous → expect mean-rev. | Hawkes calibration | high | 4
58 | hawkes_imbalance_lambda | λ_buy/λ_sell ratio as continuous directional alpha. | Hawkes both sides | low | 5
59 | hawkes_cancel_intensity | Spike in cancel-arrival λ → impending move; trade prevailing OBI. | Multi-type Hawkes | high | 4

## 8. LOB-shape / depth-pressure / slope
60 | book_slope_alpha | Linear-regress quantity-vs-price across L1-L5; slope asymmetry predicts dir. | L5 snapshot | med | 5
61 | book_lopsidedness | (depth_bid − depth_ask)/(depth_bid + depth_ask) at L5 as continuous signal. | L5 snapshot | low | 5
62 | depth_pressure_decay | If bid-depth shrinks faster than ask over N ticks → short. | L5 series | med | 5
63 | wall_break_continuation | Large stationary wall at level X → enter continuation when wall fully eaten. | L5 + size tracker | med | 5
64 | wall_pull_fade | Wall pulled (cancelled) just before price arrival → fade reverse direction. | L5 cancel events | med | 4
65 | liquidity_vacuum_break | Sum-depth in N-tick band < q10 → momentum entry on first push. | L5 + bands | med | 5
66 | thin_book_throttle | When breadth < q15, halve size and widen stops (impact risk). | L5 breadth | low | 5
67 | spread_widening_signal | Sudden bid-ask spread expansion → regime-switch flag to defensive. | L1 spread | low | 5

## 9. Latency-arb / cross-venue lead-lag
68 | binance_leads_alt_venue | Binance L1 mid leads OKX/Bybit by ~50-300ms; trade lagging venue. | Multi-venue L1 | med | 4
69 | perp_leads_spot | Perp BTC L1 leads spot BTC; trade spot in perp-direction within τ. | Perp+spot L1 | med | 5
70 | spot_leads_perp_oversold | When perp gaps below spot, fade short / enter long mean-rev. | Perp+spot L1 | med | 5
71 | cme_basis_lead | CME BTC futures move leads Binance perp; trade Binance in CME dir. | CME + Binance | high | 3
72 | cex_dex_lag_arb | CEX perp leads on-chain perp (Hyperliquid/dYdX); trade lag. | Multi-venue | high | 3
73 | latency_robust_OFI | Synchronise OFI across venues with timestamp clock-skew correction. | Multi-venue + NTP | high | 4

## 10. Market-maker-derivative directional
74 | avellaneda_skew_sign | Use AS optimal-skew sign as directional alpha (skew>0 implies inv risk → fade). | AS inventory model | high | 4
75 | inventory_directional_bet | Convert AS reservation price drift to micro-trend entry. | AS model | high | 3
76 | as_reservation_price_meanrev | Mid − reservation_price > θ → enter to reservation price. | AS model | high | 3
77 | funding_aware_AS | Adjust AS quotes by funding-rate term; trade asymmetry as alpha. | AS + funding | high | 4

## 11. Funding / basis / premium
78 | premium_index_meanrev | Fade premium-index extremes (perp − index > θ%). | Premium feed | low | 5
79 | funding_rate_arb_pair | When two venues' funding rates differ > 10bp, long-low/short-high. | Funding feeds | med | 4
80 | funding_flip_momentum | When funding flips sign with rising OI, enter in funding direction. | Funding + OI | low | 5
81 | basis_decay_curve | Trade basis-decay between perp & dated futures (where available). | Multi-instrument | high | 3
82 | premium_z_score_filter | Use premium z-score as veto on directional trades against premium. | Premium feed | low | 5

## 12. Liquidation-cascade prediction
83 | liquidation_map_proximity | When price within Xσ of cluster of liq-price levels, expect cascade — trade into it. | Liq-heatmap (Coinglass/intern) | med | 5
84 | book_depletion_predictor | Rapid one-side depth depletion + funding-extreme → liq-cascade entry. | L5 + funding | med | 5
85 | OI_flush_fade | Sharp OI drop + price spike = cascade complete; fade. | OI + price | low | 5
86 | slippage_at_risk_SaR | Forward SaR forecast (Hyperliquid paper) > θ → defensive close. | L5 SaR model | high | 4
87 | post_liq_mean_rev | After 5σ wick from liq cascade, enter reversion to 4h VWAP. | Prints + VWAP | low | 5
88 | cascade_chain_alt_alts | BTC cascade → trade alt-perp downside cascade with K-second lag. | Multi-pair OI | med | 5

## 13. Tape / footprint / volume-profile
89 | footprint_stacked_imbalance | 3+ consecutive bid-imbalance footprint cells → momentum long. | Footprint bars | med | 4
90 | footprint_delta_divergence | Footprint delta divergence at swing extremes → reversal. | Footprint bars | med | 4
91 | POC_magnet_fade | Fade price away from prior-session POC back toward POC. | Volume-profile | low | 5
92 | VAH_VAL_breakout | Breakout-and-hold above VAH (or below VAL) with delta confirmation. | Volume-profile + delta | med | 5
93 | LVN_breakout | Low-volume node break with 1.5× avg vol → continuation. | Volume-profile | low | 5
94 | naked_POC_fill | Untouched prior-session POC acts as target; trade toward it. | Volume-profile | low | 5
95 | VWAP_band_meanrev | Fade 2σ VWAP-band touches in ranging sessions. | VWAP bands | low | 5
96 | anchored_VWAP_event | Anchor VWAP to liq-cascade or news-event bar; trade reclaims. | Event clock + VWAP | med | 5

## 14. BOCPD changepoint entries
97 | bocpd_changepoint_breakout | On BOCPD run-length collapse, enter in direction of post-CP drift. | BOCPD posterior | med | 5
98 | bocpd_per_feature | Per-feature BOCPD (OFI / CVD / vol) — vote ensemble for regime switch. | Multi-feature BOCPD | high | 5
99 | bocpd_pause_gate | Inhibit all entries for K seconds after BOCPD alarm (regime uncertain). | BOCPD | low | 5
100 | bocpd_score_driven | Use score-driven BOCPD variant for persistent-flow regimes (Cartea 2024). | BOCPD-SD | high | 4
101 | bocpd_cross_pair | Pair-level BOCPD spike on majors triggers basket regime-switch policy. | Multi-pair BOCPD | high | 4

## 15. Deep-LOB neural signals
102 | deeplob_trend_classifier | DeepLOB (Zhang 2018) probability of up-move > θ → long. | L10 raw LOB | high | 4
103 | tlob_dual_attention | TLOB dual-attention transformer (Berti-Kasneci 2025) signal — long horizons. | L10 raw LOB | high | 4
104 | mlplob_baseline | Simple MLP LOB baseline (often beats CNN) as cheap distillation signal. | L10 raw LOB | med | 4
105 | tsetlin_microprice | Hyperdim Tsetlin-machine microprice (arXiv 2411.13594) for low-latency edge. | L10 raw LOB | high | 3
106 | cnn_transformer_hybrid | CNN-extracted features → transformer encoder for mid-price 1-5s forecast. | L10 raw LOB | high | 4
107 | lob_filtration_signal | "Filtered LOB" directional signal (arXiv 2507.22712) — noise-reduced OFI. | L10 raw LOB | high | 4

## 16. Stop-hunt / liquidity-grab fades
108 | swing_high_sweep_fade | Wick takes prior swing-high then closes back inside → short fade. | Price + swing detector | low | 5
109 | swing_low_sweep_fade | Wick takes prior swing-low then closes back inside → long fade. | Price + swing detector | low | 5
110 | round_number_stop_hunt | Fade wicks through psychological round numbers (e.g., $70k) with CVD divergence. | Price + CVD | low | 5
111 | equal_highs_lows_hunt | Liquidity pool at equal-highs/lows → fade sweep that fails. | Price levels | med | 5
112 | session_high_low_hunt | London/NY session high or low swept then reclaimed → fade. | Session clock + price | low | 5

## 17. Auction / session-open
113 | call_auction_imbalance | New-listing call-auction one-sided imbalance → trade direction on first 5min. | Auction feed | med | 3
114 | first_5min_open_fade | Fade first 5-min range extreme back to opening VWAP. | OHLC + VWAP | low | 4
115 | session_open_breakout | Asia/London/NY open breakout with delta confirm. | Session clock + delta | low | 5
116 | funding_settlement_flush | Pre-funding-settlement positioning fade (8h cadence). | Funding clock | low | 5

## 18. Cancel-rate / order-age / flicker
117 | cancel_storm_throttle | When cancel-rate > 5× baseline, throttle exposure. | L5 events | med | 4
118 | order_age_filter_OFI | Recompute OFI excluding orders living <100ms (filter ephemeral). | L5 + age tracking | high | 4
119 | quote_life_skew | Long-life resting orders weighted higher in book-pressure calc. | L5 + age | high | 4
120 | flicker_detection_veto | Detect flicker (rapid place-cancel same px) → veto trades referencing that level. | L5 events | med | 4
121 | latent_regime_early_warning | Early-warning regime detector on L5 stability metrics (arXiv 2604.20949). | L5 stability features | high | 5

---

## Notes on producers already available
Bot already emits **OBI raw + EMA, CVD, OFI, microprice, VPIN, Hawkes λ, MM-Hawkes spoof score, premium index, BOCPD per-pair, L1+L5 snapshots, >2σ large-trade flags**. Strategies 1–6, 9–10, 13–23, 24–30, 31–34, 38–39, 47–48, 50, 54–58, 60–67, 78–82, 97–101, 108–116 can be implemented with zero new producers. Remaining entries require: footprint bar builder (89–96), liq-heatmap ingest (83–84), multi-venue clock-synced feed (68–73), DeepLOB/TLOB model weights (102–107), per-order age tracker (118–120), iceberg refill tracker (40–41).

## Decay reminder
Per microstructure invariance literature: expect ~50% annual alpha decay for these signals; rotate aggressively and rely on the autonomous engine to retire dead genes.
