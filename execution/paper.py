"""N-02 to N-04: Paper execution engine — simulates fills at mark price."""
import json
from datetime import datetime, timezone
import structlog
import redis_client
import redis_keys
import config
from execution.base import ExecutionEngine
from memory.write import write_trade_open, write_trade_update, write_trade_close
from db import db_conn

log = structlog.get_logger()

_VIRTUAL_BALANCE_KEY = redis_keys.VIRTUAL_BALANCE


# cont. 75 — ATOMIC virtual-balance accounting. VIRTUAL_BALANCE is mutated by
# multiple concurrent actors: the brain's run_open (deduct on open), the
# trailing-SL monitor's close_trade (restore on close), and the DCA loop
# (add_dca / close_partial). The previous pattern everywhere was a NON-atomic
# read-modify-write — GET balance → compute → SET balance. When an open's
# deduct interleaved with a close's restore, one side read a stale value and
# its write clobbered the other's (a lost update), drifting the stored balance
# away from the true ledger (start + realised_pnl − deployed). The same window
# made the sizing check and the actual deduct see different balances, surfacing
# as "insufficient virtual balance" rejects that the sizing math forbids.
#
# All GUARDED deductions (open, DCA) now go through _reserve_balance, which does
# the check AND the deduct inside one server-side Lua call (Redis runs it
# atomically). All ADDITIVE restores use INCRBYFLOAT (a single atomic op). No
# balance mutation reads-then-writes in two steps anymore.
_RESERVE_LUA = """
local bal = tonumber(redis.call('GET', KEYS[1]) or '0')
local amt = tonumber(ARGV[1])
if bal < amt then return '-1' end
return redis.call('INCRBYFLOAT', KEYS[1], '-' .. ARGV[1])
"""


def _reserve_balance(r, amount: float) -> float | None:
    """Atomically deduct `amount` from the virtual balance, but only if the
    balance covers it. Returns the new balance, or None when there are
    insufficient funds (in which case nothing is deducted)."""
    new_bal = r.eval(_RESERVE_LUA, 1, _VIRTUAL_BALANCE_KEY, f"{float(amount):.8f}")
    if str(new_bal) == "-1":
        return None
    return float(new_bal)


class PaperExecutionEngine(ExecutionEngine):

    def open_trade(self, params: dict) -> str:
        """N-02: Simulate fill at current mark price; deduct from virtual balance."""
        r = redis_client.get()
        mark_price = float(r.get(redis_keys.MARK_PRICE.replace("{pair}", params["pair"])) or 0)
        if mark_price <= 0:
            raise ValueError(f"No mark price available for {params['pair']}")

        # cont. 69: honour a pre-set limit fill price (execution.limit_entry).
        # The pending-entry loop only opens once mark has reached the target, so
        # `entry_price` is a realistic fill; market entries leave it unset → mark.
        _fill = params.get("entry_price")
        try:
            fill_price = float(_fill) if _fill else mark_price
        except (TypeError, ValueError):
            fill_price = mark_price
        if fill_price <= 0:
            fill_price = mark_price

        params["entry_price"] = fill_price
        params["is_paper"] = True
        params["average_entry"] = fill_price

        capital = float(params["capital_usdt"])
        # cont. 75 — atomic reserve: check-and-deduct in ONE Redis op so a
        # concurrent close/DCA can't race between the check and the deduct.
        # Reserve FIRST, then persist; refund the reservation if the trade row
        # fails to write so a failed open can never leak capital.
        if _reserve_balance(r, capital) is None:
            balance = float(r.get(_VIRTUAL_BALANCE_KEY) or 0)
            raise ValueError(f"Insufficient virtual balance: {balance:.2f} < {capital:.2f}")
        try:
            trade_id = write_trade_open(params)
        except Exception:
            r.incrbyfloat(_VIRTUAL_BALANCE_KEY, capital)   # refund the reservation
            raise
        r.publish(redis_keys.CH_TRADE_OPENED, json.dumps({
            "trade_id": trade_id, "pair": params["pair"],
            "direction": params["direction"], "entry_price": fill_price,
            "capital_usdt": capital,
        }))
        log.info("paper_trade_opened", trade_id=trade_id, pair=params["pair"], price=fill_price)
        return trade_id

    def close_trade(self, trade_id: str, reason: str) -> None:
        """N-03: Close at current mark price; calculate PnL; restore virtual balance."""
        trade = self._get_trade(trade_id)
        r = redis_client.get()
        mark_price = float(r.get(redis_keys.MARK_PRICE.replace("{pair}", trade["pair"])) or 0)

        qty = float(trade["quantity"])
        entry = float(trade["average_entry"] or trade["entry_price"])
        direction_sign = 1.0 if trade["direction"] == "long" else -1.0

        final_pnl = (mark_price - entry) * qty * direction_sign
        fees = float(trade["capital_usdt"]) * float(trade["leverage"]) * 0.0004
        net_pnl = final_pnl - fees

        # F48 §Idea B (cont. 47) — Aggregate any prior partial-close PnL into
        # the trade's recorded final/net PnL so the DB row shows the full
        # lifecycle outcome (not just the remainder).
        try:
            partial_pnl_acc = float(r.get(f"trade:{trade_id}:partial_pnl_usdt") or 0)
            partial_fees_acc = float(r.get(f"trade:{trade_id}:partial_fees_usdt") or 0)
            if abs(partial_pnl_acc) > 0 or abs(partial_fees_acc) > 0:
                final_pnl = final_pnl + (partial_pnl_acc + partial_fees_acc)
                fees     = fees + partial_fees_acc
                net_pnl  = net_pnl + partial_pnl_acc
                log.info("trade_close_with_partials",
                         trade_id=trade_id,
                         partial_net=round(partial_pnl_acc, 4),
                         partial_fees=round(partial_fees_acc, 4))
                r.delete(f"trade:{trade_id}:partial_pnl_usdt",
                         f"trade:{trade_id}:partial_fees_usdt",
                         f"trade:{trade_id}:partial_count",
                         f"trade:{trade_id}:tp1_fired",
                         f"trade:{trade_id}:tp_fired")
        except Exception:
            pass

        now = datetime.now(timezone.utc)
        entry_time = trade.get("entry_time") or now
        hold_seconds = int((now - entry_time).total_seconds()) if hasattr(now - entry_time, "total_seconds") else 0

        # Blueprint §F13 cont. 40: multi-label failure diagnosis. Top-level
        # failure_type stays as the binary learning-signal router
        # (direction vs signal) but is now accompanied by structured
        # diagnosis_tags that capture sub-causes (sl_too_tight, regime_flip,
        # dca_kill, funding_burn, entry_timing, wrong_pair). See
        # execution/diagnosis.py for the classifier.
        from execution.diagnosis import diagnose
        diagnosis = diagnose(trade, exit_price=mark_price, net_pnl=net_pnl,
                             hold_seconds=hold_seconds, exit_reason=reason)
        failure_type        = diagnosis["failure_type"]
        diagnosis_tags      = diagnosis["diagnosis_tags"]
        counterfactual_raw  = diagnosis["counterfactual_pnl"] if net_pnl < 0 else None
        counterfactual_json = json.dumps(counterfactual_raw) if counterfactual_raw is not None else None

        # Quality score: 50 base ± return% × 2, clamped 0–100
        # Reflects how good this trade was relative to capital deployed
        _pct_return = net_pnl / float(trade["capital_usdt"]) * 100 if float(trade["capital_usdt"]) > 0 else 0
        trade_quality_score = round(max(0.0, min(100.0, 50.0 + _pct_return * 2)), 2)

        # cont. 57 — Final-tick peak capture. The 1Hz trailing-SL monitor
        # samples at coarse resolution and skips its own update on the tick
        # that fires the exit. The realised `net_pnl_usdt` is the
        # definitive close P/L — bump peak_pnl_usdt / peak_loss_usdt if it
        # exceeds the existing high-water marks so the dashboard's Peak +
        # and Peak − columns reflect at least the realised outcome.
        try:
            _peak_pnl  = float(trade.get("peak_pnl_usdt")  or 0)
            _peak_loss = float(trade.get("peak_loss_usdt") or 0)
            _peak_upd = {}
            if net_pnl > _peak_pnl:
                _peak_upd["peak_pnl_usdt"]  = round(net_pnl, 4)
            if net_pnl < _peak_loss:
                _peak_upd["peak_loss_usdt"] = round(net_pnl, 4)
            if _peak_upd:
                write_trade_update(trade_id, _peak_upd)
        except Exception as _pp_exc:
            log.debug("paper_final_peak_capture_skipped",
                      trade_id=trade_id, error=str(_pp_exc)[:120])

        write_trade_close(trade_id, {
            "exit_price": mark_price,
            "exit_time": now,
            "exit_reason": reason,
            "hold_time_seconds": hold_seconds,
            "final_pnl_usdt": round(final_pnl, 4),
            "fees_usdt": round(fees, 4),
            "net_pnl_usdt": round(net_pnl, 4),
            "failure_type": failure_type,
            "diagnosis_tags": diagnosis_tags,
            "counterfactual_result": counterfactual_json,
            "trade_quality_score": trade_quality_score,
        })

        # cont. 53 — manual close: trade row was just purged by write_trade_close.
        # Restore capital, then skip all learner-pollution paths below
        # (F49 perf monitor, F48 signal-event finalisation, CH_TRADE_CLOSED publish).
        from execution.base import is_purge_reason as _is_purge
        _purged = _is_purge(reason)

        # Return entry capital + any DCA capital deployed (each round = capital × 0.5)
        raw_dca = trade.get("dca_status")
        dca_st = raw_dca if isinstance(raw_dca, dict) else (json.loads(raw_dca) if raw_dca else {})
        dca_rounds = sum([
            1 if dca_st.get("round_1_triggered") else 0,
            1 if dca_st.get("round_2_triggered") else 0,
        ])
        dca_return = float(trade["capital_usdt"]) * 0.5 * dca_rounds
        total_return = float(trade["capital_usdt"]) + dca_return + net_pnl
        # cont. 75 — atomic additive restore (was a non-atomic GET+SET that
        # raced concurrent open/DCA deducts and lost updates).
        r.incrbyfloat(_VIRTUAL_BALANCE_KEY, total_return)
        if dca_rounds:
            log.info("dca_capital_returned", trade_id=trade_id, rounds=dca_rounds, dca_return=round(dca_return, 4))

        # cont. 53 — skip all learner-pollution paths on manual purge.
        if _purged:
            log.info("paper_trade_purged_manual",
                     trade_id=trade_id, net_pnl=round(net_pnl, 4), reason=reason)
            # Cleanup any remaining Redis ephemeral keys (purge helper did
            # most of these; this is the existing-engine cleanup also still
            # runs — keep it idempotent).
            try:
                r.delete(f"trade:{trade_id}:tp1", f"trade:{trade_id}:tp2",
                         f"trade:{trade_id}:tp",
                         f"trade:{trade_id}:mag1_pct", f"trade:{trade_id}:mag3_pct",
                         f"trade:{trade_id}:highest_high",
                         f"trade:{trade_id}:lowest_low")
            except Exception:
                pass
            return

        # F49 §Component 2 — Performance Monitor log_outcome.
        # The trade direction tells us the binary "up/down" outcome semantics:
        #   long  + net_pnl>0  → price moved UP relative to entry → outcome=1
        #   short + net_pnl>0  → price moved DOWN relative to entry → outcome=1
        # i.e. "outcome=1" means the price moved in the bot's PREDICTED direction.
        try:
            from ml.performance_monitor import log_outcome
            _direction_sign = 1.0 if trade["direction"] == "long" else -1.0
            _outcome = 1.0 if (mark_price - entry) * _direction_sign > 0 else 0.0
            # For each tracked model, pair the saved prediction with the outcome.
            for _model in ("candlenet_1m", "candlenet_5m", "candlenet_15m",
                           "direction_model"):
                _pred_raw = r.get(f"trade:{trade_id}:pred_{_model}")
                if _pred_raw is None:
                    continue
                try:
                    _pred = float(_pred_raw)
                except (TypeError, ValueError):
                    continue
                # For long trades: pred is P(up); outcome is up=1 / down=0.
                # For short trades: invert pred so that the dimension we
                # compare against is "did the bot's call match reality?".
                if trade["direction"] == "short":
                    _pred = 1.0 - _pred
                log_outcome(_model, _pred, _outcome)
            # Cleanup
            r.delete(
                f"trade:{trade_id}:pred_candlenet_1m",
                f"trade:{trade_id}:pred_candlenet_5m",
                f"trade:{trade_id}:pred_candlenet_15m",
                f"trade:{trade_id}:pred_direction_model",
            )
        except Exception as exc:
            log.debug("perf_log_outcome_failed",
                      trade_id=trade_id, error=str(exc)[:200])

        # F48 §Idea B — clear CandleNet TP keys for this trade (Redis cleanup)
        # F51d (cont. 51) — also clear Chandelier high/low watermark keys
        try:
            r.delete(f"trade:{trade_id}:tp1", f"trade:{trade_id}:tp2",
                     f"trade:{trade_id}:tp",
                     f"trade:{trade_id}:mag1_pct", f"trade:{trade_id}:mag3_pct",
                     f"trade:{trade_id}:highest_high",
                     f"trade:{trade_id}:lowest_low")
        except Exception:
            pass

        # F48 §Idea C — Finalise the entry-timing signal event for PPO training.
        # Reads the state captured at trade-open by signals/engine.py, computes
        # the realised PnL normalised by ATR, writes the event to the JSONL
        # history file consumed by ml.entry_timing_agent.train_entry_timing.
        # Rule 4 simplification: states[1..4] are duplicates of state[0] and
        # pnls_per_step is constant. Full entry-timing learning requires
        # capturing intermediate PnL per candle, deferred to a follow-up.
        try:
            import json as _j
            from ml.entry_timing_agent import log_signal_event
            _raw = r.get(f"trade:{trade_id}:signal_event_state")
            if _raw:
                _meta = _j.loads(_raw)
                _atr_norm = float(_meta.get("atr_norm") or 0.01) or 0.01
                _pnl_norm = (net_pnl / float(trade.get("capital_usdt") or 1.0))
                _state    = _meta.get("state", [])
                event = {
                    "pair":          _meta.get("pair"),
                    "direction":     _meta.get("direction"),
                    "signal_score":  _meta.get("signal_score"),
                    "atr_norm":      _atr_norm,
                    "states":        [_state] * 5,
                    "pnls_per_step": [_pnl_norm] * 5,
                    "trade_id":      trade_id,
                    "outcome_reason": reason,
                }
                log_signal_event(event)
                r.delete(f"trade:{trade_id}:signal_event_state")
        except Exception as exc:
            log.debug("entry_timing_event_finalise_failed",
                      trade_id=trade_id, error=str(exc)[:200])

        # Phase 2 + Phase 3 (cont. 63, 2026-05-29) — feed the realised outcome
        # back to the Bayesian threshold tuner (per-strength-bucket Beta
        # posterior) and the Thompson sampling slot selector (per-
        # (pair, regime) Beta posterior). Both are wrapped — failure ≡ no-op.
        try:
            from signals.bayes_threshold import record_outcome as _bayes_rec
            _bayes_rec(float(trade.get("trade_potential_score")
                             or trade.get("direction_confidence") or 0),
                       net_pnl > 0)
        except Exception as _bt_exc:
            log.debug("bayes_record_close_skipped",
                      trade_id=trade_id, error=str(_bt_exc)[:120])
        try:
            from signals.slot_selector import record_outcome as _bandit_rec
            _bandit_rec(trade.get("pair", ""),
                        trade.get("market_regime") or "unknown",
                        net_pnl > 0)
        except Exception as _bs_exc:
            log.debug("bandit_record_close_skipped",
                      trade_id=trade_id, error=str(_bs_exc)[:120])

        r.publish(redis_keys.CH_TRADE_CLOSED, json.dumps({
            "trade_id": trade_id, "net_pnl_usdt": round(net_pnl, 4), "reason": reason,
        }))
        log.info("paper_trade_closed", trade_id=trade_id, net_pnl=round(net_pnl, 4))

    def modify_sl(self, trade_id: str, new_sl_price: float,
                  force: bool = False) -> None:
        """N-04 / Blueprint 15.4: Update trailing SL.

        Default (force=False): monotonic guard — for longs new_sl must exceed
        current_sl; for shorts new_sl must be below current_sl (unless current
        is uninitialised at 0). This is the auto-trailing path that should
        never move SL *away* from price by accident.

        force=True bypasses the monotonic guard. Blueprint 15.4 explicitly
        lists "widen" as a Brain action — this is the only way to do it.
        Intended exclusively for Brain explicit interventions (volatility
        spike, news, regime change, etc). Auto-trailing callers always pass
        force=False (default). cont. 22d gap #6 fix.
        """
        trade = self._get_trade(trade_id)
        current_sl = float(trade.get("trailing_sl_level") or 0)
        direction = trade["direction"]

        if not force:
            if direction == "long" and new_sl_price <= current_sl:
                return
            if direction == "short" and new_sl_price >= current_sl and current_sl > 0:
                return

        write_trade_update(trade_id, {"trailing_sl_level": new_sl_price})
        # Blueprint Section 15.4: trailing SL move is a recorded Brain intervention.
        from memory.write import record_brain_intervention
        record_brain_intervention(trade_id, "sl_move", {
            "old_sl": round(current_sl, 8),
            "new_sl": round(new_sl_price, 8),
            "forced":  force,
        })
        redis_client.get().publish(redis_keys.CH_SL_MOVED, json.dumps({
            "trade_id": trade_id, "new_sl": new_sl_price, "forced": force,
        }))
        log.info("sl_moved", trade_id=trade_id, pair=trade.get("pair"),
                 direction=direction, old_sl=current_sl, new_sl=new_sl_price,
                 forced=force)

    def add_dca(self, trade_id: str, round_number: int) -> None:
        """N-04: Add DCA capital; update average_entry; deduct from virtual balance."""
        trade = self._get_trade(trade_id)
        r = redis_client.get()
        mark_price = float(r.get(redis_keys.MARK_PRICE.replace("{pair}", trade["pair"])) or 0)

        dca_capital = float(trade["capital_usdt"]) * 0.5
        # cont. 75 — atomic check-and-deduct (see _reserve_balance).
        if _reserve_balance(r, dca_capital) is None:
            raise ValueError("Insufficient virtual balance for DCA")

        orig_entry = float(trade["average_entry"] or trade["entry_price"])
        orig_qty = float(trade["quantity"])
        leverage = float(trade.get("leverage") or 1)

        dca_qty = (dca_capital * leverage) / mark_price
        new_qty = orig_qty + dca_qty

        # Units-weighted avg (NOT capital-weighted) so qty×(mark−avg) gives correct PnL at close.
        new_avg = (orig_qty * orig_entry + dca_qty * mark_price) / new_qty

        dca_key = f"dca{round_number}_price"
        raw_dca = trade.get("dca_status")
        dca_status = raw_dca if isinstance(raw_dca, dict) else (json.loads(raw_dca) if raw_dca else {})
        dca_status[f"round_{round_number}_triggered"] = True

        write_trade_update(trade_id, {
            dca_key: mark_price,
            "quantity": round(new_qty, 8),
            "average_entry": round(new_avg, 8),
            "dca_status": json.dumps(dca_status),
        })
        # Blueprint Section 15.4: DCA fire is a recorded Brain intervention.
        from memory.write import record_brain_intervention
        record_brain_intervention(trade_id, "dca_fire", {
            "round": round_number,
            "dca_price": round(mark_price, 8),
            "new_avg_entry": round(new_avg, 8),
            "added_capital_usdt": round(dca_capital, 4),
        })

        r.publish(redis_keys.CH_DCA_TRIGGERED, json.dumps({
            "trade_id": trade_id, "round": round_number,
            "dca_price": mark_price, "new_avg_entry": round(new_avg, 8),
        }))
        log.info("paper_dca_added", trade_id=trade_id, round=round_number, avg_entry=round(new_avg, 8))

    def close_partial(self, trade_id: str, qty_to_close: float,
                      reason: str) -> dict:
        """F48 §Idea B — Close a fraction of an open paper trade.

        Used for partial TP1 (half) then full TP2 (remainder). Behaviour:
          - Compute partial PnL on `qty_to_close` portion at current mark
          - Reduce trade.quantity by qty_to_close in the DB
          - Add partial PnL to virtual balance (minus partial fees)
          - Track cumulative partial state in Redis: trade:{id}:partial_pnl,
            trade:{id}:partial_fees, trade:{id}:partial_count
        """
        trade = self._get_trade(trade_id)
        r = redis_client.get()
        mark = float(r.get(redis_keys.MARK_PRICE.replace("{pair}", trade["pair"])) or 0)
        if mark <= 0:
            raise ValueError(f"No mark price for {trade['pair']}")

        full_qty = float(trade["quantity"])
        qty_close = min(float(qty_to_close), full_qty)
        if qty_close <= 0:
            return {"partial_pnl": 0.0, "remaining_qty": full_qty,
                    "fill_price": mark}

        entry = float(trade["average_entry"] or trade["entry_price"])
        direction_sign = 1.0 if trade["direction"] == "long" else -1.0
        leverage = float(trade.get("leverage") or 1)

        partial_pnl = (mark - entry) * qty_close * direction_sign
        # Approx fees: 0.04% of partial notional × leverage isn't right —
        # use the same formula the close uses: capital × leverage × 0.0004.
        # Partial fees scale with the fraction of qty closed.
        partial_fraction = qty_close / full_qty if full_qty > 0 else 0
        full_capital = float(trade["capital_usdt"])
        partial_fees = full_capital * partial_fraction * leverage * 0.0004
        partial_net = partial_pnl - partial_fees

        new_qty = full_qty - qty_close
        write_trade_update(trade_id, {"quantity": round(new_qty, 8)})

        # cont. 75 — atomic additive restore (see close_trade).
        r.incrbyfloat(_VIRTUAL_BALANCE_KEY, partial_net)

        # Accumulate partial state (TP2 close will add this to final PnL)
        try:
            r.incrbyfloat(f"trade:{trade_id}:partial_pnl_usdt", partial_net)
            r.incrbyfloat(f"trade:{trade_id}:partial_fees_usdt", partial_fees)
            r.incr(f"trade:{trade_id}:partial_count")
        except Exception:
            pass

        log.info("paper_partial_closed",
                 trade_id=trade_id, pair=trade["pair"],
                 qty_closed=round(qty_close, 8),
                 remaining=round(new_qty, 8),
                 partial_pnl=round(partial_pnl, 4),
                 partial_net=round(partial_net, 4),
                 reason=reason)
        return {"partial_pnl": round(partial_net, 4),
                "remaining_qty": round(new_qty, 8),
                "fill_price": mark}

    def _get_trade(self, trade_id: str) -> dict:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM trades WHERE id = %s", (trade_id,))
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"Trade {trade_id} not found")
                cols = [d[0] for d in cur.description]
                return dict(zip(cols, row))
