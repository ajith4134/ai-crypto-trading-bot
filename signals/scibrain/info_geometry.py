"""InformationGeometryHealth — a bank-level RECALIBRATION/health monitor over the geometry of the
MODULE-OUTPUT distributions (design §6e Tier-B, role=recalibration/context).

UNIQUE EVIDENCE (§6g): every other component scores the MARKET; this one scores the BANK ITSELF. It
answers three questions no alpha module can (design §6d gaps + line 285):
  1. MODEL-MANIFOLD DRIFT — has a module's output distribution moved on the statistical manifold since
     its IC was estimated? Fisher-Rao geodesic distance on the univariate-Gaussian manifold (μ,σ)
     between the module's CURRENT output law and a slowly-updated baseline. High drift ⇒ the module is
     behaving differently than when we measured its skill.
  2. IC-TRANSFERABILITY — a rolling IC is only trustworthy if the output law that produced it still
     holds. transferability = exp(−drift): when a module has drifted, its historical IC should be
     trusted LESS (the future router penalty / recalibration consumes this).
  3. NONLINEAR REDUNDANCY — normalized HSIC (≈CKA, RBF kernels, median-heuristic bandwidth) between
     every pair of modules' ALIGNED output vectors. Unlike a Pearson correlation it catches NONLINEAR
     dependence: two modules that look uncorrelated but carry the same information score high → they
     must not count as independent confirmations (feeds the §6g.330 evidence-family penalty, the NEXT
     checklist item).

SUBSTRATE: the aligned per-module direction vectors the IC tracker already stashes in
`scibrain:ic:pending` (one deduped vector per symbol/bucket over the IC horizon) + the rolling IC in
`scibrain:ic:map`. No new per-symbol plumbing — this is a MAIN-PROCESS periodic task (runs next to
ic_tracker.settle in gate.py, NOT in the per-symbol fork workers), Tier-0: it has NO trading authority,
it only publishes a health report (`scibrain:infogeo:health`) consumed by the dashboard /scibrain and
(later) the router. Deterministic given the sampled vectors; bounded (caps the read + HSIC sample).
"""
from __future__ import annotations

import json
import math
import time

import numpy as np
import structlog

from . import keys as K

log = structlog.get_logger()

_CAP = 1200          # max ic:pending members read per assessment
_MIN_SAMPLES = 40    # a module needs ≥ this many finite outputs to assess its drift
_MIN_OVERLAP = 30    # a module pair needs ≥ this many co-voted samples for HSIC
_EWMA_ALPHA = 0.10   # baseline (μ,σ) update rate toward the current law
_HSIC_MAX_M = 150    # subsample rows for the HSIC Gram matrices (CPU budget §6g.5)
_REDUNDANT_THRESH = 0.55   # normalized-HSIC above this ⇒ a redundant pair
_DRIFT_REF = 1.0     # Fisher-Rao scale for transferability = exp(-drift/_DRIFT_REF)


def _fisher_rao_gauss(m1: float, v1: float, m2: float, v2: float) -> float:
    """Fisher-Rao geodesic distance between two univariate Gaussians on the statistical manifold
    (metric ds²=(dμ²+2dσ²)/σ²). Maps to the hyperbolic half-plane → closed form:
        d = √2 · arccosh(1 + ((μ1−μ2)²/2 + (σ1−σ2)²)/(2 σ1 σ2)).
    Real information geometry (not a KL surrogate); symmetric, 0 iff identical."""
    s1 = math.sqrt(max(v1, 1e-9))
    s2 = math.sqrt(max(v2, 1e-9))
    delta = ((m1 - m2) ** 2 / 2.0 + (s1 - s2) ** 2) / (2.0 * s1 * s2)
    return math.sqrt(2.0) * math.acosh(max(1.0, 1.0 + delta))


def _rbf_gram(x: np.ndarray) -> np.ndarray:
    """RBF Gram matrix with the median-heuristic bandwidth (deterministic given x)."""
    d2 = (x[:, None] - x[None, :]) ** 2
    med = np.median(d2[d2 > 0]) if np.any(d2 > 0) else 1.0
    gamma = 1.0 / (med + 1e-12)
    return np.exp(-gamma * d2)


def _normalized_hsic(x: np.ndarray, y: np.ndarray) -> float:
    """Normalized HSIC (≈CKA) ∈ [0,1] — nonlinear dependence between aligned samples x,y.
    0 = independent, 1 = deterministically dependent (linear OR nonlinear)."""
    m = x.shape[0]
    if m < _MIN_OVERLAP:
        return float("nan")
    if m > _HSIC_MAX_M:                      # deterministic stride subsample for the Gram budget
        idx = np.linspace(0, m - 1, _HSIC_MAX_M).astype(int)
        x, y = x[idx], y[idx]
        m = _HSIC_MAX_M
    kx, ky = _rbf_gram(x), _rbf_gram(y)
    h = np.eye(m) - np.ones((m, m)) / m      # centering
    kxc, kyc = h @ kx @ h, h @ ky @ h
    hxy = float(np.sum(kxc * kyc))
    hxx = float(np.sum(kxc * kxc))
    hyy = float(np.sum(kyc * kyc))
    if hxx <= 1e-12 or hyy <= 1e-12:
        return 0.0
    return float(np.clip(hxy / math.sqrt(hxx * hyy), 0.0, 1.0))


def _read_output_matrix(r):
    """Read the aligned per-module direction vectors from ic:pending. Returns (modules, M) where
    M is (n_samples × n_modules) with NaN where a module did not vote on that sample."""
    try:
        members = r.zrange(K.IC_PENDING, 0, _CAP - 1)
    except Exception:
        return [], np.empty((0, 0))
    rows: list[dict] = []
    seen_mods: dict[str, int] = {}
    for mem in members:
        try:
            v = json.loads(mem).get("v") or {}
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not v:
            continue
        rows.append(v)
        for mod in v:
            seen_mods.setdefault(mod, 0)
            seen_mods[mod] += 1
    modules = sorted(m for m, n in seen_mods.items() if n >= _MIN_SAMPLES)
    if not rows or not modules:
        return [], np.empty((0, 0))
    midx = {m: j for j, m in enumerate(modules)}
    M = np.full((len(rows), len(modules)), np.nan)
    for i, v in enumerate(rows):
        for mod, val in v.items():
            j = midx.get(mod)
            if j is not None:
                try:
                    M[i, j] = float(val)
                except (TypeError, ValueError):
                    pass
    return modules, M


def assess(r, now: float | None = None) -> dict | None:
    """Compute + publish the bank's information-geometry health report. Main-process, once per cycle.
    Never raises into the caller."""
    now = now or time.time()
    try:
        modules, M = _read_output_matrix(r)
        if not modules:
            return None

        # ── 1) per-module drift on the Gaussian manifold vs an EWMA baseline ──
        try:
            base = json.loads(r.get(K.INFOGEO_BASELINE) or "{}")
        except Exception:
            base = {}
        new_base: dict[str, list] = {}
        cur_stats: dict[str, tuple] = {}
        for j, mod in enumerate(modules):
            col = M[:, j]
            col = col[np.isfinite(col)]
            if col.size < _MIN_SAMPLES:
                continue
            mu, var = float(col.mean()), float(col.var())
            cur_stats[mod] = (mu, var, col.size)
            if mod in base and isinstance(base[mod], list) and len(base[mod]) == 2:
                bmu, bvar = float(base[mod][0]), float(base[mod][1])
            else:
                bmu, bvar = mu, var                # cold start: baseline = current (drift 0)
            # EWMA the baseline toward the current law (slow manifold tracking)
            nmu = (1 - _EWMA_ALPHA) * bmu + _EWMA_ALPHA * mu
            nvar = (1 - _EWMA_ALPHA) * bvar + _EWMA_ALPHA * var
            new_base[mod] = [round(nmu, 6), round(nvar, 8)]
            cur_stats[mod] = (mu, var, col.size, bmu, bvar)

        try:
            ic_map = {k: float(v) for k, v in (r.hgetall(K.IC_MAP) or {}).items()}
        except Exception:
            ic_map = {}

        module_health: dict[str, dict] = {}
        drifts = []
        for mod, st in cur_stats.items():
            mu, var, n, bmu, bvar = st
            drift = _fisher_rao_gauss(mu, var, bmu, bvar)
            drifts.append(drift)
            transfer = math.exp(-drift / _DRIFT_REF)
            module_health[mod] = {
                "drift": round(drift, 4),
                "ic_transferability": round(transfer, 4),
                "ic": round(ic_map.get(mod, 0.0), 4),
                "out_mean": round(mu, 4),
                "out_std": round(math.sqrt(max(var, 0.0)), 4),
                "n": int(n),
            }

        # ── 2) nonlinear redundancy: normalized-HSIC over co-voted samples ──
        redundant_pairs = []
        max_red = {m: 0.0 for m in modules}
        red_partner = {m: None for m in modules}
        for a in range(len(modules)):
            ca = M[:, a]
            for b in range(a + 1, len(modules)):
                cb = M[:, b]
                ok = np.isfinite(ca) & np.isfinite(cb)
                if int(ok.sum()) < _MIN_OVERLAP:
                    continue
                h = _normalized_hsic(ca[ok], cb[ok])
                if not np.isfinite(h):
                    continue
                ma, mb = modules[a], modules[b]
                if h > max_red[ma]:
                    max_red[ma], red_partner[ma] = h, mb
                if h > max_red[mb]:
                    max_red[mb], red_partner[mb] = h, ma
                if h >= _REDUNDANT_THRESH:
                    redundant_pairs.append([ma, mb, round(h, 4)])
        for mod in module_health:
            module_health[mod]["max_redundancy"] = round(max_red.get(mod, 0.0), 4)
            module_health[mod]["redundant_with"] = red_partner.get(mod)

        redundant_pairs.sort(key=lambda p: -p[2])
        report = {
            "ts": round(now, 2),
            "n_samples": int(M.shape[0]),
            "n_modules": len(modules),
            "mean_drift": round(float(np.mean(drifts)), 4) if drifts else 0.0,
            "max_drift": round(float(np.max(drifts)), 4) if drifts else 0.0,
            "max_drift_module": (max(module_health, key=lambda m: module_health[m]["drift"])
                                 if module_health else None),
            "n_redundant_pairs": len(redundant_pairs),
            "redundant_pairs": redundant_pairs[:20],
            "modules": module_health,
        }
        try:
            pipe = r.pipeline(transaction=False)
            pipe.setex(K.INFOGEO_HEALTH, K.HOT_TTL, json.dumps(report, separators=(",", ":")))
            pipe.setex(K.INFOGEO_BASELINE, K.HOT_TTL * 24, json.dumps(new_base, separators=(",", ":")))
            pipe.incr(K.INFOGEO_RUNS)
            pipe.execute()
        except Exception as exc:
            log.debug("infogeo_publish_failed", error=str(exc)[:120])
        log.info("infogeo_health", n_modules=len(modules), n_samples=int(M.shape[0]),
                 mean_drift=report["mean_drift"], redundant_pairs=len(redundant_pairs))
        return report
    except Exception as exc:
        log.warning("infogeo_assess_failed", error=str(exc)[:160])
        return None
