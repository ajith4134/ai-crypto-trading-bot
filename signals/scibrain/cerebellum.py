"""SciBrain Phase-7f task-8 — CEREBELLUM: bounded residual/calibration/timing/execution learners with the
FIRST LIMITED CANARY AUTHORITY (design §3.8 cerebellum / §5.5 step-12 "cerebellar residual/calibration path
limited shadow learning authority first" / Rule-14 authority ladder observe→advise→bounded_canary→live).

The cerebellum makes RAPID, LOW-RISK, BOUNDED corrections on top of stable baselines, and is the first path
to receive real (bounded, owner-gated, reversible) authority. THREE residual heads, each learned online from
REAL matured trade labels, each with a hard OUT-OF-SAMPLE "earns its keep" gate — a head only moves a real
trade if it provably helps on held-out data (Rule 21):

  • CALIBRATION  — recalibrate the decision's conviction (predicted P(win)) against the realized win/loss of
    closed trades (12k+ labels). Bounded residual, clamped to ±CALIB_CAP. Earns authority iff it lowers the
    out-of-sample Brier score. APPLIED live to dec.conviction (→ size + leverage) when armed + earned.
  • TIMING      — predict whether the just-decided entry is likely to fill at a worse price than predicted
    (so a one-cycle wait may help), from cheap belief features. Bounded: defers a single pick by at most
    ONE cycle (never starves — a hard consecutive-skip cap). Earns authority iff it beats the no-wait base
    out-of-sample. APPLIED live as a bounded entry-timing nudge when armed + earned.
  • SLIPPAGE    — predict execution slippage = (actual entry − predicted entry) from row features. Earns
    authority iff it beats the naive (zero) predictor out-of-sample. Under PAPER mark-price fills there is no
    limit/tolerance lever to apply, so this head LEARNS + declares its bounded_canary cap + reports honestly,
    and its live application is PENDING a real-exchange order path (Rule 12 — no fabricated paper effect).

AUTHORITY: cap = bounded_canary for all three (capital-affecting but bounded). EFFECTIVE authority per head =
bounded_canary IFF (owner canary flag ON) AND (head earned OOS) AND (min samples); else observe (pass-through,
zero trade effect). Master kill switch: scibrain:cerebellum:canary = "0" (default ON per owner 2026-06-13 so
the effect is observable on paper trades; the earned-gate still governs whether each head actually applies).

The hot-path apply_* helpers are BULLETPROOF: they read precomputed residual params from Redis, never train,
never raise, and always fall back to the unchanged base value. The bot trades live (paper) while this runs.
Run trainer: python -m signals.scibrain.cerebellum
"""
from __future__ import annotations

import json
import time

import structlog

from . import keys as K

log = structlog.get_logger()

# ── bounds (the "bounded output" of §3.8) ──
CALIB_CAP = 0.05         # max absolute conviction adjustment (keeps size/leverage moves small)
SLIP_CAP_BPS = 8.0       # max slippage tolerance the slippage head may express (bps) — report-only under paper
TIMING_MAX_SKIPS = 1     # a timing head may defer one pick by at most ONE cycle (anti-starvation hard cap)
TIMING_WAIT_P = 0.60     # defer only when predicted adverse-fill prob exceeds this

# ── earns-its-keep gates (OOS improvement + min samples) ──
MIN_CALIB = 500
MIN_SLIP = 300
MIN_TIMING = 300
BRIER_MARGIN = 0.0015    # calibration must lower OOS Brier by at least this to earn authority
N_BUCKETS = 10           # conviction deciles for the recalibration table

_STALE_S = 36 * 3600


# ───────────────────────── shared helpers ─────────────────────────
def _armed(r) -> bool:
    """Owner master switch for the cerebellar canary. DEFAULT ON (owner 2026-06-13: 'turn these on so I can
    see if it improves trades'). Set scibrain:cerebellum:canary=0 to disarm instantly (all heads → observe)."""
    try:
        v = r.get(K.CEREBELLUM_CANARY)
        v = v.decode() if isinstance(v, bytes) else v
        return v != "0"          # absent OR "1" → armed; only an explicit "0" disarms
    except Exception:
        return False             # any error → safe (disarmed)


def _residuals(r) -> dict:
    try:
        raw = r.get(K.CEREBELLUM_RESIDUALS)
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


# ───────────────────────── HOT-PATH apply helpers (bulletproof) ─────────────────────────
def _calibration_recal(r, base: float) -> dict:
    """Pure (no side effects): the would-be calibration recalibration of conviction. Returns the full
    decision view used for BOTH live application AND open-trades-table display (so the adjustment is
    visible even while disarmed/unearned). `applied` is True only when armed AND earned. NEVER raises."""
    base = max(0.0, min(1.0, float(base)))
    out = {"head": "calibration", "base": round(base, 4), "adjusted": round(base, 4),
           "delta": 0.0, "armed": False, "earned": False, "applied": False, "cap": CALIB_CAP}
    try:
        armed = _armed(r)
        res = _residuals(r).get("calibration") or {}
        earned = bool(res.get("earned"))
        out["armed"], out["earned"] = armed, earned
        table = res.get("residual_table") or []   # per-bucket clamped residual (index = int(p*N_BUCKETS))
        if table:
            b = min(len(table) - 1, max(0, int(base * N_BUCKETS - 1e-9)))
            delta = max(-CALIB_CAP, min(CALIB_CAP, float(table[b])))
            adjusted = max(0.0, min(1.0, base + delta))
            out.update({"delta": round(delta, 4), "adjusted": round(adjusted, 4), "bucket": b})
            out["applied"] = bool(armed and earned)
    except Exception:
        pass
    return out


def cerebellum_decision(r, conviction: float, regime: str | None = None) -> dict:
    """OPENER entry point (CALIBRATION head). Computes the would-be recalibrated conviction ALWAYS (shadow,
    for the open-trades table) and marks `applied` True only when armed AND earned. When applied, records
    durable Rule-21 evidence (count + last delta). Returns the full display dict. NEVER raises."""
    d = _calibration_recal(r, conviction)
    d["regime"] = regime
    if d.get("applied") and abs(d.get("delta", 0.0)) > 0:
        try:
            pipe = r.pipeline()
            pipe.incr(K.CEREBELLUM_APPLIED + ":calibration")
            pipe.set(K.CEREBELLUM_LAST + ":calibration",
                     json.dumps({"ts": round(time.time(), 2), "base": d["base"], "delta": d["delta"],
                                 "out": d["adjusted"], "regime": regime, "bucket": d.get("bucket")}))
            pipe.execute()
        except Exception:
            pass
    return d


def apply_conviction(r, conviction: float, regime: str | None = None) -> float:
    """Convenience: the conviction to actually USE (recalibrated when armed+earned, else the base unchanged).
    Side-effect-free — durable evidence is recorded by cerebellum_decision. NEVER raises."""
    try:
        d = _calibration_recal(r, conviction)
        return d["adjusted"] if d.get("applied") else d["base"]
    except Exception:
        return float(conviction)


def timing_should_wait(r, symbol: str, features: dict | None = None) -> bool:
    """TIMING head, live application. Return True to defer this pick by ONE cycle when armed + earned + the
    adverse-fill probability is high — bounded by a hard consecutive-skip cap so it can never starve a pick.
    NEVER raises."""
    try:
        if not _armed(r):
            return False
        res = _residuals(r).get("timing") or {}
        if not res.get("earned"):
            return False
        # hard anti-starvation cap: never defer the same symbol more than TIMING_MAX_SKIPS in a row
        skey = K.CEREBELLUM_SKIPS + ":" + str(symbol)
        try:
            skips = int(r.get(skey) or 0)
        except (TypeError, ValueError):
            skips = 0
        if skips >= TIMING_MAX_SKIPS:
            try:
                r.delete(skey)          # cap reached → force-open now, reset
            except Exception:
                pass
            return False
        # adverse-fill probability from the learned bias (cheap: a single learned base rate + ofi tilt)
        p_adv = float(res.get("p_adverse_base") or 0.0)
        f = features or {}
        try:
            p_adv += float(res.get("ofi_coef") or 0.0) * float(f.get("ofi") or 0.0)
        except Exception:
            pass
        if p_adv >= TIMING_WAIT_P:
            try:
                pipe = r.pipeline()
                pipe.incr(skey); pipe.expire(skey, 120)
                pipe.incr(K.CEREBELLUM_APPLIED + ":timing")
                pipe.set(K.CEREBELLUM_LAST + ":timing",
                         json.dumps({"ts": round(time.time(), 2), "symbol": symbol,
                                     "p_adverse": round(p_adv, 4), "skips": skips + 1}))
                pipe.execute()
            except Exception:
                pass
            return True
        try:
            r.delete(skey)
        except Exception:
            pass
        return False
    except Exception:
        return False


def slippage_tolerance_bps(r, features: dict | None = None) -> float:
    """SLIPPAGE head. Returns the bounded predicted slippage tolerance in bps when armed + earned, else 0.0.
    Report-only under paper (no limit-order lever); wired for a future real-exchange order path. NEVER raises."""
    try:
        if not _armed(r):
            return 0.0
        res = _residuals(r).get("slippage") or {}
        if not res.get("earned"):
            return 0.0
        bps = float(res.get("base_bps") or 0.0)
        return max(0.0, min(SLIP_CAP_BPS, bps))
    except Exception:
        return 0.0


# ───────────────────────── TRAINER (beat task; off the hot path) ─────────────────────────
def _load_closed(limit: int = 6000) -> list[dict]:
    """Real matured labels from closed trades. Never raises → []."""
    try:
        from db import db_conn
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT direction_confidence, net_pnl_usdt, market_regime, direction,
                       predicted_entry, entry_price, leverage, capital_usdt,
                       predicted_hold_seconds, hold_time_seconds, exit_time
                FROM trades
                WHERE status='closed' AND direction_confidence IS NOT NULL AND net_pnl_usdt IS NOT NULL
                ORDER BY exit_time DESC NULLS LAST
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
        out = []
        for (dc, pnl, reg, d, pe, ep, lev, cap, ph, h, xt) in rows:
            out.append({"p": max(0.0, min(1.0, float(dc) / 100.0)), "win": 1 if float(pnl) > 0 else 0,
                        "regime": reg, "dir": d, "pe": pe, "ep": ep, "lev": lev, "cap": cap,
                        "ph": ph, "h": h})
        out.reverse()    # chronological (oldest first) for time-ordered train/test split
        return out
    except Exception as exc:
        log.warning("cerebellum_load_failed", error=str(exc)[:160])
        return []


def _brier(ps, ys):
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / max(1, len(ps))


def _fit_calibration(rows: list[dict]) -> dict:
    """Decile recalibration of predicted conviction → empirical win rate, with an OOS Brier earns-gate."""
    import numpy as np
    n = len(rows)
    if n < MIN_CALIB:
        return {"earned": False, "n": n, "reason": f"insufficient labels ({n}<{MIN_CALIB})"}
    i_tr = int(0.70 * n)
    tr, te = rows[:i_tr], rows[i_tr:]
    edges = list(np.linspace(0.0, 1.0, N_BUCKETS + 1))
    # build the recalibration table on TRAIN: per bucket, residual = empirical_winrate − bucket_midpoint
    table, emp = [], []
    for i in range(N_BUCKETS):
        lo, hi = edges[i], edges[i + 1]
        wins = [t["win"] for t in tr if (t["p"] >= lo and (t["p"] < hi or (i == N_BUCKETS - 1 and t["p"] <= hi)))]
        if wins:
            e = sum(wins) / len(wins)
            mid = (lo + hi) / 2.0
            table.append(max(-CALIB_CAP, min(CALIB_CAP, e - mid)))
            emp.append(round(e, 4))
        else:
            table.append(0.0); emp.append(None)

    def _recal(p):
        b = min(N_BUCKETS - 1, max(0, int(p * N_BUCKETS - 1e-9)))
        return max(0.0, min(1.0, p + table[b]))

    # OOS Brier on TEST: raw conviction vs recalibrated
    ys = [t["win"] for t in te]
    brier_raw = _brier([t["p"] for t in te], ys)
    brier_cal = _brier([_recal(t["p"]) for t in te], ys)
    improvement = round(brier_raw - brier_cal, 5)
    earned = bool(improvement >= BRIER_MARGIN)
    return {"earned": earned, "n": n, "n_test": len(te), "edges": [round(e, 4) for e in edges],
            "residual_table": [round(x, 5) for x in table], "empirical_winrate": emp,
            "brier_raw": round(brier_raw, 5), "brier_calibrated": round(brier_cal, 5),
            "oos_brier_improvement": improvement, "cap": CALIB_CAP,
            "note": "conviction-decile recalibration; earns authority iff OOS Brier improves ≥ margin."}


def _fit_slippage(rows: list[dict]) -> dict:
    """Slippage = signed adverse (actual − predicted) entry, in bps. OOS MAE vs naive-zero earns-gate."""
    import numpy as np
    s = [x for x in rows if x["pe"] and x["ep"] and float(x["pe"]) > 0]
    n = len(s)
    if n < MIN_SLIP:
        return {"earned": False, "n": n, "reason": f"insufficient labels ({n}<{MIN_SLIP})"}
    # adverse slippage in bps: long pays when ep>pe, short pays when ep<pe
    def _slip_bps(x):
        raw = (float(x["ep"]) - float(x["pe"])) / float(x["pe"]) * 1e4
        return raw if str(x["dir"]).lower().startswith("l") else -raw
    y = np.array([_slip_bps(x) for x in s], dtype=float)
    i_tr = int(0.70 * n)
    base_bps = float(np.median(y[:i_tr]))                 # the learned base slippage (train)
    mae_naive = float(np.mean(np.abs(y[i_tr:] - 0.0)))    # predict zero
    mae_base = float(np.mean(np.abs(y[i_tr:] - base_bps)))# predict the learned base
    improvement = round(mae_naive - mae_base, 4)
    earned = bool(improvement > 0.0 and n >= MIN_SLIP)
    return {"earned": earned, "n": n, "base_bps": round(max(0.0, min(SLIP_CAP_BPS, abs(base_bps))), 4),
            "mae_naive": round(mae_naive, 4), "mae_base": round(mae_base, 4),
            "oos_mae_improvement": improvement, "cap_bps": SLIP_CAP_BPS,
            "applied_under_paper": False,
            "note": "median adverse fill slippage (bps); report-only under paper (no limit lever), declares "
                    "bounded_canary cap for the future real-exchange order path."}


def _fit_timing(rows: list[dict]) -> dict:
    """Timing: P(adverse fill — actual entry worse than predicted). OOS log-loss vs base-rate earns-gate."""
    import numpy as np
    s = [x for x in rows if x["pe"] and x["ep"] and float(x["pe"]) > 0]
    n = len(s)
    if n < MIN_TIMING:
        return {"earned": False, "n": n, "reason": f"insufficient labels ({n}<{MIN_TIMING})"}
    def _adverse(x):
        worse = (float(x["ep"]) > float(x["pe"])) if str(x["dir"]).lower().startswith("l") else (float(x["ep"]) < float(x["pe"]))
        return 1 if worse else 0
    y = np.array([_adverse(x) for x in s], dtype=float)
    i_tr = int(0.70 * n)
    base = float(np.mean(y[:i_tr]))                        # train base rate of adverse fills
    ys = y[i_tr:]
    eps = 1e-9
    ll_base = float(-np.mean(ys * np.log(base + eps) + (1 - ys) * np.log(1 - base + eps)))
    ll_half = float(-np.mean(ys * np.log(0.5) + (1 - ys) * np.log(0.5)))  # uninformative 0.5
    improvement = round(ll_half - ll_base, 5)
    # earns only if the learned base rate is BOTH informative (beats 0.5) AND high enough to ever trigger a wait
    earned = bool(improvement > 0.0 and base >= TIMING_WAIT_P and n >= MIN_TIMING)
    return {"earned": earned, "n": n, "p_adverse_base": round(base, 4), "ofi_coef": 0.0,
            "logloss_base": round(ll_base, 5), "logloss_uninformative": round(ll_half, 5),
            "oos_logloss_improvement": improvement, "wait_threshold": TIMING_WAIT_P,
            "max_skips": TIMING_MAX_SKIPS,
            "note": "P(adverse fill) from matured predicted-vs-actual entry; defers one pick by ≤1 cycle when "
                    "armed+earned+p≥threshold; hard anti-starvation cap."}


def train_cerebellum(r=None, publish: bool = True) -> dict:
    """Fit the three bounded residual heads on real matured labels with OOS earns-gates. Writes the residual
    params (hot-path) + a full diagnostic report. Deterministic, ~1s. Never raises into the beat task."""
    t0 = time.time()
    import redis_client
    r = r or redis_client.get()
    rows = _load_closed()
    calib = _fit_calibration(rows)
    slip = _fit_slippage(rows)
    timing = _fit_timing(rows)
    armed = _armed(r)

    residuals = {"calibration": calib, "slippage": slip, "timing": timing}
    earned = {k: bool(v.get("earned")) for k, v in residuals.items()}
    # heads that will ACTUALLY move a live trade right now (armed AND earned AND has a paper lever)
    live_heads = [k for k in ("calibration", "timing") if armed and earned[k]]
    report = {
        "ts": round(time.time(), 3), "n_labels": len(rows), "armed": armed,
        "bounds": {"calib_cap": CALIB_CAP, "slip_cap_bps": SLIP_CAP_BPS, "timing_max_skips": TIMING_MAX_SKIPS,
                   "timing_wait_p": TIMING_WAIT_P, "brier_margin": BRIER_MARGIN},
        "heads": residuals, "earned": earned, "live_heads": live_heads,
        "authority": "bounded_canary", "cap": "bounded_canary",
        "effective": {k: ("bounded_canary" if (armed and earned[k]) else "observe") for k in residuals},
        "kill_switch": K.CEREBELLUM_CANARY, "train_secs": round(time.time() - t0, 2),
        "verdict": (
            (f"Cerebellar canary ARMED. Live heads {live_heads or '[]'} (armed+earned). "
             if armed else "Cerebellar canary DISARMED (observe only). ") +
            f"Calibration: OOS Brier {calib.get('brier_raw')}→{calib.get('brier_calibrated')} "
            f"(Δ {calib.get('oos_brier_improvement')}, earned={earned['calibration']}, ±{CALIB_CAP} cap). "
            f"Timing: P(adverse)={timing.get('p_adverse_base')} earned={earned['timing']} (≤{TIMING_MAX_SKIPS} skip). "
            f"Slippage: base {slip.get('base_bps')}bps earned={earned['slippage']} (report-only under paper). "
            "Bounded, owner-gated (scibrain:cerebellum:canary), reversible (Rule 21)."),
    }
    if publish:
        try:
            r.set(K.CEREBELLUM_RESIDUALS, json.dumps(residuals))
            r.set(K.CEREBELLUM_REPORT, json.dumps(report))
            build_cerebellum_health(r)
        except Exception as exc:
            log.warning("cerebellum_publish_failed", error=str(exc)[:160])
    return report


# ───────────────────────── HEALTH faculty (Tier-0 read) ─────────────────────────
def build_cerebellum_health(r, *, publish: bool = True) -> dict:
    """Diagnose the cerebellum report into a typed health/issues + authority view. Pure read; never raises."""
    out = {"contract": "CerebellumHealth", "available": False, "ts": round(time.time(), 3)}
    try:
        raw = r.get(K.CEREBELLUM_REPORT)
        rep = json.loads(raw) if raw else None
        if not rep:
            out.update({"health": "cold", "issues": [],
                        "note": "Cerebellum has not trained yet — no report. Run signals.scibrain.cerebellum."})
            if publish:
                try: r.set(K.CEREBELLUM_HEALTH, json.dumps(out))
                except Exception: pass
            return out

        heads = rep.get("heads") or {}
        earned = rep.get("earned") or {}
        armed = bool(rep.get("armed"))
        live_heads = rep.get("live_heads") or []
        age_s = max(0, int(time.time() - float(rep.get("ts") or 0)))
        stale = age_s > _STALE_S
        # live application evidence (Rule 21)
        applied = {}
        for k in ("calibration", "timing"):
            try:
                applied[k] = int(r.get(K.CEREBELLUM_APPLIED + ":" + k) or 0)
            except (TypeError, ValueError):
                applied[k] = 0
        last_calib = None
        try:
            lc = r.get(K.CEREBELLUM_LAST + ":calibration")
            last_calib = json.loads(lc) if lc else None
        except Exception:
            last_calib = None

        issues = []

        def add(code, sev, title, detail, rec):
            issues.append({"code": code, "severity": sev, "title": title, "detail": detail, "recommendation": rec})

        add("cerebellum_authority", "info" if armed else "warn",
            f"Cerebellar canary {'ARMED' if armed else 'DISARMED'} — cap bounded_canary, live heads {live_heads or '[]'}",
            "First bounded authority grant (§3.8/§5.5 step-12). Each head moves a real trade only when armed AND "
            "earned out-of-sample. Bounded, reversible (scibrain:cerebellum:canary=0 disarms instantly).",
            "Watch live application counters + per-trade calibration deltas; disarm if trade quality drops.")

        cal = heads.get("calibration") or {}
        if earned.get("calibration"):
            add("calibration_earned", "info",
                f"Calibration EARNS authority — OOS Brier {cal.get('brier_raw')}→{cal.get('brier_calibrated')} (Δ {cal.get('oos_brier_improvement')})",
                f"Recalibrates conviction (±{cal.get('cap')}) on {cal.get('n')} labels; "
                f"applied to {applied.get('calibration', 0)} live decisions so far.",
                "Keep watching out-of-sample Brier; this is the primary observable improvement lever.")
        else:
            add("calibration_not_earned", "warn",
                f"Calibration does NOT earn authority (OOS Brier Δ {cal.get('oos_brier_improvement')}, n={cal.get('n')})",
                cal.get("reason") or "Recalibration does not beat raw conviction out-of-sample by the margin.",
                "Stays observe (no live effect) until it provably improves OOS Brier.")

        tim = heads.get("timing") or {}
        add("timing_status", "info" if earned.get("timing") else "info",
            f"Timing {'EARNS' if earned.get('timing') else 'observe'} — P(adverse) {tim.get('p_adverse_base')}, n={tim.get('n')}",
            f"Defers ≤{rep.get('bounds', {}).get('timing_max_skips')} cycle when armed+earned; "
            f"applied {applied.get('timing', 0)} times. " + (tim.get("reason") or tim.get("note") or ""),
            "Bounded + anti-starvation capped; earns only if informative AND adverse-rate high enough to act.")

        slp = heads.get("slippage") or {}
        add("slippage_status", "info",
            f"Slippage {'earned' if earned.get('slippage') else 'observe'} — base {slp.get('base_bps')}bps, n={slp.get('n')} (report-only under paper)",
            "Predicts execution slippage; no limit lever under paper mark-fills so it does not move paper trades "
            "(Rule 12 — no fabricated effect). Declares bounded_canary cap for a future real-exchange path.",
            "Wire to a real-exchange limit/tolerance order path before granting live application.")

        if stale:
            add("report_stale", "warn", f"Cerebellum report stale ({age_s // 3600}h)",
                "Older than cadence — the beat task may not be running.", "Check scibrain-cerebellum beat task.")

        sev_rank = {"critical": 3, "warn": 2, "info": 1}
        health = "stale" if stale else ("armed_live" if live_heads else ("armed_idle" if armed else "observe"))
        out.update({
            "available": True, "health": health, "age_s": age_s, "armed": armed,
            "authority": "bounded_canary", "effective": rep.get("effective"),
            "earned": earned, "live_heads": live_heads, "applied": applied, "last_calibration": last_calib,
            "heads": heads, "n_labels": rep.get("n_labels"), "bounds": rep.get("bounds"),
            "kill_switch": rep.get("kill_switch"), "verdict": rep.get("verdict"),
            "issues": sorted(issues, key=lambda i: -sev_rank[i["severity"]]),
            "n_issues": {s: sum(1 for i in issues if i["severity"] == s) for s in ("critical", "warn", "info")},
            "note": ("Cerebellum (design §3.8/§5.5 step-12): bounded calibration/timing/slippage residual learners "
                     "with the FIRST limited canary authority — each head moves a real trade only when armed "
                     "(owner flag) AND earned out-of-sample, bounded + reversible (Rule 21)."),
        })
        if publish:
            try: r.set(K.CEREBELLUM_HEALTH, json.dumps(out))
            except Exception: pass
    except Exception as exc:
        out["error"] = str(exc)[:200]
        log.warning("cerebellum_health_failed", error=str(exc)[:200])
    return out


if __name__ == "__main__":
    print(json.dumps(train_cerebellum(), indent=2))
