"""SciBrain funnel gate — the REPLACEMENT for signals/launch_pad/gate.py.

When `scibrain:enabled == "1"` the AI Scientist is the SOLE trade-origin (owner goal,
2026-06-08): it scores the WHOLE pair-scanner universe (~466) through the circuit, picks
the best symbol+direction candidates, and hands them to the opener. Mirrors the launch_pad
gate contract so the engine wiring is a clean swap.

Selection: score all → keep direction!=None & conviction≥min & not on cooldown → rank by
conviction → direction-balance (cap the dominant side so we never get the all-short
concentration the audit flagged) → truncate to max_picks.

Kill switch `scibrain:enabled` (default "0"). When off, funnel_pairs() returns None and the
engine keeps its legacy flow verbatim.
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import structlog

from . import keys as K
from .contracts import Decision
from .runner import score_symbol

log = structlog.get_logger()

_DEF_MIN_CONV = 0.15
_DEF_MAX_PICKS = 10
_DEF_MAX_DIR_FRAC = 0.7
_DEF_COOLDOWN_S = 900
_DEF_CLUSTER_RHO = 0.8
_DEF_MAX_PER_CLUSTER = 2
_DEF_CLUSTER_TF = "1h"
_DEF_CLUSTER_N = 50
_MIN_CORR_OVERLAP = 8        # need ≥ this many aligned returns to trust a correlation
_DEF_WORKERS = max(2, min((os.cpu_count() or 4) - 2, 8))   # leave cores for the SL/hot loop


# ─────────────────────── parallel universe scorer (Phase 5) ───────────────────────
# Scoring the whole universe is CPU-bound (~14 numpy modules × ~491 pairs). A persistent
# process pool fans it across cores so the funnel scan drops from ~20s serial to a few
# seconds, freeing the brain hot loop. FORK (not spawn): the brain's __main__ entrypoint
# is a long-running daemon that is NOT import-safe — spawn re-imports __main__ in every
# worker and would re-run brain startup. Fork children inherit the running state instead
# and never re-import __main__. To stay fork-safe we (a) build a FRESH Redis client per
# worker (never use the inherited parent socket) and (b) pin BLAS to 1 thread per worker
# so 8 workers don't oversubscribe the 10 cores. Pool is built once and reused.
_POOL: ProcessPoolExecutor | None = None
_POOL_WORKERS = 0
_W_R = None   # per-worker Redis client (set by the initializer in each child process)


def _worker_init():
    """Runs once per worker process: pin BLAS threads + give it a private Redis client."""
    global _W_R
    for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
               "NUMEXPR_NUM_THREADS"):
        os.environ.setdefault(_v, "1")
    try:                                   # actually re-limit already-imported BLAS pools
        import threadpoolctl
        threadpoolctl.threadpool_limits(1)
    except Exception:
        pass
    import redis_client
    _W_R = redis_client._build()           # private socket — NEVER the forked parent's


def _worker_score(symbol: str):
    """Score one symbol in a worker; returns a Decision or None. Never raises across
    the process boundary (a raising worker would poison the pool)."""
    global _W_R
    try:
        if _W_R is None:                     # defensive: initializer somehow skipped
            _worker_init()
        return score_symbol(_W_R, symbol, interrogate=False)
    except Exception:
        return None


def _get_pool(workers: int) -> ProcessPoolExecutor | None:
    """Lazily build (and cache) the spawn pool. Resizes if the worker count changed.
    Returns None if the pool can't be created (caller falls back to serial)."""
    global _POOL, _POOL_WORKERS
    if _POOL is not None and _POOL_WORKERS == workers:
        return _POOL
    if _POOL is not None:
        try:
            _POOL.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        _POOL = None
    try:
        import multiprocessing as mp
        ctx = mp.get_context("fork")
        _POOL = ProcessPoolExecutor(max_workers=workers, mp_context=ctx,
                                    initializer=_worker_init)
        _POOL_WORKERS = workers
        log.info("scibrain_scorer_pool_up", workers=workers)
        return _POOL
    except Exception as exc:
        log.warning("scibrain_scorer_pool_failed", error=str(exc)[:160])
        _POOL = None
        _POOL_WORKERS = 0
        return None


def shutdown_pool() -> None:
    """Tear the pool down (e.g. when scibrain is disabled). Safe to call repeatedly."""
    global _POOL, _POOL_WORKERS
    if _POOL is not None:
        try:
            _POOL.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
    _POOL = None
    _POOL_WORKERS = 0


def _collect(pool_iter, min_conv: float) -> tuple[list[Decision], int]:
    """Drain a scorer iterator into (candidates passing direction+min_conv, n_scored)."""
    candidates: list[Decision] = []
    n_scored = 0
    for dec in pool_iter:
        if dec is None:
            continue
        n_scored += 1
        if dec.direction and dec.conviction >= min_conv:
            candidates.append(dec)
    return candidates, n_scored


def _score_cached_parallel(r, symbols: list[str], workers: int,
                           min_conv: float) -> tuple[list[Decision], int]:
    """Phase 5: bulk-load the whole universe into RAM ONCE, then fork a fresh pool so workers inherit
    the snapshot via copy-on-write — build_frame reads from RAM, not Redis (no ~16×N round-trip storm).

    A TRANSIENT pool is required (not the persistent one): COW only shares memory as it exists at fork
    time, so the snapshot must be installed in the parent BEFORE this cycle's workers are forked. The
    snapshot covers every key build_frame reads for these symbols (so workers need no Redis for frames);
    workers still use their own client (_worker_init) for IC/router reads + their scibrain:* writes."""
    import multiprocessing as mp
    from . import sensor_bus

    reader = sensor_bus.bulk_load(r, symbols)        # ONE chunked-pipelined read in the PARENT
    sensor_bus.set_snapshot(reader)                  # install BEFORE fork → children inherit (COW)
    pool = None
    try:
        ctx = mp.get_context("fork")
        pool = ProcessPoolExecutor(max_workers=workers, mp_context=ctx, initializer=_worker_init)
        chunk = max(1, len(symbols) // (workers * 4))
        return _collect(pool.map(_worker_score, symbols, chunksize=chunk), min_conv)
    finally:
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        sensor_bus.set_snapshot(None)                # clear PARENT global; forked children keep their COW copy


def _score_universe(r, symbols: list[str], min_conv: float) -> tuple[list[Decision], int, bool]:
    """Score `symbols` and return (candidates passing direction+min_conv, n_scored, parallel?).

    Tries the process pool; on ANY failure falls back to serial scoring in-process so the
    funnel can never die (C4/C8). Workers stream their own scibrain:{sym}:* keys exactly as
    the serial path does — identical side effects, just fanned across cores."""
    candidates: list[Decision] = []
    n_scored = 0
    parallel = False

    use_parallel = (r.get(K.SCORER_PARALLEL) or "1") != "0"
    workers = _icfg(r, K.SCORER_WORKERS, _DEF_WORKERS)
    if use_parallel and workers > 1 and len(symbols) > workers:
        # Phase 5 in-RAM snapshot path. DEFAULT OFF: live measurement (2026-06-11) showed it REGRESSES
        # cycle time — the per-symbol reads were already parallelized across workers, the scan is
        # compute-bound (PhD modules), and a per-cycle COW fork + serial bulk-read costs more than it
        # saves. Kept behind the flag (set scibrain:scorer_cache_enabled=1 to opt in) as correct,
        # bit-identical infra for a future shared-memory + persistent-pool design; not a win alone.
        if (r.get(K.SCORER_CACHE_ENABLED) or "0") == "1":
            try:
                candidates, n_scored = _score_cached_parallel(r, symbols, workers, min_conv)
                return candidates, n_scored, True
            except Exception as exc:
                log.warning("scibrain_cached_scan_failed_fallback", error=str(exc)[:160])
                from . import sensor_bus
                sensor_bus.set_snapshot(None)        # ensure no stale snapshot leaks into fallback paths
                candidates, n_scored = [], 0
        pool = _get_pool(workers)
        if pool is not None:
            try:
                chunk = max(1, len(symbols) // (workers * 4))
                for dec in pool.map(_worker_score, symbols, chunksize=chunk):
                    if dec is None:
                        continue
                    n_scored += 1
                    if dec.direction and dec.conviction >= min_conv:
                        candidates.append(dec)
                parallel = True
                return candidates, n_scored, parallel
            except Exception as exc:
                log.warning("scibrain_parallel_scan_failed_fallback_serial",
                            error=str(exc)[:160])
                shutdown_pool()
                candidates, n_scored = [], 0   # reset; redo cleanly in serial below

    # serial fallback (also the path when parallel is disabled or universe is tiny)
    for sym in symbols:
        try:
            dec = score_symbol(r, sym, interrogate=False)
        except Exception as exc:
            log.debug("scibrain_gate_score_error", symbol=sym, error=str(exc)[:120])
            continue
        n_scored += 1
        if dec.direction and dec.conviction >= min_conv:
            candidates.append(dec)
    return candidates, n_scored, parallel


def enabled(r) -> bool:
    """True when the Scientist is the live trade-origin funnel."""
    try:
        return r.get(K.ENABLED) == "1"
    except Exception:
        return False


def _fcfg(r, key: str, default: float) -> float:
    try:
        v = r.get(key)
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _icfg(r, key: str, default: int) -> int:
    try:
        v = r.get(key)
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _universe(r) -> list[str]:
    try:
        return list(r.smembers("scanner:active_pairs") or [])
    except Exception:
        return []


def cooldown(r, symbol: str) -> None:
    """Block re-picking `symbol` for COOLDOWN_SECS (called by the opener after an open)."""
    try:
        secs = _icfg(r, K.COOLDOWN_SECS, _DEF_COOLDOWN_S)
        r.zadd(K.COOLDOWN, {symbol: time.time() + secs})
    except Exception:
        pass


def _returns(r, symbol: str, tf: str, n: int) -> np.ndarray | None:
    """Last `n` log-returns of `symbol` on `tf` (oldest-first), or None if too thin.

    Reads the same `{sym}:{tf}:candles` list the SensorBus uses (newest-first JSON),
    so the correlation series is the exact price history the modules saw. Returns are
    order-consistent (oldest-first) across symbols so two vectors are time-aligned."""
    try:
        raw = r.lrange(f"{symbol}:{tf}:candles", 0, n)   # n+1 closes -> n returns
    except Exception:
        return None
    closes: list[float] = []
    for item in raw:
        try:
            closes.append(float(json.loads(item)["c"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    if len(closes) < _MIN_CORR_OVERLAP + 1:
        return None
    closes.reverse()                                     # newest-first -> oldest-first
    arr = np.asarray(closes, dtype=float)
    if not np.all(arr > 0):
        return None
    rets = np.diff(np.log(arr))
    rets = rets[np.isfinite(rets)]
    return rets if len(rets) >= _MIN_CORR_OVERLAP else None


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    """Pearson correlation of the overlapping tail of two return vectors; 0 when it
    can't be measured (too little overlap, zero variance, or non-finite)."""
    m = min(len(a), len(b))
    if m < _MIN_CORR_OVERLAP:
        return 0.0
    a2, b2 = a[-m:], b[-m:]
    if a2.std() == 0.0 or b2.std() == 0.0:
        return 0.0
    rho = float(np.corrcoef(a2, b2)[0, 1])
    return rho if np.isfinite(rho) else 0.0


def _dir_sign(direction: str) -> float:
    return 1.0 if direction == "long" else -1.0


def _cheap_vol_scores(r, symbols, tf: str = "5m", n: int = 16) -> dict:
    """Cheap per-pair recent realized volatility (std of last ~n 5m log-returns) for the prefilter.
    ONE pipelined read of just the last n closes per pair — far lighter than a full SensorFrame.
    Missing/thin/garbled series → 0.0 (that pair falls to the rotation slice). Never raises."""
    scores = {s: 0.0 for s in symbols}
    syms = list(symbols)
    CH = 128
    for i in range(0, len(syms), CH):
        batch = syms[i:i + CH]
        try:
            pipe = r.pipeline(transaction=False)
            for s in batch:
                pipe.lrange(f"{s}:{tf}:candles", 0, n)
            res = pipe.execute()
        except Exception:
            continue
        for s, raw in zip(batch, res):
            closes = []
            for item in (raw or []):
                try:
                    closes.append(float(json.loads(item)["c"]))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
            if len(closes) < 5:
                continue
            arr = np.asarray(closes[::-1], dtype=float)        # newest-first -> oldest-first
            if not np.all(arr > 0):
                continue
            rets = np.diff(np.log(arr))
            rets = rets[np.isfinite(rets)]
            if len(rets) >= 4:
                scores[s] = float(np.std(rets))
    return scores


def _prefilter_select(r, symbols, k: int, rotate: int, cycle: int) -> tuple[list[str], dict]:
    """Pick the subset to HEAVILY score: top-K by recent volatility ∪ a rotating slice of the rest, so
    every pair is fully scored within ~len(rest)/rotate cycles → no pair is starved (Rule 14 coverage)."""
    syms = list(symbols)
    if k <= 0 or len(syms) <= k:
        return syms, {"n_total": len(syms), "n_topk": len(syms), "n_rotate": 0}
    scores = _cheap_vol_scores(r, syms)
    ranked = sorted(syms, key=lambda s: scores.get(s, 0.0), reverse=True)
    topk, rest = ranked[:k], ranked[k:]
    sel = list(topk)
    n_rot = 0
    if rotate > 0 and rest:
        start = (cycle * rotate) % len(rest)
        sel.extend(rest[(start + j) % len(rest)] for j in range(min(rotate, len(rest))))
        n_rot = min(rotate, len(rest))
    seen: set = set()
    out = [s for s in sel if not (s in seen or seen.add(s))]
    return out, {"n_total": len(syms), "n_topk": len(topk), "n_rotate": n_rot}


def funnel_pairs(r, *, interrogate: bool = False) -> list[Decision] | None:
    """Score the whole universe and return the ranked, direction-balanced picks.
    Returns None when the kill switch is off. Returns [] when nothing qualifies
    (engine then opens nothing this cycle). Never raises."""
    if not enabled(r):
        shutdown_pool()        # don't hold idle worker processes while disabled
        return None

    # Throttle: scoring the whole universe is ~10s of CPU. The brain loop also runs
    # SL monitoring, so re-score at most every `funnel_interval_s` (default 20s) and
    # return [] (no new opens) in between — keeps the hot loop responsive (C8).
    interval = _icfg(r, "scibrain:funnel_interval_s", 20)
    try:
        last = float(r.get("scibrain:last_funnel") or 0)
    except (TypeError, ValueError):
        last = 0.0
    if interval > 0 and (time.time() - last) < interval:
        return []

    t0 = time.time()
    min_conv = _fcfg(r, K.MIN_CONVICTION, _DEF_MIN_CONV)
    max_picks = _icfg(r, K.MAX_PICKS, _DEF_MAX_PICKS)
    max_dir_frac = _fcfg(r, K.MAX_DIR_FRAC, _DEF_MAX_DIR_FRAC)
    rho_max = _fcfg(r, K.CLUSTER_RHO, _DEF_CLUSTER_RHO)
    max_per_cluster = _icfg(r, K.MAX_PER_CLUSTER, _DEF_MAX_PER_CLUSTER)
    cluster_tf = (r.get(K.CLUSTER_TF) or _DEF_CLUSTER_TF)
    cluster_n = _icfg(r, K.CLUSTER_N, _DEF_CLUSTER_N)
    cluster_on = rho_max < 1.0 and max_per_cluster > 0
    now = time.time()

    # settle any matured IC observations ONCE per cycle here in the main process (workers
    # only record; centralizing settlement means a matured vote is graded exactly once).
    try:
        from . import ic_tracker
        ic_tracker.settle(r, now)
    except Exception as exc:
        log.debug("scibrain_ic_settle_failed", error=str(exc)[:120])

    # InformationGeometryHealth (§6e Tier-B): bank-level recalibration monitor — model-manifold drift,
    # IC-transferability, nonlinear redundancy over the module-output distributions. Main process, once
    # per cycle (reads the same ic:pending vectors settle just consumed). Tier-0: report only, no authority.
    try:
        from . import info_geometry
        info_geometry.assess(r, now)
    except Exception as exc:
        log.debug("scibrain_infogeo_failed", error=str(exc)[:120])

    # Module ablation/prune/demote report (§6g.8/§6g.10, VS-12): judge the bank on incremental IC,
    # redundancy, and stability and publish an ADVISORY verdict per module. Runs AFTER settle+infogeo
    # so it consumes their fresh IC + redundancy matrix. Separately guarded — advisory only, never acts.
    try:
        from . import ablation
        ablation.assess(r, now)
    except Exception as exc:
        log.debug("scibrain_ablation_failed", error=str(exc)[:120])

    universe = _universe(r)

    # Universe Core (Phase 2b): build the read-only cross-market UniverseFrame ONCE per cycle
    # in this (main) process before fanning out per-symbol scoring. It's the shared substrate the
    # future cross-market modules read in-process; here it also publishes a live market-state digest
    # (breadth/crowding/factor-share). Built over the FULL universe (cross-market structure includes
    # cooled-down pairs). Tier-0 — never affects picks; a build failure is logged + counted, not fatal.
    try:
        from . import universe_frame
        _uframe = universe_frame.build_or_get(r, universe)
        # Universe-Core modules score the shared frame ONCE here (main process) and publish their
        # per-symbol votes; the per-symbol scorer (workers) folds them into fusion. They ship
        # shadow_only, so they're recorded + IC-evaluable but never move a live pick.
        universe_frame.run_universe_modules(r, _uframe)
        # VS-V5: mirror a bounded real topology snapshot (clusters/MDS nodes/directed lead-lag edges)
        # of the SAME frame for the dashboard Universe Neural Field. Ts-guarded to actual rebuilds,
        # BLAS-pinned; Tier-0 — never affects picks.
        from . import universe_field
        universe_field.publish_field(r, _uframe)
    except Exception as exc:
        log.debug("scibrain_universe_frame_failed", error=str(exc)[:120])

    # drop cooled-down symbols in ONE ZSET read instead of a zscore per pair
    try:
        on_cd = set(r.zrangebyscore(K.COOLDOWN, now, "+inf"))
    except Exception:
        on_cd = set()
    to_score = [s for s in universe if s not in on_cd]

    # Phase 5 top-K prefilter (compute-bound scan). SHADOW: score everyone but MEASURE whether the
    # top-K∪rotation would have captured the qualifying candidates (zero risk). ON: actually restrict.
    pf_mode = (r.get(K.PREFILTER_MODE) or "off")
    pf_selected = None
    pf_k = pf_rotate = 0
    if pf_mode in ("shadow", "on"):
        try:
            pf_cycle = int(r.incr(K.PREFILTER_CYCLE))
        except Exception:
            pf_cycle = 0
        pf_k = _icfg(r, K.PREFILTER_TOP_K, 150)
        pf_rotate = _icfg(r, K.PREFILTER_ROTATE, 60)
        pf_selected, _pf_meta = _prefilter_select(r, to_score, pf_k, pf_rotate, pf_cycle)
        if pf_mode == "on":
            to_score = pf_selected

    candidates, n_scored, parallel = _score_universe(r, to_score, min_conv)

    candidates.sort(key=lambda d: d.conviction, reverse=True)

    # prefilter coverage telemetry (Rule 12/14): in SHADOW, candidates are the FULL-universe qualifiers,
    # so coverage = fraction the top-K∪rotation captured — the evidence to safely flip to "on".
    if pf_mode in ("shadow", "on") and pf_selected is not None:
        try:
            sel_set = set(pf_selected)
            cand_syms = {c.symbol for c in candidates}
            coverage = (len(cand_syms & sel_set) / len(cand_syms)) if cand_syms else 1.0
            r.setex(K.PREFILTER_STATUS, K.HOT_TTL, json.dumps({
                "mode": pf_mode, "k": pf_k, "rotate": pf_rotate,
                "n_scored": len(to_score), "n_selected": len(sel_set),
                "n_candidates": len(cand_syms), "coverage": round(coverage, 4),
                "missed": sorted(cand_syms - sel_set)[:20], "ts": round(now, 2),
            }, separators=(",", ":")))
        except Exception as exc:
            log.debug("scibrain_prefilter_status_failed", error=str(exc)[:120])

    # ── selection: two risk caps applied greedily in conviction order ──
    # (1) direction-balance: cap each side so the book can't collapse to one direction.
    # (2) correlation-cluster: cap how many picks share a risk cluster, where "cluster"
    #     means signed-return-correlation ρ·sign(dᵢ)·sign(dⱼ) ≥ rho_max. Two longs (or two
    #     shorts) in co-moving assets concentrate the SAME factor bet; a long+short in
    #     co-moving assets is a hedge (negative signed-ρ) and is NOT clustered. This kills
    #     correlated concentration the direction count alone can't see.
    cap_dom = max(1, int(round(max_picks * max_dir_frac)))
    cnt = {"long": 0, "short": 0}
    picks: list[Decision] = []
    pick_rets: list[np.ndarray | None] = []   # return vector per accepted pick (None if thin)
    pick_cluster: list[int] = []              # cluster id per accepted pick
    cluster_size: dict[int, int] = {}
    next_cluster = 0
    dropped_dir = 0
    dropped_cluster = 0

    for d in candidates:
        if len(picks) >= max_picks:
            break
        if cnt.get(d.direction, 0) >= cap_dom:
            dropped_dir += 1
            continue

        cid: int | None = None
        rets = _returns(r, d.symbol, cluster_tf, cluster_n) if cluster_on else None
        if rets is not None:
            sign = _dir_sign(d.direction)
            for i, prv in enumerate(picks):
                if pick_rets[i] is None:
                    continue
                signed_rho = _corr(rets, pick_rets[i]) * sign * _dir_sign(prv.direction)
                if signed_rho >= rho_max:
                    cid = pick_cluster[i]          # joins the strongest-conviction neighbour's cluster
                    break
        if cid is not None and cluster_size.get(cid, 0) >= max_per_cluster:
            dropped_cluster += 1
            continue

        if cid is None:                            # opens a new cluster (or unmeasurable → its own)
            cid = next_cluster
            next_cluster += 1
        picks.append(d)
        pick_rets.append(rets)
        pick_cluster.append(cid)
        cluster_size[cid] = cluster_size.get(cid, 0) + 1
        cnt[d.direction] = cnt.get(d.direction, 0) + 1

    # PICK-level prefilter evidence (Rule 14): in SHADOW the picks above are the TRUE full-universe
    # picks, so "would top-K∪rotation have KEPT them?" is the capital-relevant safety metric — the
    # qualifier coverage published above over-counts low-rank qualifiers that never open a trade. A
    # rolling aggregate (picks_captured/picks_total across cycles, + cycles that starved a real pick)
    # is the honest gate for flipping prefilter_mode to "on". CONSUMER: dashboard /scibrain.
    if pf_mode in ("shadow", "on") and pf_selected is not None and picks:
        try:
            sel_set = set(pf_selected)
            pick_syms = [d.symbol for d in picks]
            captured = sum(1 for s in pick_syms if s in sel_set)
            missed_picks = [s for s in pick_syms if s not in sel_set]
            r.hincrby(K.PREFILTER_AGG, "pick_cycles", 1)
            r.hincrby(K.PREFILTER_AGG, "picks_total", len(pick_syms))
            r.hincrby(K.PREFILTER_AGG, "picks_captured", captured)
            if missed_picks:
                r.hincrby(K.PREFILTER_AGG, "starve_cycles", 1)
                r.hset(K.PREFILTER_AGG, "last_missed", json.dumps(missed_picks))
                r.hset(K.PREFILTER_AGG, "last_missed_ts", round(now, 2))
        except Exception as exc:
            log.debug("scibrain_prefilter_pickcov_failed", error=str(exc)[:120])

    cycle_ms = int((time.time() - t0) * 1000)
    try:
        r.set("scibrain:last_funnel", int(time.time()))
        r.incrby(K.PICK_COUNT, len(picks))
        if dropped_dir:
            r.incrby(K.DIR_CAPPED, dropped_dir)
        if dropped_cluster:
            r.incrby(K.CLUSTER_CAPPED, dropped_cluster)
        # heartbeat for the dashboard panel header (the funnel is the live cadence now)
        r.setex(K.STATUS, K.HOT_TTL, json.dumps({
            "ts": round(time.time(), 3), "pairs": len(universe),
            "scored": n_scored, "actionable": len(candidates),
            "picks": len(picks), "longs": cnt["long"], "shorts": cnt["short"],
            "clusters": next_cluster, "dropped_dir": dropped_dir,
            "dropped_cluster": dropped_cluster, "cycle_ms": cycle_ms,
            "parallel": parallel, "workers": (_POOL_WORKERS if parallel else 1),
        }))
        r.incr(K.CYCLES)
    except Exception:
        pass
    log.info("scibrain_funnel", universe=len(universe), scored=n_scored,
             actionable=len(candidates), picks=len(picks), longs=cnt["long"],
             shorts=cnt["short"], clusters=next_cluster, dropped_dir=dropped_dir,
             dropped_cluster=dropped_cluster, parallel=parallel,
             workers=(_POOL_WORKERS if parallel else 1), ms=cycle_ms)
    return picks
