"""SciBrain Phase 6 — VS-V5 Universe Neural Field topology mirror (design §4.4 / §VS-V5).

Publishes a bounded, DETERMINISTIC relational-field snapshot of the live universe so the dashboard
can render all active pairs as a dynamic field — WITHOUT shipping the full S×S matrices or inventing
a force-layout. The topology comes entirely from the real `UniverseFrame` the Universe-Core modules
already scored (design mandate: "topology must come from UniverseFrame, not arbitrary force-layout"):

  • clusters  — correlation territories: connected components of the graph where |corr| ≥ an adaptive
                (percentile) threshold. Each cluster carries its size, mean intra-correlation, centroid,
                net direction, and aggregate momentum/volatility/liquidity.
  • nodes     — per-symbol points placed by CLASSICAL MDS on the correlation distance d=√(2(1−ρ)) (the
                top-2 eigenvectors of the double-centred distance matrix → real co-movement geometry),
                each tagged with cluster, direction, momentum, volatility, liquidity, and centrality.
  • edges     — the strongest DIRECTED lead-lag links L[i,j]=corr(rᵢ[t−1], rⱼ[t]): predictive-flow /
                contagion arrows (asymmetric). Capped to the top |L| so the overview never hairballs.
  • meta      — the market-state digest + the thresholds + counts (so the frontend can aggregate).

Built in the BRAIN main process right after the frame rebuilds (gated to an actual rebuild via a ts
guard, BLAS pinned to 1 thread like run_universe_modules — the box is saturated). Tier-0 observability:
it reads the frame and writes ONE Redis key; it has NO trading authority. Never raises into the gate.
Current open positions are NOT included here — that fast-moving overlay is joined by the dashboard.
"""
from __future__ import annotations

import json
import time

import numpy as np
import structlog

from . import keys as K
from .contracts import UniverseFrame

log = structlog.get_logger()

FIELD_VERSION = 1
_DEF_MAX_NODES = 160       # cap the per-pair reveal payload (overview aggregates to clusters anyway)
_DEF_MAX_EDGES = 110       # cap directed lead-lag arrows so the overview never hairballs
_LAST_FIELD_TS: float = 0.0


def _icfg(r, key, default):
    try:
        v = r.get(key)
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _norm(v: np.ndarray) -> np.ndarray:
    """Scale a vector into [-1, 1] by its max abs (0 → 0). Pure presentation, never fabricates rank."""
    m = float(np.max(np.abs(v))) if v.size else 0.0
    return (v / m) if m > 1e-12 else np.zeros_like(v)


def _mds_coords(corr: np.ndarray) -> np.ndarray:
    """Classical MDS (Torgerson) on the correlation distance d_ij = √(2(1−ρ_ij)): double-centre
    −½·D², take the top-2 eigenvectors scaled by √eigenvalue → an (S, 2) embedding whose geometry IS
    the real co-movement structure (correlated pairs sit close). Deterministic; degrades to zeros."""
    s = corr.shape[0]
    if s < 2:
        return np.zeros((s, 2), dtype=float)
    d2 = 2.0 * (1.0 - np.clip(corr, -1.0, 1.0))      # squared distance (d² = 2(1−ρ))
    np.fill_diagonal(d2, 0.0)
    j = np.eye(s) - np.full((s, s), 1.0 / s)         # centring matrix
    b = -0.5 * (j @ d2 @ j)
    b = 0.5 * (b + b.T)                              # symmetrize against fp drift
    try:
        evals, evecs = np.linalg.eigh(b)
    except np.linalg.LinAlgError:
        return np.zeros((s, 2), dtype=float)
    idx = np.argsort(evals)[::-1][:2]               # two largest
    lam = np.clip(evals[idx], 0.0, None)
    coords = evecs[:, idx] * np.sqrt(lam)
    out = np.zeros((s, 2), dtype=float)
    out[:, 0] = _norm(coords[:, 0])
    out[:, 1] = _norm(coords[:, 1]) if coords.shape[1] > 1 else 0.0
    return out


class _DSU:
    """Tiny union-find for correlation-territory connected components."""
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _cluster(corr: np.ndarray, thr: float) -> list[int]:
    """Connected components of the |corr| ≥ thr graph → a cluster id per symbol (aligned to columns)."""
    s = corr.shape[0]
    dsu = _DSU(s)
    iu = np.triu_indices(s, k=1)
    strong = np.abs(corr[iu]) >= thr
    rows, cols = iu[0][strong], iu[1][strong]
    for a, b in zip(rows.tolist(), cols.tolist()):
        dsu.union(a, b)
    roots = [dsu.find(i) for i in range(s)]
    # relabel roots to compact 0..K-1, biggest cluster first for stable colour assignment
    from collections import Counter
    order = [r for r, _ in Counter(roots).most_common()]
    remap = {r: i for i, r in enumerate(order)}
    return [remap[r] for r in roots]


def build_field_snapshot(frame: UniverseFrame, *, max_nodes: int = _DEF_MAX_NODES,
                         max_edges: int = _DEF_MAX_EDGES) -> dict:
    """Pure-compute the bounded UniverseGraphSnapshot from the frame's real matrices. Never raises."""
    snap: dict = {"available": False, "schema_version": FIELD_VERSION, "ts": round(time.time(), 3)}
    try:
        syms = list(frame.symbols)
        s = len(syms)
        if s < 2:
            snap["note"] = "frame too thin"
            return snap
        corr = np.asarray(frame.correlation, dtype=float)
        ll = np.asarray(frame.directed_lead_lag, dtype=float)
        feats = np.asarray(frame.feature_matrix, dtype=float)   # cols: last_ret, vol, momentum, log_liq
        last_ret, vol, momentum, log_liq = feats[:, 0], feats[:, 1], feats[:, 2], feats[:, 3]

        # adaptive correlation-territory threshold: the 95th percentile of off-diagonal |corr|,
        # clamped to a sane band so clusters are meaningful in a low-corr (crypto) market.
        iu = np.triu_indices(s, k=1)
        off = np.abs(corr[iu])
        thr = float(np.clip(np.quantile(off, 0.95) if off.size else 0.5, 0.30, 0.90))
        cluster_id = _cluster(corr, thr)

        coords = _mds_coords(corr)
        # centrality = absolute directed-flow throughput (out + in) — how much a symbol leads/follows
        centrality = _norm(np.abs(ll).sum(axis=1) + np.abs(ll).sum(axis=0))
        liq_n = _norm(log_liq - np.median(log_liq))      # liquidity relative to the median pair
        vol_n = _norm(vol - np.median(vol))              # volatility relative to the median pair

        # per-cluster aggregates (computed over the FULL membership, not just shown nodes)
        clusters: dict[int, dict] = {}
        for j, cid in enumerate(cluster_id):
            c = clusters.setdefault(cid, {"cluster_id": cid, "members": 0, "xs": [], "ys": [],
                                          "ret": [], "mom": [], "vol": [], "liq": [], "idx": []})
            c["members"] += 1
            c["xs"].append(float(coords[j, 0])); c["ys"].append(float(coords[j, 1]))
            c["ret"].append(float(last_ret[j])); c["mom"].append(float(momentum[j]))
            c["vol"].append(float(vol[j])); c["liq"].append(float(log_liq[j]))
            c["idx"].append(j)
        cluster_list = []
        for cid, c in clusters.items():
            idx = c["idx"]
            sub = corr[np.ix_(idx, idx)]
            mean_intra = float(np.abs(sub[np.triu_indices(len(idx), k=1)]).mean()) if len(idx) > 1 else 1.0
            cluster_list.append({
                "cluster_id": cid, "members": c["members"],
                "cx": round(float(np.mean(c["xs"])), 4), "cy": round(float(np.mean(c["ys"])), 4),
                "mean_intra_corr": round(mean_intra, 4),
                "net_direction": round(float(np.mean(np.sign(c["ret"]))), 3),   # −1 short … +1 long tilt
                "mean_momentum": round(float(np.mean(c["mom"])), 5),
                "mean_vol": round(float(np.mean(c["vol"])), 6),
                "mean_log_liquidity": round(float(np.mean(c["liq"])), 3),
            })
        cluster_list.sort(key=lambda x: x["members"], reverse=True)

        # choose the symbols to SHOW as individual nodes: most salient first (centrality + liquidity),
        # so the overview reveals the structurally important pairs; the rest live inside their cluster.
        salience = np.abs(centrality) + 0.5 * np.abs(liq_n)
        shown = list(np.argsort(salience)[::-1][:min(max_nodes, s)])
        shown_set = set(int(i) for i in shown)
        nodes = [{
            "symbol": syms[i], "x": round(float(coords[i, 0]), 4), "y": round(float(coords[i, 1]), 4),
            "cluster_id": cluster_id[i],
            "direction": int(np.sign(last_ret[i])),         # last-return sign (risk-on/off of the pair)
            "momentum": round(float(momentum[i]), 5),
            "volatility": round(float(vol[i]), 6), "vol_z": round(float(vol_n[i]), 3),
            "liquidity_z": round(float(liq_n[i]), 3),
            "centrality": round(float(centrality[i]), 3),
        } for i in shown]

        # directed lead-lag edges among shown nodes: take the strongest |L| (predictive-flow / contagion)
        edges = []
        if shown:
            sub_idx = np.array(sorted(shown_set))
            sub = ll[np.ix_(sub_idx, sub_idx)].copy()
            np.fill_diagonal(sub, 0.0)
            flat = np.abs(sub).ravel()
            if flat.size:
                k = min(max_edges, int((np.abs(sub) > 0).sum()))
                if k > 0:
                    top = np.argpartition(flat, -k)[-k:]
                    for f in top[np.argsort(flat[top])[::-1]]:
                        a, b = divmod(int(f), sub.shape[1])
                        v = float(sub[a, b])
                        if abs(v) < 1e-9:
                            continue
                        edges.append({"source": syms[int(sub_idx[a])], "target": syms[int(sub_idx[b])],
                                      "value": round(v, 4), "magnitude": round(abs(v), 4)})

        snap.update({
            "available": True,
            "primary_tf": frame.primary_tf,
            "n_symbols_total": s,
            "n_nodes_shown": len(nodes),
            "n_clusters": len(cluster_list),
            "corr_threshold": round(thr, 4),
            "digest": frame.digest,
            "clusters": cluster_list,
            "nodes": nodes,
            "edges": edges,
            "frame_ts": round(float(frame.ts), 3),
            "note": ("Positions placed by classical MDS on the correlation distance (real co-movement "
                     "geometry, not force-layout). Edges = lag-1 DIRECTED lead-lag (predictive flow / "
                     "contagion). 'volatility' is the pair's return std on the primary TF (a tail PROXY; "
                     "no fitted tail-index per pair is computed here). Tier-0 mirror — affects no picks."),
        })
    except Exception as exc:
        snap["error"] = str(exc)[:200]
        log.warning("scibrain_universe_field_build_failed", error=str(exc)[:200])
    return snap


def publish_field(r, frame: UniverseFrame | None) -> int:
    """Build + publish the field snapshot to Redis ONCE per frame rebuild (ts-guarded), BLAS pinned to
    1 thread (the saturated box). Returns the node count published (0 on skip/failure). Never raises."""
    global _LAST_FIELD_TS
    if frame is None:
        return 0
    if getattr(frame, "ts", 0.0) == _LAST_FIELD_TS:    # same cached frame → snapshot already current
        return 0
    try:
        import threadpoolctl
        _tp = threadpoolctl.threadpool_limits(1)
    except Exception:
        _tp = None
    try:
        snap = build_field_snapshot(frame)
        if snap.get("available"):
            try:
                r.setex(K.UNIVERSE_FIELD, K.HOT_TTL, json.dumps(snap, separators=(",", ":")))
                r.incr(K.UNIVERSE_FIELD_BUILDS)
            except Exception as exc:
                log.debug("scibrain_universe_field_publish_failed", error=str(exc)[:120])
        _LAST_FIELD_TS = frame.ts
        return int(snap.get("n_nodes_shown", 0) or 0)
    finally:
        if _tp is not None:
            try:
                _tp.restore()
            except Exception:
                pass
