"""N-05 to N-08: Live execution engine — real Binance orders."""
import json
import time
from datetime import datetime, timezone
import structlog
import redis_keys
import redis_client
from execution.base import ExecutionEngine
from memory.write import write_trade_open, write_trade_update, write_trade_close
from db import db_conn

log = structlog.get_logger()


class LiveExecutionEngine(ExecutionEngine):

    def __init__(self, binance_client) -> None:
        self._client = binance_client

    def _resolve_fill_price(self, order: dict, pair: str) -> float:
        """Binance's futures_create_order response for MARKET orders often
        returns avgPrice=0 because the order is still ACK'd, not yet filled,
        when the call returns. Resolve the real fill price by:
          1. If response already has non-zero avgPrice, use it.
          2. Otherwise re-query the order by ID with a short retry loop.
          3. Final fallback: query the position's entryPrice.
        """
        try:
            ap = float(order.get("avgPrice") or 0)
            if ap > 0:
                return ap
        except (TypeError, ValueError):
            pass
        order_id = order.get("orderId")
        if order_id:
            for _ in range(5):
                time.sleep(0.2)
                try:
                    detail = self._client.get_order_status(pair, order_id)
                    ap = float(detail.get("avgPrice") or 0)
                    if ap > 0:
                        return ap
                    # Some responses report executedQty + cumQuote — derive avg
                    cum_q = float(detail.get("cumQuote") or 0)
                    exec_q = float(detail.get("executedQty") or 0)
                    if cum_q > 0 and exec_q > 0:
                        return cum_q / exec_q
                except Exception:
                    continue
        # Last-resort fallback: position entry price (works for OPEN only)
        try:
            pos = self._client.get_position(pair)
            ep = float(pos.get("entryPrice") or 0)
            if ep > 0:
                return ep
        except Exception:
            pass
        return 0.0

    def open_trade(self, params: dict) -> str:
        """N-05: Set leverage, place market order, wait for fill, write real
        fill price, and arm the exchange-native initial STOP_MARKET."""
        # cont. 69: set per-trade leverage on the exchange BEFORE ordering.
        # Without this, Binance uses the account's existing symbol leverage
        # (often 20x default), not the bot's intended leverage → wrong margin /
        # liquidation distance, possible margin-reject. Best-effort: a failure
        # here shouldn't block the trade (account default still applies).
        try:
            self._client.change_leverage(params["pair"], int(params.get("leverage", 5)))
        except Exception as _lev_exc:
            log.warning("live_set_leverage_failed", pair=params["pair"],
                        leverage=params.get("leverage"), error=str(_lev_exc)[:120])

        order = self._client.place_market_order(
            pair=params["pair"],
            side="BUY" if params["direction"] == "long" else "SELL",
            qty=params["quantity"],
        )
        fill_price = self._resolve_fill_price(order, params["pair"])
        fees = float(order.get("commission") or 0)

        if fill_price <= 0:
            # Hard fail — without a real entry price, trailing SL math is wrong.
            # Position exists on Binance though; raise so caller knows.
            raise RuntimeError(
                f"live_open_fill_price_unresolved pair={params['pair']} "
                f"order_id={order.get('orderId')} — manual reconciliation needed")

        params["entry_price"] = fill_price
        params["average_entry"] = fill_price
        params["is_paper"] = False

        trade_id = write_trade_open(params)

        # cont. 69: arm the initial exchange-native stop so the position is
        # protected even if the bot/host dies. Uses the initial SL the engine
        # computed (trailing_sl_level). Best-effort — the monitor's mark-based
        # close is still the primary trail driver via modify_sl().
        _init_sl = params.get("trailing_sl_level")
        if _init_sl:
            try:
                self._arm_stop(trade_id, params["pair"], params["direction"],
                               float(_init_sl))
            except Exception as _sl_exc:
                log.warning("live_initial_stop_failed", trade_id=trade_id,
                            pair=params["pair"], sl=_init_sl,
                            error=str(_sl_exc)[:120])

        redis_client.get().publish(redis_keys.CH_TRADE_OPENED, json.dumps({
            "trade_id": trade_id, "pair": params["pair"],
            "direction": params["direction"], "entry_price": fill_price,
        }))
        log.info("live_trade_opened", trade_id=trade_id, pair=params["pair"], fill=fill_price)
        return trade_id

    def _sl_order_key(self, trade_id: str) -> str:
        return f"trade:{trade_id}:sl_order_id"

    def _arm_stop(self, trade_id: str, pair: str, direction: str,
                  stop_price: float) -> None:
        """Cancel/replace the exchange-native STOP_MARKET for this trade. A long
        is protected by a SELL stop below price; a short by a BUY stop above. The
        prior stop's order id is tracked in Redis and cancelled before the new
        one is placed (closePosition stops are one-per-side, and we must move the
        trigger price). cont. 69."""
        r = redis_client.get()
        prev_id = r.get(self._sl_order_key(trade_id))
        if prev_id:
            try:
                self._client.cancel_order(pair, int(prev_id))
            except Exception as _c_exc:
                # Already filled/cancelled/gone — fine, just log at debug.
                log.debug("live_stop_cancel_skipped", trade_id=trade_id,
                          order_id=prev_id, error=str(_c_exc)[:100])
        stop_side = "SELL" if direction == "long" else "BUY"
        order = self._client.place_stop_market_order(
            pair=pair, side=stop_side, stop_price=stop_price)
        oid = order.get("orderId")
        if oid is not None:
            r.set(self._sl_order_key(trade_id), int(oid))
        log.info("live_stop_armed", trade_id=trade_id, pair=pair,
                 side=stop_side, stop_price=stop_price, order_id=oid)

    def close_trade(self, trade_id: str, reason: str) -> None:
        """N-06: Close position via Binance; write real exit price and fees.

        cont. 61 audit fix: passes reduce_only=True so a redundant close
        (race between SL hit + MTF reversal + frontier kill, etc.) fails
        safely on Binance instead of opening a reverse-direction position.
        """
        trade = self._get_trade(trade_id)
        # cont. 69: cancel the resting exchange-native stop first so it can't
        # fire a second reduce-only order against an already-flat position.
        try:
            _r0 = redis_client.get()
            _prev_sl = _r0.get(self._sl_order_key(trade_id))
            if _prev_sl:
                self._client.cancel_order(trade["pair"], int(_prev_sl))
                _r0.delete(self._sl_order_key(trade_id))
        except Exception as _csl_exc:
            log.debug("live_close_stop_cancel_skipped", trade_id=trade_id,
                      error=str(_csl_exc)[:100])
        close_side = "SELL" if trade["direction"] == "long" else "BUY"
        order = self._client.place_market_order(
            pair=trade["pair"], side=close_side, qty=float(trade["quantity"]),
            reduce_only=True,
        )
        exit_price = self._resolve_fill_price(order, trade["pair"])
        if exit_price <= 0:
            # Position is closed on Binance but we couldn't resolve the exit
            # price. Fall back to current mark so PnL math doesn't break — log
            # the discrepancy for manual reconciliation.
            import redis_client as _rc
            r = _rc.get()
            exit_price = float(r.get(redis_keys.MARK_PRICE.replace("{pair}", trade["pair"])) or 0)
            log.warning("live_close_fill_price_fallback_to_mark",
                        trade_id=trade_id, mark=exit_price,
                        order_id=order.get("orderId"))
        fees = float(order.get("commission") or 0)

        entry = float(trade["average_entry"] or trade["entry_price"])
        qty = float(trade["quantity"])
        direction_sign = 1.0 if trade["direction"] == "long" else -1.0
        final_pnl = (exit_price - entry) * qty * direction_sign
        net_pnl = final_pnl - fees

        # F48 §Idea B — aggregate any prior partial-close PnL into the
        # recorded final/net (same pattern as execution/paper.py).
        try:
            _r_tmp = redis_client.get()
            partial_pnl_acc = float(_r_tmp.get(f"trade:{trade_id}:partial_pnl_usdt") or 0)
            partial_fees_acc = float(_r_tmp.get(f"trade:{trade_id}:partial_fees_usdt") or 0)
            if abs(partial_pnl_acc) > 0 or abs(partial_fees_acc) > 0:
                final_pnl = final_pnl + (partial_pnl_acc + partial_fees_acc)
                fees     = fees + partial_fees_acc
                net_pnl  = net_pnl + partial_pnl_acc
                log.info("live_trade_close_with_partials",
                         trade_id=trade_id,
                         partial_net=round(partial_pnl_acc, 4),
                         partial_fees=round(partial_fees_acc, 4))
                _r_tmp.delete(f"trade:{trade_id}:partial_pnl_usdt",
                              f"trade:{trade_id}:partial_fees_usdt",
                              f"trade:{trade_id}:partial_count",
                              f"trade:{trade_id}:tp1_fired",
                              f"trade:{trade_id}:tp_fired")
        except Exception:
            pass

        now = datetime.now(timezone.utc)
        entry_time = trade.get("entry_time") or now
        hold_seconds = int((now - entry_time).total_seconds()) if hasattr(now - entry_time, "total_seconds") else 0

        # cont. 57 — Final-tick peak capture (same pattern as paper.py).
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
            log.debug("live_final_peak_capture_skipped",
                      trade_id=trade_id, error=str(_pp_exc)[:120])

        write_trade_close(trade_id, {
            "exit_price": exit_price,
            "exit_time": now,
            "exit_reason": reason,
            "hold_time_seconds": hold_seconds,
            "final_pnl_usdt": round(final_pnl, 4),
            "fees_usdt": round(fees, 4),
            "net_pnl_usdt": round(net_pnl, 4),
        })

        _r = redis_client.get()

        # cont. 53 — manual close: trade row was just purged by write_trade_close.
        # Skip all learner-pollution paths below (perf monitor, entry-timing
        # event, CH_TRADE_CLOSED publish).
        from execution.base import is_purge_reason as _is_purge
        if _is_purge(reason):
            log.info("live_trade_purged_manual",
                     trade_id=trade_id, net_pnl=round(net_pnl, 4), reason=reason)
            try:
                _r.delete(f"trade:{trade_id}:tp1", f"trade:{trade_id}:tp2",
                          f"trade:{trade_id}:tp",
                          f"trade:{trade_id}:mag1_pct", f"trade:{trade_id}:mag3_pct",
                          f"trade:{trade_id}:highest_high",
                          f"trade:{trade_id}:lowest_low")
            except Exception:
                pass
            return

        # F49 §Component 2 — Performance Monitor log_outcome.
        try:
            from ml.performance_monitor import log_outcome
            _direction_sign = 1.0 if trade["direction"] == "long" else -1.0
            _outcome = 1.0 if (exit_price - entry) * _direction_sign > 0 else 0.0
            for _model in ("candlenet_1m", "candlenet_5m", "candlenet_15m",
                           "direction_model"):
                _pred_raw = _r.get(f"trade:{trade_id}:pred_{_model}")
                if _pred_raw is None:
                    continue
                try:
                    _pred = float(_pred_raw)
                except (TypeError, ValueError):
                    continue
                if trade["direction"] == "short":
                    _pred = 1.0 - _pred
                log_outcome(_model, _pred, _outcome)
            _r.delete(
                f"trade:{trade_id}:pred_candlenet_1m",
                f"trade:{trade_id}:pred_candlenet_5m",
                f"trade:{trade_id}:pred_candlenet_15m",
                f"trade:{trade_id}:pred_direction_model",
            )
        except Exception:
            pass

        # F48 §Idea B — clear CandleNet TP keys for this trade (Redis cleanup)
        # F51d (cont. 51) — also clear Chandelier high/low watermark keys
        try:
            _r.delete(f"trade:{trade_id}:tp1", f"trade:{trade_id}:tp2",
                      f"trade:{trade_id}:tp",
                      f"trade:{trade_id}:mag1_pct", f"trade:{trade_id}:mag3_pct",
                      f"trade:{trade_id}:highest_high",
                      f"trade:{trade_id}:lowest_low")
        except Exception:
            pass

        # F48 §Idea C — Finalise entry-timing signal event (Rule 4 simplified
        # version — see execution/paper.py for the same logic + docstring).
        try:
            import json as _j
            from ml.entry_timing_agent import log_signal_event
            _raw = _r.get(f"trade:{trade_id}:signal_event_state")
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
                _r.delete(f"trade:{trade_id}:signal_event_state")
        except Exception:
            pass

        # Phase 2 + Phase 3 (cont. 63, 2026-05-29) — feed realised outcome
        # back to the Bayesian threshold tuner and Thompson sampling slot
        # selector. Mirror of execution/paper.py close path.
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

        _r.publish(redis_keys.CH_TRADE_CLOSED, json.dumps({
            "trade_id": trade_id, "net_pnl_usdt": round(net_pnl, 4), "reason": reason,
        }))
        log.info("live_trade_closed", trade_id=trade_id, net_pnl=round(net_pnl, 4))

    def modify_sl(self, trade_id: str, new_sl_price: float,
                  force: bool = False) -> None:
        """N-07 / Blueprint 15.4: Place/update SL order on Binance.

        `force=True` bypasses the monotonic guard so the Brain can widen the
        SL (move it away from price) per Blueprint 15.4's "tighten or widen"
        Brain action. cont. 22d gap #6 fix. Auto-trailing callers always
        pass force=False (default).
        """
        trade = self._get_trade(trade_id)
        current_sl = float(trade.get("trailing_sl_level") or 0)
        direction = trade["direction"]

        if not force:
            if direction == "long" and new_sl_price <= current_sl:
                return
            if direction == "short" and new_sl_price >= current_sl and current_sl > 0:
                return

        # cont. 69: was a plain GTC LIMIT at the SL price — a limit SELL below
        # market (long) / BUY above market (short) is immediately marketable, so
        # it closed the trade the instant the first trail fired, AND stacked an
        # order per trail. Now: cancel/replace an exchange-native STOP_MARKET so
        # it triggers only when price actually reaches the stop (true SL),
        # matching paper's mark-monitored close while adding crash protection.
        self._arm_stop(trade_id, trade["pair"], direction, new_sl_price)

        write_trade_update(trade_id, {"trailing_sl_level": new_sl_price})
        # Blueprint Section 15.4: record Brain intervention.
        from memory.write import record_brain_intervention
        record_brain_intervention(trade_id, "sl_move", {
            "old_sl": round(current_sl, 8),
            "new_sl": round(new_sl_price, 8),
            "forced": force,
        })
        redis_client.get().publish(redis_keys.CH_SL_MOVED, json.dumps({
            "trade_id": trade_id, "new_sl": new_sl_price, "forced": force,
        }))

    def add_dca(self, trade_id: str, round_number: int) -> None:
        """N-08: Place additional market order; update average_entry."""
        trade = self._get_trade(trade_id)
        dca_qty = float(trade["quantity"]) * 0.5

        order = self._client.place_market_order(
            pair=trade["pair"],
            side="BUY" if trade["direction"] == "long" else "SELL",
            qty=dca_qty,
        )
        dca_price = float(order.get("avgPrice") or order.get("price") or 0)
        dca_capital = dca_qty * dca_price

        orig_capital = float(trade["capital_usdt"])
        orig_entry = float(trade["average_entry"] or trade["entry_price"])
        new_avg = (orig_capital * orig_entry + dca_capital * dca_price) / (orig_capital + dca_capital)

        dca_key = f"dca{round_number}_price"
        import json as _json
        dca_status = _json.loads(trade.get("dca_status") or '{}')
        dca_status[f"round_{round_number}_triggered"] = True

        write_trade_update(trade_id, {
            dca_key: dca_price,
            "average_entry": round(new_avg, 8),
            "dca_status": _json.dumps(dca_status),
        })
        # Blueprint Section 15.4: record Brain intervention.
        from memory.write import record_brain_intervention
        record_brain_intervention(trade_id, "dca_fire", {
            "round": round_number,
            "dca_price": round(dca_price, 8),
            "new_avg_entry": round(new_avg, 8),
            "added_capital_usdt": round(dca_capital, 4),
        })

        redis_client.get().publish(redis_keys.CH_DCA_TRIGGERED, _json.dumps({
            "trade_id": trade_id, "round": round_number,
            "dca_price": dca_price, "new_avg_entry": round(new_avg, 8),
        }))

    def close_partial(self, trade_id: str, qty_to_close: float,
                      reason: str) -> dict:
        """F48 §Idea B — Close a fraction of an open live trade on Binance.

        Places an opposite-side market order for qty_to_close (which reduces
        but does not close the position on Binance). Updates trade.quantity
        in DB. Records partial PnL into Redis for the eventual close to
        aggregate.
        """
        trade = self._get_trade(trade_id)
        full_qty = float(trade["quantity"])
        qty_close = min(float(qty_to_close), full_qty)
        if qty_close <= 0:
            return {"partial_pnl": 0.0, "remaining_qty": full_qty, "fill_price": 0.0}

        close_side = "SELL" if trade["direction"] == "long" else "BUY"
        # cont. 61 audit fix: reduce_only=True so partial close can't
        # accidentally open a reverse-direction position if the parent
        # trade was already fully closed by a concurrent path.
        order = self._client.place_market_order(
            pair=trade["pair"], side=close_side, qty=qty_close,
            reduce_only=True,
        )
        fill_price = self._resolve_fill_price(order, trade["pair"])
        if fill_price <= 0:
            _r = redis_client.get()
            fill_price = float(_r.get(redis_keys.MARK_PRICE.replace("{pair}", trade["pair"])) or 0)
            log.warning("live_partial_close_fill_fallback_to_mark",
                        trade_id=trade_id, mark=fill_price)

        entry = float(trade["average_entry"] or trade["entry_price"])
        direction_sign = 1.0 if trade["direction"] == "long" else -1.0
        partial_pnl = (fill_price - entry) * qty_close * direction_sign
        # Real fees come from order.commission; approximate as 0.04% of notional
        fees = float(order.get("commission") or
                     (abs(qty_close * fill_price) * 0.0004))
        partial_net = partial_pnl - fees

        new_qty = full_qty - qty_close
        write_trade_update(trade_id, {"quantity": round(new_qty, 8)})

        _r = redis_client.get()
        try:
            _r.incrbyfloat(f"trade:{trade_id}:partial_pnl_usdt", partial_net)
            _r.incrbyfloat(f"trade:{trade_id}:partial_fees_usdt", fees)
            _r.incr(f"trade:{trade_id}:partial_count")
        except Exception:
            pass

        log.info("live_partial_closed",
                 trade_id=trade_id, pair=trade["pair"],
                 qty_closed=round(qty_close, 8),
                 remaining=round(new_qty, 8),
                 partial_pnl=round(partial_pnl, 4),
                 partial_net=round(partial_net, 4),
                 reason=reason)
        return {"partial_pnl": round(partial_net, 4),
                "remaining_qty": round(new_qty, 8),
                "fill_price": fill_price}

    def _get_trade(self, trade_id: str) -> dict:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM trades WHERE id = %s", (trade_id,))
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"Trade {trade_id} not found")
                cols = [d[0] for d in cur.description]
                return dict(zip(cols, row))
