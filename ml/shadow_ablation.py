"""cont. 70e (2026-06-03) — Track 2 shadow-ablation harness.

PURPOSE: measure the *directional-AUC lift* contributed by the 3 microstructure
features added in cont. 70c (cvd_z, ofi_l1, ofi_accel) to the online direction
model. The production online_predictor is plain SGD that tracks only n_updates —
it has NO AUC metric — and the 3 features have NO historical reconstruction, so a
retrospective ablation on past data is impossible. The only honest measurement is
a FORWARD shadow ablation: two prequential SGD direction classifiers fed the
exact same live candle samples, one on all 35 cols ("full") and one on the 32
baseline cols ("ablated", the 3 dropped). The lift = AUC_full - AUC_ablated.

This is a PURE SHADOW: it never touches the production online_predictor, never
trades, never gates. Zero trading impact. Self-contained beat task on the
`microstructure` queue (celery_worker_candlenet, ml/ bind-mounted -> no rebuild).

It mirrors f50e's enqueue/drain (ml/candle_online_trainer.py) on its OWN
watermark/pending keys, reusing f50e's candle reader + label definition so the
samples are statistically identical to what the live model learns. Both shadow
models see identical X each step (only the column set differs), so the AUC
comparison is internally fair regardless of any drift vs the production stream.

Prequential (interleaved test-then-train): for each realized sample, predict
BEFORE fitting, append (p_full, p_ablated, y) to a rolling window, then fit both.
AUC is roc_auc_score over the most-recent window each tick.

Kill switch: shadow_abl:disabled = "1". Never raises (caught at tick boundary).
"""
from __future__ import annotations

import json
import pickle
import time
from typing import Optional

import structlog

import redis_client

log = structlog.get_logger(__name__)

# Features dropped to form the "ablated" baseline — the cont.70c additions.
_ABLATED_COLS = ("cvd_z", "ofi_l1", "ofi_accel")

_MODEL_PATH = "/app/models/shadow_ablation.pkl"
_FALLBACK_PATH = "/opt/trading-bot/models/shadow_ablation.pkl"

_WM_KEY = "shadow_abl:wm:{pair}:{tf}"
_PENDING_KEY = "shadow_abl:pending:{pair}:{tf}"
_WINDOW_KEY = "shadow_abl:window"          # rolling list of "p_full,p_ab,y"
_WINDOW_MAX = 3000                          # prequential eval window
_MIN_EVAL = 500                             # need this many before reporting AUC
_WARM_BEFORE_EVAL = 1000                    # only record predictions from a converged
                                            # model — early cold-start predictions
                                            # (esp. the wider 35-col head, which warms
                                            # slower) would bias the lift. SGD logistic
                                            # converges well before 1000 samples.
_PERSIST_EVERY = 100
_PER_TICK_DRAIN = 400                       # cap labeled samples processed / tick

_state: Optional[dict] = None


def _is_disabled() -> bool:
    try:
        return redis_client.get().get("shadow_abl:disabled") in (b"1", "1")
    except Exception:
        return False


def _keep_indices() -> tuple[list[int], int]:
    """Return (indices_to_keep_for_ablated, n_full) from FEATURE_COLUMNS."""
    from prediction.features import FEATURE_COLUMNS
    keep = [i for i, c in enumerate(FEATURE_COLUMNS) if c not in _ABLATED_COLS]
    return keep, len(FEATURE_COLUMNS)


def _new_state() -> dict:
    """Two SGD logistic heads + scalers. Hyperparams MATCH
    online_predictor.direction so this is a faithful proxy."""
    from sklearn.linear_model import SGDClassifier
    from sklearn.preprocessing import StandardScaler
    keep, n_full = _keep_indices()
    return {
        "n_full": n_full,
        "keep": keep,                       # ablated column indices
        "scaler_full": StandardScaler(),
        "scaler_ab": StandardScaler(),
        "clf_full": SGDClassifier(loss="log_loss", alpha=1e-4, random_state=42),
        "clf_ab": SGDClassifier(loss="log_loss", alpha=1e-4, random_state=42),
        "fitted": False,                    # True once both clfs have seen 0 and 1
        "seen_labels": set(),
        "n_updates": 0,
    }


def _load_state(force_reload: bool = False) -> dict:
    # The candlenet worker runs concurrency=2; caching the model in a module
    # global would let the two forks diverge from a stale disk copy (n_updates
    # frozen). The task fires once / 60s so there is never a concurrent writer
    # -> reload-from-disk each tick + persist each tick keeps both forks synced
    # through the pickle and lets n_updates accumulate monotonically.
    global _state
    if force_reload:
        _state = None
    if _state is not None:
        return _state
    for path in (_MODEL_PATH, _FALLBACK_PATH):
        try:
            with open(path, "rb") as fh:
                st = pickle.load(fh)
            # Discard a stale-width persisted state if FEATURE_COLUMNS changed.
            _, n_full = _keep_indices()
            if st.get("n_full") == n_full:
                _state = st
                log.info("shadow_abl_loaded", n_updates=st.get("n_updates"))
                return _state
            log.warning("shadow_abl_reset_on_feature_change",
                        old_n=st.get("n_full"), new_n=n_full)
        except FileNotFoundError:
            continue
        except Exception as exc:
            log.warning("shadow_abl_load_failed", path=path, error=str(exc)[:200])
    _state = _new_state()
    return _state


def _persist(st: dict) -> None:
    import os
    path = _MODEL_PATH if os.access("/app/models", os.W_OK) else _FALLBACK_PATH
    try:
        with open(path, "wb") as fh:
            pickle.dump(st, fh)
    except Exception as exc:
        log.warning("shadow_abl_persist_failed", error=str(exc)[:200])


# --------------------------------------------------------------------------- #
# Enqueue / drain — mirrors f50e but feeds the two shadow models.
# --------------------------------------------------------------------------- #
def _watermark(r, pair: str, tf: str) -> int:
    try:
        return int(r.get(_WM_KEY.format(pair=pair, tf=tf)) or 0)
    except (TypeError, ValueError):
        return 0


def _enqueue_new(r, pair: str, tf: str, candles_newest_first: list[dict],
                 tf_s: int) -> int:
    from prediction.features import live_features
    wm = _watermark(r, pair, tf)
    new_candles = sorted((c for c in candles_newest_first if c["_t_s"] > wm),
                         key=lambda c: c["_t_s"])
    if not new_candles:
        return 0
    try:
        feats = live_features(pair)
    except Exception:
        return 0
    try:
        atr = float(r.get(f"{pair}:atr") or 0.0)
    except (TypeError, ValueError):
        atr = 0.0
    pkey = _PENDING_KEY.format(pair=pair, tf=tf)
    n = 0
    max_ts = wm
    try:
        pipe = r.pipeline()
        for c in new_candles:
            payload = json.dumps({"snap_ts": c["_t_s"], "ref_close": c["_c"],
                                  "ref_atr": atr, "features": feats})
            pipe.zadd(pkey, {payload: c["_t_s"] + tf_s})
            max_ts = max(max_ts, c["_t_s"])
            n += 1
        pipe.zremrangebyrank(pkey, 0, -501)
        pipe.set(_WM_KEY.format(pair=pair, tf=tf), int(max_ts))
        pipe.execute()
    except Exception as exc:
        log.debug("shadow_abl_enqueue_failed", pair=pair, error=str(exc)[:160])
        return 0
    return n


def _observe(st: dict, feats: dict, y: int, window_buf: list[str]) -> None:
    """One prequential step: predict-then-fit on both heads."""
    import numpy as np
    from prediction.features import to_vector
    x_full = np.asarray([to_vector(feats)], dtype=np.float32)
    x_ab = x_full[:, st["keep"]]

    # TEST first — only once BOTH heads have converged (warm-gate), else cold
    # predictions (the wider head warms slower) pollute the lift estimate.
    if st["fitted"] and st["n_updates"] >= _WARM_BEFORE_EVAL:
        try:
            sf = st["scaler_full"].transform(x_full)
            sa = st["scaler_ab"].transform(x_ab)
            p_full = float(st["clf_full"].predict_proba(sf)[0][1])
            p_ab = float(st["clf_ab"].predict_proba(sa)[0][1])
            window_buf.append(f"{p_full:.5f},{p_ab:.5f},{int(y)}")
        except Exception:
            pass

    # TRAIN.
    st["scaler_full"].partial_fit(x_full)
    st["scaler_ab"].partial_fit(x_ab)
    sf = st["scaler_full"].transform(x_full)
    sa = st["scaler_ab"].transform(x_ab)
    yv = np.asarray([int(y)])
    first = not st["fitted"]
    classes = np.asarray([0, 1])
    try:
        if first:
            st["clf_full"].partial_fit(sf, yv, classes=classes)
            st["clf_ab"].partial_fit(sa, yv, classes=classes)
        else:
            st["clf_full"].partial_fit(sf, yv)
            st["clf_ab"].partial_fit(sa, yv)
        st["seen_labels"].add(int(y))
        if len(st["seen_labels"]) >= 2:
            st["fitted"] = True
        st["n_updates"] += 1
    except Exception as exc:
        log.debug("shadow_abl_fit_failed", error=str(exc)[:160])


def _drain(r, st: dict, pair: str, tf: str, candles: list[dict],
           budget: int, window_buf: list[str]) -> int:
    """Drain realized pending entries; prequentially observe each. Returns count."""
    from ml.candle_online_trainer import _resolve_label
    pkey = _PENDING_KEY.format(pair=pair, tf=tf)
    now = int(time.time())
    try:
        members = r.zrangebyscore(pkey, 0, now, start=0, num=budget) or []
    except Exception:
        return 0
    if not members:
        return 0
    to_remove = []
    trained = 0
    for raw in members:
        try:
            entry = json.loads(raw if isinstance(raw, str) else raw.decode())
            ref_ts = int(entry.get("snap_ts") or 0)
            ref_close = float(entry.get("ref_close") or 0)
            ref_atr = float(entry.get("ref_atr") or 0)
            feats = entry.get("features") or {}
            if ref_ts <= 0 or ref_close <= 0 or not feats:
                to_remove.append(raw)
                continue
            label = _resolve_label(candles, ref_ts, ref_close, ref_atr)
            if label is None:
                continue                    # not realized yet — leave queued
            _observe(st, feats, label[0], window_buf)
            to_remove.append(raw)
            trained += 1
        except Exception:
            to_remove.append(raw)
    if to_remove:
        try:
            r.zrem(pkey, *to_remove)
        except Exception:
            pass
    return trained


def _compute_auc(r) -> None:
    """roc_auc over the rolling window for both heads; publish lift to Redis."""
    try:
        rows = r.lrange(_WINDOW_KEY, 0, _WINDOW_MAX - 1) or []
    except Exception:
        return
    if len(rows) < _MIN_EVAL:
        try:
            r.set("shadow_abl:n", len(rows))
            r.set("shadow_abl:status", "warming")
        except Exception:
            pass
        return
    p_full, p_ab, ys = [], [], []
    for row in rows:
        try:
            a, b, c = (row if isinstance(row, str) else row.decode()).split(",")
            p_full.append(float(a)); p_ab.append(float(b)); ys.append(int(c))
        except Exception:
            continue
    if len(set(ys)) < 2:
        return                               # AUC undefined with one class
    try:
        from sklearn.metrics import roc_auc_score
        auc_full = float(roc_auc_score(ys, p_full))
        auc_ab = float(roc_auc_score(ys, p_ab))
    except Exception as exc:
        log.debug("shadow_abl_auc_failed", error=str(exc)[:160])
        return
    lift = auc_full - auc_ab
    base = sum(ys) / len(ys)
    try:
        pipe = r.pipeline()
        pipe.set("shadow_abl:auc_full", f"{auc_full:.4f}")
        pipe.set("shadow_abl:auc_ablated", f"{auc_ab:.4f}")
        pipe.set("shadow_abl:lift", f"{lift:.4f}")
        pipe.set("shadow_abl:n", len(ys))
        pipe.set("shadow_abl:positive_rate", f"{base:.4f}")
        pipe.set("shadow_abl:status", "ready")
        pipe.set("shadow_abl:last_auc_ts", int(time.time()))
        pipe.execute()
    except Exception:
        pass
    log.info("shadow_abl_auc", n=len(ys), auc_full=round(auc_full, 4),
             auc_ablated=round(auc_ab, 4), lift=round(lift, 4),
             positive_rate=round(base, 4))


def tick() -> dict:
    """One beat invocation. Mirror-enqueue + drain + recompute AUC.
    Never raises (caught here)."""
    if _is_disabled():
        try:
            redis_client.get().incr("shadow_abl:disabled_skip_count")
        except Exception:
            pass
        return {"status": "disabled"}
    try:
        from ml.candle_online_trainer import _active_pairs, _read_candles
        r = redis_client.get()
        st = _load_state(force_reload=True)   # fresh disk read each tick
        pairs = _active_pairs()
        if not pairs:
            return {"status": "no_pairs"}
        window_buf: list[str] = []
        budget = _PER_TICK_DRAIN
        enq = trained = 0
        for pair in pairs:
            for tf in ("1m", "5m", "15m"):
                if budget <= 0:
                    break
                candles = _read_candles(pair, tf, n=50)
                if not candles:
                    continue
                enq += _enqueue_new(r, pair, tf, candles, _TF_SECONDS(tf))
                t = _drain(r, st, pair, tf, candles, min(50, budget), window_buf)
                trained += t
                budget -= t
            if budget <= 0:
                break
        # Flush the prequential window (newest first) + cap it.
        if window_buf:
            try:
                pipe = r.pipeline()
                for row in window_buf:
                    pipe.lpush(_WINDOW_KEY, row)
                pipe.ltrim(_WINDOW_KEY, 0, _WINDOW_MAX - 1)
                pipe.execute()
            except Exception:
                pass
        if trained:
            _persist(st)                      # write back each tick (forks share via disk)
        _compute_auc(r)
        try:
            r.incrby("shadow_abl:enqueued_total", enq)
            r.incrby("shadow_abl:trained_total", trained)
            r.set("shadow_abl:last_tick_ts", int(time.time()))
        except Exception:
            pass
        log.info("shadow_abl_tick", pairs=len(pairs), enqueued=enq,
                 trained=trained, n_updates=st["n_updates"])
        return {"status": "ok", "enqueued": enq, "trained": trained,
                "n_updates": st["n_updates"]}
    except Exception as exc:
        log.warning("shadow_abl_tick_failed", error=str(exc)[:200])
        return {"status": "error", "error": str(exc)[:200]}


def _TF_SECONDS(tf: str) -> int:
    return {"1m": 60, "5m": 300, "15m": 900}.get(tf, 60)
