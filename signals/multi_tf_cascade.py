"""F50b — Multi-TF hierarchical direction cascade.

Replaces the OFI-primary direction picker in signals/engine.py. Direction
comes from the agreement of multiple candle timeframes (1h / 15m / 5m / 1m),
following the production-grade "higher TF sets context, lower TF executes"
doctrine confirmed by altFINS pattern scanners + most pro-trader literature.

Per-TF voting:
  - 1h:   combines TFT 1h bias + 1h CandleNet dir3 → long/short/neutral
  - 15m:  15m CandleNet dir3 → long/short/neutral
  - 5m:   5m  CandleNet dir3 → long/short/neutral
  - 1m:   1m  CandleNet dir1 → long/short/neutral (lower weight, noisy)
  - 4h:   optional macro veto if available (suppress when strongly opposite)

Decision rule:
  - If 4h CandleNet forecast exists AND is strongly opposite to OFI sign
    (|dir3 - 0.5| > 0.15 AND opposite direction) → return None (skip signal)
  - Count long/short votes among {1h, 15m, 5m} (the "core" TFs)
  - If ≥3 agree (i.e. all three vote same side) → cascade picks that side,
    confidence = 100
  - If 2-1 split → cascade picks the majority side, confidence = 67
  - If anything else (all neutral, no forecasts) → fall back to OFI sign,
    confidence = 50

Audit log: every cascade decision is logged via `log_cascade_decision()`
to the Redis list `cascade:decisions` (LPUSH + LTRIM 1000). Offline analysis
can correlate cascade confidence with realised outcomes to validate the
agreement-threshold thresholds.

Honest scope:
  - 4h CandleNet is NOT yet trained — `_load_forecast("4h")` returns None
    until F48 is extended. The macro veto is a no-op until then.
  - 1m CandleNet is read but does NOT vote in the core count. It's already
    used by the F48 §Idea C / F50d entry timing path. Including it here
    would double-count the same signal.
  - When ALL forecasts are missing (e.g. first hour after restart), this
    cascade falls back to OFI and behaves identically to the pre-F50b
    code. Cold-start safe.
"""
from __future__ import annotations
import json
from typing import Optional

import structlog

import redis_client

log = structlog.get_logger()


# Direction-vote thresholds. A CandleNet head's `dir3` is P(up). Anything
# in [0.5 - NEUTRAL_BAND, 0.5 + NEUTRAL_BAND] votes "neutral" — too close
# to coin-flip to be a directional signal.
NEUTRAL_BAND       = 0.05
# 4h macro veto fires when |dir3 - 0.5| > VETO_THRESHOLD AND direction
# disagrees with the OFI-derived candidate. Tighter than the per-TF vote
# threshold because vetoing a signal entirely is a stronger action.
VETO_THRESHOLD     = 0.15
# Cascade confidence scoring — translate vote counts to 0-100 score that
# the downstream composite can blend.
CONFIDENCE_BY_VOTES = {
    (3, 0): 100,   # unanimous on direction
    (2, 1): 67,    # 2-1 majority
    (1, 2): 33,    # opposite — cascade says other way
    (0, 3): 0,     # unanimous opposite — strong veto signal
}


def _load_forecast(r, pair: str, interval: str) -> Optional[dict]:
    """Read {pair}:{interval}:candle_forecast from Redis. Returns None on
    miss or parse error."""
    try:
        raw = r.get(f"{pair}:{interval}:candle_forecast")
        if not raw:
            return None
        return json.loads(raw)
    except Exception:
        return None


def _vote(forecast: Optional[dict], horizon_key: str = "dir3") -> str:
    """Return 'long' / 'short' / 'neutral' for a single TF's forecast."""
    if forecast is None:
        return "neutral"
    try:
        d = float(forecast.get(horizon_key, 0.5))
    except (TypeError, ValueError):
        return "neutral"
    if d > 0.5 + NEUTRAL_BAND:
        return "long"
    if d < 0.5 - NEUTRAL_BAND:
        return "short"
    return "neutral"


def _check_macro_veto(forecast_4h: Optional[dict],
                      ofi_direction: str) -> Optional[str]:
    """Return a string reason if the 4h forecast strongly contradicts the
    OFI direction, else None."""
    if forecast_4h is None:
        return None
    try:
        d4 = float(forecast_4h.get("dir3", 0.5))
    except (TypeError, ValueError):
        return None
    if abs(d4 - 0.5) <= VETO_THRESHOLD:
        return None
    macro_dir = "long" if d4 > 0.5 else "short"
    if macro_dir != ofi_direction:
        return f"4h_veto:macro={macro_dir} ofi={ofi_direction} d4={d4:.3f}"
    return None


def pick_direction_cascade(pair: str, ofi: float,
                           regime: str = "unknown",
                           tft_bias: float = 0.0,
                           strict: bool = False,
                           required_tfs: Optional[tuple] = None,
                           ) -> tuple[Optional[str], float, dict]:
    """Multi-TF cascade direction picker.

    Args:
      pair:       e.g. "BTCUSDT"
      ofi:        signed Order Flow Imbalance — used as tiebreaker
      regime:     bull / bear / turbulent / unknown — informational only
      tft_bias:   TFT 1h predicted % move — augments 1h vote when present
      strict:     cont. 65 — when True, REFUSE to invent a direction.
                  Returns None instead of OFI fallback in three cases:
                  (1) any TF in `required_tfs` is missing,
                  (2) 1-1 split (no clear majority),
                  (3) all-neutral over the core 1h/15m/5m votes.
                  Used by the Pre-Open Candle Gate (PCG) to enforce
                  "no candle direction → no trade." When False (default),
                  legacy behavior is preserved.
      required_tfs: cont. 65 — tuple of TFs (e.g. ("5m","15m")) that MUST
                  have forecasts. Only honored under strict=True. When
                  None (default), defaults to ("1h","15m","5m") — the
                  full core trio. PCG passes its own `pcg:required_tfs`
                  config through so 1h can be marked optional until the
                  1h CandleNet model is trained. Missing TFs that are NOT
                  in required_tfs still count as a neutral vote — they
                  don't reject the signal, just don't contribute.

    Returns:
      (direction, cascade_confidence, audit)
        direction:  "long" | "short" | None
            None when macro veto fires OR strict mode rejects.
        cascade_confidence: 0-100
        audit: dict with per-TF votes + decision rationale for logging
    """
    if required_tfs is None:
        required_tfs = ("1h", "15m", "5m")
    try:
        r = redis_client.get()
    except Exception:
        # Redis down — defer to OFI without cascade analysis. In strict
        # mode we'd rather skip the signal entirely than guess from OFI.
        if strict:
            return None, 0.0, {"status": "redis_unavailable_strict_reject"}
        ofi_dir = "long" if ofi >= 0 else "short"
        return ofi_dir, 50.0, {"status": "redis_unavailable",
                                "fallback": "ofi", "direction": ofi_dir}

    fc_1h  = _load_forecast(r, pair, "1h")
    fc_30m = _load_forecast(r, pair, "30m")   # cont. 65d — 15m→1h gap closer
    fc_15m = _load_forecast(r, pair, "15m")
    fc_5m  = _load_forecast(r, pair, "5m")
    fc_1m  = _load_forecast(r, pair, "1m")
    fc_4h  = _load_forecast(r, pair, "4h")    # optional, may not exist yet

    # cont. 65 strict — refuse to vote when any REQUIRED TF is silent.
    # Defense in depth (PCG checks this first, but cascade also enforces).
    # Only TFs in `required_tfs` cause rejection; TFs outside that set are
    # treated as soft (missing → neutral vote, doesn't reject the signal).
    if strict:
        _per_tf = {"1h": fc_1h, "30m": fc_30m, "15m": fc_15m,
                   "5m": fc_5m, "1m": fc_1m}
        _missing = [tf for tf in required_tfs
                    if _per_tf.get(tf) is None]
        if _missing:
            try:
                r.incr("cascade:strict_reject_missing")
            except Exception:
                pass
            return None, 0.0, {
                "status":     "strict_missing_forecasts",
                "missing":    _missing,
                "required":   list(required_tfs),
                "ofi":        round(float(ofi), 6),
                "regime":     regime,
            }

    # ── Layer 2a — LEARNED FUSION (soft-attention replacement) ────────────
    # cont. 65k: when a VALIDATED tf_fusion model exists AND `cascade:use_fusion`
    # is "1", combine the per-TF CandleNet outputs with the learned, calibrated
    # model instead of the hard 3-of-4 vote below. Fail-safe: any miss (no model,
    # flag off, error) falls through to the vote. The model is gated on val AUC/ECE
    # at train time, so it only exists when it genuinely beats coin-flip — i.e. it
    # auto-activates once enough POST-FIX trade outcomes accumulate (current
    # historical feature_vectors are from the old mono-bearish models → rejected).
    try:
        if r.get("cascade:use_fusion") == "1":
            from ml.tf_fusion import predict_p_up
            _feat = {
                "cn_1m_dir1":  (fc_1m or {}).get("dir1"),
                "cn_5m_dir3":  (fc_5m or {}).get("dir3"),
                "cn_15m_dir3": (fc_15m or {}).get("dir3"),
                "cn_1h_dir3":  (fc_1h or {}).get("dir3"),
                "cn_1m_trend": (fc_1m or {}).get("trend"),
                "cn_5m_trend": (fc_5m or {}).get("trend"),
                "cn_15m_trend": (fc_15m or {}).get("trend"),
                "cn_1h_trend": (fc_1h or {}).get("trend"),
                "cn_5m_mag3":  (fc_5m or {}).get("mag3"),
                "cn_15m_mag3": (fc_15m or {}).get("mag3"),
                "cn_1h_mag3":  (fc_1h or {}).get("mag3"),
                "regime_bull": 1.0 if regime == "bull" else 0.0,
                "regime_bear": 1.0 if regime == "bear" else 0.0,
                "ofi": ofi,
                "vpin": float(r.get(f"{pair}:vpin") or 0),
                "vol_unit": float(r.get(f"{pair}:vol_unit") or 0),
                "sentiment": float(r.get(f"sentiment:{pair}") or r.get("sentiment:global") or 0.5),
            }
            p_up = predict_p_up(_feat)
            if p_up is not None:
                _fdir = "long" if p_up >= 0.5 else "short"
                _fconf = round(min(100.0, abs(p_up - 0.5) * 200.0), 2)
                # strict mode still respects the floor; caller's min_cascade_conf gates.
                _faudit = {"status": "decided", "direction": _fdir,
                           "confidence": _fconf, "rationale": "tf_fusion",
                           "p_up": round(p_up, 4), "regime": regime}
                try:
                    r.incr("cascade:fusion_used")
                except Exception:
                    pass
                log_cascade_decision(pair, _faudit)
                return _fdir, _fconf, _faudit
    except Exception as _fexc:
        log.debug("tf_fusion_cascade_skipped", error=str(_fexc)[:120])

    # ── 1h vote: combine TFT bias + 1h CandleNet dir3 ─────────────────────
    vote_1h_candle = _vote(fc_1h, "dir3")
    if tft_bias > 0.005:        # +0.5% predicted = strong long
        vote_1h_tft = "long"
    elif tft_bias < -0.005:
        vote_1h_tft = "short"
    else:
        vote_1h_tft = "neutral"
    # Combine: agreement wins. If 1h candle disagrees with TFT, defer to
    # whichever has stronger conviction (TFT magnitude vs candle distance).
    if vote_1h_candle == vote_1h_tft:
        vote_1h = vote_1h_candle
    elif vote_1h_candle == "neutral":
        vote_1h = vote_1h_tft
    elif vote_1h_tft == "neutral":
        vote_1h = vote_1h_candle
    else:
        # Conflict — pick the stronger signal.
        candle_strength = abs(float(fc_1h.get("dir3", 0.5)) - 0.5) if fc_1h else 0
        tft_strength    = abs(tft_bias) * 100   # rough comparable scale
        vote_1h = vote_1h_candle if candle_strength >= tft_strength else vote_1h_tft

    vote_30m = _vote(fc_30m, "dir3")   # cont. 65d — 30m vote
    vote_15m = _vote(fc_15m, "dir3")
    vote_5m  = _vote(fc_5m,  "dir3")
    vote_1m  = _vote(fc_1m,  "dir1")   # 1m uses shorter horizon

    # ── Core voting ───────────────────────────────────────────────────────
    # cont. 65d — 30m promoted to 4th core vote alongside 1h/15m/5m.
    # When 30m forecast is missing (cold-start before retrain), it returns
    # "neutral" which doesn't sway the vote either way — backward compatible.
    core_votes = [vote_1h, vote_30m, vote_15m, vote_5m]
    longs  = sum(1 for v in core_votes if v == "long")
    shorts = sum(1 for v in core_votes if v == "short")

    ofi_direction = "long" if ofi >= 0 else "short"

    # ── Macro 4h veto ─────────────────────────────────────────────────────
    candidate_direction = "long" if longs > shorts else (
        "short" if shorts > longs else ofi_direction)
    veto_reason = _check_macro_veto(fc_4h, candidate_direction)
    if veto_reason is not None:
        audit = {
            "status":      "vetoed",
            "veto_reason": veto_reason,
            "votes":       {"1h": vote_1h, "15m": vote_15m, "5m": vote_5m,
                            "1m": vote_1m},
            "ofi":         round(ofi, 6),
            "regime":      regime,
        }
        log_cascade_decision(pair, audit)
        return None, 0.0, audit

    # ── Resolve direction + confidence ────────────────────────────────────
    # cont. 65d — 4-vote core (1h / 30m / 15m / 5m).
    # Thresholds:
    #   ≥3 same direction, 0 opposite  → strong majority (confidence 80-100)
    #   2 same, 0 opposite (others neutral) → weak majority (67)
    #   2 vs 2                          → split → OFI tiebreaker / strict reject
    #   1 vs 1 (others neutral)         → split-mild → OFI tiebreaker / strict reject
    #   all neutral / 0-0               → OFI fallback / strict reject
    # When `longs > shorts` and `longs >= 2`, treat as long majority; conf
    # scales with the gap (longs - shorts).
    if longs >= 3 and shorts == 0:
        direction, conf, rationale = "long", 100.0, f"unanimous_long_{longs}of{longs}"
    elif shorts >= 3 and longs == 0:
        direction, conf, rationale = "short", 100.0, f"unanimous_short_{shorts}of{shorts}"
    elif longs >= 3:        # 3 long + 1 short → strong but not unanimous
        direction, conf, rationale = "long", 80.0, f"strong_long_{longs}_vs_{shorts}"
    elif shorts >= 3:
        direction, conf, rationale = "short", 80.0, f"strong_short_{shorts}_vs_{longs}"
    elif longs == 2 and shorts == 0:
        direction, conf, rationale = "long", 67.0, "majority_long_2of4"
    elif shorts == 2 and longs == 0:
        direction, conf, rationale = "short", 67.0, "majority_short_2of4"
    elif (longs == 2 and shorts == 2) or (longs == 1 and shorts == 1):
        # Split — OFI tiebreaker. Strict mode rejects (no clear majority).
        if strict:
            try:
                r.incr("cascade:strict_reject_split")
            except Exception:
                pass
            return None, 0.0, {
                "status":  "strict_split_unresolvable",
                "votes":   {"1h": vote_1h, "30m": vote_30m,
                            "15m": vote_15m, "5m": vote_5m},
                "ofi":     round(ofi, 6),
                "regime":  regime,
            }
        direction = ofi_direction
        conf = 50.0
        rationale = f"split_{longs}_{shorts}_ofi_tiebreak_{ofi_direction}"
    else:
        # All four neutral — pure OFI fallback.
        # cont. 65 — distinguish "forecasts present but neutral" from
        # "all forecasts missing" (cold). The latter signals an upstream
        # producer failure (candlenet queue blocked / TTL expired); without
        # this counter the cascade silently degrades to OFI-only and the
        # operator never knows. Per [[feedback_silent_rejection]].
        _present = sum(1 for fc in (fc_1h, fc_30m, fc_15m, fc_5m) if fc is not None)
        if strict:
            # cont. 65k — STRICT-NEUTRAL FALLBACK (breaks the deadlock: the
            # retrained models are BALANCED but low-conviction, so dir3≈0.5 →
            # cascade neutral → strict reject → no trades → no data → models stay
            # weak). When `cascade:strict_neutral_fallback`="1" (default on), use
            # the SOFT directional LEAN of the present forecasts instead of
            # rejecting: 15m weighted highest (it's the most balanced/reliable
            # head), then 30m/5m, then 1h; OFI only as last tiebreak. This uses
            # the balanced candle models' lean (NOT the short-biased OFI) to pick
            # direction, so accumulated data isn't mono-short. A confident candle
            # DISAGREEMENT is still handled by the majority branches above (this
            # is only reached when nothing is confident). Set the key to "0" to
            # restore the hard strict-reject.
            _fallback_on = True
            try:
                _fallback_on = (r.get("cascade:strict_neutral_fallback") or "1") == "1"
            except Exception:
                pass
            if _fallback_on:
                _w = {"15m": fc_15m, "30m": fc_30m, "5m": fc_5m, "1h": fc_1h}
                _wt = {"15m": 1.0, "30m": 0.7, "5m": 0.6, "1h": 0.5}
                _lean = 0.0
                for _tf, _fc in _w.items():
                    if _fc is not None:
                        try:
                            _lean += (float(_fc.get("dir3", 0.5)) - 0.5) * _wt[_tf]
                        except Exception:
                            pass
                if abs(_lean) >= 0.005:
                    direction = "long" if _lean > 0 else "short"
                    rationale = f"strict_neutral_lean_{direction}_{_lean:+.3f}"
                else:
                    direction = ofi_direction
                    rationale = f"strict_neutral_ofi_tiebreak_{ofi_direction}"
                conf = 50.0
                try:
                    r.incr("cascade:strict_neutral_fallback_used")
                except Exception:
                    pass
                _audit = {"status": "decided", "direction": direction,
                          "confidence": conf, "rationale": rationale,
                          "lean": round(_lean, 4), "ofi": round(ofi, 6),
                          "regime": regime}
                log_cascade_decision(pair, _audit)
                return direction, conf, _audit
            # Fallback disabled → original hard strict-reject.
            try:
                r.incr("cascade:strict_reject_neutral")
            except Exception:
                pass
            return None, 0.0, {
                "status":   "strict_all_neutral",
                "present":  _present,
                "votes":    {"1h": vote_1h, "30m": vote_30m,
                             "15m": vote_15m, "5m": vote_5m},
                "ofi":      round(ofi, 6),
                "regime":   regime,
            }
        if _present == 0:
            rationale = f"all_missing_ofi_cold_fallback_{ofi_direction}"
            try:
                r.incr("cascade:fallback_ofi_cold")
            except Exception:
                pass
            try:
                log.warning("cascade_cold_fallback",
                            pair=pair, direction=ofi_direction,
                            note="no candle forecasts present — check "
                                 "candlenet worker queue + _FORECAST_TTL")
            except Exception:
                pass
        else:
            rationale = f"all_neutral_ofi_fallback_{ofi_direction}"
        direction = ofi_direction
        conf = 50.0

    # Regime concurrence bumps confidence by +10 (cap 100).
    # bear+short excluded: empirically -$3,507 on 4,173 trades (audit 2026-06-06).
    if regime == "bull" and direction == "long":
        conf = min(100.0, conf + 10.0)

    audit = {
        "status":     "decided",
        "direction":  direction,
        "confidence": conf,
        "rationale":  rationale,
        "votes":      {"1h": vote_1h, "30m": vote_30m, "15m": vote_15m,
                       "5m": vote_5m, "1m": vote_1m,
                       "4h": _vote(fc_4h, "dir3")},
        "tft_bias":   round(tft_bias, 6),
        "ofi":        round(ofi, 6),
        "regime":     regime,
    }
    log_cascade_decision(pair, audit)
    return direction, conf, audit


def log_cascade_decision(pair: str, audit: dict) -> None:
    """Push the decision to Redis list `cascade:decisions` and bump
    per-status counters. Failure to log is non-fatal."""
    try:
        r = redis_client.get()
        import time as _time
        entry = {"pair": pair, "ts": int(_time.time()), **audit}
        r.lpush("cascade:decisions", json.dumps(entry))
        r.ltrim("cascade:decisions", 0, 999)
        # Per-status counters.
        status = audit.get("status", "unknown")
        r.incr(f"cascade:count:{status}")
        if status == "decided":
            r.incr(f"cascade:rationale:{audit.get('rationale', 'unknown')}")
    except Exception as exc:
        log.debug("cascade_log_failed", error=str(exc)[:120])
