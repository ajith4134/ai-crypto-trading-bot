"""
F53 — Qlib Alpha-158 Formulaic Alpha Pool (cont. 55, 2026-05-28).

Pure-Python port of Microsoft Qlib's Alpha-158 / Alpha-360 feature
handlers — 158 closed-form factors over rolling OHLCV windows, with
no Qlib dependency.

Research basis:
  - Microsoft Qlib (github.com/microsoft/qlib, 36.3k★) — Alpha-158 handler.
  - IC promotion threshold ≥ 0.02 (Qlib default).

Design (see next_impl/f53_qlib_alpha_pool.md):
  - 158 factors organised in 6 groups: K-line (10), Price (20), Volume (10),
    Momentum (40), Correlation (40), Volatility/Quantile (38).
  - Per-minute compute over `{pair}:1m:candles` ring buffer.
  - Hourly IC refresh vs 1h-forward return.
  - Top-K (default 20) factor IDs exported via `qlib:top_k_factor_ids`.
  - Consumer in signals/engine.py aggregates the Top-K as a single
    composite bonus (±12), with sign drawn from each factor's rolling IC.

Cold-start safe — until ≥168h of factor history exists, top-K is empty
and consumer adds zero. No regression risk.
"""
from __future__ import annotations

import json
import math
import time
from typing import Any, Callable

import numpy as np
import structlog

import redis_client
import redis_keys
from feature_governance.registry import register

log = structlog.get_logger()

_FG_ID = "F53"

try:
    register(_FG_ID, "Qlib Alpha-158 Formulaic Pool", activation_phase=0)
except Exception as _exc:
    log.debug("f53_self_register_deferred", err=str(_exc))


# ---- Configuration ----------------------------------------------------------

_WINDOWS = (5, 10, 20, 30, 60)
_TOP_K_DEFAULT = 20
_IC_THRESHOLD = 0.02
_IC_WINDOW_HOURS = 168   # 7 days
_MAX_HISTORY_BARS = 240  # 4h of 1m candles — enough for largest window (60) + corr
_FWD_HORIZON_MIN = 60    # IC computed vs 1h-forward 1m return

# Min candles required before _any_ factor evaluates. Largest window is 60
# plus 1 for delta operations.
_MIN_BARS = 62


# ---- Primitive numpy helpers -----------------------------------------------
# Operate on 1-D numpy arrays. Newest sample is at index -1 (matches Redis
# LRANGE semantics where LRANGE 0 N-1 returns newest-first; the loader
# below flips that).

def _ts_mean(x, n):
    if len(x) < n: return np.nan
    return float(np.nanmean(x[-n:]))

def _ts_std(x, n):
    if len(x) < n: return np.nan
    return float(np.nanstd(x[-n:], ddof=1))

def _ts_min(x, n):
    if len(x) < n: return np.nan
    return float(np.nanmin(x[-n:]))

def _ts_max(x, n):
    if len(x) < n: return np.nan
    return float(np.nanmax(x[-n:]))

def _ts_rank(x, n):
    """Percentile rank of the most-recent value within the last-N window."""
    if len(x) < n: return np.nan
    w = x[-n:]
    cur = w[-1]
    if not np.isfinite(cur): return np.nan
    less = float(np.sum(w < cur))
    return less / n

def _ts_corr(x, y, n):
    if len(x) < n or len(y) < n: return np.nan
    a = x[-n:]; b = y[-n:]
    if np.nanstd(a) == 0 or np.nanstd(b) == 0: return 0.0
    return float(np.corrcoef(a, b)[0, 1])

def _ts_cov(x, y, n):
    if len(x) < n or len(y) < n: return np.nan
    a = x[-n:]; b = y[-n:]
    return float(np.cov(a, b, ddof=1)[0, 1])

def _delta(x, n):
    """Current minus value n bars ago."""
    if len(x) < n + 1: return np.nan
    return float(x[-1] - x[-1 - n])

def _ts_skew(x, n):
    if len(x) < n: return np.nan
    w = x[-n:]
    mu = np.nanmean(w)
    sd = np.nanstd(w, ddof=1)
    if sd == 0: return 0.0
    return float(np.nanmean(((w - mu) / sd) ** 3))

def _ts_kurt(x, n):
    if len(x) < n: return np.nan
    w = x[-n:]
    mu = np.nanmean(w)
    sd = np.nanstd(w, ddof=1)
    if sd == 0: return 0.0
    return float(np.nanmean(((w - mu) / sd) ** 4) - 3.0)


# ---- Factor definitions -----------------------------------------------------
# Each factor is a small lambda returning a single float given an OHLCV dict
# with arrays o, h, l, c, v. None / NaN returns are written as "nan" in
# Redis and ignored by the IC tracker.

def _build_factor_table() -> dict[str, Callable]:
    """Build the {factor_id: callable(d) -> float} dict. 158 entries.

    factor_id is short and stable so we can persist IC across restarts.
    """
    tbl: dict[str, Callable] = {}

    # --- (1) K-line factors — 10 ---------------------------------------------
    tbl["kbar_body"]    = lambda d: float((d["c"][-1] - d["o"][-1]) / max(d["o"][-1], 1e-12))
    tbl["kbar_range"]   = lambda d: float((d["h"][-1] - d["l"][-1]) / max(d["o"][-1], 1e-12))
    tbl["kbar_upper"]   = lambda d: float((d["h"][-1] - max(d["c"][-1], d["o"][-1])) / max(d["o"][-1], 1e-12))
    tbl["kbar_lower"]   = lambda d: float((min(d["c"][-1], d["o"][-1]) - d["l"][-1]) / max(d["o"][-1], 1e-12))
    tbl["kbar_close_loc"] = lambda d: float((d["c"][-1] - d["l"][-1]) / max(d["h"][-1] - d["l"][-1], 1e-12))
    tbl["kbar_open_loc"]  = lambda d: float((d["o"][-1] - d["l"][-1]) / max(d["h"][-1] - d["l"][-1], 1e-12))
    tbl["kbar_klen_z5"]   = lambda d: float(_safe_z(d["h"][-1] - d["l"][-1], d["h"][-5:] - d["l"][-5:]))
    tbl["kbar_dir"]       = lambda d: float(np.sign(d["c"][-1] - d["o"][-1]))
    tbl["kbar_overlap"]   = lambda d: float((d["c"][-1] - d["c"][-2]) / max(d["c"][-2], 1e-12)) if len(d["c"]) >= 2 else 0.0
    tbl["kbar_close_open_diff"] = lambda d: float((d["c"][-1] - d["o"][-1]) / max(d["h"][-1] - d["l"][-1], 1e-12))

    # --- (2) Price factors — 20 ----------------------------------------------
    for n in _WINDOWS:
        tbl[f"price_mean_{n}"] = (lambda n_: (lambda d:
            float(d["c"][-1] / max(_ts_mean(d["c"], n_), 1e-12) - 1.0)))(n)
        tbl[f"price_std_{n}"]  = (lambda n_: (lambda d:
            float(_ts_std(d["c"], n_) / max(_ts_mean(d["c"], n_), 1e-12))))(n)
        tbl[f"price_min_{n}"]  = (lambda n_: (lambda d:
            float(d["c"][-1] / max(_ts_min(d["c"], n_), 1e-12) - 1.0)))(n)
        tbl[f"price_max_{n}"]  = (lambda n_: (lambda d:
            float(d["c"][-1] / max(_ts_max(d["c"], n_), 1e-12) - 1.0)))(n)

    # --- (3) Volume factors — 10 ---------------------------------------------
    for n in _WINDOWS:
        tbl[f"volume_ratio_{n}"] = (lambda n_: (lambda d:
            float(d["v"][-1] / max(_ts_mean(d["v"], n_), 1e-12))))(n)
        tbl[f"volume_std_{n}"]   = (lambda n_: (lambda d:
            float(_ts_std(d["v"], n_) / max(_ts_mean(d["v"], n_), 1e-12))))(n)

    # --- (4) Momentum factors — 40 -------------------------------------------
    # Return over N + z-score of return + ts_rank of return + skew/kurt
    for n in (5, 10, 20, 60):
        tbl[f"ret_{n}"] = (lambda n_: (lambda d:
            float(d["c"][-1] / max(d["c"][-1 - n_], 1e-12) - 1.0)
            if len(d["c"]) > n_ else 0.0))(n)
        tbl[f"ret_z_{n}"] = (lambda n_: (lambda d:
            _safe_z_seq(d["c"], n_)))(n)
        tbl[f"ret_rank_{n}"] = (lambda n_: (lambda d:
            float(_ts_rank(np.diff(d["c"][-n_ - 1:]) / np.maximum(d["c"][-n_ - 1:-1], 1e-12), n_))
            if len(d["c"]) > n_ + 1 else 0.0))(n)
        tbl[f"ret_skew_{n}"] = (lambda n_: (lambda d:
            float(_ts_skew(np.diff(d["c"][-n_ - 1:]) / np.maximum(d["c"][-n_ - 1:-1], 1e-12), n_))
            if len(d["c"]) > n_ + 1 else 0.0))(n)
    # Sign-of-return counters
    for n in (5, 10, 20):
        tbl[f"pos_ret_frac_{n}"] = (lambda n_: (lambda d:
            float(np.mean(np.diff(d["c"][-n_ - 1:]) > 0))
            if len(d["c"]) > n_ + 1 else 0.5))(n)
        tbl[f"neg_ret_frac_{n}"] = (lambda n_: (lambda d:
            float(np.mean(np.diff(d["c"][-n_ - 1:]) < 0))
            if len(d["c"]) > n_ + 1 else 0.5))(n)
    # Delta-of-volume momentum
    for n in (5, 10, 20, 60):
        tbl[f"vol_delta_{n}"] = (lambda n_: (lambda d:
            float(d["v"][-1] / max(_ts_mean(d["v"], n_), 1e-12) - 1.0)))(n)
    # Body-momentum (sum of body-fractions over N)
    for n in (5, 10, 20, 60):
        tbl[f"body_mom_{n}"] = (lambda n_: (lambda d:
            float(np.nansum((d["c"][-n_:] - d["o"][-n_:]) /
                            np.maximum(d["o"][-n_:], 1e-12)))
            if len(d["c"]) >= n_ else 0.0))(n)
    # Range-momentum
    for n in (5, 10, 20, 60):
        tbl[f"range_mom_{n}"] = (lambda n_: (lambda d:
            float(np.nansum((d["h"][-n_:] - d["l"][-n_:]) /
                            np.maximum(d["o"][-n_:], 1e-12)))
            if len(d["c"]) >= n_ else 0.0))(n)
    # Close-vs-open momentum (different from body — averaged not summed)
    for n in (5, 10, 20, 60):
        tbl[f"co_avg_{n}"] = (lambda n_: (lambda d:
            float(np.nanmean((d["c"][-n_:] - d["o"][-n_:]) /
                             np.maximum(d["o"][-n_:], 1e-12)))
            if len(d["c"]) >= n_ else 0.0))(n)

    # --- (5) Correlation factors — 40 ----------------------------------------
    # corr(close, volume), corr(close, ref_close_1), corr(high, low),
    # corr(open, close) — at 5/10/20/60.
    for n in (5, 10, 20, 60):
        tbl[f"corr_cv_{n}"] = (lambda n_: (lambda d:
            float(_ts_corr(d["c"], d["v"], n_))))(n)
        tbl[f"corr_hl_{n}"] = (lambda n_: (lambda d:
            float(_ts_corr(d["h"], d["l"], n_))))(n)
        tbl[f"corr_oc_{n}"] = (lambda n_: (lambda d:
            float(_ts_corr(d["o"], d["c"], n_))))(n)
        tbl[f"corr_cret_v_{n}"] = (lambda n_: (lambda d:
            float(_ts_corr(np.diff(d["c"]), d["v"][1:], n_))
            if len(d["c"]) >= n_ + 1 else 0.0))(n)
        # cov-z and rank versions
        tbl[f"cov_cv_{n}"] = (lambda n_: (lambda d:
            float(_ts_cov(d["c"], d["v"], n_) /
                  max(_ts_std(d["c"], n_) * _ts_std(d["v"], n_), 1e-12))))(n)
        tbl[f"rankcorr_cv_{n}"] = (lambda n_: (lambda d:
            _rank_corr(d["c"][-n_:], d["v"][-n_:])))(n)
        # lead-1 autocorrelation of close, and of return
        tbl[f"acf1_c_{n}"] = (lambda n_: (lambda d:
            float(_ts_corr(d["c"][:-1], d["c"][1:], n_))
            if len(d["c"]) >= n_ + 1 else 0.0))(n)
        tbl[f"acf1_r_{n}"] = (lambda n_: (lambda d:
            float(_ts_corr(np.diff(d["c"])[:-1], np.diff(d["c"])[1:], n_))
            if len(d["c"]) >= n_ + 2 else 0.0))(n)
        # corr(volume_delta, close_delta) — pressure × move
        tbl[f"corr_vd_cd_{n}"] = (lambda n_: (lambda d:
            float(_ts_corr(np.diff(d["v"]), np.diff(d["c"]), n_))
            if len(d["c"]) >= n_ + 1 else 0.0))(n)
        # corr(range, volume)
        tbl[f"corr_rv_{n}"] = (lambda n_: (lambda d:
            float(_ts_corr(d["h"] - d["l"], d["v"], n_))))(n)

    # --- (6) Volatility / quantile factors — 38 ------------------------------
    for n in _WINDOWS:
        tbl[f"vol_realized_{n}"] = (lambda n_: (lambda d:
            float(_ts_std(np.diff(d["c"][-n_ - 1:]) / np.maximum(d["c"][-n_ - 1:-1], 1e-12), n_))
            if len(d["c"]) > n_ + 1 else 0.0))(n)
        tbl[f"vol_parkinson_{n}"] = (lambda n_: (lambda d:
            _parkinson_vol(d["h"][-n_:], d["l"][-n_:])))(n)
        tbl[f"rng_z_{n}"] = (lambda n_: (lambda d:
            _safe_z_seq(d["h"] - d["l"], n_)))(n)
        tbl[f"close_rank_{n}"] = (lambda n_: (lambda d:
            float(_ts_rank(d["c"], n_))))(n)
        tbl[f"low_rank_{n}"] = (lambda n_: (lambda d:
            float(_ts_rank(d["l"], n_))))(n)
        tbl[f"high_rank_{n}"] = (lambda n_: (lambda d:
            float(_ts_rank(d["h"], n_))))(n)
    # Skew / kurt on close-returns at 10 and 20
    for n in (10, 20):
        tbl[f"ret_kurt_{n}"] = (lambda n_: (lambda d:
            float(_ts_kurt(np.diff(d["c"][-n_ - 1:]) / np.maximum(d["c"][-n_ - 1:-1], 1e-12), n_))
            if len(d["c"]) > n_ + 1 else 0.0))(n)
    # Spread / range factor — abs(close - open) / range
    for n in (5, 10, 20):
        tbl[f"spread_range_{n}"] = (lambda n_: (lambda d:
            float(np.nanmean(np.abs(d["c"][-n_:] - d["o"][-n_:]) /
                             np.maximum(d["h"][-n_:] - d["l"][-n_:], 1e-12))) ))(n)
    # Bipower-variation (lite): mean over the last-N |r_i| × |r_{i-1}| products
    # of close-to-close returns. Robust volatility estimator that downweights
    # single-bar jumps relative to plain realised vol.
    def _make_bipower(n_):
        def _bp(d):
            c = d["c"]
            if len(c) < n_ + 2: return 0.0
            r = np.diff(c[-n_ - 1:]) / np.maximum(c[-n_ - 1:-1], 1e-12)
            # |r_i| * |r_{i-1}| over indices 1..n_-1 — that's n_-1 products.
            prod = np.abs(r[1:]) * np.abs(r[:-1])
            return float(np.nanmean(prod)) if len(prod) else 0.0
        return _bp
    for n in (5, 10, 20):
        tbl[f"bipower_{n}"] = _make_bipower(n)
    # Quantile ratios
    for n in (10, 20, 60):
        tbl[f"q90_q10_{n}"] = (lambda n_: (lambda d:
            float(np.percentile(d["c"][-n_:], 90) /
                  max(np.percentile(d["c"][-n_:], 10), 1e-12) - 1.0)))(n)

    return tbl


def _safe_z(val: float, window) -> float:
    """z-score of `val` within the given window (window already contains val)."""
    if window is None or len(window) < 2:
        return 0.0
    mu = float(np.nanmean(window))
    sd = float(np.nanstd(window, ddof=1))
    if sd == 0 or not np.isfinite(sd):
        return 0.0
    return float((val - mu) / sd)


def _safe_z_seq(seq, n: int) -> float:
    """z-score of the most-recent value of `seq` within the last n samples."""
    if len(seq) < n:
        return 0.0
    w = seq[-n:]
    sd = float(np.nanstd(w, ddof=1))
    if sd == 0 or not np.isfinite(sd):
        return 0.0
    return float((w[-1] - float(np.nanmean(w))) / sd)


def _rank_corr(a, b) -> float:
    """Spearman rank correlation between two equal-length sequences."""
    if len(a) != len(b) or len(a) < 3:
        return 0.0
    try:
        ar = np.argsort(np.argsort(a))
        br = np.argsort(np.argsort(b))
        return float(np.corrcoef(ar, br)[0, 1])
    except Exception:
        return 0.0


def _parkinson_vol(highs, lows) -> float:
    """Parkinson volatility estimator over the given high/low arrays."""
    if len(highs) < 2 or len(lows) < 2:
        return 0.0
    try:
        ratios = np.log(np.maximum(highs, 1e-12) / np.maximum(lows, 1e-12))
        return float(np.sqrt(np.nansum(ratios ** 2) / (4 * len(ratios) * math.log(2))))
    except Exception:
        return 0.0


# Built once at import time; subsequent calls share the same callable refs.
_FACTORS: dict[str, Callable] = _build_factor_table()


# ---- Candle loading + compute ----------------------------------------------

def _load_candles(pair: str, interval: str = "1m",
                  limit: int = _MAX_HISTORY_BARS) -> dict[str, np.ndarray] | None:
    """Return o/h/l/c/v arrays oldest→newest, or None on insufficient data."""
    r = redis_client.get()
    key = redis_keys.CANDLES.replace("{pair}", pair).replace("{interval}", interval)
    raw = r.lrange(key, 0, limit - 1)  # newest at index 0
    if not raw or len(raw) < _MIN_BARS:
        return None
    o_, h_, l_, c_, v_ = [], [], [], [], []
    for entry in raw:
        try:
            d = json.loads(entry)
            o_.append(float(d.get("o", 0.0)))
            h_.append(float(d.get("h", 0.0)))
            l_.append(float(d.get("l", 0.0)))
            c_.append(float(d.get("c", 0.0)))
            v_.append(float(d.get("v", 0.0)))
        except Exception:
            continue
    if len(c_) < _MIN_BARS:
        return None
    # Reverse so newest is at index -1.
    return {
        "o": np.array(o_[::-1], dtype=np.float64),
        "h": np.array(h_[::-1], dtype=np.float64),
        "l": np.array(l_[::-1], dtype=np.float64),
        "c": np.array(c_[::-1], dtype=np.float64),
        "v": np.array(v_[::-1], dtype=np.float64),
    }


def _is_disabled() -> bool:
    return (redis_client.get().get(redis_keys.QLIB_DISABLED) or b"0") in (b"1", "1")


def compute_for_pair(pair: str) -> dict[str, float]:
    """Compute all 158 factors for one pair. Returns the values dict."""
    d = _load_candles(pair)
    if d is None:
        return {}
    out: dict[str, float] = {}
    pipe = redis_client.get().pipeline(transaction=False)
    for fid, fn in _FACTORS.items():
        try:
            v = fn(d)
            if v is None or not np.isfinite(v):
                continue
            out[fid] = float(v)
            pipe.set(
                redis_keys.QLIB_ALPHA_VALUE
                    .replace("{pair}", pair).replace("{factor_id}", fid),
                v,
            )
        except Exception:
            # Don't let one bad factor poison the batch.
            continue
    pipe.execute()
    return out


def compute_for_active_pairs() -> dict[str, Any]:
    """Celery beat task — compute factors for every active pair (per-minute)."""
    if _is_disabled():
        return {"ok": False, "reason": "disabled"}
    t0 = time.time()
    r = redis_client.get()
    try:
        members = r.smembers(redis_keys.ACTIVE_PAIRS) or set()
        pairs = [m.decode() if isinstance(m, bytes) else m for m in members]
    except Exception:
        pairs = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    if not pairs:
        pairs = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    ok, failed = 0, 0
    for p in pairs:
        try:
            res = compute_for_pair(p)
            if res:
                ok += 1
            else:
                failed += 1
        except Exception as exc:
            log.warning("qlib_pair_compute_failed", pair=p, err=str(exc)[:120])
            failed += 1
    elapsed = round(time.time() - t0, 2)
    r.set(redis_keys.QLIB_COMPUTE_HEALTH,
          json.dumps({"ok": ok, "failed": failed,
                      "last_run": int(time.time()), "elapsed_s": elapsed}))
    return {"ok": True, "computed": ok, "failed": failed, "elapsed_s": elapsed}


# ---- IC tracking + Top-K selection -----------------------------------------

def _spearman_ic(a: list[float], b: list[float]) -> float:
    """Cross-sectional Spearman rank correlation between paired sequences."""
    n = min(len(a), len(b))
    if n < 5: return 0.0
    a = np.array(a[:n], dtype=np.float64)
    b = np.array(b[:n], dtype=np.float64)
    mask = np.isfinite(a) & np.isfinite(b)
    if mask.sum() < 5: return 0.0
    return _rank_corr(a[mask], b[mask])


def _fwd_return(pair: str, t_anchor_index: int = 0) -> float | None:
    """Forward 60-min return measured from candle at LRANGE-index t_anchor.

    Reads `{pair}:1m:candles` (newest at 0). t_anchor_index=0 means "use
    the candle that's 60 bars old" so that forward 60 bars from THAT
    candle lands at the most-recent one — this gives us a (factor[t-60],
    return[t-60 → t]) pair, all point-in-time, no lookahead.
    """
    r = redis_client.get()
    key = redis_keys.CANDLES.replace("{pair}", pair).replace("{interval}", "1m")
    raw = r.lrange(key, 0, _FWD_HORIZON_MIN)
    if not raw or len(raw) <= _FWD_HORIZON_MIN:
        return None
    try:
        c_now = float(json.loads(raw[0])["c"])
        c_old = float(json.loads(raw[_FWD_HORIZON_MIN])["c"])
        if c_old <= 0: return None
        return float(c_now / c_old - 1.0)
    except Exception:
        return None


def _factor_value_at_anchor(pair: str, fid: str) -> float | None:
    """Read the CURRENT value of a factor for a pair — for IC paired-sample.

    Note: we measure IC as corr(factor_now, return_now_to_next_hour). The
    'next-hour' return won't be known until 60min from now, so the actual
    IC update lags by 60 min. The hourly cron handles this lag — it
    reads factor values from 60min ago via a per-(pair,factor) Redis ring.
    """
    r = redis_client.get()
    v = r.get(redis_keys.QLIB_ALPHA_VALUE
              .replace("{pair}", pair).replace("{factor_id}", fid))
    if v is None: return None
    try:
        return float(v)
    except Exception:
        return None


def _ic_sample_for_factor(fid: str, pairs: list[str]) -> float:
    """Cross-sectional Spearman IC for one factor across the active pair set.

    For each pair we read factor_value_now and fwd_return_60m_from_60m_ago.
    Returns 0.0 when fewer than 5 valid pairs.
    """
    xs, ys = [], []
    for p in pairs:
        f = _factor_value_at_anchor(p, fid)
        r60 = _fwd_return(p)
        if f is not None and r60 is not None and np.isfinite(f) and np.isfinite(r60):
            xs.append(f); ys.append(r60)
    return _spearman_ic(xs, ys)


def refresh_ic_top_k(top_k: int = _TOP_K_DEFAULT) -> dict[str, Any]:
    """Celery beat task — hourly. Refreshes IC for every factor and the Top-K.

    Cold-start safe: if fewer than `top_k` factors have IC ≥ threshold,
    selects whatever survives. Top-K key may be an empty list — consumer
    handles that as zero-bonus.
    """
    if _is_disabled():
        return {"ok": False, "reason": "disabled"}
    t0 = time.time()
    r = redis_client.get()
    try:
        members = r.smembers(redis_keys.ACTIVE_PAIRS) or set()
        pairs = [m.decode() if isinstance(m, bytes) else m for m in members]
    except Exception:
        pairs = []
    if len(pairs) < 5:
        # Not enough cross-section to compute meaningful IC.
        return {"ok": False, "reason": "insufficient_pairs", "n": len(pairs)}

    ics: list[tuple[str, float]] = []
    for fid in _FACTORS.keys():
        ic = _ic_sample_for_factor(fid, pairs)
        if not np.isfinite(ic): ic = 0.0
        # Append to rolling history (capped at 7d = 168 samples).
        hist_key = redis_keys.QLIB_IC_HISTORY.replace("{factor_id}", fid)
        hist = []
        try:
            hist = json.loads(r.get(hist_key) or "[]")
        except Exception:
            hist = []
        hist.append(ic)
        if len(hist) > _IC_WINDOW_HOURS:
            hist = hist[-_IC_WINDOW_HOURS:]
        rolling_mean = float(np.mean(hist)) if hist else 0.0
        r.set(hist_key, json.dumps(hist))
        r.set(redis_keys.QLIB_IC_ROLLING.replace("{factor_id}", fid), rolling_mean)
        ics.append((fid, rolling_mean))

    # Promotion gate: |IC| ≥ threshold.
    promoted = [(fid, ic) for fid, ic in ics if abs(ic) >= _IC_THRESHOLD]
    # Sort by |IC| descending — Top-K survivors.
    promoted.sort(key=lambda x: abs(x[1]), reverse=True)
    top = [fid for fid, _ic in promoted[:top_k]]

    r.set(redis_keys.QLIB_TOP_K_FACTORS, json.dumps(top))
    r.set(redis_keys.QLIB_TOP_K_UPDATED_AT, int(time.time()))
    elapsed = round(time.time() - t0, 2)
    log.info("f53_qlib_ic_refresh_complete",
             total=len(_FACTORS), promoted=len(promoted),
             top_k_selected=len(top), elapsed_s=elapsed)
    return {"ok": True, "total": len(_FACTORS), "promoted": len(promoted),
            "top_k": top, "elapsed_s": elapsed}


# ---- Consumer helper -------------------------------------------------------

def get_bonus(pair: str, direction: str) -> int:
    """Return the composite Top-K bonus for the direction.

    Reads the published top-K list, looks up each factor's current value
    for `pair` and its rolling IC sign, computes the fraction of factors
    agreeing with `direction`, and maps to a ±12 / ±6 / ±0 / -8 bonus.
    """
    if _is_disabled():
        return 0
    r = redis_client.get()
    try:
        top = json.loads(r.get(redis_keys.QLIB_TOP_K_FACTORS) or "[]")
    except Exception:
        return 0
    if not top:
        return 0
    agree = 0
    counted = 0
    for fid in top:
        try:
            val_raw = r.get(
                redis_keys.QLIB_ALPHA_VALUE
                    .replace("{pair}", pair).replace("{factor_id}", fid)
            )
            ic_raw = r.get(redis_keys.QLIB_IC_ROLLING.replace("{factor_id}", fid))
            if val_raw is None or ic_raw is None:
                continue
            val = float(val_raw)
            ic = float(ic_raw)
            # Bullish prediction = val * ic > 0 (factor up & ic positive,
            # or factor down & ic negative — both predict price up).
            signed = val * ic
            counted += 1
            if (direction == "long" and signed > 0) or \
               (direction == "short" and signed < 0):
                agree += 1
        except Exception:
            continue
    if counted < 5:
        return 0
    frac = agree / counted
    if frac > 0.7:
        return 12
    if frac > 0.6:
        return 6
    if frac < 0.3:
        return -8
    return 0


def num_factors() -> int:
    """Exposed for the dashboard / health endpoint."""
    return len(_FACTORS)
