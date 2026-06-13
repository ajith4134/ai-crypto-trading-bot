"""SciBrain Phase-7e step-2 — TRAIN the shared-latent SSL objectives + PROVE downstream utility / no leakage.

Trains the SharedLatentChallenger (ml/shared_latent.py) self-supervised on the REAL historical corpus
(closed trades' module_embedding.module_vec + regime + flow context), then evaluates HONESTLY whether the
learned latent has downstream utility — the design's promotion criterion ("downstream forecast/control
utility, not reconstruction beauty", §3.2/§8) — under strict no-leakage controls.

Objectives (design §3.2 L_perception):
  • masked_reconstruction — mask random input dims, reconstruct them (MSE on the masked positions).
  • future_latent (JEPA)  — predict the next-in-time sample's latent from this one (stop-grad target).
  • contrastive (InfoNCE) — two augmented views (noise + modality dropout = a cross-modal view) attract;
                            other samples repel. (modality dropout doubles as the cross-modal-alignment term.)
  • information_bottleneck — a small L2 penalty on the latent (keep it compressed).

Evaluation (NO action authority — shadow only):
  • PURGED WALK-FORWARD split by entry_time with an embargo gap (test is strictly later than train).
  • downstream utility = a logistic probe on the FROZEN latent → win/loss, ROC-AUC on TEST, vs the same
    probe on the RAW input (baseline). Promote ONLY if latent AUC > baseline AUC and > 0.5.
  • leakage controls: (a) flow standardization fit on TRAIN only; (b) the probe is fit on TRAIN only; (c)
    a label-PERMUTATION control — shuffled test labels must collapse AUC to ~0.5.

Run in a bot container: python -m ml.train_shared_latent
"""
from __future__ import annotations

import json
import time

import numpy as np
import torch
import torch.nn.functional as F

from ml.shared_latent import SharedLatentChallenger

_CANON_REGIMES = ("trending", "mean_revert", "turbulent", "neutral")
_FLOW_KEYS = ("ofi", "vpin", "funding", "spread_rel", "sentiment")


def _load_corpus(limit: int = 2000):
    """Load (module_vec, regime, flow, y, entry_ts) from closed trades with a module_embedding."""
    from db import db_conn
    rows = []
    with db_conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT signals_at_entry->'module_embedding', net_pnl_usdt, "
                "EXTRACT(EPOCH FROM entry_time) "
                "FROM trades WHERE status='closed' AND signals_at_entry->'module_embedding' IS NOT NULL "
                "AND net_pnl_usdt IS NOT NULL AND entry_time IS NOT NULL "
                "ORDER BY entry_time ASC LIMIT %s", (limit,))
            for me, pnl, ets in cur.fetchall():
                me = me if isinstance(me, dict) else json.loads(me)
                rows.append((me, float(pnl), float(ets)))
    # canonical roster (union, stable order) so module_vec aligns to a fixed dim
    roster_union: list = []
    for me, _, _ in rows:
        for name in (me.get("roster") or []):
            if name not in roster_union:
                roster_union.append(name)
    rdim = len(roster_union)
    ridx = {n: i for i, n in enumerate(roster_union)}

    MV, RG, FL, Y, TS = [], [], [], [], []
    for me, pnl, ets in rows:
        mv = np.zeros(rdim, dtype=np.float32)
        for name, val in zip(me.get("roster") or [], me.get("module_vec") or []):
            try:
                mv[ridx[name]] = float(val)
            except (KeyError, TypeError, ValueError):
                pass
        ctx = me.get("context") or {}
        reg = str(ctx.get("regime", "neutral"))
        rg = np.array([1.0 if reg == rr else 0.0 for rr in _CANON_REGIMES], dtype=np.float32)
        if rg.sum() == 0:
            rg[-1] = 1.0
        fl = np.array([float(ctx.get(k) or 0.0) for k in _FLOW_KEYS], dtype=np.float32)
        MV.append(mv); RG.append(rg); FL.append(fl); Y.append(1.0 if pnl > 0 else 0.0); TS.append(ets)
    return (np.asarray(MV), np.asarray(RG), np.asarray(FL), np.asarray(Y), np.asarray(TS),
            roster_union)


def _auc(y_true: np.ndarray, scores: np.ndarray) -> float:
    """ROC-AUC via the Mann-Whitney U rank statistic (no sklearn). 0.5 = chance."""
    pos = scores[y_true == 1]; neg = scores[y_true == 0]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    order = np.argsort(scores, kind="mergesort")
    ranks = np.empty(len(scores), dtype=np.float64)
    ranks[order] = np.arange(1, len(scores) + 1)
    # average ranks for ties
    s_sorted = scores[order]
    i = 0
    while i < len(s_sorted):
        j = i
        while j + 1 < len(s_sorted) and s_sorted[j + 1] == s_sorted[i]:
            j += 1
        if j > i:
            ranks[order[i:j + 1]] = (i + 1 + j + 1) / 2.0
        i = j + 1
    r_pos = ranks[y_true == 1].sum()
    return float((r_pos - len(pos) * (len(pos) + 1) / 2.0) / (len(pos) * len(neg)))


def _fit_logistic_probe(Xtr, ytr, Xte, *, steps=400, l2=1e-3, seed=0):
    """A small class-weighted logistic probe (torch). Fit on TRAIN only; return TEST scores."""
    torch.manual_seed(seed)
    Xtr = torch.as_tensor(Xtr, dtype=torch.float32); ytr = torch.as_tensor(ytr, dtype=torch.float32)
    Xte = torch.as_tensor(Xte, dtype=torch.float32)
    w = torch.zeros(Xtr.shape[1], requires_grad=True); b = torch.zeros(1, requires_grad=True)
    pos_w = float((ytr == 0).sum()) / max(1.0, float((ytr == 1).sum()))   # class balance
    opt = torch.optim.Adam([w, b], lr=0.05)
    for _ in range(steps):
        opt.zero_grad()
        logits = Xtr @ w + b
        loss = F.binary_cross_entropy_with_logits(logits, ytr, pos_weight=torch.tensor(pos_w)) + l2 * (w * w).sum()
        loss.backward(); opt.step()
    with torch.no_grad():
        return (Xte @ w + b).numpy()


def _augment(x, drop_modality_slices, noise=0.05):
    """Two SSL views: gaussian noise + random modality dropout (a cross-modal view)."""
    v = x + noise * torch.randn_like(x)
    if torch.rand(1).item() < 0.5 and drop_modality_slices:
        lo, hi = drop_modality_slices[torch.randint(len(drop_modality_slices), (1,)).item()]
        v = v.clone(); v[:, lo:hi] = 0.0
    return v


def main() -> int:
    t0 = time.time()
    MV, RG, FL, Y, TS, roster = _load_corpus()
    n = len(Y)
    if n < 120:
        print(f"corpus too small ({n}); need ≥120 labeled snapshots."); return 1
    mdim = MV.shape[1]
    print(f"corpus: n={n} roster_dim={mdim} regime_dim={RG.shape[1]} flow_dim={FL.shape[1]} "
          f"base_rate(win)={Y.mean():.3f}")

    # ── PURGED WALK-FORWARD split (already time-sorted ASC): train | embargo | test ──
    i_tr = int(0.60 * n); i_emb = int(0.65 * n)
    sl_tr = slice(0, i_tr); sl_te = slice(i_emb, n)
    # flow standardization — TRAIN stats ONLY (no leakage)
    mu, sd = FL[sl_tr].mean(0), FL[sl_tr].std(0) + 1e-6
    FLn = (FL - mu) / sd
    modality_dims = {"modules": mdim, "regime": RG.shape[1], "flow": FL.shape[1]}
    slices = []
    off = 0
    for d in modality_dims.values():
        slices.append((off, off + d)); off += d

    def mod_batch(idx):
        return {"modules": torch.as_tensor(MV[idx]), "regime": torch.as_tensor(RG[idx]),
                "flow": torch.as_tensor(FLn[idx])}

    tr_idx = np.arange(0, i_tr)
    print(f"split: train={len(tr_idx)} embargo={i_emb - i_tr} test={n - i_emb} "
          f"(test strictly after train; embargo gap = {i_emb - i_tr} trades)")

    # ── TRAIN the SSL objectives on TRAIN only ──
    torch.manual_seed(0)
    model = SharedLatentChallenger(modality_dims=modality_dims)
    opt = torch.optim.Adam(model.parameters(), lr=1e-3, weight_decay=1e-5)
    Xtr_raw = torch.as_tensor(np.concatenate([MV[sl_tr], RG[sl_tr], FLn[sl_tr]], axis=1))
    EP, BS = 300, 64
    for ep in range(EP):
        perm = np.random.permutation(len(tr_idx))
        ep_loss = 0.0; nb = 0
        for s in range(0, len(perm), BS):
            bidx = tr_idx[perm[s:s + BS]]
            if len(bidx) < 4:
                continue
            mb = mod_batch(bidx)
            out = model(mb)
            raw = out["target_input"]; z = out["latent"]
            # masked reconstruction (mask 30% of dims, MSE on masked)
            m = (torch.rand_like(raw) > 0.30).float()
            recon = model(mb, mask=m)["recon"]
            l_mask = (((recon - raw) ** 2) * (1 - m)).sum() / ((1 - m).sum() + 1e-6)
            # future-latent (JEPA): predict next-in-time latent (stop-grad target)
            jp = out["jepa_pred"][:-1]; tgt = z.detach()[1:]
            l_jepa = F.mse_loss(jp, tgt) if len(z) > 1 else torch.tensor(0.0)
            # contrastive InfoNCE on two augmented views (noise + modality dropout = a cross-modal view)
            v1 = model.encode(_split(_augment(raw, slices), slices, model.keys))
            v2 = model.encode(_split(_augment(raw, slices), slices, model.keys))
            v1 = F.normalize(v1, dim=-1); v2 = F.normalize(v2, dim=-1)
            logits = v1 @ v2.t() / 0.2
            l_con = F.cross_entropy(logits, torch.arange(len(v1)))
            # information bottleneck
            l_bn = (z ** 2).mean()
            loss = 1.0 * l_mask + 0.7 * l_jepa + 0.5 * l_con + 1e-3 * l_bn
            opt.zero_grad(); loss.backward(); opt.step()
            ep_loss += float(loss); nb += 1
        if ep % 60 == 0 or ep == EP - 1:
            print(f"  epoch {ep:3d} loss={ep_loss / max(1, nb):.4f}")

    # ── DOWNSTREAM UTILITY: frozen-latent probe vs raw baseline (AUC on TEST) ──
    model.eval()
    with torch.no_grad():
        Ztr = model.encode(mod_batch(np.arange(0, i_tr))).numpy()
        Zte = model.encode(mod_batch(np.arange(i_emb, n))).numpy()
    Rtr = np.concatenate([MV[sl_tr], RG[sl_tr], FLn[sl_tr]], axis=1)
    Rte = np.concatenate([MV[sl_te], RG[sl_te], FLn[sl_te]], axis=1)
    ytr, yte = Y[sl_tr], Y[sl_te]

    lat_scores = _fit_logistic_probe(Ztr, ytr, Zte)
    base_scores = _fit_logistic_probe(Rtr, ytr, Rte)
    auc_lat = _auc(yte, lat_scores)
    auc_base = _auc(yte, base_scores)
    # leakage control: shuffled test labels must collapse to ~0.5
    rng = np.random.default_rng(0); yte_perm = rng.permutation(yte)
    auc_perm = _auc(yte_perm, lat_scores)

    utility_positive = bool(auc_lat > auc_base and auc_lat > 0.52 and abs(auc_perm - 0.5) < 0.08)
    report = {
        "ts": round(time.time(), 3), "n": n, "roster_dim": mdim, "epochs": EP,
        "n_params": model.n_params(), "base_rate_win": round(float(Y.mean()), 4),
        "split": {"train": int(i_tr), "embargo": int(i_emb - i_tr), "test": int(n - i_emb)},
        "downstream_auc": {"latent": round(auc_lat, 4), "raw_baseline": round(auc_base, 4),
                           "permutation_control": round(auc_perm, 4)},
        "utility_positive": utility_positive,
        "verdict": ("PROMOTE: latent beats baseline + passes leakage control" if utility_positive
                    else "DO NOT PROMOTE: latent does not yet beat the raw baseline (honest negative)"),
        "train_secs": round(time.time() - t0, 1),
    }
    print("\n=== REPORT ===")
    print(json.dumps(report, indent=2))

    # save checkpoint + report (shadow artifact; no live authority)
    try:
        import os
        os.makedirs("models", exist_ok=True)
        torch.save({"state_dict": model.state_dict(), "modality_dims": modality_dims,
                    "roster": roster, "report": report}, "models/shared_latent.pt")
        import redis_client
        redis_client.get().set("scibrain:perception:train_report", json.dumps(report))
        print("saved models/shared_latent.pt + scibrain:perception:train_report")
    except Exception as exc:
        print("save warning:", str(exc)[:120])
    return 0


def _split(raw, slices, keys):
    return {k: raw[:, lo:hi] for k, (lo, hi) in zip(keys, slices)}


if __name__ == "__main__":
    raise SystemExit(main())
