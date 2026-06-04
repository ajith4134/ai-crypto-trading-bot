"""Seed the strategy gene pool with 27 hand-curated archetypes.

Run via: docker compose exec brain python -m tools.seed_gene_pool

Idempotent — checks SELECT id FROM strategies WHERE name=%s first; skips if exists.
Each archetype is saved via strategy.lifecycle.create_experimental then promoted
to 'active' via strategy.lifecycle.promote_to_active.

See /opt/trading-bot/next_impl/seed_gene_pool.md for full design rationale.
"""
from __future__ import annotations

import json
import sys
import structlog

from db import db_conn
from strategy.lifecycle import create_experimental, promote_to_active

log = structlog.get_logger()


# DCA hard-off per [[feedback_dca_disabled]] — round_1_pct=-50, round_2_pct=-80
# means triggers never fire on any realistic adverse move.
DCA_DISABLED = {"round_1_pct": -50, "round_2_pct": -80}


def _typed(
    name: str,
    *,
    atr_mult: float,
    min_pct: float,
    trail_pct: float,
    cap_mult: float,
    min_sig: int,
    turb_cap: float,
    regimes: list[str],
    entry_extra: dict | None = None,
) -> dict:
    """Build the typed-config payload for one archetype."""
    overrides = {
        "min_signal_strength": int(min_sig),
        "turbulence_cap": float(turb_cap),
        "regime_whitelist": list(regimes),
    }
    if entry_extra:
        overrides.update(entry_extra)
    return {
        "name": name,
        "source": "brain",
        "code": _stub_code(name),
        "position_sizing_rules": {"capital_pct_mult": float(cap_mult)},
        "dca_rules": dict(DCA_DISABLED),
        "trailing_sl_params": {
            "initial_atr_mult": float(atr_mult),
            "initial_min_pct": float(min_pct),
            "trailing_dist_pct": float(trail_pct),
        },
        "entry_overrides": overrides,
        "generation": 0,
        "parent_strategy_id": None,
    }


def _stub_code(name: str) -> str:
    """Minimal .py stub. F8 router uses typed columns for behavior; this file
    exists for filesystem audit + provenance (per S-02 dual-save invariant)."""
    return (
        f"# Seed archetype: {name}\n"
        f"# Behavior is driven by typed-config columns in the strategies table.\n"
        f"# See /opt/trading-bot/next_impl/seed_gene_pool.md for mechanism notes.\n"
        f"# Routed via strategy/router.py — entry_overrides + trailing_sl_params + "
        f"position_sizing_rules + dca_rules.\n"
        f"ARCHETYPE_NAME = {name!r}\n"
    )


# ============================================================================
# All 27 archetypes
# ============================================================================

ARCHETYPES: list[dict] = [
    # --- A1. Trend / momentum / breakout (6) -------------------------------
    _typed("donchian_breakout",         atr_mult=3.5, min_pct=0.025, trail_pct=0.04,  cap_mult=1.0, min_sig=35, turb_cap=4.0, regimes=["bull","bear"]),
    _typed("turtle_system",             atr_mult=4.0, min_pct=0.03,  trail_pct=0.05,  cap_mult=1.0, min_sig=40, turb_cap=3.5, regimes=["bull","bear"]),
    _typed("supertrend_flip",           atr_mult=3.0, min_pct=0.02,  trail_pct=0.035, cap_mult=1.1, min_sig=30, turb_cap=4.0, regimes=["bull","bear"]),
    _typed("vwap_trend_session",        atr_mult=2.5, min_pct=0.02,  trail_pct=0.03,  cap_mult=1.0, min_sig=30, turb_cap=5.0, regimes=["bull","bear","turbulent"]),
    _typed("dual_momentum",             atr_mult=3.5, min_pct=0.025, trail_pct=0.04,  cap_mult=1.2, min_sig=40, turb_cap=3.0, regimes=["bull","bear"]),
    _typed("bollinger_keltner_squeeze", atr_mult=3.0, min_pct=0.02,  trail_pct=0.04,  cap_mult=1.0, min_sig=35, turb_cap=4.0, regimes=["bull","bear"]),

    # --- A2. Mean reversion / stat arb (3) ---------------------------------
    _typed("avellaneda_pca_residual_revert", atr_mult=1.75, min_pct=0.015, trail_pct=0.018, cap_mult=0.9, min_sig=30, turb_cap=5.0, regimes=["bull","bear","turbulent"]),
    _typed("cvd_divergence_revert",          atr_mult=2.0,  min_pct=0.015, trail_pct=0.02,  cap_mult=1.0, min_sig=35, turb_cap=4.0, regimes=["bull","bear","turbulent"]),
    _typed("hurst_gated_revert",             atr_mult=2.0,  min_pct=0.02,  trail_pct=0.022, cap_mult=0.9, min_sig=35, turb_cap=5.0, regimes=["bear","turbulent"]),

    # --- A3. Crypto-perp native (6) ----------------------------------------
    _typed("funding_extreme_fade",       atr_mult=2.0, min_pct=0.018, trail_pct=0.025, cap_mult=1.0, min_sig=30, turb_cap=6.0, regimes=["bull","bear","turbulent"]),
    _typed("premium_index_z_fade",       atr_mult=2.0, min_pct=0.015, trail_pct=0.02,  cap_mult=1.0, min_sig=30, turb_cap=5.0, regimes=["bull","bear","turbulent"]),
    _typed("liquidation_cascade_fade",   atr_mult=1.5, min_pct=0.02,  trail_pct=0.025, cap_mult=1.3, min_sig=25, turb_cap=8.0, regimes=["bull","bear","turbulent"]),
    _typed("post_liq_reversion",         atr_mult=2.0, min_pct=0.02,  trail_pct=0.03,  cap_mult=1.2, min_sig=30, turb_cap=6.0, regimes=["bull","bear","turbulent"]),
    _typed("oi_price_divergence",        atr_mult=2.5, min_pct=0.02,  trail_pct=0.03,  cap_mult=1.0, min_sig=35, turb_cap=4.0, regimes=["bull","bear"]),
    _typed("exchange_netflow_inflow_fade", atr_mult=2.5, min_pct=0.025, trail_pct=0.035, cap_mult=0.9, min_sig=35, turb_cap=4.0, regimes=["bull","bear"]),

    # --- B. Microstructure / order flow (5) --------------------------------
    _typed("classical_ofi_cont",        atr_mult=2.0,  min_pct=0.015, trail_pct=0.018, cap_mult=1.0, min_sig=30, turb_cap=5.0, regimes=["bull","bear","turbulent"]),
    _typed("depth_weighted_ofi",        atr_mult=2.0,  min_pct=0.015, trail_pct=0.02,  cap_mult=1.0, min_sig=35, turb_cap=5.0, regimes=["bull","bear","turbulent"]),
    _typed("microprice_gradient",       atr_mult=1.75, min_pct=0.012, trail_pct=0.015, cap_mult=1.0, min_sig=30, turb_cap=5.0, regimes=["bull","bear","turbulent"]),
    _typed("hawkes_lambda_spike_ride",  atr_mult=2.5,  min_pct=0.02,  trail_pct=0.025, cap_mult=1.1, min_sig=35, turb_cap=7.0, regimes=["bull","bear","turbulent"]),
    _typed("swing_sweep_fade",          atr_mult=1.5,  min_pct=0.015, trail_pct=0.018, cap_mult=1.0, min_sig=30, turb_cap=5.0, regimes=["bull","bear","turbulent"]),

    # --- C. ML-augmented overlays (3) --------------------------------------
    _typed("patchtst_direction_confirmer",  atr_mult=2.5, min_pct=0.02,  trail_pct=0.025, cap_mult=1.00, min_sig=40, turb_cap=4.0, regimes=["bull","bear"]),
    _typed("tft_regime_conditioned_overlay", atr_mult=2.5, min_pct=0.02,  trail_pct=0.025, cap_mult=1.00, min_sig=40, turb_cap=4.0, regimes=["bull","bear"]),
    _typed("gnn_contagion_risk_off",        atr_mult=2.0, min_pct=0.018, trail_pct=0.022, cap_mult=0.85, min_sig=40, turb_cap=3.5, regimes=["bull","bear"]),

    # --- D. Risk overlays / gates (2) — piggy-back mode ---------------------
    _typed(
        "vol_target_sizing_overlay",
        atr_mult=2.0, min_pct=0.015, trail_pct=0.02, cap_mult=1.0,
        min_sig=30, turb_cap=99.0, regimes=["bull","bear","turbulent"],
        entry_extra={
            "overlay_type": "sizing",
            "target_realized_vol_pct": 0.20,
            "vol_lookback_bars": 1440,
        },
    ),
    _typed(
        "hmm_regime_gate_overlay",
        atr_mult=2.0, min_pct=0.015, trail_pct=0.02, cap_mult=1.0,
        min_sig=30, turb_cap=99.0, regimes=["bull","bear","turbulent"],
        entry_extra={
            "overlay_type": "regime_gate",
            "kill_in_turbulent": True,
            "hmm_confidence_min": 0.7,
        },
    ),

    # --- E. Stat-arb anchors (2) — added 2026-05-29 -------------------------
    _typed("kalman_pair_residual_revert", atr_mult=1.5, min_pct=0.012, trail_pct=0.015, cap_mult=0.9, min_sig=30, turb_cap=4.0, regimes=["bull","bear"]),
    _typed("transfer_entropy_lead_lag",   atr_mult=2.0, min_pct=0.015, trail_pct=0.02,  cap_mult=1.0, min_sig=30, turb_cap=4.0, regimes=["bull","bear"]),
]


def _name_exists(name: str) -> str | None:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM strategies WHERE name = %s", (name,))
            row = cur.fetchone()
    return row[0] if row else None


def main() -> int:
    assert len(ARCHETYPES) == 27, f"expected 27 archetypes, got {len(ARCHETYPES)}"
    created = 0
    skipped = 0
    promoted = 0
    failed = 0

    for arc in ARCHETYPES:
        name = arc["name"]
        existing_id = _name_exists(name)
        if existing_id:
            log.info("seed_skip_exists", name=name, id=str(existing_id))
            skipped += 1
            continue
        try:
            sid = create_experimental(arc)
            created += 1
            promote_to_active(sid)
            promoted += 1
            log.info("seed_promoted", name=name, id=sid)
        except Exception as exc:
            failed += 1
            log.error("seed_failed", name=name, error=str(exc)[:200])

    log.info("seed_summary",
             total=len(ARCHETYPES),
             created=created, promoted=promoted,
             skipped=skipped, failed=failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
