"""F46 — Heikin-Ashi candle computation + 6 binary candle pattern detectors.

Pure numpy — no external dependencies beyond numpy. Used by ml/candlenet.py
to build the 17-feature input tensor for each candle.

Feature layout (per candle):
  idx 0–4 : raw OHLCV  (open, high, low, close, volume)
  idx 5–8 : Heikin-Ashi (ha_open, ha_high, ha_low, ha_close)
  idx 9   : doji flag
  idx 10  : hammer flag
  idx 11  : shooting_star flag
  idx 12  : bull_engulfing flag
  idx 13  : bear_engulfing flag
  idx 14  : morning_star flag
  idx 15  : OFI   (filled in by candlenet.py from Redis)
  idx 16  : VPIN  (filled in by candlenet.py from Redis)

N_FEATURES = 17
"""
import numpy as np

N_FEATURES = 17


def compute_ha(opens, highs, lows, closes):
    """Compute Heikin-Ashi OHLC arrays from raw OHLCV arrays.

    Formula matches TradingView exactly (noobquant1/tradingview-Heikin-Ashi-candle-calculation):
      HA_close[i] = (O[i] + H[i] + L[i] + C[i]) / 4
      HA_open[i]  = (HA_open[i-1] + HA_close[i-1]) / 2   (seed: (O[0]+C[0])/2)
      HA_high[i]  = max(H[i], HA_open[i], HA_close[i])
      HA_low[i]   = min(L[i], HA_open[i], HA_close[i])
    """
    n = len(closes)
    ha_close = (opens + highs + lows + closes) / 4.0
    ha_open = np.empty(n, dtype=np.float32)
    ha_open[0] = (opens[0] + closes[0]) / 2.0
    for i in range(1, n):
        ha_open[i] = (ha_open[i - 1] + ha_close[i - 1]) / 2.0
    ha_high = np.maximum(highs, np.maximum(ha_open, ha_close))
    ha_low = np.minimum(lows, np.minimum(ha_open, ha_close))
    return ha_open, ha_high, ha_low, ha_close


def _body_size(opens, closes):
    return np.abs(closes - opens)


def _range_size(highs, lows):
    return highs - lows


def _upper_wick(opens, highs, closes):
    return highs - np.maximum(opens, closes)


def _lower_wick(opens, lows, closes):
    return np.minimum(opens, closes) - lows


def _doji_flags(opens, highs, lows, closes, body_pct=0.10):
    """Body < body_pct of total range → indecision candle."""
    rng = _range_size(highs, lows)
    body = _body_size(opens, closes)
    return (rng > 0) & (body / (rng + 1e-10) < body_pct)


def _hammer_flags(opens, highs, lows, closes, wick_mult=2.0, upper_max=0.30):
    """Lower wick > wick_mult × body AND upper wick < upper_max × body."""
    body = _body_size(opens, closes)
    lower = _lower_wick(opens, lows, closes)
    upper = _upper_wick(opens, highs, closes)
    return (body > 0) & (lower > wick_mult * body) & (upper < upper_max * body)


def _shooting_star_flags(opens, highs, lows, closes, wick_mult=2.0, lower_max=0.30):
    """Upper wick > wick_mult × body AND lower wick < lower_max × body."""
    body = _body_size(opens, closes)
    upper = _upper_wick(opens, highs, closes)
    lower = _lower_wick(opens, lows, closes)
    return (body > 0) & (upper > wick_mult * body) & (lower < lower_max * body)


def _bull_engulfing_flags(opens, closes):
    """Current green body fully engulfs previous candle's body."""
    n = len(opens)
    flags = np.zeros(n, dtype=bool)
    for i in range(1, n):
        is_green = closes[i] > opens[i]
        prev_top = max(opens[i - 1], closes[i - 1])
        prev_bot = min(opens[i - 1], closes[i - 1])
        flags[i] = is_green and opens[i] <= prev_bot and closes[i] >= prev_top
    return flags


def _bear_engulfing_flags(opens, closes):
    """Current red body fully engulfs previous candle's body."""
    n = len(opens)
    flags = np.zeros(n, dtype=bool)
    for i in range(1, n):
        is_red = closes[i] < opens[i]
        prev_top = max(opens[i - 1], closes[i - 1])
        prev_bot = min(opens[i - 1], closes[i - 1])
        flags[i] = is_red and opens[i] >= prev_top and closes[i] <= prev_bot
    return flags


def _morning_star_flags(opens, highs, lows, closes, body_pct=0.10):
    """3-candle bullish reversal: bearish → doji/small-body → bullish.

    Simplified: candle[i-2] bearish, candle[i-1] doji, candle[i] bullish and
    closes above midpoint of candle[i-2].
    """
    n = len(opens)
    flags = np.zeros(n, dtype=bool)
    doji = _doji_flags(opens, highs, lows, closes, body_pct)
    for i in range(2, n):
        bearish_first = closes[i - 2] < opens[i - 2]
        small_middle = doji[i - 1]
        bullish_third = closes[i] > opens[i]
        mid_first = (opens[i - 2] + closes[i - 2]) / 2.0
        flags[i] = bearish_first and small_middle and bullish_third and closes[i] > mid_first
    return flags


def build_feature_matrix(opens, highs, lows, closes, volumes):
    """Build [N, 15] feature matrix (indices 0–14; 15 + 16 filled by caller).

    Input arrays must be float32, shape [N], chronological order.
    Normalisation is NOT applied here — candlenet.py normalises per-sequence.
    """
    n = len(closes)
    opens = np.asarray(opens, dtype=np.float32)
    highs = np.asarray(highs, dtype=np.float32)
    lows = np.asarray(lows, dtype=np.float32)
    closes = np.asarray(closes, dtype=np.float32)
    volumes = np.asarray(volumes, dtype=np.float32)

    ha_open, ha_high, ha_low, ha_close = compute_ha(opens, highs, lows, closes)

    doji    = _doji_flags(opens, highs, lows, closes).astype(np.float32)
    hammer  = _hammer_flags(opens, highs, lows, closes).astype(np.float32)
    shoot   = _shooting_star_flags(opens, highs, lows, closes).astype(np.float32)
    bull_e  = _bull_engulfing_flags(opens, closes).astype(np.float32)
    bear_e  = _bear_engulfing_flags(opens, closes).astype(np.float32)
    morn_s  = _morning_star_flags(opens, highs, lows, closes).astype(np.float32)

    mat = np.zeros((n, 15), dtype=np.float32)
    mat[:, 0] = opens
    mat[:, 1] = highs
    mat[:, 2] = lows
    mat[:, 3] = closes
    mat[:, 4] = volumes
    mat[:, 5] = ha_open
    mat[:, 6] = ha_high
    mat[:, 7] = ha_low
    mat[:, 8] = ha_close
    mat[:, 9] = doji
    mat[:, 10] = hammer
    mat[:, 11] = shoot
    mat[:, 12] = bull_e
    mat[:, 13] = bear_e
    mat[:, 14] = morn_s
    return mat


def compute_exhaustion_features(ha_opens, ha_closes, volumes,
                                shoot_flags=None, doji_flags=None):
    """Raw exhaustion-detection features for the most recent candle window.

    Blueprint F48 §Production Extension Idea A — Counter-trend detection.
    Returns a dict of raw values; Z-score normalisation against per-pair
    history is computed by the caller (run_inference) which owns the Redis
    EWMA stats. Keeping the Redis I/O outside this module preserves the
    pure-numpy contract that ha_features.py maintains.

    Returns:
      streak:           int  ≥ 0  — consecutive same-direction HA candles
      streak_direction: int  +1 = up streak, -1 = down streak, 0 = no streak
      body_slope:       float     — slope of HA body sizes across the streak
                                    (negative = bodies shrinking = exhaustion)
      vol_slope:        float     — slope of volume across the streak
                                    (negative = volume declining = exhaustion)
      pattern_flag:     0/1       — last candle is shooting_star or doji
      shrinking:        bool      — body_slope < 0
      vol_declining:    bool      — vol_slope < 0
    """
    ha_opens = np.asarray(ha_opens, dtype=np.float64)
    ha_closes = np.asarray(ha_closes, dtype=np.float64)
    volumes = np.asarray(volumes, dtype=np.float64)
    n = len(ha_closes)
    if n < 3:
        return {"streak": 0, "streak_direction": 0,
                "body_slope": 0.0, "vol_slope": 0.0,
                "pattern_flag": 0, "shrinking": False,
                "vol_declining": False}

    # Determine streak by walking backwards from the most recent candle
    last_dir = 1 if ha_closes[-1] >= ha_opens[-1] else -1
    streak = 1
    for i in range(n - 2, -1, -1):
        d = 1 if ha_closes[i] >= ha_opens[i] else -1
        if d != last_dir:
            break
        streak += 1
        if streak >= n:
            break

    streak = max(streak, 1)

    # Compute slopes over the streak window only — that's where exhaustion lives
    win = ha_closes[-streak:]
    win_open = ha_opens[-streak:]
    win_vol = volumes[-streak:]
    bodies = np.abs(win - win_open)
    body_slope = 0.0
    vol_slope = 0.0
    if streak >= 2:
        xs = np.arange(streak, dtype=np.float64)
        # numpy polyfit deg=1 → returns [slope, intercept]
        # Normalise slopes by mean to keep cross-pair comparable
        body_mean = float(bodies.mean()) or 1.0
        vol_mean = float(win_vol.mean()) or 1.0
        try:
            body_slope = float(np.polyfit(xs, bodies, 1)[0]) / body_mean
            vol_slope = float(np.polyfit(xs, win_vol, 1)[0]) / vol_mean
        except Exception:
            body_slope = 0.0
            vol_slope = 0.0

    # Pattern flag — exhaustion signature on last candle.
    # Shooting star at end of up-streak = bearish reversal; doji = indecision.
    # Caller may pass pre-computed flags; otherwise recompute from raw OHLC
    # equivalents inside ha_opens/ha_closes (approximate but safe).
    pat = 0
    if shoot_flags is not None and len(shoot_flags) > 0:
        if float(shoot_flags[-1]) > 0:
            pat = 1
    if pat == 0 and doji_flags is not None and len(doji_flags) > 0:
        if float(doji_flags[-1]) > 0:
            pat = 1

    return {
        "streak":            int(streak),
        "streak_direction":  int(last_dir),
        "body_slope":        round(body_slope, 6),
        "vol_slope":         round(vol_slope, 6),
        "pattern_flag":      int(pat),
        "shrinking":         body_slope < 0,
        "vol_declining":     vol_slope < 0,
    }


def compute_atr(highs, lows, closes, period=14):
    """ATR(period) from actual candle ranges.

    True Range = max(H-L, |H-prev_C|, |L-prev_C|)
    ATR = EMA(TR, period) — using Wilder's smoothing (alpha = 1/period).
    Returns float (latest ATR value) or None if insufficient data.
    """
    highs = np.asarray(highs, dtype=np.float64)
    lows = np.asarray(lows, dtype=np.float64)
    closes = np.asarray(closes, dtype=np.float64)
    n = len(closes)
    if n < period + 1:
        return None
    tr = np.empty(n)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]))
    # Wilder's smoothing
    atr = float(np.mean(tr[1:period + 1]))
    alpha = 1.0 / period
    for i in range(period + 1, n):
        atr = alpha * tr[i] + (1 - alpha) * atr
    return atr
