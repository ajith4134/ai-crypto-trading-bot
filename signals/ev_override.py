"""signals/ev_override.py — cont. 69s.

F9/F12 §4.3 "gate-override at reduced size" — FIRST INCREMENT (shadow).

Turns the postmortem CF commentary into a DECISION-TIME, positive-EV check.
For a signal the gates rejected for a RECOVERABLE reason, estimate whether
taking it (at reduced size) would be positive expected-value, using data the
bot already maintains — no new model artifact required:

  * p_win  = the Bayesian per-strength-bucket posterior LOWER bound
             (signals/bayes_threshold.py — trained on counterfactual outcomes).
             This is the CF-trained win-probability proxy.
  * peak/dd = per-direction counterfactual averages (avg peak% on would-have-won,
             avg |loss%| on would-have-lost), cached in Redis by refresh_segments()
             so the hot path does ZERO DB work.

  EV_$ = p_win * CAPTURE * peak_frac * notional
        - (1 - p_win) * dd_frac * notional

DECISION: override (would-take) iff  EV_$ >= ev_min  AND  p_win >= p_win_min
AND the strength bucket is mature (>= MIN_SAMPLES CF outcomes). size_mult scales
with p_win, clamped to [size_min, size_max].

MODE
----
SHADOW (default): evaluate() records what it WOULD override (Redis counters +
capped audit list + structured log). It does NOT change the trade decision — the
engine takes no position from this module. Flip `ev_override:live=1` ONLY after
the shadow stream shows the override is calibrated AND the full-deploy capital
question (predicted_profit_loop.md §7.5) is resolved — live-take re-admits at
reduced size and must carve capital from the full-deploy allocation.

Kill switch: `ev_override:enabled` ("1" default — shadow). `ev_override:live`
("0" default — do not take trades).

HONEST SCOPE (Rule 4): this is NOT the dedicated XGBoost CF predictor (§4.1) nor
the Meta-RL judge (§9.1). p_win is the bayes bucket posterior used as a calibrated
proxy; peak/dd are per-direction segment averages, not per-signal regressions.
Those remain deferred. This increment makes F9/F12 emit a decision-time EV signal
(measured + audited) for the first time, which is the prerequisite for live-take.
"""
from __future__ import annotations

import json
import time
from typing import Optional

import structlog

import redis_client

log = structlog.get_logger()

# --- tunables (all Redis-overridable via the keys below) ---
_CAPTURE = 0.5            # trailing-SL capture fraction of peak (empirical, §4.6)
_MIN_SAMPLES = 30         # bucket must have >= this many CF outcomes (cold-start)
_DEFAULT_PEAK_FRAC = 0.16 # fallback avg peak on wins (doc §2: +15.99%)
_DEFAULT_DD_FRAC = 0.12   # fallback avg drawdown on losses (doc §2: -12.26%)
_DEFAULT_EV_MIN = 0.50    # $ EV floor to override
_DEFAULT_PWIN_MIN = 0.55  # min win-prob lower bound
_SIZE_MIN = 0.25          # reduced-size floor
_SIZE_MAX = 0.50          # reduced-size ceiling
_AUDIT_MAX = 500          # capped audit list length

# Redis keys
K_ENABLED = "ev_override:enabled"
K_LIVE = "ev_override:live"
K_EV_MIN = "ev_override:ev_min_usd"
K_PWIN_MIN = "ev_override:p_win_min"
K_SEG_PEAK = "ev_override:seg:{d}:peak_frac"
K_SEG_DD = "ev_override:seg:{d}:dd_frac"
K_SEG_N = "ev_override:seg:{d}:n"
K_EVALUATED = "ev_override:evaluated_count"
K_WOULD = "ev_override:would_override_count"
K_LIVE_TAKEN = "ev_override:live_taken_count"
K_COLDSTART = "ev_override:coldstart_skip_count"
K_AUDIT = "ev_override:audit"


def _r():
    return redis_client.get()


def _enabled() -> bool:
    try:
        v = _r().get(K_ENABLED)
        return True if v is None else v in ("1", b"1")
    except Exception:
        return True


def _live() -> bool:
    try:
        v = _r().get(K_LIVE)
        return False if v is None else v in ("1", b"1")
    except Exception:
        return False


def _f(key: str, default: float) -> float:
    try:
        v = _r().get(key)
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _segment(direction: str) -> tuple[float, float, int]:
    """(peak_frac, dd_frac, n) for a direction — cached by refresh_segments()."""
    d = "long" if direction == "long" else "short"
    peak = _f(K_SEG_PEAK.replace("{d}", d), _DEFAULT_PEAK_FRAC)
    dd = _f(K_SEG_DD.replace("{d}", d), _DEFAULT_DD_FRAC)
    try:
        n = int(_r().get(K_SEG_N.replace("{d}", d)) or 0)
    except (TypeError, ValueError):
        n = 0
    return peak, dd, n


def evaluate(signal: dict, rejection_reason: str,
             notional: float, signal_id: Optional[str] = None) -> dict:
    """Decision-time EV check for a recoverable-rejected signal.

    Returns a decision dict. In SHADOW mode (default) the caller MUST ignore
    `override` for trade-taking — it is observational. Never raises.
    """
    out = {"override": False, "live": False, "mode": "shadow",
           "reason": rejection_reason}
    try:
        if not _enabled():
            out["mode"] = "disabled"
            return out
        from signals.bayes_threshold import (
            _bucket_for, _read_bucket, _posterior_lower_bound,
        )
        strength = float(signal.get("signal_strength") or 0)
        direction = signal.get("direction") or "long"
        bucket = _bucket_for(strength)
        if bucket is None:
            out["mode"] = "below_buckets"
            return out
        alpha, beta = _read_bucket(bucket)
        n_bucket = int((alpha - 0.5) + (beta - 0.5))
        if n_bucket < _MIN_SAMPLES:
            try:
                _r().incr(K_COLDSTART)
            except Exception:
                pass
            out["mode"] = "coldstart"
            out["bucket_n"] = n_bucket
            return out

        p_win = _posterior_lower_bound(alpha, beta)
        peak_frac, dd_frac, seg_n = _segment(direction)
        notional = max(0.0, float(notional or 0.0))
        ev = (p_win * _CAPTURE * peak_frac * notional
              - (1.0 - p_win) * dd_frac * notional)

        ev_min = _f(K_EV_MIN, _DEFAULT_EV_MIN)
        p_win_min = _f(K_PWIN_MIN, _DEFAULT_PWIN_MIN)
        # size scales linearly with conviction above the p_win floor
        if p_win_min < 1.0:
            frac = (p_win - p_win_min) / (1.0 - p_win_min)
        else:
            frac = 0.0
        size_mult = max(_SIZE_MIN, min(_SIZE_MAX,
                        _SIZE_MIN + (_SIZE_MAX - _SIZE_MIN) * max(0.0, min(1.0, frac))))

        would = ev >= ev_min and p_win >= p_win_min
        out.update({
            "override": bool(would),
            "live": _live(),
            "p_win": round(p_win, 4),
            "ev_usd": round(ev, 4),
            "size_mult": round(size_mult, 3),
            "bucket": list(bucket),
            "bucket_n": n_bucket,
            "peak_frac": round(peak_frac, 4),
            "dd_frac": round(dd_frac, 4),
            "strength": round(strength, 2),
            "direction": direction,
            "notional": round(notional, 2),
        })

        try:
            r = _r()
            r.incr(K_EVALUATED)
            if would:
                r.incr(K_WOULD)
                if out["live"]:
                    r.incr(K_LIVE_TAKEN)
                audit = {**{k: out[k] for k in
                            ("p_win", "ev_usd", "size_mult", "strength",
                             "direction", "reason", "live")},
                         "pair": signal.get("pair"),
                         "signal_id": signal_id, "ts": int(time.time())}
                r.lpush(K_AUDIT, json.dumps(audit))
                r.ltrim(K_AUDIT, 0, _AUDIT_MAX - 1)
        except Exception:
            pass

        log.info("ev_override_eval",
                 pair=signal.get("pair"), reason=rejection_reason,
                 would_override=would, live=out["live"],
                 p_win=out.get("p_win"), ev_usd=out.get("ev_usd"),
                 size_mult=out.get("size_mult"))
    except Exception as exc:
        log.debug("ev_override_eval_failed", error=str(exc)[:160])
    return out


def refresh_segments(lookback_days: int = 30) -> dict:
    """Periodic task: recompute per-direction avg peak (wins) + avg |dd| (losses)
    from evaluable counterfactuals and cache to Redis. Excludes the
    'stale_backlog_unevaluable' stubs (NULL outcomes) by requiring would_have_won
    IS NOT NULL. Hot-path evaluate() then does no DB work."""
    from db import db_conn
    out: dict = {}
    # direction is reliably on signals (counterfactuals.direction is only set on
    # the decode / bulk-retire paths) → join. This is a periodic task, not hot path.
    sql = """
        SELECT s.direction,
               AVG(c.peak_profit_pct) FILTER (WHERE c.would_have_won) AS peak,
               AVG(ABS(LEAST(c.peak_loss_pct,0))) FILTER (WHERE NOT c.would_have_won) AS dd,
               COUNT(*) AS n
        FROM counterfactuals c
        JOIN signals s ON s.id = c.signal_id
        WHERE c.created_at > NOW() - INTERVAL %s
          AND c.would_have_won IS NOT NULL
          AND s.direction IS NOT NULL
        GROUP BY s.direction
    """
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (f"{int(lookback_days)} days",))
                rows = cur.fetchall()
        r = _r()
        for direction, peak, dd, n in rows:
            d = "long" if direction == "long" else "short"
            # peak_profit_pct / peak_loss_pct are PERCENT units → /100 to fraction
            peak_frac = max(0.0, float(peak or 0.0)) / 100.0
            dd_frac = max(0.0, float(dd or 0.0)) / 100.0
            if peak_frac > 0:
                r.set(K_SEG_PEAK.replace("{d}", d), round(peak_frac, 5))
            if dd_frac > 0:
                r.set(K_SEG_DD.replace("{d}", d), round(dd_frac, 5))
            r.set(K_SEG_N.replace("{d}", d), int(n or 0))
            out[d] = {"peak_frac": round(peak_frac, 5),
                      "dd_frac": round(dd_frac, 5), "n": int(n or 0)}
        log.info("ev_override_segments_refreshed", segments=out)
    except Exception as exc:
        log.warning("ev_override_refresh_failed", error=str(exc)[:200])
    return out


def state() -> dict:
    """Dashboard helper."""
    r = _r()

    def _i(k: str) -> int:
        try:
            v = r.get(k)
            return int(v) if v is not None else 0
        except Exception:
            return 0

    audit = []
    try:
        for raw in (r.lrange(K_AUDIT, 0, 49) or []):
            try:
                audit.append(json.loads(raw))
            except Exception:
                continue
    except Exception:
        pass
    return {
        "enabled": _enabled(),
        "live": _live(),
        "evaluated": _i(K_EVALUATED),
        "would_override": _i(K_WOULD),
        "live_taken": _i(K_LIVE_TAKEN),
        "coldstart_skips": _i(K_COLDSTART),
        "segments": {
            "long": _segment("long"),
            "short": _segment("short"),
        },
        "recent_audit": audit,
    }
