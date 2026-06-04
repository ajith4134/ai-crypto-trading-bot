"""
Deterministic verdict scorer — cont. 69 (2026-06-02).

The SYNCHRONOUS hot-path replacement for the F37 LLM debate. On a CPU-only box
phi3 runs ~3.4-4.2 tok/s, so a synchronous LLM debate (3 agents × up to 3 rounds)
either times out (→ "degraded" → trades blocked) or burns cloud quota. User
decision (cont. 69, Option 1): the real-time accept/size verdict comes from THIS
instant feature_vector-based scorer; the LLM debate runs ASYNC purely for belief/
weight learning (debate.council.run_debate, unchanged signature, called off-path).

Design goals:
  * NEVER block a trade on infrastructure. Pure Python, no I/O on the hot value
    (one optional Redis read for the learned prior, fail-open on error).
  * PERMISSIVE by construction. Signals reaching here already passed
    accept_or_reject (strength ≥ the min_signal_strength ceiling). So the default
    is full/reduced allocation; `skip_risk` fires only on strong NEGATIVE
    confluence (counter-regime + cascade risk + candlenet disagreement). This
    avoids re-creating the zero-trades incident the gate caused.
  * INTERPRETABLE. Every verdict carries the component scores + reasons so the
    dashboard and postmortems can see exactly why.

Verdict vocabulary preserved for signals/engine.py (debate_applied block):
  full_allocation (mult 1.0) | reduced_allocation (0.7) | exploratory (0.05) |
  skip_risk (rejected). 'skip' is not emitted here (that's a no-trade-direction
  call the upstream gates already make).
"""
import json
import time

import structlog

import redis_client

log = structlog.get_logger()

# ── Scoring weights (composite 0-100, anchored on signal_strength) ───────────
_W_REGIME_ALIGNED = 8.0       # long&bull or short&bear
_W_REGIME_OPPOSED = -12.0     # long&bear or short&bull
_W_REGIME_TURBULENT = -6.0    # turbulent regime is risk-off for either side
_W_CN_PER_TF = 4.0           # each candlenet TF agreeing with direction
_W_FLOW = 3.0                # ofi / bid_ask_imbalance aligned
_W_XSMOM = 4.0               # cross-sectional momentum rank tailwind
# Risk penalties
_P_CASCADE = 10.0            # liq_cascade_prob > _CASCADE_HI
_P_VPIN = 6.0               # toxic flow
_P_FUNDING = 5.0            # crowded funding against the trade
_CASCADE_HI = 0.50
_VPIN_HI = 0.60
_FUNDING_HI = 0.0005         # 0.05% per interval ~ crowded (legacy binary threshold)
_P_FUNDING_CAP = 8.0         # cont. 69q — graded funding prior cap (±points)

# ── Verdict thresholds on the composite score ────────────────────────────────
_TH_FULL = 60.0
_TH_REDUCED = 46.0
_TH_EXPLORATORY = 34.0
# below _TH_EXPLORATORY → skip_risk

_CANDLENET_TFS = ("cn_1m_dir1", "cn_5m_dir3", "cn_15m_dir3", "cn_1h_dir3")


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
    """Optional adjustment from the ASYNC LLM debate's learned cache. The async
    council writes `debate:prior:{cluster}:{regime}:{dir}` ∈ [-1,1] (its net
    verdict bias for that combo). Fail-open to 0 (no effect) on any error so the
    hot path never depends on it."""
    try:
        if cluster_id is None:
            return 0.0
        r = redis_client.get()
        key = f"debate:prior:{int(float(cluster_id))}:{regime}:{direction}"
        raw = r.get(key)
        if raw is None:
            return 0.0
        return max(-1.0, min(1.0, float(raw))) * 10.0   # ±10 score points max
    except Exception:
        return 0.0


def _kline_prior_delta(pair: str, direction: str) -> float:
    """cont. 69j (P3 finish) — soft directional prior from the kline-trained
    predict-all model (published live to `predict:kline:{pair}` by
    publish_kline_predictions_task). The model is LOW-EDGE (val_auc ~0.54), so it
    only NUDGES: signed = (p_up-0.5) for long / (0.5-p_up) for short, × small K,
    capped to ±_KLINE_PRIOR_CAP. Toggle `entry:kline_prior_enabled` (default on);
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
    """cont. 69q — GRADED crowd-positioning prior from funding rate, replacing the
    old binary `funding>HI → -5` penalty. Funding is the blueprint's positioning
    proxy and is already collected per-pair (data/feed.py → FUNDING_RATE, surfaced
    on the signal feature_vector as `funding_rate`).

    Two-sided + contrarian: funding > 0 = longs pay shorts = CROWDED LONG → penalise
    new longs, mildly favour shorts; funding < 0 = crowded short → vice versa. Graded
    by magnitude (more crowded = stronger) so a marginal funding doesn't get the same
    hit as an extreme one. Capped to ±_P_FUNDING_CAP. Toggle
    `entry:funding_prior_enabled` (default on); weight `entry:funding_prior_k`
    (default 4000 — funding ~±0.0005 at the old threshold → ±2 pts; extremes saturate
    the cap). Fail-open to 0 so the hot path never depends on it."""
    try:
        r = redis_client.get()
        if (r.get("entry:funding_prior_enabled") or "1") != "1":
            return 0.0
        funding = _g(fv, "funding_rate")
        if funding == 0.0:
            return 0.0
        k = float(r.get("entry:funding_prior_k") or 4000.0)
        # Trading WITH the crowd → negative; AGAINST the crowd → positive.
        signed = -funding if direction == "long" else funding
        return max(-_P_FUNDING_CAP, min(_P_FUNDING_CAP, signed * k))
    except Exception:
        return 0.0


def deterministic_verdict(signal: dict, market: dict,
                          capital_pct: float, balance: float = 0.0) -> dict:
    """Return a debate-compatible result dict computed instantly from the
    feature_vector. Shape mirrors debate.council.run_debate so signals/engine.py
    and persistence treat it identically (llm_available=False → args not stored)."""
    direction = str(signal.get("direction") or "long").lower()
    fv = _parse_fv(signal)
    reasons: list[str] = []

    # Anchor on the signal's own strength (fallback to fv/50).
    score = _g(fv, "signal_strength", float(signal.get("signal_strength") or 50.0))
    base = score

    # ── Regime alignment ─────────────────────────────────────────────────────
    bull = _g(fv, "regime_bull") >= 0.5
    bear = _g(fv, "regime_bear") >= 0.5
    turbulent = _g(fv, "regime_turbulent") >= 0.5
    regime = (market or {}).get("regime") or (
        "bull" if bull else "bear" if bear else "turbulent" if turbulent else "unknown")
    aligned = (direction == "long" and bull) or (direction == "short" and bear)
    opposed = (direction == "long" and bear) or (direction == "short" and bull)
    if aligned:
        score += _W_REGIME_ALIGNED; reasons.append(f"regime_aligned(+{_W_REGIME_ALIGNED:.0f})")
    if opposed:
        score += _W_REGIME_OPPOSED; reasons.append(f"regime_opposed({_W_REGIME_OPPOSED:.0f})")
    if turbulent:
        score += _W_REGIME_TURBULENT; reasons.append(f"turbulent({_W_REGIME_TURBULENT:.0f})")

    # ── CandleNet multi-TF directional agreement ─────────────────────────────
    cn_net = 0
    for tf in _CANDLENET_TFS:
        d = _g(fv, tf, 0.5)
        if d == 0.0:           # 0-fill = missing forecast, skip
            continue
        up = d > 0.5
        agree = (up and direction == "long") or ((not up) and direction == "short")
        cn_net += 1 if agree else -1
    if cn_net:
        score += _W_CN_PER_TF * cn_net
        reasons.append(f"candlenet_net({_W_CN_PER_TF * cn_net:+.0f})")

    # ── Order-flow alignment ─────────────────────────────────────────────────
    ofi = _g(fv, "ofi")
    bai = _g(fv, "bid_ask_imbalance")
    flow_sign = 1 if direction == "long" else -1
    if ofi * flow_sign > 0:
        score += _W_FLOW; reasons.append(f"ofi_aligned(+{_W_FLOW:.0f})")
    if bai * flow_sign > 0:
        score += _W_FLOW; reasons.append(f"imbalance_aligned(+{_W_FLOW:.0f})")

    # ── Cross-sectional momentum tailwind (longs ride high-rank, shorts low) ──
    xsmom = _g(fv, "xsmom_rank", 0.5)
    if direction == "long" and xsmom >= 0.7:
        score += _W_XSMOM; reasons.append(f"xsmom_high(+{_W_XSMOM:.0f})")
    elif direction == "short" and 0.0 < xsmom <= 0.3:
        score += _W_XSMOM; reasons.append(f"xsmom_low(+{_W_XSMOM:.0f})")

    # ── Risk penalties ───────────────────────────────────────────────────────
    cascade = _g(fv, "liq_cascade_prob")
    if cascade > _CASCADE_HI:
        score -= _P_CASCADE; reasons.append(f"cascade_risk(-{_P_CASCADE:.0f})")
    vpin = _g(fv, "vpin")
    if vpin > _VPIN_HI:
        score -= _P_VPIN; reasons.append(f"toxic_flow(-{_P_VPIN:.0f})")
    # cont. 69q — graded funding positioning prior (replaces the old binary penalty).
    fprior = _funding_prior_delta(fv, direction)
    if fprior:
        score += fprior; reasons.append(f"funding_prior({fprior:+.1f})")

    # ── Learned prior from async LLM debate (optional) ───────────────────────
    prior = _learned_prior_delta(fv.get("pattern_cluster_id"), regime, direction)
    if prior:
        score += prior; reasons.append(f"learned_prior({prior:+.1f})")

    # ── Kline predict-all soft prior (cont. 69j, P3 finish) ──────────────────
    kprior = _kline_prior_delta(signal.get("pair"), direction)
    if kprior:
        score += kprior; reasons.append(f"kline_prior({kprior:+.1f})")

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

    # Silent-rejection rule: every verdict path emits a counter + a log.
    try:
        r = redis_client.get()
        r.incr(f"debate:det:verdict:{verdict}")
        r.set("debate:det:last_ts", int(time.time()))
    except Exception:
        pass
    log.info("debate_deterministic", pair=signal.get("pair"), direction=direction,
             verdict=verdict, score=round(score, 1), base=round(base, 1),
             regime=regime, cn_net=cn_net, reasons=reasons)

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
