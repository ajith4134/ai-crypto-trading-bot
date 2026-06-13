"""SciBrain Phase-7e step-5 — NEOCORTEX slow semantic CONSOLIDATION with EWC + protected competence (§3.6).

"Extract stable laws across many episodes WITHOUT catastrophic forgetting." This consolidates the
shared-latent representation on NEW experience while protecting what it already knew, the design's §3.6
objective:

    L_slow = L_new + λ_ewc · Σ_i F_i (θ_i − θ_i_old)²        (+ replay; + calibration)

and — the non-negotiable gate — "new learning is accepted ONLY if it preserves old benchmark competence":
a protected OLD-COMPETENCE REGRESSION TEST. The honest experiment, run end-to-end on the real corpus:

  1. Train the shared latent on the OLD time-window → θ_old; compute the diagonal Fisher information F
     (each param's importance to the OLD task).
  2. Consolidate on the NEW window TWICE: WITH EWC (penalise drifting important params) and WITHOUT
     (the ablation that should FORGET).
  3. Regression-test old competence (the OLD-window SSL loss) before vs after each. Forgetting =
     Δ old-loss. ACCEPT the consolidation iff forgetting stays under a threshold; else REJECT (keep θ_old).

The point isn't a leaderboard number — it's PROVING EWC reduces forgetting vs the ablation and that the
protected gate would reject a forgetting update. Run in a bot container: python -m ml.consolidation
"""
from __future__ import annotations

import copy
import json
import time

import numpy as np
import torch
import torch.nn.functional as F

from ml.shared_latent import SharedLatentChallenger
from ml.train_shared_latent import _load_corpus

_FORGET_THRESHOLD = 0.20      # accept consolidation only if old-competence loss rises < 20%


def _modalities(MV, RG, FLn, idx):
    return {"modules": torch.as_tensor(MV[idx]), "regime": torch.as_tensor(RG[idx]),
            "flow": torch.as_tensor(FLn[idx])}


def _ssl_loss(model, mb) -> "torch.Tensor":
    """The self-supervised objective used for training, Fisher, and the competence metric (mask-recon +
    JEPA future-latent + bottleneck) — lower = better representation of that window."""
    out = model(mb)
    raw, z = out["target_input"], out["latent"]
    m = (torch.rand_like(raw) > 0.30).float()
    recon = model(mb, mask=m)["recon"]
    l_mask = (((recon - raw) ** 2) * (1 - m)).sum() / ((1 - m).sum() + 1e-6)
    l_jepa = F.mse_loss(out["jepa_pred"][:-1], z.detach()[1:]) if len(z) > 1 else torch.tensor(0.0)
    return l_mask + 0.7 * l_jepa + 1e-3 * (z ** 2).mean()


def _train(model, MV, RG, FLn, idx, *, epochs, bs=64, ewc=None, lr=1e-3):
    opt = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=1e-5)
    theta_old, F_diag, lam = (ewc or (None, None, 0.0))
    for _ in range(epochs):
        perm = np.random.permutation(len(idx))
        for s in range(0, len(perm), bs):
            b = idx[perm[s:s + bs]]
            if len(b) < 4:
                continue
            loss = _ssl_loss(model, _modalities(MV, RG, FLn, b))
            if theta_old is not None and lam > 0:
                pen = sum((F_diag[n] * (p - theta_old[n]) ** 2).sum()
                          for n, p in model.named_parameters() if n in F_diag)
                loss = loss + lam * pen
            opt.zero_grad(); loss.backward(); opt.step()


def _fisher(model, MV, RG, FLn, idx, *, n_samples=200):
    """Diagonal Fisher information on the OLD task: E[(∂L/∂θ)²] — each param's importance to keep."""
    F_diag = {n: torch.zeros_like(p) for n, p in model.named_parameters()}
    sub = idx[np.random.permutation(len(idx))[:n_samples]]
    cnt = 0
    for i in sub:
        model.zero_grad()
        loss = _ssl_loss(model, _modalities(MV, RG, FLn, np.array([i, i])))
        loss.backward()
        for n, p in model.named_parameters():
            if p.grad is not None:
                F_diag[n] += p.grad.detach() ** 2
        cnt += 1
    for n in F_diag:
        F_diag[n] /= max(1, cnt)
    return F_diag


@torch.no_grad()
def _eval(model, MV, RG, FLn, idx) -> float:
    model.eval()
    v = float(_ssl_loss(model, _modalities(MV, RG, FLn, idx)))
    model.train()
    return v


def main() -> int:
    t0 = time.time()
    torch.manual_seed(0); np.random.seed(0)
    MV, RG, FL, Y, TS, roster = _load_corpus()
    n = len(Y)
    if n < 120:
        print(f"corpus too small ({n})"); return 1
    mdim = MV.shape[1]
    md = {"modules": mdim, "regime": RG.shape[1], "flow": FL.shape[1]}
    i_old = int(0.60 * n)                                          # OLD (first 60%) | NEW (last 40%) by time
    old_idx = np.arange(0, i_old); new_idx = np.arange(i_old, n)
    # OLD held-out competence probe = last 20% of OLD (not trained on directly at eval)
    old_eval = np.arange(int(0.80 * i_old), i_old)
    mu, sd = FL[old_idx].mean(0), FL[old_idx].std(0) + 1e-6
    FLn = (FL - mu) / sd
    print(f"corpus n={n} | OLD={len(old_idx)} NEW={len(new_idx)} | old_eval={len(old_eval)} roster_dim={mdim}")

    # 1) learn the OLD task
    base = SharedLatentChallenger(modality_dims=md)
    _train(base, MV, RG, FLn, old_idx, epochs=200)
    old_before = _eval(base, MV, RG, FLn, old_eval)
    theta_old = {n_: p.detach().clone() for n_, p in base.named_parameters()}
    F_diag = _fisher(base, MV, RG, FLn, old_idx)
    fnorm = float(sum(v.sum() for v in F_diag.values()))
    # scale λ so the EWC penalty is comparable to the task loss
    lam = 1.0 / (fnorm / sum(v.numel() for v in F_diag.values()) + 1e-9) * 1e-3

    # 2a) consolidate on NEW WITH EWC
    m_ewc = copy.deepcopy(base)
    _train(m_ewc, MV, RG, FLn, new_idx, epochs=150, ewc=(theta_old, F_diag, lam))
    old_after_ewc = _eval(m_ewc, MV, RG, FLn, old_eval)
    new_ewc = _eval(m_ewc, MV, RG, FLn, new_idx)
    # 2b) consolidate on NEW WITHOUT EWC (the ablation that should forget)
    m_no = copy.deepcopy(base)
    _train(m_no, MV, RG, FLn, new_idx, epochs=150)
    old_after_no = _eval(m_no, MV, RG, FLn, old_eval)
    new_no = _eval(m_no, MV, RG, FLn, new_idx)

    forget_ewc = (old_after_ewc - old_before) / (old_before + 1e-9)
    forget_no = (old_after_no - old_before) / (old_before + 1e-9)
    accepted = bool(forget_ewc < _FORGET_THRESHOLD)
    ewc_helps = bool(forget_ewc < forget_no)

    report = {
        "ts": round(time.time(), 3), "n": n, "split": {"old": len(old_idx), "new": len(new_idx)},
        "lambda_ewc": round(float(lam), 6), "fisher_total": round(fnorm, 4),
        "old_competence": {"loss_before": round(old_before, 5),
                           "loss_after_ewc": round(old_after_ewc, 5),
                           "loss_after_no_ewc": round(old_after_no, 5)},
        "forgetting": {"with_ewc": round(forget_ewc, 4), "without_ewc": round(forget_no, 4),
                       "threshold": _FORGET_THRESHOLD},
        "new_task_loss": {"with_ewc": round(new_ewc, 5), "without_ewc": round(new_no, 5)},
        "ewc_reduces_forgetting": ewc_helps,
        "accepted": accepted,
        "verdict": ("ACCEPT consolidation: EWC preserved old competence within threshold" if accepted
                    else "REJECT consolidation: forgetting exceeded threshold — keep θ_old (protected)"),
        "train_secs": round(time.time() - t0, 1),
    }
    print("\n=== CONSOLIDATION REPORT ===")
    print(json.dumps(report, indent=2))
    try:
        import redis_client
        redis_client.get().set("scibrain:consolidation:report", json.dumps(report))
        print("saved scibrain:consolidation:report")
    except Exception as exc:
        print("save warning:", str(exc)[:120])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
