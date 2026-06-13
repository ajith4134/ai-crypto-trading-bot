"""
Deterministic verdict scorer — cont. 69 (2026-06-02), REDESIGNED cont. 72 (2026-06-07).

The SYNCHRONOUS hot-path replacement for the F37 LLM debate. On a CPU-only box
phi3 runs ~3.4-4.2 tok/s, so a synchronous LLM debate (3 agents × up to 3 rounds)
either times out (→ "degraded" → trades blocked) or burns cloud quota. User
decision (cont. 69, Option 1): the real-time accept/size verdict comes from THIS
instant feature_vector-based scorer.

────────────────────────────────────────────────────────────────────────────────
cont. 72 redesign — WHY (ground-truth, Rule 13):
A verdict×realized-PnL JOIN over closed trades showed the OLD scorer was INVERTED:
  full_allocation  -> -$0.500 avg / 46.9% WR  (LOSES, on FULL size)
  reduced_allocation-> +$0.093 avg / 54.6% WR  (WINS, on 70% size)
The win-rate gap is size-independent → full_allocation genuinely selected the WORST
trades. Cause: the old scorer anchored `base = signal_strength` then ADDED regime/
candlenet/ofi bonuses — but Cont.71 already baked those SAME features into
signal_strength. So `full` just meant "high signal_strength", and high-confluence
trades are the crowded losers (audit: bull+long / bear+short crowding).

THE NEW DESIGN — the debate is an ORTHOGONAL risk/crowding/outcome overlay.
It scores ONLY factors that are NOT already inside signal_strength:
  * realized regime:direction PnL prior  (debate.learning, EWMA of net_pnl/capital) — PRIMARY
  * funding-rate crowd positioning        (contrarian)
  * volatility regime                     (vol_unit / ATR)
  * cross-sectional momentum tailwind     (xsmom_rank, when populated)
  * global recent loss-streak             (bot-wide risk-off)
  * guarded cascade/vpin penalties        (rare, fail-safe)
base = 50 (neutral). The all-neutral default → reduced_allocation, which is the
MEASURED winner. full_allocation now REQUIRES a POSITIVE realized prior + a favourable
risk context; skip_risk fires only on strong NEGATIVE confluence. This breaks the
"high signal_strength → full size → loser" mechanism at the root.

Design invariants kept from cont. 69:
  * NEVER block a trade on infrastructure — pure Python; every external read is
    Redis-only, fail-open to 0 on any error.
  * INTERPRETABLE — every verdict carries component scores + reasons.

Verdict vocabulary preserved for signals/engine.py (debate_applied block):
  full_allocation (mult 1.0) | reduced_allocation (0.7) | exploratory (0.05) |
  skip_risk (rejected). 'skip' is not emitted here.
"""
import json
import time

import structlog

import redis_client

log = structlog.get_logger()

# ── Orthogonal-overlay weights (composite anchored at NEUTRAL 50) ────────────
# NB: deliberately NO signal_strength / regime-aligned / candlenet / ofi terms —
# those live inside signal_strength already (Cont.71). Re-adding them is the
# cont.72 inversion bug. Every term below is INDEPENDENT of signal_strength.
_BASE = 50.0

_W_PRIOR = 15.0              # realized regime:dir PnL prior ∈[-1,1] → ±15 (PRIMARY)
_P_FUNDING_CAP = 8.0         # graded funding crowd prior (±points)

# Volatility regime (vol_unit ≈ ATR/mark; live avg ~0.030, p90 ~0.065, p10 ~0.009)
_VOL_HI = 0.060             # high vol → risk-off
_VOL_MID = 0.045
_VOL_LO = 0.012            # unusually calm → mild tailwind
_P_VOL_HI = 10.0
_P_VOL_MID = 5.0
_W_VOL_LO = 4.0

_W_XSMOM = 4.0             # cross-sectional momentum tailwind (when populated)

_W_LOSS_STREAK = 3.0       # per consecutive bot-wide loss
_LOSS_STREAK_CAP = 12.0

# Guarded risk penalties (rarely fire — liq_cascade_prob is 0-fill today, kept
# fail-safe in case the producer comes online).
_P_CASCADE = 18.0
_P_CASCADE_MID = 8.0
_CASCADE_HI = 0.50
_CASCADE_MID = 0.35
_P_VPIN = 6.0
_VPIN_HI = 0.60

# ── Verdict thresholds on the composite score ────────────────────────────────
# Default (all-neutral) = 50 → reduced_allocation (the MEASURED winner).
_TH_FULL = 60.0            # needs a positive realized prior + favourable context
_TH_REDUCED = 42.0
_TH_EXPLORATORY = 30.0
# below _TH_EXPLORATORY → skip_risk


def _parse_fv(signal: dict) -> dict:
    """feature_vector is stored as a JSON string on the signal. Parse defensively."""
    raw = signal.get("feature_vector")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes)):
        try:
            d = json.loads(raw)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}
    return {}


def _g(fv: dict, key: str, default: float = 0.0) -> float:
    try:
        v = fv.get(key, default)
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _learned_prior_delta(cluster_id, regime: str, direction: str) -> float:
    """REALIZED-outcome prior written by debate.learning at every trade close
    (EWMA of net_pnl/capital ∈[-1,1] per regime:direction). This is the council's
    PRIMARY contribution — it learns which regime×direction combos actually pay
    (e.g. bear:short avg -$0.84 → negative prior → fewer/smaller bear:shorts).

    Two tiers: cluster-specific first (currently always empty — pattern_cluster_id
    is NULL in the DB) then the regime:direction tier (the live signal). Fail-open
    to 0 so the hot path never depends on it. ±_W_PRIOR score points."""
    try:
        r = redis_client.get()
        val = None
        if cluster_id is not None:
            try:
                raw = r.get(f"debate:prior:{int(float(cluster_id))}:{regime}:{direction}")
                if raw is not None:
                    val = float(raw)
            except (TypeError, ValueError):
                val = None
        if val is None:
            raw = r.get(f"debate:prior:regime:{regime}:{direction}")
            if raw is not None:
                val = float(raw)
        if val is None:
            return 0.0
        return max(-1.0, min(1.0, val)) * _W_PRIOR
    except Exception:
        return 0.0


def _loss_streak_penalty() -> float:
    """Bot-wide risk-off: count consecutive recent LOSSES from the
    `debate:recent_outcomes` list (most-recent-first '1'=win / '0'=loss, maintained
    by debate.learning at close). Each leading loss costs _W_LOSS_STREAK, capped.
    Negative number (penalty). Fail-open to 0."""
    try:
        r = redis_client.get()
        out = r.lrange("debate:recent_outcomes", 0, 19) or []
        streak = 0
        for v in out:
            s = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
            if s == "0":
                streak += 1
            else:
                break
        return -min(_LOSS_STREAK_CAP, streak * _W_LOSS_STREAK)
    except Exception:
        return 0.0


def _volatility_delta(fv: dict) -> float:
    """Volatility regime from vol_unit (ATR/mark). High vol → risk-off penalty;
    unusually calm → small tailwind. Independent of signal_strength."""
    v = _g(fv, "vol_unit")
    if v <= 0:
        return 0.0
    if v > _VOL_HI:
        return -_P_VOL_HI
    if v > _VOL_MID:
        return -_P_VOL_MID
    if v < _VOL_LO:
        return _W_VOL_LO
    return 0.0


def _xsmom_delta(fv: dict, direction: str) -> float:
    """Cross-sectional momentum tailwind: longs ride high-rank names, shorts ride
    low-rank. Only contributes when xsmom_rank is populated (≠0, ≠0.5)."""
    xs = _g(fv, "xsmom_rank", 0.5)
    if xs in (0.0, 0.5):
        return 0.0
    if direction == "long" and xs >= 0.7:
        return _W_XSMOM
    if direction == "short" and 0.0 < xs <= 0.3:
        return _W_XSMOM
    return 0.0


def _kline_prior_delta(pair: str, direction: str) -> float:
    """cont. 69j — soft directional prior from the kline-trained predict-all model
    (published live to `predict:kline:{pair}`). LOW-EDGE (val_auc ~0.54), so it only
    NUDGES: signed = (p_up-0.5) for long / (0.5-p_up) for short, × small K, capped.
    Independent of signal_strength. Toggle `entry:kline_prior_enabled` (default on);
    weight `entry:kline_prior_k` (default 20). Fail-open to 0."""
    try:
        r = redis_client.get()
        if (r.get("entry:kline_prior_enabled") or "1") != "1":
            return 0.0
        raw = r.get(f"predict:kline:{pair}")
        if not raw:
            return 0.0
        p_up = float(json.loads(raw).get("p_up"))
        k = float(r.get("entry:kline_prior_k") or 20.0)
        signed = (p_up - 0.5) if direction == "long" else (0.5 - p_up)
        cap = 6.0
        return max(-cap, min(cap, signed * k))
    except Exception:
        return 0.0


def _funding_prior_delta(fv: dict, direction: str) -> float:
    """cont. 69q — GRADED crowd-positioning prior from funding rate (the blueprint's
    positioning proxy, populated on ~99% of live signals). Contrarian + two-sided:
    funding > 0 = crowded LONG → penalise new longs, mildly favour shorts; funding < 0
    = crowded short → vice versa. Graded by magnitude, capped to ±_P_FUNDING_CAP.
    Independent of signal_strength. Toggle `entry:funding_prior_enabled` (default on);
    weight `entry:funding_prior_k` (default 4000). Fail-open to 0."""
    try:
        r = redis_client.get()
        if (r.get("entry:funding_prior_enabled") or "1") != "1":
            return 0.0
        funding = _g(fv, "funding_rate")
        if funding == 0.0:
            return 0.0
        k = float(r.get("entry:funding_prior_k") or 4000.0)
        signed = -funding if direction == "long" else funding
        return max(-_P_FUNDING_CAP, min(_P_FUNDING_CAP, signed * k))
    except Exception:
        return 0.0


def deterministic_verdict(signal: dict, market: dict,
                          capital_pct: float, balance: float = 0.0) -> dict:
    """Return a debate-compatible result dict computed instantly from the
    feature_vector + Redis-cached realized priors. Shape mirrors
    debate.council.run_debate so signals/engine.py and persistence treat it
    identically (llm_available=False → LLM args not stored).

    cont. 72: the score is an ORTHOGONAL overlay anchored at NEUTRAL 50, using only
    factors NOT already in signal_strength. See module docstring for the rationale."""
    direction = str(signal.get("direction") or "long").lower()
    fv = _parse_fv(signal)
    reasons: list[str] = []

    score = _BASE
    base = _BASE

    # ── Regime label (for the realized prior key + logging) ──────────────────
    bull = _g(fv, "regime_bull") >= 0.5
    bear = _g(fv, "regime_bear") >= 0.5
    turbulent = _g(fv, "regime_turbulent") >= 0.5
    regime = (market or {}).get("regime") or (
        "bull" if bull else "bear" if bear else "turbulent" if turbulent else "unknown")

    # ── Realized regime:direction PnL prior (PRIMARY contribution) ───────────
    prior = _learned_prior_delta(fv.get("pattern_cluster_id"), regime, direction)
    if prior:
        score += prior
        reasons.append(f"realized_prior({prior:+.1f})")

    # ── Funding crowd positioning (contrarian) ───────────────────────────────
    fprior = _funding_prior_delta(fv, direction)
    if fprior:
        score += fprior
        reasons.append(f"funding({fprior:+.1f})")

    # ── Volatility regime ────────────────────────────────────────────────────
    vdelta = _volatility_delta(fv)
    if vdelta:
        score += vdelta
        reasons.append(f"vol({vdelta:+.0f})")

    # ── Cross-sectional momentum tailwind ────────────────────────────────────
    xdelta = _xsmom_delta(fv, direction)
    if xdelta:
        score += xdelta
        reasons.append(f"xsmom(+{xdelta:.0f})")

    # ── Bot-wide loss-streak (risk-off) ──────────────────────────────────────
    lstreak = _loss_streak_penalty()
    if lstreak:
        score += lstreak
        reasons.append(f"loss_streak({lstreak:+.0f})")

    # ── Kline predict-all soft prior (independent low-edge model) ────────────
    kprior = _kline_prior_delta(signal.get("pair"), direction)
    if kprior:
        score += kprior
        reasons.append(f"kline({kprior:+.1f})")

    # ── Guarded risk penalties (rare — fail-safe if producers come online) ───
    cascade = _g(fv, "liq_cascade_prob")
    if cascade > _CASCADE_HI:
        score -= _P_CASCADE
        reasons.append(f"cascade(-{_P_CASCADE:.0f})")
    elif cascade > _CASCADE_MID:
        score -= _P_CASCADE_MID
        reasons.append(f"cascade(-{_P_CASCADE_MID:.0f})")
    vpin = _g(fv, "vpin")
    if vpin > _VPIN_HI:
        score -= _P_VPIN
        reasons.append(f"toxic_flow(-{_P_VPIN:.0f})")

    score = max(0.0, min(100.0, score))

    # ── Map composite → verdict + size ───────────────────────────────────────
    if score >= _TH_FULL:
        verdict, size_pct = "full_allocation", capital_pct
    elif score >= _TH_REDUCED:
        verdict, size_pct = "reduced_allocation", capital_pct * 0.7
    elif score >= _TH_EXPLORATORY:
        verdict, size_pct = "exploratory", capital_pct * 0.05
    else:
        verdict, size_pct = "skip_risk", 0.0

    # Silent-rejection rule (Rule 12): every verdict path emits a counter + log.
    try:
        r = redis_client.get()
        r.incr(f"debate:det:verdict:{verdict}")
        r.set("debate:det:last_ts", int(time.time()))
    except Exception:
        pass
    log.info("debate_deterministic", pair=signal.get("pair"), direction=direction,
             verdict=verdict, score=round(score, 1), base=round(base, 1),
             regime=regime, prior=round(prior, 1), reasons=reasons)

    return {
        "verdict": verdict,
        "size_pct": round(size_pct, 2),
        "rounds_used": 0,
        "rounds": {},
        "weights": {},
        "source": "deterministic",
        "score": round(score, 1),
        "reasons": reasons,
        # Back-compat agent fields some consumers read; synthesised, not LLM.
        "bull": {"argue_for": verdict in ("full_allocation", "reduced_allocation"),
                 "confidence": int(round(score))},
        "bear": {"argue_against": verdict == "skip_risk",
                 "risk_score": int(round(100 - score))},
        "risk": {"risk_acceptable": verdict != "skip_risk",
                 "recommended_size_pct": round(size_pct, 2)},
        "llm_available": False,   # → save_debate_arguments skips DB persistence
    }
