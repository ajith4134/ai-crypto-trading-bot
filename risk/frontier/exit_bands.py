"""cont. 60 — Band-driven exit features.

  * Conformal exit bands — use TCP quantile residuals to set SL/TP at the
    5%/95% predicted price levels per pair.
  * Mamba SSM multi-horizon quantiles — read the Mamba forecaster's predicted
    distribution at H=5min and use its 5%/95% as alt SL/TP.
  * Diffusion model exit distribution — sample from the generative forecast.

The existing `ml.conformal_wrapper` provides `kelly_multiplier` for entry
sizing. We extend it for exit by reading per-pair conformal quantile residuals
and converting them into absolute price bands.

Each function returns ExitDecision or None.
"""
from __future__ import annotations
from typing import Optional
import structlog

from .decision import ExitDecision

log = structlog.get_logger()


# ─────────────────────────────────────────────────────────────────────────
# 7. Temporal Conformal Prediction exit bands
# ─────────────────────────────────────────────────────────────────────────
def evaluate_conformal_bands(trade, mark, r, sl_level, direction) -> Optional[ExitDecision]:
    """Use ml.conformal_wrapper rolling residual quantile to set sl_floor.

    The conformal residuals over the last 100 ticks tell us the empirical 5%
    quantile of |actual - predicted|. The lower (long) / upper (short) SL
    floor is `mark - q5%_residual` (long) or `mark + q5%_residual` (short).
    This produces an SL that's data-driven, not ATR-multiplied.

    Reads:
      `conformal:residual_q5:{pair}` — rolling 5% quantile of residual (USDT)
    Falls back silently if the key is missing (cold start).

    Source: arXiv:2507.05470 Temporal Conformal Prediction (Jul 2025).
    """
    pair = trade["pair"]
    q5_raw = r.get(f"conformal:residual_q5:{pair}")
    if q5_raw is None:
        return None
    try:
        q5 = float(q5_raw)
    except (TypeError, ValueError):
        return None
    if q5 <= 0 or mark <= 0:
        return None
    # The conformal floor is wider than the typical Chandelier — it's an
    # ABSOLUTE floor (won't tighten the SL more than necessary, but won't
    # let it go below the conformal level either).
    if direction == "long":
        floor = mark - q5
        try:
            r.incr("trail:conformal_band_applied_count")
        except Exception:
            pass
        return ExitDecision(sl_floor=floor,
                            notes={"q5_residual": q5})
    else:
        ceiling = mark + q5
        try:
            r.incr("trail:conformal_band_applied_count")
        except Exception:
            pass
        return ExitDecision(sl_ceiling=ceiling,
                            notes={"q5_residual": q5})


# ─────────────────────────────────────────────────────────────────────────
# 8. Mamba SSM multi-horizon quantile exit
# ─────────────────────────────────────────────────────────────────────────
def evaluate_mamba_quantiles(trade, mark, r, sl_level, direction) -> Optional[ExitDecision]:
    """Read Mamba forecaster q5 / q95 predictions at 5-min horizon and use
    them as adaptive SL/TP.

    The existing ml.mamba_forecaster publishes per-pair quantile forecasts:
      `mamba:forecast_q5:{pair}:5m`  (lower 5%)
      `mamba:forecast_q95:{pair}:5m` (upper 95%)

    For LONG: SL floor = q5 (don't let SL widen below the model's worst-case)
    For SHORT: SL ceiling = q95 (similarly)

    Source: CryptoMamba arXiv:2501.01010 (ICLR 2025).
    """
    pair = trade["pair"]
    q5_raw  = r.get(f"mamba:forecast_q5:{pair}:5m")
    q95_raw = r.get(f"mamba:forecast_q95:{pair}:5m")
    if q5_raw is None and q95_raw is None:
        return None
    try:
        q5  = float(q5_raw)  if q5_raw  is not None else None
        q95 = float(q95_raw) if q95_raw is not None else None
    except (TypeError, ValueError):
        return None
    if direction == "long" and q5 is not None and 0 < q5 < mark:
        try:
            r.incr("trail:mamba_quantile_applied_count")
        except Exception:
            pass
        return ExitDecision(sl_floor=q5,
                            notes={"q5_price": q5})
    if direction == "short" and q95 is not None and q95 > mark > 0:
        try:
            r.incr("trail:mamba_quantile_applied_count")
        except Exception:
            pass
        return ExitDecision(sl_ceiling=q95,
                            notes={"q95_price": q95})
    return None


# ─────────────────────────────────────────────────────────────────────────
# 9. Diffusion model generative exit distribution
# ─────────────────────────────────────────────────────────────────────────
def evaluate_diffusion_band(trade, mark, r, sl_level, direction) -> Optional[ExitDecision]:
    """Read diffusion forecast ensemble quantiles for adaptive bands.

    Diffusion is the most experimental of the three — 2025 papers note it
    doesn't yet beat top conventional forecasters consistently. Treated as
    an advisory layer: if both Conformal AND Mamba are missing, fall back to
    Diffusion's q5/q95 bands.

    Reads:
      `diffusion:forecast_q5:{pair}:5m`
      `diffusion:forecast_q95:{pair}:5m`

    Source: arXiv:2509.02308 Diffusion Models for Financial Charts (Sep 2025).
    """
    pair = trade["pair"]
    q5_raw  = r.get(f"diffusion:forecast_q5:{pair}:5m")
    q95_raw = r.get(f"diffusion:forecast_q95:{pair}:5m")
    if q5_raw is None and q95_raw is None:
        return None
    try:
        q5  = float(q5_raw)  if q5_raw  is not None else None
        q95 = float(q95_raw) if q95_raw is not None else None
    except (TypeError, ValueError):
        return None
    # Diffusion only applies WHEN the other quantile sources are missing —
    # otherwise the fold rules in decision.py would let it widen good SLs.
    has_other = (r.get(f"mamba:forecast_q5:{trade['pair']}:5m") is not None
                 or r.get(f"conformal:residual_q5:{trade['pair']}") is not None)
    if has_other:
        return None
    if direction == "long" and q5 is not None and 0 < q5 < mark:
        try:
            r.incr("trail:diffusion_band_applied_count")
        except Exception:
            pass
        return ExitDecision(sl_floor=q5,
                            notes={"diffusion_q5": q5, "source": "fallback"})
    if direction == "short" and q95 is not None and q95 > mark > 0:
        try:
            r.incr("trail:diffusion_band_applied_count")
        except Exception:
            pass
        return ExitDecision(sl_ceiling=q95,
                            notes={"diffusion_q95": q95, "source": "fallback"})
    return None
