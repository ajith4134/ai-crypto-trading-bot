"""L-04 / Blueprint Feature 24 — GAT-based Inter-Asset Correlation Model.

Replaces the previous Linear-only "GNN" stub with a real Graph Attention Network
(see ml/architectures.GNNModel). Two outputs of interest:

  1. Per-pair embeddings — could feed Direction Model / MemRL later
  2. Leader-follower relationships — consumed by signals/engine.py to boost
     candidate signals when the pair's correlated leader has just moved in the
     same direction (the "BTC leads alts by ~15 min" effect from blueprint).

Inputs:
  - Per-pair feature vector (4 dims) built from Redis (LAST_PRICE,
    TICKER_VOLUME_24H, TICKER_CHANGE_24H, recent realized vol from CANDLES)
  - Correlation matrix computed from CANDLES (close-price series) — graph edges
    are pairs with |corr| > 0.5

The dynamic graph satisfies blueprint's "evolving multiscale GNN" property —
edges are recomputed every call rather than fixed at training time.

NOTE: CANDLES Redis key MUST be populated (see data/feed.py:_poll_candles). This
is enforced via the consume-side check in Rule 4 — F24's value depends on the
CANDLES feed pipeline being live.
"""
import json
import math
from pathlib import Path
import structlog
import redis_client
import redis_keys

log = structlog.get_logger()
_MODEL_PATH = Path("models/gnn.pth")
_model      = None
_last_mtime = 0.0

MIN_CANDLES_FOR_CORR = 30   # need at least 30 hourly closes for stable correlation
MAX_PAIRS            = 30   # cap graph size — O(N²) edge construction
CORR_THRESHOLD       = 0.5  # absolute correlation above this → graph edge
LEADER_LAG_HOURS     = 15   # blueprint: "BTC leads most altcoins by ~15 minutes" → on 1h candles this is ~15 hours of lookback


def _load():
    """Mtime-tracked load (matches direction_model / world_model / tft pattern)."""
    global _model, _last_mtime
    if not _MODEL_PATH.exists():
        return _model
    mtime = _MODEL_PATH.stat().st_mtime
    if _model is not None and mtime == _last_mtime:
        return _model
    try:
        import torch
        from ml.architectures import GNNModel, inject_into_main
        inject_into_main()
        checkpoint = torch.load(_MODEL_PATH, map_location="cpu", weights_only=False)
        if isinstance(checkpoint, dict):
            m = GNNModel()
            try:
                m.load_state_dict(checkpoint)
            except Exception as exc:
                log.warning("gnn_state_dict_mismatch_must_repretrain", error=str(exc)[:200])
                return _model
            _model = m
        else:
            log.warning("gnn_legacy_full_pickle_must_repretrain")
            return _model
        _model.eval()
        _last_mtime = mtime
        log.info("gnn_model_loaded", mtime=round(mtime, 2))
    except Exception as exc:
        log.warning("gnn_load_failed", error=str(exc)[:200])
    return _model


def _read_close_series(r, pair: str, n: int = 100) -> list[float]:
    """Read up to n most-recent 1h closes for pair from CANDLES list (oldest→newest order)."""
    raw = r.lrange(redis_keys.CANDLES.replace("{pair}", pair).replace("{interval}", "1h"), 0, n - 1)
    if not raw:
        return []
    try:
        # CANDLES stored newest-first; reverse to chronological
        return [float(json.loads(c)["c"]) for c in reversed(raw)]
    except Exception:
        return []


def _build_node_features(r, pair: str, closes: list[float]) -> list[float] | None:
    """4-dim feature vector for one pair. Returns None if insufficient data."""
    if len(closes) < MIN_CANDLES_FOR_CORR:
        return None
    try:
        # Price changes (recent momentum at two scales)
        change_5h = (closes[-1] - closes[-5]) / closes[-5] if len(closes) >= 5 and closes[-5] > 0 else 0.0
        change_1h = (closes[-1] - closes[-2]) / closes[-2] if closes[-2] > 0 else 0.0

        # Volume (log-scaled to handle the BTC-to-PEPE range)
        vol_raw = float(r.get(redis_keys.TICKER_VOLUME_24H.replace("{pair}", pair)) or 0)
        vol_log = math.log1p(vol_raw)

        # Realized volatility (std of last 20 hourly returns)
        returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(max(1, len(closes)-20), len(closes)) if closes[i-1] > 0]
        if returns:
            mean_r = sum(returns) / len(returns)
            var_r = sum((x - mean_r) ** 2 for x in returns) / len(returns)
            vol_r = math.sqrt(var_r)
        else:
            vol_r = 0.0
        return [change_5h, change_1h, vol_log, vol_r]
    except Exception:
        return None


def _pearson(a: list[float], b: list[float]) -> float:
    """Pearson correlation, NaN-safe."""
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


def _lead_lag(leader: list[float], follower: list[float], max_lag: int = 5) -> tuple[float, int]:
    """Find the lag (in candles) where corr(leader[t-lag], follower[t]) is maximum.
    Returns (best_corr, best_lag). Positive lag = leader leads follower.
    Negative result is symmetric — we only call this where best is positive.
    """
    best_corr = -2.0
    best_lag  = 0
    for lag in range(1, max_lag + 1):
        if len(leader) <= lag or len(follower) <= lag:
            break
        a = leader[:-lag]
        b = follower[lag:]
        c = _pearson(a, b)
        if c > best_corr:
            best_corr = c
            best_lag = lag
    return (best_corr, best_lag)


def get_interasset_signals() -> dict:
    """Build dynamic correlation graph, run GAT, identify leader-follower pairs."""
    r = redis_client.get()
    cached = r.get(redis_keys.INTERASSET_SIGNALS)
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass

    if not _MODEL_PATH.exists():
        return {}

    try:
        active = list(r.smembers(redis_keys.ACTIVE_PAIRS))[:MAX_PAIRS]
        if not active:
            return {}

        # 1) Read close-price series + build node features per pair
        series: dict[str, list[float]] = {}
        node_feats: list[list[float]] = []
        kept_pairs: list[str] = []
        for pair in active:
            closes = _read_close_series(r, pair, n=100)
            feats = _build_node_features(r, pair, closes)
            if feats is None:
                continue
            series[pair] = closes
            node_feats.append(feats)
            kept_pairs.append(pair)

        if len(kept_pairs) < 3:
            log.warning("gnn_insufficient_pairs", n=len(kept_pairs),
                        reason="need ≥3 pairs with ≥30 1h candles each — check data/feed.py:_poll_candles")
            return {}

        import torch, numpy as np
        x = torch.tensor(node_feats, dtype=torch.float32)        # [N, 4]

        # 2) Build correlation matrix and edge_index from |corr| > threshold
        n = len(kept_pairs)
        corr_mat = np.zeros((n, n), dtype=np.float32)
        edges_src: list[int] = []
        edges_dst: list[int] = []
        edges_w  : list[float] = []
        for i in range(n):
            for j in range(n):
                if i == j:
                    corr_mat[i, j] = 1.0
                    continue
                c = _pearson(series[kept_pairs[i]], series[kept_pairs[j]])
                corr_mat[i, j] = c
                if abs(c) >= CORR_THRESHOLD:
                    edges_src.append(i); edges_dst.append(j)
                    edges_w.append(abs(c))

        # Self-loops — GATConv handles these via add_self_loops=True by default but
        # being explicit makes graph structure visible.
        for i in range(n):
            edges_src.append(i); edges_dst.append(i); edges_w.append(1.0)

        if not edges_src:
            log.warning("gnn_no_edges_above_threshold", n_pairs=n, threshold=CORR_THRESHOLD)
            edge_index = torch.zeros((2, 0), dtype=torch.long)
        else:
            edge_index = torch.tensor([edges_src, edges_dst], dtype=torch.long)

        # 3) Run GAT forward pass to get per-pair embeddings
        model = _load()
        if model is None:
            return {}
        with torch.no_grad():
            embeddings = model(x, edge_index)            # [N, OUT_DIM]
        emb_list = embeddings.tolist()

        # 4) Identify leader-follower relationships via cross-correlation lead-lag.
        # For each pair, find the OTHER pair whose past closes best predict this
        # pair's recent move. That predicting pair is the "leader".
        leader_followers: dict[str, dict] = {}
        for j, follower in enumerate(kept_pairs):
            best = None  # (leader_pair, best_corr, best_lag)
            for i, candidate in enumerate(kept_pairs):
                if i == j:
                    continue
                # Skip if instantaneous correlation is weak — saves work
                if abs(corr_mat[i, j]) < CORR_THRESHOLD:
                    continue
                c, lag = _lead_lag(series[candidate], series[follower], max_lag=5)
                if c > 0.6 and (best is None or c > best[1]):
                    best = (candidate, float(c), int(lag))
            if best is not None:
                leader_followers[follower] = {
                    "leader": best[0],
                    "correlation": round(best[1], 3),
                    "lead_candles": best[2],
                }

        signals = {
            "pairs": kept_pairs,
            "correlation_matrix": corr_mat.tolist(),
            "leader_followers": leader_followers,
            "embeddings": emb_list,    # [N, OUT_DIM] — for future direction-model / MemRL use
            "n_edges": len(edges_src),
        }
        r.set(redis_keys.INTERASSET_SIGNALS, json.dumps(signals), ex=3600)
        log.info("gnn_signals_computed",
                 pairs=len(kept_pairs), edges=len(edges_src),
                 leader_follower_pairs=len(leader_followers))
        return signals
    except Exception as exc:
        log.error("gnn_signals_failed", error=str(exc)[:200])
        return {}
