"""cont. 60 — Signal-driven exit features.

  * CVD divergence (price HH / CVD LH for shorts, etc.) → tighten SL
  * Filtered OBI (structural — lifetime + update-count filter) → tighten SL
  * Funding/premium divergence (extreme funding + premium spike) → force exit
  * Hawkes self-excitation (λ(t) rising) → tighten SL
  * MM-Hawkes spoof / stop-hunt pre-detector → temporary SL widen
  * BOCPD changepoint posterior > 0.85 → force exit (kill switch)

Each function returns ExitDecision or None.
"""
from __future__ import annotations
from typing import Optional
import json
import time as _time_mod
import structlog

from .decision import ExitDecision
import redis_keys

log = structlog.get_logger()


# ─────────────────────────────────────────────────────────────────────────
# 1. CVD divergence
# ─────────────────────────────────────────────────────────────────────────
def evaluate_cvd_divergence(trade, mark, r, sl_level, direction) -> Optional[ExitDecision]:
    """Cumulative Volume Delta divergence vs price → trend-end signal.

    LONG bearish divergence:  price HH but CVD LH   → tighten SL ×0.6
    SHORT bullish divergence: price LL but CVD HL   → tighten SL ×0.6

    CVD is maintained by data/feed.py as rolling sum over last 50 candles
    (taker_buy_volume - taker_sell_volume). The divergence detector compares
    the last 3 swings (peaks for longs, troughs for shorts).

    Source: CofiaTrading CVD Divergence Guide, Bookmap (2025).
    """
    pair = trade["pair"]
    cvd_hist_raw = r.lrange(f"{pair}:cvd_history", 0, 99)
    price_hist_raw = r.lrange(f"{pair}:close_history", 0, 99)
    if not cvd_hist_raw or not price_hist_raw or len(cvd_hist_raw) < 50:
        return None
    cvd  = [float(x) for x in cvd_hist_raw]
    prices = [float(x) for x in price_hist_raw]
    n = min(len(cvd), len(prices))
    cvd, prices = cvd[:n], prices[:n]
    if n < 20:
        return None

    # Compare last 10 bars to the prior 10 bars.
    recent_cvd  = sum(cvd[:10])
    prior_cvd   = sum(cvd[10:20])
    recent_high = max(prices[:10])
    prior_high  = max(prices[10:20])
    recent_low  = min(prices[:10])
    prior_low   = min(prices[10:20])

    divergence = False
    if direction == "long":
        # Price higher high, CVD lower (cumulative buying weakening)
        if recent_high > prior_high * 1.001 and recent_cvd < prior_cvd * 0.85:
            divergence = True
    else:
        # Price lower low, CVD higher (cumulative selling weakening)
        if recent_low < prior_low * 0.999 and recent_cvd > prior_cvd * 0.85:
            # cvd > prior_cvd means LESS net selling pressure
            divergence = True

    if divergence:
        try:
            r.incr("trail:cvd_divergence_tighten_count")
        except Exception:
            pass
        return ExitDecision(tighten_sl_mult=0.6,
                            notes={"cvd_recent": recent_cvd, "cvd_prior": prior_cvd})
    return None


# ─────────────────────────────────────────────────────────────────────────
# 2. Filtered OBI structural exit
# ─────────────────────────────────────────────────────────────────────────
def evaluate_filtered_obi(trade, mark, r, sl_level, direction) -> Optional[ExitDecision]:
    """Filtered Order Book Imbalance flip → proactive exit signal.

    arXiv:2507.22712 strips spoofed orders by filtering on:
      * minimum order lifetime (no flash orders)
      * minimum update count (orders that get repeatedly modified are noise)
    The remaining filtered OBI is a tradable directional signal.

    For exits: if the filtered OBI flips opposite the trade direction for 3+
    consecutive snapshots, tighten SL ×0.7. If the flip is severe (>0.6
    against), force close — but ONLY if the trade is currently losing
    (cont. 69r gate; a green trade is downgraded to the ×0.7 tighten so the
    trailing ratchet manages it). Kill switch: risk:filtered_obi_force_exit_enabled.

    OBI is published by data/feed.py via Binance order book snapshots.
    Filtered OBI is maintained in `{pair}:filtered_obi_history`.
    """
    pair = trade["pair"]
    obi_hist_raw = r.lrange(f"{pair}:filtered_obi_history", 0, 4)
    if not obi_hist_raw or len(obi_hist_raw) < 3:
        return None
    obis = [float(x) for x in obi_hist_raw[:3]]

    # cont. 69r — gate the SEVERE-FLIP FORCE CLOSE exactly like Path F
    # mtf_15m_reversal was gated: only kill a CURRENTLY-LOSING trade.
    # Evidence (7d, 2026-05-26..06-02): filtered_obi_severe_flip force-closed
    # 185 trades for -$571, avg peak +$1.58 *when it closed them* — i.e. it
    # was booking losses on trades that were green moments earlier. A winning
    # trade no longer gets force-killed; it is downgraded to the ×0.7 tighten
    # so the (profitable) trailing ratchet manages the exit instead.
    #   Kill switch: risk:filtered_obi_force_exit_enabled (default "1").
    #   Skip telemetry: trail:filtered_obi_force_suppressed_count (silent-
    #   rejection rule — every suppressed force-exit is counted).
    _entry = float(trade.get("average_entry") or trade.get("entry_price") or 0)
    _sign = 1.0 if direction == "long" else -1.0
    _currently_losing = _entry > 0 and (mark - _entry) * _sign < 0
    try:
        _force_enabled = (r.get("risk:filtered_obi_force_exit_enabled") or "1") != "0"
    except Exception:
        _force_enabled = True

    def _severe_decision() -> ExitDecision:
        """Severe flip: force-close only when enabled AND the trade is
        currently in the red; otherwise downgrade to the ×0.7 tighten."""
        if _force_enabled and _currently_losing:
            try:
                r.incr("trail:filtered_obi_force_exit_count")
            except Exception:
                pass
            return ExitDecision(force_close=True,
                                reason="filtered_obi_severe_flip",
                                notes={"obis": obis})
        try:
            r.incr("trail:filtered_obi_force_suppressed_count")
        except Exception:
            pass
        return ExitDecision(tighten_sl_mult=0.7, notes={"obis": obis})

    # OBI sign convention: positive = bid-heavy (bullish), negative = ask-heavy (bearish)
    if direction == "long":
        # All recent OBI snapshots strongly negative → bid wall fading
        if all(o < -0.20 for o in obis):
            try:
                r.incr("trail:filtered_obi_tighten_count")
            except Exception:
                pass
            if all(o < -0.60 for o in obis):
                return _severe_decision()
            return ExitDecision(tighten_sl_mult=0.7,
                                notes={"obis": obis})
    else:
        # Short: all recent OBI snapshots strongly positive → ask wall fading
        if all(o > 0.20 for o in obis):
            try:
                r.incr("trail:filtered_obi_tighten_count")
            except Exception:
                pass
            if all(o > 0.60 for o in obis):
                return _severe_decision()
            return ExitDecision(tighten_sl_mult=0.7,
                                notes={"obis": obis})
    return None


# ─────────────────────────────────────────────────────────────────────────
# 3. Funding / premium divergence exit
# ─────────────────────────────────────────────────────────────────────────
def evaluate_funding_premium(trade, mark, r, sl_level, direction) -> Optional[ExitDecision]:
    """Extreme funding + premium index spike → near-term reversal zone.

    Q3 2025 Bitmex research: when perp funding rate enters top decile AND
    premium index (mark vs spot) spikes >0.3%, the market is crowded on the
    same side — sharp reversal historically follows in 30-60min.

    For LONG: funding rate > +0.05% (per 8h) AND premium > +0.30% → force exit
    For SHORT: funding rate < -0.05% (per 8h) AND premium < -0.30% → force exit
    """
    pair = trade["pair"]
    funding_raw = r.get(f"{pair}:funding_rate")
    premium_raw = r.get(f"{pair}:premium_index")
    if funding_raw is None or premium_raw is None:
        return None
    try:
        funding = float(funding_raw)
        premium = float(premium_raw)
    except (TypeError, ValueError):
        return None

    crowded_long  = funding >  0.0005 and premium >  0.0030
    crowded_short = funding < -0.0005 and premium < -0.0030

    if direction == "long" and crowded_long:
        try:
            r.incr("trail:funding_premium_force_exit_count")
        except Exception:
            pass
        return ExitDecision(force_close=True,
                            reason="funding_premium_crowded_long",
                            notes={"funding": funding, "premium": premium})
    if direction == "short" and crowded_short:
        try:
            r.incr("trail:funding_premium_force_exit_count")
        except Exception:
            pass
        return ExitDecision(force_close=True,
                            reason="funding_premium_crowded_short",
                            notes={"funding": funding, "premium": premium})

    # Lighter version: just funding extreme (no premium spike) → tighten SL
    if direction == "long" and funding > 0.0003:
        try:
            r.incr("trail:funding_tighten_count")
        except Exception:
            pass
        return ExitDecision(tighten_sl_mult=0.75,
                            notes={"funding": funding})
    if direction == "short" and funding < -0.0003:
        try:
            r.incr("trail:funding_tighten_count")
        except Exception:
            pass
        return ExitDecision(tighten_sl_mult=0.75,
                            notes={"funding": funding})
    return None


# ─────────────────────────────────────────────────────────────────────────
# 4. Hawkes self-excitation intensity
# ─────────────────────────────────────────────────────────────────────────
def evaluate_hawkes(trade, mark, r, sl_level, direction) -> Optional[ExitDecision]:
    """Hawkes process intensity λ(t) reflects clustered-event arrival rate.

    When λ(t) rises sharply, the market is in an "excited" state — cascading
    liquidations or directional bursts are more likely. Tighten the trail.

    Implementation: λ(t) is computed by data/feed.py from large-trade arrival
    times (trades > 0.5% of 24h volume). Decay constant β = 0.1, baseline λ_0 = 0.5.

    Published key: `{pair}:hawkes_intensity` (float, updated every candle close).
    Rolling baseline: `{pair}:hawkes_intensity_baseline` (1h avg).

    Source: Hawkes Processes HFT arXiv:2503.14814.
    """
    pair = trade["pair"]
    lam_raw  = r.get(f"{pair}:hawkes_intensity")
    base_raw = r.get(f"{pair}:hawkes_intensity_baseline")
    if lam_raw is None or base_raw is None:
        return None
    try:
        lam, base = float(lam_raw), float(base_raw)
    except (TypeError, ValueError):
        return None
    if base <= 0:
        return None
    excitation_ratio = lam / base
    if excitation_ratio < 1.5:
        return None  # quiet
    # Continuous tightening: ratio 1.5 → mult 0.85, ratio 3.0 → mult 0.45
    tighten = max(0.40, min(1.0, 1.05 - 0.20 * (excitation_ratio - 1.0)))
    try:
        r.incr("trail:hawkes_tighten_count")
        r.set("trail:hawkes_last_ratio", round(excitation_ratio, 3))
    except Exception:
        pass
    return ExitDecision(tighten_sl_mult=tighten,
                        notes={"hawkes_lambda": lam, "baseline": base,
                               "ratio": round(excitation_ratio, 3)})


# ─────────────────────────────────────────────────────────────────────────
# 5. Markov-Modulated Hawkes spoof / stop-hunt pre-detector
# ─────────────────────────────────────────────────────────────────────────
def evaluate_mm_hawkes_spoof(trade, mark, r, sl_level, direction) -> Optional[ExitDecision]:
    """Detect spoof clusters / stop-hunts via burst-of-cancellations features.

    When the detector fires NEAR the current SL level (within 1.5% of SL),
    temporarily WIDEN the SL by ×1.5 for 5 minutes so we ride out the hunt
    instead of getting stopped at the manipulated level.

    Implementation: data/feed.py maintains a rolling "cancel burst score"
    per pair, derived from L2 order book diff events. Score > 0.8 = active
    spoofing detected. Source: arXiv:2502.04027 (Feb 2025).

    Published key: `{pair}:cancel_burst_score` (0-1 float).
    """
    pair = trade["pair"]
    burst_raw = r.get(f"{pair}:cancel_burst_score")
    if burst_raw is None:
        return None
    try:
        burst = float(burst_raw)
    except (TypeError, ValueError):
        return None
    if burst < 0.8:
        return None
    # Spoofing only matters if SL is close to current price (hunt-zone proximity).
    if sl_level <= 0 or mark <= 0:
        return None
    sl_proximity = abs(mark - sl_level) / mark
    if sl_proximity > 0.015:  # SL > 1.5% away — hunt unlikely to reach it
        return None
    # WIDEN the SL temporarily. We set loosen_sl_mult > 1.0 which the
    # orchestrator translates into a wider Chandelier distance.
    try:
        r.incr("trail:mm_hawkes_widen_count")
    except Exception:
        pass
    return ExitDecision(loosen_sl_mult=1.5,
                        notes={"cancel_burst_score": burst,
                               "sl_proximity_pct": round(sl_proximity * 100, 3)})


# ─────────────────────────────────────────────────────────────────────────
# 6. BOCPD changepoint kill switch (per-pair)
# ─────────────────────────────────────────────────────────────────────────
def evaluate_bocpd_killswitch(trade, mark, r, sl_level, direction) -> Optional[ExitDecision]:
    """Bayesian Online Change-Point Detection per-pair posterior triggers
    immediate force-close on genuine regime shifts.

    The existing ml.bocpd.update() writes `{pair}:bocpd_posterior` per candle.
    When the posterior crosses the threshold, the underlying return distribution
    has likely shifted — exit the position before the new regime takes over.

    cont. 61 audit fix: threshold lowered from 0.85 → 0.05.
    Audit 2026-05-29 found posterior values cluster at 0.005 (hazard=1/250)
    so 0.85 was unreachable; 95 detections fired ml.bocpd globally but the
    per-pair kill switch fired zero times. 0.05 is 10× background which
    represents a genuine shock without overreaction. Configurable via
    redis key `risk:bocpd_kill_threshold` (default 0.05).

    Source: ACM 2025 BOCPD Financial TS paper.
    """
    pair = trade["pair"]
    post_raw = r.get(f"{pair}:bocpd_posterior")
    if post_raw is None:
        return None
    try:
        posterior = float(post_raw)
    except (TypeError, ValueError):
        return None
    try:
        threshold = float(r.get("risk:bocpd_kill_threshold") or 0.05)
    except (TypeError, ValueError):
        threshold = 0.05
    if posterior < threshold:
        return None
    try:
        r.incr("trail:bocpd_killswitch_count")
        r.set("trail:bocpd_last_posterior", round(posterior, 4))
        r.set("trail:bocpd_last_threshold", threshold)
        r.set("trail:bocpd_last_pair", pair)
    except Exception:
        pass
    return ExitDecision(force_close=True,
                        reason="bocpd_changepoint_detected",
                        notes={"posterior": posterior, "threshold": threshold})
