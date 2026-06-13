"""SciBrain opener — the Scientist OWNS the trade open (replaces the launch_pad funnel +
the engine's ~40-gate gauntlet for trade origination).

For each ranked pick from gate.funnel_pairs it:
  1. reads the live mark price,
  2. SIZES via the circuit's own Kelly `size_frac` (cont.71 L5: the funnel owns sizing):
         capital_usdt = clip(size_frac × pool, 5, max_position)
  3. picks leverage via the proven `risk.manager.assign_leverage`,
  4. sets the initial SL via the proven `risk.manager.compute_initial_sl`,
  5. quantity = capital × leverage / mark,
  6. calls `engine.open_trade(params)` (the same executor the engine uses), stamping the full
     decision snapshot + normalized influence manifest onto the trade,
  7. cooldowns the symbol + bumps counters.

`dry_run=True` does everything EXCEPT step 6 — it returns the planned opens so we can verify
sizing/SL on live data before any trade is placed.
Never raises into the engine; a per-pick failure is logged + counted and skipped.
"""
from __future__ import annotations

import json
import time

import structlog

import redis_keys
from . import gate
from . import keys as K

log = structlog.get_logger()

_DEF_MAX_POSITION = 200.0
_DEF_MAX_LEVERAGE = 5


def _f(r, key: str, default: float) -> float:
    try:
        v = r.get(key)
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _mark(r, pair: str) -> float:
    try:
        return float(r.get(redis_keys.MARK_PRICE.replace("{pair}", pair)) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _forecast_dir3(r, pair: str, tf: str):
    """CandleNet dir3 for one TF (for the open-trades CN-* columns). None if absent."""
    try:
        raw = r.get(f"{pair}:{tf}:candle_forecast")
        if not raw:
            return None
        return float(json.loads(raw).get("dir3"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _feature_vector(r, pair: str) -> dict:
    """Build the feature_vector the open-trades table reads for CN-5m/15m/30m/1h."""
    fv = {}
    for tf, col in (("5m", "cn_5m_dir3"), ("15m", "cn_15m_dir3"),
                    ("30m", "cn_30m_dir3"), ("1h", "cn_1h_dir3")):
        d3 = _forecast_dir3(r, pair, tf)
        if d3 is not None:
            fv[col] = round(d3, 6)
    return fv


def _plan_open(r, dec, brain_state: dict) -> dict | None:
    """Compute the full open parameters for one pick. None if not openable."""
    from risk.manager import assign_leverage, compute_initial_sl, _volatility_unit

    pair, direction = dec.symbol, dec.direction
    mark = _mark(r, pair)
    if mark <= 0:
        log.debug("scibrain_open_skip", pair=pair, reason="no_mark")
        try: r.incr("scibrain:open:reject:no_mark")
        except Exception: pass
        return None

    # SIZE within the USER's configured position caps (owner: "follow the max cap and min
    # cap set by me"). Reads the SAME canonical keys the rest of the bot uses
    # (bot:min/max_position_usdt). Conviction scales the size within [min_cap, max_cap].
    # Bounded by free balance; a trade must meet the min cap, else WAIT (don't under-size).
    available = _f(r, redis_keys.VIRTUAL_BALANCE, 0.0)
    max_cap = _f(r, "bot:max_position_usdt", _DEF_MAX_POSITION)
    min_cap = min(_f(r, "bot:min_position_usdt", 0.0), max_cap)
    floor = max(min_cap, 5.0)
    if available < floor:
        log.info("scibrain_open_skip", pair=pair, reason="below_min_cap",
                 available=round(available, 2), min_cap=min_cap)
        try: r.incr("scibrain:open:reject:below_min_cap")
        except Exception: pass
        return None
    # Cerebellum CALIBRATION head (Phase-7f task-8 — FIRST limited canary authority, §3.8/§5.5 step-12):
    # recalibrate conviction against realized win/loss. Always shadow-computed (shown on the open-trades
    # table); APPLIED to size+leverage only when armed (owner flag scibrain:cerebellum:canary, default ON)
    # AND earned out-of-sample. Bounded (±CALIB_CAP), reversible, never raises (Rule 21).
    try:
        from . import cerebellum as _cer
        cer = _cer.cerebellum_decision(r, dec.conviction, regime=dec.regime)
    except Exception:
        cer = None
    eff_conviction = cer["adjusted"] if (cer and cer.get("applied")) else dec.conviction

    # conviction (0..1) scales the size across the user's band, capped by free balance
    target = min_cap + max(0.0, max_cap - min_cap) * eff_conviction
    capital_usdt = round(max(min_cap, min(target, max_cap, available)), 2)

    vol_unit = _volatility_unit(r, pair)
    leverage = assign_leverage(eff_conviction * 100.0, vol_unit)
    max_lev = int(_f(r, K.MAX_LEVERAGE, _DEF_MAX_LEVERAGE))
    leverage = max(1, min(int(leverage), max_lev))

    try:
        initial_sl = compute_initial_sl(pair, direction)
    except Exception as exc:
        log.warning("scibrain_open_sl_failed", pair=pair, error=str(exc)[:120])
        initial_sl = None

    quantity = round((capital_usdt * leverage) / mark, 8)
    if quantity <= 0:
        return None

    decision_snapshot = dec.to_dict()
    influence_manifest = decision_snapshot.pop("influence_manifest")
    # Phase 7a — immutable EntrySnapshot: replay fingerprints + version lineage + action
    # propensities + execution micro-state, frozen at open (design §5.1). Best-effort; never blocks.
    try:
        from . import snapshot as _snap
        entry_snapshot = _snap.build_entry_snapshot(r, dec, mark)
    except Exception as _sexc:
        log.warning("scibrain_entry_snapshot_skip", pair=pair, error=str(_sexc)[:140])
        entry_snapshot = {"complete": False, "error": str(_sexc)[:140]}
    provenance = {
        "schema_version": 3,
        "origin": "scibrain", "primary_driver": dec.primary_driver,
        "regime": dec.regime, "conviction": round(dec.conviction, 4),
        "size_frac": round(dec.size_frac, 4), "attribution": dec.attribution,
        "router": dec.router,
        # Phase-7f task-8: bounded cerebellar calibration adjustment, frozen at open (base→adjusted,
        # applied?). Surfaced on the open-trades table so the canary's effect is observable (Rule 21).
        "cerebellum": cer,
        "influence_manifest": influence_manifest,
        "decision_snapshot": decision_snapshot,
        "entry_snapshot": entry_snapshot,
        # Phase 7a — freeze the INITIAL risk policy (the trailing_sl_level column is mutated by
        # trailing during the trade, so the row no longer carries the entry SL). The path-aware
        # Counterfactual Twin needs the exact entry SL/leverage to replay actual/opposite policies.
        "entry_policy": {
            "initial_sl": initial_sl,
            "sl_source": "compute_initial_sl",
            "leverage": leverage,
            "entry_price": mark,
        },
    }
    params = {
        "pair": pair, "direction": direction,
        "brain_stage": brain_state.get("stage", 1),
        "capital_usdt": capital_usdt, "leverage": leverage, "quantity": quantity,
        "strategy_id": brain_state.get("active_strategy_id"),
        "timeframe": "scibrain", "market_regime": dec.regime,
        "trade_potential_score": round(dec.conviction * 100.0, 2),
        "direction_confidence": round(dec.conviction * 100.0, 2),
        "trailing_sl_level": initial_sl, "average_entry": None,
        # cont. 75 — write_trade_open passes feature_vector straight into the SQL
        # insert (memory/write.py), so it MUST be a JSON string, not a raw dict
        # (psycopg2 raises "can't adapt type 'dict'" otherwise → open fails,
        # opened=0). Mirror the legacy engine path (engine.py:1247 json.dumps).
        # Empty (no CandleNet forecasts) → None, same as before.
        "feature_vector": (json.dumps(_fv) if (_fv := _feature_vector(r, pair)) else None),
        "signals_at_entry": json.dumps(provenance),
        "mark": mark,
    }
    return params


def _finalize_open(r, trade_id, pair: str, direction: str, mark: float,
                   leverage: int) -> None:
    """Post-open: fill the columns the engine fills downstream — TP1/TP2 (reusing the
    proven compute_tp_targets) and the pattern cluster id — so scibrain trades show the
    same complete row as engine trades. Best-effort; never unwinds an open."""
    try:
        from risk.manager import compute_tp_targets
        tps = compute_tp_targets(pair, direction, entry_price=mark, leverage=leverage)
        if tps:
            pipe = r.pipeline()
            pipe.set(f"trade:{trade_id}:tp1", tps["tp1"])
            pipe.set(f"trade:{trade_id}:tp2", tps["tp2"])
            pipe.set(f"trade:{trade_id}:tp", tps.get("tp", tps["tp1"]))
            pipe.set(f"trade:{trade_id}:mag1_pct", tps["mag1_pct"])
            pipe.set(f"trade:{trade_id}:mag3_pct", tps["mag3_pct"])
            pipe.execute()
            from memory.write import write_trade_update
            write_trade_update(str(trade_id), {
                "tp1": tps["tp1"], "tp2": tps["tp2"], "tp": tps.get("tp", tps["tp1"]),
                "tp1_target": tps["tp1"], "tp2_target": tps["tp2"],
                "tp_target": tps.get("tp", tps["tp1"]),
                "mag1_pct": tps["mag1_pct"], "mag3_pct": tps["mag3_pct"],
            })
    except Exception as exc:
        log.warning("scibrain_tp_failed", pair=pair, error=str(exc)[:140])

    # pattern_cluster_id is not in write_trade_update's whitelist → direct UPDATE.
    try:
        cid = r.get(f"pattern:cluster_id:{pair}")
        if cid is not None:
            from db import db_conn
            with db_conn() as conn, conn.cursor() as cur:
                cur.execute("UPDATE trades SET pattern_cluster_id=%s WHERE id=%s",
                            (int(float(cid)), str(trade_id)))
    except Exception as exc:
        log.debug("scibrain_cluster_set_failed", pair=pair, error=str(exc)[:120])


def run_open(r, engine, brain_state: dict, *, dry_run: bool = False) -> list:
    """Score the universe, pick, and open (or plan) trades. Returns opened trade ids
    (or planned param dicts when dry_run). Never raises into the engine."""
    picks = gate.funnel_pairs(r)
    if picks is None:
        return []          # kill switch off
    if not picks:
        log.info("scibrain_open_wait", reason="no_qualified_picks")
        return []

    from memory.query import get_open_trades
    max_open = int(r.get("bot:max_open_trades") or 999)
    try:
        max_per_pair = int(r.get("risk:max_open_per_pair") or 1)
    except (TypeError, ValueError):
        max_per_pair = 1

    open_trades = get_open_trades()
    open_per_pair: dict[str, int] = {}
    for ot in open_trades:
        p = ot.get("pair")
        open_per_pair[p] = open_per_pair.get(p, 0) + 1
    n_open = len(open_trades)

    results = []
    for dec in picks:
        if n_open >= max_open:
            break
        if open_per_pair.get(dec.symbol, 0) >= max_per_pair:
            try: r.incr("scibrain:open:reject:dup_pair")
            except Exception: pass
            continue
        # Cerebellum TIMING head (Phase-7f task-8): bounded one-cycle entry-timing deferral when
        # armed+earned and adverse-fill prob is high. Hard anti-starvation cap (≤1 skip/symbol) so it
        # can never starve a pick. Not applied on dry-run planning. Never raises (Rule 21).
        if not dry_run:
            try:
                from . import cerebellum as _cer
                if _cer.timing_should_wait(r, dec.symbol):
                    log.info("scibrain_open_defer", pair=dec.symbol, reason="cerebellum_timing")
                    continue
            except Exception:
                pass
        params = _plan_open(r, dec, brain_state)
        if params is None:
            continue

        if dry_run:
            results.append(params)
            log.info("scibrain_open_DRYRUN", pair=dec.symbol, direction=dec.direction,
                     capital=params["capital_usdt"], leverage=params["leverage"],
                     qty=params["quantity"], sl=params["trailing_sl_level"],
                     mark=params["mark"], driver=dec.primary_driver,
                     conviction=round(dec.conviction, 3))
            continue

        # real open (PAPER executor)
        mark = params.get("mark")
        try:
            params.pop("mark", None)
            trade_id = engine.open_trade(params)
        except ValueError as exc:           # e.g. insufficient virtual balance
            log.info("scibrain_open_rejected", pair=dec.symbol, error=str(exc)[:140])
            try: r.incr("scibrain:open:reject:executor")
            except Exception: pass
            continue
        except Exception as exc:
            log.warning("scibrain_open_error", pair=dec.symbol, error=str(exc)[:160])
            # cont. 75 — Rule 12: this path had a log but NO counter, so the
            # "can't adapt type 'dict'" open failures were invisible to the
            # reject counters (opened=0 with no reject-reason attribution).
            try: r.incr("scibrain:open:reject:error")
            except Exception: pass
            continue

        # fill TP1/TP2 + cluster id so the open-trades row is complete (like engine trades)
        _finalize_open(r, trade_id, dec.symbol, dec.direction, mark, params["leverage"])

        results.append(trade_id)
        n_open += 1
        open_per_pair[dec.symbol] = open_per_pair.get(dec.symbol, 0) + 1
        gate.cooldown(r, dec.symbol)
        try:
            r.incr(K.OPEN_COUNT)
            r.setex(K.sym_key(K.REASONING, dec.symbol) + ":opened", K.HOT_TTL,
                    json.dumps({"trade_id": str(trade_id), "ts": time.time()}))
        except Exception:
            pass
        # Phase 4 — auto-interrogate this OPENED trade out-of-band (the EDENUSDT post-mortem,
        # automated). Enqueue a snapshot; the celery audit-drain does the slow Ollama work so
        # the brain hot loop never blocks. Best-effort: never unwinds the open.
        try:
            from . import audit as _audit
            _audit.enqueue_open(r, trade_id, dec)
        except Exception as _aexc:
            log.debug("scibrain_audit_enqueue_skip", pair=dec.symbol, error=str(_aexc)[:120])
        log.info("scibrain_open", pair=dec.symbol, direction=dec.direction,
                 trade_id=trade_id, capital=params["capital_usdt"],
                 leverage=params["leverage"], driver=dec.primary_driver,
                 conviction=round(dec.conviction, 3))

    return results
