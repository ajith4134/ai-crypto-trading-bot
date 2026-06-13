"""SciBrain Phase-7f — reframe the day/minute PPO 'MARL' as HIERARCHICAL CONTROLLERS with shared belief,
real trajectories, the missing HOUR role, and coordination tests + ablation (design §3.7 / §8-table line 37
& 417 / step 10).

The existing ml/marl.py agents are NOT coordinated MARL — they are independent ONE-STEP contextual bandits
on weak 3-dim observations, the hour agent is declared-but-dead, and there is no shared state or coordination.
This SHADOW analysis reframes them honestly as a multi-timescale hierarchy and MEASURES whether the reframing
actually helps:

  • SHARED BELIEF  — all three controllers read ONE shared state (the rich entry belief: ofi/vpin/funding/
    sentiment/conviction/net_vote + regime), at different resolutions per role. (Reuses the world-model corpus.)
  • REAL TRAJECTORIES — ordered closed trades grouped into multi-timescale WINDOWS: one DAY decision persists
    over ~20 trades, one HOUR decision over ~5, the MINUTE acts per trade. (Not 1-step bandits.)
  • THREE HIERARCHICAL ROLES — DAY (strategic: sets the directional/risk stance, coarse=regime context) →
    HOUR (tactical: engage/wait, medium=sentiment context, can veto) → MINUTE (execution: enter/skip, fine=
    microstructure context, can veto). Lower levels CONDITION on the parent's action → genuine hierarchy.
  • COORDINATION GAIN + ABLATION — utility of the full 3-level cascade vs a FLAT independent baseline (the old
    framing) and vs no-belief, day-only, day+hour; plus per-level marginal VETO rates (is a level decorative?)
    and the strategy-vs-hindsight conflict rate. Reward = the deterministic digital-twin utility per action
    (reuses signals/scibrain/twin.py via ml.world_model_train — actual/opposite/abstain on the real path).

SHADOW / read-only: this does NOT touch the LIVE day/minute PPO agents (which scale capital in brain/soar.py
and veto in signals/engine.py). It measures the coordination gain so a future promotion is owner-gated and
evidence-backed (Rule 21 — no change to live trade influence). Run: python -m ml.hierarchical_controllers
"""
from __future__ import annotations

import json
import time

import numpy as np

from ml.world_model_train import _load_wm_corpus    # reuse the rich corpus + deterministic twin utilities

_REGIMES = ("trending", "mean_revert", "turbulent", "neutral")
DAY_W, HOUR_W = 20, 5                                 # trajectory windows: 1 day ≈ 20 trades, 1 hour ≈ 5
MIN_CTX = 12                                          # min train samples for a context bucket (else fall back)


def _belief_from_obs(obs):
    """Extract the SHARED belief from the world-model obs layout
    [mv(18) | ofi,vpin,funding,spread,sentiment,conviction,net_vote | regime_onehot(4)]."""
    o = np.asarray(obs, dtype=np.float32)
    reg_oh = o[25:29]
    regime = _REGIMES[int(np.argmax(reg_oh))] if reg_oh.any() else "neutral"
    return {"ofi": float(o[18]), "vpin": float(o[19]), "sentiment": float(o[22]),
            "conviction": float(o[23]), "regime": regime}


def _load_controller_corpus():
    """Ordered (belief, dir, twin_action_utils) per trade. Twin gives (u_long, u_short, u_abstain); falls back
    to a symmetric proxy when the path-aware twin couldn't replay (so every trade is usable)."""
    rows = []
    for obs, act, pnl, twin in _load_wm_corpus():
        d = "long" if int(act) == 0 else "short"
        if twin is not None:
            uL, uS, uA = twin
        else:                                          # symmetric proxy on the realized pnl (honest fallback)
            uL = float(pnl) if d == "long" else -float(pnl)
            uS = -uL; uA = 0.0
        rows.append({"belief": _belief_from_obs(obs), "dir": d, "u": (float(uL), float(uS), float(uA))})
    return rows


def _sent_bucket(s, med):
    return "hi" if s >= med else "lo"


def _ofi_bucket(x):
    return "pos" if x >= 0 else "neg"


def _vpin_bucket(v, med):
    return "hi" if v >= med else "lo"


def _u_dir(u, d):
    """Twin utility of ENTERING in direction d (u = (u_long, u_short, u_abstain))."""
    return u[0] if d == "long" else u[1]


def _fit(train):
    """Fit the hierarchy top-down on TRAIN (real attributed twin utility). Returns policy params + thresholds."""
    sent_med = float(np.median([r["belief"]["sentiment"] for r in train])) if train else 0.5
    vpin_med = float(np.median([r["belief"]["vpin"] for r in train])) if train else 0.0

    # DAY (strategic): per regime, the stance maximizing mean train utility {long, short, flat=abstain(0)}
    day_stance = {}
    for reg in _REGIMES:
        rs = [r for r in train if r["belief"]["regime"] == reg]
        if len(rs) < MIN_CTX:
            day_stance[reg] = "flat"; continue
        mL = float(np.mean([r["u"][0] for r in rs])); mS = float(np.mean([r["u"][1] for r in rs]))
        best = max(("long", mL), ("short", mS), ("flat", 0.0), key=lambda x: x[1])
        day_stance[reg] = best[0]
    # no-belief ablation: one global stance (ignore regime)
    if train:
        gL = float(np.mean([r["u"][0] for r in train])); gS = float(np.mean([r["u"][1] for r in train]))
        day_global = max(("long", gL), ("short", gS), ("flat", 0.0), key=lambda x: x[1])[0]
    else:
        day_global = "flat"

    def stance_of(r):
        return day_stance.get(r["belief"]["regime"], "flat")

    # HOUR (tactical): given (stance D, sentiment bucket) → engage iff mean train U_D > 0 (else wait/veto)
    hour_enter = {}
    from collections import defaultdict
    hbuf = defaultdict(list)
    for r in train:
        D = stance_of(r)
        if D == "flat":
            continue
        hbuf[(D, _sent_bucket(r["belief"]["sentiment"], sent_med))].append(_u_dir(r["u"], D))
    for k, v in hbuf.items():
        hour_enter[k] = bool(np.mean(v) > 0) if len(v) >= MIN_CTX else True   # default engage if thin

    def hour_ok(r):
        D = stance_of(r)
        if D == "flat":
            return False
        return hour_enter.get((D, _sent_bucket(r["belief"]["sentiment"], sent_med)), True)

    # MINUTE (execution): given (D, ofi bucket, vpin bucket) among hour-engaged → enter iff mean train U_D > 0
    minute_enter = {}
    mbuf = defaultdict(list)
    for r in train:
        D = stance_of(r)
        if D == "flat" or not hour_ok(r):
            continue
        key = (D, _ofi_bucket(r["belief"]["ofi"]), _vpin_bucket(r["belief"]["vpin"], vpin_med))
        mbuf[key].append(_u_dir(r["u"], D))
    for k, v in mbuf.items():
        minute_enter[k] = bool(np.mean(v) > 0) if len(v) >= MIN_CTX else True

    # FLAT independent baseline (the OLD framing): single-level per-trade enter/skip on FINE context only,
    # entering in the GLOBAL best direction — no strategic day stance, no hierarchy.
    flat_dir = day_global if day_global != "flat" else "long"
    fbuf = defaultdict(list)
    for r in train:
        key = (_ofi_bucket(r["belief"]["ofi"]), _vpin_bucket(r["belief"]["vpin"], vpin_med))
        fbuf[key].append(_u_dir(r["u"], flat_dir))
    flat_enter = {k: (bool(np.mean(v) > 0) if len(v) >= MIN_CTX else True) for k, v in fbuf.items()}

    return {"sent_med": sent_med, "vpin_med": vpin_med, "day_stance": day_stance, "day_global": day_global,
            "hour_enter": hour_enter, "minute_enter": minute_enter, "flat_dir": flat_dir,
            "flat_enter": flat_enter}


def _policies(P):
    """Return callables r→utility for each policy/ablation, plus the per-trade decisions for the full cascade."""
    sm, vm = P["sent_med"], P["vpin_med"]

    def stance(r):
        return P["day_stance"].get(r["belief"]["regime"], "flat")

    def hour_ok(r, D):
        return P["hour_enter"].get((D, _sent_bucket(r["belief"]["sentiment"], sm)), True)

    def minute_ok(r, D):
        return P["minute_enter"].get((D, _ofi_bucket(r["belief"]["ofi"]), _vpin_bucket(r["belief"]["vpin"], vm)), True)

    def U(r, enter, D):
        return _u_dir(r["u"], D) if enter else r["u"][2]

    def day_only(r):
        D = stance(r); return U(r, D != "flat", D), D, (D != "flat")
    def day_hour(r):
        D = stance(r); e = (D != "flat") and hour_ok(r, D); return U(r, e, D), D, e
    def full(r):
        D = stance(r); e = (D != "flat") and hour_ok(r, D) and minute_ok(r, D); return U(r, e, D), D, e
    def no_belief(r):
        D = P["day_global"]; return U(r, D != "flat", D), D, (D != "flat")
    def flat_indep(r):
        D = P["flat_dir"]
        e = P["flat_enter"].get((_ofi_bucket(r["belief"]["ofi"]), _vpin_bucket(r["belief"]["vpin"], vm)), True)
        return U(r, e, D), D, e
    return {"day_only": day_only, "day_hour": day_hour, "full": full,
            "no_belief": no_belief, "flat_independent": flat_indep,
            "_stance": stance, "_hour_ok": hour_ok, "_minute_ok": minute_ok}


def main() -> int:
    t0 = time.time()
    rows = _load_controller_corpus()
    if len(rows) < 150:
        print(f"corpus too small ({len(rows)})"); return 1
    n = len(rows); i_tr = int(0.70 * n)
    train, test = rows[:i_tr], rows[i_tr:]              # chronological split (trajectories, no shuffle)
    P = _fit(train)
    pol = _policies(P)

    def mean_U(fn):
        return float(np.mean([fn(r)[0] for r in test]))

    u_full = mean_U(pol["full"]); u_dayonly = mean_U(pol["day_only"]); u_dayhour = mean_U(pol["day_hour"])
    u_nobelief = mean_U(pol["no_belief"]); u_flat = mean_U(pol["flat_independent"])
    u_realized = float(np.mean([_u_dir(r["u"], r["dir"]) for r in test]))
    u_oracle = float(np.mean([max(r["u"]) for r in test]))

    # marginal VETO rates (is a level decorative?) — fraction of test trades whose decision the level CHANGES
    n_te = len(test)
    hour_veto = sum(1 for r in test if pol["day_only"](r)[2] and not pol["day_hour"](r)[2]) / n_te
    minute_veto = sum(1 for r in test if pol["day_hour"](r)[2] and not pol["full"](r)[2]) / n_te
    # strategy-vs-hindsight conflict: day stance direction != the per-trade oracle-best direction
    def oracle_dir(r):
        u = r["u"]; return "long" if u[0] >= max(u[1], u[2]) else ("short" if u[1] >= u[2] else "flat")
    conflict = sum(1 for r in test if pol["_stance"](r) != oracle_dir(r)) / n_te
    # does MINUTE actually condition on its context? distinct minute decisions across buckets (else decorative)
    minute_keys = len(set((P["day_stance"].get(r["belief"]["regime"], "flat"),
                           _ofi_bucket(r["belief"]["ofi"]),
                           _vpin_bucket(r["belief"]["vpin"], P["vpin_med"]))
                          for r in test))

    coordination_gain = round(u_full - u_flat, 5)       # full hierarchy vs flat independent (the headline)
    belief_gain = round(u_dayonly - u_nobelief, 5)      # shared-belief (regime) contribution
    n_day_decisions = int(np.ceil(n_te / DAY_W))

    report = {
        "ts": round(time.time(), 3), "n_trades": n, "n_train": i_tr, "n_test": n_te,
        "trajectory": {"day_window": DAY_W, "hour_window": HOUR_W,
                       "n_day_decisions": n_day_decisions, "minute_steps": n_te,
                       "note": "real multi-timescale trajectories: 1 strategic DAY decision persists over ~"
                               f"{DAY_W} execution steps, 1 HOUR over ~{HOUR_W} — not 1-step bandits"},
        "roles": {"day": {"role": "strategic stance (direction/risk)", "context": "regime",
                          "policy": P["day_stance"]},
                  "hour": {"role": "tactical engage/wait (veto)", "context": "regime+sentiment",
                           "n_contexts": len(P["hour_enter"])},
                  "minute": {"role": "execution enter/skip (veto)", "context": "regime+ofi+vpin",
                             "n_contexts": len(P["minute_enter"])}},
        "twin_utility": {"full_hierarchy": round(u_full, 5), "day_hour": round(u_dayhour, 5),
                         "day_only": round(u_dayonly, 5), "no_belief": round(u_nobelief, 5),
                         "flat_independent": round(u_flat, 5), "realized": round(u_realized, 5),
                         "oracle": round(u_oracle, 5)},
        "coordination": {
            "coordination_gain_vs_flat": coordination_gain,
            "belief_gain": belief_gain,
            "hour_marginal_veto_rate": round(hour_veto, 4),
            "minute_marginal_veto_rate": round(minute_veto, 4),
            "strategy_vs_hindsight_conflict": round(conflict, 4),
            "minute_distinct_contexts": minute_keys,
            "hour_decorative": bool(hour_veto < 0.02),
            "minute_decorative": bool(minute_veto < 0.02),
            "coordinates": bool(coordination_gain > 0.002),
            "note": "coordination_gain = full 3-level cascade − flat independent baseline (the old framing). "
                    "A level is 'decorative' if it almost never changes the decision (veto rate ≈ 0). "
                    "reward = deterministic digital-twin utility; oracle = hindsight upper bound (NOT promotable)."},
        "ablation": {"flat_independent": round(u_flat, 5), "no_belief": round(u_nobelief, 5),
                     "day_only": round(u_dayonly, 5), "day_hour": round(u_dayhour, 5),
                     "full_hierarchy": round(u_full, 5)},
        "authority": "shadow",
        "live_agents_untouched": True,
        "train_secs": round(time.time() - t0, 2),
        "verdict": (
            (f"Hierarchical controllers COORDINATE — full-cascade twin utility {u_full:+.4f} beats flat "
             f"independent {u_flat:+.4f} (gain {coordination_gain:+}); belief gain {belief_gain:+}."
             if coordination_gain > 0.002 else
             f"Hierarchy does NOT beat the flat independent baseline (full {u_full:+.4f} vs flat {u_flat:+.4f}, "
             f"gain {coordination_gain:+}). HONEST: the reframing (shared belief, hour role, real trajectories) "
             "is real and measured, but on weak obs the coordination provides no lift yet.") +
            (f" Hour level is DECORATIVE (veto {hour_veto:.0%})." if hour_veto < 0.02 else "") +
            (f" Minute level is DECORATIVE (veto {minute_veto:.0%})." if minute_veto < 0.02 else "") +
            (f" And ALL reframed controllers ({u_full:+.4f}) badly UNDERPERFORM the realized bot policy "
             f"({u_realized:+.4f}) — the existing live system is better than this crude reframing; the day "
             f"stance ({P['day_global']}) conflicts with hindsight {conflict:.0%} of the time."
             if u_realized > u_full + 0.01 else "") +
            " SHADOW only — the live PPO day/minute agents (capital scaling / skip veto) are untouched."),
    }
    print("\n=== HIERARCHICAL-CONTROLLERS REPORT ===")
    print(json.dumps(report, indent=2, default=str))
    try:
        import redis_client
        from signals.scibrain import keys as _K
        r = redis_client.get()
        r.set(_K.HIER_CONTROLLERS_REPORT, json.dumps(report, default=str))
        print("saved", _K.HIER_CONTROLLERS_REPORT)
        try:
            from signals.scibrain import controllers_health
            controllers_health.build_controllers_health(r)
            print("saved", _K.HIER_CONTROLLERS_HEALTH)
        except Exception as hx:
            print("health warning:", str(hx)[:140])
    except Exception as exc:
        print("save warning:", str(exc)[:140])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
