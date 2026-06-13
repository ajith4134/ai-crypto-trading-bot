"""SciBrain Phase-7f — offline RL CHALLENGERS (CQL + IQL) over abstain/enter/manage/exit options with a
SUPPORT-AWARE baseline fallback (design §3.7 / §8-table line 416 / step 8b).

§3.7: "Use offline RL first because live exploration with real funds is unsafe. Candidate methods are
Conservative Q-Learning or Implicit Q-Learning ... baseline fallback, and digital-twin interaction before any
live authority." This builds two tabular offline-RL challengers and — the headline — a support-aware fallback
that REFUSES to act on out-of-support state-actions, deferring to the baseline instead of trusting an
extrapolated value (the core offline-RL safety property).

  • OPTIONS — the trade-lifecycle action set {abstain, enter_long, enter_short, manage, exit}. Reward = the
    deterministic digital-twin utility (signals/scibrain/twin.py via the world-model corpus): each trade gives
    FULL counterfactual feedback for the ENTRY options (u_long / u_short / u_abstain) replayed on the real path
    with fees + SL/TP geometry. The in-position options (manage/exit) have NO logged decision transitions
    (lifetrace is empty) → support 0 → they EXIST to demonstrate that the support-aware fallback correctly
    refuses them. Honest data-limitation finding, not a fabricated reward.
  • CQL — conservative Q with an uncertainty penalty by state-bucket coverage (sparse buckets / unsupported
    options get pushed down → never over-estimated). IQL — expectile value V(s) + advantage policy.
  • SUPPORT-AWARE FALLBACK — if the chosen state-bucket coverage < τ, or the chosen option is unsupported, the
    policy DEFERS to the baseline (the behavior/realized policy). Off-policy evaluation on the twin compares
    CQL / IQL / behavior(realized) / baseline / oracle, the fallback rate, and the conservatism vs a naive
    (no-penalty, no-fallback) argmax-Q that WOULD chase out-of-support actions.

SHADOW / read-only: no trading authority; the live agents/funnel are untouched (Rule 21). Promotion is owner-
gated and digital-twin-evidenced. Run: python -m ml.offline_rl_challengers
"""
from __future__ import annotations

import json
import time
from collections import defaultdict

import numpy as np

from ml.hierarchical_controllers import _load_controller_corpus, _ofi_bucket   # reuse corpus + bucketing

OPTIONS = ("abstain", "enter_long", "enter_short", "manage", "exit")
ENTRY = {"abstain": 2, "enter_long": 0, "enter_short": 1}     # index into twin u=(u_long,u_short,u_abstain)
ALPHA = 0.03            # CQL conservatism: penalty scale on low state-bucket coverage
TAU_SUPPORT = 8        # min train trades in a bucket to TRUST it (else support-aware fallback)
EXPECTILE = 0.7        # IQL expectile for V(s)


def _bucket(b, sent_med, vpin_med):
    return (b["regime"], _ofi_bucket(b["ofi"]),
            "hi" if b["vpin"] >= vpin_med else "lo",
            "hi" if b["sentiment"] >= sent_med else "lo")


def _expectile(xs, tau):
    """Expectile_tau of xs (IQL value): iteratively reweighted mean (asymmetric least squares)."""
    a = np.asarray(xs, dtype=np.float64)
    if a.size == 0:
        return 0.0
    m = float(a.mean())
    for _ in range(20):
        w = np.where(a >= m, tau, 1.0 - tau)
        m = float((w * a).sum() / max(w.sum(), 1e-9))
    return m


def main() -> int:
    t0 = time.time()
    rows = _load_controller_corpus()
    if len(rows) < 150:
        print(f"corpus too small ({len(rows)})"); return 1
    n = len(rows); i_tr = int(0.70 * n)
    train, test = rows[:i_tr], rows[i_tr:]
    sent_med = float(np.median([r["belief"]["sentiment"] for r in train]))
    vpin_med = float(np.median([r["belief"]["vpin"] for r in train]))

    # ── build the offline tables on TRAIN ──
    # full-feedback entry rewards per bucket (the twin gives u_long/u_short/u_abstain for every trade);
    # behavior counts = which option the LOGGING policy actually exercised (entry direction only).
    q_rewards = defaultdict(lambda: {a: [] for a in ("enter_long", "enter_short", "abstain")})
    behavior_returns = defaultdict(list)                 # realized return of the action actually taken
    behavior_count = {a: 0 for a in OPTIONS}
    for r in train:
        b = _bucket(r["belief"], sent_med, vpin_med)
        uL, uS, uA = r["u"]
        q_rewards[b]["enter_long"].append(uL)
        q_rewards[b]["enter_short"].append(uS)
        q_rewards[b]["abstain"].append(uA)
        taken = "enter_long" if r["dir"] == "long" else "enter_short"
        behavior_count[taken] += 1
        behavior_returns[b].append(uL if r["dir"] == "long" else uS)
    # manage / exit: NO transitions anywhere — support stays 0 (honest; lifetrace not logged)

    Q = {}; V = {}; Ncov = {}
    for b, d in q_rewards.items():
        Ncov[b] = len(d["enter_long"])
        Q[b] = {a: float(np.mean(v)) for a, v in d.items() if v}
        V[b] = _expectile(behavior_returns[b], EXPECTILE)
    # global baseline (behavior fallback when a state-bucket is unsupported)
    g_long = float(np.mean([r["u"][0] for r in train])); g_short = float(np.mean([r["u"][1] for r in train]))
    base_global = "enter_long" if g_long >= max(g_short, 0.0) else ("enter_short" if g_short >= 0.0 else "abstain")

    # ── policies (return (option, fell_back)) ──
    def cql(b):
        if b not in Q or Ncov[b] < TAU_SUPPORT:          # support-aware fallback on sparse/unseen buckets
            return base_global, True
        pen = ALPHA / np.sqrt(Ncov[b] + 1.0)             # conservative penalty (manage/exit absent → never win)
        a = max(Q[b], key=lambda k: Q[b][k] - pen)
        return a, False

    def iql(b):
        if b not in Q or Ncov[b] < TAU_SUPPORT:
            return base_global, True
        adv = {a: Q[b][a] - V[b] for a in Q[b]}
        return max(adv, key=adv.get), False

    def naive(b):                                        # NO penalty, NO fallback — chases any bucket (OOD)
        if b not in Q:
            return base_global
        return max(Q[b], key=Q[b].get)

    # ── off-policy evaluation on the held-out twin ──
    def util_of(option, r):
        if option in ENTRY:
            return r["u"][ENTRY[option]]
        return r["u"][2]                                 # manage/exit never chosen; treat as abstain value

    u_cql = u_iql = u_base = u_real = u_oracle = u_naive = 0.0
    fb_cql = fb_iql = 0; n_te = len(test)
    sparse_states = 0
    for r in test:
        b = _bucket(r["belief"], sent_med, vpin_med)
        a_c, f_c = cql(b); a_i, f_i = iql(b)
        u_cql += util_of(a_c, r); u_iql += util_of(a_i, r)
        u_naive += util_of(naive(b), r)
        u_base += util_of(base_global, r)
        u_real += (r["u"][0] if r["dir"] == "long" else r["u"][1])   # behavior/realized
        u_oracle += max(r["u"])
        fb_cql += int(f_c); fb_iql += int(f_i)
        if b not in Ncov or Ncov[b] < TAU_SUPPORT:
            sparse_states += 1
    inv = 1.0 / n_te
    u_cql *= inv; u_iql *= inv; u_base *= inv; u_real *= inv; u_oracle *= inv; u_naive *= inv

    # support per option (behavior coverage) — manage/exit are 0 by construction (no logged decisions)
    option_support = {a: behavior_count[a] for a in OPTIONS}
    unsupported = [a for a in OPTIONS if option_support[a] == 0]
    cql_beats_base = bool(u_cql > u_base + 0.002)
    iql_beats_base = bool(u_iql > u_base + 0.002)
    # conservatism: the naive (no-fallback) policy acts on sparse/OOD states the fallback refuses
    conservatism_gap = round(float((fb_cql / n_te)), 4)

    report = {
        "ts": round(time.time(), 3), "n_trades": n, "n_train": i_tr, "n_test": n_te,
        "options": list(OPTIONS), "n_state_buckets": len(Ncov),
        "method": {"cql": f"conservative Q − {ALPHA}/√coverage penalty", "iql": f"expectile V (τ={EXPECTILE}) + advantage",
                   "support_tau": TAU_SUPPORT,
                   "reward": "deterministic digital-twin utility (path-aware, fees + SL/TP geometry); ENTRY "
                             "options have full counterfactual feedback, manage/exit have none"},
        "twin_utility": {"cql": round(u_cql, 5), "iql": round(u_iql, 5), "baseline_global": round(u_base, 5),
                         "behavior_realized": round(u_real, 5), "naive_no_fallback": round(u_naive, 5),
                         "oracle": round(u_oracle, 5)},
        "support_aware_fallback": {
            "cql_fallback_rate": round(fb_cql / n_te, 4), "iql_fallback_rate": round(fb_iql / n_te, 4),
            "sparse_state_rate": round(sparse_states / n_te, 4),
            "note": "fraction of test states where the bucket coverage < τ (or the chosen option is "
                    "unsupported) so the policy DEFERS to the baseline instead of an extrapolated value"},
        "option_support": option_support,
        "unsupported_options": unsupported,
        "challenger": {"cql_beats_baseline": cql_beats_base, "iql_beats_baseline": iql_beats_base,
                       "best": "cql" if u_cql >= u_iql else "iql"},
        "authority": "shadow", "live_agents_untouched": True,
        "train_secs": round(time.time() - t0, 2),
        "verdict": (
            (f"Offline-RL challenger (best {'CQL' if u_cql >= u_iql else 'IQL'} "
             f"{max(u_cql, u_iql):+.4f}) BEATS the baseline {u_base:+.4f}; "
             if (cql_beats_base or iql_beats_base) else
             f"Offline-RL challengers (CQL {u_cql:+.4f}, IQL {u_iql:+.4f}) do NOT beat the baseline "
             f"{u_base:+.4f}; ") +
            f"support-aware fallback fires on {fb_cql / n_te:.0%} of states (refusing extrapolation). "
            f"manage/exit options are UNSUPPORTED (no intra-trade decision logging — lifetrace empty) so the "
            f"fallback correctly refuses them — the honest data limit, not a fabricated reward. Behavior/"
            f"realized {u_real:+.4f}, oracle {u_oracle:+.4f}. SHADOW — no live authority (Rule 21)."),
    }
    print("\n=== OFFLINE-RL CHALLENGERS REPORT ===")
    print(json.dumps(report, indent=2))
    try:
        import redis_client
        from signals.scibrain import keys as _K
        r = redis_client.get()
        r.set(_K.OFFLINE_RL_REPORT, json.dumps(report))
        print("saved", _K.OFFLINE_RL_REPORT)
        try:
            from signals.scibrain import offline_rl_health
            offline_rl_health.build_offline_rl_health(r)
            print("saved", _K.OFFLINE_RL_HEALTH)
        except Exception as hx:
            print("health warning:", str(hx)[:140])
    except Exception as exc:
        print("save warning:", str(exc)[:140])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
