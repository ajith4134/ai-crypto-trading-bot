"""SciBrain Phase-7f — META-LEARNING on REAL regime/cohort tasks + PROTECTED COMPETENCE, twin-evaluated in
shadow (design §3.7 / §5.4-7 protected-competence / §5.5 stage-6 meta-learning / §8-419).

UPGRADE of ml/maml.py. The live FOMAML path (adapt_world_model_to_recent_regime) has the exact defects the
spec flags ("task construction is synthetic and weak; adaptation target is too narrow") and the two this task
is named for:
  • SYNTHETIC-ZERO-STATE SHORTCUT — it builds all-zeros RSSM latents and stuffs cap/pnl into slot 0
    (deter/stoch = torch.zeros(...)); the "state" carries no real belief.
  • TARGET LEAKAGE — it writes pnl into the INPUT (stoch[:,0]=pnl/100) and then trains the reward head to
    predict pnl → a degenerate task that learns nothing real.
  • PER-PAIR, NOT PER-REGIME/COHORT — tasks are one-per-trading-pair, not the regime/cohort structure
    meta-learning needs; and it OVERWRITES the shared world-model weights every changepoint (50× so far) with
    no protected-competence guard → catastrophic-forgetting risk.

This shadow module does it correctly and measures whether it actually helps:
  • REAL REGIME/COHORT TASKS — each task = a market COHORT (regime × VPIN-tertile) built from the immutable
    controller corpus, with REAL belief feature vectors [ofi, vpin, sentiment, conviction, bias] (nonzero,
    standardized) and targets = the deterministic digital-twin utilities (u_long, u_short) — full counterfactual
    feedback, NOT the leaked pnl. NO zero-state, NO target in the input.
  • META-LEARN A FAST-ADAPTING INIT (Reptile, first-order, deterministic) — meta-train a shared linear init over
    cohort support sets, then FEW-SHOT adapt to each cohort and evaluate on its held-out query set.
  • HONEST GAINS — adaptation gain (few-shot adapt vs the un-adapted meta-init) and meta gain (meta-init vs
    learning few-shot FROM SCRATCH) and vs a single POOLED model; positive only if fast adaptation truly helps.
  • PROTECTED COMPETENCE (§5.4-7) — contrast the MAML/Reptile protocol (ephemeral per-cohort adapt from a
    PROTECTED shared init) against the OLD-STYLE sequential fine-tune (mutate the shared weights cohort-by-
    cohort, as the live maml does). Re-evaluate the FIRST cohort after the sequence: sequential fine-tune
    FORGETS it; the protected-init MAML does not. That forgetting is exactly why the live overwrite is unsafe.

SHADOW / read-only: no live authority; ml/maml.py and the world-model weights are untouched (Rule 21). The
world model currently has zero planning authority anyway. Promotion owner-gated (§5.5 — stage-6 meta-learning
must pass fixed benchmarks before it receives authority).
Run: python -m ml.meta_regime_tasks
"""
from __future__ import annotations

import json
import time
from collections import defaultdict

import numpy as np

from ml.hierarchical_controllers import _load_controller_corpus

SEED = 17
FEATS = ("ofi", "vpin", "sentiment", "conviction")   # REAL belief features (+ bias appended); NO target, NO zeros
MIN_COHORT = 40        # min rows for a cohort to be a usable task
SUPPORT_K = 24         # few-shot support size per cohort (rest = query)
INNER_STEPS = 12       # inner-loop adaptation steps
INNER_LR = 0.1         # inner adaptation lr (stable on standardized features+targets)
META_LR = 0.3          # Reptile meta step (interpolation toward adapted params)
META_EPOCHS = 60       # Reptile meta-training epochs
GRAD_CLIP = 5.0        # per-step gradient L2-norm clip — keeps the inner SGD stable on heavy-tailed cohorts
WINSOR = (0.01, 0.99)  # winsorize twin-utility targets before standardizing (kill outlier blow-ups)


def _phi(rows, mu, sd):
    """Real standardized feature matrix [n,5] = [ofi,vpin,sentiment,conviction, bias]. No zero-state, no leak."""
    x = np.array([[r["belief"][f] for f in FEATS] for r in rows], dtype=np.float64)
    x = (x - mu) / sd
    return np.hstack([x, np.ones((len(rows), 1))])       # bias column


def _make_y(lo, hi, ymu, ysd):
    """Closure: targets = winsorized + standardized twin utilities (u_long, u_short). Full counterfactual
    feedback, NOT the input/leaked pnl; standardization keeps the MSE gradient O(1) and stable."""
    def _y(rows):
        raw = np.array([[r["u"][0], r["u"][1]] for r in rows], dtype=np.float64)
        raw = np.clip(raw, lo, hi)
        return (raw - ymu) / ysd
    return _y


def _mse(W, X, Y):
    return float(np.mean((X @ W - Y) ** 2))


def _adapt(W0, X, Y, steps=INNER_STEPS, lr=INNER_LR):
    """Inner loop: k clipped-SGD steps of linear regression from init W0 on (X,Y). Deterministic + stable."""
    W = W0.copy(); n = max(1, len(X))
    for _ in range(steps):
        grad = (2.0 / n) * X.T @ (X @ W - Y)
        gn = float(np.linalg.norm(grad))
        if gn > GRAD_CLIP:
            grad = grad * (GRAD_CLIP / gn)               # clip → no blow-up on outlier cohorts
        W = W - lr * grad
    return W


def main() -> int:
    t0 = time.time()
    rng = np.random.default_rng(SEED)
    rows = _load_controller_corpus()
    if len(rows) < 250:
        print(f"corpus too small ({len(rows)})"); return 1

    # ── standardize on the full corpus (real, nonzero belief features) ──
    Xall = np.array([[r["belief"][f] for f in FEATS] for r in rows], dtype=np.float64)
    mu = Xall.mean(0); sd = Xall.std(0); sd[sd < 1e-9] = 1.0
    state_real = bool(np.all(sd > 1e-6))                 # real, varying state — NOT a zero-state shortcut

    # ── target normalizer: winsorize + standardize twin utilities (heavy-tailed → stable MSE) ──
    Uall = np.array([[r["u"][0], r["u"][1]] for r in rows], dtype=np.float64)
    ylo, yhi = np.quantile(Uall, WINSOR[0]), np.quantile(Uall, WINSOR[1])
    Uw = np.clip(Uall, ylo, yhi)
    ymu = Uw.mean(0); ysd = Uw.std(0); ysd[ysd < 1e-9] = 1.0
    _y = _make_y(ylo, yhi, ymu, ysd)                     # losses below are in standardized-utility units

    # ── REAL COHORT TASKS: regime × VPIN-tertile ──
    vlo, vhi = np.quantile([r["belief"]["vpin"] for r in rows], [1 / 3, 2 / 3])
    def _vt(v):
        return "vlo" if v < vlo else ("vhi" if v >= vhi else "vmid")
    cohorts = defaultdict(list)
    for r in rows:
        cohorts[(r["belief"]["regime"], _vt(r["belief"]["vpin"]))].append(r)
    tasks = {k: v for k, v in cohorts.items() if len(v) >= MIN_COHORT}
    if len(tasks) < 3:
        print(f"too few populated cohorts ({len(tasks)})"); return 1

    # per-task support/query split (few-shot support, held-out query)
    split = {}
    for k, v in tasks.items():
        idx = rng.permutation(len(v))
        ksup = min(SUPPORT_K, len(v) // 2)
        sup = [v[i] for i in idx[:ksup]]; qry = [v[i] for i in idx[ksup:]]
        split[k] = {"Xs": _phi(sup, mu, sd), "Ys": _y(sup), "Xq": _phi(qry, mu, sd), "Yq": _y(qry),
                    "n": len(v), "n_sup": len(sup), "n_qry": len(qry)}
    task_keys = sorted(split.keys())
    n_feat = split[task_keys[0]]["Xs"].shape[1]

    # ── META-TRAIN a fast-adapting init (Reptile, first-order, deterministic) ──
    W = np.zeros((n_feat, 2))
    for _ in range(META_EPOCHS):
        upd = np.zeros_like(W)
        for k in task_keys:
            Wk = _adapt(W, split[k]["Xs"], split[k]["Ys"])
            upd += (Wk - W)
        W = W + META_LR * (upd / len(task_keys))         # Reptile meta-step toward the adapted params
    meta_init = W.copy()

    # POOLED baseline: a single model fit on ALL support data (no per-task adaptation)
    Xpool = np.vstack([split[k]["Xs"] for k in task_keys]); Ypool = np.vstack([split[k]["Ys"] for k in task_keys])
    W_pool = _adapt(np.zeros((n_feat, 2)), Xpool, Ypool, steps=400, lr=INNER_LR)

    # ── FEW-SHOT EVAL on each cohort's held-out QUERY ──
    per = {}
    for k in task_keys:
        s = split[k]
        l_noadapt = _mse(meta_init, s["Xq"], s["Yq"])                       # meta-init, no inner steps
        l_maml = _mse(_adapt(meta_init, s["Xs"], s["Ys"]), s["Xq"], s["Yq"])  # few-shot adapt from meta-init
        l_scratch = _mse(_adapt(np.zeros((n_feat, 2)), s["Xs"], s["Ys"]), s["Xq"], s["Yq"])  # from scratch
        l_pool = _mse(W_pool, s["Xq"], s["Yq"])                            # one pooled model
        per[" / ".join(k)] = {"n": s["n"], "n_sup": s["n_sup"], "n_qry": s["n_qry"],
                              "noadapt": round(l_noadapt, 5), "maml": round(l_maml, 5),
                              "scratch": round(l_scratch, 5), "pooled": round(l_pool, 5)}
    mean_noadapt = float(np.mean([p["noadapt"] for p in per.values()]))
    mean_maml = float(np.mean([p["maml"] for p in per.values()]))
    mean_scratch = float(np.mean([p["scratch"] for p in per.values()]))
    mean_pool = float(np.mean([p["pooled"] for p in per.values()]))
    adaptation_gain = round(mean_noadapt - mean_maml, 5)     # +ve = few-shot adapt lowers query loss
    meta_gain = round(mean_scratch - mean_maml, 5)           # +ve = meta-init beats from-scratch few-shot
    pool_gain = round(mean_pool - mean_maml, 5)              # +ve = per-task adapt beats one pooled model
    adaptation_reduces_loss = bool(adaptation_gain > 1e-4 and meta_gain > 1e-4)  # vs no-adapt & scratch
    beats_pooled = bool(pool_gain > 1e-4)                    # the honest bar: does per-task adapt beat 1 pooled model?
    adaptation_helps = bool(adaptation_reduces_loss and beats_pooled)  # earns its keep only if it ALSO beats pooled

    # ── PROTECTED COMPETENCE (§5.4-7): MAML protected-init vs OLD-STYLE sequential fine-tune ──
    first = task_keys[0]                                     # the "old/protected" cohort
    Xq0, Yq0 = split[first]["Xq"], split[first]["Yq"]
    # MAML: protected shared init untouched; old cohort served by ephemeral adapt from meta_init.
    maml_old_after = _mse(_adapt(meta_init, split[first]["Xs"], split[first]["Ys"]), Xq0, Yq0)
    # Sequential fine-tune (what the live maml does): mutate the SAME weights cohort-by-cohort.
    Wseq = meta_init.copy()
    Wseq = _adapt(Wseq, split[first]["Xs"], split[first]["Ys"])
    seq_old_before = _mse(Wseq, Xq0, Yq0)                    # right after learning the old cohort
    for k in task_keys[1:]:                                  # then drift through every later cohort
        Wseq = _adapt(Wseq, split[k]["Xs"], split[k]["Ys"])
    seq_old_after = _mse(Wseq, Xq0, Yq0)                     # old cohort RE-evaluated at the end
    finetune_forgetting = round(seq_old_after - seq_old_before, 5)     # +ve = sequential fine-tune forgot it
    maml_forgetting = 0.0                                              # 0 by construction (protected init never mutated)
    forgetting_demonstrated = bool(finetune_forgetting > 1e-3)         # does the naive baseline actually forget?
    # The MAML protocol protects competence by construction (it never overwrites the shared init). When the
    # naive baseline DOES forget, MAML's preservation is a clear win; when it doesn't (positive transfer on
    # similar cohorts), forgetting simply isn't the active failure here — both stated honestly.
    competence_protected = bool(maml_forgetting <= 1e-6)
    maml_wins_on_old = bool(maml_old_after <= seq_old_after + 1e-6)    # did MAML also end up better on the old cohort?

    target_leakage = False                                   # target = twin u; never an input feature (vs live maml)

    report = {
        "ts": round(time.time(), 3), "n_trades": len(rows), "n_cohorts": len(task_keys),
        "cohorts": [" / ".join(k) for k in task_keys],
        "config": {"feats": list(FEATS), "support_k": SUPPORT_K, "inner_steps": INNER_STEPS,
                   "inner_lr": INNER_LR, "meta_lr": META_LR, "meta_epochs": META_EPOCHS, "algo": "Reptile (FO)"},
        "integrity": {"state_real": state_real, "target_leakage": target_leakage,
                      "note": "REAL belief features (nonzero, varying) → twin-utility targets; the target is "
                              "never an input. Fixes the live maml's synthetic-zero-state + pnl-in-input leak."},
        "few_shot": {"support_k": SUPPORT_K, "loss_units": "MSE on winsorized z-scored twin utility",
                     "mean_noadapt": round(mean_noadapt, 5), "mean_maml": round(mean_maml, 5),
                     "mean_scratch": round(mean_scratch, 5), "mean_pooled": round(mean_pool, 5),
                     "adaptation_gain": adaptation_gain, "meta_gain": meta_gain, "pool_gain": pool_gain,
                     "adaptation_reduces_loss": adaptation_reduces_loss, "beats_pooled": beats_pooled,
                     "adaptation_helps": adaptation_helps, "per_cohort": per},
        "protected_competence": {"protected_cohort": " / ".join(first),
                                 "maml_old_loss": round(maml_old_after, 5),
                                 "seq_old_before": round(seq_old_before, 5),
                                 "seq_old_after": round(seq_old_after, 5),
                                 "finetune_forgetting": finetune_forgetting, "maml_forgetting": maml_forgetting,
                                 "competence_protected": competence_protected,
                                 "forgetting_demonstrated": forgetting_demonstrated,
                                 "maml_wins_on_old": maml_wins_on_old,
                                 "note": "MAML adapts an EPHEMERAL copy from a PROTECTED shared init → old-cohort "
                                         "competence preserved by construction (maml_forgetting=0). The live-style "
                                         "sequential fine-tune mutates the SHARED weights cohort-by-cohort (what "
                                         "ml/maml.py does every changepoint). finetune_forgetting>0 means it "
                                         "degrades the old cohort; <=0 means positive transfer (no forgetting to "
                                         "protect against on this corpus). Either way the live overwrite has NO "
                                         "competence guard — that is the risk this test adds."},
        "authority": "shadow", "live_agents_untouched": True, "live_maml_untouched": True,
        "train_secs": round(time.time() - t0, 2),
        "verdict": (
            (f"Meta-learned init adapts few-shot to REAL regime/cohort tasks AND beats the pooled model — "
             f"adaptation gain {adaptation_gain:+} (query MSE {mean_noadapt:.4f}→{mean_maml:.4f}), meta gain vs "
             f"from-scratch {meta_gain:+}, pool gain {pool_gain:+}. "
             if adaptation_helps else
             f"Few-shot adaptation lowers loss vs no-adapt ({adaptation_gain:+}) and from-scratch ({meta_gain:+}), "
             f"but a single POOLED linear model is BETTER (pool gain {pool_gain:+}) — so meta-learning does NOT "
             f"earn its keep on this corpus yet. HONEST: the real regime×VPIN cohort tasks + protected-competence "
             "test are built and measured; the few-shot edge isn't there. ") +
            (f"PROTECTED COMPETENCE: MAML preserves the old cohort '{' / '.join(first)}' by construction "
             f"(maml_forgetting 0); the live-style sequential fine-tune {'FORGETS it to' if forgetting_demonstrated else 'does not forget here (positive transfer,'} "
             f"{seq_old_after:.4f}{')' if not forgetting_demonstrated else ''} (Δ {finetune_forgetting:+}) — but it has no "
             "competence guard, which is the risk this test adds.") +
            " Fixes the live maml's synthetic-zero-state + target-leak + per-pair tasks. SHADOW — no live "
            "authority; ml/maml.py untouched (Rule 21)."),
    }
    print("\n=== META-REGIME-TASKS REPORT ===")
    print(json.dumps(report, indent=2))
    try:
        import redis_client
        from signals.scibrain import keys as _K
        r = redis_client.get()
        r.set(_K.META_LEARNING_REPORT, json.dumps(report))
        print("saved", _K.META_LEARNING_REPORT)
        try:
            from signals.scibrain import meta_learning_health
            meta_learning_health.build_meta_learning_health(r)
            print("saved", _K.META_LEARNING_HEALTH)
        except Exception as hx:
            print("health warning:", str(hx)[:140])
    except Exception as exc:
        print("save warning:", str(exc)[:140])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
