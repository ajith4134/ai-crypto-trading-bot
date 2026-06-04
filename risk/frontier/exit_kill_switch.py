"""cont. 60 — Portfolio-level kill switches.

  * GNN cross-asset contagion early warning: when the GNN contagion score
    crosses 0.80 on the portfolio graph, flatten all leveraged positions
    before the cascade lands.

The existing ml.gnn_multiscale.get_contagion_score is used at signal-emit
time for entries. We extend it as an EXIT signal too.
"""
from __future__ import annotations
from typing import Optional
import structlog

from .decision import ExitDecision

log = structlog.get_logger()


# ─────────────────────────────────────────────────────────────────────────
# 12. GNN cross-asset contagion kill switch
# ─────────────────────────────────────────────────────────────────────────
def evaluate_gnn_contagion_kill(trade, mark, r, sl_level, direction) -> Optional[ExitDecision]:
    """Force-exit if GNN contagion score > 0.80.

    Reads:
      `gnn:contagion_score`        — portfolio-level cascade likelihood (0-1)
      `gnn:contagion_threshold`    — adaptive threshold (default 0.80)

    Catches Oct-10-11 2025-type cascades ($19B liquidated in 24h) before the
    second wave by flattening all positions when the GNN detects systemic
    correlation spike.

    Source: SagePub Dynamic GNN Crisis Modeling (2025) — 92.8% risk alert
    accuracy on BTC/ETH/SOL/stablecoin graphs.
    """
    # Read per-pair contagion from existing ml.gnn_multiscale producer.
    pair = trade["pair"]
    score_raw = r.get(f"multiscale_gnn:contagion:{pair}")
    if score_raw is None:
        # Fallback to global score if per-pair missing.
        score_raw = r.get("gnn:contagion_score")
    if score_raw is None:
        return None
    try:
        score = float(score_raw)
    except (TypeError, ValueError):
        return None
    thresh_raw = r.get("gnn:contagion_threshold")
    try:
        thresh = float(thresh_raw) if thresh_raw is not None else 0.80
    except (TypeError, ValueError):
        thresh = 0.80
    if score < thresh:
        return None
    try:
        r.incr("trail:gnn_contagion_kill_count")
        r.set("trail:gnn_last_score", round(score, 4))
    except Exception:
        pass
    return ExitDecision(force_close=True,
                        reason="gnn_contagion_systemic_risk",
                        notes={"contagion_score": score, "threshold": thresh})


# ─────────────────────────────────────────────────────────────────────────
# 13. Real-time liquidation-cascade kill switch (cont. 69x item 2)
# ─────────────────────────────────────────────────────────────────────────
def evaluate_liq_cascade_kill(trade, mark, r, sl_level, direction) -> Optional[ExitDecision]:
    """Exit/tighten when live !forceOrder@arr flow shows a liquidation cascade
    running AGAINST the held position (data/liq_ws.py producer).

    Adverse = we're LONG while longs are being force-sold (flow "short"), or
    we're SHORT while shorts are being force-bought (flow "long"). When the
    per-pair flow intensity is extreme AND the market-wide liquidation rate is
    elevated (systemic), force-close ahead of the deepening cascade; at moderate
    intensity, tighten the trail instead.

    Reads:
      {pair}:liq_flow_dir        long|short|neutral
      {pair}:liq_flow_intensity  [0,1]
      liq:global_rate            market-wide USD/min liquidation rate
    Levers:
      liq:exit_force_intensity    default 0.80
      liq:exit_tighten_intensity  default 0.40
      liq:exit_global_min_usd     default 5e7 (systemic gate for a force-close)
    """
    pair = trade["pair"]
    d_raw = r.get(f"{pair}:liq_flow_dir")
    if d_raw is None:
        return None
    flow = d_raw.decode() if isinstance(d_raw, bytes) else d_raw
    if flow == "neutral":
        return None
    adverse = (direction == "long" and flow == "short") or \
              (direction == "short" and flow == "long")
    if not adverse:
        return None
    try:
        inten = float(r.get(f"{pair}:liq_flow_intensity") or 0)
    except (TypeError, ValueError):
        return None

    def _lever(key: str, dflt: float) -> float:
        try:
            v = r.get(key)
            return float(v) if v is not None else dflt
        except (TypeError, ValueError):
            return dflt

    force_th = _lever("liq:exit_force_intensity", 0.80)
    tighten_th = _lever("liq:exit_tighten_intensity", 0.40)
    global_min = _lever("liq:exit_global_min_usd", 5e7)
    try:
        global_rate = float(r.get("liq:global_rate") or 0)
    except (TypeError, ValueError):
        global_rate = 0.0

    if inten >= force_th and global_rate >= global_min:
        try:
            r.incr("trail:liq_cascade_kill_count")
            r.set("trail:liq_cascade_last_pair", pair)
        except Exception:
            pass
        return ExitDecision(force_close=True,
                            reason="liq_cascade_adverse",
                            notes={"flow": flow, "intensity": inten,
                                   "global_rate": global_rate})
    if inten >= tighten_th:
        try:
            r.incr("trail:liq_cascade_tighten_count")
        except Exception:
            pass
        return ExitDecision(tighten_sl_mult=0.5,
                            reason="liq_cascade_tighten",
                            notes={"flow": flow, "intensity": inten})
    return None
