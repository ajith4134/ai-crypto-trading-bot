"""Trade-close failure diagnosis (Blueprint §F13 cont. 40).

Computes a top-level `failure_type` ('direction' | 'signal' | 'win' |
'unclassified') and a multi-label `diagnosis_tags` list for every closed
trade. Replaces the binary if/else previously inline in
`execution/paper.py:69-95`.

Tag set is defined by the blueprint update (cont. 40):

  sl_too_tight           — exit hit at <1.5× VPIN noise band via trailing_sl
  entry_timing           — peak loss substantially worse than final, recovered
  regime_flip            — HMM regime at entry ≠ regime at exit
  dca_kill               — DCA round triggered and trade still lost
  funding_burn           — estimated funding cost ≥ 25% of |net_pnl|
  wrong_pair             — pair dir-accuracy <40% and early peak loss
  signal_false_positive  — opposite direction would have also lost

Tags are additive and orthogonal to failure_type. A losing trade with
failure_type='direction' can also carry {sl_too_tight, regime_flip} — the
Direction Predictor training pipeline filters those out (see
ml/direction_model.py::_clean_training_corpus).
"""
from __future__ import annotations

import structlog

import redis_client
import redis_keys

log = structlog.get_logger()

# Tuning constants — single source of truth.
_SL_NOISE_BAND_MULT      = 1.5
_ENTRY_TIMING_PEAK_RATIO  = 1.6   # |peak_loss| ≥ 1.6 × |net_pnl|
_FUNDING_BURN_RATIO       = 0.25
_WRONG_PAIR_ACC_THRESHOLD = 40.0
_WRONG_PAIR_EARLY_LOSS_FRACTION = 0.05


def _vol_unit(pair: str) -> float:
    """Per-pair volatility unit (fraction of mark). Mirrors
    risk.manager._volatility_unit so this module doesn't pull risk into
    execution. Falls back to 1.0% when VPIN is missing."""
    r = redis_client.get()
    try:
        vpin = float(r.get(redis_keys.VPIN.replace("{pair}", pair)) or 0)
    except (TypeError, ValueError):
        vpin = 0.0
    return max(0.005, min(0.025, vpin * 8.0))


def _pair_dir_accuracy(pair: str) -> float | None:
    r = redis_client.get()
    try:
        raw = r.get(f"brain:directional_accuracy:{pair}")
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _current_regime() -> str | None:
    r = redis_client.get()
    try:
        return r.get(redis_keys.CURRENT_REGIME)
    except Exception:
        return None


def _funding_rate(pair: str) -> float:
    r = redis_client.get()
    try:
        return float(r.get(redis_keys.FUNDING_RATE.replace("{pair}", pair)) or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_dca_triggered(dca_status) -> bool:
    if isinstance(dca_status, dict):
        return bool(dca_status.get("round_1_triggered") or dca_status.get("round_2_triggered"))
    return False


def diagnose(trade: dict, exit_price: float, net_pnl: float,
             hold_seconds: int, exit_reason: str | None) -> dict:
    """Classify a closed trade. Returns {failure_type, diagnosis_tags, counterfactual_pnl}.

    Inputs are taken from the trade dict (DB row) plus the close-time
    snapshot. Pure function modulo Redis reads — safe to call from
    paper.py or live.py.
    """
    pair       = trade["pair"]
    direction  = trade["direction"]
    entry      = float(trade.get("average_entry") or trade.get("entry_price") or 0)
    qty        = float(trade.get("quantity") or 0)
    capital    = float(trade.get("capital_usdt") or 0)
    leverage   = float(trade.get("leverage") or 1)
    peak_loss  = float(trade.get("peak_loss_usdt") or 0)
    entry_regime = trade.get("market_regime")
    direction_sign = 1.0 if direction == "long" else -1.0
    fees = capital * leverage * 0.0004   # taker estimate, same as paper.py

    # ── Counterfactual: would the opposite direction have profited? ─────
    opposite_pnl = (exit_price - entry) * qty * (-direction_sign) - fees

    tags: list[str] = []

    # ── failure_type top-level routing ────────────────────────────────
    if net_pnl > 0:
        failure_type: str | None = "win"
    elif opposite_pnl > 0:
        failure_type = "direction"
    elif opposite_pnl <= 0:
        failure_type = "signal"
        tags.append("signal_false_positive")
    else:
        failure_type = "unclassified"

    # Only run failure-mode tagging on losing trades. Wins don't need a
    # post-mortem (yet — Brain may add winning-trade tags later).
    if failure_type == "win":
        return {
            "failure_type": failure_type,
            "diagnosis_tags": tags,
            "counterfactual_pnl": round(opposite_pnl, 4),
        }

    # ── sl_too_tight ────────────────────────────────────────────────────
    # Exit hit via trailing_sl within 1.5× pair's realised volatility band.
    # Tight stop ⇒ noise stopped us out, not a true adverse move.
    if exit_reason == "trailing_sl" and entry > 0 and exit_price > 0:
        move_pct  = abs(exit_price - entry) / entry
        noise_pct = _vol_unit(pair)
        if move_pct < _SL_NOISE_BAND_MULT * noise_pct:
            tags.append("sl_too_tight")

    # ── entry_timing ────────────────────────────────────────────────────
    # Trade went into deep drawdown then partly recovered — direction was
    # roughly right but entry happened against a short-term swing.
    if peak_loss < 0 and net_pnl < 0:
        if abs(peak_loss) >= _ENTRY_TIMING_PEAK_RATIO * abs(net_pnl):
            tags.append("entry_timing")

    # ── regime_flip ────────────────────────────────────────────────────
    exit_regime = _current_regime()
    if entry_regime and exit_regime and entry_regime != exit_regime:
        tags.append("regime_flip")

    # ── dca_kill ────────────────────────────────────────────────────────
    if _is_dca_triggered(trade.get("dca_status")) and net_pnl < 0:
        tags.append("dca_kill")

    # ── funding_burn ────────────────────────────────────────────────────
    # Binance USDT-M funding fires every 8h. Estimate accrued cost as
    # position_size × |funding_rate| × n_funding_events. Tag when this is
    # a meaningful fraction of the loss.
    if hold_seconds > 0 and capital > 0:
        n_funding_events = max(0, int(hold_seconds // (8 * 3600)))
        if n_funding_events > 0:
            funding_cost = (capital * leverage *
                            abs(_funding_rate(pair)) * n_funding_events)
            if abs(net_pnl) > 0 and funding_cost >= _FUNDING_BURN_RATIO * abs(net_pnl):
                tags.append("funding_burn")

    # ── wrong_pair ──────────────────────────────────────────────────────
    pair_acc = _pair_dir_accuracy(pair)
    if pair_acc is not None and pair_acc < _WRONG_PAIR_ACC_THRESHOLD:
        early_loss = (peak_loss < 0 and hold_seconds > 0
                      and abs(peak_loss) >= abs(net_pnl) * 0.8)
        if early_loss:
            tags.append("wrong_pair")

    return {
        "failure_type":       failure_type,
        "diagnosis_tags":     tags,
        "counterfactual_pnl": round(opposite_pnl, 4),
    }
