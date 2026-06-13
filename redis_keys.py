"""
F-01 / F-02 — Redis key registry and pub/sub channel names.

All modules must use these constants — never hardcode key strings inline.
Owner (write) and readers are noted for each key.

Key naming convention: {pair}:{metric} for per-pair data, plain name for global.
"""

# --- Per-pair data (owner: data_feed) ---
MARK_PRICE         = "{pair}:mark_price"        # str (float)
FUNDING_RATE       = "{pair}:funding_rate"       # str (float)
TICKER_VOLUME_24H  = "{pair}:volume_24h"         # str (float)
TICKER_CHANGE_24H  = "{pair}:change_24h"         # str (float)
LAST_PRICE         = "{pair}:last_price"         # str (float)
BID_PRICE          = "{pair}:bid"                # str (float)
ASK_PRICE          = "{pair}:ask"                # str (float)
BID_ASK_IMBALANCE  = "{pair}:bid_ask_imbalance"  # str (float)

# Candle buffer: last N closed candles per pair per timeframe
# Type: Redis List (LPUSH + LTRIM), each element is JSON-encoded OHLCV
CANDLES            = "{pair}:{interval}:candles"

# --- Microstructure (owner: data_feed) ---
VPIN               = "{pair}:vpin"               # str (float), TTL: 60s
OFI                = "{pair}:ofi"                # str (float), TTL: 60s
KYLES_LAMBDA       = "{pair}:kyles_lambda"       # str (float), TTL: 300s
AMIHUD             = "{pair}:amihud"             # str (float), TTL: 300s

# Global turbulence (owner: data_feed)
TURBULENCE_INDEX   = "turbulence_index"          # str (float)

# --- ML model outputs (owner: ml/) ---
CURRENT_REGIME     = "current_regime"            # str: bull | bear | turbulent (owner: ml/hmm.py)
REGIME_CONFIDENCE  = "regime_confidence"         # str (float)
PRICE_FORECAST     = "{pair}:{interval}:forecast"  # JSON: quantile distribution (owner: ml/tft.py)
LONGSEQ_FORECAST   = "{pair}:longseq_forecast"   # JSON (owner: ml/patchtst.py)
INTERASSET_SIGNALS = "interasset_signals"        # JSON: lead-lag matrix (owner: ml/gnn.py)
SENTIMENT_PAIR     = "{pair}:sentiment"          # str (float) (owner: ml/sentiment.py)
SENTIMENT_GLOBAL   = "global_sentiment"          # str (float)
SHADOW_WIN_RATE    = "shadow_win_rate"           # JSON: {total, won} (owner: signals/)

# F45 Cross-Sectional Momentum (Liu-Tsyvinski 2022) — owner: signals/xsmom.py
XSMOM_RANK         = "xsmom:rank:{pair}"         # str (float, 0.0-1.0 percentile)
XSMOM_RETURN_7D    = "xsmom:return_7d:{pair}"    # str (float, fraction e.g. 0.05 = +5%)
XSMOM_MAX_1H_7D    = "xsmom:max_1h_7d:{pair}"    # str (float, max abs 1h ret in 7d window)
XSMOM_UPDATED_AT   = "xsmom:updated_at"          # str (unix epoch)
XSMOM_UNIVERSE_SIZE = "xsmom:universe_size"      # str (int) — how many pairs in last ranking

# F52 — Exchange Net-Flow Directional Gate (cont. 55) — owner: data/onchain_netflow.py
EXCHANGE_NETFLOW_RAW      = "{pair}:exchange_netflow_raw"      # str (float, USD)
EXCHANGE_NETFLOW_Z        = "{pair}:exchange_netflow_z"        # str (float, z-score)
EXCHANGE_NETFLOW_REGIME   = "{pair}:exchange_netflow_regime"   # str: inflow | outflow | neutral
NETFLOW_UPDATED_AT        = "netflow:updated_at"               # str (epoch)
NETFLOW_DEADLOCK_DISABLED = "netflow:disabled"                 # str ("1" = kill switch)
NETFLOW_REJECT_COUNT      = "netflow:reject_count"             # str (int) — total parse/transport/http failures
NETFLOW_CALL_COUNT        = "netflow:call_count"               # str (int) — total provider calls

# Priority 2 (cont. 74) — OI velocity + Long/Short + Taker ratio — owner: data/oi_ls_taker.py
# Free production /futures/data Binance endpoints. The audit's Gate 3 (OI×price
# divergence) + Gate 4 (LS crowding) + taker aggressor momentum. TTL 600s.
OI_NOW            = "{pair}:oi_now"           # str (float) — current open interest (base units)
OI_CHANGE_5M      = "{pair}:oi_change_5m"     # str (float) — last 5m pct change in OI
OI_CHANGE_Z       = "{pair}:oi_change_z"      # str (float) — z-score of OI pct-change over 30 samples
OI_PRICE_DIV      = "{pair}:oi_price_div"     # str (float) — sign(Δoi)·sign(Δprice) ∈ {-1,0,+1} (Gate 3)
LS_GLOBAL_RATIO   = "{pair}:ls_global_ratio"  # str (float) — global account long/short ratio (retail crowd)
LS_TOP_RATIO      = "{pair}:ls_top_ratio"     # str (float) — top-trader position long/short ratio (smart money)
LS_CROWD_Z        = "{pair}:ls_crowd_z"       # str (float) — z-score of top-trader ratio over 30 samples (Gate 4)
TAKER_RATIO       = "{pair}:taker_ratio"      # str (float) — taker buy/sell volume ratio (>1 = buyers aggress)
TAKER_RATIO_Z     = "{pair}:taker_ratio_z"    # str (float) — z-score of taker ratio over 30 samples
OILS_UPDATED_AT   = "oils:updated_at"         # str (epoch)
OILS_DISABLED     = "oils:disabled"           # str ("1" = kill switch / deadlock)
OILS_CALL_COUNT   = "oils:call_count"         # str (int)
OILS_REJECT_COUNT = "oils:reject_count"       # str (int)

# F53 — Qlib Alpha-158 Formulaic Alpha Pool (cont. 55) — owner: ml/qlib_alphas.py
QLIB_ALPHA_VALUE          = "{pair}:qlib_alpha:{factor_id}"    # str (float) per-pair per-factor instantaneous value
QLIB_IC_HISTORY           = "qlib:ic_history:{factor_id}"      # JSON list — rolling 7d hourly IC samples
QLIB_IC_ROLLING           = "qlib:ic_rolling:{factor_id}"      # str (float) — current 7d mean IC
QLIB_TOP_K_FACTORS        = "qlib:top_k_factor_ids"            # JSON list[str] — currently selected
QLIB_TOP_K_UPDATED_AT     = "qlib:top_k_updated_at"            # str (epoch)
QLIB_DISABLED             = "qlib:disabled"                    # str ("1" = kill switch)
QLIB_COMPUTE_HEALTH       = "qlib:compute_health"              # JSON: {ok, failed, last_run}

# F54 — Crypto LLM-DSL Alpha Miner (cont. 55) — owner: ml/llm_alpha_dsl.py
DSL_PROMOTED_FACTORS      = "dsl:promoted_factors"      # JSON list[dict]
DSL_REJECTED_FACTORS      = "dsl:rejected_factors"      # JSON list[dict] (capped 1000)
DSL_LAST_MINING_RUN       = "dsl:last_mining_run"       # str (epoch)
DSL_MINING_HEALTH         = "dsl:mining_health"         # JSON: {accepted, rejected, errors, elapsed_s}
DSL_DISABLED              = "dsl:disabled"              # str ("1" = kill switch)
DSL_ALPHA_VALUE           = "{pair}:dsl_alpha:{factor_hash}"   # str (float) per-pair per-factor value

# F56 — Conformal Prediction Wrapper (cont. 56) — owner: ml/conformal_wrapper.py
CONFORMAL_WIDTH           = "conformal:width:{model}"   # str (float) — calibrated half-width
CONFORMAL_N_CALIB         = "conformal:n_calib:{model}" # str (int)
CONFORMAL_ALPHA           = "conformal:alpha:{model}"   # str (float) — α used
CONFORMAL_LAST_UPDATE     = "conformal:last_update"     # str (epoch)
CONFORMAL_HEALTH          = "conformal:health"          # JSON: {ts, warmed_up, cold}
CONFORMAL_DISABLED        = "conformal:disabled"        # str ("1" = kill switch)

# F58 — Liquidation Cascade Alpha (cont. 56) — owner: data/liquidation_levels.py
LIQ_CLUSTERS_JSON         = "{pair}:liq_clusters_json"         # JSON list[dict]
LIQ_NEAREST_ABOVE_PCT     = "{pair}:liq_nearest_above_pct"     # str (float) — distance fraction
LIQ_NEAREST_BELOW_PCT     = "{pair}:liq_nearest_below_pct"     # str (float)
LIQ_CASCADE_DIRECTION     = "{pair}:liq_cascade_direction"     # str: long | short | neutral
LIQ_CASCADE_PROB          = "{pair}:liq_cascade_prob"          # str (float [0,1])
LIQ_SOURCE                = "{pair}:liq_source"                # str: coinglass | coinalyze | proxy
LIQ_UPDATED_AT            = "liq:updated_at"                   # str (epoch)
LIQ_DISABLED              = "liq:disabled"                     # str ("1" = kill switch)
LIQ_REJECT_COUNT          = "liq:reject_count"                 # str (int)
LIQ_CALL_COUNT            = "liq:call_count"                   # str (int)

# cont. 69x item 2 — real-time liquidation FLOW from !forceOrder@arr (owner:
# data/liq_ws.py). Distinct from the cluster LEVELS above: these are actual
# forced-order prints in a rolling window. side=SELL = long liquidated (bearish
# → "short"); side=BUY = short liquidated (bullish → "long").
LIQ_FLOW_DIR              = "{pair}:liq_flow_dir"              # str: long | short | neutral
LIQ_FLOW_INTENSITY        = "{pair}:liq_flow_intensity"       # str (float [0,1])
LIQ_FLOW_BUY_NOTIONAL     = "{pair}:liq_buy_notional"         # str (USD, window) short-liqs
LIQ_FLOW_SELL_NOTIONAL    = "{pair}:liq_sell_notional"        # str (USD, window) long-liqs
LIQ_GLOBAL_RATE           = "liq:global_rate"                 # str (USD/min market-wide)
LIQ_WS_CONNECTED          = "liq:ws:connected"                # str (epoch heartbeat)
LIQ_WS_EVENTS_TOTAL       = "liq:ws:events_total"             # str (int)

# F60 — AlphaAgent novelty regularisers (cont. 56) — owner: ml/dsl_ast_embed.py
DSL_REMINING_HINTS        = "dsl:remining_hints"        # JSON list[dict] — capped 50

# Per-trade SL overrides (Blueprint 15.4 "any trade's trailing SL at any time")
# owner: Brain. Read by risk/manager.py:monitor_trailing_sl. All optional.
TRADE_TRAIL_DIST_OVERRIDE  = "trade:{trade_id}:trail_dist_pct"   # str (float, 0.003-0.05)
TRADE_TRAIL_ACT_OVERRIDE   = "trade:{trade_id}:trail_activation_pct"  # str (float, 0.002-0.05)
TRADE_TRAIL_FROZEN         = "trade:{trade_id}:trail_frozen"     # str ("1" = frozen)

# Capital-anchored SL + activation + dead-trade time-exit knobs (cont. 62, 2026-05-29).
# All optional, runtime-overridable; defaults baked into risk/manager.py.
RISK_CAPITAL_SL_FRAC          = "risk:capital_sl_frac"             # str float, default 0.50
RISK_CAPITAL_SL_DISABLED      = "risk:capital_sl_frac_disabled"    # "1" → off
RISK_CAPITAL_ACT_FRAC         = "risk:capital_activation_frac"     # str float, default 0.11
RISK_CAPITAL_ACT_DISABLED     = "risk:capital_activation_disabled" # "1" → off
RISK_DEAD_TRADE_MIN_AGE_S     = "risk:dead_trade_min_age_s"        # default 2700 (45 min)
RISK_DEAD_TRADE_ABS_USDT      = "risk:dead_trade_small_abs_usdt"   # default 2.0
RISK_DEAD_TRADE_PCT_CAPITAL   = "risk:dead_trade_small_pct_capital"# default 0.015 (1.5%)
RISK_DEAD_TRADE_PEAK_USDT     = "risk:dead_trade_peak_threshold_usdt" # default 0.5
RISK_DEAD_TRADE_FORCE_MAX_S   = "risk:dead_trade_force_close_max_s"   # default 21600 (6 h)
RISK_DEAD_TRADE_DISABLED      = "risk:dead_trade_disabled"         # "1" → off

# Brain state cache (owner: brain/)
BRAIN_STAGE        = "brain:stage"              # str (int)
BRAIN_FEATURE_WEIGHTS = "brain:feature_weights" # JSON dict
BRAIN_ACTIVE_FLAGS = "brain:active_feature_flags"  # JSON dict
EXPLORATION_COUNTER = "brain:exploration_trades_this_session"  # str (int)

# F9/F12 Decoder writeback (owner: metacognition/actuator, cont. 27).
# JSON dict of accumulated deltas applied on top of GA/config defaults in
# signals/engine.py. Hard-capped by metacognition.actuator._MAX_DRIFT so
# runaway LLM proposals cannot break the system. del-able for reset.
BRAIN_FILTER_OVERRIDES = "brain:filter_overrides"   # JSON dict — F9 writeback (v2 bucketed since cont. 55)
BRAIN_SCORER_OVERRIDES = "brain:scorer_overrides"   # JSON dict — F12 writeback
BRAIN_PAIR_PROBATION   = "brain:pair_probation"     # JSON dict — R2 per-pair probation list
BRAIN_PAIR_SUSPENSION  = "brain:pair_suspension"    # JSON dict — R2 per-pair suspension list
BRAIN_BOT_CONFIDENCE   = "brain:bot_confidence"     # str (float) — R4 bot self-confidence index

# Account state (owner: data_feed / execution)
ACCOUNT_BALANCE    = "account:balance_usdt"      # str (float)
VIRTUAL_BALANCE    = "account:virtual_balance"   # str (float) — paper mode only
MARGIN_RATIO       = "account:margin_ratio"      # str (float)
TOTAL_EXPOSURE     = "account:total_exposure"    # str (float)
UNREALISED_PNL     = "account:unrealised_pnl"   # str (float)

# Active pairs list (owner: scanner/)
ACTIVE_PAIRS       = "scanner:active_pairs"      # Redis Set
SCANNER_ANCHOR_PAIRS = "scanner:anchor_pairs"    # Redis Set — manually-curated majors, always union'd into ACTIVE_PAIRS (cont. 41)

# Categorised 10-bucket × 10-pair scanner (cont. 62c, 2026-05-29 owner mandate).
# Active universe = 100 bucketed picks + composite tail to max_active_pairs.
SCANNER_PAIR_BUCKET            = "scanner:pair_bucket:{pair}"        # str — bucket name; TTL 6h
SCANNER_CATEGORISED_DISABLED   = "scanner:categorised_mode_disabled" # "1" → revert to legacy
SCANNER_CATEGORY_SLUGS         = "scanner:category_slugs"            # JSON override of DEFAULT_BUCKETS
SCANNER_CATEGORY_CACHE_STALE   = "scanner:category_cache_stale"      # "1" → force CG refetch
SCANNER_PER_BUCKET_TARGET      = "scanner:per_bucket_target"         # int (default 10)
SCANNER_BUCKETED_CORE_TARGET   = "scanner:bucketed_core_target"      # int (default 100)
SCANNER_RERANK_INTERVAL_MIN    = "scanner:rerank_interval_minutes"   # float (default 20, clamp [5, 180])

# Fast memory (owner: memory/)
FAST_MEMORY        = "memory:fast_trade_ids"     # Redis Sorted Set (score=timestamp)

# OPRO (owner: brain/self_improve)
OPRO_WINDOW_COUNTER = "opro:window_counter"      # str (int)

# Signal Monitor Phases 1-4 (cont. 63, 2026-05-29) — owner: signals/replay_pool.py
#   signals/bayes_threshold.py, signals/slot_selector.py,
#   signals/utility_calibration.py.
# Hysteresis replay pool + Bayesian Beta adaptive threshold + Multi-play
# Thompson sampling slot selector + utility-weighted walk-forward calibration.

# Phase 1 — Hysteresis Replay Pool
REPLAY_POOL                = "signals:replay_pool"                  # ZSET, score=push_ts
REPLAY_POOL_ENABLED        = "signals:replay_pool:enabled"          # "1" | "0"
REPLAY_POOL_TTL_SECONDS    = "signals:replay:ttl_seconds"           # int default 900
REPLAY_POOL_DRIFT_PCT_MAX  = "signals:replay:drift_pct_max"         # float default 2.0
REPLAY_POOL_MAX_ENTRIES    = "signals:replay:max_entries"           # int default 100
REPLAY_PRODUCE_COUNT       = "signals:replay:produce_count"
REPLAY_CONSUME_COUNT       = "signals:replay:consume_count"
REPLAY_STALE_AGE_COUNT     = "signals:replay:stale_age_count"
REPLAY_STALE_DRIFT_COUNT   = "signals:replay:stale_drift_count"
REPLAY_STALE_REGIME_COUNT  = "signals:replay:stale_regime_count"
REPLAY_STALE_PAIR_COUNT    = "signals:replay:stale_pair_count"
REPLAY_LAST_PRODUCE_TS     = "signals:replay:last_produce_ts"
REPLAY_LAST_CONSUME_TS     = "signals:replay:last_consume_ts"

# Phase 2 — Bayesian Beta-Distribution Adaptive Threshold
BAYES_THRESHOLD_ENABLED    = "bayes_threshold:enabled"              # "1" | "0"
BAYES_THRESHOLD_T_HIGH     = "bayes_threshold:t_high"               # float
BAYES_THRESHOLD_T_LOW      = "bayes_threshold:t_low"                # float
BAYES_THRESHOLD_REFRESH_TS = "bayes_threshold:last_refresh_ts"
BAYES_BUCKET_ALPHA         = "bayes_threshold:bucket:{lo}_{hi}:alpha"
BAYES_BUCKET_BETA          = "bayes_threshold:bucket:{lo}_{hi}:beta"
BAYES_APPLIED_COUNT        = "bayes_threshold:applied_count"

# Phase 3 — Position-Based Multi-Play Thompson Sampling Slot Selector
BANDIT_ENABLED             = "bandit:enabled"                       # "1" | "0"; default "0"
BANDIT_PAIR_REGIME_ALPHA   = "bandit:pair_regime:{pair}:{regime}:alpha"
BANDIT_PAIR_REGIME_BETA    = "bandit:pair_regime:{pair}:{regime}:beta"
BANDIT_SELECTION_COUNT     = "bandit:selection_count"

# Phase 4 — Utility-Weighted Walk-Forward Calibration
UTIL_CALIB_ENABLED         = "util_calib:enabled"                   # "1" | "0"
UTIL_CALIB_T_HIGH          = "util_calib:recommended_t_high"        # float
UTIL_CALIB_RUN_TS          = "util_calib:last_run_ts"
UTIL_CALIB_UTILITY_DELTA   = "util_calib:utility_delta_vs_current"  # float
UTIL_CALIB_FOLDS_AGREE     = "util_calib:folds_agree"               # int 0-4
UTIL_CALIB_LAST_REPORT     = "util_calib:last_report"               # JSON

# Launch-Pad (cont. 70, 2026-06-03) — owner: signals/launch_pad/{maintainer,shadow,qualify}.py
# A 10-deep on-deck buffer of pre-qualified symbols = SOLE funnel for opening trades
# (plan: /opt/trading-bot/next_impl/launch_pad_10.md). Ships behind LAUNCHPAD_ENABLED=0
# (shadow-only) until reviewed. Postgres table launch_pad is the source of truth;
# these keys are the hot Redis mirror + tunables + silent-rejection counters.
LAUNCHPAD_ENABLED        = "launchpad:enabled"          # "1"|"0"; default "0" (shadow-only / legacy flow live)
LAUNCHPAD_DEPTH          = "launchpad:depth"            # int; default 10 (buffer slots)
LAUNCHPAD_SLOTS          = "launchpad:slots"            # HASH slot(str)->JSON occupant (hot mirror of launch_pad)
LAUNCHPAD_COOLDOWN       = "launchpad:cooldown"         # ZSET symbol->cooldown_until_ts (after flip-cap / fire)
LAUNCHPAD_TTL_SECONDS    = "launchpad:ttl_seconds"      # int; default 1800 — staged candidate staleness
LAUNCHPAD_COOLDOWN_SECONDS = "launchpad:cooldown_seconds" # int; default 900 — post-exit re-entry block
# Flip rule (D8): flip only when peak_loss(MAE) past threshold AND momentum reversal confirmed; capped.
LAUNCHPAD_FLIP_MAE_PCT   = "launchpad:flip_mae_pct"     # float; default 1.5 — adverse % to consider a flip
LAUNCHPAD_FLIP_CAP       = "launchpad:flip_cap"         # int; default 2 — flips before symbol -> cooldown
# Displacement: an outside candidate must beat the weakest slot's score by this margin to displace it.
LAUNCHPAD_DISPLACE_MARGIN = "launchpad:displace_margin" # float; default 0.10 (10% better)
LAUNCHPAD_RANK_METRIC    = "launchpad:rank_metric"      # str; default "mv_realized" (which of the 3 ranks; review later)
# Health / telemetry counters
LAUNCHPAD_MAINTAIN_COUNT = "launchpad:maintain_count"   # int — maintainer loop iterations
LAUNCHPAD_REFILL_COUNT   = "launchpad:refill_count"     # int — slot refills
LAUNCHPAD_FLIP_COUNT     = "launchpad:flip_count"       # int — direction flips applied
LAUNCHPAD_DISPLACE_COUNT = "launchpad:displace_count"   # int — weakest-slot displacements
LAUNCHPAD_OPEN_COUNT     = "launchpad:open_count"       # int — trades opened from buffer (P5)
LAUNCHPAD_LAST_RUN_TS    = "launchpad:last_run_ts"      # str (epoch)
# Qualify gate ("open-green") silent-rejection + deadlock (per silent-rejection / RL-deadlock rules).
LAUNCHPAD_QUALIFY_TOTAL  = "launchpad:qualify:total_calls"   # int
LAUNCHPAD_QUALIFY_REJECT = "launchpad:qualify:reject:{reason}" # int per reason
LAUNCHPAD_QUALIFY_DISABLED = "launchpad:qualify:disabled"   # "1" — auto-disabled when reject rate > 80%/50+

# cont. 70d — Replay-pool → Launch-Pad integration. Recoverable rejected signals
# are staged as EXTRA slots on TOP of the base depth (slot ids >= 1001), tagged
# source='replay' permanently (survives into launch_pad_history + trades for the
# how-did-replay-signals-do study). Kill switch default OFF.
LAUNCHPAD_REPLAY_ENABLED      = "launchpad:replay_slots_enabled"  # "1"|"0"; default "0"
LAUNCHPAD_REPLAY_MAX_SLOTS    = "launchpad:replay_max_slots"      # int; default 5 — cap on extra replay slots
LAUNCHPAD_REPLAY_STAGED_COUNT = "launchpad:replay:staged_count"   # int — replay entries staged into the table
LAUNCHPAD_REPLAY_EXPIRE_COUNT = "launchpad:replay:expire_count"   # int — replay slots aged out unfired

# --- Pub/Sub channels (F-02) ---
CH_PRICE_UPDATE    = "price_update"
CH_TRADE_OPENED    = "trade_opened"
CH_TRADE_CLOSED    = "trade_closed"
CH_SL_MOVED        = "sl_moved"
CH_DCA_TRIGGERED   = "dca_triggered"
CH_BRAIN_DECISION  = "brain_decision"
CH_SIGNAL_GENERATED = "signal_generated"
CH_ALERT           = "alert"
CH_FEATURE_EVENT   = "feature_event"
CH_SYSTEM_EVENT    = "system_event"
CH_BRAIN_METRICS   = "brain_metrics"
