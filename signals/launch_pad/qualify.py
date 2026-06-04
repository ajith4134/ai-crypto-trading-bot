"""Launch-Pad P3 — "open-green" qualify gate + the 3 movement columns (cont. 70).

A candidate symbol QUALIFIES for a direction when the immediate momentum is
confirmed favourable, so a trade opened off it tends green fast (the achievable
reading of "always open in profit" = minimise Maximum Adverse Excursion at entry;
see next_impl/launch_pad_10.md and the MAE research). The gate REUSES the proven
engine logic (P4 candle-close confirm + CandleNet dir3 consensus) and hardens it
with the falling-knife research (min-move, exhaustion filter).

It also emits the THREE movement metrics as separate values (owner Q4 — do NOT
blend; observe which is most reliable first):
  mv_candlenet = mean over TFs of |dir3-0.5| * |mag3|   (model conviction)
  mv_predicted = max over TFs of max(|mag3|,|mag5|)      (model forecast move %)
  mv_realized  = recent_return% / ATR%                   (live momentum, ATR units)

All reads are non-mutating; no orders. Silent-rejection counters + an auto-disable
deadlock detector follow the house rules (every reject path logs + counts; gate
fails OPEN when >80% reject over 50+ calls so the buffer never starves).
"""
from __future__ import annotations

import json

import structlog

import redis_keys

log = structlog.get_logger()

# TFs used for direction consensus + expected-move (cont. 69: 5m/15m/1h are the
# reliable, less-noisy heads; 1m is reserved for the candle-close confirm only).
_FORECAST_TFS = ("5m", "15m", "1h")
_ATR_PERIOD = 14
_RET_LOOKBACK = 5          # candles for realized momentum
_DEADLOCK_MIN_CALLS = 50
_DEADLOCK_REJECT_RATE = 0.80


# ── forecast + candle reads ──────────────────────────────────────────────────

def _get_forecast(r, symbol: str, tf: str) -> dict | None:
    raw = r.get(f"{symbol}:{tf}:candle_forecast")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _forecasts(r, symbol: str) -> dict[str, dict]:
    out = {}
    for tf in _FORECAST_TFS:
        fc = _get_forecast(r, symbol, tf)
        if fc is not None:
            out[tf] = fc
    return out


def _candles(r, symbol: str, n: int) -> list[dict]:
    """Last n closed 1m candles, newest first (matches LPUSH convention)."""
    raw = r.lrange(f"{symbol}:1m:candles", 0, n - 1)
    out = []
    for item in raw:
        try:
            out.append(json.loads(item))
        except Exception:
            continue
    return out


# ── the 3 movement columns ───────────────────────────────────────────────────

def _atr_pct(candles: list[dict]) -> float | None:
    """ATR(period) over 1m candles, normalised to % of latest close. None if
    insufficient data."""
    if len(candles) < _ATR_PERIOD + 1:
        return None
    trs = []
    # candles[0] is newest; walk newest→older building true ranges
    for i in range(_ATR_PERIOD):
        cur, prev = candles[i], candles[i + 1]
        try:
            h, l = float(cur["h"]), float(cur["l"])
            pc = float(prev["c"])
        except (KeyError, TypeError, ValueError):
            return None
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs) / len(trs)
    last_close = float(candles[0].get("c") or 0)
    if last_close <= 0:
        return None
    return atr / last_close * 100.0


def movement_metrics(r, symbol: str) -> dict:
    """Compute the 3 movement columns + raw forecasts for a symbol. Missing
    inputs yield None for that column (never raises)."""
    fcs = _forecasts(r, symbol)
    mv_candlenet = mv_predicted = mv_realized = None

    if fcs:
        convictions = []
        moves = []
        for fc in fcs.values():
            try:
                dir3 = float(fc.get("dir3", fc.get("dir1", 0.5)))
                mag3 = abs(float(fc.get("mag3", 0.0) or 0.0))
                mag5 = abs(float(fc.get("mag5", 0.0) or 0.0))
            except (TypeError, ValueError):
                continue
            convictions.append(abs(dir3 - 0.5) * mag3)
            moves.append(max(mag3, mag5))
        if convictions:
            mv_candlenet = round(sum(convictions) / len(convictions), 6)
        if moves:
            mv_predicted = round(max(moves), 6)

    candles = _candles(r, symbol, _ATR_PERIOD + 2)
    atrp = _atr_pct(candles)
    if atrp and atrp > 0 and len(candles) > _RET_LOOKBACK:
        try:
            c_now = float(candles[0]["c"])
            c_then = float(candles[_RET_LOOKBACK]["c"])
            if c_then > 0:
                ret_pct = (c_now - c_then) / c_then * 100.0
                mv_realized = round(ret_pct / atrp, 6)   # signed momentum in ATR units
        except (KeyError, TypeError, ValueError):
            pass

    return {"mv_candlenet": mv_candlenet, "mv_predicted": mv_predicted,
            "mv_realized": mv_realized, "forecasts": fcs}


# ── direction decision ───────────────────────────────────────────────────────

def decide_direction(forecasts: dict[str, dict]) -> str | None:
    """CandleNet dir3 consensus across TFs → long / short / None (no clean
    majority). dir3 > 0.5 ⇒ up bias."""
    if not forecasts:
        return None
    up = down = 0
    for fc in forecasts.values():
        try:
            d = float(fc.get("dir3", fc.get("dir1", 0.5)))
        except (TypeError, ValueError):
            continue
        if d > 0.5:
            up += 1
        elif d < 0.5:
            down += 1
    if up > down:
        return "long"
    if down > up:
        return "short"
    return None


# ── the open-green gate ──────────────────────────────────────────────────────

def _candle_close_confirms(r, symbol: str, direction: str) -> bool:
    """Reuse of engine P4: last CLOSED 1m candle must confirm direction
    (close>open long / close<open short). High-conviction 1m dir1 bypasses."""
    fc1m = _get_forecast(r, symbol, "1m")
    if fc1m:
        try:
            d1 = float(fc1m.get("dir1", 0.5))
            if (direction == "long" and d1 > 0.7) or (direction == "short" and d1 < 0.3):
                return True
        except (TypeError, ValueError):
            pass
    cands = _candles(r, symbol, 1)
    if not cands:
        return False
    try:
        o, c = float(cands[0]["o"]), float(cands[0]["c"])
    except (KeyError, TypeError, ValueError):
        return False
    if o <= 0 or c <= 0:
        return False
    return (direction == "long" and c > o) or (direction == "short" and c < o)


def _exhaustion_ok(r, symbol: str, direction: str) -> bool:
    """Falling-knife guard: reject if 5m exhaustion is over-extended IN the
    trade direction (chasing a blow-off). exhaustion direction: +1 up, -1 down."""
    raw = r.get(f"{symbol}:5m:exhaustion_score")
    if not raw:
        return True   # no data → don't block
    try:
        ex = json.loads(raw)
        score = float(ex.get("score", 0.0))
        ex_dir = int(ex.get("direction", 0))
    except (TypeError, ValueError, json.JSONDecodeError):
        return True
    try:
        thresh = float(r.get("launchpad:exhaustion_max") or 0.8)
    except (TypeError, ValueError):
        thresh = 0.8
    # over-extended up while we want long, or over-extended down while we want short
    if score >= thresh and ((direction == "long" and ex_dir > 0) or
                            (direction == "short" and ex_dir < 0)):
        return False
    return True


def _min_move(r) -> float:
    try:
        return float(r.get("launchpad:min_predicted_move_pct")
                     or r.get("entry:min_predicted_move_pct") or 0.35)
    except (TypeError, ValueError):
        return 0.35


def _deadlock_disabled(r) -> bool:
    return r.get(redis_keys.LAUNCHPAD_QUALIFY_DISABLED) == "1"


def _record(r, accepted: bool, reason: str) -> None:
    """Silent-rejection counters + deadlock auto-disable (fail-open)."""
    try:
        r.incr(redis_keys.LAUNCHPAD_QUALIFY_TOTAL)
        if not accepted:
            r.incr(redis_keys.LAUNCHPAD_QUALIFY_REJECT.replace("{reason}", reason))
            tot = int(r.get(redis_keys.LAUNCHPAD_QUALIFY_TOTAL) or 0)
            # rejects across all reasons
            rej = 0
            for rk in r.scan_iter(match="launchpad:qualify:reject:*"):
                rej += int(r.get(rk) or 0)
            if tot >= _DEADLOCK_MIN_CALLS and (rej / max(tot, 1)) > _DEADLOCK_REJECT_RATE:
                r.set(redis_keys.LAUNCHPAD_QUALIFY_DISABLED, "1")
                log.warning("launchpad_qualify_auto_disabled",
                            reject_rate=round(rej / max(tot, 1), 3), total=tot)
    except Exception:
        pass


def qualify(r, symbol: str, direction: str) -> tuple[bool, str]:
    """Return (qualified, reason) for opening `symbol` in `direction` right now.
    Fails OPEN when the deadlock detector has disabled the gate."""
    if _deadlock_disabled(r):
        return True, "gate_disabled_failopen"

    fcs = _forecasts(r, symbol)
    if not fcs:
        _record(r, False, "no_forecasts")
        return False, "no_forecasts"

    # expected move must clear the dead-mover floor
    moves = []
    for fc in fcs.values():
        try:
            moves.append(max(abs(float(fc.get("mag3", 0.0) or 0.0)),
                             abs(float(fc.get("mag5", 0.0) or 0.0))))
        except (TypeError, ValueError):
            continue
    if moves and max(moves) < _min_move(r):
        _record(r, False, "low_predicted_move")
        return False, "low_predicted_move"

    # CandleNet consensus must not dissent from the intended direction
    consensus = decide_direction(fcs)
    if consensus is not None and consensus != direction:
        _record(r, False, "consensus_disagree")
        return False, "consensus_disagree"

    if not _exhaustion_ok(r, symbol, direction):
        _record(r, False, "exhausted")
        return False, "exhausted"

    if not _candle_close_confirms(r, symbol, direction):
        _record(r, False, "candle_close_no_confirm")
        return False, "candle_close_no_confirm"

    _record(r, True, "ok")
    return True, "ok"


def evaluate(r, symbol: str) -> dict:
    """Convenience used by the maintainer: pick a direction from CandleNet,
    compute the 3 movement columns, and run the open-green gate.
    Returns {direction, qualified, reason, mv_candlenet, mv_predicted, mv_realized}."""
    mm = movement_metrics(r, symbol)
    direction = decide_direction(mm["forecasts"])
    if direction is None:
        return {"direction": None, "qualified": False, "reason": "no_direction",
                "mv_candlenet": mm["mv_candlenet"], "mv_predicted": mm["mv_predicted"],
                "mv_realized": mm["mv_realized"]}
    ok, reason = qualify(r, symbol, direction)
    return {"direction": direction, "qualified": ok, "reason": reason,
            "mv_candlenet": mm["mv_candlenet"], "mv_predicted": mm["mv_predicted"],
            "mv_realized": mm["mv_realized"]}
