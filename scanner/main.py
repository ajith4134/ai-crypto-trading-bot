"""
Section R: Pair Scanner & Selector — R-01 to R-11.
Scans all USDT-M futures, scores on 5 criteria, maintains active pair list.
"""
import asyncio
import json
import time
import structlog
import numpy as np
import redis_client
import redis_keys
import config
from db import db_conn

log = structlog.get_logger()


def _wilder_adx(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                period: int = 14) -> float:
    """Compute ADX(14) using Wilder's smoothing. Returns the latest ADX value.

    Pure numpy — no TA-Lib dependency. Mirrors TA-Lib's ADX implementation:
      TR        = max(H-L, |H-prevC|, |L-prevC|)
      +DM       = max(H-prevH, 0) when (H-prevH) > (prevL-L), else 0
      -DM       = max(prevL-L, 0) when (prevL-L) > (H-prevH), else 0
      ATR/+DM14/-DM14 = Wilder EMA with alpha = 1/period
      +DI = 100 × +DM14 / ATR ;  -DI = 100 × -DM14 / ATR
      DX  = 100 × |+DI − -DI| / (+DI + -DI)
      ADX = Wilder EMA(DX)

    Returns 0.0 when the input is too short (<2*period+1 bars) for a stable
    reading, signalling "no opinion" rather than a false low/high.
    """
    n = len(closes)
    if n < 2 * period + 1:
        return 0.0
    prev_close = closes[:-1]
    h, l, c = highs[1:], lows[1:], closes[1:]
    tr = np.maximum.reduce([h - l, np.abs(h - prev_close), np.abs(l - prev_close)])

    up   = h - highs[:-1]
    down = lows[:-1] - l
    plus_dm  = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)

    def _wilder_ema(x: np.ndarray) -> np.ndarray:
        out = np.zeros_like(x)
        if len(x) < period:
            return out
        out[period - 1] = x[:period].sum()
        for i in range(period, len(x)):
            out[i] = out[i - 1] - (out[i - 1] / period) + x[i]
        return out

    atr_s    = _wilder_ema(tr)
    plus_s   = _wilder_ema(plus_dm)
    minus_s  = _wilder_ema(minus_dm)

    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di  = 100.0 * np.where(atr_s > 0, plus_s / atr_s, 0.0)
        minus_di = 100.0 * np.where(atr_s > 0, minus_s / atr_s, 0.0)
        di_sum   = plus_di + minus_di
        dx       = 100.0 * np.where(di_sum > 0, np.abs(plus_di - minus_di) / di_sum, 0.0)

    # ADX = Wilder EMA of DX; valid output starts at index 2*period-1
    adx_arr = np.zeros_like(dx)
    if len(dx) >= 2 * period:
        adx_arr[2 * period - 1] = dx[period - 1: 2 * period - 1].mean() if (2 * period - 1) > period - 1 else dx[2 * period - 1]
        for i in range(2 * period, len(dx)):
            adx_arr[i] = (adx_arr[i - 1] * (period - 1) + dx[i]) / period
    return float(adx_arr[-1]) if adx_arr[-1] > 0 else 0.0


def _wilder_rsi(closes: np.ndarray, period: int = 14) -> float:
    """Compute RSI(14) using Wilder's smoothing. Returns the latest RSI (0-100).

    Pure numpy. Returns 50.0 (neutral "no opinion") when input is too short
    (<period+1 bars), mirroring _wilder_adx's no-data convention.
    """
    n = len(closes)
    if n < period + 1:
        return 50.0
    deltas = np.diff(closes)
    gains = np.where(deltas > 0.0, deltas, 0.0)
    losses = np.where(deltas < 0.0, -deltas, 0.0)
    avg_gain = float(gains[:period].mean())
    avg_loss = float(losses[:period].mean())
    # Wilder smoothing over the remaining bars.
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _rsi_momentum_score(rsi: float) -> float:
    """Map RSI → 0-100 momentum score, SYMMETRIC around 50.

    Deliberate deviation from the long-only "RSI 60-75" rule in the pasted
    framework: this bot trades BOTH directions, so strong DOWN-momentum
    (RSI 25-40) is as tradeable (short) as strong UP-momentum (RSI 60-75).
    Crucially, RSI ~50 means NO momentum — exactly the flat "no-movement"
    pairs we want to deprioritise. So we score by distance from the 50
    midline:
        |RSI-50| in 10..25  → 100  (RSI 60-75 OR 25-40: strong directional momentum)
        |RSI-50| in 25..30  →  70  (75-80 / 20-25: strong but extending)
        |RSI-50| in  5..10  →  60  (building momentum)
        |RSI-50| <= 5       →  25  (45-55: FLAT — the no-mover penalty zone)
        |RSI-50| >  30      →  35  (>80 / <20: exhausted, reversion risk)
    """
    d = abs(rsi - 50.0)
    if d <= 5.0:
        return 25.0
    if d < 10.0:
        return 60.0
    if d <= 25.0:
        return 100.0
    if d <= 30.0:
        return 70.0
    return 35.0


def _ema_distance_score(closes: np.ndarray, span: int = 50) -> float:
    """Map |price − EMA50| / EMA50 → 0-100 trend-extension score, symmetric.

    Reward pairs trending away from their 50-EMA in EITHER direction (real
    movement), penalise pairs hugging the EMA (flat) and pairs stretched too
    far (mean-reversion risk):
        |dist| < 0.5%      →  25  (hugging EMA — flat, no-mover penalty)
        0.5% .. 4%         → 100  (healthy directional trend)
        4% .. 8%           →  65  (extended)
        > 8%               →  35  (overextended — reversion risk)
    Returns 50.0 (neutral) when there aren't enough bars for a stable EMA.
    """
    n = len(closes)
    if n < span:
        return 50.0
    # Standard EMA (alpha = 2/(span+1)), seeded with the first close.
    alpha = 2.0 / (span + 1.0)
    ema = float(closes[0])
    for c in closes[1:]:
        ema = alpha * float(c) + (1.0 - alpha) * ema
    if ema <= 0.0:
        return 50.0
    a = abs((float(closes[-1]) - ema) / ema)
    if a < 0.005:
        return 25.0
    if a <= 0.04:
        return 100.0
    if a <= 0.08:
        return 65.0
    return 35.0


def score_trend_momentum(
    symbols: list[str], exchange_client
) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    """Fetch 1h klines ONCE per symbol and compute ADX(14), RSI(14) and
    50-EMA-distance scores together. Returns (adx_s, rsi_s, ema_s).

    Replaces three separate single-metric passes (each re-fetching the same
    klines) with one bounded fetch loop — same top-N candidate set + API
    budget pattern as the former score_adx_trend (50 calls × weight 2 / 8h).
    All three default to neutral (50.0) on missing/short data so pairs we
    couldn't fetch are neither rewarded nor penalised.
    """
    adx_s: dict[str, float] = {}
    rsi_s: dict[str, float] = {}
    ema_s: dict[str, float] = {}
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - 60 * 60 * 60 * 1000   # 60h of 1h candles (need ≥50 for EMA50)
    for sym in symbols:
        try:
            kl = exchange_client.get_historical_klines(sym, "1h", start_ms, end_ms)
            if not kl or len(kl) < 30:
                adx_s[sym] = rsi_s[sym] = ema_s[sym] = 50.0
                continue
            highs  = np.array([float(k[2]) for k in kl])
            lows   = np.array([float(k[3]) for k in kl])
            closes = np.array([float(k[4]) for k in kl])
            adx_val = _wilder_adx(highs, lows, closes, period=14)
            if adx_val >= 25.0:
                adx_s[sym] = 80.0
            elif adx_val >= 20.0:
                adx_s[sym] = 50.0
            elif adx_val > 0:
                adx_s[sym] = 20.0
            else:
                adx_s[sym] = 50.0
            rsi_s[sym] = _rsi_momentum_score(_wilder_rsi(closes, period=14))
            ema_s[sym] = _ema_distance_score(closes, span=50)
        except Exception as exc:
            log.debug("trend_momentum_fetch_failed", sym=sym, error=str(exc)[:120])
            adx_s[sym] = rsi_s[sym] = ema_s[sym] = 50.0
    return adx_s, rsi_s, ema_s


def score_adx_trend(symbols: list[str], exchange_client) -> dict[str, float]:
    """F51c (cont. 51): ADX(14) on 1h candles for the trimmed top-N candidate
    list. Distinguishes trending pairs (good for CandleNet's directional
    setups) from choppy/range-bound pairs (CandleNet false-signals).

    Returns 0-100 score per symbol:
        ADX ≥ 25  →  80   (strong trend — full pass)
        ADX 20-24 →  50   (borderline)
        ADX < 20  →  20   (choppy — heavy penalty)
        no data   →  50   (neutral — don't penalise pairs we couldn't fetch)

    Called on the post-composite top-N candidates only (50 max) to keep
    weight usage bounded. 50 calls × weight 2 = 100/8h is trivial.

    Source: TradingView JOAT MTF Strength Scanner uses ADX as primary
    trend-strength gate. Quantpedia / numerous trend-following studies
    confirm ADX > 25 is the canonical "trending" threshold.
    """
    out: dict[str, float] = {}
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - 36 * 60 * 60 * 1000   # 36 hours of 1h candles ≈ 36 bars
    for sym in symbols:
        try:
            kl = exchange_client.get_historical_klines(sym, "1h", start_ms, end_ms)
            if not kl or len(kl) < 30:
                out[sym] = 50.0
                continue
            highs  = np.array([float(k[2]) for k in kl])
            lows   = np.array([float(k[3]) for k in kl])
            closes = np.array([float(k[4]) for k in kl])
            adx_val = _wilder_adx(highs, lows, closes, period=14)
            if adx_val >= 25.0:
                out[sym] = 80.0
            elif adx_val >= 20.0:
                out[sym] = 50.0
            elif adx_val > 0:
                out[sym] = 20.0
            else:
                out[sym] = 50.0
        except Exception as exc:
            log.debug("adx_fetch_failed", sym=sym, error=str(exc)[:120])
            out[sym] = 50.0
    return out


def _norm(values: list[float]) -> list[float]:
    """Normalise a list of floats to 0–100."""
    if not values:
        return []
    mn, mx = min(values), max(values)
    if mx == mn:
        return [50.0] * len(values)
    return [(v - mn) / (mx - mn) * 100 for v in values]


def score_volume(ticker_data: dict[str, dict]) -> dict[str, float]:
    """R-02: Volume score (0–100) per symbol."""
    vols = {sym: float(d.get("volume", 0)) for sym, d in ticker_data.items()}
    normed = _norm(list(vols.values()))
    return {sym: normed[i] for i, sym in enumerate(vols)}


def score_volatility(ticker_data: dict[str, dict]) -> dict[str, float]:
    """R-03: Volatility score — absolute 24h price change % normalised to 0–100.
    Uses change_24h from 24h ticker poll (already in Redis). High volatility = more
    trade opportunities per blueprint. Absolute value so both up and down moves count.
    """
    vols = {sym: abs(float(d.get("change_24h", 0))) for sym, d in ticker_data.items()}
    normed = _norm(list(vols.values()))
    return {sym: normed[i] for i, sym in enumerate(vols)}


def score_spread(ticker_data: dict[str, dict]) -> dict[str, float]:
    """R-04: Spread score — inverted bid/ask spread (tighter = higher)."""
    def _spread(d: dict) -> float:
        bid = float(d.get("bidPrice", 0))
        ask = float(d.get("askPrice", 0))
        mid = (bid + ask) / 2
        return (ask - bid) / mid * 100 if mid else 999

    spreads = {sym: _spread(d) for sym, d in ticker_data.items()}
    inverted = {sym: -v for sym, v in spreads.items()}
    normed = _norm(list(inverted.values()))
    return {sym: normed[i] for i, sym in enumerate(inverted)}


def score_win_rate(symbols: list[str]) -> dict[str, float]:
    """R-05: Historical win rate on this pair from trades table."""
    scores = {}
    with db_conn() as conn:
        with conn.cursor() as cur:
            for sym in symbols:
                cur.execute(
                    "SELECT COUNT(*) FILTER (WHERE net_pnl_usdt > 0) AS wins, COUNT(*) AS total "
                    "FROM trades WHERE pair = %s AND status = 'closed'",
                    (sym,),
                )
                row = cur.fetchone()
                wins, total = (row[0] or 0), (row[1] or 0)
                scores[sym] = (wins / total * 100) if total >= 5 else 50.0
    normed = _norm(list(scores.values()))
    return {sym: normed[i] for i, sym in enumerate(scores)}


def score_pnl(symbols: list[str]) -> dict[str, float]:
    """R-06: Total historical net PnL on this pair from pairs table."""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol, total_net_pnl_usdt FROM pairs WHERE symbol = ANY(%s)",
                (symbols,),
            )
            pnl_map = {row[0]: float(row[1] or 0) for row in cur.fetchall()}
    scores = {sym: pnl_map.get(sym, 0) for sym in symbols}
    normed = _norm(list(scores.values()))
    return {sym: normed[i] for i, sym in enumerate(scores)}


def score_candle_setup(symbols: list[str]) -> dict[str, float]:
    """F50a — R-06b: CandleNet 15m+1h setup score (0-100).

    Reads {pair}:15m:candle_forecast + {pair}:1h:candle_forecast from Redis
    (published by F48 inference loop). Combines:
      - dir_conviction:  max(|dir1-0.5|, |dir3-0.5|) × 2 → 0-100
                         (distance from 0.5 chance = directional confidence)
      - low_exhaustion:  (1 - clip(|exhaustion_score| / 3, 0, 1)) × 100
                         (lower exhaustion = cleaner setup)
      - tf_alignment:    100 if 15m and 1h direction agree, 50 if mixed, 0 if conflict

    Score = 0.5 * dir_conviction + 0.3 * low_exhaustion + 0.2 * tf_alignment.

    Cold-start (forecast missing): returns 50 (neutral). With weight 0.20 in
    the composite, a neutral score means candle does not bias the ranking
    until F48 forecasts start flowing — but it also doesn't penalize cold
    pairs. The F10 weight learner will discover the true value once a few
    weeks of (selection, realised PnL) data accumulates.

    Falls back to {sym: 50.0} if Redis is down so the scan never fails on
    candle-side issues.
    """
    try:
        r = redis_client.get()
    except Exception:
        return {sym: 50.0 for sym in symbols}

    out: dict[str, float] = {}
    for sym in symbols:
        try:
            fc_15m_raw = r.get(f"{sym}:15m:candle_forecast")
            fc_1h_raw  = r.get(f"{sym}:1h:candle_forecast")
            if fc_15m_raw is None and fc_1h_raw is None:
                out[sym] = 50.0
                continue

            def _parse(raw):
                if raw is None:
                    return None
                try:
                    return json.loads(raw)
                except Exception:
                    return None

            fc_15m = _parse(fc_15m_raw)
            fc_1h  = _parse(fc_1h_raw)
            primary = fc_15m or fc_1h
            if not primary:
                out[sym] = 50.0
                continue

            # Directional conviction: distance from 0.5 chance.
            dir1 = abs(float(primary.get("dir1", 0.5)) - 0.5)
            dir3 = abs(float(primary.get("dir3", 0.5)) - 0.5)
            dir_conviction = min(100.0, max(dir1, dir3) * 200.0)

            # Exhaustion penalty (only the 15m exhaustion if present).
            exh_raw = r.get(f"{sym}:15m:exhaustion_score")
            try:
                exh_obj = json.loads(exh_raw) if exh_raw else None
                exh_score = (float(exh_obj.get("exhaustion_score", 0.0))
                             if isinstance(exh_obj, dict) else 0.0)
            except Exception:
                exh_score = 0.0
            low_exhaustion = max(0.0, 1.0 - min(1.0, abs(exh_score) / 3.0)) * 100.0

            # TF alignment between 15m and 1h direction calls.
            if fc_15m and fc_1h:
                d_15m_long = float(fc_15m.get("dir3", 0.5)) >= 0.5
                d_1h_long  = float(fc_1h.get("dir3", 0.5))  >= 0.5
                tf_alignment = 100.0 if d_15m_long == d_1h_long else 0.0
            else:
                tf_alignment = 50.0   # only one TF available — neutral

            out[sym] = round(
                0.5 * dir_conviction + 0.3 * low_exhaustion + 0.2 * tf_alignment, 2)
        except Exception:
            out[sym] = 50.0

    # Already in 0-100 — no need to normalise across pairs (this is a
    # per-pair absolute score, unlike volume/volatility which are relative).
    return out


# OI tiers from research (Starkiller Capital / CoinGlass practitioner consensus):
# <$20M = below institutional floor (micro-cap liquidity trap territory)
# $20M-$100M = normal mid-cap
# $100M-$500M = good (trend confirmation when rising alongside price)
# >$500M = excellent (top-tier liquid; BTC/ETH class)
_OI_TIERS = [(500_000_000, 100), (100_000_000, 80), (20_000_000, 50), (0, 20)]


def score_open_interest(
    symbols: list[str],
    exchange_client,
    price_by_sym: dict[str, float],
) -> dict[str, float]:
    """Score symbols by Open Interest in USD.

    Fetches current OI (base-asset) for each symbol, converts to USD via
    the last price, then maps to a 0-100 tier score. Called on the post-
    pass-1 top-N candidates only (same pattern as score_adx_trend) to
    keep API weight bounded.

    Rising OI alongside price = trend confirmation; included as a composite
    criterion so trending, liquid pairs rank above thin-book movers.
    Source: Starkiller Capital cross-sectional momentum paper; CoinGlass
    practitioner thresholds ($20M floor, $100M preferred).
    """
    out: dict[str, float] = {}
    for sym in symbols:
        try:
            oi_base = exchange_client.get_open_interest(sym)
            price = price_by_sym.get(sym, 0.0)
            oi_usd = oi_base * price
            score = 20.0
            for threshold, s in _OI_TIERS:
                if oi_usd >= threshold:
                    score = float(s)
                    break
            out[sym] = score
        except Exception as exc:
            log.debug("oi_fetch_failed", sym=sym, error=str(exc)[:80])
            out[sym] = 50.0   # neutral — don't penalise when API is unavailable
    return out


def compute_composite(
    syms: list[str],
    vol_s: dict, volatility_s: dict, spread_s: dict,
    winrate_s: dict, pnl_s: dict,
    weights: dict,
    candle_s: dict | None = None,
    adx_s: dict | None = None,
    oi_s: dict | None = None,
    rsi_s: dict | None = None,
    ema_s: dict | None = None,
) -> dict[str, float]:
    """R-07: Composite score using Brain-learned weights.

    Brain-learned weights (from F10 criteria_weights learner) use the key
    `winrate`; config.yaml defaults use `win_rate`. Accept either so the
    scan succeeds in both cases — previously this raised KeyError silently
    inside scanner_loop and the whole scan was skipped.

    F50a (cont. 50): `candle_s` + `weights["candle_setup"]` are optional.
    F51c (cont. 51): `adx_s` + `weights["adx_trend"]` are optional.
    F52  (cont. 52+): `oi_s` + `weights["open_interest"]` are optional.
    If absent, the term contributes 0 — preserving backward compatibility
    with any caller that still passes the 5/6/7-criteria form.
    """
    w_winrate = weights.get("winrate", weights.get("win_rate", 0.0))
    w_candle  = weights.get("candle_setup", 0.0)
    w_adx     = weights.get("adx_trend", 0.0)
    w_oi      = weights.get("open_interest", 0.0)
    # cont. 68: RSI momentum band + 50-EMA distance (optional, like adx/oi).
    # Absent weight ⇒ term contributes 0 ⇒ backward-compatible.
    w_rsi     = weights.get("rsi", 0.0)
    w_ema     = weights.get("ema_distance", 0.0)
    scores = {}
    for sym in syms:
        candle_term = (candle_s.get(sym, 0) if candle_s else 0) * w_candle
        adx_term    = (adx_s.get(sym, 0)    if adx_s    else 0) * w_adx
        oi_term     = (oi_s.get(sym, 0)     if oi_s     else 0) * w_oi
        rsi_term    = (rsi_s.get(sym, 0)    if rsi_s    else 0) * w_rsi
        ema_term    = (ema_s.get(sym, 0)    if ema_s    else 0) * w_ema
        scores[sym] = round(
            vol_s.get(sym, 0) * weights["volume"] +
            volatility_s.get(sym, 0) * weights["volatility"] +
            spread_s.get(sym, 0) * weights["spread"] +
            winrate_s.get(sym, 0) * w_winrate +
            pnl_s.get(sym, 0) * weights["pnl"] +
            candle_term +
            adx_term +
            oi_term +
            rsi_term +
            ema_term,
            2,
        )
    return scores


_DEFAULT_MIN_QUOTE_VOLUME_USD = 150_000_000.0   # cont. 51: raised from $50M.
# Research (arXiv:2602.11708, MDPI 2227-9091/13/9/180): mid-cap and below
# crypto perpetuals show elevated spread cost and regime instability that
# erodes systematic edge. $150M floor keeps the universe at quality mid-caps
# (NEAR/ARB/SUI/AAVE class) while excluding micro-cap meme/sector tokens
# (HMSTR/ESPORTS/AGT class). Runtime override still available via Redis key
# `scanner:min_quote_volume_usd`.


def update_active_pairs(composite: dict[str, float], max_pairs: int,
                        sub_scores_by_sym: dict | None = None,
                        quote_volumes: dict[str, float] | None = None,
                        listing_ages_arg: dict[str, int] | None = None,
                        market_caps_arg: dict[str, float] | None = None) -> list[str]:
    """R-08: Select top-N pairs; write to Redis and pairs table.

    Cont. 18: also stamps per-pair sub-score audit rows into pair_selections
    when sub_scores_by_sym is supplied, so ml/criteria_weights.py can later
    correlate sub-scores with realized PnL and learn the weights.

    Cont. 42: when `scanner:anchors_only=1` AND `scanner:movers_topN > 0`,
    the universe = anchors ∪ top-N non-anchor pairs ranked by full
    5-criteria composite, filtered by quote-volume floor
    (`scanner:min_quote_volume_usd`, default $50M). This re-opens the
    discovery channel in a controlled way after the cont. 24 anchors-only
    lockdown."""
    sorted_pairs = sorted(composite, key=lambda s: composite[s], reverse=True)
    discovery = sorted_pairs[:max_pairs]

    r = redis_client.get()

    # cont. 51 — dynamic anchor list. Was previously hardcoded
    # ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT"] as a one-time seed;
    # now derived from live CoinGecko market-cap data, intersected with the
    # live Binance USDT-M perp set. The Redis SCANNER_ANCHOR_PAIRS set is
    # treated as a refreshable cache, not a persistent manual list — every
    # scan rebuilds it from market_data.get_anchor_universe(). Operator can
    # still pin specific symbols by manually adding them to the set; they
    # will be re-added after each refresh as long as they survive the
    # top-N-by-mcap cut.
    anchor_target_count = int(r.get("scanner:anchors_target_count") or 30)
    binance_universe = set(composite.keys())
    try:
        from scanner.market_data import get_anchor_universe
        dynamic_anchors = get_anchor_universe(binance_universe,
                                              top_n=anchor_target_count)
    except Exception as exc:
        log.warning("anchor_universe_fetch_failed", error=str(exc)[:120])
        dynamic_anchors = []

    if dynamic_anchors:
        anchors = dynamic_anchors
        # Refresh the Redis cache so other consumers (dashboard, governance)
        # see the current anchor list.
        try:
            r.delete(redis_keys.SCANNER_ANCHOR_PAIRS)
            r.sadd(redis_keys.SCANNER_ANCHOR_PAIRS, *anchors)
        except Exception:
            pass
        log.info("scanner_anchors_dynamic_refreshed",
                 count=len(anchors), target=anchor_target_count,
                 sample=anchors[:5])
    else:
        # CoinGecko down AND no on-disk cache → fall back to whatever's in
        # the Redis set (may be empty on first boot). Never reintroduce a
        # hardcoded seed list (Rule: no hardcoded symbols).
        anchors = list(r.smembers(redis_keys.SCANNER_ANCHOR_PAIRS) or [])
        log.warning("scanner_anchors_fallback_to_cache", cached=len(anchors))

    anchors_only   = r.get("scanner:anchors_only") == "1"
    movers_topN    = int(r.get("scanner:movers_topN") or 30)
    min_quote_vol  = float(r.get("scanner:min_quote_volume_usd")
                           or _DEFAULT_MIN_QUOTE_VOLUME_USD)
    anchor_set = set(anchors)

    # cont. 51 — Filter cascade for movers (anchors bypass these — they're
    # inherently liquid mid-caps by mcap-rank, and excluding them would
    # eject BTC during a spread spike, which is wrong). Cascade:
    #   1. exclude_pairs (manual blacklist Redis set)
    #   2. AgeFilter: listing_age_days >= min_listing_days (default 30)
    #   3. SpreadFilter: spread_bps <= max_spread_bps (default 15)
    #   4. MarketCapFilter: market_cap_usd >= min_marketcap_usd (default $250M)
    #   5. Quote-volume floor (existing, default $150M)
    # Source: arXiv:2503.08692 pump-and-dump research; freqtrade
    # SpreadFilter/AgeFilter/VolatilityFilter defaults.
    min_listing_days    = int(r.get("scanner:min_listing_days") or 42)
    max_spread_bps      = float(r.get("scanner:max_spread_bps") or 15.0)
    min_marketcap_usd   = float(r.get("scanner:min_marketcap_usd") or 250_000_000.0)
    # Funding rate extremes: |rate| > 0.05%/8h signals over-leveraged crowded
    # position likely to squeeze. Research consensus: exclude from momentum entries.
    # Runtime override via `scanner:max_abs_funding_rate` (decimal, default 0.0005).
    max_abs_funding     = float(r.get("scanner:max_abs_funding_rate") or 0.0005)
    # Dead-pair stability: pairs with near-zero 24h movement are stable coins or
    # dead alts. Hard floor keeps them out of the active universe.
    # Runtime override via `scanner:min_abs_change_24h_pct`. cont. 66: default
    # raised 0.5%→1.5% — a pair moving <1.5% over a FULL DAY is intraday-flat
    # and was a prime source of the no-movement/dead trades. Reversible: set the
    # Redis key back to 0.5 to restore the old floor.
    min_change_pct      = float(r.get("scanner:min_abs_change_24h_pct") or 1.5)
    exclude_pairs       = set(r.smembers("scanner:exclude_pairs") or [])

    # Auto-blacklist harmful symbol patterns (leveraged tokens, stablecoins,
    # depegged assets). Checked before any other filter to short-circuit fast.
    # Source: Freqtrade community blacklist standard + arXiv:2503.08692 pump-and-dump.
    _BLACKLIST_SUFFIXES  = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S")
    _BLACKLIST_EXACT     = {"LUNCUSDT", "USTUSDT", "USDCUSDT", "BUSDUSDT",
                            "USDPUSDT", "TUSDUSDT", "FDUSDUSDT"}

    # Listing ages and market caps are fetched once in run_scan (where
    # exchange_client is available) and passed in as args; both are also
    # cached in Redis (7d / 24h) by market_data so subsequent calls are
    # cache hits.
    listing_ages = listing_ages_arg or {}
    market_caps  = market_caps_arg or {}

    def _passes_mover_filters(sym: str) -> tuple[bool, str]:
        """Return (passes, reason_if_rejected)."""
        # 0. Auto-blacklist: leveraged tokens, stablecoins, depegged assets.
        if sym in _BLACKLIST_EXACT:
            return False, "blacklist_exact"
        base = sym.removesuffix("USDT")
        if any(base.endswith(sfx) for sfx in _BLACKLIST_SUFFIXES):
            return False, f"blacklist_pattern_{base[-4:]}"

        # 1. Manual operator blacklist.
        if sym in exclude_pairs:
            return False, "excluded"

        # 2. Age filter — new listings are pump-and-dump targets.
        if listing_ages:
            age = listing_ages.get(sym)
            if age is not None and age < min_listing_days:
                return False, f"age_{age}d_lt_{min_listing_days}d"

        # 3. Spread filter — wide spread bleeds PnL on every round-trip.
        try:
            bid = float(r.get(redis_keys.BID_PRICE.replace("{pair}", sym)) or 0)
            ask = float(r.get(redis_keys.ASK_PRICE.replace("{pair}", sym)) or 0)
            if bid > 0 and ask > 0:
                mid = (bid + ask) / 2.0
                spread_bps = (ask - bid) / mid * 10_000.0
                if spread_bps > max_spread_bps:
                    return False, f"spread_{spread_bps:.1f}bps_gt_{max_spread_bps:.1f}"
        except Exception:
            pass

        # 4. Market cap filter.
        if market_caps:
            mc = market_caps.get(sym, 0.0)
            if mc > 0 and mc < min_marketcap_usd:
                return False, f"mcap_{mc:.0f}_lt_{min_marketcap_usd:.0f}"

        # 5. Quote-volume floor.
        if quote_volumes:
            qv = quote_volumes.get(sym, 0.0)
            if qv < min_quote_vol:
                return False, f"vol_{qv:.0f}_lt_{min_quote_vol:.0f}"

        # 6. Funding rate extremes — over-leveraged crowded position signals
        #    imminent squeeze. Source: Sharpe Terminal / ArbitrageScanner.io
        #    practitioner consensus: |funding| > 0.05%/8h is a risk signal.
        try:
            fr = float(r.get(redis_keys.FUNDING_RATE.replace("{pair}", sym)) or 0)
            if abs(fr) > max_abs_funding:
                return False, f"funding_{fr:.5f}_gt_{max_abs_funding:.5f}"
        except Exception:
            pass

        # 7. Range stability — dead pairs (|24h change| < 0.5%) are stablecoins
        #    or zero-movement alts. Kills USDC-tracking assets not caught by name.
        try:
            chg = abs(float(r.get(
                redis_keys.TICKER_CHANGE_24H.replace("{pair}", sym)) or 0))
            if chg < min_change_pct:
                return False, f"dead_pair_change_{chg:.2f}pct"
        except Exception:
            pass

        return True, ""

    rejection_counts: dict[str, int] = {}

    # cont. 62c — Categorised 10×10 universe (owner mandate 2026-05-29).
    # When `scanner:categorised_mode_disabled` is NOT set, build the active
    # set as: top-10-per-category × 10 categories (≈100 bucketed core) +
    # top-(max_pairs-100) composite tail (200-cap by default), all subject
    # to the existing mover-filter cascade.
    #
    # Falls open to the legacy anchors_only / discovery paths when the kill
    # switch is set, so reverting is one Redis SET away.
    categorised_disabled = (r.get("scanner:categorised_mode_disabled") == "1")
    if not categorised_disabled:
        try:
            from scanner.categories import (
                build_buckets, select_top_per_bucket)
            buckets = build_buckets(binance_universe)
            # Apply the mover filter cascade INSIDE each bucket so spread/
            # liquidity / age gates still hold. Counter records why pairs
            # were stripped, per silent-rejection rule.
            filtered_buckets: dict[str, list[tuple[str, float]]] = {}
            for bname, members in buckets.items():
                kept: list[tuple[str, float]] = []
                for sym, mcap in members:
                    if sym not in composite:
                        continue   # Binance pair but not in current scan window
                    ok, reason = _passes_mover_filters(sym)
                    if not ok:
                        rejection_counts[reason.split("_", 1)[0] if reason else "unknown"] \
                            = rejection_counts.get(
                                reason.split("_", 1)[0] if reason else "unknown", 0) + 1
                        continue
                    kept.append((sym, mcap))
                filtered_buckets[bname] = kept

            bucketed_core, pair_to_bucket = select_top_per_bucket(
                filtered_buckets, composite,
                per_bucket=int(r.get("scanner:per_bucket_target") or 10),
                target_total=int(r.get("scanner:bucketed_core_target") or 100),
                residual_bucket="residual",
            )

            # Persist bucket assignment per pair for downstream consumers
            # (risk/pair_classes.classify reads these). Replace any prior
            # snapshot atomically.
            try:
                pipe = r.pipeline()
                for key in r.scan_iter(match="scanner:pair_bucket:*"):
                    pipe.delete(key)
                for sym, bname in pair_to_bucket.items():
                    pipe.setex(f"scanner:pair_bucket:{sym}", 6 * 3600, bname)
                pipe.execute()
            except Exception as exc:
                log.warning("scanner_pair_bucket_persist_failed",
                            error=str(exc)[:120])

            # Pad with non-bucketed top-composite up to max_pairs so the
            # brain still has a discovery tail beyond the 100-bucketed core
            # (D-4: keep 200 cap; bucketed 100 is the priority core).
            tail_target = max(0, max_pairs - len(bucketed_core))
            if tail_target > 0:
                already_in = set(bucketed_core)
                for sym in sorted_pairs:
                    if tail_target <= 0:
                        break
                    if sym in already_in:
                        continue
                    ok, _ = _passes_mover_filters(sym)
                    if not ok:
                        continue
                    bucketed_core.append(sym)
                    already_in.add(sym)
                    tail_target -= 1

            active = list(dict.fromkeys(bucketed_core))
            log.info("scanner_categorised_universe",
                     bucketed_core=len(pair_to_bucket),
                     padded_total=len(active),
                     buckets={b: sum(1 for s, x in pair_to_bucket.items() if x == b)
                              for b in filtered_buckets.keys()},
                     rejections=rejection_counts)
            try:
                r.incr("scanner:categorised_mode_applied_count")
            except Exception:
                pass

            # Write the same downstream artefacts as the legacy paths.
            active_set = set(active)
            r.delete(redis_keys.ACTIVE_PAIRS)
            if active:
                r.sadd(redis_keys.ACTIVE_PAIRS, *active)
            # Skip the legacy anchors/movers branches — they'd overwrite
            # the carefully bucketed selection. Jump to per-symbol audit.
            _categorised_done = True
        except Exception as exc:
            log.warning("scanner_categorised_mode_failed_fallback",
                        error=str(exc)[:200])
            _categorised_done = False
    else:
        _categorised_done = False

    # cont. 24 — anchors-only universe. When the operator sets
    # `scanner:anchors_only=1` the discovery channel is dropped and the
    # trading universe IS exactly the anchor set. Used to concentrate the
    # bot on top majors and stop the alt-spray pattern. Falls open
    # (full discovery + mover filter cascade) when flag unset or set to 0.
    if _categorised_done:
        pass   # already populated; skip legacy paths
    elif anchors_only and movers_topN > 0:
        candidates_iter = (s for s in sorted_pairs if s not in anchor_set)
        movers: list[str] = []
        for sym in candidates_iter:
            ok, reason = _passes_mover_filters(sym)
            if not ok:
                # Bucket rejections by first underscore-prefix for compact logging.
                bucket = reason.split("_", 1)[0] if reason else "unknown"
                rejection_counts[bucket] = rejection_counts.get(bucket, 0) + 1
                continue
            movers.append(sym)
            if len(movers) >= movers_topN:
                break
        active = list(dict.fromkeys(anchors + movers))
        log.info("scanner_anchors_plus_movers",
                 anchors=len(anchors), movers=len(movers),
                 movers_picked=movers,
                 rejections=rejection_counts,
                 min_quote_vol_usd=min_quote_vol,
                 max_spread_bps=max_spread_bps,
                 min_listing_days=min_listing_days,
                 min_marketcap_usd=min_marketcap_usd,
                 exclude_count=len(exclude_pairs))
    elif anchors_only:
        active = list(dict.fromkeys(anchors))
        log.info("scanner_anchors_only_mode", count=len(active))
    else:
        # Discovery mode (no anchors_only flag): also apply mover filters
        # to the full discovery list — same rationale.
        filtered_discovery = []
        for sym in discovery:
            if sym in anchor_set:
                filtered_discovery.append(sym)   # anchors bypass
                continue
            ok, _ = _passes_mover_filters(sym)
            if ok:
                filtered_discovery.append(sym)
        active = list(dict.fromkeys(anchors + filtered_discovery))
    active_set = set(active)

    r.delete(redis_keys.ACTIVE_PAIRS)
    if active:
        r.sadd(redis_keys.ACTIVE_PAIRS, *active)

    # cont. 61 audit fix — Persist per-pair 24h quote volume so the
    # signals/engine.py liquidity gate can tier-filter at trade time.
    # Without this the gate has no input and falls through silently.
    if quote_volumes:
        try:
            pipe = r.pipeline()
            for sym, qv in quote_volumes.items():
                pipe.hset(f"scanner:meta:{sym}", "quote_volume_24h", float(qv))
                pipe.expire(f"scanner:meta:{sym}", 60 * 60 * 12)   # 12h TTL
            pipe.execute()
        except Exception as exc:
            log.warning("scanner_meta_persist_failed", error=str(exc)[:200])

    # Stamp per-pair sub-scores for F10 Brain-learned weight update loop.
    if sub_scores_by_sym:
        try:
            from ml.criteria_weights import record_pair_selection
            for sym in active:
                ss = sub_scores_by_sym.get(sym)
                if ss is not None:
                    record_pair_selection(
                        sym, ss, composite[sym], selected=(sym in active_set))
        except Exception as exc:
            log.warning("pair_selection_stamping_failed", error=str(exc)[:120])

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE pairs SET is_active = FALSE")
            for sym in active:
                # cont. 51 fix: also persist sub-scores to `pairs` so the
                # dashboard's `/pairs/active` endpoint can render the
                # Vol/Volat/Spread/WinRate columns (previously all "—").
                # sub_scores_by_sym is built by run_scan() with the six
                # criteria scores; fall back to None when absent.
                _ss = (sub_scores_by_sym or {}).get(sym) or {}
                cur.execute("""
                    INSERT INTO pairs (symbol, is_active, composite_score,
                                       volume_score, volatility_score, spread_score,
                                       win_rate_score, pnl_score)
                    VALUES (%s, TRUE, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (symbol) DO UPDATE
                    SET is_active        = TRUE,
                        composite_score  = EXCLUDED.composite_score,
                        volume_score     = EXCLUDED.volume_score,
                        volatility_score = EXCLUDED.volatility_score,
                        spread_score     = EXCLUDED.spread_score,
                        win_rate_score   = EXCLUDED.win_rate_score,
                        pnl_score        = EXCLUDED.pnl_score,
                        last_scanned_at  = NOW(),
                        updated_at       = NOW()
                """, (
                    sym, composite[sym],
                    _ss.get("volume"),
                    _ss.get("volatility"),
                    _ss.get("spread"),
                    _ss.get("winrate"),
                    _ss.get("pnl"),
                ))

    log.info("active_pairs_updated", count=len(active))
    return active


async def run_scan(exchange_client) -> list[str]:
    """R-01: Full market scan — called on startup and every 8 hours."""
    r = redis_client.get()
    brain_stage = int(r.get(redis_keys.BRAIN_STAGE) or 1)

    all_symbols = exchange_client.get_all_usdt_futures_symbols()

    ticker_data: dict[str, dict] = {}
    for sym in all_symbols:
        ticker_data[sym] = {
            "volume":     float(r.get(redis_keys.TICKER_VOLUME_24H.replace("{pair}", sym)) or 0),
            "change_24h": float(r.get(redis_keys.TICKER_CHANGE_24H.replace("{pair}", sym)) or 0),
            "close":      float(r.get(redis_keys.LAST_PRICE.replace("{pair}", sym)) or 0),
            "bidPrice":   float(r.get(redis_keys.BID_PRICE.replace("{pair}", sym)) or 0),
            "askPrice":   float(r.get(redis_keys.ASK_PRICE.replace("{pair}", sym)) or 0),
        }

    syms = list(ticker_data.keys())
    weights_raw = r.get(redis_keys.BRAIN_FEATURE_WEIGHTS)
    weights = json.loads(weights_raw) if weights_raw else {}
    # Fall back to config weights if Brain hasn't learned custom weights yet
    if not weights.get("volume"):
        weights = config.scanner.criteria_weights

    # cont. 66 — volatility-weight boost. The "no-movement" trade problem (36%
    # of trades moved <0.2%) is partly a UNIVERSE problem: the composite ranked
    # flat-but-liquid pairs highly. Boost the volatility term so the active
    # universe skews toward genuine movers — WITHOUT adding micro-cap symbols
    # (which widen spreads + worsen adverse moves, the -$12k >2% loss bucket).
    # Redis-tunable `scanner:volatility_weight_boost` (default 1.6); set 1.0 off.
    try:
        _vol_boost = float(r.get("scanner:volatility_weight_boost") or 1.6)
    except Exception:
        _vol_boost = 1.6
    if _vol_boost != 1.0 and weights.get("volatility"):
        weights = dict(weights)
        weights["volatility"] = round(float(weights["volatility"]) * _vol_boost, 4)
        log.info("scanner_volatility_weight_boosted",
                 boost=_vol_boost, volatility_weight=weights["volatility"])

    vol_s = score_volume(ticker_data)
    volatility_s = score_volatility(ticker_data)
    spread_s = score_spread(ticker_data)
    winrate_s = score_win_rate(syms)
    pnl_s = score_pnl(syms)
    candle_s = score_candle_setup(syms)   # F50a — CandleNet 15m+1h setup

    # cont. 66 (owner request 2026-06-01): use the FULL max_active_pairs (200) at
    # ALL stages. Previously stages 3-4 halved it to 100 ("quality over quantity"),
    # but the owner wants the full 200-pair universe active. Reversible without a
    # code change: set Redis `scanner:stage_halving` = "1" to restore the old
    # stage-3+ halving.
    if r.get("scanner:stage_halving") == "1" and brain_stage > 2:
        max_pairs = max(60, config.trading.max_active_pairs // 2)
    else:
        # Redis override: `scanner:max_active_pairs` lets operator raise beyond
        # the image-baked config.yaml ceiling without a rebuild.
        _r_max = r.get("scanner:max_active_pairs")
        max_pairs = int(_r_max) if _r_max else config.trading.max_active_pairs

    # F51c (cont. 51) — two-pass scoring with ADX trend filter.
    # Pass 1: compute composite WITHOUT ADX → identifies the top liquidity/
    #          volatility/win-rate candidates without burning API weight.
    # Pass 2: fetch 1h ADX(14) for top 50 candidates → adds trend-strength
    #          dimension and re-ranks. Choppy pairs (ADX < 20) get penalised.
    # This keeps the ADX API cost bounded (50 calls × weight 2 = 100/8h)
    # while still applying the trend filter where it matters.
    _pass1 = compute_composite(syms, vol_s, volatility_s, spread_s,
                               winrate_s, pnl_s, weights, candle_s=candle_s)
    _top_for_adx = sorted(_pass1, key=lambda s: _pass1[s], reverse=True)[:50]
    # cont. 68: one bounded fetch computes ADX + RSI-momentum + 50-EMA-distance.
    adx_s, rsi_s, ema_s = score_trend_momentum(_top_for_adx, exchange_client)
    log.info("scanner_adx_pass2", n_pairs=len(adx_s),
             n_trending=sum(1 for v in adx_s.values() if v >= 70),
             n_choppy=sum(1 for v in adx_s.values() if v <= 30),
             n_rsi_momentum=sum(1 for v in rsi_s.values() if v >= 100),
             n_rsi_flat=sum(1 for v in rsi_s.values() if v <= 25),
             n_ema_trending=sum(1 for v in ema_s.values() if v >= 100),
             n_ema_flat=sum(1 for v in ema_s.values() if v <= 25))

    # Pass 2b: Open Interest score for top-N candidates.
    # Same bounding strategy as ADX — fetch only for the post-pass-1 top-60
    # so API weight stays bounded (60 calls × weight 1 = 60/8h).
    # Source: Starkiller Capital momentum paper; CoinGlass OI thresholds.
    _pass2_composite = compute_composite(syms, vol_s, volatility_s, spread_s,
                                         winrate_s, pnl_s, weights,
                                         candle_s=candle_s, adx_s=adx_s,
                                         rsi_s=rsi_s, ema_s=ema_s)
    _top_for_oi = sorted(_pass2_composite, key=lambda s: _pass2_composite[s], reverse=True)[:60]
    price_by_sym = {sym: float(ticker_data[sym].get("close", 0)) for sym in syms}
    oi_s = score_open_interest(_top_for_oi, exchange_client, price_by_sym)
    log.info("scanner_oi_pass2b",
             n_pairs=len(oi_s),
             n_high_oi=sum(1 for v in oi_s.values() if v >= 80),
             n_low_oi=sum(1 for v in oi_s.values() if v <= 25))

    composite = compute_composite(syms, vol_s, volatility_s, spread_s,
                                  winrate_s, pnl_s, weights,
                                  candle_s=candle_s, adx_s=adx_s, oi_s=oi_s,
                                  rsi_s=rsi_s, ema_s=ema_s)

    # Build per-symbol sub-scores dict for the F10 weight learner.
    sub_scores_by_sym = {
        sym: {
            "volume":         vol_s.get(sym, 0),
            "volatility":     volatility_s.get(sym, 0),
            "spread":         spread_s.get(sym, 0),
            "winrate":        winrate_s.get(sym, 0),
            "pnl":            pnl_s.get(sym, 0),
            "candle_setup":   candle_s.get(sym, 0),
            "adx_trend":      adx_s.get(sym, 50.0),
            "open_interest":  oi_s.get(sym, 50.0),
            "rsi":            rsi_s.get(sym, 50.0),
            "ema_distance":   ema_s.get(sym, 50.0),
        }
        for sym in syms
    }
    # Cont. 42: quote-volume (USD) ≈ base-volume × close. Binance's ticker
    # `volume` field is base-asset; the scanner's $50M liquidity floor must
    # be expressed in USD to be meaningful across pairs of different prices.
    quote_volumes = {
        sym: float(d.get("volume", 0)) * float(d.get("close", 0))
        for sym, d in ticker_data.items()
    }
    # cont. 51 — fetch listing ages + market caps once per scan (both
    # cached in Redis with 7d / 24h TTL so this is normally a cache hit).
    # Required by the mover filter cascade in update_active_pairs.
    listing_ages: dict[str, int] = {}
    market_caps:  dict[str, float] = {}
    try:
        from scanner.market_data import fetch_listing_ages, fetch_top_marketcaps
        listing_ages = fetch_listing_ages(exchange_client)
        market_caps  = fetch_top_marketcaps(set(syms), top_n=250)
    except Exception as exc:
        log.warning("scanner_metadata_fetch_failed", error=str(exc)[:200])

    return update_active_pairs(composite, max_pairs, sub_scores_by_sym,
                               quote_volumes,
                               listing_ages_arg=listing_ages,
                               market_caps_arg=market_caps)


async def scanner_loop(exchange_client) -> None:
    """R-10: Rescan loop.

    cont. 62c — sleep duration is now read from Redis
    `scanner:rerank_interval_minutes` (default 20 min). Falls back to the
    legacy 8 h cadence via `config.scanner.rescan_interval_hours` only
    when the categorised mode is disabled — i.e. legacy behaviour stays
    8 h-cadenced; the new 100-pair mode cadences at the 20 min default.
    """
    r = redis_client.get()
    while True:
        try:
            await run_scan(exchange_client)
        except Exception as exc:
            log.error("scanner_error", error=str(exc))
        try:
            disabled = (r.get("scanner:categorised_mode_disabled") == "1")
            if disabled:
                sleep_s = float(config.scanner.rescan_interval_hours) * 3600.0
            else:
                rerank_min = float(r.get("scanner:rerank_interval_minutes") or 20.0)
                rerank_min = max(5.0, min(180.0, rerank_min))
                sleep_s = rerank_min * 60.0
        except Exception:
            sleep_s = 20.0 * 60.0
        await asyncio.sleep(sleep_s)


if __name__ == "__main__":
    import asyncio
    import db, redis_client, config
    from exchange.client import BinanceClient
    db.init_pool()
    redis_client.init()
    client = BinanceClient()
    asyncio.run(scanner_loop(client))
