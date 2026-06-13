"""SciBrain Phase 7a — path-aware Counterfactual Digital Twin (living_intelligence §6).

The existing failure label comes from execution/diagnosis.py, which estimates the opposite
direction at the ACTUAL trade's exit price:

    opposite_pnl = (exit_price - entry) * qty * (-sign) - fees        # same-exit-price proxy

That is not a counterfactual — the opposite trade would have hit DIFFERENT SL/TP/exit events on
the same market path. This module replays the real forward OHLC path bar-by-bar under bounded
interventions, each with its own path-aware exit:

    do(actual)    — the real direction + entry SL/TP  → VALIDATION (should reproduce realized PnL)
    do(opposite)  — flip direction, MIRROR the SL/TP geometry about entry
    do(abstain)   — no position, utility 0

From the three realized utilities it assigns a confidence-labelled fault class:

    none       — the actual decision was the best of the three on the real path
    direction  — the opposite policy would have beaten it (a genuine wrong-direction fault)
    selection  — abstaining would have beaten it (should not have traded)

This is the path-aware replacement the design requires; it is the basis the calibration grader and
the ex-post causal audit will eventually use instead of the diagnosis proxy. Bounded + deterministic
+ honest: when the candle path needed for a faithful replay is unavailable, it returns
status='unavailable' with the reason rather than a fabricated number (C2/C11). The richer
interventions the design lists (entry-delay, module-ablation, gain/size/SL-TP sweeps) need a
re-score of the circuit on the entry frame and are a documented follow-on; this slice delivers the
engine + the three core policy replays end-to-end.
"""
from __future__ import annotations

import math
import time

import numpy as np
import structlog

from . import keys as K
from .outcome import _utility, _f, _jload, _epoch_ms
from .sensor_bus import _candles

log = structlog.get_logger()

TWIN_SCHEMA_VERSION = 1

# round-trip taker fee on NOTIONAL (capital × leverage) — matches execution/paper.close_trade
# (fees = capital × leverage × 0.0004) so the twin's PnL is comparable to the realized row. C6.
_DEFAULT_FEE_RATE = 0.0004

# bars searched finest→coarsest for the forward path; finest covering TF wins (best fidelity).
_TF_MS = (("1m", 60_000), ("5m", 300_000), ("15m", 900_000), ("1h", 3_600_000))

# simulation horizon bounds (seconds): replay at least 30 min and at most 6 h past entry so the
# opposite policy can reach its own SL/TP even if our actual trade exited quickly. C6-overridable.
_SIM_MIN_S = 1800
_SIM_MAX_S = 21_600


def _cfg_float(r, key, default):
    try:
        v = r.get(key)
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _forward_path(r, pair: str, entry_ms: float, end_ms: float):
    """OHLC rows on [entry_ms, end_ms] from the live candle lists, finest TF that spans them.
    Returns (path (M,6) oldest-first, tf) or (None, None) when no loaded TF covers the window
    (e.g. the trade is older than the longest candle window — an honest 'unavailable')."""
    best = None
    for tf, tf_ms in _TF_MS:
        arr = _candles(r, pair, tf)            # (N,6) oldest-first, col0=t(ms), col4=close
        if len(arr) == 0:
            continue
        first_t, last_t = float(arr[0, 0]), float(arr[-1, 0])
        # need the window to start at/before entry and extend past entry by a useful amount
        if first_t > entry_ms + tf_ms:
            continue                            # this TF doesn't reach back to entry
        if last_t < entry_ms:
            continue                            # window ends before entry
        mask = (arr[:, 0] >= entry_ms - tf_ms) & (arr[:, 0] <= end_ms)
        rows = arr[mask]
        if len(rows) >= 2:
            return rows, tf                     # finest covering TF — take it
        if best is None and len(rows) >= 1:
            best = (rows, tf)
    return best if best is not None else (None, None)


def _simulate(entry_price: float, direction: str, qty: float, sl, tp,
              path: np.ndarray, fee_rate: float, notional: float) -> dict:
    """Walk the forward OHLC path and exit at the FIRST of SL / TP / horizon-end, path-aware.

    Intrabar both-touched is resolved SL-first (conservative — we never credit a lucky TP when the
    bar also pierced the stop). PnL is signed by direction, net of the round-trip notional fee."""
    sign = 1.0 if direction == "long" else -1.0
    exit_price = float(path[-1, 4])             # default: mark-to-market at horizon end
    exit_reason = "horizon_end"
    bars = 0
    mfe = mae = 0.0                              # running excursion envelope in USDT
    for row in path:
        bars += 1
        hi, lo, cl = float(row[2]), float(row[3]), float(row[4])
        # excursion envelope from intrabar extremes
        for px in (hi, lo):
            pnl_px = (px - entry_price) * qty * sign
            mfe = max(mfe, pnl_px)
            mae = min(mae, pnl_px)
        hit_sl = (sl is not None) and ((lo <= sl) if direction == "long" else (hi >= sl))
        hit_tp = (tp is not None) and ((hi >= tp) if direction == "long" else (lo <= tp))
        if hit_sl:                              # SL first on a both-touched bar
            exit_price, exit_reason = float(sl), "sl"
            break
        if hit_tp:
            exit_price, exit_reason = float(tp), "tp"
            break
    gross = (exit_price - entry_price) * qty * sign
    fees = notional * fee_rate
    net = gross - fees
    return {
        "direction": direction,
        "exit_price": round(exit_price, 10),
        "exit_reason": exit_reason,
        "bars": bars,
        "gross_pnl": round(gross, 4),
        "fees": round(fees, 4),
        "net_pnl": round(net, 4),
        "mfe_usdt": round(mfe, 4),
        "mae_usdt": round(mae, 4),
    }


def _policy_utility(sim: dict, capital: float, lam: dict) -> dict:
    """Wrap a simulated outcome in the same multi-objective utility as the realized OutcomePacket
    so actual / opposite / abstain are scored on one comparable axis."""
    net = sim["net_pnl"]
    roc = (net / capital) if capital > 1e-9 else 0.0
    mae_frac = (abs(sim["mae_usdt"]) / capital) if capital > 1e-9 else 0.0
    cost_frac = (sim["fees"] / capital) if capital > 1e-9 else 0.0
    u = _utility(roc, mae_frac, cost_frac, lam)
    return {**sim, "return_on_capital": round(roc, 6), "utility": u["utility"],
            "utility_terms": u["terms"]}


def _mirror_sl_tp(entry_price: float, sl, tp, orig_direction: str):
    """Mirror the actual SL/TP geometry about entry for the OPPOSITE direction: keep the same
    risk/reward DISTANCES but on the other side of entry."""
    sl_dist = abs(entry_price - sl) if sl is not None else None
    tp_dist = abs(tp - entry_price) if tp is not None else None
    opp = "short" if orig_direction == "long" else "long"
    if opp == "long":
        return opp, (entry_price - sl_dist if sl_dist is not None else None), \
               (entry_price + tp_dist if tp_dist is not None else None)
    return opp, (entry_price + sl_dist if sl_dist is not None else None), \
           (entry_price - tp_dist if tp_dist is not None else None)


def _derive_sl(path: np.ndarray, entry_price: float, direction: str, mult: float):
    """Fallback entry SL for trades that predate the stored entry_policy: place it `mult`×ATR from
    entry on the loss side. ATR over the first ~14 bars of the forward path (best available proxy
    for entry-time volatility). Labelled sl_source='derived' so the confidence is discounted."""
    n = min(14, len(path))
    if n < 2:
        return None
    seg = path[:n]
    trs = [float(seg[i, 2] - seg[i, 3]) for i in range(n)]   # high-low per bar
    atr = sum(trs) / len(trs)
    if atr <= 0:
        return None
    return (entry_price - mult * atr) if direction == "long" else (entry_price + mult * atr)


def simulate_counterfactuals(r, row: dict, provenance: dict) -> dict:
    """Path-aware actual/opposite/abstain replay + confidence-labelled fault class for one closed
    trade. Deterministic from the immutable row + the realized candle path. Never raises."""
    out: dict = {"schema_version": TWIN_SCHEMA_VERSION, "method": "path_aware_policy_replay",
                 "built_ts": round(time.time(), 3)}
    try:
        pair = row.get("pair")
        direction = row.get("direction")
        entry_price = _f(row.get("entry_price"))
        qty = _f(row.get("quantity"))
        capital = _f(row.get("capital_usdt")) or 0.0
        leverage = _f(row.get("leverage")) or 1.0
        entry_ms = _epoch_ms(row.get("entry_time"))
        exit_ms = _epoch_ms(row.get("exit_time"))
        if not (pair and direction and entry_price and qty and entry_ms):
            out["status"] = "unavailable"
            out["reason"] = "missing_entry_fields"
            return out

        hold_s = _f(row.get("hold_time_seconds")) or ((exit_ms - entry_ms) / 1000.0 if exit_ms else 0.0)
        sim_s = max(_SIM_MIN_S, min(_SIM_MAX_S, hold_s * 2.0))
        end_ms = entry_ms + sim_s * 1000.0

        path, tf = _forward_path(r, pair, entry_ms, end_ms)
        if path is None or len(path) < 2:
            out["status"] = "unavailable"
            out["reason"] = "forward_path_out_of_window"
            return out

        # entry SL/TP policy: prefer the frozen entry_policy (new trades); else derive from ATR.
        ep = (provenance or {}).get("entry_policy") or {}
        sl = _f(ep.get("initial_sl"))
        sl_source = ep.get("sl_source") if sl is not None else None
        if sl is None:
            sl = _derive_sl(path, entry_price, direction,
                            _cfg_float(r, K.TWIN_DERIVED_SL_ATR_MULT, 1.5))
            sl_source = "derived_atr"
        tp = _f(row.get("tp_target")) or _f(row.get("tp1_target")) or _f(row.get("tp"))

        fee_rate = _cfg_float(r, K.TWIN_FEE_RATE, _DEFAULT_FEE_RATE)
        notional = capital * leverage
        from .outcome import _lambdas
        lam = _lambdas(r)

        # ACTUAL = the REALIZED outcome (ground truth), NOT a simulation — the real trade used the
        # real (often trailing) SL/TP we can't perfectly reconstruct, so simulating it would inject
        # error into the comparison (e.g. a derived stop the winning trade never had). We only
        # SIMULATE the counterfactuals, and separately replay the actual policy as a fidelity check.
        real_net = _f(row.get("net_pnl_usdt"))
        if real_net is None:
            real_net = _f(row.get("final_pnl_usdt"), 0.0)
        real_fees = _f(row.get("fees_usdt"), 0.0) or 0.0
        real_mae = _f(row.get("peak_loss_usdt"))
        real_roc = (real_net / capital) if capital > 1e-9 else 0.0
        real_mae_frac = (abs(real_mae) / capital) if (capital > 1e-9 and real_mae is not None) else 0.0
        real_util = _utility(real_roc, real_mae_frac,
                             (real_fees / capital) if capital > 1e-9 else 0.0, lam)
        actual = {"direction": direction, "net_pnl": round(real_net, 4),
                  "return_on_capital": round(real_roc, 6), "utility": real_util["utility"],
                  "exit_reason": row.get("exit_reason"), "source": "realized"}

        opp_dir, opp_sl, opp_tp = _mirror_sl_tp(entry_price, sl, tp, direction)
        opposite = _policy_utility(
            _simulate(entry_price, opp_dir, qty, opp_sl, opp_tp, path, fee_rate, notional),
            capital, lam)
        abstain = {"direction": "abstain", "net_pnl": 0.0, "return_on_capital": 0.0,
                   "utility": 0.0, "exit_reason": "no_trade"}

        # FAULT CLASS (path-aware, matches the diagnosis win/direction/signal taxonomy but with a
        # real opposite-policy replay instead of the same-exit-price proxy):
        #   none      — the trade was profitable → the chosen direction was right
        #   direction — we lost AND the opposite policy would have WON on the real path
        #   selection — we lost AND the opposite would also have lost → should have abstained
        if real_net > 0:
            fault = "none"
        elif opposite["net_pnl"] > 0:
            fault = "direction"
        else:
            fault = "selection"
        # advantage the best counterfactual had over the realized utility (0 ⇒ actual was best)
        margin = round(max(opposite["utility"], abstain["utility"]) - actual["utility"], 6)

        # FIDELITY: replay the ACTUAL policy too and compare to realized — a built-in Rule-9 check
        # that also tells us how much to trust the (same-simulator) opposite replay.
        actual_sim = _policy_utility(
            _simulate(entry_price, direction, qty, sl, tp, path, fee_rate, notional), capital, lam)
        val_err = round(actual_sim["net_pnl"] - real_net, 4)
        val_err_frac = (abs(val_err) / capital) if capital > 1e-9 else 0.0

        # confidence in the OPPOSITE replay: discounted for coarse/short paths, a derived (unknown)
        # entry SL, and a simulator that can't even reproduce the actual outcome.
        conf = 1.0
        if tf != "1m":
            conf *= {"5m": 0.85, "15m": 0.7, "1h": 0.5}.get(tf, 0.5)
        if len(path) < 10:
            conf *= 0.7
        if sl_source == "derived_atr":
            conf *= 0.6
        if val_err_frac > 0.05:
            conf *= 0.6                          # actual replay diverged > 5% of capital
        confidence = ("high" if conf >= 0.75 else "medium" if conf >= 0.45 else "low")

        out.update({
            "status": "ok",
            "tf": tf, "path_bars": len(path), "sl": (round(sl, 10) if sl else None),
            "tp": (round(tp, 10) if tp else None), "sl_source": sl_source,
            "actual": actual, "opposite": opposite, "abstain": abstain,
            "fault_class": fault, "fault_margin": margin,
            "confidence": confidence, "confidence_score": round(conf, 3),
            "validation": {"twin_actual_sim_net": actual_sim["net_pnl"], "realized_net": real_net,
                           "abs_error": abs(val_err),
                           "note": "actual is the REALIZED ground truth; twin_actual_sim_net is the "
                                   "simulator replaying the same policy — closeness = replay fidelity "
                                   "(trailing-SL drift is the main expected gap)"},
        })
        return out
    except Exception as exc:
        log.warning("scibrain_twin_failed", trade_id=str(row.get("id")), error=str(exc)[:160])
        out["status"] = "error"
        out["error"] = str(exc)[:160]
        return out
