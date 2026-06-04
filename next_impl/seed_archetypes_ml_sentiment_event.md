# Seed Archetypes — ML / RL / AI / Sentiment / Event / Cross-Asset / Regime / Seasonality

Purpose: A broad seed gene-pool for the autonomous strategy-research engine. Each entry is a
single-line spec the discovery layer can instantiate, mutate, and combine. Strategies were
chosen to be **distinct in mechanism** (not just parameter variants) and to fit **crypto
perpetual futures** where the bot already lives.

Format: `name | mechanism (1 sentence) | required_data | complexity | crypto-perp_fit (1-5)`

Notation:
- complexity: low / med / high
- crypto-perp_fit: 1 (poor fit, included for completeness) ... 5 (high-conviction fit)
- "OHLCV" = standard candles; "funding" = perp funding rate; "OI" = open interest
- "LOB" = level-2 order book; "on-chain" = block/mempool/wallet flows
- "social" = Reddit/Twitter/Discord text streams; "news" = RSS / news APIs

Families:
1. ML supervised price/direction
2. Deep sequence (LSTM/GRU/Transformer/Mamba)
3. Foundation forecasters
4. Graph + cross-asset
5. Vision / chart-image
6. Anomaly / conformal
7. Reinforcement learning (model-free)
8. Reinforcement learning (model-based / planning)
9. Meta / inverse / imitation / bandit
10. Genetic / symbolic / LLM-driven alpha
11. LLM agents
12. Sentiment / NLP
13. Event-driven
14. Cross-asset / regime
15. Seasonality / calendar

The bot already has running production components for: PatchTST, TFT, Mamba forecaster,
GNN multiscale, HMM regime, BOCPD changepoint, CandleNet (1m/5m/15m), CryptoBERT+FinBERT,
web intelligence (Reddit+RSS), MARL, MAML, qlib alphas, LLM DSL alpha miner, world model
(DreamerV3 style), MCTS strategy search, Conformal wrapper, Kronos foundation, TLOB.
Archetypes below assume these as substrate — many entries are *strategies built on top of*
those models, not the models themselves.

---

## 1. ML supervised price / direction

1. xgb_directional_1h | XGBoost binary classifier on 100+ TA + funding + OI features predicting 1h direction | OHLCV, funding, OI | low | 5
2. lgbm_directional_5m | LightGBM next-bar direction with leaf-wise growth on microstructure features | OHLCV, LOB summaries | low | 5
3. catboost_categorical_session | CatBoost using categorical session/day/exchange/regime features for direction | OHLCV, calendar | low | 4
4. rf_volatility_regime_classifier | RandomForest classifies high/low vol regime; trades only in chosen regime | OHLCV, realized vol | low | 4
5. xgb_funding_mean_reversion | XGB predicting funding-rate mean-reversion magnitude; fade extremes | funding, OHLCV | low | 5
6. lgbm_liquidation_predictor | LightGBM predicts 5m liquidation cluster probability; front-runs cascade | liquidations, OI, OHLCV | med | 5
7. xgb_quantile_regression_bands | XGBoost quantile regression yields p10/p90 bands; trade band re-entry | OHLCV | low | 4
8. stacked_ml_meta_learner | Stack XGB + LGBM + CatBoost outputs into a logistic meta-model | OHLCV + features | med | 4
9. svm_kernel_breakout_filter | SVM with RBF kernel filters breakout signals by validity probability | OHLCV | low | 3
10. naive_bayes_event_classifier | Gaussian NB on event-window features for fast directional bet | OHLCV, event tags | low | 3

## 2. Deep sequence models (LSTM / GRU / Transformer / Mamba / SSM)

11. lstm_direction_15m | Stacked LSTM on 200-bar 15m window predicting sign of next return | OHLCV | med | 4
12. gru_volatility_forecast | GRU forecasting realized vol; trade vol-breakout when forecast > threshold | OHLCV | med | 4
13. bidirectional_lstm_with_attention | BiLSTM + soft attention over multi-timeframe features for entry | OHLCV multi-TF | med | 4
14. tft_strategy_overlay | Use existing TFT forecast + uncertainty to size momentum bets | TFT outputs | med | 5
15. patchtst_breakout_confirmer | Use PatchTST 15-min ahead forecast to confirm structural breakouts | PatchTST outputs | med | 5
16. informer_long_horizon | Informer ProbSparse attention forecasts 4h ahead for swing entries | OHLCV 1h | high | 4
17. autoformer_decomposition_trend | Autoformer trend-seasonal decomposition; trade trend component only | OHLCV | high | 4
18. fedformer_frequency_swing | FEDformer extracts dominant Fourier modes; trade cycle peaks/troughs | OHLCV | high | 3
19. itransformer_multivariate_basket | iTransformer treats each asset as a token; basket-level directional bet | multi-asset OHLCV | high | 4
20. mamba_microstructure | Mamba SSM on tick/LOB stream for sub-minute direction | LOB ticks | high | 5
21. cryptomamba_btc_forecast | CryptoMamba dedicated BTC predictor → bias filter for all longs | BTC OHLCV | med | 5
22. ssm_linear_attention_hybrid | Mamba + linear attention hybrid for very long context (5d) | OHLCV 1h | high | 3
23. tcn_dilated_direction | Temporal Convolutional Network with dilated kernels for direction | OHLCV | med | 4
24. nbeats_blocks_forecast | N-BEATS interpretable blocks for trend+seasonality forecast | OHLCV | med | 4
25. deepar_probabilistic | DeepAR autoregressive probabilistic forecast; bet on quantile-cross | OHLCV | med | 3

## 3. Foundation forecasters (zero/few-shot)

26. kronos_zero_shot_bias | Use Kronos zero-shot forecast as long/short bias gate | OHLCV | low | 5
27. timesfm_directional_vote | Google TimesFM forecast vote in ensemble of bias filters | OHLCV | low | 4
28. chronos_t5_quantile | Amazon Chronos quantile forecasts → band-trade strategy | OHLCV | low | 4
29. moirai_mixture_distribution | Moirai mixture-distribution probabilistic bands; fade band exits | OHLCV | med | 3
30. lagllama_few_shot_altcoin | Lag-Llama few-shot on new listings/altcoins (cold start) | OHLCV | med | 4
31. timer_xl_long_horizon | Timer-XL long-context forecast for swing/position trades | OHLCV | med | 3
32. foundation_ensemble_vote | Majority vote across Kronos+TimesFM+Chronos for high-conviction entries | foundation outputs | low | 5

## 4. Graph / cross-asset GNN

33. gnn_contagion_risk_off | Existing GNN multiscale spike → de-risk / short basket | GNN graph outputs | med | 5
34. temporal_gnn_lead_lag | T-GNN captures BTC→alt lead-lag; trade lagging alt on BTC move | multi-asset OHLCV | high | 5
35. dynamic_gnn_volatility_spillover | Dynamic-graph GNN predicts vol spillover; hedge in receiver asset | multi-asset OHLCV+vol | high | 4
36. correlation_network_clustering | Build rolling corr graph, trade pairs in same cluster on divergence | multi-asset OHLCV | med | 4
37. gcn_lstm_hybrid_forecast | GCN + LSTM hybrid forecasts price using neighbor-asset context | multi-asset OHLCV | high | 4
38. gnn_defi_tvl_signal | GNN over DeFi protocol graph (TVL flows) → bias on related tokens | on-chain TVL | high | 3
39. transaction_graph_anomaly | GNN anomaly on Ethereum tx graph signals smart-money rotation | on-chain tx | high | 3

## 5. Vision / chart-image strategies

40. gaf_cnn_pattern | Gramian Angular Field encodes 64-bar window → CNN classifies pattern | OHLCV | med | 4
41. mtf_image_cnn | Markov Transition Field image → CNN for state-transition direction | OHLCV | med | 3
42. candle_image_resnet | ResNet18 on rendered candlestick PNGs (60-bar) for direction | OHLCV | med | 3
43. candlenet_overlay | Use existing CandleNet 1m/5m/15m confidence as entry gate | CandleNet outputs | low | 5
44. yolo_chart_pattern_detector | YOLO-style object detector finds H&S / triangle / flag patterns on chart | OHLCV | high | 3
45. vit_chart_classifier | Vision Transformer (ViT) on chart image for pattern classification | OHLCV | high | 3

## 6. Anomaly detection / conformal

46. ae_reconstruction_anomaly_entry | Autoencoder reconstruction error spike → mean-reversion entry | OHLCV+LOB | med | 4
47. vae_latent_outlier | VAE latent outliers signal regime break; flip directional bias | OHLCV features | med | 3
48. iforest_microstructure_anomaly | IsolationForest on LOB features triggers fade-the-flash entries | LOB | low | 4
49. conformal_band_breakout | Trade only when price exits conformal prediction band (existing wrapper) | conformal outputs | low | 5
50. conformal_skip_filter | Skip any entry where conformal band width exceeds threshold | conformal outputs | low | 5
51. adaptive_conformal_quantile | Adaptive conformal quantile-tracking on returns; trade band re-entry | OHLCV | med | 4
52. transformer_ae_lob_manipulation | Transformer AE on LOB detects spoofing → fade the manipulation | LOB | high | 4

## 7. Model-free reinforcement learning

53. dqn_discrete_actions | Deep Q-Network with {long, flat, short} actions on engineered state | OHLCV+features | med | 4
54. double_dqn_image | Double-DQN over candlestick-image state | candle images | med | 3
55. rainbow_dqn | Rainbow (Dueling+Noisy+PER+C51+N-step+Double) DQN portfolio agent | OHLCV+features | high | 4
56. ppo_continuous_size | PPO with continuous position-size action | OHLCV+features | med | 5
57. sac_offpolicy | SAC entropy-regularized continuous control for position sizing | OHLCV+features | med | 5
58. td3_twin_critic | TD3 twin-critic continuous policy, low variance vs DDPG | OHLCV+features | med | 4
59. ddpg_continuous | DDPG continuous baseline (kept for comparison) | OHLCV+features | med | 3
60. a2c_synchronous | A2C parallel-env synchronous baseline | OHLCV+features | med | 3
61. impala_distributed | IMPALA distributed actor-learner over many symbols | multi-asset OHLCV | high | 3
62. marl_consensus_overlay | Existing MARL → trade only when N-of-K agents agree | MARL outputs | med | 5
63. ppo_lstm_recurrent | PPO with recurrent (LSTM) policy for partial observability | OHLCV | high | 4
64. cql_offline_rl | Conservative Q-Learning trained on historical replay only (no exploration risk) | replay logs | high | 4
65. iql_implicit_qlearning | Implicit Q-Learning offline RL on broker fill history | replay logs | high | 3
66. decision_transformer | Return-conditioned Decision Transformer over historical trajectories | replay logs | high | 4

## 8. Model-based RL / planning

67. dreamerv3_world_model | Existing DreamerV3 world-model imagined rollouts → policy | OHLCV+features | high | 5
68. muzero_planning | MuZero learned-model + MCTS for position/timing planning | OHLCV+features | high | 4
69. efficientzero_sample_eff | EfficientZero (self-supervised representation) on limited data | OHLCV | high | 3
70. mcts_strategy_search | Existing MCTS over discrete strategy nodes → adaptive ensemble | strategy bank | med | 5
71. mpc_dreamer_planner | Model-predictive control on learned latent dynamics | world model | high | 4
72. tdmpc2_trajectory | TD-MPC2 latent dynamics + trajectory optimization | OHLCV | high | 3

## 9. Meta / inverse / imitation / bandit

73. maml_fast_regime_adapt | Existing MAML fine-tunes on last-K bars at regime switch | OHLCV | med | 5
74. reptile_first_order_meta | Reptile first-order alternative to MAML for cheaper adaptation | OHLCV | med | 4
75. protonet_few_shot_pattern | Prototypical-net classifier learns new pattern from 5 examples | OHLCV | med | 3
76. airl_market_maker_reward | Adversarial IRL recovers reward from top market-maker logs | MM trade tape | high | 3
77. gail_top_trader_imitation | GAIL imitates top public copy-traders' action sequences | public trade tape | high | 3
78. bc_supervised_imitation | Behavior cloning from labeled "good" trades | replay logs | low | 3
79. ucb_strategy_bandit | UCB1 over strategy-bank picks current-best strategy each bar | strategy returns | low | 5
80. thompson_strategy_bandit | Thompson sampling over strategy-bank with Beta posteriors | strategy returns | low | 5
81. linucb_contextual | LinUCB selects strategy conditional on regime/context features | regime, returns | med | 5
82. exp3_adversarial_bandit | EXP3 adversarial bandit robust to non-stationary regimes | strategy returns | low | 4
83. neural_bandit | Neural-network bandit (DeepUCB) over high-dim context | regime + features | high | 3

## 10. Genetic / symbolic / LLM-driven alpha

84. gplearn_symbolic_alpha | gplearn symbolic regression mines formula alphas from OHLCV | OHLCV | med | 4
85. alphagen_rl_formula | AlphaGen RL agent generates formulaic alphas (existing) | OHLCV | high | 5
86. alpha2_logical_program | Alpha² assembles logical alpha programs via RL | OHLCV | high | 4
87. alphaforge_dynamic_combine | AlphaForge mines + dynamically combines factor pool | OHLCV+factor pool | high | 4
88. alphaagent_llm_regularized | LLM-driven AlphaAgent with decay-regularizers (existing) | OHLCV+LLM | high | 5
89. rd_agent_outer_loop | RD-Agent outer loop proposes & tests alphas autonomously (existing) | full pipeline | high | 4
90. qlib_alpha158_pool | Qlib Alpha158 factor pool combined via lgbm (existing) | OHLCV | low | 5
91. qlib_alpha360_pool | Qlib Alpha360 deeper factor pool variant | OHLCV | med | 4
92. differentiable_gp | Differentiable genetic programming for high-dim symbolic alpha | OHLCV+features | high | 3
93. wfo_genetic_mutation | Walk-forward genetic optimizer mutating existing strategy params | strategy bank | med | 4

## 11. LLM agents

94. llm_dsl_alpha_miner | Existing LLM DSL formula synthesizer | OHLCV+LLM | med | 5
95. fingpt_news_decision | FinGPT scores news → directional bias for tagged symbol | news | med | 4
96. reflective_llm_trader | Reflective LLM agent reasons over chart+news, refines via outcome memory | OHLCV+news+memory | high | 4
97. multimodal_llm_agent | Multimodal LLM (chart image + news text) emits trade plan | image+news | high | 3
98. llm_committee_debate | Bull/bear LLM debate → moderator decides; bot already has debate | LLM | med | 5
99. llm_strategy_synthesizer | LLM generates new Python strategy code (sandboxed) for backtest | LLM + backtester | high | 4
100. cot_explainability_filter | LLM chain-of-thought sanity-check on any RL action before send | RL action + LLM | med | 3

## 12. Sentiment / NLP

101. cryptobert_velocity | Existing CryptoBERT sentiment slope over 1h window as signal | social | low | 5
102. finbert_news_shock_fade | FinBERT-scored news shock → fade overshoot (existing model) | news | low | 5
103. finbert_news_shock_follow | FinBERT-scored news shock → follow when conf > 0.9 & volume confirms | news | low | 4
104. reddit_velocity_breakout | Reddit mention-velocity z-score breakout entry | Reddit | low | 4
105. twitter_engagement_surge | Twitter/X like+retweet surge per symbol triggers momentum entry | Twitter | med | 4
106. fear_greed_extreme_fade | Fade Fear&Greed <15 (long) and >80 (short) (research-validated) | FNG index | low | 5
107. funding_rate_sentiment_fade | Fade extreme positive funding (>0.05%); long extreme negative | funding | low | 5
108. long_short_ratio_fade | Fade exchange long/short ratio extremes | exchange L/S | low | 4
109. cvix_implied_vol_extreme | Deribit DVOL extremes as vol-regime entry/skip filter | DVOL | med | 4
110. lda_topic_rotation | LDA topic-model on news; trade tokens whose topic share is rising | news | med | 3
111. influencer_alpha_followup | Track curated influencer signal list → momentum trade on mention | Twitter | med | 3
112. whale_alert_followthrough | Whale-Alert tweet + on-chain confirmation → directional bet | Twitter+on-chain | med | 4
113. discord_telegram_alpha | Curated Discord/Telegram channel alpha-signal scraping | social | high | 2
114. google_trends_attention | Google Trends interest spike per token → momentum entry | Google Trends | low | 4
115. wikipedia_pageview_spike | Wikipedia pageview spike on token → attention-driven momentum | Wikipedia | low | 3
116. news_llm_directional_bias | LLM (existing pipeline) interprets news → directional bias filter | news+LLM | med | 5
117. sentiment_divergence_pricedown | Sentiment up but price down → bullish divergence entry | social+OHLCV | med | 4
118. cryptopanic_news_count_burst | News article-count burst on token signals event-driven entry | news API | low | 4

## 13. Event-driven

119. fomc_pre_event_derisk | Flatten/halve size in 60-min FOMC pre-window; re-enter post | macro calendar | low | 5
120. cpi_release_straddle | Long-vol option-style straddle on perps via gamma-scalp around CPI | macro calendar | med | 3
121. etf_flow_followthrough | Daily BTC/ETH ETF net inflow > $X → next-day long bias | ETF flow API | low | 5
122. etf_flow_reversal_fade | Multi-day extreme inflow streak → fade reversal | ETF flow | med | 4
123. exchange_listing_pump | New major-exchange listing announcement → momentum entry on token | listing feed | low | 4
124. delisting_short | Delisting announcement → short into rotation | listing feed | low | 4
125. hardfork_upgrade_runup | Buy 14d before scheduled upgrade, sell on event day | event calendar | low | 4
126. token_unlock_frontrun_short | Short into known large unlock 24-72h prior | unlock calendar | low | 5
127. token_unlock_postdump_long | Long the post-unlock capitulation low | unlock calendar | low | 4
128. halving_cycle_position | Position by halving-cycle phase (accumulation/markup/distribution) | halving calendar | low | 3
129. regulatory_event_fade | Fade initial overreaction to regulatory headlines after 2h | news | med | 4
130. earnings_lead_lag_coinbase | Coinbase/MicroStrategy earnings → next-session BTC bias | equity earnings | med | 3
131. validator_unbond_event | Staking unbond unlocks → short LST tokens at expiry | on-chain | high | 2
132. cme_gap_fill | Trade BTC CME futures weekend-gap-fill on Monday open | CME futures data | low | 4
133. liquidation_cascade_fade | Cascade > $200M in 5m → fade the wick (uses existing liq feed) | liquidations | low | 5
134. funding_payment_window | Fade extreme funding right before payment, take after | funding | low | 4

## 14. Cross-asset / regime

135. risk_on_off_spx_correlation | Long crypto only when SPX > 50d MA AND VIX < 20 | SPX, VIX | low | 5
136. dxy_regime_filter | Skip longs when DXY > 50d MA & rising | DXY | low | 5
137. btc_gold_correlation_regime | Trade BTC-gold rolling-corr regime flip (risk hedge → risk asset) | gold, BTC | med | 3
138. nasdaq_lead_lag | NASDAQ 15-min lead → bias BTC same direction | NDX intraday | med | 4
139. btc_dominance_rotation | BTC.D falling + ETH/BTC rising → rotate longs to large-cap alts | BTC.D, ETH/BTC | low | 5
140. altcoin_season_index | Trade alt basket when Altcoin Season Index > 75 | ASI | low | 4
141. correlation_breakdown_pairs | Pair-trade two normally-correlated coins when corr breaks | multi-asset | med | 4
142. sector_rotation_defi_l1_meme | Rotate exposure to leading sector (DeFi/L1/meme) weekly | sector indices | med | 5
143. stocks_open_reaction | Trade BTC reaction to US equities open (9:30 ET) | equities calendar | low | 4
144. real_yield_regime | Long crypto only when 10y real yield < 0% | macro yields | low | 3
145. eth_btc_ratio_breakout | Trade ETH/BTC ratio breakouts as alt-season proxy | ETH, BTC | low | 4
146. stablecoin_supply_pulse | Stablecoin total-supply ↑ > 2% / 7d → risk-on bias | on-chain stable supply | low | 4
147. hmm_regime_strategy_router | Existing HMM regime → route to regime-specialized strategy | HMM state | med | 5
148. bocpd_changepoint_flatten | Existing BOCPD changepoint detected → flatten & re-evaluate | BOCPD output | low | 5
149. vol_regime_garch | GARCH-classified high/low vol regime → switch sizing | OHLCV | low | 4
150. cross_exchange_basis | Spot-perp basis divergence between exchanges → arbitrage / lean | multi-venue | med | 4
151. funding_dispersion_regime | High dispersion in funding across venues → regime instability flag | funding multi | med | 3

## 15. Seasonality / calendar

152. asia_open_momentum | Long BTC bias 22:00–02:00 UTC (Asia Monday-open effect) | calendar | low | 5
153. us_session_breakout | US-session 13:30–15:30 UTC breakout filter | calendar | low | 5
154. eu_open_reversal | Fade overnight Asia move at EU open (06:00–08:00 UTC) | calendar | low | 3
155. friday_2100_utc_long | Friday 21:00–23:00 UTC long (research: highest seasonal return) | calendar | low | 5
156. weekend_low_liquidity_fade | Fade weekend spikes (low-liq, mean-revert Monday) | calendar | low | 4
157. monday_asia_open_long | Sunday-night/Monday Asia-open long bias | calendar | low | 4
158. end_of_month_flow | Long last 2 days / first 2 days of month (rebalance effect) | calendar | low | 4
159. end_of_quarter_derisk | De-risk last 3 days of quarter (institutional rebalance) | calendar | low | 3
160. deribit_monthly_opex_pin | Fade extension toward Deribit monthly-expiry max-pain strike | options OI | med | 4
161. deribit_weekly_opex_pin | Same as monthly but for weekly expiries | options OI | med | 3
162. funding_8h_cycle | Trade pre/post 8h funding payment window dynamics | funding | low | 4
163. tax_loss_harvest_dec | December tax-loss-selling dip → January rebound long | calendar | low | 3
164. us_holiday_low_liquidity | Reduce size / widen stops on US holidays | calendar | low | 4
165. chinese_new_year_dip | Annual CNY week pre-event de-risk, post-event re-enter | calendar | low | 3
166. cme_close_to_open_gap | Sun 18:00 CT CME futures open vs Fri close gap → reversion | CME calendar | low | 4
167. halving_phase_overlay | Phase-aware position scaler tied to halving-epoch month-count | halving calendar | low | 3

---

## Total strategies: 167

## Top picks for seed pool (high-ROI given existing infrastructure)

These leverage models the bot **already has running**, so the marginal cost to instantiate
is small while expected diversity benefit is large:

1. **#14 tft_strategy_overlay** — TFT already produces forecast+uncertainty; trivial to size by it.
2. **#15 patchtst_breakout_confirmer** — turns existing PatchTST into a high-precision breakout filter.
3. **#26 kronos_zero_shot_bias** — Kronos already loaded; use as a cheap directional gate.
4. **#33 gnn_contagion_risk_off** — existing GNN; turn its contagion score into a de-risk valve.
5. **#43 candlenet_overlay** — CandleNet already provides confidences; convert to entry gate.
6. **#49 conformal_band_breakout** — leverages existing conformal wrapper; very high statistical fit.
7. **#62 marl_consensus_overlay** — MARL agents already exist; require N-of-K consensus for entry.
8. **#67 dreamerv3_world_model** — world model already running; imagine rollouts for planning.
9. **#73 maml_fast_regime_adapt** — MAML stack present; ties beautifully with HMM/BOCPD output.
10. **#80 thompson_strategy_bandit** — pure orchestrator on the strategy bank; trivial to implement, huge upside.
11. **#88 alphaagent_llm_regularized** — already on roadmap (f60); decay-regularizers prevent overfit.
12. **#106 fear_greed_extreme_fade** — empirically strong, near-zero compute, complementary signal.

Honorable mentions: #126 token_unlock_frontrun_short (already feature-flagged in f52),
#133 liquidation_cascade_fade (already feature f58), #155 friday_2100_utc_long
(research-validated seasonality with high Calmar).
