"""M-01 to M-05: All trade, signal, and counterfactual write operations."""
import json
import uuid
from datetime import datetime, timezone
import structlog
from db import db_conn

log = structlog.get_logger()


def write_trade_open(params: dict) -> str:
    """M-01: Insert a new open trade. Returns trade_id."""
    required = [
        "pair", "direction", "strategy_id", "brain_stage", "is_paper",
        "entry_price", "quantity", "capital_usdt", "leverage",
    ]
    for field in required:
        if field not in params:
            raise ValueError(f"write_trade_open: missing required field '{field}'")

    trade_id = str(uuid.uuid4())

    # cont. 65f (tp_optimal_percentage Phase 1) — persist position_size_usdt
    # and capital_pct at open so per-capital-tier TP analysis is possible
    # without joining Redis state retroactively. Both columns were NULL on
    # all 2139 closed-24h trades pre-fix → blocked the capital-tiered TP
    # design until wired.
    _cap = float(params.get("capital_usdt") or 0)
    _lev = float(params.get("leverage") or 0)
    _position_size_usdt = _cap * _lev if _cap > 0 and _lev > 0 else None
    _capital_pct = None
    try:
        import redis_client as _rc_cp
        _balance = float(_rc_cp.get().get("account:balance_usdt") or 0)
        if _balance > 0 and _cap > 0:
            _capital_pct = _cap / _balance
    except Exception:
        _capital_pct = None

    # cont. 65g — wire predictions ↔ trades FK link. Read the
    # `predictions:{pair}` Redis hash (written by prediction/refresh_loop;
    # the same hash consumed by the signals/engine soft gate). Copy the
    # 6 predicted_* fields into the trades row and stash the prediction
    # id for the post-INSERT UPDATE that sets predictions.trade_id /
    # consumed_at. NULL-safe: when Redis hash is missing or unparseable,
    # all 6 columns insert as NULL and no UPDATE is attempted.
    _pred_id = None
    _pred_dir = None
    _pred_entry = None
    _pred_sl = None
    _pred_tp = None
    _pred_hold = None
    _pred_rr = None
    try:
        import redis_client as _rc_pred
        import json as _json_pred
        _raw = _rc_pred.get().get(f"predictions:{params['pair']}")
        if _raw is not None:
            _p = (_json_pred.loads(_raw)
                  if isinstance(_raw, (str, bytes)) else dict(_raw))
            if isinstance(_p, dict):
                _pred_id = _p.get("id")
                _pred_dir = _p.get("predicted_direction")
                _pred_hold = _p.get("predicted_hold_seconds")
                _pred_rr = _p.get("predicted_rr_p50")
                _mark = float(_p.get("mark_at_predict") or params["entry_price"])
                _dir_s = str(_pred_dir or "").lower()
                _sign = 1 if _dir_s == "long" else (-1 if _dir_s == "short" else 0)
                _e_bps = _p.get("predicted_entry_offset_bps")
                _s_bps = _p.get("predicted_sl_offset_bps")
                _t_bps = _p.get("predicted_tp_offset_bps")
                if _sign != 0 and _e_bps is not None:
                    _pred_entry = _mark * (1 + _sign * float(_e_bps) / 1e4)
                if _sign != 0 and _s_bps is not None:
                    _pred_sl = _mark * (1 - _sign * abs(float(_s_bps)) / 1e4)
                if _sign != 0 and _t_bps is not None:
                    _pred_tp = _mark * (1 + _sign * abs(float(_t_bps)) / 1e4)
    except Exception as _exc:
        log.warning("predict_link_redis_read_failed",
                    pair=params.get("pair"), error=str(_exc)[:120])

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO trades (
                    id, pair, direction, strategy_id, brain_stage, is_paper,
                    entry_price, entry_time, quantity, capital_usdt, leverage,
                    timeframe, market_regime, trailing_sl_level,
                    dca_status, average_entry, feature_vector,
                    trade_potential_score, direction_confidence,
                    position_size_usdt, capital_pct,
                    predicted_direction, predicted_entry,
                    predicted_sl, predicted_tp,
                    predicted_hold_seconds, predicted_rr,
                    signals_at_entry
                ) VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s,
                    %s
                )
            """, (
                trade_id,
                params["pair"],
                params["direction"],
                params.get("strategy_id"),
                params["brain_stage"],
                params["is_paper"],
                params["entry_price"],
                params.get("entry_time", datetime.now(timezone.utc)),
                params["quantity"],
                params["capital_usdt"],
                params["leverage"],
                params.get("timeframe"),
                params.get("market_regime"),
                params.get("trailing_sl_level"),
                params.get("dca_status", '{"round_1_triggered": false, "round_2_triggered": false}'),
                params.get("average_entry", params["entry_price"]),
                params.get("feature_vector"),
                params.get("trade_potential_score"),
                params.get("direction_confidence"),
                _position_size_usdt,
                _capital_pct,
                _pred_dir,
                _pred_entry,
                _pred_sl,
                _pred_tp,
                _pred_hold,
                _pred_rr,
                params.get("signals_at_entry"),
            ))
            if _pred_id is not None:
                try:
                    cur.execute("""
                        UPDATE predictions
                           SET trade_id = %s,
                               consumed_at = NOW()
                         WHERE id = %s AND trade_id IS NULL
                    """, (trade_id, _pred_id))
                except Exception as _exc:
                    log.warning("predict_link_update_failed",
                                trade_id=trade_id, pred_id=_pred_id,
                                error=str(_exc)[:120])
    log.info("trade_opened", trade_id=trade_id, pair=params["pair"], direction=params["direction"])

    # Blueprint F35: MemRL — generate embedding on every trade open for future memory retrieval
    try:
        from memory.embed import embed_trade
        embed_trade(trade_id, params)
    except Exception as exc:
        log.warning("embed_trade_failed", trade_id=trade_id, error=str(exc))

    # Blueprint F34 U-05: store world-model prediction so update_on_trade_close
    # at exit can do REAL online learning instead of the old hardcoded predicted=0.
    # 7-day TTL covers max realistic trade duration; key is deleted at close.
    try:
        from world_model.model import imagine_trajectory
        import redis_client as _rcwm
        import json as _jsonwm
        obs_wm = {
            "price":   float(params.get("entry_price") or 0),
            "volume":  float(params.get("capital_usdt") or 0),
            "lev":     float(params.get("leverage") or 1),
        }
        action_wm = "open_long" if params.get("direction") == "long" else "open_short"
        traj_wm = imagine_trajectory(action_wm, n_steps=5, initial_obs=obs_wm)
        _rcwm.get().setex(
            f"world_model:prediction:{trade_id}",
            7 * 86400,
            _jsonwm.dumps({
                "predicted_mean_pnl": traj_wm.get("mean_pnl", 0.0),
                "latent": traj_wm.get("latent", []),
                "action": action_wm,
                # Persist the raw entry obs so write_trade_close can also
                # train the RSSM core (encoder+GRU+prior+posterior) via
                # train_rssm_step — not just the reward head.
                "entry_obs": obs_wm,
                "capital_usdt": float(params.get("capital_usdt") or 0),
                "leverage":     float(params.get("leverage") or 1),
            }),
        )
    except Exception as exc:
        log.warning("world_model_prediction_store_failed", trade_id=trade_id, error=str(exc)[:120])

    return trade_id


def write_trade_update(trade_id: str, updates: dict) -> None:
    """M-02: Update mutable trade fields during the trade's life."""
    allowed = {
        "brain_actions", "trailing_sl_level", "dca_status", "dca1_price",
        "dca2_price", "average_entry", "quantity", "peak_pnl_usdt", "peak_loss_usdt",
        "brain_influenced", "intervention_count",
        "tp1_target", "tp2_target", "mag1_pct", "mag3_pct",
        "tp_target",  # Phase A write-through (cont. 55)
        # cont. 65k-5 — canonical TP columns the dashboard reads. They were NOT
        # whitelisted, so every open trade showed NULL tp1/tp2 on the dashboard
        # even though the engine wrote tp1_target + Redis. Add them so the
        # dashboard shows the real (capital-based 15%/30%) TPs.
        "tp1", "tp2", "tp",
        # cont. 63 (2026-05-29) — persist TP-fire events to DB so audits /
        # dashboards / F12 mismatch decoder / Bayesian threshold tuner can
        # see TP actuation. Without these, loss_audit §4 showed 0 / 1163
        # tp_fired rows in 24h even though Redis counters proved fires.
        "tp_fired", "tp1_fired",
    }
    fields = {k: v for k, v in updates.items() if k in allowed}
    if not fields:
        return
    set_clause = ", ".join(f"{k} = %s" for k in fields)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE trades SET {set_clause} WHERE id = %s",
                list(fields.values()) + [trade_id],
            )


def _purge_trade_row(trade_id: str, reason, net_pnl) -> None:
    """cont. 53 — delete a manually-closed trade row + FK refs so no learner
    ever sees it. Called from write_trade_close when exit_reason is a manual
    purge reason.

    FK cleanup (handled inside one transaction):
      - signals.trade_id           → SET NULL (nullable FK; orphan the signal row)
      - debate_arguments.trade_id  → SET NULL (nullable FK)
      - mismatches.loser/winner_trade_id  → DELETE (NOT NULL FK; row no longer meaningful)
      - trades.hedge_of_trade_id (self-ref)→ SET NULL on any hedge that referenced this trade
      - DELETE FROM trades WHERE id = trade_id
    Counters: `trade:purge:count` increments; `trade:purge:last_id` /
    `trade:purge:last_reason` capture the most recent purge for the dashboard.
    """
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE signals SET trade_id = NULL WHERE trade_id = %s",
                    (trade_id,),
                )
                cur.execute(
                    "UPDATE debate_arguments SET trade_id = NULL WHERE trade_id = %s",
                    (trade_id,),
                )
                # cont. 66 — predictions.trade_id was MISSING from this cleanup.
                # Its FK (predictions_trade_id_fkey) blocked DELETE FROM trades,
                # so manual_close_all purges silently failed and the trade stayed
                # status='open' forever (the "positions not closing" bug). Column
                # is nullable → SET NULL (preserve the prediction rows).
                cur.execute(
                    "UPDATE predictions SET trade_id = NULL WHERE trade_id = %s",
                    (trade_id,),
                )
                cur.execute(
                    "DELETE FROM mismatches "
                    "WHERE loser_trade_id = %s OR winner_trade_id = %s",
                    (trade_id, trade_id),
                )
                cur.execute(
                    "UPDATE trades SET hedge_of_trade_id = NULL "
                    "WHERE hedge_of_trade_id = %s",
                    (trade_id,),
                )
                cur.execute(
                    "DELETE FROM trades WHERE id = %s",
                    (trade_id,),
                )
        log.warning("trade_purged_manual_close",
                    trade_id=str(trade_id),
                    reason=str(reason),
                    net_pnl_usdt=net_pnl)
        # Telemetry counters (silent-rejection-rule shape — every skip emits a counter)
        try:
            import redis_client as _rc_purge
            _r_purge = _rc_purge.get()
            _r_purge.incr("trade:purge:count")
            _r_purge.set("trade:purge:last_id", str(trade_id))
            _r_purge.set("trade:purge:last_reason", str(reason or "unknown"))
            import time as _t_purge
            _r_purge.set("trade:purge:last_ts", str(int(_t_purge.time())))
        except Exception:
            pass
        # Ephemeral Redis state for this trade — best-effort cleanup of any
        # keys the close-engine cleanup might miss in the purge path.
        try:
            import redis_client as _rc_clean
            _r_clean = _rc_clean.get()
            _r_clean.delete(
                f"trade:{trade_id}:tp1",
                f"trade:{trade_id}:tp2",
                f"trade:{trade_id}:tp",          # Phase A write-through
                f"trade:{trade_id}:mag1_pct",
                f"trade:{trade_id}:mag3_pct",
                f"trade:{trade_id}:tp1_fired",
                f"trade:{trade_id}:tp_fired",    # Phase A write-through
                f"trade:{trade_id}:highest_high",
                f"trade:{trade_id}:lowest_low",
                f"trade:{trade_id}:partial_pnl_usdt",
                f"trade:{trade_id}:partial_fees_usdt",
                f"trade:{trade_id}:partial_count",
                f"trade:{trade_id}:closing",
                f"trade:{trade_id}:signal_event_state",
                f"trade:{trade_id}:pred_candlenet_1m",
                f"trade:{trade_id}:pred_candlenet_5m",
                f"trade:{trade_id}:pred_candlenet_15m",
                f"trade:{trade_id}:pred_direction_model",
                f"trail:lock_frac_used:{trade_id}",
                f"world_model:prediction:{trade_id}",
            )
        except Exception:
            pass
    except Exception as exc:
        log.error("trade_purge_failed",
                  trade_id=str(trade_id),
                  reason=str(reason),
                  error=str(exc)[:200])
        # cont. 66 — FALLBACK: a purge must never leave a trade stuck 'open'
        # (that is the exact symptom that froze live trading). If the hard
        # delete fails — e.g. a future FK child we haven't cleaned up — mark
        # the trade closed in a fresh transaction so the exit still takes
        # effect. exit_reason forced to 'manual_close_all' (a value allowed by
        # trades_exit_reason_check; raw purge reasons like 'force_close' are not).
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE trades SET status = 'closed', "
                        "exit_time = COALESCE(exit_time, NOW()), "
                        "exit_reason = 'manual_close_all', "
                        "net_pnl_usdt = COALESCE(net_pnl_usdt, %s) "
                        "WHERE id = %s AND status = 'open'",
                        (net_pnl, trade_id),
                    )
            log.warning("trade_purge_fallback_marked_closed",
                        trade_id=str(trade_id), reason=str(reason))
        except Exception as exc2:
            log.error("trade_purge_fallback_failed",
                      trade_id=str(trade_id), error=str(exc2)[:200])


def record_brain_intervention(trade_id: str, action_type: str, details: dict) -> None:
    """Blueprint Section 15.4 / 13 trades schema:
    Set brain_influenced=true, increment intervention_count, append to brain_actions JSONB.

    Called whenever the Brain modifies an open trade after entry — SL moves, DCA fires,
    hedge opens, etc. Without this, the dashboard's "Brain Influence" column on the
    Closed Trades table is always 'No' even when the Brain managed the entire trade.
    """
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action_type,
        **details,
    }
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE trades SET
                    brain_influenced  = TRUE,
                    intervention_count = COALESCE(intervention_count, 0) + 1,
                    brain_actions     = COALESCE(brain_actions, '[]'::jsonb) || %s::jsonb
                WHERE id = %s
                """,
                (json.dumps([entry]), trade_id),
            )


def write_trade_close(trade_id: str, exit_data: dict) -> None:
    """M-03: Write exit data and close the trade.

    cont. 53: when `exit_data["exit_reason"]` is a manual purge reason
    (see `execution.base.PURGE_REASONS` — `manual_close_all`, `force_close`,
    etc.), the trade is treated as a user action and deleted from the
    trades table entirely. All learner side-effects below are skipped so
    no feature is polluted by manually-closed trades.
    """
    required = ["exit_price", "exit_time", "exit_reason", "hold_time_seconds",
                "final_pnl_usdt", "fees_usdt", "net_pnl_usdt"]
    for field in required:
        if field not in exit_data:
            raise ValueError(f"write_trade_close: missing required field '{field}'")

    # cont. 53 — purge path. When exit_reason is a manual close, nuke the
    # trade row + FK references and skip ALL learner side effects below.
    # Done at the TOP of this function so no UPDATE-then-DELETE race can
    # leak the closed row to a learner that reads between the two.
    try:
        from execution.base import is_purge_reason
    except Exception:
        is_purge_reason = lambda _r: False
    if is_purge_reason(exit_data.get("exit_reason")):
        _purge_trade_row(trade_id, exit_data.get("exit_reason"),
                         exit_data.get("net_pnl_usdt"))
        return

    import json as _json
    diag_tags_raw = exit_data.get("diagnosis_tags") or []
    if not isinstance(diag_tags_raw, str):
        diag_tags_raw = _json.dumps(list(diag_tags_raw))
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE trades SET
                    status = 'closed',
                    exit_price = %s,
                    exit_time = %s,
                    exit_reason = %s,
                    hold_time_seconds = %s,
                    final_pnl_usdt = %s,
                    fees_usdt = %s,
                    net_pnl_usdt = %s,
                    failure_type = %s,
                    diagnosis_tags = %s::jsonb,
                    counterfactual_result = %s,
                    trade_quality_score = %s
                WHERE id = %s
            """, (
                exit_data["exit_price"],
                exit_data["exit_time"],
                exit_data["exit_reason"],
                exit_data["hold_time_seconds"],
                exit_data["final_pnl_usdt"],
                exit_data["fees_usdt"],
                exit_data["net_pnl_usdt"],
                exit_data.get("failure_type"),
                diag_tags_raw,
                exit_data.get("counterfactual_result"),
                exit_data.get("trade_quality_score"),
                trade_id,
            ))
    log.info("trade_closed", trade_id=trade_id, net_pnl=exit_data["net_pnl_usdt"])

    # F47 cont. 30 — feed the trailing-ratchet learner with this trade's
    # outcome. No-op when (a) F47 governance off, (b) no ratchet snapshot
    # was stamped (trade never hit ratchet activation), (c) peak_pnl ≤ 0.
    # Reads peak_pnl from the just-written DB row.
    try:
        from risk.trail_params import record_outcome as _tp_record
        _tp_peak = None
        with db_conn() as _tp_conn:
            with _tp_conn.cursor() as _tp_cur:
                _tp_cur.execute(
                    "SELECT peak_pnl_usdt FROM trades WHERE id = %s",
                    (trade_id,),
                )
                _tp_row = _tp_cur.fetchone()
                if _tp_row:
                    _tp_peak = _tp_row[0]
        _tp_record(
            str(trade_id),
            float(_tp_peak) if _tp_peak is not None else 0.0,
            float(exit_data.get("net_pnl_usdt", 0)),
        )
    except Exception as _tp_exc:
        log.warning("trail_params_record_failed",
                    trade_id=str(trade_id), error=str(_tp_exc)[:200])

    # OPRO (F39A / AB-01 to AB-04): increment window counter; every N trades, score the
    # window and either revert the prompt (regression) or save a new LLM candidate.
    # Gated on F30 governance flag so a bad OPRO loop can be globally disabled.
    try:
        from self_improve.opro import increment_opro_counter, trigger_opro_if_due, get_current_prompt
        from feature_governance.registry import is_active as _fg_active_opro
        import config as _cfg
        count = increment_opro_counter()
        window = getattr(_cfg.brain, "opro_window_size", 5)
        if trigger_opro_if_due(count, window) and _fg_active_opro("F39A"):
            from redis_client import get as _get_redis
            _r = _get_redis()
            # Dedup lock: only one opro task in queue at a time. TTL=600s covers
            # the worst-case 165s task + margin. Prevents the 1372-task pile-up
            # that starves sweep_pending_counterfactuals and other celery tasks.
            if not _r.set("opro:queued_lock", "1", nx=True, ex=600):
                log.info("opro_skipped_already_queued", trade_count=count)
            else:
                with db_conn() as _opro_conn:
                    with _opro_conn.cursor() as _opro_cur:
                        _opro_cur.execute("""
                            SELECT net_pnl_usdt, capital_usdt
                            FROM trades
                            WHERE status='closed' AND is_paper=true AND capital_usdt > 0
                            ORDER BY exit_time DESC
                            LIMIT %s
                        """, (window,))
                        _opro_rows = _opro_cur.fetchall()
                # Per-trade % returns: compute_opro_score expects these and divides by 100 internally.
                window_scores = [round(float(p) / float(c) * 100, 4) for p, c in _opro_rows]
                from celery_app import opro_optimize
                opro_optimize.apply_async(args=[get_current_prompt(), window_scores])
                log.info("opro_triggered", closed_trade_count=count, samples=len(window_scores))
    except Exception as exc:
        log.warning("opro_trigger_skipped", error=str(exc))

    # Direction Decoder (X-12): run post-mortem on every direction failure
    # Blueprint: analyse which signals at entry pointed to the correct direction
    if exit_data.get("failure_type") == "direction":
        try:
            _run_direction_decoder(trade_id, exit_data)
        except Exception as exc:
            log.warning("direction_decoder_skipped", trade_id=trade_id, error=str(exc))

    # Directional accuracy: count wins too — accuracy = correct / (correct + failures).
    # Cont. 40: failure_type='win' now replaces the old "None means win" sentinel.
    if exit_data.get("failure_type") == "win":
        try:
            _update_directional_accuracy_win(trade_id)
        except Exception as exc:
            log.warning("dir_accuracy_win_skipped", trade_id=trade_id, error=str(exc))

    # Strategy metrics: recompute win_rate, trade_count, avg_pnl from closed trades
    try:
        _update_strategy_metrics(trade_id)
    except Exception as exc:
        log.warning("strategy_metrics_skipped", trade_id=trade_id, error=str(exc))

    # Blueprint F34 U-05: real online learning — read the stored open-time prediction
    # and pass it (+ latent) to update_on_trade_close so the reward head actually
    # gets a backprop step from prediction error vs actual_pnl.
    try:
        from world_model.model import update_on_trade_close
        import redis_client as _rcwm2
        import json as _jsonwm2
        _r_wm = _rcwm2.get()
        _key_wm = f"world_model:prediction:{trade_id}"
        _raw_wm = _r_wm.get(_key_wm)
        if _raw_wm:
            _pred_wm = _jsonwm2.loads(_raw_wm)
            _actual_pnl = float(exit_data.get("net_pnl_usdt", 0))
            # Reward-head SGD step (existing path — unchanged).
            update_on_trade_close(
                predicted_outcome={"mean_pnl": _pred_wm.get("predicted_mean_pnl", 0.0)},
                actual_pnl=_actual_pnl,
                latent=_pred_wm.get("latent"),
            )
            # F34 RSSM-core training (added 2026-05-21 cont. 5). Needs the
            # raw entry obs that was persisted to the prediction key. If the
            # trade was opened before this fix, entry_obs will be missing and
            # we skip RSSM training silently — the reward-head SGD above is
            # the legacy fallback.
            try:
                _entry_obs = _pred_wm.get("entry_obs")
                if _entry_obs:
                    from world_model.model import train_rssm_step
                    _exit_obs = {
                        "price":   float(exit_data.get("exit_price") or 0),
                        "volume":  float(_pred_wm.get("capital_usdt") or 0),
                        "lev":     float(_pred_wm.get("leverage") or 1),
                    }
                    train_rssm_step(
                        entry_obs=_entry_obs,
                        action=_pred_wm.get("action", "hold"),
                        exit_obs=_exit_obs,
                        actual_pnl=_actual_pnl,
                    )
            except Exception as exc:
                log.warning("world_model_rssm_train_skipped",
                            trade_id=trade_id, error=str(exc)[:200])
            _r_wm.delete(_key_wm)
        else:
            # Trade opened before this fix (or TTL expired) — log-only fallback.
            update_on_trade_close(
                predicted_outcome={"mean_pnl": 0.0},
                actual_pnl=float(exit_data.get("net_pnl_usdt", 0)),
            )
    except Exception as exc:
        log.warning("world_model_update_skipped", error=str(exc))

    # Blueprint F44 — Brain-learned hedge parameters (D-08, PROGRESS cont. 10).
    # If the closed trade is a hedge (hedge_of_trade_id set), read the param
    # snapshot from its feature_vector and update each learned scalar via
    # constant-α MC. F44 governance gate.
    try:
        from feature_governance.registry import is_active as _fg_active_f44
        if _fg_active_f44("F44"):
            with db_conn() as _hconn:
                with _hconn.cursor() as _hcur:
                    _hcur.execute(
                        "SELECT hedge_of_trade_id, capital_usdt, feature_vector "
                        "FROM trades WHERE id = %s",
                        (trade_id,),
                    )
                    _hrow = _hcur.fetchone()
            if _hrow and _hrow[0] is not None:
                _h_capital = float(_hrow[1] or 0)
                _h_fv = _hrow[2]
                if isinstance(_h_fv, str):
                    try:
                        _h_fv = json.loads(_h_fv)
                    except Exception:
                        _h_fv = {}
                _h_params = (_h_fv.get("hedge_params_used")
                             if isinstance(_h_fv, dict) else None)
                if isinstance(_h_params, dict):
                    from risk.hedge_params import record_outcome
                    _h_pnl = float(exit_data.get("net_pnl_usdt", 0))
                    _h_res = record_outcome(_h_params, _h_pnl, _h_capital)
                    log.info("hedge_params_outcome_recorded",
                             trade_id=trade_id,
                             reward=_h_res.get("reward"),
                             updated=len(_h_res.get("updates") or []))
    except Exception as exc:
        log.warning("hedge_params_update_skipped", error=str(exc))

    # Blueprint F37 Verbal Reinforcement — read the persisted debate arguments
    # for this trade, compute was_correct per agent, update the per-agent
    # weight priors in Redis. Future debates' verdict synthesis will weight
    # Bull/Bear/Risk by their track record. Gated on F37 governance.
    try:
        from feature_governance.registry import is_active as _fg_active_f37vr
        if _fg_active_f37vr("F37"):
            from debate.council import update_beliefs_on_close
            # Look up the trade row to get won + capital
            with db_conn() as _vconn:
                with _vconn.cursor() as _vcur:
                    _vcur.execute(
                        "SELECT capital_usdt FROM trades WHERE id = %s",
                        (trade_id,),
                    )
                    _vrow = _vcur.fetchone()
            _v_cap = float(_vrow[0]) if _vrow and _vrow[0] is not None else 100.0
            _v_pnl = float(exit_data.get("net_pnl_usdt", 0))
            _v_won = _v_pnl > 0
            update_beliefs_on_close(trade_id, _v_won, _v_pnl, _v_cap)
    except Exception as exc:
        log.warning("debate_beliefs_update_skipped", error=str(exc))

    # cont. 72 — Debate REALIZED-outcome learning loop. Writes the
    # `debate:prior:regime:{regime}:{dir}` EWMA that debate.fallback reads as its
    # PRIMARY term (the cont.69 design left this loop unimplemented → the prior was
    # always empty). No LLM. Own flag (default on), independent of F37 governance so
    # the core fix runs even when the LLM council is deactivated. Best-effort.
    try:
        import redis_client as _rcprior
        if (_rcprior.get().get("debate:prior_learning_enabled") or "1") == "1":
            from debate.learning import update_outcome_prior
            with db_conn() as _pconn:
                with _pconn.cursor() as _pcur:
                    _pcur.execute(
                        "SELECT direction, market_regime, capital_usdt "
                        "FROM trades WHERE id = %s",
                        (trade_id,),
                    )
                    _prow = _pcur.fetchone()
            if _prow:
                _p_dir, _p_regime, _p_cap = _prow[0], _prow[1], _prow[2]
                _p_pnl = float(exit_data.get("net_pnl_usdt", 0))
                _p_capf = float(_p_cap) if _p_cap is not None else 100.0
                update_outcome_prior(_p_regime, _p_dir, _p_pnl, _p_capf)
    except Exception as exc:
        log.warning("debate_prior_learning_skipped", error=str(exc))

    # Blueprint F35 MemRL Phase 2 — constant-α MC Q-update from the closed trade.
    # The Q-table is consumed by memrl._phase2_quality_rerank to re-rank candidate
    # memories by learned quality rather than raw past PnL. Gated on F35 governance.
    try:
        from feature_governance.registry import is_active as _fg_active_f35
        if _fg_active_f35("F35"):
            from memory.cognitive.q_learning import update_q_from_trade
            with db_conn() as _qconn:
                with _qconn.cursor() as _qcur:
                    _qcur.execute(
                        "SELECT pair, direction, market_regime, "
                        "       trade_potential_score, direction_confidence "
                        "FROM trades WHERE id = %s",
                        (trade_id,),
                    )
                    _qrow = _qcur.fetchone()
            if _qrow:
                _qtrade = {
                    "pair": _qrow[0],
                    "direction": _qrow[1],
                    "market_regime": _qrow[2],
                    "trade_potential_score": _qrow[3],
                    "direction_confidence": _qrow[4],
                    "net_pnl_usdt": float(exit_data.get("net_pnl_usdt", 0)),
                }
                _qres = update_q_from_trade(_qtrade)
                log.info("q_learning_updated",
                         bucket=_qres.get("bucket"),
                         action=_qres.get("action"),
                         q_value=_qres.get("q_value"),
                         n_samples=_qres.get("n_samples"))
    except Exception as exc:
        log.warning("q_learning_update_skipped", error=str(exc))

    # Blueprint F36: Strategy Research Engine — trigger every 25 trades at 100+ trades.
    # F30 governance gate: skip when F36 deactivated.
    try:
        import redis_client as _rc4
        from feature_governance.registry import is_active as _fg_active_f36
        _pc4 = int(_rc4.get().get("brain:paper_closed") or 0)
        if _pc4 >= 100 and _pc4 % 25 == 0 and _fg_active_f36("F36"):
            from celery_app import run_strategy_research
            run_strategy_research.apply_async()
            log.info("strategy_research_triggered", trade_count=_pc4)
    except Exception as exc:
        log.warning("strategy_research_trigger_skipped", error=str(exc))

    # Blueprint F13 / X-09: retrain Direction Prediction Model every 50 trades at ≥100 closed
    try:
        import redis_client as _rcdm
        _pcdm = int(_rcdm.get().get("brain:paper_closed") or 0)
        if _pcdm >= 100 and _pcdm % 50 == 0:
            from celery_app import retrain_direction_model
            retrain_direction_model.apply_async()
            log.info("direction_model_retrain_triggered", trade_count=_pcdm)
    except Exception as exc:
        log.warning("direction_model_retrain_skipped", error=str(exc))

    # Blueprint F30: Feature Governance — update contribution scores for active features
    # Every active feature that COULD have influenced this trade gets a credit/blame.
    # Pair- and regime-specific histories are tracked separately for per-regime
    # deactivation per blueprint Section "Feature Contribution Scoring".
    try:
        from feature_governance.registry import update_contribution, _REGISTRY, is_active
        won = float(exit_data.get("net_pnl_usdt", 0)) > 0
        # Fetch pair + regime from the closed trade row
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pair, market_regime FROM trades WHERE id = %s", (trade_id,))
                row = cur.fetchone()
        pair = row[0] if row else "unknown"
        regime = row[1] if row and row[1] else "unknown"
        # Update every feature that is currently active
        for feature_id in list(_REGISTRY.keys()):
            if is_active(feature_id):
                update_contribution(feature_id, won, pair, regime)
                # Also track per-regime history for the temporary-vs-structural classifier
                try:
                    import json as _json
                    import redis_client as _rcg
                    rg = _rcg.get()
                    rkey = f"feature:{feature_id}:contribution:{regime}"
                    rhist = _json.loads(rg.get(rkey) or "[]")
                    rhist.append(1 if won else -1)
                    if len(rhist) > 100:
                        rhist = rhist[-100:]
                    rg.set(rkey, _json.dumps(rhist))
                except Exception:
                    pass
    except Exception as exc:
        log.warning("feature_governance_contribution_skipped", error=str(exc))

    # Blueprint F17 EWC + Experience Replay — buffer every closed trade so EWC
    # consolidation at 300+ trades has a representative reservoir of past outcomes.
    # F30 governance gate: skip when F17 deactivated.
    try:
        from feature_governance.registry import is_active as _fg_active_f17
        if _fg_active_f17("F17"):
            from ml.continual_learning import add_to_replay_buffer
            with db_conn() as _ewc_conn:
                with _ewc_conn.cursor() as _ewc_cur:
                    _ewc_cur.execute(
                        "SELECT pair, direction, market_regime, capital_usdt, "
                        "feature_vector, net_pnl_usdt, trade_quality_score "
                        "FROM trades WHERE id = %s",
                        (trade_id,),
                    )
                    _ewc_row = _ewc_cur.fetchone()
            if _ewc_row:
                _ewc_cols = ["pair", "direction", "market_regime", "capital_usdt",
                             "feature_vector", "net_pnl_usdt", "trade_quality_score"]
                add_to_replay_buffer(dict(zip(_ewc_cols, _ewc_row)))
    except Exception as exc:
        log.warning("ewc_replay_skipped", trade_id=trade_id, error=str(exc))

    # Trigger full governance check every 25 trades at 50+ trades
    try:
        import redis_client as _rc5
        _pc5 = int(_rc5.get().get("brain:paper_closed") or 0)
        if _pc5 >= 50 and _pc5 % 25 == 0:
            from celery_app import run_feature_governance_check
            run_feature_governance_check.apply_async()
            log.info("feature_governance_check_triggered", trade_count=_pc5)
    except Exception as exc:
        log.warning("feature_governance_trigger_skipped", error=str(exc))

    # Blueprint F31 / brain_state DB: sync actual trade statistics after every close
    try:
        _sync_brain_state()
    except Exception as exc:
        log.warning("brain_state_sync_skipped", error=str(exc))

    # Blueprint F43: Metacognitive Monitor — activates at 100+ closed trades.
    # F30 governance gate: skip when F43 deactivated.
    try:
        import redis_client as _rc2
        from feature_governance.registry import is_active as _fg_active_f43
        _pc = int(_rc2.get().get("brain:paper_closed") or 0)
        if _pc >= 100 and _fg_active_f43("F43"):
            from metacognition.monitor import update_competence_map, get_priority_learning_gap
            with db_conn() as _conn:
                with _conn.cursor() as _cur:
                    _cur.execute(
                        "SELECT pair, market_regime, timeframe FROM trades WHERE id = %s",
                        (trade_id,),
                    )
                    _row = _cur.fetchone()
            _trade_meta = {
                "pair": _row[0] if _row else "unknown",
                "market_regime": _row[1] if _row else "unknown",
                "timeframe": _row[2] if _row else "unknown",
                "net_pnl_usdt": exit_data.get("net_pnl_usdt", 0),
                "failure_type": exit_data.get("failure_type"),
            }
            update_competence_map(_trade_meta)
            get_priority_learning_gap(every_n_trades=50, trade_count=_pc)
    except Exception as exc:
        log.warning("metacog_update_skipped", trade_id=trade_id, error=str(exc))

    # Analytics: recompute all rolling metrics so dashboard shows live data after every close
    try:
        from analytics.metrics import update_all_metrics
        update_all_metrics()
    except Exception as exc:
        log.warning("analytics_update_skipped", error=str(exc))

    # Account risk: refresh paper-mode margin/exposure/unrealised values
    try:
        from account_risk.monitor import update_account_metrics
        update_account_metrics()
    except Exception as exc:
        log.warning("account_risk_update_skipped", error=str(exc))


def _run_direction_decoder(trade_id: str, exit_data: dict) -> None:
    """X-12: Direction Decoder post-mortem — runs after every direction failure.
    Blueprint Feature 13: analyse which signals at entry pointed to correct direction,
    which were ignored/underweighted. Store structured record in brain_actions.
    Feeds into Direction Prediction Model training at Stage 2.
    """
    import json as _json
    import redis_client as _rc

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT direction, entry_price, feature_vector, market_regime, pair FROM trades WHERE id = %s",
                (trade_id,),
            )
            row = cur.fetchone()
    if not row:
        return

    direction, entry_price, feature_vector, regime, pair = row
    exit_price = float(exit_data.get("exit_price") or 0)
    correct_direction = "short" if direction == "long" else "long"

    # Parse signal data available at entry time
    fv = feature_vector if isinstance(feature_vector, dict) else \
         (_json.loads(feature_vector) if feature_vector else {})
    ofi       = float(fv.get("ofi", 0))
    sentiment = float(fv.get("sentiment", 0.5))
    vpin      = float(fv.get("vpin", 0))

    # What each signal was indicating at entry
    ofi_said       = "long" if ofi > 0.0001 else "short" if ofi < -0.0001 else "neutral"
    sentiment_said = "long" if sentiment > 0.55 else "short" if sentiment < 0.45 else "neutral"
    # High VPIN = informed order flow = trend continuation signal (same as OFI direction)
    vpin_said      = ofi_said if vpin > 0.001 else "neutral"
    # Regime: bull = lean long, bear = lean short
    regime_said    = "long" if regime == "bull" else "short" if regime == "bear" else "neutral"

    signals_correct = []
    signals_wrong   = []
    for name, said in [("OFI", ofi_said), ("sentiment", sentiment_said),
                       ("VPIN", vpin_said), ("regime", regime_said)]:
        if said == "neutral":
            continue
        if said == correct_direction:
            signals_correct.append(name)
        else:
            signals_wrong.append(name)

    # Root cause classification
    if not signals_wrong and signals_correct:
        root_cause = "brain_ignored_correct_signals"
    elif not signals_correct and signals_wrong:
        root_cause = "all_signals_pointed_wrong"
    elif signals_correct and signals_wrong:
        root_cause = "conflicting_signals_wrong_choice"
    else:
        root_cause = "no_signal_data_available"

    decoded = {
        "type": "direction_failure_decoded",
        "correct_direction": correct_direction,
        "taken_direction": direction,
        "signals_at_entry": {
            "ofi": ofi, "ofi_said": ofi_said,
            "sentiment": sentiment, "sentiment_said": sentiment_said,
            "vpin": vpin, "vpin_said": vpin_said,
            "regime": regime, "regime_said": regime_said,
        },
        "signals_that_confirmed_correct": signals_correct,
        "signals_that_confirmed_wrong":   signals_wrong,
        "root_cause": root_cause,
        "lesson": (
            f"Went {direction} on {pair} but should have gone {correct_direction}. "
            f"Signals pointing correct direction: {signals_correct or ['none']}. "
            f"Signals pointing wrong direction: {signals_wrong or ['none']}. "
            f"Root cause: {root_cause}."
        ),
    }

    # Write to brain_actions on the trade
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE trades SET brain_actions = %s WHERE id = %s",
                (_json.dumps(decoded), trade_id),
            )

    # Update per-pair directional accuracy in Redis
    r = _rc.get()
    acc_key = f"brain:directional_accuracy:{pair}"
    raw = r.get(acc_key)
    stats = _json.loads(raw) if raw else {"total": 0, "correct": 0}
    stats["total"] += 1
    # direction failure = brain was wrong on this trade
    stats["rate"] = round(stats["correct"] / stats["total"] * 100, 2) if stats["total"] else 0
    r.set(acc_key, _json.dumps(stats))

    log.info("direction_decoded", trade_id=trade_id, pair=pair,
             root_cause=root_cause, correct_dir=correct_direction,
             signals_correct=signals_correct, signals_wrong=signals_wrong)


def _sync_brain_state() -> None:
    """Sync brain_state DB row with actual Redis + trade statistics after every close."""
    import redis_client as _rc
    r = _rc.get()
    paper_closed = int(r.get("brain:paper_closed") or 0)
    stage = int(r.get("brain:stage") or 1)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    ROUND(COUNT(*) FILTER (WHERE net_pnl_usdt > 0)::numeric
                          / NULLIF(COUNT(*), 0) * 100, 2)          AS win_rate,
                    ROUND(COUNT(*) FILTER (WHERE failure_type != 'direction')::numeric
                          / NULLIF(COUNT(*), 0) * 100, 2)          AS dir_accuracy,
                    ROUND(
                        COALESCE(
                            AVG(net_pnl_usdt / NULLIF(capital_usdt, 0))::numeric
                            / NULLIF(STDDEV(net_pnl_usdt / NULLIF(capital_usdt, 0))::numeric, 0)
                            * 19.1049::numeric,
                            0
                        ), 4
                    )                                               AS sharpe
                FROM trades WHERE status = 'closed' AND is_paper = true
            """)
            row = cur.fetchone()
            if not row:
                return
            total, win_rate, dir_accuracy, sharpe = row
            cur.execute("""
                UPDATE brain_state SET
                    paper_closed_trades  = %s,
                    total_closed_trades  = %s,
                    evolution_stage      = %s,
                    overall_win_rate     = %s,
                    directional_accuracy = %s,
                    rolling_sharpe       = %s,
                    updated_at           = NOW()
                WHERE id = 1
            """, (paper_closed, total, stage, win_rate, dir_accuracy, sharpe))


def _update_strategy_metrics(trade_id: str) -> None:
    """Recompute win_rate, trade_count, avg_pnl_usdt for the strategy used by this trade.
    Then call auto_retire_if_underperforming (F8 lifecycle: retire bad strategies)."""
    strategy_id = None
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT strategy_id FROM trades WHERE id = %s", (trade_id,))
            row = cur.fetchone()
            if not row or not row[0]:
                return
            strategy_id = row[0]
            cur.execute("""
                UPDATE strategies SET
                    trade_count    = sub.total,
                    win_rate       = sub.win_rate,
                    avg_pnl_usdt   = sub.avg_pnl
                FROM (
                    SELECT
                        COUNT(*)                                        AS total,
                        ROUND(AVG(net_pnl_usdt)::numeric, 4)           AS avg_pnl,
                        ROUND(
                            COUNT(*) FILTER (WHERE net_pnl_usdt > 0)::numeric
                            / NULLIF(COUNT(*), 0) * 100, 2
                        )                                               AS win_rate
                    FROM trades
                    WHERE strategy_id = %s AND status = 'closed'
                ) sub
                WHERE strategies.id = %s
            """, (strategy_id, strategy_id))

    # Blueprint F8: Strategy Lifecycle — auto-retire underperformers
    # Threshold: win_rate < 30% after 50+ trades on the strategy
    if strategy_id:
        try:
            from strategy.lifecycle import auto_retire_if_underperforming
            auto_retire_if_underperforming(str(strategy_id), threshold_win_rate=30.0)
        except Exception as exc:
            log.warning("auto_retire_skipped", strategy_id=str(strategy_id), error=str(exc))

    # Blueprint F8: Strategy Lifecycle — auto-promote experimental → active
    # Trigger: experimental status + S-07 trial eligibility (≥30 trades, ≥7 days)
    # + win_rate ≥ 50% + non-negative Sharpe. Symmetric to auto_retire above.
    if strategy_id:
        try:
            from strategy.lifecycle import check_trial_eligible, promote_to_active
            with db_conn() as _pconn:
                with _pconn.cursor() as _pcur:
                    _pcur.execute(
                        "SELECT status, win_rate, sharpe_ratio "
                        "FROM strategies WHERE id = %s",
                        (str(strategy_id),),
                    )
                    _prow = _pcur.fetchone()
            if _prow and _prow[0] == "experimental":
                _wr = float(_prow[1] or 0)
                _sh = float(_prow[2] or 0)
                eligible, _reason = check_trial_eligible(str(strategy_id))
                if eligible and _wr >= 50.0 and _sh >= 0.0:
                    promote_to_active(str(strategy_id))
                    log.info("strategy_auto_promoted",
                             strategy_id=str(strategy_id), win_rate=_wr, sharpe=_sh)
        except Exception as exc:
            log.warning("auto_promote_skipped",
                        strategy_id=str(strategy_id), error=str(exc)[:200])


def _update_directional_accuracy_win(trade_id: str) -> None:
    """Count winning trades in per-pair directional accuracy — wins = brain got direction right."""
    import json as _json
    import redis_client as _rc
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pair FROM trades WHERE id = %s", (trade_id,))
            row = cur.fetchone()
    if not row:
        return
    pair = row[0]
    r = _rc.get()
    acc_key = f"brain:directional_accuracy:{pair}"
    raw = r.get(acc_key)
    stats = _json.loads(raw) if raw else {"total": 0, "correct": 0}
    stats["total"] += 1
    stats["correct"] += 1
    stats["rate"] = round(stats["correct"] / stats["total"] * 100, 2) if stats["total"] else 0
    r.set(acc_key, _json.dumps(stats))


def write_signal(params: dict) -> str:
    """M-04: Log every signal (accepted AND rejected) immediately after decision.
    Persists debate_verdict, potential_score, direction_confidence, is_paper —
    previously dropped silently despite columns existing (mig 011) and being
    set by signals.engine."""
    required = ["pair", "direction", "accepted", "brain_stage"]
    for field in required:
        if field not in params:
            raise ValueError(f"write_signal: missing required field '{field}'")

    import config as _cfg
    signal_id = str(uuid.uuid4())
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO signals (
                    id, generated_at, pair, direction, timeframe, strategy_id,
                    accepted, rejection_reason, trade_id, feature_vector,
                    signal_strength, market_regime, brain_stage,
                    potential_score, direction_confidence, is_paper,
                    debate_verdict
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s)
            """, (
                signal_id,
                params.get("generated_at", datetime.now(timezone.utc)),
                params["pair"],
                params["direction"],
                params.get("timeframe"),
                params.get("strategy_id"),
                params["accepted"],
                params.get("rejection_reason"),
                params.get("trade_id"),
                params.get("feature_vector"),
                params.get("signal_strength"),
                params.get("market_regime"),
                params["brain_stage"],
                params.get("potential_score") or params.get("trade_potential"),
                params.get("direction_confidence"),
                params.get("is_paper", _cfg.TRADING_MODE == "paper"),
                params.get("debate_verdict"),
            ))
    return signal_id


def write_counterfactual(signal_id: str, data: dict) -> None:
    """M-05: Record 72-hour counterfactual outcome for a rejected signal."""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO counterfactuals (
                    signal_id, tracking_window_start, tracking_window_end,
                    peak_profit_pct, peak_loss_pct, trailing_sl_exit_pct, would_have_won,
                    miss_decode_reason
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                signal_id,
                data["tracking_window_start"],
                data["tracking_window_end"],
                data.get("peak_profit_pct"),
                data.get("peak_loss_pct"),
                data.get("trailing_sl_exit_pct"),
                data.get("would_have_won"),
                data.get("miss_decode_reason"),  # cont. 69s — carry stale/eval tag
            ))
