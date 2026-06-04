"""cont. 60 — SL placement modifiers (NOT exits — these adjust where SL sits).

  * DVOL scaler: scale SL distance by Deribit's DVOL (implied volatility index)
    decile vs 252d median. Top decile = wide & switch to time-stops; bottom
    decile = tighten.
  * Liquidation heatmap dark-side SL: place SL on the LOW-density side of
    the nearest Coinglass cluster, not in/before the cluster.

These return apply-by-side modifiers rather than ExitDecision — they're called
during initial SL placement (compute_initial_sl) and during trail update, not
as evaluators in the main exit decision chain.
"""
from __future__ import annotations
from typing import Optional
import structlog

log = structlog.get_logger()


# ─────────────────────────────────────────────────────────────────────────
# 10. DVOL-conditioned SL scaler
# ─────────────────────────────────────────────────────────────────────────
def apply_dvol_scaling(base_sl_distance: float, mark: float, r) -> tuple[float, dict]:
    """Scale SL distance by current DVOL decile vs rolling 252d median.

    DVOL = Deribit Volatility Index (BTC/ETH implied vol). When DVOL is in
    the BOTTOM decile (calm regime), the SL distance can tighten — smaller
    moves are expected. When DVOL is in the TOP decile (turbulence), widen
    the SL to survive the volatility, and the time-stop becomes the primary
    exit instead.

    Reads:
      `external:dvol:current`          — current DVOL (annualized %)
      `external:dvol:median_252d`      — rolling 252-day median DVOL
      `external:dvol:decile_current`   — 0-9 decile of current vs history

    Multipliers (research-recommended):
      decile 0   : 0.7×  (tighten — calm market)
      decile 1-2 : 0.85×
      decile 3-7 : 1.0×  (neutral)
      decile 8   : 1.20×
      decile 9   : 1.50× (widen — extreme vol)

    Returns (new_distance, telemetry_dict).
    """
    dvol_decile_raw = r.get("external:dvol:decile_current")
    if dvol_decile_raw is None:
        return base_sl_distance, {"dvol_scaler": "no_data"}
    try:
        decile = int(float(dvol_decile_raw))
    except (TypeError, ValueError):
        return base_sl_distance, {"dvol_scaler": "parse_error"}

    if decile == 0:
        mult = 0.70
    elif decile in (1, 2):
        mult = 0.85
    elif decile in (8,):
        mult = 1.20
    elif decile == 9:
        mult = 1.50
    else:
        mult = 1.0

    new_distance = base_sl_distance * mult
    if mult != 1.0:
        try:
            r.incr("trail:dvol_scaling_applied_count")
            r.set("trail:dvol_last_mult", round(mult, 3))
        except Exception:
            pass
    return new_distance, {"dvol_decile": decile, "dvol_mult": mult}


# ─────────────────────────────────────────────────────────────────────────
# 11. Liquidation heatmap dark-side SL placement
# ─────────────────────────────────────────────────────────────────────────
def apply_liquidation_dark_side(raw_sl: float, mark: float, direction: str,
                                pair: str, r) -> tuple[float, dict]:
    """Snap SL to the "dark side" of the nearest liquidation cluster.

    Coinglass heatmap shows liquidation density clusters. Price magnetically
    pulls into bright clusters (high density). Placing SL INSIDE / right
    BEFORE a cluster guarantees you get stop-hunted. Place it on the FAR side
    (dark zone) instead.

    Algorithm:
      1. Get nearest cluster top/bottom in the direction of the trade's loss
         (above mark for short, below mark for long).
      2. If the proposed SL falls inside `[cluster_bottom, cluster_top + 1.5×ATR]`,
         move it to `cluster_top + 1.5×ATR` (short) or `cluster_bottom - 1.5×ATR`
         (long).

    Reads:
      `liquidation:nearest_above:{pair}`   {top, bottom, density}
      `liquidation:nearest_below:{pair}`   {top, bottom, density}
      `{pair}:atr`                          (existing key)

    The existing data/liquidation_levels.py publishes the cluster data per
    pair every 5 minutes via the Celery `liquidation_levels_refresh_task`.

    Source: Glassnode Liquidation Heatmaps, Coinglass (2025).
    """
    import json
    if direction == "long":
        cluster_raw = r.get(f"liquidation:nearest_below:{pair}")
    else:
        cluster_raw = r.get(f"liquidation:nearest_above:{pair}")
    if cluster_raw is None:
        return raw_sl, {"liq_dark_side": "no_cluster_data"}
    try:
        cluster = json.loads(cluster_raw) if isinstance(cluster_raw, (str, bytes)) else cluster_raw
        c_top    = float(cluster.get("top", 0))
        c_bottom = float(cluster.get("bottom", 0))
        density  = float(cluster.get("density", 0))
    except (json.JSONDecodeError, TypeError, ValueError, KeyError):
        return raw_sl, {"liq_dark_side": "parse_error"}
    if c_top <= 0 or c_bottom <= 0 or density < 0.3:
        return raw_sl, {"liq_dark_side": "low_density"}

    atr_raw = r.get(f"{pair}:atr")
    try:
        atr = float(atr_raw) if atr_raw is not None else mark * 0.005
    except (TypeError, ValueError):
        atr = mark * 0.005
    dark_offset = 1.5 * atr

    new_sl = raw_sl
    snapped = False
    if direction == "long":
        # Long SL is below mark. If raw_sl is inside [c_bottom-1.5atr, c_top],
        # snap it to c_bottom - 1.5*atr (dark side, below cluster).
        zone_top    = c_top
        zone_bottom = c_bottom - dark_offset
        if zone_bottom <= raw_sl <= zone_top:
            new_sl = round(c_bottom - dark_offset, 8)
            snapped = True
    else:
        # Short SL is above mark. Dark side is c_top + 1.5*atr (above cluster).
        zone_top    = c_top + dark_offset
        zone_bottom = c_bottom
        if zone_bottom <= raw_sl <= zone_top:
            new_sl = round(c_top + dark_offset, 8)
            snapped = True

    if snapped:
        try:
            r.incr("trail:liq_dark_side_snap_count")
        except Exception:
            pass

    return new_sl, {"liq_dark_side": "snapped" if snapped else "ok",
                    "cluster_top": c_top, "cluster_bottom": c_bottom,
                    "density": density, "raw_sl": raw_sl, "new_sl": new_sl}
