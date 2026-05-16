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

# Brain state cache (owner: brain/)
BRAIN_STAGE        = "brain:stage"              # str (int)
BRAIN_FEATURE_WEIGHTS = "brain:feature_weights" # JSON dict
BRAIN_ACTIVE_FLAGS = "brain:active_feature_flags"  # JSON dict
EXPLORATION_COUNTER = "brain:exploration_trades_this_session"  # str (int)

# Account state (owner: data_feed / execution)
ACCOUNT_BALANCE    = "account:balance_usdt"      # str (float)
VIRTUAL_BALANCE    = "account:virtual_balance"   # str (float) — paper mode only
MARGIN_RATIO       = "account:margin_ratio"      # str (float)
TOTAL_EXPOSURE     = "account:total_exposure"    # str (float)
UNREALISED_PNL     = "account:unrealised_pnl"   # str (float)

# Active pairs list (owner: scanner/)
ACTIVE_PAIRS       = "scanner:active_pairs"      # Redis Set

# Fast memory (owner: memory/)
FAST_MEMORY        = "memory:fast_trade_ids"     # Redis Sorted Set (score=timestamp)

# OPRO (owner: brain/self_improve)
OPRO_WINDOW_COUNTER = "opro:window_counter"      # str (int)

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
