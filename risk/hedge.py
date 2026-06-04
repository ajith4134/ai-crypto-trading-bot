"""F44 — Directional Hedge on Confirmed Trend Continuation.

Blueprint:
  Triggers when a losing trade's DCA round-1 has fired AND price has continued
  another -5% past the DCA trigger, confirming the trend isn't reversing. A
  counter-direction position is opened on the same symbol to profit from the
  continuation. The hedge is a COMPANION to DCA, not a replacement — DCA bets
  on reversal, the hedge profits from continuation. Both run simultaneously.

All five trigger conditions must hold:
  1. Original trade has completed DCA round 1 (price already at -20% from entry)
  2. Price has continued -5% past the DCA-1 trigger point
  3. HMM regime classifies state as `bull` or `bear` (trending, not ranging/turbulent)
  4. Brain directional accuracy on this pair >= 55%
  5. Free balance >= hedge_capital × 2 (DCA reserve rule applies to the hedge too)

Sizing:
  hedge_capital = abs(unrealised_loss_usdt) / (CONTINUATION_PCT × leverage)
  capped at 50% of the original trade's capital (HEDGE_CAP_FRAC=0.5).

Exit:
  Trailing SL is set by the standard 2% trailing distance via compute_initial_sl
  at hedge open. The +5% breakeven lock is handled in risk.manager.monitor_trailing_sl
  using the trade.hedge_of_trade_id flag.
"""
import json
import time
import structlog
import redis_client
import redis_keys
import config
from db import db_conn

log = structlog.get_logger()


# Blueprint hard gates / categorical params — NOT learnable.
_MIN_PAPER_CLOSED = 300             # stage-3 minimum
_TRENDING_REGIMES = {"bull", "bear"}

# The five SCALAR parameters below are now Brain-learned per blueprint F44
# ("Brain learns optimal parameters through OPRO/GA"). Implementation lives in
# `risk/hedge_params.py` using constant-α MC + exploration (the F35 Q-learning
# pattern, suited to the rare-fire / low-N regime). The hardcoded defaults
# referenced in `risk/hedge_params.PARAMS` are exactly the blueprint values
# the previous code used directly; `get_param()` returns them until
# `n_samples >= _MIN_SAMPLES` accumulate.
#
#   trigger_pct_beyond_dca      (was 0.05) — % past DCA-1 needed to fire
#   expected_continuation_pct   (was 0.05) — sizing denominator
#   hedge_cap_frac              (was 0.50) — max % of parent capital
#   min_directional_accuracy    (was 55.0) — pair-level dir-acc gate
#   breakeven_lock_pct          (was 0.05) — entry-SL lock trigger
#
# See D-08 in BLUEPRINT_COMPLIANCE_AUDIT.md and PROGRESS.md cont. 10.


def _mark_evaluated(reason: str) -> None:
    """Evidence for feature_health — every trigger evaluation increments this."""
    try:
        r = redis_client.get()
        r.incr("hedge:eval_count")
        r.set("hedge:last_eval_ts", str(int(time.time())))
        r.set("hedge:last_eval_reason", reason[:80])
    except Exception:
        pass


def _mark_opened(parent_id: str, hedge_id: str, capital: float) -> None:
    try:
        r = redis_client.get()
        r.incr("hedge:open_count")
        r.set("hedge:last_open_ts", str(int(time.time())))
        r.set("hedge:last_parent_id", str(parent_id))
        r.set("hedge:last_hedge_id", str(hedge_id))
        r.set("hedge:last_capital_usdt", str(round(capital, 2)))
    except Exception:
        pass


def _get_directional_accuracy(pair: str) -> float:
    """Read per-pair directional accuracy 0-100 from brain. 50 = no prior info
    so we reject the hedge — we only act on confirmed-good pairs."""
    try:
        r = redis_client.get()
        raw = r.get(f"brain:directional_accuracy:{pair}")
        if not raw:
            return 0.0
        d = json.loads(raw)
        rate = d.get("rate")
        if rate is None and d.get("total"):
            rate = 100.0 * d.get("correct", 0) / d["total"]
        return float(rate) if rate is not None else 0.0
    except Exception:
        return 0.0


def check_and_open_hedge(trade: dict, engine) -> str | None:
    """Evaluate F44 trigger on this trade; open hedge if all conditions met.
    Returns the hedge trade_id when opened, else None.

    Called from risk.manager.monitor_trailing_sl on every tick for each open
    trade. Cost is dominated by the Redis reads — gates short-circuit quickly
    so the no-op path is cheap."""
    # Quick gate: F44 active in governance?
    try:
        from feature_governance.registry import is_active
        if not is_active("F44"):
            return None
    except Exception:
        pass

    # Gate 0: trade not already hedged
    if trade.get("is_hedge_active") or trade.get("hedge_of_trade_id"):
        return None

    # Gate: stage 3 + 300 closed trades
    r = redis_client.get()
    try:
        paper_closed = int(r.get("brain:paper_closed") or 0)
    except Exception:
        paper_closed = 0
    if paper_closed < _MIN_PAPER_CLOSED:
        return None
    try:
        stage = int(r.get(redis_keys.BRAIN_STAGE) or 1)
    except Exception:
        stage = 1
    if stage < 3:
        return None

    pair = trade["pair"]
    direction = trade["direction"]
    entry = float(trade["entry_price"])
    mark = float(r.get(redis_keys.MARK_PRICE.replace("{pair}", pair)) or 0)
    if mark <= 0:
        return None

    # Gate 1: DCA round 1 must have fired
    # cont. 62 — DCA-disabled bypass. When config.capital.dca_rounds_max == 0
    # (DCA permanently off per feedback_dca_disabled.md), the DCA-1 trigger
    # can never fire. Without this bypass, F44 is dead-by-design. Fallback:
    # require pct_move <= dca_trigger_1_pct / 100 (i.e. trade is at or beyond
    # what would have been the DCA-1 depth). The Gate-2 hedge_threshold check
    # below then adds the learnable extra distance on top.
    raw_dca = trade.get("dca_status")
    dca = raw_dca if isinstance(raw_dca, dict) else (json.loads(raw_dca) if raw_dca else {})
    if not dca.get("round_1_triggered"):
        try:
            _dca_max = int(config.capital.dca_rounds_max or 0)
        except Exception:
            _dca_max = 1
        if _dca_max > 0:
            return None
        # DCA disabled — defer to Gate 2 to check raw % move depth.

    # Snapshot the entire learned param set ONCE per evaluation so the
    # eligibility check and the sizing math see the same values. Drawn with
    # exploration so the learner gathers samples around the current best.
    from risk.hedge_params import sample_open_params
    params_used = sample_open_params()
    trigger_pct_beyond_dca = float(params_used["trigger_pct_beyond_dca"])
    expected_continuation_pct = float(params_used["expected_continuation_pct"])
    hedge_cap_frac = float(params_used["hedge_cap_frac"])
    min_dir_acc = float(params_used["min_directional_accuracy"])

    # Gate 2: price beyond DCA-1 trigger (learnable distance).
    # DCA-1 fires at config.capital.dca_trigger_1_pct (e.g. -20%). Hedge needs
    # an additional trigger_pct_beyond_dca beyond that.
    dca1 = config.capital.dca_trigger_1_pct / 100.0
    hedge_threshold = dca1 - trigger_pct_beyond_dca
    if direction == "long":
        pct_move = (mark - entry) / entry
    else:
        pct_move = (entry - mark) / entry
    if pct_move > hedge_threshold:
        # Not yet -25% (or whatever threshold) — wait
        return None

    # Gate 3: HMM regime must be trending
    regime = r.get(redis_keys.CURRENT_REGIME) or "unknown"
    if regime not in _TRENDING_REGIMES:
        _mark_evaluated(f"reject_regime_{regime}")
        return None

    # Gate 4: directional accuracy on this pair must clear the learnable threshold
    dir_acc = _get_directional_accuracy(pair)
    if dir_acc < min_dir_acc:
        _mark_evaluated(f"reject_dir_acc_{dir_acc:.0f}_lt_{min_dir_acc:.0f}")
        return None

    # Compute hedge sizing per blueprint formula.
    # hedge_capital = abs(unrealised_loss_usdt) / (expected_continuation_pct × leverage)
    leverage = int(trade.get("leverage") or 5)
    qty = float(trade.get("quantity") or 0)
    if qty <= 0 or leverage <= 0:
        return None
    sign = 1.0 if direction == "long" else -1.0
    unrealised_pnl = sign * (mark - entry) * qty
    if unrealised_pnl >= 0:
        # Trade is somehow profitable despite a move past the trigger? Shouldn't
        # happen but the formula divides by leverage so abs(loss) must be > 0.
        return None
    abs_loss = abs(unrealised_pnl)
    raw_hedge_capital = abs_loss / (expected_continuation_pct * leverage)
    cap = hedge_cap_frac * float(trade.get("capital_usdt") or 0)
    hedge_capital = round(min(raw_hedge_capital, cap), 2)
    if hedge_capital < 5.0:  # MIN_TRADE_USDT floor
        _mark_evaluated(f"reject_too_small_{hedge_capital}")
        return None

    # Gate 5: free balance >= hedge_capital × 2 (DCA reserve rule applies)
    try:
        balance = float(r.get(redis_keys.VIRTUAL_BALANCE)
                        or r.get(redis_keys.ACCOUNT_BALANCE) or 0)
    except Exception:
        balance = 0.0
    if balance < hedge_capital * 2:
        _mark_evaluated(f"reject_balance_{balance:.0f}_need_{hedge_capital*2:.0f}")
        return None

    # All gates pass — open the counter-direction hedge.
    from risk.manager import compute_initial_sl, apply_capital_sl_floor
    hedge_direction = "short" if direction == "long" else "long"
    initial_sl = compute_initial_sl(pair, hedge_direction)
    # cont. 62 — Capital-anchored SL floor also applies to hedges.
    initial_sl = apply_capital_sl_floor(
        initial_sl, mark, hedge_direction, hedge_capital, leverage, r=r)
    hedge_qty = round((hedge_capital * leverage) / mark, 8)

    hedge_trade = {
        "pair": pair,
        "direction": hedge_direction,
        "brain_stage": stage,
        "capital_usdt": hedge_capital,
        "leverage": leverage,
        "quantity": hedge_qty,
        "strategy_id": trade.get("strategy_id"),
        "timeframe": trade.get("timeframe"),
        "market_regime": regime,
        "trade_potential_score": 0,
        "direction_confidence": dir_acc,
        "trailing_sl_level": initial_sl,
        "average_entry": None,
        "feature_vector": json.dumps({
            "hedge_of": str(trade["id"]),
            "parent_unrealised_loss": round(abs_loss, 2),
            # Learned-param snapshot — read back in memory/write.py:write_trade_close
            # to attribute the close outcome to the values that opened this hedge.
            "hedge_params_used": params_used,
        }),
    }
    try:
        hedge_id = engine.open_trade(hedge_trade)
    except Exception as exc:
        log.error("hedge_open_failed", parent_id=str(trade["id"]),
                  pair=pair, error=str(exc)[:200])
        _mark_evaluated(f"open_failed:{str(exc)[:40]}")
        return None

    # Record the hedge relationship on both rows in a single transaction.
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE trades SET hedge_of_trade_id = %s WHERE id = %s",
                    (str(trade["id"]), hedge_id),
                )
                cur.execute(
                    "UPDATE trades SET is_hedge_active = TRUE WHERE id = %s",
                    (str(trade["id"]),),
                )
    except Exception as exc:
        log.error("hedge_link_failed", hedge_id=str(hedge_id),
                  parent_id=str(trade["id"]), error=str(exc)[:200])

    _mark_opened(trade["id"], hedge_id, hedge_capital)
    log.info("hedge_opened",
             parent_id=str(trade["id"]),
             hedge_id=str(hedge_id),
             pair=pair,
             parent_direction=direction,
             hedge_direction=hedge_direction,
             hedge_capital=hedge_capital,
             parent_loss=round(abs_loss, 2),
             dir_acc=round(dir_acc, 1),
             regime=regime,
             pct_move=round(pct_move * 100, 2))
    return hedge_id


def maybe_lock_hedge_breakeven(trade: dict, engine) -> None:
    """Blueprint F44 exit rule: when a hedge trade is +5% in profit, lock the
    trailing SL to breakeven (entry price) so the offset is protected.

    Called from risk.manager.monitor_trailing_sl alongside the regular trailing
    SL move, only for trades that ARE hedges (hedge_of_trade_id IS NOT NULL).
    """
    if not trade.get("hedge_of_trade_id"):
        return
    r = redis_client.get()
    mark = float(r.get(redis_keys.MARK_PRICE.replace("{pair}", trade["pair"])) or 0)
    entry = float(trade["entry_price"])
    if entry <= 0 or mark <= 0:
        return
    direction = trade["direction"]
    sl_level = float(trade.get("trailing_sl_level") or 0)

    # Breakeven lock threshold is also learnable. Read the value snapshotted on
    # this hedge's feature_vector — preserving the same value across the trade's
    # lifecycle so the outcome attributes to the threshold that actually fired.
    lock_pct = _read_lock_pct_from_trade(trade)

    if direction == "long":
        profit_pct = (mark - entry) / entry
        if profit_pct >= lock_pct and sl_level < entry:
            engine.modify_sl(trade["id"], entry)
            log.info("hedge_locked_breakeven", trade_id=str(trade["id"]),
                     direction=direction, mark=mark, entry=entry,
                     lock_pct=round(lock_pct, 4))
    else:
        profit_pct = (entry - mark) / entry
        if profit_pct >= lock_pct and (sl_level > entry or sl_level == 0):
            engine.modify_sl(trade["id"], entry)
            log.info("hedge_locked_breakeven", trade_id=str(trade["id"]),
                     direction=direction, mark=mark, entry=entry,
                     lock_pct=round(lock_pct, 4))


def _read_lock_pct_from_trade(trade: dict) -> float:
    """Pull breakeven_lock_pct from the params snapshot stored on the hedge
    row's feature_vector at open time. Falls back to the current learned /
    default value when missing (legacy hedges from before D-08)."""
    fv = trade.get("feature_vector")
    if isinstance(fv, str):
        try:
            fv = json.loads(fv)
        except Exception:
            fv = {}
    if isinstance(fv, dict):
        snap = fv.get("hedge_params_used") or {}
        if "breakeven_lock_pct" in snap:
            try:
                return float(snap["breakeven_lock_pct"])
            except (TypeError, ValueError):
                pass
    try:
        from risk.hedge_params import get_param
        return float(get_param("breakeven_lock_pct"))
    except Exception:
        return 0.05
