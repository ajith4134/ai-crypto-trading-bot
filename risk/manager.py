"""
Section O: Risk & Position Manager.
O-01 to O-10: Trailing SL, DCA triggers, position sizing, turbulence circuit breaker.
"""
import asyncio
import json
import structlog
import redis_client
import redis_keys
import config
from db import db_conn
from memory.write import write_trade_update
from memory.query import get_open_trades

log = structlog.get_logger()


def _volatility_unit(r, pair: str) -> float:
    """Per-pair volatility unit as a fraction of mark — shared between initial
    SL sizing (compute_initial_sl) and trailing distance (monitor_trailing_sl).

    Uses VPIN (rolling realized vol from data/feed.py:_compute_microstructure)
    as the live volatility proxy. Amplifier ×8 converts per-tick stdev to a
    session-scale unit. Floor 0.5% so a missing/zero VPIN never collapses the
    SL formulas to zero. Cap 2.5% so noisy/spiky pairs don't get absurd
    distances. Both bounds are hand-picked (Rule 4 cont. 18 #2).

    Centralising here so initial SL and trailing distance share one definition
    of "this pair's volatility right now" — fixes cont. 22d gap #5 where
    initial SL was effectively hardcoded at 3% because compute_initial_sl
    read the dead `{pair}:atr` Redis key.
    """
    pair_vpin = float(r.get(redis_keys.VPIN.replace("{pair}", pair)) or 0)
    return max(0.005, min(0.025, pair_vpin * 8.0))


# cont. 53 — Regime-adaptive lock_frac scaling.
# Honest review (cont. 53) showed the static 0.80 floor (cont. 44 mandate)
# cut trends short: 0.80 = 20% retracement budget, while real crypto trend
# pullbacks routinely consume 30-50%. The scaling below relaxes the floor
# in trending regimes (HMM: bull/bear) so trends can reach their natural
# peak. Chop / unknown still get the original 0.80-0.92 — that's where
# the static defence earned its keep.
#
# Kill switch: set `risk:regime_scaling_disabled=1` in Redis to revert to
# the cont. 44 static-0.80 behaviour without redeploying.
_REGIME_LOCK_BANDS = {
    "bull":      (0.40, 0.55),   # let trends breathe; 40-55% lock
    "bear":      (0.40, 0.55),   # symmetric for short trends
    "turbulent": (0.65, 0.75),   # compromise — trend bursts + reversal noise
    "unknown":   (0.80, 0.92),   # original cont. 44 chop-anchored band
    "chop":      (0.80, 0.92),   # explicit chop = original
}


def _regime_scaled_lock_frac(raw: float, regime: str | None, r=None) -> float:
    """Map a chop-regime lock_frac in [0.80, 0.92] to a regime-appropriate
    band per `_REGIME_LOCK_BANDS`. Linear interpolation within the band.

    Returns `raw` unchanged when:
      - Redis flag `risk:regime_scaling_disabled` == "1" (kill switch)
      - regime maps to (0.80, 0.92) (chop / unknown — no scaling needed)
      - raw is outside [0.80, 0.92] (caller is in a different band — e.g.
        Path D 0.95 reversal lock — don't double-scale)
    """
    try:
        if r is None:
            r = redis_client.get()
        if r.get("risk:regime_scaling_disabled") == "1":
            return raw
    except Exception:
        pass
    band = _REGIME_LOCK_BANDS.get((regime or "unknown").lower())
    if band is None or band == (0.80, 0.92):
        return raw
    if raw < 0.80 or raw > 0.92:
        return raw
    frac = (raw - 0.80) / 0.12
    scaled = round(band[0] + frac * (band[1] - band[0]), 6)
    try:
        if r is not None:
            r.incr("trail:regime_scaled_count")
    except Exception:
        pass
    return scaled


def compute_initial_sl(pair: str, direction: str,
                       strategy_id: str | None = None) -> float:
    """O-01 / Blueprint 10.4: Compute initial SL distance from pair volatility.

    Blueprint says: "Every trade opens with a wide initial SL — distance
    determined by the Brain based on pair volatility and current market
    conditions, never hardcoded."

    Pre-cont.22d this read `{pair}:atr` (a Redis key nothing currently writes)
    and fell back to a single 3% floor for every pair — effectively hardcoded.
    Now uses VPIN-derived `_volatility_unit` as the volatility input.

    Legacy `{pair}:atr` is still preferred when populated (backward compat
    for any future ATR producer). When it's empty (current state for ~all
    pairs), VPIN drives the distance instead.

    Strategy router can override `initial_atr_mult` and `initial_min_pct` per
    strategy (added cont. 13).
    """
    r = redis_client.get()
    mark = float(r.get(redis_keys.MARK_PRICE.replace("{pair}", pair)) or 0)
    legacy_atr = float(r.get(f"{pair}:atr") or 0)

    # F51a (cont. 51): HMM-regime-adaptive ATR multiplier.
    # Turbulence needs a wider SL to survive noise; calm directional regimes
    # can use a tighter SL that trails more aggressively. Strategy override
    # (below) still wins. Source: LuxAlgo Dynamic Stop, QuantStrategy ATR sizing.
    _regime = str(r.get(redis_keys.CURRENT_REGIME) or "unknown").lower()
    atr_mult = {"bull": 1.5, "bear": 1.5, "turbulent": 3.5}.get(_regime, 2.5)
    min_pct  = 0.010   # 1% absolute floor — VPIN provides real-time width above this
    if strategy_id:
        try:
            from strategy.router import get_sl_overrides, record_routed_sl
            sl_ov = get_sl_overrides(strategy_id)
            if sl_ov:
                if "initial_atr_mult" in sl_ov:
                    atr_mult = max(0.5, min(10.0, float(sl_ov["initial_atr_mult"])))
                if "initial_min_pct" in sl_ov:
                    min_pct = max(0.005, min(0.20, float(sl_ov["initial_min_pct"])))
                record_routed_sl(strategy_id, "initial", atr_mult)
        except Exception as exc:
            log.debug("sl_router_initial_skipped", error=str(exc)[:120])

    # cont. 23 — DCA-room floor (only when DCA is enabled).
    # DCA disabled (dca_rounds_max=0, cont. 43): skip entirely — the 48% floor
    # this code produces when dca_trigger_2_pct=-40 means SLs never fire.
    try:
        if int(config.capital.dca_rounds_max or 0) > 0:
            dca2_depth_abs = abs(float(config.capital.dca_trigger_2_pct)) / 100.0
            if strategy_id:
                try:
                    from strategy.router import get_dca_rules
                    _dca = get_dca_rules(strategy_id)
                    if _dca and _dca.get("round_2_pct") is not None:
                        dca2_depth_abs = abs(float(_dca["round_2_pct"])) / 100.0
                except Exception:
                    pass
            dca_floor_pct = dca2_depth_abs * 1.2
            if dca_floor_pct > min_pct:
                min_pct = min(0.60, dca_floor_pct)  # 60% hard ceiling
    except Exception as exc:
        log.debug("sl_dca_room_floor_skipped", error=str(exc)[:120])

    if legacy_atr > 0:
        # Legacy ATR feed available — use it as the volatility input.
        atr_distance = max(legacy_atr * atr_mult, mark * min_pct)
    else:
        # No ATR — use VPIN-derived per-pair volatility unit (cont. 22d fix #5).
        # vol_unit ∈ [0.5%, 2.5%]; atr_mult × vol_unit ∈ [1.25%, 6.25%].
        vol_unit = _volatility_unit(r, pair)
        atr_distance = max(mark * vol_unit * atr_mult, mark * min_pct)

    # ── F48 §Idea B — Magnitude-driven SL floor ──
    # CandleNet predicts mag1 = % expected move in 1 candle. The SL should
    # never be tighter than HALF the expected move in the favourable direction,
    # otherwise normal noise stops us out before the predicted move even has a
    # chance to play out. Pulls forecasts from both 1m and 5m (averaged when
    # both available) and converts to an absolute price distance.
    try:
        cn_mag_pct = _candlenet_avg_mag_pct(r, pair)   # returns % already (e.g. 0.5 = 0.5%)
        if cn_mag_pct > 0:
            mag_distance = mark * (cn_mag_pct / 100.0) * 0.5
            atr_distance = max(atr_distance, mag_distance)
    except Exception as exc:
        log.debug("sl_candlenet_mag_skipped", error=str(exc)[:120])

    # ── cont.70f Track-4 — Chronos vol-band SL floor ──
    # The foundation forecast's q10/q90 give a 3h predictive band; don't place
    # the SL tighter than HALF that band's uncertainty, else near-term vol stops
    # us out before the move plays. Soft floor (max), Redis-toggleable. Parallel
    # to the F48 CandleNet-mag floor above.
    try:
        if r.get("vol_prior:sl_floor_enabled") != "0":
            from ml.foundation_forecast import load_forecast as _fdn_sl
            _vf = _fdn_sl(pair)
            if _vf:
                _k = float(r.get("vol_prior:sl_band_k") or 0.5)
                band_distance = mark * float(_vf.get("spread_frac", 0) or 0) * _k
                if band_distance > 0:
                    atr_distance = max(atr_distance, band_distance)
                    r.incr("vol_prior:sl_floor_applied_count")
    except Exception as exc:
        log.debug("sl_vol_prior_skipped", error=str(exc)[:120])

    # cont. 60 — DVOL-conditioned SL distance scaling.
    # Scales atr_distance by current DVOL decile vs 252d median.
    # Decile 0 → 0.7× (calm), decile 9 → 1.5× (extreme vol).
    try:
        from risk.frontier.exit_placement import apply_dvol_scaling
        atr_distance, _dvol_meta = apply_dvol_scaling(atr_distance, mark, r)
    except Exception as _dvex:
        log.debug("sl_dvol_scaling_skipped", error=str(_dvex)[:120])

    if direction == "long":
        raw_sl = round(mark - atr_distance, 8)
    else:
        raw_sl = round(mark + atr_distance, 8)

    # cont. 60 — Liquidation-heatmap dark-side SL placement.
    # If the raw SL falls inside a high-density Coinglass cluster, snap it
    # to the dark side (cluster_top + 1.5×ATR for short / cluster_bottom -
    # 1.5×ATR for long). Source: Glassnode Liquidation Heatmaps (2025).
    try:
        from risk.frontier.exit_placement import apply_liquidation_dark_side
        raw_sl, _liq_meta = apply_liquidation_dark_side(
            raw_sl, mark, direction, pair, r)
    except Exception as _liqex:
        log.debug("sl_liq_dark_side_skipped", error=str(_liqex)[:120])

    return raw_sl


def apply_capital_sl_floor(raw_sl: float, mark: float, direction: str,
                           capital_usdt: float, leverage: float,
                           r=None) -> float:
    """cont. 62 — Capital-anchored SL floor (owner mandate 2026-05-29).

    Translates "initial SL must lose at most `frac` of allocated capital" into
    a minimum price-distance, then returns the WIDER of the existing
    volatility/CandleNet/liquidation-driven `raw_sl` and the capital floor.

    Math: at notional = capital × leverage, a price move of `frac/leverage`
    of mark equals `frac × capital` in PnL terms. Independent of mark price.

    Runtime overrides (Redis):
      risk:capital_sl_frac_disabled  ("1" → revert to raw_sl)
      risk:capital_sl_frac           (default 0.50; clamp [0.20, 0.90])
    """
    if mark <= 0 or capital_usdt <= 0 or leverage <= 0:
        return raw_sl
    if r is None:
        r = redis_client.get()
    try:
        if r.get("risk:capital_sl_frac_disabled") == "1":
            return raw_sl
        # cont. 69u — initial-SL floor lowered 0.50 -> 0.12 (full-path 1m backtest:
        # -50% cap was the single worst grid choice; ~12% tail-safety cap dominates).
        frac = float(r.get("risk:capital_sl_frac") or 0.12)
    except (TypeError, ValueError):
        frac = 0.12
    frac = max(0.05, min(0.90, frac))
    required_pct_notional = frac / leverage
    capital_distance = mark * required_pct_notional
    if direction == "long":
        capital_sl = round(mark - capital_distance, 8)
        if raw_sl <= 0 or capital_sl < raw_sl:
            try:
                r.incr("trail:capital_sl_floor_applied_count")
            except Exception:
                pass
            return capital_sl
        return raw_sl
    else:
        capital_sl = round(mark + capital_distance, 8)
        if raw_sl <= 0 or capital_sl > raw_sl:
            try:
                r.incr("trail:capital_sl_floor_applied_count")
            except Exception:
                pass
            return capital_sl
        return raw_sl


def apply_capital_sl_ceiling(raw_sl: float, mark: float, direction: str,
                             capital_usdt: float, leverage: float,
                             r=None) -> float:
    """cont. 65k Guard 3 — hard UPPER bound on SL distance (mirror of the floor).
    Never place the initial SL beyond `ceiling_frac` of capital (default 0.80).
    At 20× lev that's 4% notional — beyond which a ~5% adverse move liquidates
    the position before the SL would ever fire, so a wider SL is meaningless and
    only inflates the displayed risk (the cont. 65h chaos: SL to −247% capital).
    Returns the NARROWER (closer-to-mark) of raw_sl and the ceiling.
    Override: risk:capital_sl_ceiling_frac (clamp [0.50, 0.95])."""
    if mark <= 0 or capital_usdt <= 0 or leverage <= 0:
        return raw_sl
    if r is None:
        r = redis_client.get()
    try:
        # cont. 69u — tail cap lowered 0.80 -> 0.12 (with floor=0.12 this places the
        # initial SL flat at -12% of capital: entry*(1 - sign*0.12/L)).
        ceiling_frac = float(r.get("risk:capital_sl_ceiling_frac") or 0.12)
    except (TypeError, ValueError):
        ceiling_frac = 0.12
    ceiling_frac = max(0.08, min(0.95, ceiling_frac))
    max_distance = mark * (ceiling_frac / leverage)
    if direction == "long":
        ceiling_sl = round(mark - max_distance, 8)
        if raw_sl > 0 and raw_sl < ceiling_sl:   # raw_sl too far below → pull up
            try:
                r.incr("trail:capital_sl_ceiling_applied_count")
            except Exception:
                pass
            return ceiling_sl
        return raw_sl
    else:
        ceiling_sl = round(mark + max_distance, 8)
        if raw_sl > 0 and raw_sl > ceiling_sl:   # raw_sl too far above → pull down
            try:
                r.incr("trail:capital_sl_ceiling_applied_count")
            except Exception:
                pass
            return ceiling_sl
        return raw_sl


# cont. 65k — SL/TP placement redesign (sl_tp_placement_redesign.md). CandleNet
# emits a number for every pair even on thin/new symbols; unclamped, single-candle
# mags of 8-19% flowed straight into SL+TP → TP1 15-75% capital, SL to −247%
# (past liquidation). Guard 1: any per-candle mag > 5% is a model artifact (BTC's
# biggest 1m candle in 2025 was ~3.1%) → drop that TF's contribution.
_MAG_CLIP_PCT = 5.0


def _candlenet_avg_mag_pct(r, pair: str) -> float:
    """Return the abs average of CandleNet mag1 across whichever timeframes
    have a forecast in Redis (1m, 5m, 15m). Returns 0 if none available.
    cont. 65k — mags beyond _MAG_CLIP_PCT are skipped as model artifacts."""
    import json as _j
    vals = []
    for interval in ("1m", "5m", "15m"):
        raw = r.get(f"{pair}:{interval}:candle_forecast")
        if not raw:
            continue
        try:
            fc = _j.loads(raw)
            m = float(fc.get("mag1", 0.0))
            if m != 0.0 and abs(m) <= _MAG_CLIP_PCT:
                vals.append(abs(m))
        except Exception:
            continue
    if not vals:
        return 0.0
    return sum(vals) / len(vals)


def _tp_distance_cap_notional(leverage: int) -> tuple:
    """cont. 65k Guard 2 — per-leverage ceiling on TP distance (notional %),
    sized so worst-case TP ≈ 30%+ capital. Sanity bound only — prevents the
    8-19% mag artifacts; the 1.5/3.0 ×vol_unit multipliers set the normal TP.
    Returns (tp1_cap, tp2_cap) as fractions of entry price."""
    if leverage <= 5:
        return (0.05, 0.10)
    if leverage <= 10:
        return (0.03, 0.06)
    if leverage <= 20:
        return (0.015, 0.03)
    return (0.01, 0.02)


def _apply_tp_ceiling(tp1, tp2, entry_price, sign, leverage):
    """Clamp TP1/TP2 to the per-leverage notional ceiling (Guard 2)."""
    tp1_cap, tp2_cap = _tp_distance_cap_notional(int(leverage or 20))
    if abs(tp1 - entry_price) / entry_price > tp1_cap:
        tp1 = entry_price * (1.0 + tp1_cap * sign)
    if abs(tp2 - entry_price) / entry_price > tp2_cap:
        tp2 = entry_price * (1.0 + tp2_cap * sign)
    return tp1, tp2


def compute_tp_targets(pair: str, direction: str, entry_price: float,
                       leverage: int = None) -> dict:
    """F48 §Idea B — Magnitude-driven TP1 / TP2 targets with ATR fallback.

    Primary path: reads CandleNet forecasts (1m + 5m + 15m, averaged where
    present) and sets
      TP1 = entry × (1 + mag1_avg × sign)
      TP2 = entry × (1 + mag3_avg × sign)

    Fallback path (cont. 58 — user-mandated): when no CandleNet forecast is
    available OR CandleNet's mag1 predicts a move OPPOSITE to the trade
    direction, compute static ATR-multiple TPs from `_volatility_unit`
    (same vol input the initial SL uses). The bot trusts its own
    directional bet over CandleNet's disagreement, so TPs always populate
    on every trade. `mag1_pct` / `mag3_pct` return 0 in the fallback path
    so the dashboard can render '—' for the % markers.

    Returns dict {tp1, tp2, mag1_pct, mag3_pct}. Empty dict only when
    `entry_price` is invalid.
    """
    import json as _j
    if entry_price <= 0:
        return {}

    r = redis_client.get()
    sign = 1.0 if direction == "long" else -1.0
    if leverage is None:
        try:
            leverage = int(float(r.get("bot:leverage") or 20))
        except (TypeError, ValueError):
            leverage = 20

    # cont. 65k-5 — CAPITAL-BASED TP (owner mandate 2026-05-31): TP1/TP2 are % of
    # the trade's CAPITAL (margin, before leverage), not a price-move %. TP1 =
    # +15% capital, TP2 = +30% capital. Price move = capital% / leverage (since
    # PnL% of capital = price_move% × leverage). Redis-tunable. This is the
    # authoritative TP when risk:capital_ladder_enabled != "0" (default on).
    if (r.get("risk:capital_ladder_enabled") or "1") != "0" and leverage > 0:
        try:
            tp1_cap = float(r.get("risk:tp1_capital_pct") or 0.15)
            tp2_cap = float(r.get("risk:tp2_capital_pct") or 0.30)
        except (TypeError, ValueError):
            tp1_cap, tp2_cap = 0.15, 0.30
        tp1 = entry_price * (1.0 + sign * (tp1_cap / leverage))
        tp2 = entry_price * (1.0 + sign * (tp2_cap / leverage))
        return {
            "tp1": round(float(tp1), 8),
            "tp2": round(float(tp2), 8),
            "tp":  round(float(tp1), 8),
            # cont. 65k-6 — mag*_pct must be the PRICE-MOVE % (= capital%/leverage),
            # NOT capital %. The dashboard computes $ = capital × leverage ×
            # mag_pct/100, so it treats mag_pct as a price move. Storing capital%
            # (15/30) here made the displayed $ = 15%×notional = 5× too big
            # (exceeded capital). Price-move% (3%/6% at 5×) → correct $ (15%/30%
            # of capital). The TP PRICES were always correct.
            "mag1_pct": round(tp1_cap / leverage * 100.0, 2),
            "mag3_pct": round(tp2_cap / leverage * 100.0, 2),
        }

    # cont. 65k-4 — TP placement was WRONG: the CandleNet mag-driven primary path
    # produced erratic, often microscopic TPs (e.g. COS tp1=0.069%, AT tp1=0.17%)
    # because the balanced models predict tiny magnitudes — so TP1 hit on noise and
    # the SL checkpoint ratcheted to a microscopic profit, then stopped out. Owner
    # wants the CONSISTENT vol_unit × 1.5/3.0 formula. `risk:tp_use_vol_only`
    # (default "1") skips the mag path entirely → mag1_vals stays empty → the
    # bounded vol_unit fallback below always runs (TP1≈1.5×vol, TP2≈3.0×vol,
    # vol_unit∈[0.5%,2.5%]). Set "0" to restore CandleNet-mag-driven TPs.
    _tp_vol_only = (r.get("risk:tp_use_vol_only") or "1") == "1"

    mag1_vals, mag3_vals = [], []
    for interval in (() if _tp_vol_only else ("1m", "5m", "15m")):
        raw = r.get(f"{pair}:{interval}:candle_forecast")
        if not raw:
            continue
        try:
            fc = _j.loads(raw)
        except Exception:
            continue
        m1 = float(fc.get("mag1", 0.0))
        m3 = float(fc.get("mag3", 0.0))
        # cont. 65k Guard 1 — drop single-candle mags > _MAG_CLIP_PCT (model
        # artifacts on thin pairs). When all TFs clip, mag1_vals stays empty →
        # use_fallback → bounded vol_unit TPs.
        if m1 != 0.0 and abs(m1) <= _MAG_CLIP_PCT:
            mag1_vals.append(m1)
        if m3 != 0.0 and abs(m3) <= _MAG_CLIP_PCT:
            mag3_vals.append(m3)

    use_fallback = False
    if not mag1_vals:
        use_fallback = True
    else:
        mag1_avg = sum(mag1_vals) / len(mag1_vals)         # % units
        mag3_avg = (sum(mag3_vals) / len(mag3_vals)
                    if mag3_vals else mag1_avg * 2.0)
        # CandleNet predicts opposite direction → trust the trade's own
        # directional bet and switch to static ATR-multiple TPs.
        if (sign > 0 and mag1_avg < 0) or (sign < 0 and mag1_avg > 0):
            use_fallback = True

    if use_fallback:
        # ATR-multiple fallback: TP1 = 1.5 × vol_unit, TP2 = 3.0 × vol_unit.
        # vol_unit is in [0.5%, 2.5%] of mark — same formula compute_initial_sl
        # uses. TP1 is tighter than the typical SL distance (~2-2.5 × vol_unit)
        # so a favourable move hits TP1 before round-tripping to SL.
        vol_unit = _volatility_unit(r, pair)
        # TP1 = 1.5 × vol_unit, TP2 = 3.0 × vol_unit (owner mandate cont. 65k-2:
        # keep the original 1.5/3.0 multipliers). vol_unit ∈ [0.5%, 2.5%].
        tp1_pct = vol_unit * 1.5 * 100.0   # % units
        tp2_pct = vol_unit * 3.0 * 100.0
        tp1 = entry_price * (1.0 + (tp1_pct / 100.0) * sign)
        tp2 = entry_price * (1.0 + (tp2_pct / 100.0) * sign)
        tp1, tp2 = _apply_tp_ceiling(tp1, tp2, entry_price, sign, leverage)
        return {
            "tp1":       round(float(tp1), 8),
            "tp2":       round(float(tp2), 8),
            # Phase A write-through (cont. 55): single `tp` mirrors tp1.
            # Phase C will replace this with predictor output.
            "tp":        round(float(tp1), 8),
            "mag1_pct":  0.0,    # marker: ATR-fallback, not CandleNet-driven
            "mag3_pct":  0.0,
        }

    # Use abs() so CandleNet's signed magnitude (negative = predicts down)
    # always places TP in the FAVOURABLE direction for both longs and shorts.
    # Without abs(), a short with mag1_avg=-2% would get TP ABOVE entry
    # (neg × sign(-1) = positive offset), firing immediately at a loss.
    tp1 = entry_price * (1.0 + (abs(mag1_avg) / 100.0) * sign)
    tp2 = entry_price * (1.0 + (abs(mag3_avg) / 100.0) * sign)

    # tp2 must be further from entry than tp1 in the favourable direction
    if (sign > 0 and tp2 <= tp1) or (sign < 0 and tp2 >= tp1):
        tp2 = entry_price * (1.0 + (abs(mag1_avg) * 2.0 / 100.0) * sign)

    # cont. 65k Guard 2 — clamp mag-driven TPs to the per-leverage ceiling.
    tp1, tp2 = _apply_tp_ceiling(tp1, tp2, entry_price, sign, leverage)
    return {
        "tp1":       round(float(tp1), 8),
        "tp2":       round(float(tp2), 8),
        # Phase A write-through.
        "tp":        round(float(tp1), 8),
        "mag1_pct":  round(float(abs(mag1_avg)), 4),
        "mag3_pct":  round(float(abs(mag3_avg)), 4),
    }


async def monitor_trailing_sl(engine) -> None:
    """
    O-02/O-03: Continuous loop — on every mark price tick, check every open trade's
    trailing SL and trigger close if SL is hit.
    """
    r = redis_client.get()
    import time as _time_mod_loop

    # cont. 70e — launch the Binance→DB reconciler as a sibling background task
    # (main.py is baked into the image; risk/ is bind-mounted, so spawning it
    # here makes it a restart-only deploy). It closes ghosts (DB open / Binance
    # flat) and reports DB↔Binance net drift. No-op in paper mode.
    try:
        from risk.reconciler import reconcile_loop
        asyncio.ensure_future(reconcile_loop(engine))
    except Exception as _rec_exc:
        log.warning("reconciler_launch_failed", error=str(_rec_exc)[:160])

    while True:
        try:
            trades = get_open_trades()
            _loop_start = _time_mod_loop.monotonic()
            _per_trade_times = []
            for trade in trades:
                _t_start = _time_mod_loop.monotonic()
                pair = trade["pair"]
                sl_level = float(trade.get("trailing_sl_level") or 0)
                if sl_level <= 0:
                    # SL was 0 at open (mark price missing from Redis then).
                    # Try to backfill it now — if mark is available, compute
                    # and write the initial SL so this trade gets protection.
                    try:
                        _now_mark = float(
                            r.get(redis_keys.MARK_PRICE.replace("{pair}", pair)) or 0)
                        if _now_mark > 0:
                            _recovered = compute_initial_sl(
                                pair, trade["direction"],
                                strategy_id=trade.get("strategy_id"))
                            if _recovered > 0:
                                # cont. 62 — apply capital-anchored floor on
                                # backfill so a recovered SL also respects the
                                # 50%-of-capital mandate.
                                _bf_cap = float(trade.get("capital_usdt") or 0)
                                _bf_lev = float(trade.get("leverage") or 0)
                                if _bf_cap > 0 and _bf_lev > 0:
                                    _recovered = apply_capital_sl_floor(
                                        _recovered, _now_mark,
                                        trade["direction"], _bf_cap, _bf_lev,
                                        r=r)
                                engine.modify_sl(trade["id"], _recovered, force=True)
                                log.info("sl_recovered_backfill",
                                         trade_id=trade["id"],
                                         pair=pair, sl=_recovered)
                                r.incr("trail:sl_recovered_count")
                    except Exception as _exc:
                        log.warning("sl_backfill_failed",
                                    trade_id=str(trade.get("id")),
                                    error=str(_exc)[:120])
                    continue

                # cont. 47 — Race-condition guard. monitor_trailing_sl runs
                # every 1s; engine.close_trade can take 200ms-1s (Binance
                # network + DB write). Without this guard, the SAME trade
                # could be closed TWICE on consecutive ticks before the DB's
                # status='closed' write lands. On live mode the second close
                # places a fresh OPPOSITE-direction order on Binance —
                # creating an unintended new position. The 2026-05-25
                # NILUSDT incident produced exactly this. Redis flag with
                # 120s TTL bridges the close-trade write window.
                _closing_key = f"trade:{trade['id']}:closing"
                try:
                    if r.get(_closing_key):
                        continue   # close already in flight
                except Exception:
                    pass

                mark = float(r.get(redis_keys.MARK_PRICE.replace("{pair}", pair)) or 0)
                if mark <= 0:
                    continue

                direction = trade["direction"]

                def _guarded_close(reason: str) -> None:
                    """Set the close-in-flight flag, then call engine.close_trade.
                    cont. 70d — close failures must NOT escape (they used to abort
                    the whole monitor tick) and the flag must ALWAYS be cleared
                    (a stuck flag skipped the trade for 120s, so a failed close
                    retried forever and left the DB row open = ghost)."""
                    try:
                        r.setex(_closing_key, 120, "1")
                    except Exception:
                        pass
                    try:
                        engine.close_trade(trade["id"], reason=reason)
                    except Exception as _gc_exc:
                        log.error("guarded_close_failed", trade_id=trade["id"],
                                  reason=reason, error=str(_gc_exc)[:180])
                    finally:
                        try:
                            r.delete(_closing_key)
                        except Exception:
                            pass

                # cont. 57 — Inline final-tick peak update. Called immediately
                # before any SL/TP/MTF-15m exit `continue` so the final tick's
                # peak isn't lost (the regular peak block below is skipped on
                # exit ticks). Uses actual `quantity` (NOT capital × leverage)
                # so it stays correct after a TP1 partial halved the position.
                # Folds any realised `partial_pnl_usdt` into the total so the
                # high-water mark tracks unrealised + realised together.
                def _capture_final_peak(_mark: float) -> None:
                    _entry = float(trade.get("average_entry")
                                   or trade.get("entry_price") or 0)
                    _qty = float(trade.get("quantity") or 0)
                    if _entry <= 0 or _qty <= 0:
                        return
                    _sign = 1.0 if direction == "long" else -1.0
                    _mtm = (_mark - _entry) * _qty * _sign
                    try:
                        _partial = float(
                            r.get(f"trade:{trade['id']}:partial_pnl_usdt") or 0)
                    except (TypeError, ValueError):
                        _partial = 0.0
                    _total = _mtm + _partial
                    _pp = float(trade.get("peak_pnl_usdt") or 0)
                    _pl = float(trade.get("peak_loss_usdt") or 0)
                    _upd = {}
                    if _total > _pp:
                        _upd["peak_pnl_usdt"]  = round(_total, 4)
                    if _total < _pl:
                        _upd["peak_loss_usdt"] = round(_total, 4)
                    if _upd:
                        try:
                            from memory.write import write_trade_update as _wtu
                            _wtu(trade["id"], _upd)
                        except Exception:
                            pass

                # Path F (cont. 53): 15m close-direction veto.
                # cont. 64: gate by "currently losing" only.
                # cont. 67 fix: (a) Redis kill switch trail:mtf_15m_reversal_enabled=0;
                # (b) post-entry candle filter — only candles whose open time (t ms)
                # is >= trade entry_time are counted, so pre-entry market history
                # can't trigger an immediate close on a freshly opened trade.
                # Root cause of 1513 losses (-$6,979): 63% fired under 1 min because
                # absolute candle history was adverse at entry time.
                try:
                    if r.get("trail:mtf_15m_reversal_enabled") != "0":
                        _15m_raw = r.lrange(f"{pair}:15m:candles", 0, 9)
                        if _15m_raw and len(_15m_raw) >= 3:
                            _all_bars = [json.loads(b) for b in _15m_raw]
                            # Filter to candles opened after trade entry.
                            _et = trade.get("entry_time")
                            _entry_ms = (
                                int(_et.timestamp() * 1000)
                                if _et and hasattr(_et, "timestamp")
                                else 0
                            )
                            _post_bars = [
                                b for b in _all_bars
                                if int(b.get("t", 0)) >= _entry_ms
                            ] if _entry_ms > 0 else _all_bars
                            # Need 3 post-entry candles to confirm reversal.
                            if len(_post_bars) >= 3:
                                _bars = _post_bars[:3]
                                if direction == "long":
                                    _all_opposite = all(
                                        float(b.get("c", 0)) < float(b.get("o", 0))
                                        for b in _bars)
                                else:
                                    _all_opposite = all(
                                        float(b.get("c", 0)) > float(b.get("o", 0))
                                        for b in _bars)
                                if _all_opposite:
                                    _f_entry = float(trade.get("average_entry")
                                                     or trade.get("entry_price") or 0)
                                    _f_sign  = 1.0 if direction == "long" else -1.0
                                    _f_mtm_pnl_per_unit = (mark - _f_entry) * _f_sign
                                    _currently_losing = (_f_entry > 0
                                                         and _f_mtm_pnl_per_unit < 0)
                                    if not _currently_losing:
                                        try:
                                            r.incr("trail:mtf_15m_skip_winner_count")
                                        except Exception:
                                            pass
                                    else:
                                        log.info("mtf_15m_reversal_force_close",
                                                 trade_id=trade["id"], pair=pair,
                                                 direction=direction,
                                                 post_entry_bars=len(_post_bars))
                                        try:
                                            r.incr("trail:mtf_15m_force_close_count")
                                        except Exception:
                                            pass
                                        _capture_final_peak(mark)
                                        _guarded_close("mtf_15m_reversal_confirmed")
                                        continue
                except Exception as exc:
                    log.debug("path_f_15m_force_close_skipped",
                              trade_id=str(trade.get("id")),
                              error=str(exc)[:120])

                # F48 §Idea B (cont. 47) — Magnitude-driven TP check.
                # TP1 hit → close HALF of remaining qty (partial — "scale-out")
                # TP2 hit → close FULL remaining qty
                # SL hit  → close FULL remaining qty (handled below)
                # Per-trade state: trade:{id}:tp1_fired Redis flag = "1" once
                # TP1 has been partially-closed, so we don't re-fire it.
                tp1 = 0.0
                tp2 = 0.0
                try:
                    _tp1_raw = r.get(f"trade:{trade['id']}:tp1")
                    if _tp1_raw is not None:
                        tp1 = float(_tp1_raw)
                    _tp2_raw = r.get(f"trade:{trade['id']}:tp2")
                    if _tp2_raw is not None:
                        tp2 = float(_tp2_raw)
                except (TypeError, ValueError):
                    tp1 = tp2 = 0.0

                # cont. 64 (2026-05-30) — TP-as-checkpoint semantics. The
                # `tp1_fired` / `tp2_fired` Redis keys are RENAMED in spirit
                # to `tp1_locked` / `tp2_locked` — they mark that the SL
                # ratchet has been advanced to the checkpoint level, NOT
                # that any position close happened. The legacy `tp1_fired`
                # key is also still read for back-compat with in-flight trades
                # opened pre-cont.64. Either flag being set means "TP1
                # checkpoint already applied to this trade — do not re-apply".
                _tp1_already_locked = (
                    r.get(f"trade:{trade['id']}:tp1_locked") == "1"
                    or r.get(f"trade:{trade['id']}:tp1_fired") == "1")
                _tp2_already_locked = (
                    r.get(f"trade:{trade['id']}:tp2_locked") == "1")

                def _tp_hit(level: float, dir_: str, mark_: float) -> bool:
                    if level <= 0:
                        return False
                    return (dir_ == "long" and mark_ >= level) or \
                           (dir_ == "short" and mark_ <= level)

                # cont. 62 — Wickless TP confirmation. The mark price reflects
                # every micro-wick; a 1-tick spike that touches TP and reverts
                # would otherwise trigger an instant partial close at the wick
                # high (zero margin for slippage). Require that mark has been
                # past the TP level CONTINUOUSLY for `min_seconds` (default 30s,
                # ~half a 1m candle) before firing. SL is excluded — it must
                # fire fast to bound loss.
                # Runtime overrides:
                #   risk:wickless_tp_disabled (set "1" to revert to mark-only)
                #   risk:wickless_tp_debounce_s (default 30)
                try:
                    _wickless_disabled = (r.get("risk:wickless_tp_disabled") == "1")
                    _wickless_min_s = float(r.get("risk:wickless_tp_debounce_s") or 30.0)
                except (TypeError, ValueError):
                    _wickless_disabled = False
                    _wickless_min_s = 30.0

                def _tp_confirmed(level: float, dir_: str, mark_: float,
                                  tp_label: str) -> bool:
                    """Returns True only when mark has been past `level`
                    continuously for `_wickless_min_s` seconds. Tracks first-
                    crossing timestamp per trade per TP tier in Redis. If
                    price reverts off the TP side, the armed key is cleared
                    so the next attempt must re-debounce from scratch.
                    """
                    if not _tp_hit(level, dir_, mark_):
                        try:
                            r.delete(f"trade:{trade['id']}:{tp_label}_armed_at")
                        except Exception:
                            pass
                        return False
                    if _wickless_disabled or _wickless_min_s <= 0:
                        return True
                    import time as _time_mod
                    armed_key = f"trade:{trade['id']}:{tp_label}_armed_at"
                    now_s = _time_mod.time()
                    try:
                        armed_raw = r.get(armed_key)
                        if armed_raw is None:
                            r.setex(armed_key, 600, str(now_s))
                            return False   # newly armed — wait for debounce
                        return (now_s - float(armed_raw)) >= _wickless_min_s
                    except Exception:
                        # Redis hiccup — fail-OPEN so TP isn't blocked indefinitely.
                        return True

                def _tp_peak_crossed(level: float, dir_: str,
                                     tp_label: str) -> bool:
                    """cont. 65 — peak-crossed latch. Returns True once the
                    trade's peak mark has been past `level` in the favourable
                    direction. Latched in Redis (`trade:{id}:{tp}_peak_crossed`),
                    24h TTL. Replaces the strict tick-time `_tp_confirmed`
                    check for the TP-as-checkpoint semantics: the user
                    observed (ZAMAUSDT, 151.9 % capital peak) that
                    fast-moving wicks crossed TP1 between monitor ticks and
                    `_tp_confirmed`'s armed_at-clear-on-revert logic
                    repeatedly reset the 30s debounce, so the checkpoint
                    never fired. With this latch, ONE genuine peak-cross of
                    TP is sufficient — the checkpoint fires on the very next
                    tick. SL is moved to the TP level (NOT entry/TP1 — see
                    TP fire blocks below) so the trade only exits if mark
                    bounces back through the new SL.
                    """
                    if level <= 0:
                        return False
                    _entry_v = float(trade.get("average_entry")
                                     or trade.get("entry_price") or 0)
                    _qty_v = float(trade.get("quantity") or 0)
                    if _entry_v <= 0 or _qty_v <= 0:
                        return False
                    latch_key = f"trade:{trade['id']}:{tp_label}_peak_crossed"
                    try:
                        if r.get(latch_key) == "1":
                            return True
                    except Exception:
                        pass
                    # cont. 65 bugfix — self-contained: must NOT reference
                    # outer-scope `peak_pnl`/`current_pnl` (those are assigned
                    # later at ~line 976/996, AFTER this closure is called at
                    # the TP1/TP2 check sites). Compute fresh from `mark`
                    # (in scope from line 428) + DB peak from `trade` row.
                    _sign = 1.0 if dir_ == "long" else -1.0
                    _db_peak = float(trade.get("peak_pnl_usdt") or 0)
                    _tick_pnl = (mark - _entry_v) * _qty_v * _sign
                    _peak_pnl_now = max(_db_peak, _tick_pnl, 0.0)
                    # peak_pnl = (peak_mark - entry) * qty * sign
                    # → peak_mark = entry + sign * peak_pnl / qty
                    _peak_mark = _entry_v + _sign * _peak_pnl_now / _qty_v
                    crossed = (
                        (dir_ == "long" and _peak_mark >= level) or
                        (dir_ == "short" and _peak_mark <= level)
                    )
                    if crossed:
                        try:
                            r.setex(latch_key, 86400, "1")
                            r.incr(f"trail:{tp_label}_peak_crossed_count")
                        except Exception:
                            pass
                        return True
                    return False

                # TP2 checkpoint (cont. 65) — lock SL to **TP2 price**.
                # No close. User mandate 2026-05-30: "after TP2 lock the SL
                # trailing will trail 85 % of the profit" — implemented as
                # SL = TP2, then the existing 85 % post-TP2 trail-cap (in
                # both Path A and Path B further below) provides the
                # additional ratchet protection on subsequent gains.
                # Previous cont. 64 logic moved SL to TP1 here — superseded.
                # Fire condition is peak-crossed (latched in Redis), NOT the
                # strict tick-time `_tp_confirmed`, so fast moves that wick
                # past TP2 between monitor ticks reliably register.
                if (tp2 > 0 and not _tp2_already_locked
                        and _tp_peak_crossed(tp2, direction, "tp2")):
                    log.info("tp2_checkpoint_lock",
                             trade_id=trade["id"], mark=mark,
                             tp1=tp1, tp2=tp2, new_sl=tp2,
                             tp1_already=_tp1_already_locked)
                    _capture_final_peak(mark)
                    try:
                        engine.modify_sl(trade["id"], tp2)
                    except Exception as _tp2_exc:
                        # cont. 70 — never let one trade's SL move abort the
                        # whole monitor loop (one outer handler at func bottom).
                        log.warning("tp2_checkpoint_modify_failed",
                                    trade_id=trade["id"],
                                    error=str(_tp2_exc)[:160])
                    r.set(f"trade:{trade['id']}:tp1_locked", "1")
                    r.set(f"trade:{trade['id']}:tp2_locked", "1")
                    try:
                        from memory.write import write_trade_update as _wtu_tp
                        _wtu_tp(trade["id"], {
                            "tp_fired":  True,
                            "tp1_fired": True,
                        })
                        r.incr("trail:tp2_checkpoint_locked_count")
                    except Exception as _wtp_exc:
                        log.warning("tp2_db_persist_failed",
                                    trade_id=trade["id"],
                                    error=str(_wtp_exc)[:200])
                    # Clear the wickless-debounce armed timestamps so they
                    # can't accidentally re-fire after the lock.
                    try:
                        r.delete(f"trade:{trade['id']}:tp1_armed_at",
                                 f"trade:{trade['id']}:tp2_armed_at")
                    except Exception:
                        pass
                    continue

                # TP1 checkpoint (cont. 65) — lock SL to **TP1 price**.
                # No close, no qty change. User mandate 2026-05-30: TP1 is a
                # checkpoint where SL jumps to TP1, locking the TP1-worth of
                # profit. The trade only exits if mark bounces back to TP1.
                # Previous cont. 64 logic moved SL to entry (breakeven) here
                # — superseded. Fire condition is peak-crossed (latched in
                # Redis), NOT the strict tick-time `_tp_confirmed`, so fast
                # moves that wick past TP1 between monitor ticks reliably
                # register. The monotonic guard inside engine.modify_sl
                # safely rejects a non-tighter SL.
                if (tp1 > 0 and not _tp1_already_locked
                        and _tp_peak_crossed(tp1, direction, "tp1")):
                    entry_p = float(trade.get("average_entry")
                                    or trade.get("entry_price") or 0)
                    if entry_p > 0:
                        log.info("tp1_checkpoint_lock",
                                 trade_id=trade["id"], mark=mark,
                                 tp1=tp1, new_sl=tp1)
                        _capture_final_peak(mark)
                        try:
                            engine.modify_sl(trade["id"], tp1)
                        except Exception as _tp1_exc:
                            # cont. 70 — contain blast radius (see TP2 above).
                            log.warning("tp1_checkpoint_modify_failed",
                                        trade_id=trade["id"],
                                        error=str(_tp1_exc)[:160])
                        r.set(f"trade:{trade['id']}:tp1_locked", "1")
                        try:
                            from memory.write import write_trade_update as _wtu_tp1
                            _wtu_tp1(trade["id"], {
                                "tp_fired":  True,
                                "tp1_fired": True,
                            })
                            r.incr("trail:tp1_checkpoint_locked_count")
                        except Exception as _wtp_exc:
                            log.warning("tp1_db_persist_failed",
                                        trade_id=trade["id"],
                                        error=str(_wtp_exc)[:200])
                        # Clear the wickless-debounce armed timestamp so a
                        # later pull-back-and-recross doesn't re-arm.
                        try:
                            r.delete(f"trade:{trade['id']}:tp1_armed_at")
                        except Exception:
                            pass
                        continue

                # O-04: Close if SL is hit
                if direction == "long" and mark <= sl_level:
                    log.info("sl_hit", trade_id=trade["id"], mark=mark, sl=sl_level)
                    _capture_final_peak(mark)
                    _guarded_close("trailing_sl")
                    continue
                if direction == "short" and mark >= sl_level:
                    log.info("sl_hit", trade_id=trade["id"], mark=mark, sl=sl_level)
                    _capture_final_peak(mark)
                    _guarded_close("trailing_sl")
                    continue

                # cont. 66 — Hard minute-granularity time-stop (scalping/HFT
                # regime support). Unlike the 48h barrier below, this fires
                # REGARDLESS of PnL or TP1 state — it is the "strict time-based
                # exit" required by short-horizon (15-min / 30-min) holding
                # strategies: market-close the moment max_hold_minutes elapses
                # so capital recycles into fresh momentum instead of hope-trading.
                # DISABLED by default (bot:max_hold_minutes = 0) so the current
                # swing posture is untouched until the user opts in. Placed
                # before the TP1-gated 48h barrier so it applies even post-TP1.
                try:
                    _mhm = float(r.get("bot:max_hold_minutes") or 0)
                except (TypeError, ValueError):
                    _mhm = 0.0
                if _mhm > 0:
                    try:
                        _ts_entry = trade.get("entry_time")
                        _ts_elapsed = 0.0
                        if _ts_entry and hasattr(_ts_entry, "replace"):
                            from datetime import datetime, timezone
                            _ts_elapsed = (datetime.now(timezone.utc)
                                           - _ts_entry.replace(tzinfo=timezone.utc)
                                           ).total_seconds()
                        elif trade.get("hold_time_seconds") is not None:
                            _ts_elapsed = float(trade.get("hold_time_seconds") or 0)
                        if _ts_elapsed > _mhm * 60.0:
                            log.info("time_stop_minutes_force_close",
                                     trade_id=trade["id"], pair=pair,
                                     direction=direction,
                                     elapsed_min=round(_ts_elapsed / 60.0, 2),
                                     max_min=_mhm)
                            try:
                                r.incr("trail:max_hold_minutes_count")
                            except Exception:
                                pass
                            _capture_final_peak(mark)
                            _guarded_close("time_stop_minutes")
                            continue
                    except Exception as _tsexc:
                        log.debug("time_stop_minutes_skipped",
                                  trade_id=str(trade.get("id")),
                                  error=str(_tsexc)[:120])

                # cont. 59 — Time barrier (Lopez de Prado Triple Barrier).
                # Force-close any trade that has neither hit TP1 nor SL within
                # bot:max_hold_hours (default 48h). Eliminates dead-money
                # sideways trades that bleed capital via fees/funding without
                # resolving. cont. 64: skipped after TP1 has been locked
                # (checkpoint semantics — the trade is now risk-free and
                # trailing should be allowed to ride the continuation as
                # long as needed).
                # Source: Lopez de Prado "Advances in Financial ML" ch. 3.4.
                if not _tp1_already_locked:
                    try:
                        _entry_time = trade.get("entry_time")
                        _elapsed_s = 0.0
                        if _entry_time and hasattr(_entry_time, "replace"):
                            from datetime import datetime, timezone
                            _elapsed_s = (datetime.now(timezone.utc)
                                          - _entry_time.replace(tzinfo=timezone.utc)
                                          ).total_seconds()
                        elif trade.get("hold_time_seconds") is not None:
                            _elapsed_s = float(trade.get("hold_time_seconds") or 0)
                        try:
                            _max_hold_h = float(r.get("bot:max_hold_hours") or 48)
                        except (TypeError, ValueError):
                            _max_hold_h = 48.0
                        if _elapsed_s > _max_hold_h * 3600:
                            log.info("time_barrier_force_close",
                                     trade_id=trade["id"], pair=pair,
                                     direction=direction,
                                     elapsed_h=round(_elapsed_s/3600, 1),
                                     max_h=_max_hold_h)
                            try:
                                r.incr("trail:time_barrier_count")
                            except Exception:
                                pass
                            _capture_final_peak(mark)
                            _guarded_close("time_barrier_max_hold")
                            continue
                    except Exception as _exc:
                        log.debug("time_barrier_skipped",
                                  trade_id=str(trade.get("id")),
                                  error=str(_exc)[:120])

                # cont. 62 — Dead-trade time-exit (owner mandate 2026-05-29).
                # "If a trade has been open > 30 min, is in loss, never reached
                # profit, AND the loss is small (≤ $2 / ≤ 1.5 % of capital) →
                # close. If the loss is much larger → WAIT for it to
                # mean-revert into the small band, then close. Hard 6 h
                # backstop in case mean reversion never arrives."
                #
                # Looser defaults chosen by owner (D-3): 45 min age, $2 abs,
                # 1.5 % capital, $0.50 peak-ever, 6 h force-close ceiling.
                #
                # Coexists with §10.4: only fires for trades that never showed
                # real profit (peak_pnl_usdt ≤ peak_threshold). Profitable
                # trades are governed by the ratchet + 48 h backstop.
                # Frozen trades (Brain manual control) are exempt.
                #
                # Runtime overrides: redis_keys.RISK_DEAD_TRADE_*
                try:
                    if r.get("risk:dead_trade_disabled") != "1":
                        # Frozen-trade guard — Brain owns these.
                        _dt_frozen = r.get(redis_keys.TRADE_TRAIL_FROZEN.replace(
                            "{trade_id}", str(trade["id"])))
                        if not (_dt_frozen and str(_dt_frozen).strip()
                                in ("1", "true", "True", "yes")):
                            # Read knobs (Redis runtime; defaults match D-3).
                            try:
                                _dt_min_age = float(
                                    r.get("risk:dead_trade_min_age_s") or 2700.0)
                                _dt_abs_max = float(
                                    r.get("risk:dead_trade_small_abs_usdt") or 2.0)
                                _dt_pct_max = float(
                                    r.get("risk:dead_trade_small_pct_capital") or 0.015)
                                _dt_peak_max = float(
                                    r.get("risk:dead_trade_peak_threshold_usdt") or 0.5)
                                _dt_force_s = float(
                                    r.get("risk:dead_trade_force_close_max_s") or 21600.0)
                            except (TypeError, ValueError):
                                _dt_min_age = 2700.0
                                _dt_abs_max = 2.0
                                _dt_pct_max = 0.015
                                _dt_peak_max = 0.5
                                _dt_force_s = 21600.0

                            # Trade age in seconds.
                            _dt_entry_time = trade.get("entry_time")
                            _dt_age_s = 0.0
                            if _dt_entry_time and hasattr(_dt_entry_time, "replace"):
                                from datetime import datetime, timezone
                                _dt_age_s = (datetime.now(timezone.utc)
                                             - _dt_entry_time.replace(tzinfo=timezone.utc)
                                             ).total_seconds()
                            elif trade.get("hold_time_seconds") is not None:
                                _dt_age_s = float(trade.get("hold_time_seconds") or 0)

                            # Compute current PnL inline (full ratchet block
                            # below recomputes — duplication OK; we need it now).
                            _dt_entry = float(trade.get("average_entry")
                                              or trade.get("entry_price") or 0)
                            _dt_capital = float(trade.get("capital_usdt") or 0)
                            _dt_lev = float(trade.get("leverage") or 1)
                            _dt_qty = float(trade.get("quantity") or 0)
                            _dt_sign = 1.0 if direction == "long" else -1.0
                            _dt_pnl_usdt = 0.0
                            if _dt_entry > 0 and _dt_qty > 0:
                                _dt_pnl_usdt = (mark - _dt_entry) * _dt_qty * _dt_sign
                                try:
                                    _dt_partial = float(
                                        r.get(f"trade:{trade['id']}:partial_pnl_usdt")
                                        or 0)
                                except (TypeError, ValueError):
                                    _dt_partial = 0.0
                                _dt_pnl_usdt += _dt_partial
                            _dt_peak_usdt = float(trade.get("peak_pnl_usdt") or 0)
                            _dt_pct_capital = (
                                abs(_dt_pnl_usdt) / _dt_capital
                                if _dt_capital > 0 else 0.0)

                            _dt_age_ok = _dt_age_s >= _dt_min_age
                            _dt_in_loss = _dt_pnl_usdt < 0
                            _dt_never_profited = _dt_peak_usdt <= _dt_peak_max
                            _dt_small_loss = (
                                abs(_dt_pnl_usdt) <= _dt_abs_max
                                or _dt_pct_capital <= _dt_pct_max)
                            _dt_force = (_dt_force_s > 0
                                         and _dt_age_s >= _dt_force_s)

                            if (_dt_age_ok and _dt_in_loss
                                    and _dt_never_profited):
                                if _dt_small_loss:
                                    log.info("dead_trade_time_exit_close",
                                             trade_id=trade["id"], pair=pair,
                                             age_s=round(_dt_age_s, 1),
                                             pnl_usdt=round(_dt_pnl_usdt, 4),
                                             pct_capital=round(_dt_pct_capital, 4),
                                             peak_usdt=round(_dt_peak_usdt, 4))
                                    try:
                                        r.incr("trail:dead_trade_exit_count")
                                    except Exception:
                                        pass
                                    _capture_final_peak(mark)
                                    _guarded_close("dead_trade_time_exit")
                                    continue
                                elif _dt_force:
                                    log.info("dead_trade_force_close_max_age",
                                             trade_id=trade["id"], pair=pair,
                                             age_s=round(_dt_age_s, 1),
                                             pnl_usdt=round(_dt_pnl_usdt, 4),
                                             pct_capital=round(_dt_pct_capital, 4))
                                    try:
                                        r.incr("trail:dead_trade_force_close_count")
                                    except Exception:
                                        pass
                                    _capture_final_peak(mark)
                                    _guarded_close("dead_trade_force_close_max_age")
                                    continue
                                else:
                                    # Loss too large — wait for mean reversion
                                    # back into the small band. Log so the
                                    # decision is auditable (silent-rejection rule).
                                    log.info("dead_trade_time_exit_wait",
                                             trade_id=trade["id"], pair=pair,
                                             age_s=round(_dt_age_s, 1),
                                             pnl_usdt=round(_dt_pnl_usdt, 4),
                                             pct_capital=round(_dt_pct_capital, 4),
                                             abs_threshold=_dt_abs_max,
                                             pct_threshold=_dt_pct_max,
                                             force_close_at_s=_dt_force_s)
                                    try:
                                        r.incr("trail:dead_trade_wait_count")
                                    except Exception:
                                        pass
                except Exception as _dtex:
                    log.debug("dead_trade_time_exit_skipped",
                              trade_id=str(trade.get("id")),
                              error=str(_dtex)[:120])

                # cont. 60 — Frontier exit-feature evaluation.
                # Runs 13 SOTA SL/TP techniques (CVD divergence, filtered OBI,
                # funding/premium, Hawkes self-excitation, MM-Hawkes spoof,
                # BOCPD changepoint, Conformal bands, Mamba quantiles,
                # Diffusion bands, GNN contagion, LLM exit council, PPO policy,
                # Deep Hedging policy). Each feature is advisory: failures
                # degrade silently, force_close wins immediately, tighten/loosen
                # modifiers fold into the downstream Chandelier distance.
                # Sources: 24+ academic / industry research papers (2024-2026)
                # documented in PROGRESS.md cont. 60.
                try:
                    from risk.frontier import evaluate_all_exits as _front_eval
                    _front_dec = _front_eval(trade, mark, r, sl_level, direction)
                    if _front_dec.force_close:
                        log.info("frontier_force_close",
                                 trade_id=trade["id"], pair=pair,
                                 reason=_front_dec.reason,
                                 features=_front_dec.features_fired,
                                 notes=_front_dec.notes)
                        try:
                            r.incr("trail:frontier_force_close_count")
                        except Exception:
                            pass
                        _capture_final_peak(mark)
                        _guarded_close(_front_dec.reason)
                        continue
                except Exception as _fexc:
                    log.debug("frontier_eval_skipped",
                              trade_id=str(trade.get("id")),
                              error=str(_fexc)[:120])
                    _front_dec = None

                # Variables consumed by the downstream ratchet block
                # (notional, profit_pct, locked_pct_notional). These are the
                # full-notional formulations the ratchet was designed around;
                # ratchet behaviour stays %-based even after a TP1 partial.
                entry = float(trade.get("average_entry") or trade.get("entry_price") or 0)
                capital = float(trade.get("capital_usdt") or 0)
                leverage = int(trade.get("leverage") or 1)
                quantity = float(trade.get("quantity") or 0)
                direction_sign = 1.0 if direction == "long" else -1.0
                current_pnl = 0.0
                if entry > 0 and capital > 0:
                    current_pnl = capital * leverage * (mark - entry) / entry * direction_sign

                # Update peak PnL tracking (cont. 57 fix — was 2× inflated
                # after TP1 partial fired because the prior formula used
                # `capital * leverage * pct_change` which is the FULL-position
                # notional even though close_partial halved `quantity` but
                # left `capital_usdt` unchanged. Peak now uses
                # `quantity * (mark - entry) * direction_sign` so the MTM
                # rides the actual open quantity, plus realised
                # `partial_pnl_usdt` for the realised+unrealised high-water).
                if entry > 0 and quantity > 0:
                    mtm_pnl = (mark - entry) * quantity * direction_sign
                    try:
                        partial_pnl_acc = float(
                            r.get(f"trade:{trade['id']}:partial_pnl_usdt") or 0)
                    except (TypeError, ValueError):
                        partial_pnl_acc = 0.0
                    total_open_pnl = mtm_pnl + partial_pnl_acc
                    peak_pnl  = float(trade.get("peak_pnl_usdt") or 0)
                    peak_loss = float(trade.get("peak_loss_usdt") or 0)
                    updates = {}
                    if total_open_pnl > peak_pnl:
                        updates["peak_pnl_usdt"] = round(total_open_pnl, 4)
                    if total_open_pnl < peak_loss:
                        updates["peak_loss_usdt"] = round(total_open_pnl, 4)
                    if updates:
                        from memory.write import write_trade_update
                        write_trade_update(trade["id"], updates)

                # cont. 65k-5 — AUTHORITATIVE CAPITAL-LADDER SL (owner's exact spec
                # 2026-05-31). Replaces the legacy multi-path ratchet below. All
                # thresholds are % of CAPITAL (margin, before leverage):
                #   profit < 10%        → leave initial −50% SL (no move)
                #   10% ≤ profit < 15%  → SL = max(+10%, 50%×profit)  [activate +10, trail 50%]
                #   15% ≤ profit < 30%  → SL = max(+15% (TP1), 75%×profit)
                #   profit ≥ 30%        → SL = max(+30% (TP2), 85%×profit)
                # Price = entry × (1 + sign × (capital%/100)/leverage). engine.modify_sl
                # ratchets (rejects non-tighter). SL-hit close + TP1/TP2 checkpoints
                # already ran ABOVE, so `continue` is safe. Toggle:
                # risk:capital_ladder_enabled=0 → legacy ratchet.
                if ((r.get("risk:capital_ladder_enabled") or "1") != "0"
                        and entry > 0 and capital > 0 and leverage > 0):
                    _peak_usdt = max(float(trade.get("peak_pnl_usdt") or 0),
                                     current_pnl, 0.0)
                    _peak_cap = _peak_usdt / capital * 100.0   # +% of capital
                    # cont. 69z — activation lowered 0.10 -> 0.06 so it sits BELOW tp1c
                    # (0.08). With act>=tp1c the `elif peak<tp1c` stage-1 branch was DEAD
                    # (10>8), collapsing the 3-stage ladder to 2 and leaving 6-10%-cap
                    # peakers unprotected (HEI/KERNEL/BLESS reversed from 7-9% peaks).
                    # cont. 70 — owner lowered activation 0.06 -> 0.04 and stage-1 trail
                    # 60% -> 50%, so at the 4% activation peak the SL locks at 50%*4% = 2%.
                    # Now: hold<4 | 4-8 trail 50% | 8-16 trail 75% | >=16 trail 85%.
                    _act  = float(r.get("risk:capital_activation_pct") or 0.04) * 100.0
                    # cont. 69u — TP rungs lowered 0.15/0.30 -> 0.08/0.16 (MFE backtest:
                    # only 17%/3.7% of trades ever reached 15%/30% cap; take profit early).
                    _tp1c = float(r.get("risk:tp1_capital_pct") or 0.08) * 100.0
                    _tp2c = float(r.get("risk:tp2_capital_pct") or 0.16) * 100.0
                    _tgt = None
                    if _peak_cap < _act:
                        _tgt = None                                  # below activation: hold −50%
                    elif _peak_cap < _tp1c:
                        # cont. 70 — owner mandate 2026-06-03: stage-1 trails a TRUE
                        # 50% of peak FROM ACTIVATION (no floor pin). At the 4% activation
                        # peak the SL locks 2% (= 50% of 4%) and trails 50% of peak up to
                        # TP1, letting the trade breathe. Stages 2/3 unchanged (rest are
                        # same). Reversible without redeploy: set Redis
                        # risk:ladder_stage1_floor="1" to restore the activation pin.
                        if (r.get("risk:ladder_stage1_floor") or "0") == "1":
                            _tgt = max(_act, 0.50 * _peak_cap)       # legacy: pinned at activation
                        else:
                            _tgt = 0.50 * _peak_cap                  # true 50% trail from activation
                    elif _peak_cap < _tp2c:
                        _tgt = max(_tp1c, 0.75 * _peak_cap)          # TP1 floor, trail 75%
                    else:
                        _tgt = max(_tp2c, 0.85 * _peak_cap)          # TP2 floor, trail 85%
                    if _tgt is not None:
                        _sl_price = entry * (1.0 + direction_sign * (_tgt / 100.0) / leverage)
                        try:
                            engine.modify_sl(trade["id"], round(_sl_price, 8))
                            r.incr("trail:capital_ladder_applied_count")
                        except Exception as _cl_exc:
                            log.debug("capital_ladder_modify_failed",
                                      trade_id=trade["id"], error=str(_cl_exc)[:120])
                    continue

                # F51d (cont. 51): Chandelier Exit watermark tracking.
                # Track highest_high (longs) and lowest_low (shorts) since
                # entry per trade in Redis. Chandelier SL anchors at the
                # watermark minus N×ATR — auto-scales with both price level
                # and current volatility, unlike fixed-dollar tiers. Floor
                # to entry so an early adverse move doesn't seed a watermark
                # tighter than entry.
                _hh_key = f"trade:{trade['id']}:highest_high"
                _ll_key = f"trade:{trade['id']}:lowest_low"
                if entry > 0:
                    try:
                        _hh = float(r.get(_hh_key) or entry)
                        _ll = float(r.get(_ll_key) or entry)
                        if mark > _hh:
                            r.set(_hh_key, mark)
                        if mark < _ll:
                            r.set(_ll_key, mark)
                    except (TypeError, ValueError):
                        r.set(_hh_key, max(entry, mark))
                        r.set(_ll_key, min(entry, mark))

                # O-07 (continuous): Post-DCA breakeven check — runs every tick for DCA'd trades.
                # Blueprint: "when price recovers to -10% of original entry → SL moves to break-even"
                # Must run continuously because DCA fires at -20%/-40%, not at the -10% recovery point.
                raw_dca = trade.get("dca_status")
                dca_triggered = raw_dca if isinstance(raw_dca, dict) else (json.loads(raw_dca) if raw_dca else {})
                if dca_triggered.get("round_1_triggered") or dca_triggered.get("round_2_triggered"):
                    original_entry = float(trade.get("entry_price") or 0)
                    avg_entry_be = float(trade.get("average_entry") or original_entry)
                    if original_entry > 0 and avg_entry_be > 0:
                        if direction == "long" and mark >= original_entry * 0.90:
                            if sl_level < avg_entry_be:
                                log.info("sl_breakeven_set", trade_id=trade["id"],
                                         pair=pair, direction=direction,
                                         avg_entry=avg_entry_be, mark=mark)
                                engine.modify_sl(trade["id"], avg_entry_be)
                        elif direction == "short" and mark <= original_entry * 1.10:
                            if sl_level > avg_entry_be:
                                log.info("sl_breakeven_set", trade_id=trade["id"],
                                         pair=pair, direction=direction,
                                         avg_entry=avg_entry_be, mark=mark)
                                engine.modify_sl(trade["id"], avg_entry_be)

                # O-02/O-03: Blueprint Feature 4 + Section 10.4 — volatility-anchored trailing SL.
                # Activation: don't trail until profit clears a volatility-scaled threshold.
                #             Below activation, the initial wide SL is left alone (blueprint:
                #             "SL is placed wide ... as trade moves into profit, SL moves to follow").
                # Distance:   progressive tightening — wide when just activated, tighter as
                #             profit grows ("locking in gains progressively").
                # Volatility: per-pair VPIN via `_volatility_unit` helper (same source as
                #             initial SL — single definition of "this pair's volatility").
                #
                # Brain override surface (Blueprint 15.4 cont. 22d fix #7): per-trade Redis
                # keys let the Brain set its own activation / distance for any open trade,
                # and a freeze flag lets the Brain take full manual control.
                trade_id_str = str(trade["id"])
                # Freeze check: if set, skip the auto-trailing block entirely. Brain
                # is driving this trade's SL by hand via modify_sl(force=True).
                _frozen = r.get(redis_keys.TRADE_TRAIL_FROZEN.replace(
                    "{trade_id}", trade_id_str))
                if _frozen and str(_frozen).strip() in ("1", "true", "True", "yes"):
                    try:
                        r.incr("trail:frozen_skip_count")
                    except Exception:
                        pass
                    # Hedge eval still runs below; just skip trailing.
                else:
                    vol_unit = _volatility_unit(r, pair)
                    activation_pct = max(0.010, 1.5 * vol_unit)

                    _sid = trade.get("strategy_id")
                    _sl_ov = None
                    if _sid:
                        try:
                            from strategy.router import get_sl_overrides
                            _sl_ov = get_sl_overrides(_sid)
                            if _sl_ov and "activation_pct" in _sl_ov:
                                activation_pct = max(0.002, min(0.05, float(_sl_ov["activation_pct"])))
                        except Exception as exc:
                            log.debug("sl_router_activation_skipped", error=str(exc)[:120])

                    # Per-trade activation override (highest precedence — Brain's voice).
                    _act_override = r.get(redis_keys.TRADE_TRAIL_ACT_OVERRIDE.replace(
                        "{trade_id}", trade_id_str))
                    if _act_override is not None:
                        try:
                            activation_pct = max(0.002, min(0.05, float(_act_override)))
                        except (TypeError, ValueError):
                            pass

                    # cont. 62 — Capital-anchored activation FLOOR (owner mandate
                    # 2026-05-29: "tail gating should start AFTER it reach
                    # AT LEAST 15 % of the capital"). Trailing must NOT arm
                    # below `capital_activation_frac / leverage` notional —
                    # so the wide initial 50 %-capital SL persists until real
                    # profit arrives.
                    #
                    # cont. 62b bugfix (was min): the prior min() implementation
                    # let the vol-anchor (≈1 % notional = 5 % capital at 5×)
                    # fire FIRST on low-vol pairs, tightening the wide
                    # 50 %-capital SL within a tick of open. MAX preserves the
                    # owner's "at-least 15 % capital" invariant.
                    #
                    # cont. 62d — default raised 0.11 → 0.15.
                    #
                    # Runtime overrides:
                    #   risk:capital_activation_disabled ("1" → off)
                    #   risk:capital_activation_frac     (default 0.15)
                    capital_leverage = float(
                        trade.get("leverage") or 0) or float(leverage or 0)
                    # Defaults hoisted so Path B/C gates can reference them
                    # regardless of whether the rung adjustment ran.
                    # cont. 64 (2026-05-30): activation lowered 0.15 → 0.10.
                    # cont. 65b (2026-05-30): lowered to 0.05.
                    # cont. 65d (2026-05-30): owner rolled back to 0.10 — 5 %
                    # armed too eagerly on minor wicks. 10 % of capital is the
                    # current floor. Lock caps below ramp 50 → 70 → 85.
                    _cap_act_off = False
                    _cap_act_frac = 0.10
                    if capital_leverage > 0:
                        try:
                            _cap_act_off = (
                                r.get("risk:capital_activation_disabled") == "1")
                            _cap_act_frac = float(
                                r.get("risk:capital_activation_frac") or 0.10)
                        except (TypeError, ValueError):
                            _cap_act_off = False
                            _cap_act_frac = 0.10
                        if not _cap_act_off:
                            _cap_act_frac = max(0.03, min(0.50, _cap_act_frac))
                            capital_activation_pct = (
                                _cap_act_frac / capital_leverage)
                            if capital_activation_pct > activation_pct:
                                activation_pct = capital_activation_pct
                                try:
                                    r.incr("trail:capital_activation_applied_count")
                                except Exception:
                                    pass

                    # cont. 65 — parallel capital-percentage activation
                    # predicate (owner mandate 2026-05-30: "use percentage
                    # everywhere, not dollar"). The `capital_activation_pct >
                    # activation_pct` block above only RAISES the floor — at
                    # leverage ≥ ~10 the 1 % notional floor wins (since
                    # `_cap_act_frac / leverage` drops below 0.01) and the
                    # cont. 64 "10 %-of-capital activates trail" mandate never
                    # fires. This OR-predicate fixes that: whenever
                    # peak_pnl_pct_capital (peak / capital) has crossed
                    # `_cap_act_frac`, treat the trail as armed regardless of
                    # the vol-anchored notional floor. Path A's gate uses
                    # `peak_profit_pct >= activation_pct OR _capital_pct_active`
                    # so high-leverage trades activate at the intended capital
                    # threshold instead of waiting for the 1 % notional move.
                    _capital_pct_active = False
                    if (capital_leverage > 0 and not _cap_act_off
                            and capital > 0):
                        # Use peak_pnl loaded for this trade — refreshed below
                        # to fresh_peak_pnl after we know notional.
                        _peak_pct_capital = float(peak_pnl or 0) / capital
                        _capital_pct_active = (
                            _peak_pct_capital >= _cap_act_frac)

                    notional = capital * leverage if entry > 0 else 0
                    profit_pct = (current_pnl / notional) if notional > 0 else 0.0

                    # Peak-profit ratchet (cont. 23, redesigned cont. 44).
                    # Blueprint §10.4: "as trade moves into profit, SL moves in
                    # the direction of profit — never backward ... locking in
                    # gains progressively."
                    #
                    # cont. 44 (2026-05-24): two parallel activations now arm
                    # the ratchet — whichever fires first wins:
                    #   A) %-activation: peak_profit_pct >= activation_pct
                    #      (vol-anchored, original cont. 23 path)
                    #   B) $-activation: peak_pnl_usdt >= tier_trigger_usdt
                    #      (user-mandated tier table from config.risk.profit_lock_tiers)
                    # B fixes leveraged trades where a $10 peak on a $300
                    # notional is 3.3% — below typical activation of 4.5% on
                    # an alt — but is real money worth locking. Empirically
                    # the $5-20 peak bucket bled $15k pre-cont-44 because
                    # %-activation never armed.
                    #
                    # When B fires, lock_frac comes straight from the tier
                    # table (≥80% per user spec, tiered up to 92% for big peaks).
                    # When A fires, lock_frac comes from the F47 learner (now
                    # clamped 0.80-0.92). The TIGHTER of the two ratchet_sl
                    # candidates wins.
                    #
                    # `peak_pnl` is from the DB row (stale by ≤1s); current_pnl
                    # is fresh — take the larger so the ratchet captures this
                    # tick's high even before the write lands.
                    fresh_peak_pnl = max(peak_pnl, current_pnl)
                    peak_profit_pct = fresh_peak_pnl / notional if notional > 0 else 0.0
                    # cont. 65 — refresh the capital-percentage activation
                    # predicate against the fresh peak (current_pnl can exceed
                    # DB peak by up to ~1s of price movement). Percentage form
                    # per owner mandate 2026-05-30.
                    if (capital_leverage > 0 and not _cap_act_off
                            and capital > 0):
                        _peak_pct_capital = fresh_peak_pnl / capital
                        _capital_pct_active = (
                            _peak_pct_capital >= _cap_act_frac)
                    ratchet_sl = None

                    # cont. 53 — read HMM regime once for all paths below.
                    # Hoisted from the old Path-C-local position so that
                    # Paths A and B can also regime-scale their lock_frac.
                    _regime_now = str(r.get(redis_keys.CURRENT_REGIME) or "unknown").lower()

                    # cont. 65g — single profit-lock cap shared across all
                    # ratchet paths (A, B, D, E). Pre-fix, Paths D (MTF
                    # reversal) and E (exhaustion) used hard-coded 0.95 / 0.90
                    # locks and bypassed the activation gate, silently
                    # overriding Path A's pre-TP1 0.10 cap via the min/max
                    # Trailing-RATE ladder = the fraction of peak profit the SL
                    # follows (NOT a hard lock). Owner mandate cont. 65k-2
                    # (2026-05-31): pre-TP1 trail 60 %, post-TP1 75 %, post-TP2
                    # 85 % — applies to EVERY ratchet candidate, no exceptions.
                    # Activation stays 10 % capital (SL doesn't move until peak
                    # profit ≥10 % capital; see _cap_act_frac above). Pre-TP1 was
                    # 0.10 (cont.65f/g); owner raised to 0.60 for closer early
                    # profit protection after activation.
                    if _tp2_already_locked:
                        _lock_cap = 0.85
                    elif _tp1_already_locked:
                        _lock_cap = 0.75
                    else:
                        _lock_cap = 0.60

                    # cont. 65g — shared activation-armed predicate for the
                    # 10 %-of-capital floor. Used by Paths C, D, E and the
                    # frontier-bound block so a stray tightener can never fire
                    # before the user-mandated 10 % activation.
                    _ratchet_armed_10pct = (
                        capital_leverage > 0
                        and peak_profit_pct >= (
                            _cap_act_frac / capital_leverage
                            if not _cap_act_off else 0.0))

                    # --- Path 0: Breakeven Shield — REMOVED cont. 62d ---
                    # Owner mandate 2026-05-29 removed it. cont. 64 owner
                    # mandate lowered the activation floor 15 % → 10 % of
                    # capital. The 10 %-capital activation floor (above) plus
                    # the standard trailing block now provide profit
                    # protection without snapping SL to entry prematurely.

                    # --- Path B: dollar-tier activation ---
                    # cont. 64 (owner mandate 2026-05-30): Path B is now
                    # gated by the 10 %-capital activation floor. The tier
                    # lookup still runs, but the ratchet_sl assignment is
                    # skipped below 10 % capital peak.
                    _path_b_armed = (
                        capital_leverage > 0
                        and peak_profit_pct >= (
                            _cap_act_frac / capital_leverage
                            if not _cap_act_off else 0.0))
                    # Find highest matching tier (tiers in config are descending).
                    tier_lock_frac = None
                    try:
                        for trig_usdt, lock_pct in config.risk.profit_lock_tiers:
                            if fresh_peak_pnl >= float(trig_usdt):
                                tier_lock_frac = float(lock_pct)
                                break
                    except Exception as exc:
                        log.warning("profit_lock_tier_lookup_failed", error=str(exc)[:120])

                    if (tier_lock_frac is not None and notional > 0
                            and _path_b_armed):
                        # cont. 53 — regime-scale the tier lock_frac. In trending
                        # regimes the 0.80-0.92 tier becomes 0.40-0.55 (let
                        # trends breathe); chop / unknown stays unchanged.
                        tier_lock_frac = _regime_scaled_lock_frac(
                            tier_lock_frac, _regime_now, r=r)
                        # cont. 65g — uses the hoisted `_lock_cap`
                        # (computed once just before Path B) so Paths A/B/D/E
                        # all share the same pre-TP1 10 % / post-TP1 75 % /
                        # post-TP2 85 % ladder.
                        tier_lock_frac = min(tier_lock_frac, _lock_cap)
                        # Lock locked_frac of the dollar peak. Convert dollar →
                        # price level via the standard PnL→price relation:
                        #   locked_pnl = capital * leverage * (sl - entry)/entry * sign
                        # → (sl - entry)/entry = locked_pnl / notional
                        locked_usdt = fresh_peak_pnl * tier_lock_frac
                        locked_pct_notional = locked_usdt / notional
                        if direction == "long":
                            ratchet_sl = round(entry * (1 + locked_pct_notional), 8)
                        else:
                            ratchet_sl = round(entry * (1 - locked_pct_notional), 8)

                    # --- Path A: %-activation (vol-anchored, F47 learner) ---
                    # cont. 65 — OR-gate with the parallel capital-percentage
                    # predicate (owner mandate 2026-05-30: percentage form, not
                    # dollar). Fixes the prior "trail never activates at
                    # 10 %-of-capital for high-leverage trades" bug where the
                    # 1 % notional floor swallowed the cont. 64 mandate. Now
                    # the trail arms when EITHER (a) peak_profit_pct ≥ the
                    # vol-anchored threshold (original semantics) OR (b)
                    # peak / capital ≥ _cap_act_frac (the capital mandate,
                    # leverage-independent).
                    if peak_profit_pct >= activation_pct or _capital_pct_active:
                        # peak_ratio must stay sane when capital path fires
                        # while vol path hasn't — clamp to ≥ 1.0 so the F47
                        # lock_frac formula doesn't underflow.
                        peak_ratio = (
                            max(1.0, peak_profit_pct / activation_pct)
                            if activation_pct > 0 else 1.0)
                        if _capital_pct_active and peak_profit_pct < activation_pct:
                            try:
                                r.incr("trail:capital_pct_only_armed_count")
                            except Exception:
                                pass
                        try:
                            from risk.trail_params import get_lock_frac as _glf
                            lock_frac = _glf(peak_ratio, trade_id=trade_id_str,
                                             with_exploration=True)
                        except Exception:
                            lock_frac = min(0.92, 0.80 + 0.05 * (peak_ratio - 1))
                        # cont. 53 — regime-scale the F47-learned lock_frac.
                        # The learner stays clamped 0.80-0.92 in trail_params.py;
                        # the trending-regime relaxation is applied here so the
                        # learner's gradient (which was trained on chop semantics)
                        # remains interpretable.
                        lock_frac = _regime_scaled_lock_frac(
                            lock_frac, _regime_now, r=r)
                        # cont. 59 — Volatility-sensitive ratchet step.
                        # Compare current vol_unit to 2h rolling avg. When
                        # vol is EXPANDING (trend accelerating), LOOSEN the
                        # lock so we don't snap back during the explosion.
                        # When vol is CONTRACTING (momentum stalling), TIGHTEN
                        # to capture the peak before it fades. Maintains a
                        # 2h history in `{pair}:vol_unit_hist` (60 ticks min,
                        # 120 max @ 1m sampling).
                        # Source: LuxAlgo Volatility Stop, arXiv:2602.11708.
                        try:
                            _vol_hist_key = f"{pair}:vol_unit_hist"
                            _vol_hist = r.lrange(_vol_hist_key, 0, 59)
                            if _vol_hist and len(_vol_hist) >= 10:
                                _vol_avg = (sum(float(v) for v in _vol_hist)
                                            / len(_vol_hist))
                                if _vol_avg > 0:
                                    _vol_now = _volatility_unit(r, pair)
                                    _expansion = _vol_now / _vol_avg
                                    if _expansion > 1.30:
                                        lock_frac = max(0.65, lock_frac - 0.10)
                                        try:
                                            r.incr("trail:vol_expansion_loosen_count")
                                        except Exception:
                                            pass
                                    elif _expansion < 0.70:
                                        lock_frac = min(0.95, lock_frac + 0.05)
                                        try:
                                            r.incr("trail:vol_contraction_tighten_count")
                                        except Exception:
                                            pass
                            # Append current vol_unit and trim to 2h
                            r.lpush(_vol_hist_key, _volatility_unit(r, pair))
                            r.ltrim(_vol_hist_key, 0, 119)
                        except Exception as _exc:
                            log.debug("vol_adaptive_ratchet_skipped",
                                      pair=pair, error=str(_exc)[:120])
                        # cont. 65g — uses hoisted `_lock_cap` (shared with
                        # Paths B/D/E). Same 10 / 75 / 85 ladder.
                        lock_frac = min(lock_frac, _lock_cap)
                        locked_pct = peak_profit_pct * lock_frac
                        if direction == "long":
                            pct_sl = round(entry * (1 + locked_pct), 8)
                            # Take the tighter (higher for long) of the two.
                            ratchet_sl = max(ratchet_sl, pct_sl) if ratchet_sl else pct_sl
                        else:
                            pct_sl = round(entry * (1 - locked_pct), 8)
                            # Take the tighter (lower for short) of the two.
                            ratchet_sl = min(ratchet_sl, pct_sl) if ratchet_sl else pct_sl

                    # --- F51d Path C: Chandelier Exit ratchet candidate ---
                    # Anchors SL at the favourable watermark minus N×ATR.
                    # Auto-scales with volatility AND price level (unlike
                    # fixed-dollar tiers). Standard Chandelier multiplier
                    # (Charles Le Beau) is 2.5–3.0. Source: StockCharts
                    # ChartSchool, QuantifiedStrategies.
                    #
                    # cont. 53: bull/bear widened 2.0× → 3.5×. The 2.0×
                    # value was a defensive cont. 51 choice; with the
                    # cont. 53 regime-adaptive ratchet floor relaxing in
                    # trending regimes, Chandelier should also widen to
                    # match — let trends actually breathe instead of
                    # snapping back at the first 2-ATR retracement.
                    #
                    # cont. 59 — VPIN mid-trade tightening. When VPIN > 0.7,
                    # toxic flow / imminent cascade liquidation is signaled
                    # (Easley & Lopez de Prado 2012). Snap Chandelier mult
                    # to 2.0× regardless of regime to escape the toxic flow
                    # before it inverts. Source: Buildix VPIN guide (2026).
                    _vpin_now = 0.0
                    try:
                        _vpin_now = float(
                            r.get(redis_keys.VPIN.replace("{pair}", pair)) or 0)
                    except (TypeError, ValueError):
                        pass
                    if _vpin_now > 0.7:
                        _chand_mult = 2.0
                        try:
                            r.incr("trail:vpin_tightened_count")
                        except Exception:
                            pass
                    else:
                        _chand_mult = {"bull": 3.5, "bear": 3.5,
                                       "turbulent": 3.0}.get(_regime_now, 2.5)
                    # cont. 60 — Apply frontier tighten/loosen modifiers to
                    # the Chandelier multiplier. Tighten = smaller mult (e.g.
                    # 0.6× from CVD divergence shrinks 3.5×ATR → 2.1×ATR).
                    # Loosen = larger mult (e.g. 1.5× from MM-Hawkes spoof
                    # detector widens 3.5× → 5.25× for stop-hunt ride-out).
                    if _front_dec is not None:
                        _chand_mult *= _front_dec.tighten_sl_mult
                        _chand_mult *= _front_dec.loosen_sl_mult

                    # cont. 62 — Time-decayed Chandelier multiplier.
                    # Research consensus (StratBase, LuxAlgo ATR Stop): trades
                    # that sit open too long should be exited progressively
                    # — multiplier decays as exp(-(age_h - 6)/12). At hour 6
                    # the trail is at full width; at hour 12 it's down to 0.61×;
                    # at hour 24 to 0.22×. Floor 0.5 so we never crush the
                    # trail entirely (the time-barrier at 48h does the final close).
                    # Runtime overrides:
                    #   risk:trail_time_decay_disabled (set "1" to disable)
                    #   risk:trail_time_decay_start_h  (default 6)
                    #   risk:trail_time_decay_halflife_h (default 12)
                    try:
                        if r.get("risk:trail_time_decay_disabled") != "1":
                            _entry_time = trade.get("entry_time")
                            _age_h = 0.0
                            if _entry_time and hasattr(_entry_time, "replace"):
                                from datetime import datetime, timezone
                                _age_h = ((datetime.now(timezone.utc)
                                           - _entry_time.replace(tzinfo=timezone.utc))
                                          .total_seconds() / 3600.0)
                            _decay_start_h = float(r.get("risk:trail_time_decay_start_h") or 6.0)
                            _decay_halflife_h = float(r.get("risk:trail_time_decay_halflife_h") or 12.0)
                            if _age_h > _decay_start_h and _decay_halflife_h > 0:
                                import math as _math
                                _decay = _math.exp(-(_age_h - _decay_start_h) / _decay_halflife_h)
                                _decay = max(0.5, _decay)
                                _chand_mult *= _decay
                                try:
                                    r.incr("trail:time_decay_applied_count")
                                except Exception:
                                    pass
                    except Exception as _tdex:
                        log.debug("trail_time_decay_skipped", error=str(_tdex)[:120])

                    # cont. 62d (owner mandate 2026-05-29): Path C gated by
                    # the 15 %-capital activation floor — Chandelier is
                    # functionally a trailing mechanism (anchors SL at
                    # watermark − N×ATR) and belongs on the same gate as
                    # Path A. Without this gate, Chandelier tightens the
                    # wide 50 %-capital SL on any pair with reasonable
                    # volatility once a small watermark exists.
                    _path_c_armed = (
                        capital_leverage > 0
                        and peak_profit_pct >= (
                            _cap_act_frac / capital_leverage
                            if not _cap_act_off else 0.0))
                    _atr_distance = mark * vol_unit  # VPIN-derived ATR equivalent
                    if entry > 0 and _atr_distance > 0 and _path_c_armed:
                        try:
                            _hh_val = float(r.get(_hh_key) or mark)
                            _ll_val = float(r.get(_ll_key) or mark)
                            if direction == "long":
                                chandelier_sl = round(_hh_val - _atr_distance * _chand_mult, 8)
                                # Chandelier must be ABOVE entry to qualify as
                                # a profit-protecting ratchet — never let it
                                # widen below entry (that's the initial SL's
                                # job, not the trailing ratchet).
                                if chandelier_sl > entry:
                                    ratchet_sl = (max(ratchet_sl, chandelier_sl)
                                                  if ratchet_sl else chandelier_sl)
                                    try:
                                        r.incr("trail:chandelier_applied_count")
                                    except Exception:
                                        pass
                            else:
                                chandelier_sl = round(_ll_val + _atr_distance * _chand_mult, 8)
                                if chandelier_sl < entry:
                                    ratchet_sl = (min(ratchet_sl, chandelier_sl)
                                                  if ratchet_sl else chandelier_sl)
                                    try:
                                        r.incr("trail:chandelier_applied_count")
                                    except Exception:
                                        pass
                        except (TypeError, ValueError) as exc:
                            log.debug("chandelier_skipped",
                                      trade_id=str(trade["id"]),
                                      error=str(exc)[:120])

                    # --- Path D (cont. 53): MTF reversal ratchet ---
                    # When 1m AND 5m CandleNet dir1 both invert opposite to
                    # trade direction by > 0.10 (NEUTRAL_BAND + 0.05), the
                    # trend is dying on the lower TFs.
                    #
                    # cont. 65g — owner mandate: gate on the 10 %-capital
                    # activation floor (matching Paths A/B/C) and clamp the
                    # lock fraction by the shared `_lock_cap` ladder. Previous
                    # code fired on any positive peak with a hard-coded 0.95
                    # lock, which overrode Path A's 10 % pre-TP1 cap via the
                    # min/max merge and produced premature SL tightening on
                    # trades with peak << 10 % capital.
                    try:
                        if _ratchet_armed_10pct:
                            fc_1m_raw = r.get(f"{pair}:1m:candle_forecast")
                            fc_5m_raw = r.get(f"{pair}:5m:candle_forecast")
                            if fc_1m_raw and fc_5m_raw:
                                fc_1m_d = json.loads(fc_1m_raw)
                                fc_5m_d = json.loads(fc_5m_raw)
                                d1_1m = float(fc_1m_d.get("dir1", 0.5))
                                d1_5m = float(fc_5m_d.get("dir1", 0.5))
                                _REV_THRESH = 0.10
                                if direction == "long":
                                    _rev_1m = d1_1m < 0.5 - _REV_THRESH
                                    _rev_5m = d1_5m < 0.5 - _REV_THRESH
                                else:
                                    _rev_1m = d1_1m > 0.5 + _REV_THRESH
                                    _rev_5m = d1_5m > 0.5 + _REV_THRESH
                                if _rev_1m and _rev_5m:
                                    locked_pct_d = peak_profit_pct * min(0.95, _lock_cap)
                                    if direction == "long":
                                        _mtf_sl = round(entry * (1 + locked_pct_d), 8)
                                        if ratchet_sl is None or _mtf_sl > ratchet_sl:
                                            ratchet_sl = _mtf_sl
                                            try:
                                                r.incr("trail:mtf_reversal_tighten_count")
                                            except Exception:
                                                pass
                                    else:
                                        _mtf_sl = round(entry * (1 - locked_pct_d), 8)
                                        if ratchet_sl is None or _mtf_sl < ratchet_sl:
                                            ratchet_sl = _mtf_sl
                                            try:
                                                r.incr("trail:mtf_reversal_tighten_count")
                                            except Exception:
                                                pass
                    except Exception as exc:
                        log.debug("path_d_mtf_reversal_skipped",
                                  trade_id=str(trade.get("id")),
                                  error=str(exc)[:120])

                    # --- Path E (cont. 53): Exhaustion ratchet ---
                    # When 1m AND 5m exhaustion scores together signal that
                    # the move IN OUR FAVOUR is exhausting (combined ≥ 4.0),
                    # snap-tighten. Exhaustion direction must match trade
                    # direction.
                    #
                    # cont. 65g — owner mandate: gate on 10 %-capital
                    # activation floor and clamp the lock fraction by shared
                    # `_lock_cap`. Previous code fired on any positive peak
                    # with a hard-coded 0.90 lock, bypassing both the
                    # activation gate and the pre-TP1 10 % cap.
                    try:
                        if _ratchet_armed_10pct:
                            exh_1m_raw = r.get(f"{pair}:1m:exhaustion_score")
                            exh_5m_raw = r.get(f"{pair}:5m:exhaustion_score")
                            if exh_1m_raw and exh_5m_raw:
                                exh_1m_d = json.loads(exh_1m_raw)
                                exh_5m_d = json.loads(exh_5m_raw)
                                s_1m = float(exh_1m_d.get("score", 0.0))
                                s_5m = float(exh_5m_d.get("score", 0.0))
                                dir_1m = int(exh_1m_d.get("direction", 0))
                                dir_5m = int(exh_5m_d.get("direction", 0))
                                _favour = 1 if direction == "long" else -1
                                _same = (dir_1m == _favour and dir_5m == _favour)
                                if _same and (s_1m + s_5m) >= 4.0:
                                    locked_pct_e = peak_profit_pct * min(0.90, _lock_cap)
                                    if direction == "long":
                                        _exh_sl = round(entry * (1 + locked_pct_e), 8)
                                        if ratchet_sl is None or _exh_sl > ratchet_sl:
                                            ratchet_sl = _exh_sl
                                            try:
                                                r.incr("trail:exhaustion_tighten_count")
                                            except Exception:
                                                pass
                                    else:
                                        _exh_sl = round(entry * (1 - locked_pct_e), 8)
                                        if ratchet_sl is None or _exh_sl < ratchet_sl:
                                            ratchet_sl = _exh_sl
                                            try:
                                                r.incr("trail:exhaustion_tighten_count")
                                            except Exception:
                                                pass
                    except Exception as exc:
                        log.debug("path_e_exhaustion_skipped",
                                  trade_id=str(trade.get("id")),
                                  error=str(exc)[:120])

                    # cont. 60 — Apply frontier sl_floor / sl_ceiling constraints
                    # from conformal bands, Mamba quantiles, diffusion bands.
                    # These are statistical safety-bounds.
                    #
                    # cont. 62d (owner mandate 2026-05-29): gated by the
                    # 15 %-capital activation floor — same reasoning as
                    # Paths B/C. Below 15 % capital profit, the owner's
                    # 50 %-capital initial SL must persist; frontier
                    # stat-bands resume their bound-application role only
                    # AFTER the 15 % activation has armed the trailing
                    # ratchet. force_close handling earlier is unaffected
                    # — those are confirmed exit signals, not bounds.
                    _front_bound_armed = (
                        capital_leverage > 0
                        and peak_profit_pct >= (
                            _cap_act_frac / capital_leverage
                            if not _cap_act_off else 0.0))
                    if _front_dec is not None and _front_bound_armed:
                        if direction == "long" and _front_dec.sl_floor > 0:
                            # Long: SL cannot be lower than sl_floor.
                            if ratchet_sl is None or ratchet_sl < _front_dec.sl_floor:
                                ratchet_sl = _front_dec.sl_floor
                                try:
                                    r.incr("trail:frontier_floor_applied_count")
                                except Exception:
                                    pass
                        elif direction == "short" and _front_dec.sl_ceiling > 0:
                            # Short: SL cannot be higher than sl_ceiling.
                            if ratchet_sl is None or ratchet_sl > _front_dec.sl_ceiling:
                                ratchet_sl = _front_dec.sl_ceiling
                                try:
                                    r.incr("trail:frontier_ceiling_applied_count")
                                except Exception:
                                    pass

                    # cont. 64 debug — log gate decision for stuck-pattern trades
                    # (peak armed for Path B but standalone never fires).
                    try:
                        if peak_profit_pct >= (_cap_act_frac / max(capital_leverage, 1)):
                            log.info("trail_gate",
                                     trade_id=str(trade["id"])[:8],
                                     pair=pair, dir=direction,
                                     peak_pct=round(peak_profit_pct*100, 3),
                                     profit_pct=round(profit_pct*100, 3),
                                     act_pct=round(activation_pct*100, 3),
                                     ratchet_sl=(round(ratchet_sl, 6) if ratchet_sl else None),
                                     sl_level=round(sl_level, 6),
                                     outer_if=profit_pct >= activation_pct)
                    except Exception:
                        pass

                    if profit_pct >= activation_pct:
                        # Progressive tightening (continuous): mult shrinks with profit.
                        # At profit==activation: mult≈1.5 (wide initial lock)
                        # At profit==3×activation: mult≈0.87
                        # At profit==10×activation: mult≈0.47
                        profit_ratio = profit_pct / activation_pct
                        trail_mult = max(0.4, min(2.0, 1.5 / (profit_ratio ** 0.5)))
                        trail_dist_pct = trail_mult * vol_unit
                        # R4 — widen trail during low-confidence periods so
                        # noise doesn't stop us out before recovery (cont. 55).
                        # conf=1.0 → ×1.0; conf=0.5 → ×1.5; conf=0.2 → ×1.8.
                        try:
                            from metacognition.confidence import get_confidence
                            _conf = get_confidence()
                            if _conf < 1.0:
                                trail_dist_pct *= (2.0 - _conf)
                                try:
                                    r.incr("trail:confidence_widen_count")
                                except Exception:
                                    pass
                        except Exception:
                            pass

                        # Strategy-router static override: replaces the progressive formula.
                        if _sl_ov and "trailing_dist_pct" in _sl_ov:
                            try:
                                from strategy.router import record_routed_sl
                                trail_dist_pct = max(0.003, min(0.05, float(_sl_ov["trailing_dist_pct"])))
                                record_routed_sl(_sid, "trailing", trail_dist_pct)
                            except Exception as exc:
                                log.debug("sl_router_trailing_skipped", error=str(exc)[:120])

                        # Per-trade distance override (highest precedence — Brain's voice).
                        _dist_override = r.get(redis_keys.TRADE_TRAIL_DIST_OVERRIDE.replace(
                            "{trade_id}", trade_id_str))
                        if _dist_override is not None:
                            try:
                                trail_dist_pct = max(0.003, min(0.05, float(_dist_override)))
                                r.incr("trail:per_trade_dist_override_count")
                            except (TypeError, ValueError):
                                pass

                        trailing_dist = mark * trail_dist_pct

                        if direction == "long":
                            new_sl = round(mark - trailing_dist, 8)
                            # Ratchet floor: prefer locked-peak price when tighter.
                            if ratchet_sl is not None and ratchet_sl > new_sl:
                                new_sl = ratchet_sl
                                try:
                                    r.incr("trail:ratchet_applied_count")
                                except Exception:
                                    pass
                            if new_sl > sl_level:
                                engine.modify_sl(trade["id"], new_sl)
                        elif direction == "short":
                            new_sl = round(mark + trailing_dist, 8)
                            if ratchet_sl is not None and ratchet_sl < new_sl:
                                new_sl = ratchet_sl
                                try:
                                    r.incr("trail:ratchet_applied_count")
                                except Exception:
                                    pass
                            if new_sl < sl_level or sl_level == 0:
                                engine.modify_sl(trade["id"], new_sl)
                    elif ratchet_sl is not None:
                        # Trail doesn't update (current profit fell below
                        # activation), but the ratchet has a peak to defend.
                        # Apply it standalone. Monotonic guard in modify_sl
                        # ensures we never widen.
                        # cont. 64 debug — emit per-trade decision when peak
                        # ≥ activation but standalone ratchet skipped.
                        try:
                            if peak_profit_pct >= (_cap_act_frac / max(capital_leverage, 1)):
                                _decided = "apply" if (
                                    (direction == "long" and ratchet_sl > sl_level)
                                    or (direction == "short" and (ratchet_sl < sl_level or sl_level == 0))
                                ) else "skip_not_tighter"
                                log.info("ratchet_debug",
                                         trade_id=str(trade["id"])[:8],
                                         pair=pair, dir=direction,
                                         peak_pct=round(peak_profit_pct*100, 3),
                                         profit_pct=round(profit_pct*100, 3),
                                         ratchet_sl=round(ratchet_sl, 6),
                                         sl_level=round(sl_level, 6),
                                         decision=_decided)
                        except Exception:
                            pass
                        if direction == "long" and ratchet_sl > sl_level:
                            engine.modify_sl(trade["id"], ratchet_sl)
                            try:
                                r.incr("trail:ratchet_only_count")
                            except Exception:
                                pass
                        elif direction == "short" and (ratchet_sl < sl_level or sl_level == 0):
                            engine.modify_sl(trade["id"], ratchet_sl)
                            try:
                                r.incr("trail:ratchet_only_count")
                            except Exception:
                                pass

                # F44 Directional Hedge: evaluate trigger AND apply the +5%
                # breakeven lock for hedges in profit. Two cheap calls; each
                # short-circuits early when conditions aren't met.
                try:
                    from risk.hedge import check_and_open_hedge, maybe_lock_hedge_breakeven
                    check_and_open_hedge(trade, engine)
                    maybe_lock_hedge_breakeven(trade, engine)
                except Exception as exc:
                    log.warning("hedge_check_skipped",
                                trade_id=str(trade.get("id")),
                                error=str(exc)[:120])

                _t_elapsed_ms = (_time_mod_loop.monotonic() - _t_start) * 1000
                _per_trade_times.append((str(trade.get("id"))[:8], pair, _t_elapsed_ms))
            _loop_elapsed_s = _time_mod_loop.monotonic() - _loop_start
            try:
                _slow_top = sorted(_per_trade_times, key=lambda x: -x[2])[:5]
                log.info("sl_monitor_loop_timing",
                         total_s=round(_loop_elapsed_s, 2),
                         n_trades=len(trades),
                         avg_ms=round(1000 * _loop_elapsed_s / max(len(trades), 1), 1),
                         slowest=[{"id": t[0], "pair": t[1], "ms": round(t[2], 1)} for t in _slow_top])
            except Exception:
                pass
        except Exception as exc:
            log.error("sl_monitor_error", error=str(exc))

        await asyncio.sleep(1)


def check_dca_triggers(trade: dict, engine) -> None:
    """O-05/O-06: Trigger DCA round 1/2 from entry.

    Thresholds come from `strategy/router.get_dca_rules(strategy_id)` when the
    trade's chosen strategy has typed dca_rules; otherwise fall back to
    `config.capital.dca_trigger_{1,2}_pct`. Per-strategy override added
    2026-05-21 (cont. 8) so the F8 selector's picks actually change behaviour."""
    # Kill switch (cont. 43, 2026-05-24): DCA empirically destroys capital
    # (123 DCA trades = 1.6% win rate, -$2364.61 total vs 2135 no-DCA = 49.8%, +$4317).
    # Setting dca_rounds_max=0 in config disables all DCA triggers globally.
    if config.capital.dca_rounds_max <= 0:
        return
    r = redis_client.get()
    mark = float(r.get(redis_keys.MARK_PRICE.replace("{pair}", trade["pair"])) or 0)
    entry = float(trade["entry_price"])
    direction = trade["direction"]
    raw_dca = trade.get("dca_status")
    dca_status = raw_dca if isinstance(raw_dca, dict) else (json.loads(raw_dca) if raw_dca else {})

    # Per-strategy thresholds when the router has them, else config defaults.
    dca1_pct = config.capital.dca_trigger_1_pct
    dca2_pct = config.capital.dca_trigger_2_pct
    routed_rules = None
    try:
        from strategy.router import get_dca_rules
        routed_rules = get_dca_rules(trade.get("strategy_id"))
        if routed_rules:
            # cont. 33 defense-in-depth: reject routed values tighter than the
            # validator floor. 113 DCA-1-only trades closed 0-for-113 because
            # retired strategies in DB had round_1_pct as tight as -0.05%.
            # Anything tighter than -8 / -12 is treated as broken; fall back
            # to config defaults. Round 2 must also be strictly deeper than 1.
            r1 = float(routed_rules["round_1_pct"])
            r2 = float(routed_rules["round_2_pct"])
            if r1 <= -8.0 and r2 <= -12.0 and r2 < r1:
                dca1_pct = r1
                dca2_pct = r2
            else:
                routed_rules = None  # disable record_routed_dca for this trade
                log.warning("dca_router_rules_rejected",
                            strategy_id=trade.get("strategy_id"),
                            r1=r1, r2=r2,
                            reason="below_floor_or_inverted")
    except Exception as exc:
        log.debug("dca_router_lookup_failed", error=str(exc)[:120])

    # pct_move is always negative when the trade is losing:
    #   long:  pct_move = (mark - entry) / entry  — negative when mark < entry
    #   short: pct_move = (entry - mark) / entry  — negative when mark > entry
    # Config thresholds are -20 and -40 (already negative), so NO abs() needed.
    dca1 = dca1_pct / 100
    dca2 = dca2_pct / 100

    if direction == "long":
        pct_move = (mark - entry) / entry
    else:
        pct_move = (entry - mark) / entry

    # cont. 36: recovery-signal gate.
    # Pre-cont.36, DCA fired the instant pct_move crossed the threshold, which
    # meant adding capital INTO an in-progress crash (the down-leg may not be
    # finished). Now we ALSO require evidence the move has paused: the last
    # 15-min return is favourable AND price is at least 1.5% off the recent
    # adverse extreme. Both conditions must hold.
    if not dca_status.get("round_1_triggered") and pct_move <= dca1:
        if not _dca_recovery_ok(trade["pair"], direction, mark, r):
            return  # depth reached but down-move not yet stalled — wait
        engine.add_dca(trade["id"], round_number=1)
        if routed_rules:
            try:
                from strategy.router import record_routed_dca
                record_routed_dca(trade.get("strategy_id"), 1, routed_rules)
            except Exception:
                pass
        _maybe_move_to_breakeven(trade, engine, round_completed=1)
    elif not dca_status.get("round_2_triggered") and pct_move <= dca2:
        if not _dca_recovery_ok(trade["pair"], direction, mark, r):
            return
        engine.add_dca(trade["id"], round_number=2)
        if routed_rules:
            try:
                from strategy.router import record_routed_dca
                record_routed_dca(trade.get("strategy_id"), 2, routed_rules)
            except Exception:
                pass
        _maybe_move_to_breakeven(trade, engine, round_completed=2)


def _dca_recovery_ok(pair: str, direction: str, mark: float, r) -> bool:
    """cont. 36 — DCA recovery-signal gate. Returns True iff the adverse
    move has paused enough to make adding capital sensible (not catching a
    falling knife). Two conditions, BOTH required:

      A. Last 15-min return is in our favour:
           long  → mark > price_15m_ago
           short → mark < price_15m_ago
      B. Price is ≥ 1.5% off the 30-min adverse extreme:
           long  → mark > recent_low  × 1.015
           short → mark < recent_high × 0.985

    Data source: `{pair}:mark_window` list maintained by data/feed.py at
    every 5s poll (lpush + ltrim 0..359). Index 0 = newest. 15 min ≈ 180
    samples; 30 min ≈ 360 samples.

    Fails OPEN (returns True) when the window is too short — needed so the
    bot doesn't permanently block DCA on a freshly-restarted system that
    hasn't accumulated 15 min of history yet. Logs the bypass."""
    try:
        window = r.lrange(f"{pair}:mark_window", 0, -1)
    except Exception:
        log.warning("dca_recovery_window_read_failed", pair=pair)
        return True
    if len(window) < 180:
        log.info("dca_recovery_bypassed_short_history",
                 pair=pair, samples=len(window))
        return True

    try:
        prices = [float(p) for p in window]
    except (TypeError, ValueError):
        log.warning("dca_recovery_window_parse_failed", pair=pair)
        return True

    # Index 0 is the freshest (lpush). 15 min ago ≈ index 180.
    price_15m_ago = prices[min(180, len(prices) - 1)]
    # 30-min window for the adverse extreme.
    window_30m = prices[:min(360, len(prices))]

    if direction == "long":
        recent_low = min(window_30m)
        return_ok = mark > price_15m_ago
        floor_ok  = mark > recent_low * 1.015
    else:
        recent_high = max(window_30m)
        return_ok = mark < price_15m_ago
        floor_ok  = mark < recent_high * 0.985

    if return_ok and floor_ok:
        return True

    log.info("dca_recovery_gate_blocked",
             pair=pair, direction=direction, mark=mark,
             ref_15m=price_15m_ago,
             return_ok=return_ok, floor_ok=floor_ok)
    try:
        r.incr("dca:recovery_gate_blocked_count")
    except Exception:
        pass
    return False


def _maybe_move_to_breakeven(trade: dict, engine, round_completed: int) -> None:
    """O-07: Seed check at DCA time — real continuous check is in monitor_trailing_sl."""
    r = redis_client.get()
    mark = float(r.get(redis_keys.MARK_PRICE.replace("{pair}", trade["pair"])) or 0)
    entry = float(trade["entry_price"])
    avg_entry = float(trade.get("average_entry") or entry)
    direction = trade["direction"]
    sl_level = float(trade.get("trailing_sl_level") or 0)

    if direction == "long" and mark >= entry * 0.90 and sl_level < avg_entry:
        engine.modify_sl(trade["id"], avg_entry)
    elif direction == "short" and mark <= entry * 1.10 and sl_level > avg_entry:
        engine.modify_sl(trade["id"], avg_entry)


def compute_kelly_capital(balance_usdt: float,
                          min_trades: int = 30,
                          window_n: int = 50,
                          fractional: float = 0.5) -> dict:
    """F16 (Blueprint Feature 16): Fractional Kelly Criterion for position sizing.

    Computes the Kelly-optimal capital allocation from recent trade history.

    Formula: f* = W − (1 − W) / R
        where W = win rate (decimal)
              R = avg_win / abs(avg_loss)

    Then applies fractional Kelly (default 0.5 = half-Kelly) which retains
    ~75% of compound growth at half the variance — derived from Taylor
    expansion of log-growth: G(f) ≈ f·μ − f²·σ²/2.

    Result is clamped to config.capital [per_trade_min_pct, per_trade_max_pct]
    (default 5%-30% of balance) per blueprint hard band. Kelly informs;
    the band enforces.

    Returns dict {
        kelly_capital_usdt: float | None,   # None when insufficient data
        kelly_fraction: float,              # raw f* before clamping
        win_rate: float,                    # 0-1
        payoff_ratio: float,                # avg_win / abs(avg_loss)
        n_trades: int,
        active: bool                        # False when data < min_trades
    }

    Reference: QuantPedia — Beware of Excessive Leverage (Kelly).
    """
    from db import db_conn

    if balance_usdt <= 0:
        return {"kelly_capital_usdt": None, "kelly_fraction": 0.0,
                "win_rate": 0.0, "payoff_ratio": 0.0,
                "n_trades": 0, "active": False}

    rows = []
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT net_pnl_usdt FROM trades "
                "WHERE status = 'closed' AND net_pnl_usdt IS NOT NULL "
                "ORDER BY exit_time DESC LIMIT %s",
                (window_n,),
            )
            rows = [float(r[0]) for r in cur.fetchall() if r[0] is not None]
    except Exception as exc:
        log.warning("kelly_db_query_failed", error=str(exc)[:120])
        return {"kelly_capital_usdt": None, "kelly_fraction": 0.0,
                "win_rate": 0.0, "payoff_ratio": 0.0,
                "n_trades": 0, "active": False}

    n = len(rows)
    if n < min_trades:
        return {"kelly_capital_usdt": None, "kelly_fraction": 0.0,
                "win_rate": 0.0, "payoff_ratio": 0.0,
                "n_trades": n, "active": False}

    wins   = [p for p in rows if p > 0]
    losses = [p for p in rows if p < 0]
    if not wins or not losses:
        # All wins or all losses — Kelly formula is undefined; defer to bands.
        return {"kelly_capital_usdt": None, "kelly_fraction": 0.0,
                "win_rate": (len(wins) / n) if n else 0.0,
                "payoff_ratio": 0.0,
                "n_trades": n, "active": False}

    w_rate   = len(wins) / n
    avg_win  = sum(wins) / len(wins)
    avg_loss = abs(sum(losses) / len(losses))
    if avg_loss <= 1e-9:
        return {"kelly_capital_usdt": None, "kelly_fraction": 0.0,
                "win_rate": w_rate, "payoff_ratio": 0.0,
                "n_trades": n, "active": False}

    payoff = avg_win / avg_loss
    f_star = w_rate - (1.0 - w_rate) / payoff
    f_frac = max(0.0, f_star * fractional)   # negative Kelly → 0 (don't trade)

    # F56 (cont. 56) — Conformal-uncertainty size multiplier.
    # Reads `conformal:width:{model}` for each warmed-up forecaster and
    # scales Kelly by the geometric-mean of (1 - width). Cold-start safe —
    # returns 1.0 (no scaling) when no model has enough calibration data.
    # Per memory feedback_silent_rejection: any size reduction is observable
    # via `conformal:health` Redis key, not a silent multiplier.
    conformal_mult = 1.0
    try:
        from ml.conformal_wrapper import kelly_multiplier as _cf_mult
        conformal_mult = float(_cf_mult())
    except Exception as _exc:
        log.debug("conformal_mult_unavailable", err=str(_exc)[:120])
    f_frac_scaled = f_frac * conformal_mult

    # Clamp to blueprint hard band (5%–30% of balance).
    min_pct = float(config.capital.per_trade_min_pct) / 100.0
    max_pct = float(config.capital.per_trade_max_pct) / 100.0
    pct_used = max(min_pct, min(max_pct, f_frac_scaled))
    kelly_cap = round(balance_usdt * pct_used, 2)

    return {
        "kelly_capital_usdt": kelly_cap,
        "kelly_fraction":     round(f_frac, 4),
        "kelly_fraction_scaled": round(f_frac_scaled, 4),
        "conformal_multiplier": round(conformal_mult, 4),
        "win_rate":           round(w_rate, 4),
        "payoff_ratio":       round(payoff, 4),
        "n_trades":           n,
        "active":             True,
    }


def check_position_sizing(capital_pct: float, total_deployed_pct: float) -> tuple[bool, str]:
    """O-08: Verify position sizing constraints before opening a trade."""
    min_pct = config.capital.per_trade_min_pct
    max_pct = config.capital.per_trade_max_pct
    max_total = config.trading.max_total_capital_pct

    if capital_pct < min_pct:
        return False, f"capital_pct {capital_pct}% below minimum {min_pct}%"
    if capital_pct > max_pct:
        return False, f"capital_pct {capital_pct}% above maximum {max_pct}%"
    if total_deployed_pct + capital_pct > max_total:
        return False, f"would exceed max total capital {max_total}%"
    return True, "ok"


def assign_leverage(potential_score: float, volatility: float) -> int:
    """O-09: Map trade potential score to leverage (5x–20x hard cap).

    cont. 23: drawdown-adaptive throttle. Blueprint Section 4.1 Feature 7
    requires leverage to be data-driven; closed-trade evidence shows
    leverage 10+ trades net-lose (-$130 over 51 trades) while leverage 5
    trades net-win (+$552 over 1853 trades). When account equity drops far
    below the starting capital — i.e. the bot is in drawdown and the high-
    leverage path is the bleeding source — we cap leverage_max progressively.
    This is the same risk-reduction posture the blueprint mandates for high
    turbulence (Section 5: "High turbulence → Brain reduces all position
    sizes, tightens trailing SLs") generalised to equity drawdown.
    """
    r = redis_client.get()
    lev_min = config.capital.leverage_min
    lev_max = config.capital.leverage_max

    # PHASE 0 DE-RISK (professor audit 2026-06-04): make leverage Redis-tunable.
    # Previously leverage was config-baked (no Redis override existed) so a 20x
    # default could not be cut without an image rebuild. Now `risk:leverage_min` /
    # `risk:leverage_max` override the config when set. Reversible: delete the keys
    # to restore config defaults. See PROFESSOR_AUDIT.md F-024.
    try:
        _lm = r.get("risk:leverage_min")
        _lx = r.get("risk:leverage_max")
        if _lm:
            lev_min = int(float(_lm))
        if _lx:
            lev_max = int(float(_lx))
    except (TypeError, ValueError):
        pass

    # USER DASHBOARD LEVERAGE (bot:leverage) is AUTHORITATIVE when set: the owner's
    # explicit choice from the dashboard pins leverage to that FIXED value (clamped to
    # the 1-20 hard cap), overriding both the config default and the Phase-0 risk:* band.
    # The drawdown throttle below still applies on top (protective reduction in losses).
    # Previously assign_leverage never read bot:leverage, so the dashboard value was
    # silently ignored (auto score->leverage within the risk:* band). Fixes that.
    try:
        _ulev = r.get("bot:leverage")
        if _ulev:
            _uv = max(1, min(20, int(float(_ulev))))
            lev_min = _uv
            lev_max = _uv
    except (TypeError, ValueError):
        pass

    try:
        start_cap = float(r.get("bot:starting_capital_usdt") or 0)
        equity    = float(r.get("account:balance_usdt")
                          or r.get(redis_keys.VIRTUAL_BALANCE) or 0)
        if start_cap > 0 and equity > 0:
            drawdown = max(0.0, 1.0 - equity / start_cap)
            if drawdown >= 0.50:
                # Severe — force min leverage until recovery.
                lev_max = lev_min
            elif drawdown >= 0.30:
                # Heavy — cap at lev_min + 3.
                lev_max = min(lev_max, lev_min + 3)
            elif drawdown >= 0.15:
                # Moderate — cap halfway through the band.
                lev_max = min(lev_max, lev_min + (lev_max - lev_min) // 2)
    except Exception as exc:
        log.debug("assign_leverage_drawdown_check_failed", error=str(exc)[:120])

    # Margin-ratio safety: regardless of starting-capital math, if exchange
    # margin ratio is high we're already near liquidation — force min lev.
    try:
        margin_ratio = float(r.get("account:margin_ratio") or 0)
        if margin_ratio >= 0.60:
            lev_max = lev_min
    except Exception:
        pass

    # cont. 62e (owner mandate 2026-05-29): "make it 20x default; if I see
    # issues in future trades I will change it at margins by the confidence".
    # Default leverage = lev_max (20x). The drawdown safety + margin-ratio
    # safety blocks above still cap lev_max progressively when equity is at
    # risk, so this only takes effect during healthy operation. Legacy
    # potential-score interpolation + volatility penalty are kept behind the
    # Redis flag `risk:dynamic_leverage_enabled=1` for one-SET revert.
    if r.get("risk:dynamic_leverage_enabled") == "1":
        base = lev_min + (lev_max - lev_min) * (potential_score / 100)
        vol_penalty = min(volatility * 10, 5)
        leverage = max(lev_min, min(lev_max, int(base - vol_penalty)))
    else:
        leverage = lev_max
    return leverage


def check_turbulence_circuit_breaker() -> bool:
    """O-10: Return True (block new trades) if turbulence index exceeds threshold."""
    r = redis_client.get()
    turbulence = float(r.get(redis_keys.TURBULENCE_INDEX) or 0)
    threshold = 2.5
    if turbulence > threshold:
        log.warning("turbulence_circuit_breaker_active", turbulence=turbulence)
        return True
    return False
