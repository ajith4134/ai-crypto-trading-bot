"""SciBrain Universe Core — build the read-only in-RAM `UniverseFrame` once per cycle.

Design §4a/§5a: most of the 14 hot modules inspect ONE symbol at a time, so they can't see
market-wide factor flow, directed contagion, or crowding. The Universe Core computes the
expensive cross-market objects ONCE per funnel cycle from a shared `UniverseFrame` (return /
feature / correlation / lead-lag / liquidity matrices) and the future cross-market modules
(SparseFactorResidual, SpectralGraphContagion, CausalLeadLag, OptimalTransportRegime…) read it
in-process — instead of each re-reading the whole universe (the Phase-5 round-trip storm).

This module ships the FRAME itself (the shared substrate + a cheap, immediately-consumed
market-state digest). It is Tier-0 observability: it has NO trading authority — it only builds a
read-only object and mirrors a digest to Redis for the dashboard. The directional cross-market
modules that VOTE on top of it are separate admitted components (Tier-A table, §6e).

Built ONCE per cycle in the main process (the workers fan out per-symbol scoring; the universe
substrate is a single shared object, not a per-symbol one). Cached in-RAM behind a cycle guard so
a busy hot loop can't rebuild it more than once per `universe_interval_s`. Every read is
defensive — a missing/garbled symbol is dropped from the frame, never fabricated (C4/Rule 12).
"""
from __future__ import annotations

import json
import time

import numpy as np
import structlog

from . import keys as K
from .contracts import UniverseFrame

log = structlog.get_logger()

_DEF_TFS = ("1h", "15m")
_DEF_PRIMARY_TF = "1h"
_DEF_LOOKBACK = 120
_DEF_INTERVAL_S = 60      # Universe-Core cadence — deliberately SLOWER than the 20s funnel: the
                          # cross-market decomposition (RPCA) costs ~seconds and market structure is
                          # slow-moving, so it runs once per minute and the hot loop stays responsive
                          # on the intervening funnel cycles (the ts-guard skips RPCA when the frame
                          # is still cached). Tunable via scibrain:universe_interval_s.
_DEF_MIN_SYMBOLS = 20
_MIN_RETURNS = 8          # a symbol needs ≥ this many finite returns on the primary TF to be in the frame

# ── in-RAM shared cache (single-process: the gate builds it in the brain main process) ──
_CACHE: dict = {"frame": None, "built_at": 0.0, "primary_tf": None}
# ts of the frame the Universe-Core modules last scored — so RPCA et al. recompute ONLY when the
# frame actually rebuilds (not on every funnel cycle that reuses the cached frame). This is what
# makes a separately-budgeted slower Universe-Core cadence (universe_interval_s) actually save CPU.
_LAST_MODULE_TS: float = 0.0


def get_current_frame() -> UniverseFrame | None:
    """Read-only accessor for the most recently built frame (what Universe modules consume).
    Returns None if no frame has been built yet this run."""
    return _CACHE.get("frame")


def _icfg(r, key: str, default: int) -> int:
    try:
        v = r.get(key)
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _tfs(r) -> list[str]:
    try:
        raw = r.get(K.UNIVERSE_TFS)
    except Exception:
        raw = None
    if not raw:
        return list(_DEF_TFS)
    tfs = [t.strip() for t in str(raw).split(",") if t.strip()]
    return tfs or list(_DEF_TFS)


def _read_closes_vols(r, symbols: list[str], tf: str, lookback: int):
    """ONE pipelined round trip for the whole universe on `tf`. Returns
    {symbol: (closes, vols)} oldest-first float arrays, parsed defensively. A symbol whose
    list is missing/garbled is simply absent from the dict (it drops out of the frame)."""
    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    try:
        pipe = r.pipeline(transaction=False)
        for s in symbols:
            pipe.lrange(f"{s}:{tf}:candles", 0, lookback)   # newest-first
        raw_lists = pipe.execute()
    except Exception as exc:
        log.warning("universe_frame_read_failed", tf=tf, error=str(exc)[:160])
        return out
    for s, raw in zip(symbols, raw_lists):
        if not raw:
            continue
        closes: list[float] = []
        vols: list[float] = []
        for item in raw:
            try:
                c = json.loads(item)
                closes.append(float(c["c"]))
                vols.append(float(c.get("v", 0.0)))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        if len(closes) < _MIN_RETURNS + 1:
            continue
        closes.reverse()                                    # newest-first -> oldest-first
        vols.reverse()
        ca = np.asarray(closes, dtype=float)
        va = np.asarray(vols, dtype=float)
        if not np.all(ca > 0):
            continue
        out[s] = (ca, va)
    return out


def _returns_matrix(close_vol: dict, symbols: list[str], n_ret: int):
    """Build a time-aligned (T, S) log-returns matrix over `symbols` (col order preserved).
    Every column is the LAST `n_ret` returns (newest-aligned), so two columns share the same
    calendar tail. Symbols already filtered to have enough history by the caller."""
    cols = []
    for s in symbols:
        ca, _va = close_vol[s]
        rets = np.diff(np.log(ca))
        rets = rets[np.isfinite(rets)]
        cols.append(rets[-n_ret:])
    return np.column_stack(cols)        # (n_ret, S)


def _correlation(ret_mat: np.ndarray) -> np.ndarray:
    """Pearson correlation across columns (symbols). Zero-variance columns get a 0 row/col and
    a 1 on the diagonal so the matrix stays well-formed (no NaNs leak to consumers)."""
    s = ret_mat.shape[1]
    std = ret_mat.std(axis=0)
    corr = np.eye(s, dtype=float)
    good = std > 1e-12
    if good.sum() >= 2:
        sub = ret_mat[:, good]
        c = np.corrcoef(sub, rowvar=False)
        c = np.nan_to_num(c, nan=0.0, posinf=0.0, neginf=0.0)
        np.fill_diagonal(c, 1.0)
        idx = np.where(good)[0]
        corr[np.ix_(idx, idx)] = c
    return np.clip(corr, -1.0, 1.0)


def _directed_lead_lag(ret_mat: np.ndarray) -> np.ndarray:
    """Lag-1 directed cross-correlation L[i,j] = corr(r_i[t-1], r_j[t]): does symbol i's PAST
    return predict symbol j's NEXT return? Asymmetric (L != L.T) → a directed predictive-flow
    primitive the contagion/lead-lag modules build on. Standardize then normalized inner product."""
    past = ret_mat[:-1, :]      # r[t-1]
    fut = ret_mat[1:, :]        # r[t]
    n = past.shape[0]
    if n < _MIN_RETURNS:
        s = ret_mat.shape[1]
        return np.zeros((s, s), dtype=float)
    pm = past - past.mean(axis=0)
    fm = fut - fut.mean(axis=0)
    ps = past.std(axis=0)
    fs = fut.std(axis=0)
    denom = np.outer(ps, fs) * n
    with np.errstate(divide="ignore", invalid="ignore"):
        ll = (pm.T @ fm) / denom
    ll = np.nan_to_num(ll, nan=0.0, posinf=0.0, neginf=0.0)
    return np.clip(ll, -1.0, 1.0)


_FEATURE_NAMES = ("last_ret", "vol", "momentum", "log_liquidity")


def _feature_matrix(close_vol: dict, symbols: list[str], ret_mat: np.ndarray):
    """(S, 4) per-symbol summary features aligned to `symbols`:
       last_ret (newest primary-TF return), vol (std of returns), momentum (sum of returns
       in the window = log total move), log_liquidity (log median close·volume)."""
    feats = np.zeros((len(symbols), len(_FEATURE_NAMES)), dtype=float)
    for j, s in enumerate(symbols):
        col = ret_mat[:, j]
        ca, va = close_vol[s]
        dvol = ca * va                       # dollar volume per bar
        dvol = dvol[np.isfinite(dvol) & (dvol > 0)]
        log_liq = float(np.log(np.median(dvol))) if dvol.size else 0.0
        feats[j, 0] = float(col[-1])
        feats[j, 1] = float(col.std())
        feats[j, 2] = float(col.sum())
        feats[j, 3] = log_liq
    return feats


def _digest(symbols, ret_mat, corr, lead_lag, feats) -> dict:
    """Cheap, immediately-consumable market-state observables derived from the frame. These are
    the live-visible mirror; the full matrices stay in-RAM for the in-process Universe modules."""
    s = len(symbols)
    last_ret = feats[:, 0]
    breadth_up = float((last_ret > 0).mean()) if s else 0.0
    # mean absolute off-diagonal correlation = systemic co-movement / crowding
    if s >= 2:
        iu = np.triu_indices(s, k=1)
        mean_abs_corr = float(np.abs(corr[iu]).mean())
        mean_corr = float(corr[iu].mean())
    else:
        mean_abs_corr = mean_corr = 0.0
    # PC1 variance share = how one-factor the market is (largest eigenvalue / trace; trace == S)
    market_factor_share = 0.0
    if s >= 2:
        try:
            evals = np.linalg.eigvalsh(corr)
            tot = float(evals.sum())
            if tot > 1e-9:
                market_factor_share = float(evals[-1] / tot)
        except np.linalg.LinAlgError:
            market_factor_share = 0.0
    return {
        "breadth_up": breadth_up,                       # fraction of pairs with a positive last return
        "net_breadth": breadth_up - 0.5,                # signed risk-on/off tilt
        "return_dispersion": float(last_ret.std()) if s else 0.0,
        "mean_abs_corr": mean_abs_corr,                 # crowding / systemic co-movement
        "mean_corr": mean_corr,                         # signed average correlation
        "market_factor_share": market_factor_share,     # PC1 dominance (1 = single-factor market)
        "mean_abs_lead_lag": float(np.abs(lead_lag).mean()) if s else 0.0,
        "median_log_liquidity": float(np.median(feats[:, 3])) if s else 0.0,
    }


def build_frame(r, symbols: list[str]) -> UniverseFrame | None:
    """Construct a fresh UniverseFrame from live Redis. Returns None (and bumps the skip
    counter) when too few symbols have usable history. Never raises into the caller."""
    primary_tf = (r.get(K.UNIVERSE_PRIMARY_TF) or _DEF_PRIMARY_TF)
    tfs = _tfs(r)
    if primary_tf not in tfs:
        tfs = [primary_tf] + tfs
    lookback = _icfg(r, K.UNIVERSE_LOOKBACK, _DEF_LOOKBACK)
    min_symbols = _icfg(r, K.UNIVERSE_MIN_SYMBOLS, _DEF_MIN_SYMBOLS)

    # read every TF once (pipelined). The frame is the INTERSECTION of symbols that have enough
    # history on the PRIMARY tf — those define the aligned column order for every matrix.
    reads: dict[str, dict] = {}
    for tf in tfs:
        reads[tf] = _read_closes_vols(r, symbols, tf, lookback)

    primary = reads.get(primary_tf, {})
    usable = [s for s in symbols if s in primary]      # keep the universe's ordering, drop the unusable
    if len(usable) < min_symbols:
        try:
            r.incr(K.UNIVERSE_SKIPS)
        except Exception:
            pass
        log.warning("universe_frame_too_thin", usable=len(usable),
                    need=min_symbols, primary_tf=primary_tf)
        return None

    # the aligned window length = the shortest usable history across the kept symbols (≥ _MIN_RETURNS)
    n_ret = min(int(len(primary[s][0]) - 1) for s in usable)
    n_ret = max(_MIN_RETURNS, min(n_ret, lookback))

    ret_primary = _returns_matrix(primary, usable, n_ret)
    corr = _correlation(ret_primary)
    lead_lag = _directed_lead_lag(ret_primary)
    feats = _feature_matrix(primary, usable, ret_primary)
    digest = _digest(usable, ret_primary, corr, lead_lag, feats)

    returns_by_tf = {primary_tf: ret_primary}
    for tf in tfs:
        if tf == primary_tf:
            continue
        cv = reads.get(tf, {})
        # only symbols present on BOTH this tf and the primary, in the primary's column order
        cols_ok = [s for s in usable if s in cv and len(cv[s][0]) > _MIN_RETURNS]
        if len(cols_ok) >= min_symbols:
            n2 = min(int(len(cv[s][0]) - 1) for s in cols_ok)
            n2 = max(_MIN_RETURNS, min(n2, lookback))
            # build aligned to the FULL usable set: missing symbols get a zero column (kept aligned)
            mat = np.zeros((n2, len(usable)), dtype=float)
            present = _returns_matrix(cv, cols_ok, n2)
            pos = {s: i for i, s in enumerate(usable)}
            for k, s in enumerate(cols_ok):
                mat[:, pos[s]] = present[:, k]
            returns_by_tf[tf] = mat

    return UniverseFrame(
        symbols=usable,
        primary_tf=primary_tf,
        returns_by_tf=returns_by_tf,
        feature_matrix=feats,
        feature_names=_FEATURE_NAMES,
        correlation=corr,
        directed_lead_lag=lead_lag,
        liquidity=feats[:, 3].copy(),
        digest=digest,
    )


def build_or_get(r, symbols: list[str], *, force: bool = False) -> UniverseFrame | None:
    """Build the shared frame at most once per `universe_interval_s`; otherwise return the cached
    in-RAM frame. This is what the gate calls each funnel cycle: a busy hot loop reuses the same
    object, the digest is refreshed only on an actual rebuild. Publishes the digest mirror + counter.

    `force=True` ignores the cache (used by read-only consumers/tests that want a fresh build)."""
    interval = _icfg(r, K.UNIVERSE_INTERVAL, _DEF_INTERVAL_S)
    now = time.time()
    cached = _CACHE.get("frame")
    if (not force and cached is not None and interval > 0
            and (now - float(_CACHE.get("built_at") or 0)) < interval):
        return cached

    frame = build_frame(r, symbols)
    if frame is None:
        return cached      # keep serving the last good frame to in-process consumers; skip counter already bumped

    _CACHE["frame"] = frame
    _CACHE["built_at"] = now
    _CACHE["primary_tf"] = frame.primary_tf
    try:
        r.setex(K.UNIVERSE_STATE, K.HOT_TTL, json.dumps(frame.to_digest_dict()))
        r.incr(K.UNIVERSE_BUILDS)
    except Exception as exc:
        log.debug("universe_frame_publish_failed", error=str(exc)[:120])
    log.info("universe_frame_built", symbols=frame.n_symbols, tfs=list(frame.returns_by_tf),
             primary_tf=frame.primary_tf,
             breadth_up=round(frame.digest.get("breadth_up", 0.0), 3),
             mean_abs_corr=round(frame.digest.get("mean_abs_corr", 0.0), 3),
             factor_share=round(frame.digest.get("market_factor_share", 0.0), 3))
    return frame


def _universe_modules_enabled(r) -> bool:
    try:
        return (r.get(K.UNIVERSE_MODULES_ENABLED) or "1") != "0"
    except Exception:
        return True


def run_universe_modules(r, frame: UniverseFrame | None) -> int:
    """Run the Universe-Core module bank ONCE over the shared frame (main process, once per cycle)
    and publish each module's per-symbol ModuleOutput to scibrain:universe:contrib:{sym} so the
    per-symbol scorer (in the fork workers) can fold them into fusion. Returns the number of symbols
    that received at least one universe vote. Never raises; a module failure degrades to no votes.

    Tier-0/shadow: the modules ship shadow_only=True, so fusion RECORDS but never APPLIES them.
    Publishing the votes here is what makes them visible + IC-evaluable across the process boundary."""
    global _LAST_MODULE_TS
    if frame is None or not _universe_modules_enabled(r):
        return 0
    # only (re)score when the frame actually rebuilt — a cached frame's contrib is already published
    # and TTL-valid, so recomputing the same decomposition every funnel cycle is pure waste.
    if frame.ts == _LAST_MODULE_TS:
        return 0
    try:
        from .universe_modules import UNIVERSE_MODULES
    except Exception as exc:
        log.warning("universe_modules_import_failed", error=str(exc)[:160])
        return 0
    if not UNIVERSE_MODULES:
        return 0

    t0 = time.time()
    # PIN BLAS to 1 thread for the heavy linear algebra (SVD/eigsh/solve). The brain main process
    # runs alongside the 8-worker parallel scorer + candlenet/cn_train containers, so on the saturated
    # box an UNpinned numpy oversubscribes threads and thrashes — measured 14s vs 0.17s for the RPCA
    # at 1 thread (~80×). The fork workers already pin to 1 (gate._worker_init); the main process must too.
    try:
        import threadpoolctl
        _tp_ctx = threadpoolctl.threadpool_limits(1)
    except Exception:
        _tp_ctx = None

    # symbol -> list[ModuleOutput] accumulated across the bank
    per_sym: dict[str, list] = {}
    ran: list[str] = []
    try:
        for mod in UNIVERSE_MODULES:
            outs = mod.evaluate(frame)          # {symbol: ModuleOutput}; {} on failure (safe)
            if not outs:
                continue
            ran.append(mod.name)
            for sym, mo in outs.items():
                per_sym.setdefault(sym, []).append(mo)
    finally:
        if _tp_ctx is not None:
            try:
                _tp_ctx.restore()
            except Exception:
                pass

    if not per_sym:
        _LAST_MODULE_TS = frame.ts      # nothing emitted on this frame; don't re-attempt it every cycle
        return 0
    _LAST_MODULE_TS = frame.ts

    interval = _icfg(r, K.UNIVERSE_INTERVAL, _DEF_INTERVAL_S)
    ttl = max(60, interval * 3)             # contrib survives a few cycles; absent key → no contribution
    n_published = 0
    try:
        pipe = r.pipeline(transaction=False)
        for sym, mos in per_sym.items():
            pipe.setex(K.UNIVERSE_CONTRIB.replace("{sym}", sym), ttl,
                       json.dumps([mo.to_dict() for mo in mos]))
            n_published += 1
        ms = int((time.time() - t0) * 1000)
        pipe.setex(K.UNIVERSE_MODULES_SUMMARY, K.HOT_TTL, json.dumps({
            "modules": ran, "n_symbols": n_published, "ms": ms,
            "ts": round(time.time(), 3),
        }))
        pipe.incr(K.UNIVERSE_MODULE_RUNS)
        pipe.execute()
    except Exception as exc:
        log.debug("universe_modules_publish_failed", error=str(exc)[:120])
    log.info("universe_modules_ran", modules=ran, symbols=n_published,
             ms=int((time.time() - t0) * 1000))
    return n_published
