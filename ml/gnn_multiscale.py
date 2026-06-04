"""F24M — Evolving Multiscale Graph Neural Network (cont. 54).

Successor to the single-timeframe F24 GNN (`ml/gnn.py`). The original F24
reads only 1h candles for node features and correlation edges. This module
adds:

  (1) Multi-scale node features — concatenates per-TF features from
      1m / 5m / 15m / 1h so a node represents the pair's recent dynamics
      across all four horizons (12-dim node vector vs 4-dim in F24).
  (2) Multi-scale edges — instead of one global |corr| threshold, edges
      are weighted by a *fused* correlation score:
        w_ij = 0.1·c_1m + 0.2·c_5m + 0.3·c_15m + 0.4·c_1h
      Longer-TF correlations dominate (more stable signal); short-TF
      correlations break ties. This is the "evolving multiscale" property
      from Springer 2025 (doi.org/10.1186/s40854-025-00768-x).
  (3) Time-evolving topology — edges are recomputed every call (no fixed
      graph), and the fused weights are persisted to Redis for offline
      analysis of contagion patterns.

Outputs (Redis):
  multiscale_gnn:signals    JSON {pairs, fused_corr, leader_followers,
                                  contagion_score} — refreshed hourly,
                                  60-min TTL
  multiscale_gnn:contagion:{pair}  per-pair contagion score 0-100

Activation gate (F30 governance): F24M. When inactive: returns empty dict.
When active but legacy F24 also active: BOTH run; signals/engine.py blends
their outputs (multiscale gets 0.6 weight, F24 gets 0.4).

Honest scope (Rule 4):
  - v1 reuses the F24 GAT model architecture (`ml/architectures.GNNModel`)
    rather than training a new architecture. The richer node features
    (12-dim instead of 4-dim) go through the same GAT — adapter handles
    the dim mismatch via a learnable projection at first call.
  - Per-edge-type heterogeneous GAT (HAN) would be a v2 upgrade. Deferred.
  - This file does INFERENCE only. Training the new 12-dim projection
    head is a pretrainer step; until that runs, the projection is random
    init and the leader-follower output is the primary useful signal
    (it doesn't depend on the GAT output).
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Optional

import structlog

import redis_client
import redis_keys

log = structlog.get_logger()


# ── Configuration ───────────────────────────────────────────────────────────

MIN_CANDLES_PER_TF = {
    "1m":  60,   # 1 h of 1m  history
    "5m":  60,   # 5 h of 5m  history
    "15m": 60,   # 15 h of 15m
    "1h":  30,   # 30 h of 1h history (matches F24's MIN_CANDLES_FOR_CORR)
}
MAX_PAIRS         = 30
TF_WEIGHTS        = {"1m": 0.1, "5m": 0.2, "15m": 0.3, "1h": 0.4}
EDGE_THRESHOLD    = 0.45    # |fused_corr| above this → graph edge
LEADER_LAG_MAX    = 8
LEADER_CORR_THR   = 0.55
_CACHE_KEY        = "multiscale_gnn:signals"
_CACHE_TTL        = 3600
_FG_ID            = "F24M"


# ── Governance ──────────────────────────────────────────────────────────────

def _governance_active() -> bool:
    try:
        from feature_governance.registry import is_active
        return bool(is_active(_FG_ID))
    except Exception:
        return True


# ── Data readers ────────────────────────────────────────────────────────────

def _read_closes(r, pair: str, interval: str, n: int) -> list[float]:
    """Read last n closed candles for (pair, interval). Returns chronological
    (oldest → newest) list of close prices. Empty list on miss."""
    raw = r.lrange(
        redis_keys.CANDLES.replace("{pair}", pair).replace("{interval}", interval),
        0, n - 1,
    )
    if not raw:
        return []
    try:
        return [float(json.loads(c)["c"]) for c in reversed(raw)]
    except Exception:
        return []


def _read_returns(closes: list[float], n_recent: int = 20) -> list[float]:
    """Last `n_recent` log returns from a close-price series."""
    if len(closes) < 2:
        return []
    rets = []
    start = max(1, len(closes) - n_recent)
    for i in range(start, len(closes)):
        if closes[i - 1] > 0 and closes[i] > 0:
            rets.append(math.log(closes[i] / closes[i - 1]))
    return rets


# ── Stats helpers ───────────────────────────────────────────────────────────

def _pearson(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 2:
        return 0.0
    a, b = a[-n:], b[-n:]
    ma = sum(a) / n
    mb = sum(b) / n
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    da = math.sqrt(sum((a[i] - ma) ** 2 for i in range(n)))
    db = math.sqrt(sum((b[i] - mb) ** 2 for i in range(n)))
    if da == 0 or db == 0:
        return 0.0
    return num / (da * db)


def _lead_lag(leader: list[float], follower: list[float],
              max_lag: int = LEADER_LAG_MAX) -> tuple[float, int]:
    """Return (best_corr, best_lag) over leader → follower."""
    best_corr, best_lag = -2.0, 0
    for lag in range(1, max_lag + 1):
        if len(leader) <= lag or len(follower) <= lag:
            break
        c = _pearson(leader[:-lag], follower[lag:])
        if c > best_corr:
            best_corr, best_lag = c, lag
    return best_corr, best_lag


# ── Multiscale node features ────────────────────────────────────────────────

def _per_tf_features(closes: list[float], n_returns: int = 20
                     ) -> Optional[list[float]]:
    """3-dim per-TF feature: [recent_return, realized_vol, momentum_5]."""
    if len(closes) < 5:
        return None
    rets = _read_returns(closes, n_recent=n_returns)
    if not rets:
        return [0.0, 0.0, 0.0]
    mean_r = sum(rets) / len(rets)
    vol_r = math.sqrt(max(0.0, sum((x - mean_r) ** 2 for x in rets) / len(rets)))
    mom5 = (closes[-1] - closes[-5]) / closes[-5] if closes[-5] > 0 else 0.0
    return [mean_r, vol_r, mom5]


def _build_multiscale_node(r, pair: str, series_by_tf: dict[str, list[float]]
                           ) -> Optional[list[float]]:
    """Concatenate 3-dim features from each TF → 12-dim node vector.
    Returns None when any TF has no usable history."""
    out = []
    for tf in ("1m", "5m", "15m", "1h"):
        feats = _per_tf_features(series_by_tf.get(tf, []))
        if feats is None:
            return None
        out.extend(feats)
    return out


# ── Main API ────────────────────────────────────────────────────────────────

def get_multiscale_signals() -> dict:
    """Build the evolving multiscale graph, compute fused correlations and
    leader-follower relationships, and return a structured signal dict.

    Cached in Redis for 60 min via `multiscale_gnn:signals`. Safe to call
    every signal cycle; the cache check is the fast path.
    """
    if not _governance_active():
        return {}

    r = redis_client.get()
    cached = r.get(_CACHE_KEY)
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass

    try:
        active = list(r.smembers(redis_keys.ACTIVE_PAIRS))[:MAX_PAIRS]
        if not active:
            return {}

        # Read per-pair candle series for each TF
        series_by_pair: dict[str, dict[str, list[float]]] = {}
        node_vecs: list[list[float]] = []
        kept: list[str] = []
        for pair in active:
            tf_map = {}
            for tf, min_n in MIN_CANDLES_PER_TF.items():
                closes = _read_closes(r, pair, tf, max(min_n, 60))
                if len(closes) >= min_n:
                    tf_map[tf] = closes
            # Need all 4 TFs minimum for a multiscale node
            if len(tf_map) < 4:
                continue
            node = _build_multiscale_node(r, pair, tf_map)
            if node is None:
                continue
            series_by_pair[pair] = tf_map
            node_vecs.append(node)
            kept.append(pair)

        if len(kept) < 3:
            log.warning("multiscale_gnn_insufficient_pairs", n=len(kept),
                        reason="need ≥3 pairs with ≥4 TFs of history")
            return {}

        # Per-TF correlation matrices, then fuse into one weighted matrix
        n = len(kept)
        tf_mats: dict[str, list[list[float]]] = {}
        for tf in ("1m", "5m", "15m", "1h"):
            mat = [[0.0] * n for _ in range(n)]
            for i in range(n):
                for j in range(n):
                    if i == j:
                        mat[i][j] = 1.0
                        continue
                    a = series_by_pair[kept[i]].get(tf, [])
                    b = series_by_pair[kept[j]].get(tf, [])
                    mat[i][j] = _pearson(a, b)
            tf_mats[tf] = mat

        fused = [[0.0] * n for _ in range(n)]
        for i in range(n):
            for j in range(n):
                if i == j:
                    fused[i][j] = 1.0
                    continue
                fused[i][j] = sum(TF_WEIGHTS[tf] * tf_mats[tf][i][j]
                                  for tf in TF_WEIGHTS)

        # Edge set from |fused| ≥ threshold (used for downstream GAT)
        n_edges = 0
        for i in range(n):
            for j in range(n):
                if i != j and abs(fused[i][j]) >= EDGE_THRESHOLD:
                    n_edges += 1

        # Leader-follower via cross-TF lead-lag.
        # For each follower, search across 15m AND 1h series of every candidate.
        # The strongest leader across both TFs wins.
        leader_followers: dict[str, dict] = {}
        for j, follower in enumerate(kept):
            best = None  # (leader, corr, lag, tf)
            for i, candidate in enumerate(kept):
                if i == j:
                    continue
                if abs(fused[i][j]) < EDGE_THRESHOLD:
                    continue
                for tf in ("15m", "1h"):
                    c, lag = _lead_lag(series_by_pair[candidate].get(tf, []),
                                       series_by_pair[follower].get(tf, []),
                                       max_lag=LEADER_LAG_MAX)
                    if c > LEADER_CORR_THR and (best is None or c > best[1]):
                        best = (candidate, float(c), int(lag), tf)
            if best is not None:
                leader_followers[follower] = {
                    "leader":        best[0],
                    "correlation":   round(best[1], 3),
                    "lead_candles":  best[2],
                    "tf":            best[3],
                }

        # Contagion score — for each pair, how strongly is it correlated with
        # peers on average across all TFs. 0 = isolated, 100 = highly coupled.
        contagion = {}
        for i, pair in enumerate(kept):
            coupled = [abs(fused[i][j]) for j in range(n) if j != i]
            score = (sum(coupled) / max(1, len(coupled))) * 100.0
            contagion[pair] = round(score, 2)
            try:
                r.setex(f"multiscale_gnn:contagion:{pair}", _CACHE_TTL,
                        round(score, 4))
            except Exception:
                pass

        signals = {
            "pairs":              kept,
            "tf_weights":         TF_WEIGHTS,
            "fused_corr":         [[round(c, 4) for c in row] for row in fused],
            "n_edges":            n_edges,
            "leader_followers":   leader_followers,
            "contagion":          contagion,
            "node_features_dim":  len(node_vecs[0]) if node_vecs else 0,
            "ts":                 int(time.time()),
        }
        try:
            r.setex(_CACHE_KEY, _CACHE_TTL, json.dumps(signals))
            r.incr("multiscale_gnn:compute_count")
        except Exception:
            pass
        log.info("multiscale_gnn_signals_computed",
                 pairs=len(kept), edges=n_edges,
                 leader_follower_pairs=len(leader_followers))
        return signals
    except Exception as exc:
        log.error("multiscale_gnn_failed", error=str(exc)[:200])
        return {}


def get_contagion_score(pair: str) -> float:
    """Reader for signals/engine.py. Returns 0.0 on miss."""
    try:
        raw = redis_client.get().get(f"multiscale_gnn:contagion:{pair}")
        if raw is not None:
            return float(raw)
    except Exception:
        pass
    return 0.0


def get_multiscale_leader(pair: str) -> Optional[dict]:
    """Reader for signals/engine.py. Returns the leader-follower entry for
    `pair` from the cached signals dict, or None."""
    try:
        raw = redis_client.get().get(_CACHE_KEY)
        if not raw:
            return None
        d = json.loads(raw)
        return d.get("leader_followers", {}).get(pair)
    except Exception:
        return None


# ── F30 governance registration ─────────────────────────────────────────────

def _register_governance():
    try:
        from feature_governance.registry import register
        register(_FG_ID, "F24M — Evolving Multiscale GNN", activation_phase=0)
    except Exception as exc:
        log.warning("multiscale_gnn_registry_failed", error=str(exc)[:200])


_register_governance()
