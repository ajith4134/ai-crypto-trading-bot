"""OptimalTransportRegime — sliced-Wasserstein geometry of the cross-sectional feature
distribution + an OUTCOME-LEARNED winner/loser prototype classifier (Universe Core, §6e Tier-A,
role gate+context).

UNIQUE EVIDENCE (§6g.1/2): every existing regime/distribution signal in the bank is blind to the
*geometry of distribution migration*. HMM emits a label, the digest emits point summaries (breadth,
crowding), KL needs overlapping support. Optimal transport measures the actual COST of moving the
whole cross-sectional feature cloud from one shape to another — it sees tail/shape migration a mean
or a label cannot. No other module compares DISTRIBUTIONS via transport. evidence_family=
'distribution_transport'.

TWO falsifiable outputs:

 1. REGIME / MIGRATION (market-level context). Each cycle the current cross-sectional, column-
    standardized feature cloud X (S×F) is compared by sliced-Wasserstein distance to a small,
    self-bootstrapping library of past regime-prototype clouds. nearest = current regime;
    migration_velocity = |min_dist_now − min_dist_prev| = how fast the market's distribution is
    geometrically moving. A genuinely novel cloud (far from every prototype) is admitted as a new
    prototype (capped library). Published to scibrain:ot:state for the dashboard; it also damps
    per-symbol conviction when the regime is migrating fast (the learned map is least trustworthy
    mid-migration).

 2. WINNER/LOSER per-symbol vote (OUTCOME-COUPLED, the §-fork the owner approved). The module keeps
    two FIFO clouds of column-standardized feature vectors, labelled by REALIZED forward return:
    a vector goes to the WINNER cloud if the symbol subsequently rose (fwd_ret > +τ), the LOSER
    cloud if it fell (< −τ). FALSIFIABLE HYPOTHESIS: a symbol whose current standardized feature
    vector is transport-closer to the winner cloud than the loser cloud continues UP. Per symbol:
        prox_w = sliced barycentric distance(x_j → winner cloud)
        prox_l = sliced barycentric distance(x_j → loser cloud)
        direction_j = tanh((prox_l − prox_w)/SCALE)      (closer to winners ⇒ long)
        conviction_j = clip(|prox_l − prox_w|/SCALE)·cloud_maturity·regime_stability
    These are learned from real outcomes — the winner/loser clouds are EMPTY at cold start and the
    per-symbol vote ABSTAINS until both clouds reach _MIN_CLOUD labelled samples (no fabricated
    prototypes; the regime/migration context still publishes during cold start).

STATEFUL (unlike the pure decomposition modules): it owns an outcome ledger in Redis (pending ZSET
→ matured fwd-return → winner/loser FIFO clouds + regime library). The SCORING is a pure,
deterministic function of (frame, clouds, regime read); the settle/record/regime-update are
separate idempotent state ops (each pending member is ZREM'd as consumed, so a crash loses — never
double-counts — a sample, mirroring ic_tracker.settle).

ADMISSION (§6g + Rule 14): ships shadow_only=True — RECORDED + IC-evaluable, NEVER applied to a live
pick until it proves incremental out-of-sample IC. Deterministic scoring (fixed projection basis),
bounded (clouds ≤ _MAX_CLOUD, library ≤ _MAX_PROTOS, ≤ _N_RECORD samples recorded/cycle, sliced over
_N_SLICES dirs), abstains safely. Budget: a few sort-based 1-D Wasserstein projections over ≤500
symbols once per Universe-Core cycle — milliseconds.
"""
from __future__ import annotations

import json
import time

import numpy as np
import structlog

from .. import keys as K
from ..contracts import ModuleOutput, UniverseFrame
from .base import UniverseModule

log = structlog.get_logger()

_MIN_S = 20           # need ≥ this many symbols for a cross-sectional cloud
_N_SLICES = 24        # sliced-Wasserstein random projections (deterministic basis, fixed seed)
_PROJ_SEED = 20260611 # FIXED → the projection basis is identical every cycle (determinism, Rule 9)
_MAX_PROTOS = 8       # regime-prototype library cap
_PROTO_ROWS = 64      # rows kept per regime prototype (subsampled cloud)
_NOVELTY_Z = 1.5      # admit a new regime prototype when min sliced-dist > median + _NOVELTY_Z·MAD
_MAX_CLOUD = 256      # winner/loser FIFO cloud cap (each)
_MIN_CLOUD = 40       # both clouds need ≥ this many labelled samples before per-symbol votes turn on
_N_RECORD = 48        # symbols recorded into the pending ledger per cycle (bounds steady-state pending)
_MAX_PENDING = 4000   # pending ZSET safety cap
_SETTLE_BATCH = 256   # matured samples folded per cycle
_OUTCOME_TAU = 0.004  # |fwd log-ret| band: > +τ ⇒ winner, < −τ ⇒ loser, else ambiguous (discarded)
_DIR_SCALE = 0.75     # tanh scale on the (prox_l − prox_w) proximity gap → direction
_PRICE_TF = "5m"      # settlement price TF (matches ic_tracker)
_HORIZON_MIN = 30     # forward horizon a recorded sample is graded against
_PRICE_CANDLES = "{sym}:" + _PRICE_TF + ":candles"


# ───────────────────────── feature standardization ─────────────────────────
def _standardize(feats: np.ndarray) -> np.ndarray:
    """Column-wise (per-feature) cross-sectional z-score so the cloud is comparable across regimes
    (absolute return/vol levels drift; the SHAPE of the cross-section is what transports)."""
    mu = feats.mean(axis=0)
    sd = feats.std(axis=0)
    sd = np.where(sd > 1e-12, sd, 1.0)
    x = (feats - mu) / sd
    return np.clip(np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0), -6.0, 6.0)


def _projections(n_features: int) -> np.ndarray:
    """(_N_SLICES, F) fixed random unit directions for sliced-Wasserstein. Seeded → deterministic."""
    rng = np.random.default_rng(_PROJ_SEED)
    p = rng.standard_normal((_N_SLICES, n_features))
    p /= (np.linalg.norm(p, axis=1, keepdims=True) + 1e-12)
    return p


def _sliced_w1(a: np.ndarray, b: np.ndarray, proj: np.ndarray) -> float:
    """Sliced 1-Wasserstein distance between point clouds a (Na×F) and b (Nb×F): mean over random
    1-D projections of the 1-D W1 (∫|F_a^-1 − F_b^-1|), computed via sorted quantile interpolation
    onto a common grid. Both clouds must be non-empty."""
    if a.shape[0] == 0 or b.shape[0] == 0:
        return 0.0
    q = np.linspace(0.0, 1.0, 33)
    tot = 0.0
    for d in proj:
        pa = np.sort(a @ d)
        pb = np.sort(b @ d)
        qa = np.quantile(pa, q)
        qb = np.quantile(pb, q)
        tot += float(np.abs(qa - qb).mean())
    return tot / proj.shape[0]


def _sliced_point_to_cloud(x: np.ndarray, cloud: np.ndarray, proj: np.ndarray) -> float:
    """Sliced barycentric distance of a single point x (F,) to a cloud (N×F): mean over projections
    of |proj·x − median(proj·cloud)| (the 1-D W1 of a Dirac at x to the cloud's projected barycentre).
    This is the honest per-point analogue of the cloud-to-cloud sliced-Wasserstein above — it is a
    barycentric projection onto the OT-learned cloud, NOT a full distribution-to-distribution W."""
    if cloud.shape[0] == 0:
        return float("nan")
    px = proj @ x                       # (_N_SLICES,)
    pc = proj @ cloud.T                 # (_N_SLICES, N)
    centre = np.median(pc, axis=1)      # (_N_SLICES,)
    return float(np.abs(px - centre).mean())


# ───────────────────────── Redis state (bounded, double-count-proof) ─────────────────────────
def _load_cloud(r, key: str) -> np.ndarray:
    try:
        raw = r.get(key)
        if not raw:
            return np.empty((0, 0))
        arr = np.asarray(json.loads(raw), dtype=float)
        return arr if arr.ndim == 2 and arr.shape[0] > 0 else np.empty((0, 0))
    except Exception:
        return np.empty((0, 0))


def _append_fifo(r, key: str, vecs: list[list[float]]) -> None:
    """Append labelled vectors to a FIFO cloud, capped at _MAX_CLOUD (drop oldest)."""
    if not vecs:
        return
    try:
        existing = r.get(key)
        cur = json.loads(existing) if existing else []
        if not isinstance(cur, list):
            cur = []
        cur.extend(vecs)
        if len(cur) > _MAX_CLOUD:
            cur = cur[-_MAX_CLOUD:]
        r.setex(key, K.HOT_TTL * 8, json.dumps(cur, separators=(",", ":")))
    except Exception as exc:
        log.debug("ot_cloud_append_failed", key=key, error=str(exc)[:120])


def _latest_close(r, sym: str) -> float | None:
    try:
        raw = r.lrange(_PRICE_CANDLES.replace("{sym}", sym), 0, 0)
        if not raw:
            return None
        c = float(json.loads(raw[0])["c"])
        return c if c > 0 else None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, Exception):
        return None


def _settle(r, now: float) -> int:
    """Fold every matured pending sample into the winner/loser FIFO clouds by its realized forward
    return. Each member is ZREM'd as consumed (crash loses, never double-counts). Returns # settled."""
    try:
        matured = r.zrangebyscore(K.OT_PENDING, "-inf", now, start=0, num=_SETTLE_BATCH)
    except Exception:
        return 0
    if not matured:
        return 0
    win_add: list[list[float]] = []
    lose_add: list[list[float]] = []
    settled = 0
    for member in matured:
        try:
            obs = json.loads(member)
            sym, ref, x = obs["s"], float(obs["p"]), obs["x"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            try: r.zrem(K.OT_PENDING, member)
            except Exception: pass
            continue
        cur = _latest_close(r, sym) if (sym and ref > 0) else None
        try:
            r.zrem(K.OT_PENDING, member)
        except Exception:
            pass
        if cur is None or ref <= 0:
            continue                                # price gone → discard, don't poison the cloud
        fwd_ret = float(np.log(cur / ref))
        if not np.isfinite(fwd_ret):
            continue
        if fwd_ret > _OUTCOME_TAU:
            win_add.append(x)
        elif fwd_ret < -_OUTCOME_TAU:
            lose_add.append(x)
        # |fwd_ret| ≤ τ → ambiguous, intentionally dropped
        settled += 1
    _append_fifo(r, K.OT_CLOUD_WINNER, win_add)
    _append_fifo(r, K.OT_CLOUD_LOSER, lose_add)
    if settled:
        try:
            r.incrby(K.OT_SETTLED_TOTAL, settled)
        except Exception:
            pass
    return settled


def _record_pending(r, symbols, x_std, ref_prices, rank, now: float) -> None:
    """Record the _N_RECORD most cross-sectionally-extreme symbols this cycle (deterministic by
    |feature norm|) into the pending ledger to mature into winner/loser labels in _HORIZON_MIN."""
    if not symbols:
        return
    order = np.argsort(-rank)[:_N_RECORD]           # largest displacement first (most informative)
    maturity = now + _HORIZON_MIN * 60
    try:
        pipe = r.pipeline(transaction=False)
        for j in order:
            ref = ref_prices.get(symbols[j])
            if ref is None or ref <= 0:
                continue
            member = json.dumps({"s": symbols[j], "p": round(float(ref), 10),
                                 "x": [round(float(v), 5) for v in x_std[j]],
                                 "t": round(now, 2)}, separators=(",", ":"))
            pipe.zadd(K.OT_PENDING, {member: maturity})
        pipe.zremrangebyrank(K.OT_PENDING, 0, -(_MAX_PENDING + 1))
        pipe.execute()
    except Exception as exc:
        log.debug("ot_record_pending_failed", error=str(exc)[:120])


def _update_regime(r, x_std: np.ndarray, proj: np.ndarray, now: float) -> dict:
    """Compare the current cloud to the regime library by sliced-Wasserstein; pick the nearest,
    compute migration velocity vs the previous cycle, admit a novel cloud as a new prototype
    (capped). Returns a regime read dict (also persisted for the dashboard)."""
    try:
        raw = r.get(K.OT_REGIME_PROTOS)
        protos = json.loads(raw) if raw else []
        if not isinstance(protos, list):
            protos = []
    except Exception:
        protos = []

    # subsample the current cloud to a fixed prototype size (deterministic stride)
    n = x_std.shape[0]
    if n > _PROTO_ROWS:
        idx = np.linspace(0, n - 1, _PROTO_ROWS).astype(int)
        cur_cloud = x_std[idx]
    else:
        cur_cloud = x_std

    dists = [_sliced_w1(cur_cloud, np.asarray(p, dtype=float), proj) for p in protos]
    if dists:
        nearest = int(np.argmin(dists))
        min_dist = float(dists[nearest])
    else:
        nearest, min_dist = -1, float("inf")

    # novelty: admit a new prototype when the library is empty OR the current cloud is far from all
    admit = False
    if not protos:
        admit = True
    elif len(dists) >= 2:
        med = float(np.median(dists))
        mad = float(np.median(np.abs(np.asarray(dists) - med))) + 1e-9
        if min_dist > med + _NOVELTY_Z * mad:
            admit = True
    if admit and len(protos) < _MAX_PROTOS:
        protos.append([[round(float(v), 5) for v in row] for row in cur_cloud])
        try:
            r.setex(K.OT_REGIME_PROTOS, K.HOT_TTL * 24, json.dumps(protos, separators=(",", ":")))
        except Exception:
            pass
        nearest = len(protos) - 1
        if min_dist == float("inf"):
            min_dist = 0.0

    # migration velocity vs the previous cycle's nearest distance
    prev_min = None
    try:
        st = r.get(K.OT_STATE)
        if st:
            prev_min = json.loads(st).get("min_dist")
    except Exception:
        prev_min = None
    migration = abs(min_dist - float(prev_min)) if (prev_min is not None
                 and np.isfinite(min_dist)) else 0.0
    # regime_stability ∈ (0,1]: 1 when not migrating, →0 when migrating fast (conviction damper)
    stability = 1.0 / (1.0 + 4.0 * migration)

    read = {
        "regime_id": int(nearest),
        "n_protos": len(protos),
        "min_dist": (None if not np.isfinite(min_dist) else round(min_dist, 5)),
        "migration_velocity": round(float(migration), 5),
        "stability": round(float(stability), 4),
        "admitted_new": bool(admit),
        "ts": round(now, 2),
    }
    try:
        r.setex(K.OT_STATE, K.HOT_TTL, json.dumps(read, separators=(",", ":")))
    except Exception:
        pass
    return read


def _score(symbols, x_std, winner, loser, proj, regime, ts) -> dict:
    """PURE, deterministic: per-symbol winner/loser OT-proximity vote. Abstains for every symbol
    until both clouds are mature. Returns {sym: ModuleOutput}."""
    stability = float(regime.get("stability", 1.0))
    cold = (winner.shape[0] < _MIN_CLOUD or loser.shape[0] < _MIN_CLOUD)
    out: dict[str, ModuleOutput] = {}
    # cloud_maturity ∈ (0,1]: ramps conviction up as the clouds fill
    maturity = min(1.0, min(
        (winner.shape[0] if winner.size else 0),
        (loser.shape[0] if loser.size else 0)) / float(_MAX_CLOUD))
    for j, sym in enumerate(symbols):
        if cold:
            out[sym] = ModuleOutput.abstain(
                "optimal_transport_regime", "clouds_cold_start", _HORIZON_MIN,
                role="context", evidence_family="distribution_transport", shadow_only=True)
            continue
        x = x_std[j]
        prox_w = _sliced_point_to_cloud(x, winner, proj)
        prox_l = _sliced_point_to_cloud(x, loser, proj)
        if not (np.isfinite(prox_w) and np.isfinite(prox_l)):
            out[sym] = ModuleOutput.abstain(
                "optimal_transport_regime", "proximity_undefined", _HORIZON_MIN,
                role="context", evidence_family="distribution_transport", shadow_only=True)
            continue
        gap = prox_l - prox_w                                   # >0 ⇒ closer to winners ⇒ long
        direction = float(np.tanh(gap / _DIR_SCALE))
        conviction = float(min(abs(gap) / _DIR_SCALE, 1.0) * maturity * stability)
        stance = "long" if direction >= 0 else "short"
        out[sym] = ModuleOutput(
            module="optimal_transport_regime",
            direction=direction,
            conviction=conviction,
            expected_move_pct=None,
            horizon_min=_HORIZON_MIN,
            regime_tag=f"ot_regime_{regime.get('regime_id', -1)}",
            features={
                "prox_winner": round(prox_w, 5),
                "prox_loser": round(prox_l, 5),
                "proximity_gap": round(gap, 5),
                "regime_id": int(regime.get("regime_id", -1)),
                "migration_velocity": regime.get("migration_velocity", 0.0),
                "regime_stability": round(stability, 4),
                "cloud_maturity": round(maturity, 4),
                "n_winner": int(winner.shape[0]),
                "n_loser": int(loser.shape[0]),
            },
            explanation=(f"optimal_transport_regime: OT-proximity gap {gap:+.3f} "
                         f"(d_loser {prox_l:.3f} − d_winner {prox_w:.3f}) → {stance}; "
                         f"regime {regime.get('regime_id', -1)} stability {stability:.2f}"),
            ok=True,
            role="context",
            evidence_family="distribution_transport",
            shadow_only=True,
            ts=ts,
        )
    return out


class OptimalTransportRegimeModule(UniverseModule):
    name = "optimal_transport_regime"
    horizon_min = _HORIZON_MIN

    def _compute(self, frame: UniverseFrame) -> dict[str, ModuleOutput]:
        symbols = frame.symbols
        feats = frame.feature_matrix
        if feats is None or feats.ndim != 2 or feats.shape[0] < _MIN_S:
            return {}                                   # too-thin cross-section → whole module abstains

        import redis_client
        try:
            r = redis_client.get()
        except Exception:
            return {}

        now = time.time()
        x_std = _standardize(feats)                     # (S, F) column-standardized cloud
        proj = _projections(x_std.shape[1])

        # 1) settle matured outcomes into the winner/loser clouds (state op)
        try:
            _settle(r, now)
        except Exception as exc:
            log.debug("ot_settle_failed", error=str(exc)[:120])

        # 2) regime / migration read (state op + market-level context)
        regime = _update_regime(r, x_std, proj, now)

        # 3) per-symbol winner/loser OT-proximity vote (PURE, deterministic)
        winner = _load_cloud(r, K.OT_CLOUD_WINNER)
        loser = _load_cloud(r, K.OT_CLOUD_LOSER)
        out = _score(symbols, x_std, winner, loser, proj, regime, frame.ts)

        # 4) record the most-informative symbols this cycle to mature into future labels (state op)
        try:
            ref_prices = {s: _latest_close(r, s) for s in symbols}
            rank = np.linalg.norm(x_std, axis=1)        # cross-sectional displacement magnitude
            _record_pending(r, symbols, x_std, ref_prices, rank, now)
        except Exception as exc:
            log.debug("ot_record_failed", error=str(exc)[:120])

        return out
