"""SciBrain Phase 7a — bounded LifeTrace + OutcomePacket on close (the outcome-truth ledger).

The EntrySnapshot (snapshot.py) freezes WHY a trade was opened. This module freezes WHAT ACTUALLY
HAPPENED to it, so the later Ex-post Outcome Causal Audit and the path-aware Counterfactual Twin
have a trustworthy, immutable record of realized truth — not a re-derived guess (living_intelligence
§5.2/§5.3). Two artifacts per closed scibrain trade, persisted onto the immutable trade row:

  • LifeTrace (§5.2) — the bounded discrete events through the position that the bot ACTUALLY
    records: entry, DCA fills, TP fires, trailing-SL level, brain interventions, the realized
    MFE/MAE excursion envelope, and the exit. Per-tick mark/candle path is NOT persisted per trade,
    so it is honestly reported as unavailable rather than fabricated (C2/C11).

  • OutcomePacket (§5.3) — realized multi-objective utility, return on capital, drawdown, hold
    time, exit reason, the existing direction/signal failure label, AND standardized forward
    horizons: the signed direction-correctness of the DECISION at fixed times after entry
    (independent of how we actually exited), computed from the live candle history. Horizons that
    have not matured yet are filled on a later harvest pass; the path-aware counterfactual utilities
    are explicitly left pending (the digital twin is a later Phase-7a task) instead of faked.

Architecture: a celery-beat task drains this idempotently (mirrors audit.grade_calibration) — pure
DB/Redis, no LLM. The per-trade artifacts are written immutably onto signals_at_entry (durable
ledger AND idempotency marker); the aggregate is RECOMPUTED from those rows, so a dropped/duplicate
beat is harmless and restart-safe by construction. Building on CH_TRADE_CLOSED via a beat scan
(rather than a live subscriber) is also what lets the standardized horizons mature and backfill —
a synchronous on-close handler could never observe a +4h forward return.
"""
from __future__ import annotations

import json
import time
from datetime import datetime

import structlog

from . import keys as K
from .audit import _merge_trade_provenance   # reuse the immutable-merge writer (no duplication)
from .sensor_bus import _candles             # reuse the exact candle reader the circuit scored on

log = structlog.get_logger()

OUTCOME_SCHEMA_VERSION = 1

# Standardized forward horizons (minutes after ENTRY) at which the decision's direction-correctness
# is graded — anchored to entry, NOT to our exit, so the decision edge is comparable across trades
# regardless of how each was actually managed (living_intelligence §5.3). Config-overridable (C6).
_DEFAULT_HORIZONS_MIN = (15, 60, 240)

# Multi-objective realized-utility weights (living_intelligence §6). Defaults are explicit here and
# overridable via Redis (the scibrain package is Redis-config-driven; see keys.py). C6.
_DEFAULT_LAMBDAS = {"tail": 0.5, "dd": 0.25, "cost": 1.0}

# Candle timeframes searched (finest→coarsest) for a forward price at a target timestamp. Finest TF
# that covers the target wins (best temporal resolution); coarser TFs hold more history (the 1h list
# spans ~12 days, so day-scale horizons resolve). tf → milliseconds per bar.
_TF_MS = (("1m", 60_000), ("5m", 300_000), ("15m", 900_000), ("1h", 3_600_000))


def _f(v, default=None):
    """Decimal/str/None → float (psycopg2 returns NUMERIC as Decimal)."""
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _epoch_ms(v) -> float | None:
    """PG TIMESTAMPTZ (datetime) / epoch number → epoch MILLISECONDS (candle t is ms)."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.timestamp() * 1000.0
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    # tolerate seconds vs ms input: treat < 1e12 as seconds
    return x * 1000.0 if x < 1e12 else x


def _jload(v, default):
    """jsonb column → python; psycopg2 may hand back a parsed obj OR a str."""
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def _prov_for_twin(row: dict) -> dict:
    """The entry provenance (signals_at_entry) the twin reads for the frozen entry_policy."""
    return _jload(row.get("signals_at_entry"), {}) or {}


def _horizons_min(r) -> tuple[int, ...]:
    try:
        raw = r.get(K.OUTCOME_HORIZONS_MIN)
        if raw:
            hs = tuple(sorted({int(x) for x in str(raw).split(",") if x.strip()}))
            if hs:
                return hs
    except (TypeError, ValueError):
        pass
    return _DEFAULT_HORIZONS_MIN


def _lambdas(r) -> dict:
    out = dict(_DEFAULT_LAMBDAS)
    for name, key in (("tail", K.UTIL_LAMBDA_TAIL), ("dd", K.UTIL_LAMBDA_DD),
                      ("cost", K.UTIL_LAMBDA_COST)):
        try:
            v = r.get(key)
            if v is not None:
                out[name] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def _price_at(r, pair: str, target_ms: float):
    """Close price of the candle covering `target_ms`, from the live candle lists.

    Returns (price, tf_used). Iterates finest→coarsest and returns the first TF whose 200-bar
    window contains the target (finest = best resolution). (None, None) if no loaded TF spans it
    (target older than the longest window, or no candles)."""
    for tf, tf_ms in _TF_MS:
        arr = _candles(r, pair, tf)               # (N,6) oldest-first, col0=t(ms), col4=close
        n = len(arr)
        if n == 0:
            continue
        first_t = float(arr[0, 0])
        last_t = float(arr[-1, 0])
        if target_ms < first_t or target_ms >= last_t + tf_ms:
            continue                              # outside this TF's window; try a coarser/longer one
        # find the bar i whose [t, t+tf) contains target (last bar with t <= target)
        idx = 0
        for i in range(n):
            if float(arr[i, 0]) <= target_ms:
                idx = i
            else:
                break
        return float(arr[idx, 4]), tf
    return None, None


def _forward_horizons(r, pair: str, entry_ms: float, entry_price: float, direction: str,
                      now_ms: float, horizons_min) -> dict:
    """Signed direction-correctness of the decision at each standardized horizon after entry.

    For horizon h: target = entry + h. If not yet matured (target > now) → pending (filled later).
    If matured: look up the forward price; dir_ret = +fwd_ret for a long, −fwd_ret for a short, so
    dir_ret > 0 ⇔ the chosen DIRECTION was right at that horizon. If matured but the candle history
    no longer reaches back to the target, it is permanently `out_of_window` (honest unavailable, not
    a guess). `complete` is True once every horizon is resolved (matured-with-price OR matured-but-
    out-of-window) — i.e. only NOT-YET-matured horizons keep it open for a rebuild."""
    out: dict = {}
    all_resolved = True
    sign = 1.0 if direction == "long" else -1.0
    for h in horizons_min:
        target = entry_ms + h * 60_000.0
        matured = target <= now_ms
        rec: dict = {"matured": matured}
        if not matured:
            rec["pending"] = True
            all_resolved = False
        else:
            price, tf = _price_at(r, pair, target)
            if price is None or not entry_price:
                rec["available"] = False
                rec["reason"] = "out_of_window"   # matured but history no longer spans it — resolved
            else:
                fwd = (price - entry_price) / entry_price
                rec.update({
                    "available": True, "tf": tf,
                    "price": round(price, 10),
                    "fwd_ret": round(fwd, 6),
                    "dir_ret": round(sign * fwd, 6),
                    "dir_correct": bool(sign * fwd > 0),
                })
        out[str(h)] = rec
    out["complete"] = all_resolved
    return out


def _utility(roc: float, mae_frac: float, cost_frac: float, lam: dict) -> dict:
    """Realized multi-objective utility (living_intelligence §6):
        U = roc − λ_tail·tail_loss − λ_dd·drawdown − λ_cost·cost
    tail_loss = max(0, −roc) (realized downside), drawdown = |MAE|/capital (worst unrealized dip),
    cost = fees/capital. Turnover is omitted (undefined per single trade). All terms are real,
    config-weighted, and reported alongside U so the score is auditable, never a black box."""
    tail = max(0.0, -roc)
    u = (roc
         - lam["tail"] * tail
         - lam["dd"] * mae_frac
         - lam["cost"] * cost_frac)
    return {
        "utility": round(u, 6),
        "terms": {
            "roc": round(roc, 6),
            "tail_loss": round(tail, 6),
            "drawdown": round(mae_frac, 6),
            "cost": round(cost_frac, 6),
        },
        "lambdas": {k: round(v, 4) for k, v in lam.items()},
    }


def build_lifetrace(row: dict) -> dict:
    """Bounded discrete events through the position, from what the bot ACTUALLY records.

    Honest by construction (C2/C11): the per-tick mark/candle path and per-fill execution stream are
    NOT persisted per trade, so they are reported as unavailable — only the discrete recorded events
    (entry, DCA, TP, SL, brain interventions, the MFE/MAE envelope, exit) are emitted."""
    events: list[dict] = []
    cap = _f(row.get("capital_usdt")) or 0.0

    entry_ts = row.get("entry_time")
    events.append({
        "kind": "entry",
        "ts": (entry_ts.isoformat() if isinstance(entry_ts, datetime) else entry_ts),
        "price": _f(row.get("entry_price")),
        "qty": _f(row.get("quantity")),
        "capital_usdt": cap,
        "leverage": row.get("leverage"),
        "direction": row.get("direction"),
    })

    # DCA fills (averaged-in adds) — recorded as discrete triggers + their prices
    dca = _jload(row.get("dca_status"), {}) or {}
    for rnd, price_col in (("round_1_triggered", "dca1_price"), ("round_2_triggered", "dca2_price")):
        if dca.get(rnd):
            events.append({"kind": "dca", "round": rnd, "price": _f(row.get(price_col))})

    # TP fires (partial/standard take-profit checkpoints)
    if row.get("tp1_fired"):
        events.append({"kind": "tp", "level": "tp1",
                       "target": _f(row.get("tp1_target")) or _f(row.get("tp1"))})
    if row.get("tp_fired"):
        events.append({"kind": "tp", "level": "tp",
                       "target": _f(row.get("tp_target")) or _f(row.get("tp"))})

    # final trailing-SL level (the SL path itself is not stored; the final level is)
    sl = _f(row.get("trailing_sl_level"))
    if sl:
        events.append({"kind": "sl_final", "level": sl})

    # brain interventions (LLM/brain actions on the live position). brain_actions is normally a
    # JSON list, but tolerate a dict/other shape without slicing it (would raise on a dict).
    brain_actions = _jload(row.get("brain_actions"), [])
    if not isinstance(brain_actions, list):
        brain_actions = [brain_actions] if brain_actions else []
    if brain_actions:
        events.append({
            "kind": "brain_interventions",
            "count": int(row.get("intervention_count") or len(brain_actions)),
            "influenced": bool(row.get("brain_influenced")),
            "last": brain_actions[-3:],            # bounded — last few only
        })

    # realized excursion envelope (MFE/MAE in USDT, tracked live as peak_pnl / peak_loss)
    mfe = _f(row.get("peak_pnl_usdt"))
    mae = _f(row.get("peak_loss_usdt"))

    exit_ts = row.get("exit_time")
    events.append({
        "kind": "exit",
        "ts": (exit_ts.isoformat() if isinstance(exit_ts, datetime) else exit_ts),
        "price": _f(row.get("exit_price")),
        "reason": row.get("exit_reason"),
        "hold_s": row.get("hold_time_seconds"),
        "net_pnl_usdt": _f(row.get("net_pnl_usdt")),
        "fees_usdt": _f(row.get("fees_usdt")),
    })

    return {
        "schema_version": OUTCOME_SCHEMA_VERSION,
        "events": events,
        "mfe_usdt": mfe,
        "mae_usdt": mae,
        "marks": None,            # per-tick path not persisted per trade (honest, not fabricated)
        "execution_fills": None,  # per-fill stream not persisted per trade
        "note": "bounded discrete recorded events; continuous mark/fill path is not stored per trade",
        "built_ts": round(time.time(), 3),
    }


def build_outcome_packet(r, row: dict, now_ms: float, *, prev: dict | None = None) -> dict:
    """Realized OutcomePacket for one closed trade (§5.3). Deterministic from the immutable row +
    current candle history → safe to rebuild idempotently. `prev` (an earlier packet on the row)
    preserves first_built_ts across horizon-backfill rebuilds."""
    cap = _f(row.get("capital_usdt")) or 0.0
    net = _f(row.get("net_pnl_usdt"))
    if net is None:
        net = _f(row.get("final_pnl_usdt"), 0.0)
    fees = _f(row.get("fees_usdt"), 0.0) or 0.0
    mfe = _f(row.get("peak_pnl_usdt"))
    mae = _f(row.get("peak_loss_usdt"))

    roc = (net / cap) if cap > 1e-9 else 0.0
    mae_frac = (abs(mae) / cap) if (cap > 1e-9 and mae is not None) else 0.0
    cost_frac = (fees / cap) if cap > 1e-9 else 0.0
    util = _utility(roc, mae_frac, cost_frac, _lambdas(r))

    entry_ms = _epoch_ms(row.get("entry_time"))
    entry_price = _f(row.get("entry_price"))
    horizons = {}
    if entry_ms is not None and entry_price:
        horizons = _forward_horizons(r, row.get("pair"), entry_ms, entry_price,
                                     row.get("direction"), now_ms, _horizons_min(r))

    # Path-aware counterfactual twin (actual/opposite/abstain). Computed once on the first build
    # while the realized candle path is still in-window; preserved verbatim across horizon-backfill
    # rebuilds (re-simulating later would lose it as candles age out). Pending stub only until then.
    prev_cf = (prev or {}).get("counterfactual")
    if isinstance(prev_cf, dict) and prev_cf.get("status") in ("ok", "unavailable", "error"):
        counterfactual = prev_cf
    else:
        try:
            from .twin import simulate_counterfactuals
            counterfactual = simulate_counterfactuals(r, row, _prov_for_twin(row))
        except Exception as exc:
            counterfactual = {"status": "error", "error": str(exc)[:160]}

    failure_type = row.get("failure_type")
    packet = {
        "schema_version": OUTCOME_SCHEMA_VERSION,
        "net_pnl_usdt": (None if net is None else round(net, 4)),
        "return_on_capital": round(roc, 6),
        "utility": util,
        "mfe_usdt": mfe,
        "mae_usdt": mae,
        "drawdown_frac": round(mae_frac, 6),
        "hold_time_s": row.get("hold_time_seconds"),
        "exit_reason": row.get("exit_reason"),
        "won": (None if net is None else bool(net > 0)),     # DESCRIPTIVE only (not a utility)
        "failure_label": {
            "failure_type": failure_type,                    # 'direction' | 'signal' | None
            "source": "diagnosis.py",
            "confidence": "proxy",                           # heuristic label, not causal truth
        },
        "horizons": horizons,
        "horizons_complete": bool(horizons.get("complete", True)),
        # path-aware counterfactual: actual/opposite/abstain replayed on the real forward path,
        # with a confidence-labelled fault class (replaces diagnosis.py's same-exit-price proxy).
        "counterfactual": counterfactual,
        "pointers": {
            "entry_snapshot": "signals_at_entry.entry_snapshot",
            "lifetrace": "signals_at_entry.lifetrace",
            "audit": "signals_at_entry.audit",
            "audit_calibration": "signals_at_entry.audit_calibration",
        },
        "first_built_ts": (prev or {}).get("first_built_ts") or round(time.time(), 3),
        "updated_ts": round(time.time(), 3),
    }
    return packet


# ─────────────────────────────────────────────────────────────────────────────────────────────
# Idempotent close-time harvest (mirrors audit.grade_calibration: durable per-row ledger +
# recomputed aggregate; counter incremented once per trade; restart/duplicate-beat safe).
# ─────────────────────────────────────────────────────────────────────────────────────────────

_SELECT_COLS = (
    "id", "pair", "direction", "entry_price", "entry_time", "exit_price", "exit_time",
    "exit_reason", "hold_time_seconds", "capital_usdt", "quantity", "leverage",
    "net_pnl_usdt", "final_pnl_usdt", "fees_usdt", "peak_pnl_usdt", "peak_loss_usdt",
    "failure_type", "dca_status", "dca1_price", "dca2_price", "tp1_fired", "tp_fired",
    "tp1_target", "tp_target", "tp1", "tp", "trailing_sl_level", "brain_actions",
    "intervention_count", "brain_influenced", "signals_at_entry",
)


def harvest_outcomes(r, limit: int = 50) -> dict:
    """Build LifeTrace + OutcomePacket for closed scibrain trades that lack them, and backfill
    horizons for packets whose horizons hadn't matured. Then recompute the aggregate from all rows.

    Idempotent / double-count-proof: a fresh build is detected by the absence of `outcome_packet`;
    only then is the harvested counter incremented. Re-selecting an already-complete packet is
    avoided by the WHERE clause, so a stable system harvests 0 on re-run. Never raises."""
    from db import db_conn
    now_ms = time.time() * 1000.0
    summary = {"built_now": 0, "backfilled": 0, "total_packets": 0, "horizons_pending": 0}
    try:
        # eligible = closed scibrain trades with NO packet yet, OR a packet whose horizons are
        # still open (so newly-matured horizons get filled on a later pass).
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"""SELECT {', '.join(_SELECT_COLS)}
                      FROM trades
                     WHERE timeframe='scibrain' AND status='closed'
                       AND (NOT (signals_at_entry::jsonb ? 'outcome_packet')
                            OR (signals_at_entry::jsonb->'outcome_packet'->>'horizons_complete')='false'
                            -- backfill the path-aware twin onto packets built before it existed;
                            -- idempotent (once status is ok/unavailable/error it is not reselected)
                            OR (signals_at_entry::jsonb->'outcome_packet'->'counterfactual'->>'status')='pending')
                     ORDER BY exit_time DESC NULLS LAST
                     LIMIT %s""",
                (int(max(1, limit)),))
            rows = [dict(zip(_SELECT_COLS, rec)) for rec in cur.fetchall()]

        for row in rows:
            prov = _jload(row.get("signals_at_entry"), {}) or {}
            prev_packet = prov.get("outcome_packet")
            is_new = prev_packet is None

            lifetrace = build_lifetrace(row)
            packet = build_outcome_packet(r, row, now_ms, prev=prev_packet)

            patch = {"outcome_packet": packet}
            if is_new:                                  # write the LifeTrace once (events don't change)
                patch["lifetrace"] = lifetrace
            _merge_trade_provenance(str(row.get("id")), patch)

            if is_new:
                summary["built_now"] += 1
            else:
                summary["backfilled"] += 1
            if not packet.get("horizons_complete", True):
                summary["horizons_pending"] += 1

        if summary["built_now"]:
            try:
                r.incrby(K.OUTCOME_HARVESTED_TOTAL, summary["built_now"])
            except Exception:
                pass

        # recompute the aggregate from EVERY packet (idempotent; no double-count)
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT signals_at_entry::jsonb->'outcome_packet'
                     FROM trades
                    WHERE timeframe='scibrain' AND status='closed'
                      AND signals_at_entry::jsonb ? 'outcome_packet'""")
            packets = [_jload(rec[0], None) for rec in cur.fetchall()]
            packets = [p for p in packets if isinstance(p, dict)]
        agg = _outcome_aggregate(packets, _horizons_min(r))
        try:
            r.set(K.OUTCOMES_AGG, json.dumps(agg))
        except Exception:
            pass
        summary["total_packets"] = agg["n"]
    except Exception as exc:
        log.warning("scibrain_outcome_harvest_error", error=str(exc)[:160])
    if summary["built_now"] or summary["backfilled"]:
        log.info("scibrain_outcome_harvest", built=summary["built_now"],
                 backfilled=summary["backfilled"], total=summary["total_packets"],
                 horizons_pending=summary["horizons_pending"])
    return summary


def _outcome_aggregate(packets: list, horizons_min) -> dict:
    """Aggregate realized truth over all harvested packets: mean utility/ROC, win rate (descriptive),
    mean MFE/MAE, and per-horizon direction-correct rate over the MATURED-with-price horizons."""
    n = len(packets)
    if n == 0:
        return {"n": 0, "mean_utility": None, "mean_roc": None, "win_rate": None,
                "mean_mfe_usdt": None, "mean_mae_usdt": None, "horizons": {},
                "horizons_pending_trades": 0, "updated_ts": round(time.time(), 3)}

    def _avg(vals):
        vals = [v for v in vals if v is not None]
        return (round(sum(vals) / len(vals), 6) if vals else None)

    utils = [(_g(p, "utility", "utility")) for p in packets]
    rocs = [p.get("return_on_capital") for p in packets]
    wins = [p.get("won") for p in packets if p.get("won") is not None]
    mfes = [p.get("mfe_usdt") for p in packets]
    maes = [p.get("mae_usdt") for p in packets]
    pending = sum(1 for p in packets if not p.get("horizons_complete", True))

    horizon_stats = {}
    for h in horizons_min:
        hk = str(h)
        correct = total = 0
        rets = []
        for p in packets:
            rec = (p.get("horizons") or {}).get(hk)
            if isinstance(rec, dict) and rec.get("available"):
                total += 1
                correct += int(bool(rec.get("dir_correct")))
                if rec.get("dir_ret") is not None:
                    rets.append(float(rec["dir_ret"]))
        if total:
            horizon_stats[hk] = {
                "n_matured": total,
                "dir_correct_rate": round(correct / total, 4),
                "mean_dir_ret": (round(sum(rets) / len(rets), 6) if rets else None),
            }

    # path-aware counterfactual twin: fault-class distribution + replay-validation quality
    fault_counts = {"none": 0, "direction": 0, "selection": 0}
    twin_ok = 0
    val_errs = []
    for p in packets:
        cf = p.get("counterfactual")
        if isinstance(cf, dict) and cf.get("status") == "ok":
            twin_ok += 1
            fc = cf.get("fault_class")
            if fc in fault_counts:
                fault_counts[fc] += 1
            ae = (cf.get("validation") or {}).get("abs_error")
            if ae is not None:
                val_errs.append(float(ae))
    twin_stats = {
        "n_replayed": twin_ok,
        "fault_classes": fault_counts,
        "direction_fault_rate": (round(fault_counts["direction"] / twin_ok, 4) if twin_ok else None),
        "mean_replay_abs_error_usdt": (round(sum(val_errs) / len(val_errs), 4) if val_errs else None),
    }
    return {
        "n": n,
        "mean_utility": _avg(utils),
        "mean_roc": _avg(rocs),
        "win_rate": (round(sum(1 for w in wins if w) / len(wins), 4) if wins else None),
        "mean_mfe_usdt": _avg(mfes),
        "mean_mae_usdt": _avg(maes),
        "horizons": horizon_stats,
        "horizons_pending_trades": pending,
        "twin": twin_stats,
        "note": "win_rate is descriptive only; utility (multi-objective) is the score. horizon "
                "direction-correctness is the decision's forward edge, independent of our exit. "
                "twin.direction_fault_rate = share where the path-aware OPPOSITE policy beat ours.",
        "updated_ts": round(time.time(), 3),
    }


def _g(d: dict, *path):
    """Nested dict getter; None on any miss."""
    cur = d
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur
